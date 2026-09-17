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
REAL_ENDPOINTS = {
    "spray":  "opc.tcp://192.168.1.88:4840",   # 汇川 InovanceOpcUaServer
    "filter": "opc.tcp://TODO_FILTER:4840",     # 过滤站真机地址待定
}


class Panel:
    def __init__(self, kind, endpoint, *, read_only=False):
        self.kind, self.endpoint = kind, endpoint
        self.read_only = read_only
        self.defs = {d.name_cn: d for d in definitions(kind, device_model=True)}
        self.node_ids = {name: definition.node_id for name, definition in self.defs.items()}
        self.layout = "simulator"
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
        return self.client.get_node(self.node_ids[name])

    def configure_nodes(self):
        """Use the PLC's browsed names when connected to an Inovance controller."""
        try:
            children = self.client.get_node("ns=4;s=OPC").get_children()
            actual = {node.get_browse_name().Name: node.nodeid.to_string() for node in children}
        except Exception:
            return
        if not actual:
            return
        mapped = {}
        for name in self.defs:
            raw = name.removeprefix(PREFIX)
            candidates = [raw, raw.replace(".", "_")]
            if raw == MODE:
                candidates.insert(0, "实验工位启用0_AB1_A2_BPCtoPLC")
            for candidate in candidates:
                if candidate in actual:
                    mapped[name] = actual[candidate]
                    break
        if mapped:
            self.node_ids = mapped
            self.layout = "inovance-opc"

    def synthetic_world(self):
        """Build the occupancy-only world needed by the HMI when PLC JSON is absent."""
        locations = {"gripper": None}
        if self.kind == "spray":
            for kind, label in (("feed", "原料罐"), ("reaction", "反应罐")):
                for slot in range(1, 13):
                    occupied = self.values.get(f"{label}料仓{slot}号位检测")
                    locations[f"rack/{kind}/{slot:02d}"] = f"{kind}:{slot:02d}" if occupied else None
                for lane in "AB":
                    occupied = self.values.get(f"{lane}工位{label}工作位检测")
                    locations[f"work/{lane}/{kind}"] = f"{kind}:work-{lane}" if occupied else None
        return json.dumps({"locations": locations, "uncertain": []}, ensure_ascii=False)

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
            self.configure_nodes()
        names = list(self.node_ids)
        values = self.client.get_values([self.node(name) for name in names])
        self.values = {name: None for name in self.defs}
        self.values.update(zip(names, values))
        if MODE not in self.node_ids and self.layout == "inovance-opc":
            self.values[MODE] = None
        if not self.values.get(PREFIX + "物料位置JSON") and self.layout == "inovance-opc":
            self.values[PREFIX + "物料位置JSON"] = self.synthetic_world()
        contract = self.values.get(PREFIX + "设备合同")
        if contract is not None and contract != VERSION:
            raise ValueError("请启动 start_iccas_devices.command：此端点不是设备模式")
        boot = self.values.get(PREFIX + "启动标识")
        if self.boot and self.boot != boot:
            self.owned.clear()
            self.log("设备已重启，请核对位置和未完成命令")
        self.boot = boot
        for base in self.actions:
            state = self.values.get(PREFIX + base + ".状态")
            if self.last_states.get(base) != state:
                if state is not None and (base in self.last_states or state != "idle"):
                    message = self.values[PREFIX + base + ".错误信息"]
                    self.log(f"{base} · {state}" + (f" · {message}" if message else ""),
                             action=base, state=state, code=self.values[PREFIX + base + ".错误码"], reason=message)
                self.last_states[base] = state
            command = self.owned.get(base)
            accepted = self.values.get(PREFIX + base + ".已接受命令号")
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
                if self.read_only:
                    raise ValueError("当前为真机只读监控模式，操作未发送")
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
                    "read_only": self.read_only, "layout": self.layout,
                    "updated": self.updated, "values": v.copy(), "events": list(self.events),
                    "actions": [{"name": name, **config} for name, config in self.actions.items()],
                    "parameters": [{"name": name, "type": self.defs[name].data_type, "value": v.get(name)} for name in self.parameters]}


class Operation(BaseModel):
    action: str | None = None
    position: int | None = None
    mode: int | None = None
    values: dict = Field(default_factory=dict)
    locations: dict | None = None


def create_app(endpoints: dict[str, str] | None = None, *, read_only=False):
    endpoints = endpoints or {kind: f"opc.tcp://127.0.0.1:{4855 + offset}/iccas_sim/"
                              for kind, offset in (("spray", 0), ("filter", 1))}
    panels = {kind: Panel(kind, endpoint, read_only=read_only) for kind, endpoint in endpoints.items()}

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
    parser.add_argument("--mode", choices=["sim", "real"], default="sim",
                        help="sim: 连仿真服务器；real: 连真机 OPC UA")
    parser.add_argument("--base-port", type=int, default=4855,
                        help="仿真模式下 OPC UA 起始端口（sim 模式有效）")
    parser.add_argument("--spray-endpoint", default=REAL_ENDPOINTS["spray"],
                        help="喷雾站真机地址（real 模式有效，默认 %(default)s）")
    parser.add_argument("--filter-endpoint", default=REAL_ENDPOINTS["filter"],
                        help="过滤站真机地址（real 模式有效，默认 %(default)s）")
    parser.add_argument("--read-only", action="store_true",
                        help="只读取 OPC UA 数据，禁止页面向设备写值")
    args = parser.parse_args()

    if args.mode == "real":
        endpoints = {"spray": args.spray_endpoint}
        if "TODO_" not in args.filter_endpoint:
            endpoints["filter"] = args.filter_endpoint
    else:
        endpoints = {
            "spray": f"opc.tcp://127.0.0.1:{args.base_port}/iccas_sim/",
            "filter": f"opc.tcp://127.0.0.1:{args.base_port + 1}/iccas_sim/",
        }

    uvicorn.run(create_app(endpoints, read_only=args.read_only), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
