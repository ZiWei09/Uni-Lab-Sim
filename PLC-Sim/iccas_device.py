"""Versioned virtual supplier equipment: material witnesses, locks and recovery.

The location journal records confirmed transitions, not intermediate robot pose.
Interrupted motion requires an explicit full location reconciliation over OPC UA.
"""
from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from copy import deepcopy
from pathlib import Path

import yaml

try:
    from .common import NodeDef
except ImportError:
    from common import NodeDef


PREFIX = "ICCAS_SIM."
VERSION = "postprocess-device-v1"
ERRORS = {"INVALID": 1001, "EMPTY": 1002, "OCCUPIED": 1003, "BUSY": 1004,
          "NOT_READY": 1005, "COMMAND": 1006, "RECOVERY": 1007,
          "CANCELLED": 2001, "HEARTBEAT": 2002, "ALARM": 2003, "INJECTED": 2004, "RESTART": 2005}


def control_definitions(bases):
    specs = [("设备合同", "STRING", VERSION, False), ("启动标识", "STRING", "", False),
             ("设备忙碌", "BOOLEAN", False, False), ("需要恢复", "BOOLEAN", False, False),
             ("物料位置JSON", "STRING", "", False), ("位置版本", "INT32", 0, False),
             ("控制心跳", "INT32", 0, True), ("心跳监控启用", "BOOLEAN", False, True),
             ("恢复位置JSON", "STRING", "", True), ("恢复请求号", "INT32", 0, True),
             ("恢复已处理号", "INT32", 0, False),
             ("恢复应答号", "INT32", 0, False), ("恢复错误", "STRING", "", False)]
    for base in bases:
        for suffix, dtype, value, writable in [
            ("命令号", "INT32", 0, True), ("取消", "BOOLEAN", False, True),
            ("已接受命令号", "INT32", 0, False), ("状态", "STRING", "idle", False),
            ("错误码", "INT32", 0, False), ("错误信息", "STRING", "", False),
            ("上次结果JSON", "STRING", "null", False)]:
            specs.append((base + "." + suffix, dtype, value, writable))
    return [NodeDef(PREFIX + name, "Virtual supplier contract v1", "VARIABLE", dtype,
                    "ns=4;s=上位机通讯|" + PREFIX + name, initial_value=value,
                    write_owner="host" if writable else "plc") for name, dtype, value, writable in specs]


class DeviceError(ValueError):
    def __init__(self, code, message):
        self.code = ERRORS[code]
        super().__init__(message)


class Equipment:
    def __init__(self, station, path, mapping=None, watchdog_s=15):
        self.station = station
        self.path = Path(path) if path else None
        self._journal_lock = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._journal_lock = self.path.with_suffix(".lock").open("a+b")
            try:
                import os
                if os.name == "nt":
                    import msvcrt
                    self._journal_lock.seek(0)
                    if not self._journal_lock.read(1):
                        self._journal_lock.write(b"0")
                        self._journal_lock.flush()
                    self._journal_lock.seek(0)
                    msvcrt.locking(self._journal_lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._journal_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._journal_lock.close()
                self._journal_lock = None
                raise RuntimeError(f"设备状态文件已被另一实例占用: {self.path}")
        mapping = Path(mapping) if mapping else Path(__file__).parent / "config/iccas_devices.yaml"
        data = yaml.safe_load(mapping.read_text(encoding="utf-8"))
        if data["version"] != 1:
            raise ValueError("Unsupported device mapping version")
        self.actions = data["stations"][station.kind]["actions"]
        bases = {ch.trigger.removesuffix("触发PCtoPLC") for ch in station.channels}
        if set(self.actions) != bases:
            raise ValueError("Device action mapping must exactly match the endpoint handshakes")
        self.locations = self.initial_locations(station.kind)
        self.materials = sorted(value for value in self.locations.values() if value)
        self.uncertain = set()
        self.effects = {}
        self.revision = 0
        self.recovery = False
        self.records = {base: {"accepted": 0, "state": "idle", "code": 0, "message": "",
                               "plan": None, "count": 0, "last_result": None} for base in bases}
        self.recovery_id = 0
        self.recovery_seen = 0
        self.recovery_error = ""
        self.watchdog_s = watchdog_s
        self.heartbeat_value = station.read(PREFIX + "控制心跳")
        self.heartbeat_at = time.monotonic()
        self.enabled = False
        self.boot_id = str(uuid.uuid4())
        if self.path and self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            additions = {lane + "工位" + vessel + action for lane in "AB"
                         for vessel in ("原料罐", "反应罐") for action in ("工作位取料", "库位放料")} if station.kind == "spray" else set()
            compatible = set(saved["records"]) in (bases, bases - additions)
            if saved["version"] != VERSION or saved["kind"] != station.kind or not compatible:
                raise ValueError("Incompatible equipment journal; refusing a silent reset")
            self.validate_locations(saved["locations"])
            self.locations = saved["locations"]
            self.effects = saved["effects"]
            self.revision = saved["revision"]
            self.records.update(saved["records"])
            self.uncertain = set(saved["uncertain"])
            self.recovery = saved["recovery"]
            self.recovery_id = saved["recovery_id"]
            self.recovery_seen = saved["recovery_seen"]
            for record in self.records.values():
                if record["state"] in {"running", "hang"}:
                    self.recovery = True
                    self.uncertain.update(record["plan"]["sites"])
                    record.update(state="error", code=ERRORS["RESTART"], message="进程重启：执行结果待现场确认")
                    self.remember_result(record)
        for ch in station.channels:
            base = ch.trigger.removesuffix("触发PCtoPLC")
            record = self.records[base]
            ch.phase, ch.count = record["state"], record["count"]
            if record["state"] == "idle" and station.read(ch.trigger):
                ch.phase = record["state"] = "latched"
        self.save()
        self.publish()

    def close(self):
        if self._journal_lock:
            self._journal_lock.close()
            self._journal_lock = None

    def __del__(self):
        self.close()

    @staticmethod
    def initial_locations(kind):
        types = ("feed", "reaction") if kind == "spray" else ("feed", "filter", "beaker")
        locations = {f"rack/{typ}/{i:02d}": f"{typ}:{i:02d}" for typ in types for i in range(1, 13)}
        locations["gripper"] = None
        if kind == "spray":
            locations.update({f"work/{lane}/{typ}": None for lane in ("A", "B") for typ in types})
        else:
            locations.update({f"work/{typ}": None for typ in types})
            locations.update({f"vacuum/{i:02d}": None for i in range(1, 13)})
        return locations

    def validate_locations(self, locations):
        if not isinstance(locations, dict) or set(locations) != set(self.locations):
            raise DeviceError("INVALID", "必须提交全部位置，不能增加或遗漏位置")
        if any(value is not None and not isinstance(value, str) for value in locations.values()):
            raise DeviceError("INVALID", "物料标识必须为字符串或 null")
        if Counter(value for value in locations.values() if value) != Counter(self.materials):
            raise DeviceError("INVALID", "物料必须守恒：不允许丢失、重复或新增标识")
        for site, material in locations.items():
            if not material or site == "gripper":
                continue
            typ = "beaker" if site.startswith("vacuum/") else site.split("/")[-1] if site.startswith("work/") else site.split("/")[1]
            if material.split(":")[0] != typ:
                raise DeviceError("INVALID", f"位置与容器类型不匹配: {site}")

    def save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": VERSION, "kind": self.station.kind, "locations": self.locations,
                "effects": self.effects, "revision": self.revision, "uncertain": sorted(self.uncertain),
                "recovery": self.recovery, "records": self.records, "recovery_id": self.recovery_id}
        data["recovery_seen"] = self.recovery_seen
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            import os
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        if os.name != "nt":
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def plan(self, base):
        if self.recovery:
            raise DeviceError("RECOVERY", "设备需要位置确认恢复")
        action = self.actions[base]
        locks = set(action["locks"])
        for other_name, other in self.records.items():
            if other["state"] in {"running", "hang", "complete"} and locks.intersection(other["plan"]["locks"]):
                conflict = ",".join(sorted(locks.intersection(other["plan"]["locks"])))
                raise DeviceError("BUSY", f"{conflict} 被 {other_name} 占用")
        result = {"locks": sorted(locks), "sites": [], "move": None, "effect": action.get("effect"), "targets": []}
        if "move" in action:
            slot = 0
            if "parameter" in action:
                slot = self.station.read(action["parameter"])
                if type(slot) is not int or not 1 <= slot <= 12:
                    raise DeviceError("INVALID", "库位号码必须为 1..12")
            held = self.locations["gripper"]
            home = held.split(":")[1] if held else "00"
            source, target = [site.format(slot=f"{slot:02d}", home=home) for site in action["move"]]
            material = self.locations.get(source)
            if not material:
                raise DeviceError("EMPTY", f"取料源为空: {source}")
            if self.locations.get(target):
                raise DeviceError("OCCUPIED", f"放料目标已占用: {target}")
            updated = dict(self.locations)
            updated[source], updated[target] = None, material
            self.validate_locations(updated)
            result.update(sites=[source, target], move=[source, target, material])
        else:
            sites = action.get("requires", [])
            if action.get("vacuum_batch"):
                sites = [site for site, material in self.locations.items() if site.startswith("vacuum/") and material]
                if not sites:
                    raise DeviceError("EMPTY", "真空箱没有烧杯")
                if self.station.read("真空箱放置烧杯数量PCtoPLC") != len(sites):
                    raise DeviceError("INVALID", "真空批次数量与在位烧杯数不一致")
            if any(not self.locations[site] for site in sites):
                raise DeviceError("EMPTY", "工艺需要的容器未全部到位")
            result["sites"] = list(sites)
            targets = sites if action.get("vacuum_batch") else [action["result_site"]] if action.get("result_site") else []
            result["targets"] = [self.locations[site] for site in targets]
        return result

    def fail_active(self, code, message, now):
        changed = False
        for ch in self.station.channels:
            base = ch.trigger.removesuffix("触发PCtoPLC")
            record = self.records[base]
            if record["state"] in {"running", "hang"}:
                self.uncertain.update(record["plan"]["sites"])
                record.update(state="error", code=ERRORS[code], message=message)
                self.remember_result(record)
                self.station.event(ch, "error", now, message)
                self.recovery = True
                changed = True
        if changed:
            self.save()

    @staticmethod
    def remember_result(record):
        record["last_result"] = {key: record[key] for key in ("accepted", "state", "code", "message", "count")}

    def reconcile(self, request):
        previous_request = self.recovery_seen
        self.recovery_seen = max(previous_request, request)
        try:
            if request <= previous_request:
                raise DeviceError("COMMAND", "恢复请求号必须递增")
            if any(self.station.read(ch.trigger) or self.station.read(PREFIX + ch.trigger.removesuffix("触发PCtoPLC") + ".取消")
                   for ch in self.station.channels):
                raise DeviceError("RECOVERY", "恢复前须将所有 Trigger 和取消信号复位")
            if any(record["state"] in {"running", "hang"} for record in self.records.values()):
                raise DeviceError("BUSY", "动作执行中不能更新现场位置")
            locations = json.loads(self.station.read(PREFIX + "恢复位置JSON"))
            self.validate_locations(locations)
            self.locations = locations
            self.recovery = False
            self.uncertain.clear()
            self.revision += 1
            self.recovery_id = request
            self.recovery_error = ""
            for record in self.records.values():
                record.update(state="idle", code=0, message="", plan=None)
            self.save()
        except (ValueError, TypeError) as exc:
            self.recovery_error = str(exc)
        self.save()

    def tick(self, now, faults):
        station = self.station
        station._now = now
        hb = station.read(PREFIX + "控制心跳")
        enabled = station.read(PREFIX + "心跳监控启用")
        if hb != self.heartbeat_value or enabled and not self.enabled:
            self.heartbeat_at, self.heartbeat_value = now, hb
        self.enabled = bool(enabled)
        expired = self.enabled and now - self.heartbeat_at >= self.watchdog_s
        if expired:
            self.fail_active("HEARTBEAT", "控制心跳超时：动作停止，须确认现场位置", now)
        request = station.read(PREFIX + "恢复请求号")
        if request > self.recovery_seen:
            self.reconcile(request)
        # Interrupt all affected operations before any completion is committed.
        for ch in station.channels:
            base = ch.trigger.removesuffix("触发PCtoPLC")
            if self.records[base]["state"] not in {"running", "hang"}:
                continue
            if station.read(PREFIX + base + ".取消") or not station.read(ch.trigger):
                self.fail_active("CANCELLED", "动作取消：现场结果待确认", now)
            elif station.rejection(ch):
                self.fail_active("ALARM", "就绪/报警条件变化：现场结果待确认", now)
            elif faults.get(ch.trigger) == "error":
                self.fail_active("INJECTED", "注入执行故障：现场结果待确认", now)
        for ch in station.channels:
            base = ch.trigger.removesuffix("触发PCtoPLC")
            record = self.records[base]
            high = bool(station.read(ch.trigger))
            if not high:
                if record["state"] in {"complete", "rejected", "latched"}:
                    record.update(state="idle", code=0, message="", plan=None)
                    station.event(ch, "idle", now)
                    self.save()
                continue
            if record["state"] == "idle":
                command = station.read(PREFIX + base + ".命令号")
                try:
                    if type(command) is not int or command <= record["accepted"] or command <= 0:
                        raise DeviceError("COMMAND", "命令号必须为大于已接受命令号的正整数")
                    if expired:
                        raise DeviceError("HEARTBEAT", "控制心跳已超时")
                    if station.read(PREFIX + base + ".取消"):
                        raise DeviceError("INVALID", "取消信号尚未复位")
                    reason = station.rejection(ch)
                    if reason:
                        raise DeviceError("NOT_READY", reason)
                    if faults.get(ch.trigger) == "reject":
                        raise DeviceError("INJECTED", "注入拒绝")
                    plan = self.plan(base)
                    station.prepare_timing(ch, now)
                    record.update(accepted=command, state="hang" if faults.get(ch.trigger) == "hang" else "running",
                                  code=0, message="", plan=plan)
                    if plan["effect"]:
                        for material in plan["targets"]:
                            self.effects.setdefault(material, {})[plan["effect"]] = False
                    # Persist acceptance before displaying running or changing material.
                    self.save()
                    station.event(ch, record["state"], now)
                except ValueError as exc:
                    record.update(state="rejected", code=getattr(exc, "code", ERRORS["INVALID"]), message=str(exc), plan=None)
                    self.save()
                    station.event(ch, "rejected", now, str(exc))
            elif record["state"] == "running" and now >= ch.deadline:
                plan = record["plan"]
                if plan["move"]:
                    source, target, material = plan["move"]
                    self.locations[source], self.locations[target] = None, material
                    self.revision += 1
                if plan["effect"]:
                    for material in plan["targets"]:
                        self.effects.setdefault(material, {})[plan["effect"]] = True
                record.update(state="complete", count=record["count"] + 1)
                self.remember_result(record)
                ch.count = record["count"]
                self.save()  # journal terminal state before exposing Complete
                station.event(ch, "complete", now)
        self.publish()

    def publish(self):
        station = self.station
        values = {PREFIX + "设备合同": VERSION, PREFIX + "启动标识": self.boot_id,
                  PREFIX + "设备忙碌": any(r["state"] in {"running", "hang", "complete"} for r in self.records.values()),
                  PREFIX + "需要恢复": self.recovery,
                  PREFIX + "物料位置JSON": json.dumps({"locations": self.locations, "uncertain": sorted(self.uncertain),
                                                       "effects": self.effects}, ensure_ascii=False, sort_keys=True),
                  PREFIX + "位置版本": self.revision, PREFIX + "恢复应答号": self.recovery_id,
                  PREFIX + "恢复已处理号": self.recovery_seen,
                  PREFIX + "恢复错误": self.recovery_error}
        for ch in station.channels:
            base = ch.trigger.removesuffix("触发PCtoPLC")
            record = self.records[base]
            ch.phase = record["state"]
            values[ch.complete] = record["state"] == "complete"
            for suffix, key in (("已接受命令号", "accepted"), ("状态", "state"), ("错误码", "code"), ("错误信息", "message")):
                values[PREFIX + base + "." + suffix] = record[key]
            values[PREFIX + base + ".上次结果JSON"] = json.dumps(record["last_result"], ensure_ascii=False)
        # PLC-visible sensors are projections of confirmed material positions.
        if station.kind == "spray":
            for typ, cn in (("feed", "原料罐"), ("reaction", "反应罐")):
                for i in range(1, 13):
                    values[f"{cn}料仓{i}号位检测"] = bool(self.locations[f"rack/{typ}/{i:02d}"])
                for lane in ("A", "B"):
                    values[f"{lane}工位{cn}工作位检测"] = bool(self.locations[f"work/{lane}/{typ}"])
            for lane in ("A", "B"):
                material = self.locations[f"work/{lane}/reaction"]
                values[f"{lane}工位实验完成PLCtoPC"] = bool(self.effects.get(material, {}).get("sprayed"))
        else:
            for typ, cn in (("feed", "原料罐仓"), ("filter", "过滤罐仓"), ("beaker", "烧杯仓")):
                for i in range(1, 13):
                    values[f"{cn}位置{i}"] = bool(self.locations[f"rack/{typ}/{i:02d}"])
            for i in range(1, 13):
                values[f"真空箱仓位置{i}"] = bool(self.locations[f"vacuum/{i:02d}"])
            values["机器人夹爪状态PLCtoPC"] = int(bool(self.locations["gripper"]))
        for name, value in values.items():
            if station.read(name) != value:
                station.write(name, value)
