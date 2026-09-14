import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from opcua import Client, ua

from iccas_sim import (FILTER_EXTENSION_MARKER, Station, channels, definitions,
                       load_faults, topology)
from server import add_nodes, build_server, register_ns_padding


@pytest.fixture
def make_station():
    def make(kind):
        server = build_server("opc.tcp://127.0.0.1:0/iccas_sim/")
        ns = register_ns_padding(server, 4, "urn:iccas:sim")
        return Station(kind, add_nodes(server, ns, definitions(kind)), delay=0.1)
    return make


@pytest.mark.parametrize("kind,count,handshakes", [("spray", 173, 13), ("filter", 158, 18),
                                                      ("body", 17, 2), ("reaction", 45, 3)])
def test_contract(kind, count, handshakes):
    defs = definitions(kind)
    assert len(defs) == count
    assert len(channels(kind, {d.name_cn for d in defs})) == handshakes


def test_reference_snapshots():
    root = os.environ.get("ICCAS_REFERENCE_ROOT")
    if not root:
        pytest.skip("Set ICCAS_REFERENCE_ROOT to check supplier table drift")
    for path in (Path(__file__).parents[1] / "data").glob("iccas_*.csv"):
        source = Path(root) / "unilab_iccas/config/address_tables" / path.name.removeprefix("iccas_")
        assert path.read_bytes() == source.read_bytes()


def test_ab_parallel_and_latched_complete(make_station):
    st = make_station("spray")
    pair = [ch for ch in st.channels if "后处理雾化" in ch.trigger]
    for ch in pair:
        st.write(ch.trigger, True)
    st.tick(0)
    assert all(ch.phase == "running" for ch in pair)
    st.tick(0.2)
    st.tick(20)
    assert all(st.read(ch.complete) and ch.count == 1 for ch in pair)
    st.write(pair[0].trigger, False)
    st.tick(21)
    assert not st.read(pair[0].complete)
    assert st.read(pair[1].complete)


def test_alarm_cancel_fault_and_recovery(make_station):
    st = make_station("filter")
    ch = st.channels[0]
    st.write("门开报警PLCtoPC", True)
    st.write(ch.trigger, True)
    st.tick(0)
    assert ch.phase == "rejected" and not st.read(ch.complete)
    st.write("门开报警PLCtoPC", False)
    st.tick(1)
    assert ch.phase == "rejected"  # must reset before retry
    st.write(ch.trigger, False)
    st.tick(2)
    st.write(ch.trigger, True)
    st.tick(3, {ch.trigger: "hang"})
    st.tick(100)
    assert ch.phase == "hang" and not st.read(ch.complete)
    st.write(ch.trigger, False)
    st.tick(101)
    st.write(ch.trigger, True)
    st.tick(102)
    st.write(ch.trigger, False)  # client timeout/cancel
    st.tick(103)
    assert ch.phase == "idle" and ch.count == 0


def test_millisecond_duration_and_missing_handshakes(make_station):
    st = make_station("filter")
    assert "烧杯仓放料触发PCtoPLC" not in st.nodes
    assert "真空箱取料触发PCtoPLC" not in st.nodes
    ch = next(ch for ch in st.channels if ch.trigger.startswith("抽原液启动"))
    st.write("原液抽取时间PCtoPLC", 2000)
    st.write(ch.trigger, True)
    st.tick(0)
    st.tick(1)
    assert not st.read(ch.complete)
    st.tick(2)
    assert st.read(ch.complete)


def test_synthesis_busy_running_reset_and_invalid_program(make_station):
    st = make_station("body")
    init, robot = st.channels
    st.write(robot.trigger, True)
    st.tick(0)
    assert robot.phase == "rejected"
    st.write(robot.trigger, False)
    st.tick(1)
    st.write("PC_TO_PLC.机器人目标位置代码", 27)
    st.write("PC_TO_PLC.机器人目标机台编号", 2)
    st.write(robot.trigger, True)
    st.tick(2)
    assert st.read(robot.running) and not st.read("PLC_TO_PC.设备空闲")
    st.write(init.trigger, True)
    st.tick(3)
    assert init.phase == "rejected"
    assert st.read(robot.complete) and st.read(robot.running)
    st.write(robot.trigger, False)
    st.tick(4)
    assert not st.read(robot.complete) and not st.read(robot.running)
    assert st.read("PLC_TO_PC.设备空闲")


def test_fault_validation(make_station, tmp_path):
    path = tmp_path / "faults.json"
    path.write_text(json.dumps({"spray": {"typo": "hang"}}))
    with pytest.raises(ValueError, match="Unknown trigger"):
        load_faults(path, {"spray": make_station("spray")})


@pytest.mark.parametrize("kind,extensions", [("spray", False), ("filter", False),
                                             ("body", False), ("reaction", False), ("filter", True)])
def test_real_opcua_roundtrip(kind, extensions):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"opc.tcp://127.0.0.1:{port}/iccas_sim/"
    server = build_server(endpoint)
    ns = register_ns_padding(server, 4, "urn:iccas:sim")
    defs = definitions(kind, filter_extensions=extensions)
    st = Station(kind, add_nodes(server, ns, defs), delay=0.01)
    client = Client(endpoint)
    server.start()
    try:
        client.connect()
        ids = {d.name_cn: d.node_id for d in defs}
        # Exercise every mapped handshake, including both spray lanes.
        parameters = {"PC_TO_PLC.机器人目标位置代码": 27, "PC_TO_PLC.机器人目标机台编号": 1,
                      "PC_TO_PLC.移液来源": 1, "PC_TO_PLC.移液目标": 1, "PC_TO_PLC.移液量": 1.0,
                      "烧杯仓放料号码PCtoPLC": 12, "真空箱取料号码PCtoPLC": 1}
        for name, value in parameters.items():
            if name in ids:
                node = client.get_node(ids[name])
                node.set_value(ua.Variant(value, node.get_data_type_as_variant_type()))
        for ch in st.channels:
            trigger, complete = client.get_node(ids[ch.trigger]), client.get_node(ids[ch.complete])
            trigger.set_value(True)
            st.tick(0)
            if ch.running:
                assert client.get_node(ids[ch.running]).get_value()
            st.tick(1)
            assert complete.get_value(), ch.trigger
            trigger.set_value(False)
            st.tick(2)
            assert not complete.get_value()
    finally:
        client.disconnect()
        server.stop()


def test_topology():
    assert [row[2] for row in topology()] == list(range(4855, 4863))
    assert len(topology("synthesis")) == 6


def test_cli_eight_endpoints_and_shutdown(tmp_path):
    # Reserve a complete range first, then release it for the child process.
    sockets = []
    for base in range(24000, 40000, 8):
        try:
            for port in range(base, base + 8):
                sock = socket.socket()
                sockets.append(sock)
                sock.bind(("127.0.0.1", port))
            break
        except OSError:
            for sock in sockets:
                sock.close()
            sockets.clear()
    else:
        pytest.fail("No available eight-port range")
    for sock in sockets:
        sock.close()
    script = Path(__file__).parents[1] / "iccas_sim.py"
    state = tmp_path / "state.json"
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen([sys.executable, str(script), "--base-port", str(base),
                                    "--state-file", str(state), "--delay", "0.05", "--filter-extensions"],
                                   stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 45
            while not state.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            log.seek(0)
            assert state.exists(), log.read()
            assert sum(st["node_count"] for st in json.loads(state.read_text()).values()) == 580
            for name, kind, port, number in topology(base_port=base):
                client = Client(f"opc.tcp://127.0.0.1:{port}/iccas_sim/")
                client.connect()
                try:
                    if kind == "filter":
                        marker = client.get_node(f"ns=4;s=上位机通讯|{FILTER_EXTENSION_MARKER}")
                        assert marker.get_value() == "filter-return-v1"
                        with pytest.raises(ua.UaStatusCodeError):
                            marker.set_value("tampered")
                    defs = definitions(kind, number)
                    ids = {d.name_cn: d.node_id for d in defs}
                    ch = channels(kind, set(ids))[0]
                    trigger = client.get_node(ids[ch.trigger])
                    complete = client.get_node(ids[ch.complete])
                    trigger.set_value(True)
                    deadline = time.monotonic() + 3
                    while not complete.get_value() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    assert complete.get_value(), name
                    trigger.set_value(False)
                    deadline = time.monotonic() + 3
                    while complete.get_value() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    assert not complete.get_value(), name
                finally:
                    client.disconnect()
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        assert process.returncode == 0
        for port in range(base, base + 8):
            with socket.socket() as sock:
                assert sock.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.parametrize("base", ["烧杯仓放料", "真空箱取料"])
@pytest.mark.parametrize("position", [0, 13])
def test_extensions_reject_out_of_range_positions(base, position):
    server = build_server("opc.tcp://127.0.0.1:0/iccas_sim/")
    ns = register_ns_padding(server, 4, "urn:iccas:sim")
    st = Station("filter", add_nodes(server, ns, definitions("filter", filter_extensions=True)))
    st.write(base + "号码PCtoPLC", position)
    st.write(base + "触发PCtoPLC", True)
    st.tick(0)
    st.tick(10)
    assert not st.read(base + "完成PLCtoPC")
    assert next(ch for ch in st.channels if ch.trigger == base + "触发PCtoPLC").phase == "rejected"
