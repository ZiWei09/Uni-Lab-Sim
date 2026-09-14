"""Standalone ICCAS protocol simulator; no Uni-Lab-OS runtime dependency.

This models handshake timing, not chemistry, robot trajectories or inventory.
Supplier CSV snapshots remain byte-identical; NodeIds are simulator mappings.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from opcua import ua

try:
    from .common import NodeDef
    from .server import add_nodes, build_server, register_ns_padding
    from .iccas_timing import ProcessTiming, demo_recipe
    from .iccas_device import Equipment, control_definitions
except ImportError:
    from common import NodeDef
    from server import add_nodes, build_server, register_ns_padding
    from iccas_timing import ProcessTiming, demo_recipe
    from iccas_device import Equipment, control_definitions


TABLES = {"spray": "spray_173", "filter": "filter_defoam_158",
          "body": "synthesis_body_17", "reaction": "synthesis_reaction_45"}
TYPES = {"BOOL": "BOOLEAN", "INT": "INT16", "DINT": "INT32", "REAL": "FLOAT"}
FILTER_EXTENSION_ACTIONS = ("烧杯仓放料", "真空箱取料")
FILTER_EXTENSION_MARKER = "ICCAS_SIM.过滤除泡扩展合同"
FILTER_EXTENSION_VERSION = "filter-return-v1"


def definitions(kind: str, number: int = 0, *, filter_extensions: bool = False, device_model: bool = False) -> list[NodeDef]:
    path = Path(__file__).parent / "data" / f"iccas_{TABLES[kind]}.csv"
    result = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            name, dtype = row["变量名"], row["数据类型"]
            # Supplier leaves this type blank. A simulator-only placeholder;
            # the current gateway never writes this field.
            if kind == "body" and name == "PC_TO_PLC.机器人目标条码" and not dtype:
                dtype = "STRING"
            dtype = "STRING" if dtype.startswith("STRING") else TYPES[dtype]
            initial = None
            if name in {"远程模式PLCtoPC", "设备准备就绪PLCtoPC",
                        "PLC_TO_PC.设备空闲", "PLC_TO_PC.机器人空闲"}:
                initial = True
            elif name == "PLC_TO_PC.设备编号":
                initial = number
            elif name == "PLC_TO_PC.设备状态":
                initial = 2
            prefix = "上位机通讯|" if kind in {"spray", "filter"} else ""
            result.append(NodeDef(name, "", "VARIABLE", dtype, f"ns=4;s={prefix}{name}",
                                  initial_value=initial))
    if kind == "filter" and (filter_extensions or device_model):
        for base in FILTER_EXTENSION_ACTIONS:
            for suffix, dtype in (("号码PCtoPLC", "INT16"), ("触发PCtoPLC", "BOOLEAN"),
                                  ("完成PLCtoPC", "BOOLEAN")):
                name = base + suffix
                result.append(NodeDef(name, "SIMULATION EXTENSION", "VARIABLE", dtype,
                                      f"ns=4;s=上位机通讯|{name}"))
        result.append(NodeDef(FILTER_EXTENSION_MARKER, "SIMULATION ONLY", "VARIABLE", "STRING",
                              f"ns=4;s=上位机通讯|{FILTER_EXTENSION_MARKER}",
                              initial_value=FILTER_EXTENSION_VERSION))
    if device_model and kind == "spray":
        for lane in "AB":
            for vessel in ("原料罐", "反应罐"):
                for action in ("工作位取料", "库位放料"):
                    for suffix in ("触发PCtoPLC", "完成PLCtoPC"):
                        name = lane + "工位" + vessel + action + suffix
                        result.append(NodeDef(name, "", "VARIABLE", "BOOLEAN", f"ns=4;s=上位机通讯|{name}"))
    if device_model and kind in {"spray", "filter"}:
        bases = [d.name_cn.removesuffix("触发PCtoPLC") for d in result if d.name_cn.endswith("触发PCtoPLC")]
        for d in result:
            name = d.name_cn
            if (name.endswith("完成PLCtoPC") or "检测" in name or
                ("位置" in name and name[-1:].isdigit()) or name == "机器人夹爪状态PLCtoPC"):
                d.write_owner = "plc"
        result.extend(control_definitions(bases))
    if len({item.node_id for item in result}) != len(result):
        raise ValueError(f"Duplicate NodeId in {path}")
    return result


@dataclass
class Channel:
    trigger: str
    complete: str
    running: str | None = None
    phase: str = "idle"
    deadline: float = 0.0
    count: int = 0
    reason: str = ""
    started_at: float = 0.0
    process_duration_s: float = 0.0
    wall_duration_s: float = 0.0
    timing_stages: list = field(default_factory=list)


def channels(kind: str, names: set[str]) -> list[Channel]:
    if kind in {"spray", "filter"}:
        result = [Channel(name, name.removesuffix("触发PCtoPLC") + "完成PLCtoPC")
                  for name in sorted(names) if name.endswith("触发PCtoPLC")]
    else:
        result = [Channel("PC_TO_PLC.初始化", "PLC_TO_PC.初始化完成")]
        pairs = [("机器人触发启动", "机器人执行")] if kind == "body" else [
            ("移液触发", "移液工艺执行"), ("搅拌触发", "搅拌工艺执行")]
        result += [Channel(f"PC_TO_PLC.{trigger}", f"PLC_TO_PC.{stem}完成",
                           f"PLC_TO_PC.{stem}中") for trigger, stem in pairs]
    for ch in result:
        if not {ch.trigger, ch.complete, *([ch.running] if ch.running else [])} <= names:
            raise ValueError(f"Incomplete handshake: {ch.trigger}")
    return result


class Station:
    def __init__(self, kind, nodes, *, delay=0.5, time_scale=1.0,
                 timing_mode="handshake", timing_profile=None, device_model=False,
                 device_state=None, device_mapping=None, watchdog_s=15):
        if not math.isfinite(delay) or delay <= 0 or not math.isfinite(time_scale) or time_scale <= 0:
            raise ValueError("delay and time_scale must be finite and positive")
        self.kind, self.nodes = kind, nodes
        self.delay, self.time_scale = delay, time_scale
        if timing_mode not in {"handshake", "process"}:
            raise ValueError("timing_mode must be handshake or process")
        self.timing_mode = timing_mode
        self.process_timing = ProcessTiming(timing_profile) if timing_mode == "process" and kind in {"spray", "filter"} else None
        self._now = time.monotonic()
        self.channels = channels(kind, set(nodes))
        self.events = deque(maxlen=200)
        # An attached model must not replay an already-high trigger.
        for ch in self.channels:
            if self.read(ch.trigger):
                ch.phase = "latched"
        self.equipment = Equipment(self, device_state, device_mapping, watchdog_s) if device_model and kind in {"spray", "filter"} else None

    def read(self, name):
        return self.nodes[name].get_value()

    def write(self, name, value):
        node = self.nodes[name]
        node.set_value(ua.Variant(value, node.get_data_type_as_variant_type()))

    def event(self, ch, phase, now, reason=""):
        ch.phase, ch.reason = phase, reason
        self.events.append({"time": now, "trigger": ch.trigger, "phase": phase, "reason": reason})

    def rejection(self, ch):
        if self.kind == "spray":
            mode = self.read("实验工位选择0_AB1_A2_BPCtoPLC")
            if type(mode) is not int or mode not in (0, 1, 2):
                return "工位选择必须为0（A/B）、1（仅A）或2（仅B）"
            if (mode == 1 and ch.trigger.startswith("B工位")) or (mode == 2 and ch.trigger.startswith("A工位")):
                return "该工位未启用，拒绝执行动作"
        if self.kind in {"spray", "filter"}:
            if not self.read("远程模式PLCtoPC") or not self.read("设备准备就绪PLCtoPC"):
                return "工站未就绪或不在远程模式"
            if any(self.read(name) for name in self.nodes if "报警" in name):
                return "工站报警"
            base = ch.trigger.removesuffix("触发PCtoPLC")
            if self.kind == "filter" and base in FILTER_EXTENSION_ACTIONS:
                if not 1 <= self.read(base + "号码PCtoPLC") <= 12:
                    return "扩展动作库位号码必须为 1..12"
        else:
            if any(other.phase in {"running", "complete", "hang"}
                   for other in self.channels if other is not ch):
                return "PLC 忙碌"
            if self.kind == "body" and ch.running:
                if self.read("PLC_TO_PC.机器人故障中"):
                    return "机器人故障"
                if not 1 <= self.read("PC_TO_PLC.机器人目标位置代码") <= 27:
                    return "机器人程序号必须为 1..27"
                if not 1 <= self.read("PC_TO_PLC.机器人目标机台编号") <= 5:
                    return "目标机台必须为 1..5"
            if self.kind == "reaction" and ch.trigger == "PC_TO_PLC.移液触发":
                if not 1 <= self.read("PC_TO_PLC.移液来源") <= 6:
                    return "移液来源必须为 1..6"
                if not 1 <= self.read("PC_TO_PLC.移液目标") <= 2:
                    return "移液目标必须为 1..2"
                volume = self.read("PC_TO_PLC.移液量")
                if not math.isfinite(volume) or volume <= 0:
                    return "移液量必须为正数"
        return ""

    def duration(self, ch):
        if self.process_timing:
            return sum(stage["process_seconds"] for stage in
                       self.process_timing.plan(self.kind, ch.trigger, self.read)) / self.time_scale
        # Only the filtering table explicitly specifies milliseconds. Synthesis
        # stir units and compound spray timings are not guessed from names.
        timing = {"抽原液启动触发PCtoPLC": "原液抽取时间PCtoPLC",
                  "真空箱除泡启动触发PCtoPLC": "真空箱_真空除泡时间PCtoPLC"}
        seconds = self.delay
        if self.kind == "filter" and ch.trigger in timing:
            seconds = max(seconds, self.read(timing[ch.trigger]) / 1000.0)
        return seconds / self.time_scale

    def prepare_timing(self, ch, now):
        if self.process_timing:
            ch.timing_stages = self.process_timing.plan(self.kind, ch.trigger, self.read)
            ch.process_duration_s = sum(stage["process_seconds"] for stage in ch.timing_stages)
            ch.wall_duration_s = ch.process_duration_s / self.time_scale
        else:
            ch.wall_duration_s = self.duration(ch)
            ch.process_duration_s = ch.wall_duration_s * self.time_scale
            ch.timing_stages = [{"name": "握手测试延时", "process_seconds": ch.process_duration_s,
                                 "source": "handshake_model"}]
        ch.started_at = now
        ch.deadline = now + ch.wall_duration_s

    def tick(self, now=None, faults=None):
        now = time.monotonic() if now is None else now
        if self.equipment:
            self.equipment.tick(now, faults or {})
            return
        self._now = now
        faults = faults or {}
        for ch in self.channels:
            high = bool(self.read(ch.trigger))
            if not high:
                if ch.phase != "idle":
                    self.write(ch.complete, False)
                    if ch.running:
                        self.write(ch.running, False)
                    self.event(ch, "idle", now)
                continue
            if ch.phase == "idle":
                reason = self.rejection(ch)
                mode = faults.get(ch.trigger)
                if reason or mode == "reject":
                    self.event(ch, "rejected", now, reason or "注入拒绝")
                    continue
                try:
                    self.prepare_timing(ch, now)
                except ValueError as exc:
                    self.event(ch, "rejected", now, str(exc))
                    continue
                self.write(ch.complete, False)
                if ch.running:
                    self.write(ch.running, True)
                self.event(ch, "hang" if mode == "hang" else "running", now)
            elif ch.phase == "running":
                reason = self.rejection(ch)
                if reason:
                    if ch.running:
                        self.write(ch.running, False)
                    self.event(ch, "rejected", now, reason)
                elif now >= ch.deadline:
                    self.write(ch.complete, True)
                    ch.count += 1
                    self.event(ch, "complete", now)
        if self.kind in {"body", "reaction"}:
            busy = any(ch.phase in {"running", "hang", "complete"} for ch in self.channels)
            self.write("PLC_TO_PC.设备空闲", not busy)
            self.write("PLC_TO_PC.设备状态", 3 if busy else 2)
            if self.kind == "body":
                self.write("PLC_TO_PC.机器人空闲", not busy)

    def snapshot(self):
        channel_states = []
        for ch in self.channels:
            state = vars(ch).copy()
            elapsed = max(0.0, self._now - ch.started_at) if ch.phase in {"running", "hang", "complete"} else 0.0
            state["remaining_wall_s"] = max(0.0, ch.wall_duration_s - elapsed)
            state["progress"] = min(1.0, elapsed / ch.wall_duration_s) if ch.wall_duration_s else 0.0
            if ch.phase == "complete":
                state["progress"] = 1.0
            current = ""
            if ch.phase in {"running", "hang"}:
                process_elapsed = elapsed * self.time_scale
                for stage in ch.timing_stages:
                    if process_elapsed < stage["process_seconds"]:
                        current = stage["name"]
                        break
                    process_elapsed -= stage["process_seconds"]
                if not current:
                    current = "等待完成反馈"
            state["current_stage"] = current
            channel_states.append(state)
        return {"kind": self.kind, "node_count": len(self.nodes),
                "device_model": bool(self.equipment),
                "device": {"locations": self.equipment.locations, "uncertain": sorted(self.equipment.uncertain),
                           "recovery_required": self.equipment.recovery, "effects": self.equipment.effects} if self.equipment else None,
                "timing_mode": "process" if self.process_timing else "handshake", "time_scale": self.time_scale,
                "filter_extensions": FILTER_EXTENSION_MARKER in self.nodes,
                "channels": channel_states, "events": list(self.events)}


def topology(selection="all", base_port=4855):
    specs = [("spray", "spray", 0, 0), ("filter", "filter", 1, 0), ("body", "body", 2, 0)]
    specs += [(f"reaction_{i}", "reaction", i + 2, i) for i in range(1, 6)]
    return [(name, kind, base_port + offset, number) for name, kind, offset, number in specs
            if selection == "all" or name == selection or
            (selection == "postprocessing" and kind in {"spray", "filter"}) or
            (selection == "synthesis" and kind in {"body", "reaction"})]


def load_faults(path, stations):
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - set(stations):
        raise ValueError("Fault file must map known station names to trigger maps")
    for name, entries in data.items():
        known = {ch.trigger for ch in stations[name].channels}
        if not isinstance(entries, dict) or set(entries) - known:
            raise ValueError(f"Unknown trigger in faults for {name}")
        allowed = {"hang", "reject", "error"} if stations[name].equipment else {"hang", "reject"}
        if any(mode not in allowed for mode in entries.values()):
            raise ValueError(f"Fault mode must be one of {sorted(allowed)}")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", choices=["all", "postprocessing", "spray", "filter", "synthesis", "body"] +
                        [f"reaction_{i}" for i in range(1, 6)], default="all")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--base-port", type=int, default=4855)
    parser.add_argument("--delay", type=float, default=0.5, help="fallback action duration in seconds")
    parser.add_argument("--time-scale", type=float, default=1.0)
    parser.add_argument("--timing-mode", choices=["handshake", "process"], default="handshake")
    parser.add_argument("--timing-profile", type=Path, help="YAML estimates for process mode")
    parser.add_argument("--seed-process-recipe", action="store_true",
                        help="initialize local post-processing nodes with demo recipe times before listening")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--fault-file", type=Path)
    parser.add_argument("--filter-extensions", action="store_true",
                        help="enable simulator-only beaker return and vacuum unload (6 points + marker)")
    parser.add_argument("--device-model", action="store_true", help="enable virtual supplier contract, locks and material witnesses")
    parser.add_argument("--device-state-dir", type=Path, help="persistent equipment journals (required with --device-model)")
    parser.add_argument("--device-mapping", type=Path, help="versioned YAML action-to-equipment mapping")
    parser.add_argument("--watchdog-seconds", type=float, default=15, help="unscaled heartbeat timeout when monitoring is enabled")
    parser.add_argument("--list", action="store_true", help="print endpoint and handshake coverage without listening")
    args = parser.parse_args()
    if not 1 <= args.base_port <= 65528:
        parser.error("base-port must be 1..65528")
    if any(not math.isfinite(v) or v <= 0 for v in (args.delay, args.time_scale)):
        parser.error("delay and time-scale must be finite and positive")
    if args.seed_process_recipe and args.timing_mode != "process":
        parser.error("--seed-process-recipe requires --timing-mode process")
    if args.device_model and not args.device_state_dir:
        parser.error("--device-model requires --device-state-dir")
    if not math.isfinite(args.watchdog_seconds) or args.watchdog_seconds <= 0:
        parser.error("watchdog-seconds must be finite and positive")
    specs = topology(args.station, args.base_port)
    if args.list:
        for name, kind, port, number in specs:
            defs = definitions(kind, number, filter_extensions=args.filter_extensions, device_model=args.device_model)
            print(f"{name}: opc.tcp://{args.host}:{port}/iccas_sim/ "
                  f"{len(defs)} nodes, {len(channels(kind, {d.name_cn for d in defs}))} handshakes")
        if args.filter_extensions:
            print("SIMULATION EXTENSION: filter-return-v1; not a supplier-confirmed contract")
        return 0
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    servers, stations = [], {}
    try:
        for name, kind, port, number in specs:
            endpoint = f"opc.tcp://{args.host}:{port}/iccas_sim/"
            server = build_server(endpoint)
            server.set_server_name(f"ICCAS {name} protocol simulator")
            ns = register_ns_padding(server, 4, "urn:iccas:sim")
            defs = definitions(kind, number, filter_extensions=args.filter_extensions, device_model=args.device_model)
            nodes = add_nodes(server, ns, defs)
            if args.device_model:
                for definition in defs:
                    if definition.write_owner == "plc":
                        nodes[definition.name_cn].set_read_only()
            if FILTER_EXTENSION_MARKER in nodes:
                nodes[FILTER_EXTENSION_MARKER].set_read_only()
            stations[name] = Station(kind, nodes, delay=args.delay, time_scale=args.time_scale,
                                     timing_mode=args.timing_mode, timing_profile=args.timing_profile,
                                     device_model=args.device_model,
                                     device_state=args.device_state_dir / f"{name}.json" if args.device_state_dir else None,
                                     device_mapping=args.device_mapping, watchdog_s=args.watchdog_seconds)
            if args.seed_process_recipe:
                for variable, value in demo_recipe(kind).items():
                    stations[name].write(variable, value)
            server.start()
            servers.append(server)
            print(f"{name} ready: {endpoint}", flush=True)
        print(f"timing={args.timing_mode}, speed={args.time_scale:g}x, demo_recipe={args.seed_process_recipe}", flush=True)
        while not stop.is_set():
            faults = load_faults(args.fault_file, stations)
            for name, station in stations.items():
                station.tick(faults=faults.get(name))
            if args.state_file:
                args.state_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.state_file.with_suffix(args.state_file.suffix + ".tmp")
                temporary.write_text(json.dumps({name: st.snapshot() for name, st in stations.items()},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(args.state_file)
            stop.wait(0.02)
    finally:
        for station in stations.values():
            if station.equipment:
                station.equipment.fail_active("RESTART", "服务停止：执行结果待现场确认", time.monotonic())
                station.equipment.publish()
                station.equipment.close()
        for server in reversed(servers):
            server.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
