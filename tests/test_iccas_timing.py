import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from opcua import Client

from iccas_sim import Station, definitions, topology
from iccas_timing import ProcessTiming, demo_recipe
from server import add_nodes, build_server, register_ns_padding


def make_station(kind):
    server = build_server("opc.tcp://127.0.0.1:0/test/")
    ns = register_ns_padding(server, 4, "urn:iccas:sim")
    st = Station(kind, add_nodes(server, ns, definitions(kind, filter_extensions=True)),
                 timing_mode="process", time_scale=60)
    for name, value in demo_recipe(kind).items():
        st.write(name, value)
    return st


def channel(st, base):
    return next(ch for ch in st.channels if ch.trigger == base + "触发PCtoPLC")


@pytest.mark.parametrize("kind,base,process_seconds", [
    ("spray", "A工位后处理雾化", 50 + 600 + 18000 + 1200 + 50 + 300 + 1200),
    ("spray", "B工位搅拌桨清洗", 50 + 30 + 30 + 15),
    ("spray", "移液针清洗", 180),
    ("filter", "抽原液启动", 1200 + 30),
    ("filter", "真空箱除泡启动", 180 + 7920),
    ("filter", "吸液针外壁清洗", 30 + 15),
    ("filter", "原液管清洗回收罐清洗", 30 + 15 + 60 + 30 + 30 + 15 + 30),
    ("filter", "烧杯仓放料", 30),
])
def test_duration_is_process_seconds_divided_by_60(kind, base, process_seconds):
    st = make_station(kind)
    assert st.duration(channel(st, base)) == pytest.approx(process_seconds / 60)


def test_recipe_latched_at_trigger_and_independent_ab():
    st = make_station("spray")
    a, b = channel(st, "A工位后处理雾化"), channel(st, "B工位后处理雾化")
    st.write("B工位抽滤前搅拌时间", 60_000)
    st.write(a.trigger, True)
    st.write(b.trigger, True)
    st.tick(0)
    assert a.process_duration_s - b.process_duration_s == 17940
    original_deadline = a.deadline
    st.write("A工位抽滤前搅拌时间", 0)
    st.tick(b.deadline)
    assert st.read(b.complete) and not st.read(a.complete)
    assert a.deadline == original_deadline
    st.tick(a.deadline - 0.001)
    assert not st.read(a.complete)
    st.tick(a.deadline)
    assert st.read(a.complete)
    st.write(a.trigger, False)
    st.tick(a.deadline + 1)
    assert not st.read(a.complete)
    st.write(a.trigger, True)
    st.tick(a.deadline + 2)
    assert a.process_duration_s == 3400  # next request sees edited zero aging


def test_progress_reports_hold_and_remaining_time():
    st = make_station("filter")
    ch = channel(st, "真空箱除泡启动")
    st.write(ch.trigger, True)
    st.tick(0)
    st.tick(63)
    state = next(c for c in st.snapshot()["channels"] if c["trigger"] == ch.trigger)
    assert state["current_stage"] == "真空除泡保持"
    assert state["remaining_wall_s"] == 72
    assert state["progress"] == pytest.approx(63 / 135)
    st.tick(135)
    assert st.read(ch.complete)


def test_all_postprocessing_channels_have_timing():
    for kind in ("spray", "filter"):
        st = make_station(kind)
        assert all(st.duration(ch) > 0 for ch in st.channels)
    assert [s[0] for s in topology("postprocessing")] == ["spray", "filter"]


def test_negative_time_rejected_without_completion():
    st = make_station("spray")
    ch = channel(st, "A工位后处理雾化")
    st.write("A工位抽滤前搅拌时间", -1)
    st.write(ch.trigger, True)
    st.tick(0)
    st.tick(1_000_000)
    assert ch.phase == "rejected" and not st.read(ch.complete)


def test_nonfinite_volume_rejected():
    st = make_station("spray")
    ch = channel(st, "A工位后处理雾化")
    st.write("A工位最开始加水量", float("nan"))
    with pytest.raises(ValueError, match="非负数"):
        st.duration(ch)


def test_zero_wash_cycles_do_not_consume_time():
    st = make_station("spray")
    st.write("A工位第1次粉末洗涤次数", 0)
    assert st.duration(channel(st, "A工位后处理雾化")) == pytest.approx(19850 / 60)


def test_profile_override_and_validation(tmp_path):
    import yaml
    profile = ProcessTiming().estimates.copy()
    profile["transfer_s"] = 45
    path = tmp_path / "timing.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "estimates": profile}))
    model = ProcessTiming(path)
    assert model.plan("filter", "真空箱取料触发PCtoPLC", lambda _: 0)[0]["process_seconds"] == 45
    profile["liquid_flow_ml_s"] = 0
    path.write_text(yaml.safe_dump({"version": 1, "estimates": profile}))
    with pytest.raises(ValueError):
        ProcessTiming(path)


def test_process_cli_seed_and_real_opcua_timing(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # CLI reserves room for all eight possible offsets.
    if port > 65528:
        pytest.skip("Ephemeral port outside CLI base-port range")
    state_file = tmp_path / "state.json"
    script = Path(__file__).parents[1] / "iccas_sim.py"
    with (tmp_path / "server.log").open("w+") as log:
        proc = subprocess.Popen([sys.executable, str(script), "--station", "spray", "--base-port", str(port),
                                 "--timing-mode", "process", "--time-scale", "60", "--seed-process-recipe",
                                 "--state-file", str(state_file)], stdout=log, stderr=log)
        client = None
        try:
            deadline = time.monotonic() + 15
            while not state_file.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            log.seek(0)
            assert state_file.exists(), log.read()
            client = Client(f"opc.tcp://127.0.0.1:{port}/iccas_sim/")
            client.connect()
            prefix = "ns=4;s=上位机通讯|"
            assert client.get_node(prefix + "A工位抽滤前搅拌时间").get_value() == 18_000_000
            trigger = client.get_node(prefix + "A工位搅拌桨清洗触发PCtoPLC")
            complete = client.get_node(prefix + "A工位搅拌桨清洗完成PLCtoPC")
            started = time.monotonic()
            trigger.set_value(True)
            time.sleep(0.7)
            assert not complete.get_value()  # no longer the old 0.5 second handshake
            while not complete.get_value() and time.monotonic() - started < 5:
                time.sleep(0.02)
            assert complete.get_value()
            assert time.monotonic() - started >= 125 / 60
            trigger.set_value(False)
            deadline = time.monotonic() + 2
            while complete.get_value() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not complete.get_value()
            state = json.loads(state_file.read_text())["spray"]
            assert state["timing_mode"] == "process" and state["time_scale"] == 60
        finally:
            if client:
                client.disconnect()
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        assert proc.returncode == 0
