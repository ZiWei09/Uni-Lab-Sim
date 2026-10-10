"""当前 ICCAS 点表适配；仅暴露真实 CSV 节点，复用仿真物料守恒模型。"""
from __future__ import annotations

import csv
import json
import signal
import time
from pathlib import Path

from opcua import ua

try:
    from .common import NodeDef, DEFAULT_MAP, VTYPE_MAP
    from .server import add_nodes, build_server, register_ns_padding
    from .iccas_sim import Station, definitions, load_faults
    from .iccas_device import PREFIX, control_definitions
except ImportError:  # 直接执行源码入口。
    from common import NodeDef, DEFAULT_MAP, VTYPE_MAP
    from server import add_nodes, build_server, register_ns_padding
    from iccas_sim import Station, definitions, load_faults
    from iccas_device import PREFIX, control_definitions

DATA = Path(__file__).parent / 'data/current'


class MemoryNode:
    """模型内部状态；不会增加任何 OPC UA 节点。"""
    def __init__(self, dtype, value=None):
        self.dtype = VTYPE_MAP[dtype]
        self.value = DEFAULT_MAP[dtype] if value is None else value
    def get_value(self):
        return self.value
    def get_data_type_as_variant_type(self):
        return self.dtype
    def set_value(self, value):
        self.value = value.Value


def current_definitions(kind):
    filename = 'spray_real.csv' if kind == 'spray' else 'filter_defoam_real.csv'
    result = []
    with (DATA / filename).open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            name = row['变量名称']
            dtype = {'REAL': 'FLOAT', 'DOUBLE': 'DOUBLE'}.get(row['数据类型'], row['数据类型'])
            initial = True if name in {'远程模式PLCtoPC', '设备准备就绪PLCtoPC'} else None
            if name == '设备管理.接口版本': initial = 'V001'
            result.append(NodeDef(name, row['功能及读写说明'], 'VARIABLE', dtype, row['NodeId'],
                                  initial_value=initial, write_owner='host' if '写' in row['访问权限'] else 'plc'))
    if len({d.node_id for d in result}) != len(result):
        raise ValueError('当前点表含重复 NodeId')
    return result


def spray_alias(name, actions):
    if name.startswith(PREFIX):
        tail = name[len(PREFIX):]
        name = ('动作控制.' if any(tail.startswith(a + '.') for a in actions) else '设备管理.') + tail
    return {'实验工位选择0_AB1_A2_BPCtoPLC': '实验工位启用0_AB1_A2_BPCtoPLC',
            '设备管理.位置版本': '设备状态.位置版本'}.get(name, name)


class CurrentStation:
    def __init__(self, kind, nodes, state_dir, *, delay=.4, watchdog_s=15):
        self.kind, self.nodes = kind, nodes
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        if kind == 'spray':
            legacy = definitions('spray', device_model=True)
            actions = [d.name_cn.removesuffix('触发PCtoPLC') for d in legacy if d.name_cn.endswith('触发PCtoPLC')]
            aliases = {}
            for definition in legacy:
                old = definition.name_cn
                mapped = spray_alias(old, actions)
                if mapped in nodes:
                    aliases[old] = nodes[mapped]
                elif old in {PREFIX + '设备合同', PREFIX + '物料位置JSON', PREFIX + '恢复位置JSON'} or old.endswith('.上次结果JSON'):
                    aliases[old] = MemoryNode(definition.data_type, definition.initial_value)
                else:
                    raise ValueError(f'喷雾模型字段未映射: {old}')
            self.model = Station(kind, aliases, delay=delay, device_model=True,
                                 device_state=state_dir / 'spray.json', watchdog_s=watchdog_s)
        else:
            mapping = json.loads((DATA / 'filter_mapping.json').read_text())
            actions = mapping['stations']['filter']['actions']
            aliases = dict(nodes)
            for definition in control_definitions(actions):
                aliases[definition.name_cn] = MemoryNode(definition.data_type, definition.initial_value)
            for action in actions:
                # 模型使用内部握手；外部只显示 PLC 主动复位 Trigger、锁存 Complete 的协议。
                aliases[action + '触发PCtoPLC'] = MemoryNode('BOOLEAN')
                aliases[action + '完成PLCtoPC'] = MemoryNode('BOOLEAN')
            self.model = Station(kind, aliases, delay=max(delay,.4), device_model=True,
                                 device_state=state_dir / 'filter.json', device_mapping=DATA / 'filter_mapping.json')
        self.requests = {ch.trigger: 0 for ch in self.model.channels}

    def read(self, name):
        return self.nodes[name].get_value()

    def write(self, name, value):
        node = self.nodes[name]
        node.set_value(ua.Variant(value, node.get_data_type_as_variant_type()))

    def tick(self, now=None, faults=None):
        now = time.monotonic() if now is None else now
        if self.kind == 'spray':
            # 真机恢复只有请求号：仿真使用自身持久化的确认位置，外部不得伪造位置点。
            self.model.write(PREFIX + '恢复位置JSON', json.dumps(self.model.equipment.locations))
            self.model.tick(now, faults)
            return
        equipment = self.model.equipment
        for ch in self.model.channels:
            base = ch.trigger.removesuffix('触发PCtoPLC')
            record = equipment.records[base]
            if self.read(ch.trigger) and record['state'] == 'idle':
                self.write(ch.complete, False)
                self.model.write(PREFIX + base + '.命令号', record['accepted'] + 1)
                self.model.write(ch.trigger, True)
                self.requests[ch.trigger] += 1
                self.write(ch.trigger, False)
        self.model.tick(now, faults)
        completed = [ch for ch in self.model.channels
                     if equipment.records[ch.trigger.removesuffix('触发PCtoPLC')]['state'] == 'complete']
        for ch in completed:
            self.model.write(ch.trigger, False)
        # 外部 Complete 是下一动作的许可：先结束内部周期并释放锁，再发布完成。
        if completed:
            self.model.tick(now, faults)
        for ch in completed:
            self.write(ch.complete, True)
        if equipment.recovery or any(r['state'] in {'rejected','error','latched'}
                                     for r in equipment.records.values()):
            # 当前点表无通用错误码；使用现有报警点，同时在快照保留真实拒绝原因。
            alarm = next(name for name in self.nodes if '报警' in name)
            self.write(alarm, True)
        for typ, name in [('feed','原料罐'),('filter','过滤罐'),('beaker','烧杯')]:
            sensor = name + '工作位检测'
            if sensor in self.nodes:
                self.write(sensor, bool(equipment.locations['work/' + typ]))

    def snapshot(self):
        value = self.model.snapshot()
        value['contract'] = 'current-point-table-v1'
        value['node_count'] = len(self.nodes)
        value['exposed_nodes'] = len(self.nodes)
        value['requests'] = self.requests
        return value

    def close(self):
        self.model.equipment.fail_active("RESTART", "服务停止：执行结果待现场确认", time.monotonic())
        self.model.equipment.close()


def run(args):
    if args.station not in ('postprocessing','spray','filter'):
        raise ValueError('当前点表模式只支持 postprocessing、spray 或 filter')
    if args.timing_mode != 'handshake' or args.filter_extensions:
        raise ValueError('当前点表模式不接受旧 process 时序或扩展点')
    kinds = ('spray','filter') if args.station == 'postprocessing' else (args.station,)
    if args.list:
        for kind in kinds:
            print(kind, len(current_definitions(kind)), 'current-point-table-v1')
        return 0
    if not args.device_state_dir:
        raise ValueError('当前点表模式必须指定 --device-state-dir 保存动作与位置证据')
    if args.time_scale != 1 or args.device_mapping or args.timing_profile or args.seed_process_recipe:
        raise ValueError('当前点表模式使用固定动作延迟 --delay，不接受旧模型映射或工艺计时选项')
    stop = False
    def terminate(*_):
        nonlocal stop
        stop = True
    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGINT,signal.SIGTERM)}
    servers, stations = [], {}
    try:
        for kind in kinds:
            port = args.base_port + int(kind == 'filter')
            server = build_server(f'opc.tcp://{args.host}:{port}/iccas_sim/')
            servers.append(server)
            ns = register_ns_padding(server,4,'urn:INOVANCE:AutoSys:UserVar')
            defs = current_definitions(kind)
            nodes = add_nodes(server,ns,defs)
            for d in defs:
                if d.write_owner == 'plc':nodes[d.name_cn].set_read_only()
            stations[kind] = CurrentStation(kind,nodes,args.device_state_dir,
                                            delay=args.delay,watchdog_s=args.watchdog_seconds)
            server.start()
            print(f'{kind}: current contract, {len(nodes)} nodes, port={port}',flush=True)
        while not stop:
            faults = load_faults(args.fault_file, {k: v.model for k, v in stations.items()})
            for kind, station in stations.items():station.tick(faults=faults.get(kind))
            if args.state_file:
                args.state_file.parent.mkdir(parents=True,exist_ok=True)
                temporary = args.state_file.with_suffix('.tmp')
                temporary.write_text(json.dumps({k:v.snapshot() for k,v in stations.items()},ensure_ascii=False,indent=2))
                temporary.replace(args.state_file)
            time.sleep(.02)
    finally:
        for station in stations.values():station.close()
        for server in reversed(servers):server.stop()
        for sig,handler in previous.items():signal.signal(sig,handler)
    return 0
