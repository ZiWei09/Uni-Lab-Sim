import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from opcua import Client, ua

from iccas_device import ERRORS, PREFIX


@pytest.fixture
def equipment_server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port > 65529:
        pytest.skip("Port out of CLI range")
    script = Path(__file__).parents[1] / "iccas_sim.py"
    endpoint = f"opc.tcp://127.0.0.1:{port}/iccas_sim/"
    processes = []
    logs = []

    def launch():
        state = tmp_path / f"view-{len(processes)}.json"
        log = (tmp_path / f"server-{len(processes)}.log").open("w+")
        logs.append(log)
        process = subprocess.Popen([sys.executable, str(script), "--station", "filter", "--base-port", str(port - 1),
                                    "--device-model", "--device-state-dir", str(tmp_path / "journal"),
                                    "--delay", "0.5", "--watchdog-seconds", "0.4", "--state-file", str(state)],
                                   stdout=log, stderr=log)
        processes.append(process)
        deadline = time.monotonic() + 15
        while not state.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.03)
        log.seek(0)
        assert state.exists(), log.read()
        return process

    try:
        yield endpoint, launch
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()


class Wire:
    def __init__(self, endpoint):
        self.client = Client(endpoint)
        self.client.connect()

    def node(self, name):
        return self.client.get_node("ns=4;s=上位机通讯|" + name)

    def read(self, name):
        return self.node(name).get_value()

    def write(self, name, value):
        node = self.node(name)
        node.set_value(ua.Variant(value, node.get_data_type_as_variant_type()))

    def wait(self, name, expected):
        deadline = time.monotonic() + 4
        while self.read(name) != expected and time.monotonic() < deadline:
            time.sleep(0.025)
        assert self.read(name) == expected

    def start(self, base, parameters=None, command=1):
        for name, value in (parameters or {}).items():
            self.write(name, value)
        self.write(PREFIX + base + ".命令号", command)
        self.write(base + "触发PCtoPLC", True)

    def close(self):
        self.client.disconnect()


def test_real_disconnect_continues_and_result_survives_reconnect(equipment_server):
    endpoint, launch = equipment_server
    launch()
    wire = Wire(endpoint)
    try:
        with pytest.raises(ua.UaStatusCodeError):
            wire.write("烧杯仓取料完成PLCtoPC", True)
        with pytest.raises(ua.UaStatusCodeError):
            wire.write("烧杯仓位置1", False)
        wire.start("烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1})
        wire.wait(PREFIX + "烧杯仓取料.状态", "running")
    finally:
        wire.close()
    time.sleep(0.7)
    wire = Wire(endpoint)
    try:
        wire.wait("烧杯仓取料完成PLCtoPC", True)
        assert wire.read("烧杯仓位置1") is False
        assert wire.read("机器人夹爪状态PLCtoPC") == 1
        wire.write("烧杯仓取料触发PCtoPLC", False)
        wire.wait("烧杯仓取料完成PLCtoPC", False)
        wire.start("烧杯仓取料", command=1)
        wire.wait(PREFIX + "烧杯仓取料.错误码", ERRORS["COMMAND"])
        assert json.loads(wire.read(PREFIX + "烧杯仓取料.上次结果JSON"))["state"] == "complete"
    finally:
        wire.close()


def test_real_heartbeat_disconnect_and_opcua_recovery(equipment_server):
    endpoint, launch = equipment_server
    launch()
    wire = Wire(endpoint)
    try:
        wire.write(PREFIX + "心跳监控启用", True)
        wire.write(PREFIX + "控制心跳", 1)
        wire.start("烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1})
        wire.wait(PREFIX + "烧杯仓取料.状态", "running")
    finally:
        wire.close()
    time.sleep(0.6)
    wire = Wire(endpoint)
    try:
        wire.wait(PREFIX + "需要恢复", True)
        assert wire.read(PREFIX + "烧杯仓取料.错误码") == ERRORS["HEARTBEAT"]
        assert not wire.read("烧杯仓取料完成PLCtoPC")
        world = json.loads(wire.read(PREFIX + "物料位置JSON"))
        assert world["uncertain"]
        wire.write("烧杯仓取料触发PCtoPLC", False)
        wire.write(PREFIX + "心跳监控启用", False)
        wire.write(PREFIX + "恢复位置JSON", json.dumps(world["locations"]))
        wire.write(PREFIX + "恢复请求号", 1)
        wire.wait(PREFIX + "恢复应答号", 1)
        assert not wire.read(PREFIX + "需要恢复")
        wire.start("烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1}, command=2)
        wire.wait("烧杯仓取料完成PLCtoPC", True)
    finally:
        wire.close()


def test_actual_process_crash_restores_uncertain_operation(equipment_server):
    endpoint, launch = equipment_server
    process = launch()
    wire = Wire(endpoint)
    wire.start("烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1})
    wire.wait(PREFIX + "烧杯仓取料.状态", "running")
    process.kill()
    process.wait(timeout=5)
    try:
        wire.close()
    except Exception:
        pass
    launch()
    restored = Wire(endpoint)
    try:
        assert restored.read(PREFIX + "需要恢复")
        assert restored.read(PREFIX + "烧杯仓取料.错误码") == ERRORS["RESTART"]
        assert not restored.read("烧杯仓取料完成PLCtoPC")
        assert restored.read(PREFIX + "烧杯仓取料.已接受命令号") == 1
    finally:
        restored.close()
