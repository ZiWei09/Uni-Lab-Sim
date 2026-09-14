"""Local HMI bridge: two station panels backed by real OPC UA reads/writes."""
from __future__ import annotations

import argparse
import json
import math
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from opcua import Client, ua
from pydantic import BaseModel, Field

try:
    from .iccas_sim import definitions
    from .iccas_device import PREFIX, VERSION
    from .common import VTYPE_MAP
except ImportError:
    from iccas_sim import definitions
    from iccas_device import PREFIX, VERSION
    from common import VTYPE_MAP

ROOT = Path(__file__).parent
STATIC = ROOT / "gui/static"
MODE = "实验工位选择0_AB1_A2_BPCtoPLC"


class Panel:
    def __init__(self, kind, endpoint):
        self.kind, self.endpoint = kind, endpoint
        self.defs = {d.name_cn: d for d in definitions(kind, device_model=True)}
        self.actions = yaml.safe_load((ROOT / "config/iccas_devices.yaml").read_text())["stations"][kind]["actions"]
        self.lock = threading.RLock()
        self.client = None
        self.values = {}
        self.online = False
        self.error = "设备尚未连接"
        self.events = deque(maxlen=100)
        self.owned = {}
        self.last_states = {}
        self.boot = None
        self.updated = None
        self.stop = threading.Event()
        self.thread = None
        self.parameters = [name for name, d in self.defs.items() if not name.startswith(PREFIX)
                           and "PLCtoPC" not in name and "报警" not in name
                           and d.data_type != "BOOLEAN" and d.write_owner != "plc" and name != MODE]

    def log(self, message, **details):
        self.events.appendleft({"id": str(time.time_ns()), "time": time.strftime("%H:%M:%S"), "message": message, **details})

    def node(self, name):
        return self.client.get_node(self.defs[name].node_id)

    def write(self, name, value):
        data = ua.DataValue(ua.Variant(value, VTYPE_MAP[self.defs[name].data_type]))
        self.node(name).set_value(data)

    def disconnect(self):
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.online = False

    def refresh(self):
        if self.client is None:
            self.client = Client(self.endpoint, timeout=2)
            self.client.connect()
        values = self.client.get_values([self.node(name) for name in self.defs])
        self.values = dict(zip(self.defs, values))
        if self.values.get(PREFIX + "设备合同") != VERSION:
            raise ValueError("请启动 start_iccas_devices.command：此端点不是设备模式")
        boot = self.values[PREFIX + "启动标识"]
        if self.boot and self.boot != boot:
            self.owned.clear()
            self.log("设备已重启，请核对位置和未完成命令")
        self.boot = boot
        for base in self.actions:
            state = self.values[PREFIX + base + ".状态"]
            if self.last_states.get(base) != state:
                if base in self.last_states or state != "idle":
                    message = self.values[PREFIX + base + ".错误信息"]
                    self.log(f"{base} · {state}" + (f" · {message}" if message else ""),
                             action=base, state=state, code=self.values[PREFIX + base + ".错误码"], reason=message)
                self.last_states[base] = state
            command = self.owned.get(base)
            accepted = self.values[PREFIX + base + ".已接受命令号"]
            if command and state in {"complete", "error", "rejected"}:
                # Only acknowledge the request this HMI submitted. Never replay.
                if self.values[PREFIX + base + ".命令号"] == command and (state == "rejected" or accepted == command):
                    self.write(base + "触发PCtoPLC", False)
                    self.write(PREFIX + base + ".取消", False)
                self.owned.pop(base, None)
        self.online, self.error, self.updated = True, "", time.strftime("%H:%M:%S")

    def start(self):
        def loop():
            while not self.stop.is_set():
                with self.lock:
                    try:
                        self.refresh()
                    except Exception as exc:
                        self.error = str(exc)
                        self.disconnect()
                self.stop.wait(0.3 if self.online else 2)
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
        with self.lock:
            self.disconnect()

    def require_online(self):
        if not self.online:
            raise ValueError("设备离线，操作未发送")
        self.refresh()

    def typed(self, name, value):
        dtype = self.defs[name].data_type
        if dtype in {"INT16", "INT32", "FLOAT"}:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} 必须是有限数值")
            if dtype != "FLOAT":
                bits = 16 if dtype == "INT16" else 32
                if int(value) != value or not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
                    raise ValueError(f"{name} 超出整数范围")
                return int(value)
            return float(value)
        if not isinstance(value, str):
            raise ValueError(f"{name} 必须是字符串")
        return value

    def apply_parameters(self, values):
        if set(values) - set(self.parameters):
            raise ValueError("只允许写入配方参数，不能修改PLC反馈")
        converted = {name: self.typed(name, value) for name, value in values.items()}
        if self.values[PREFIX + "设备忙碌"] or self.values[PREFIX + "需要恢复"]:
            raise ValueError("设备忙碌或需要恢复，不能改写参数")
        for name, value in converted.items():
            self.write(name, value)

    def operate(self, operation, payload):
        with self.lock:
            try:
                self.require_online()
                base = payload.get("action")
                if operation in {"start", "cancel", "reset"} and base not in self.actions:
                    raise ValueError("未知动作")
                if operation == "parameters":
                    self.apply_parameters(payload.get("values", {}))
                    self.log("配方参数已写入")
                elif operation == "mode":
                    if self.kind != "spray" or type(payload.get("mode")) is not int or payload["mode"] not in (0, 1, 2):
                        raise ValueError("工位模式必须为0、1或2")
                    if self.values[PREFIX + "设备忙碌"] or self.values[PREFIX + "需要恢复"]:
                        raise ValueError("设备忙碌或需要恢复，不能切换工位")
                    self.write(MODE, payload["mode"])
                    self.log("工位模式已切换")
                elif operation == "start":
                    if self.values[PREFIX + "需要恢复"]:
                        raise ValueError("请先完成物料位置恢复")
                    if self.values[base + "触发PCtoPLC"] or self.values[base + "完成PLCtoPC"] or self.values[PREFIX + base + ".取消"]:
                        raise ValueError("该动作握手尚未复位")
                    if not self.values["远程模式PLCtoPC"] or not self.values["设备准备就绪PLCtoPC"]:
                        raise ValueError("设备不在远程就绪状态")
                    if self.kind == "spray":
                        mode = self.values[MODE]
                        if mode not in (0, 1, 2) or (mode == 1 and base.startswith("B工位")) or (mode == 2 and base.startswith("A工位")):
                            raise ValueError("该工位未启用")
                    # Per-action position may be set while another lane works.
                    parameter = self.actions[base].get("parameter")
                    position = payload.get("position")
                    if parameter:
                        if type(position) is not int or not 1 <= position <= 12:
                            raise ValueError("仓位号必须为1～12")
                        self.write(parameter, position)
                    command = max(self.values[PREFIX + base + ".已接受命令号"], self.values[PREFIX + base + ".命令号"]) + 1
                    if command > 2147483647:
                        raise ValueError("命令号已耗尽")
                    self.write(PREFIX + base + ".命令号", command)
                    self.owned[base] = command
                    self.write(base + "触发PCtoPLC", True)
                    self.log(f"已提交 {base} · #{command}")
                elif operation == "cancel":
                    self.write(PREFIX + base + ".取消", True)
                    self.log(f"已请求取消 {base}")
                elif operation == "reset":
                    if self.values[PREFIX + base + ".状态"] in {"running", "hang"}:
                        raise ValueError("执行中请使用取消")
                    self.write(base + "触发PCtoPLC", False)
                    self.write(PREFIX + base + ".取消", False)
                    self.owned.pop(base, None)
                elif operation == "recover":
                    if self.values[PREFIX + "设备忙碌"]:
                        raise ValueError("设备忙碌，不能恢复位置")
                    locations = payload.get("locations")
                    if not isinstance(locations, dict):
                        raise ValueError("必须提交完整位置对象")
                    if any(self.values[b + "触发PCtoPLC"] or self.values[PREFIX + b + ".取消"] for b in self.actions):
                        raise ValueError("请先复位全部动作握手")
                    request = self.values[PREFIX + "恢复已处理号"] + 1
                    if request > 2147483647:
                        raise ValueError("恢复请求号已耗尽")
                    self.write(PREFIX + "恢复位置JSON", json.dumps(locations, ensure_ascii=False))
                    self.write(PREFIX + "恢复请求号", request)
                    self.log(f"已提交位置恢复 #{request}，等待设备核对")
                else:
                    raise ValueError("未知操作")
                self.refresh()
                return {"ok": True}
            except ValueError:
                raise
            except Exception as exc:
                self.error = "通信中断；命令结果须核对，不自动重试：" + str(exc)
                self.disconnect()
                raise ValueError(self.error) from exc

    def snapshot(self):
        with self.lock:
            v = self.values
            return {"kind": self.kind, "online": self.online, "error": self.error, "endpoint": self.endpoint,
                    "updated": self.updated, "values": v.copy(), "events": list(self.events),
                    "actions": [{"name": name, **config} for name, config in self.actions.items()],
                    "parameters": [{"name": name, "type": self.defs[name].data_type, "value": v.get(name)} for name in self.parameters]}


class Operation(BaseModel):
    action: str | None = None
    position: int | None = None
    mode: int | None = None
    values: dict = Field(default_factory=dict)
    locations: dict | None = None


def create_app(base_port=4855):
    panels = {kind: Panel(kind, f"opc.tcp://127.0.0.1:{base_port + offset}/iccas_sim/")
              for kind, offset in (("spray", 0), ("filter", 1))}

    @asynccontextmanager
    async def lifespan(app):
        for panel in panels.values():
            panel.start()
        yield
        for panel in panels.values():
            panel.close()

    app = FastAPI(lifespan=lifespan)
    app.state.panels = panels

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method == "POST" and request.headers.get("origin"):
            if request.headers["origin"] != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "仅接受HMI本页面提交"}, status_code=403)
        return await call_next(request)

    @app.get("/")
    @app.get("/spray.html")
    def spray():
        return FileResponse(STATIC / "iccas-spray.html")

    @app.get("/filter.html")
    def filter_page():
        return FileResponse(STATIC / "iccas-filter.html")

    @app.get("/hmi.css")
    def css():
        return FileResponse(STATIC / "iccas-hmi.css")

    @app.get("/hmi.js")
    def js():
        return FileResponse(STATIC / "iccas-hmi.js")

    @app.get("/api/{kind}/state")
    def state(kind: str):
        if kind not in panels:
            raise HTTPException(404, "未知工站")
        return panels[kind].snapshot()

    @app.post("/api/{kind}/{operation}")
    def operate(kind: str, operation: str, request: Operation):
        if kind not in panels:
            raise HTTPException(404, "未知工站")
        try:
            return panels[kind].operate(operation, request.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18866)
    parser.add_argument("--base-port", type=int, default=4855)
    args = parser.parse_args()
    uvicorn.run(create_app(args.base_port), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
