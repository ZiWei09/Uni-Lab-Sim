import json

import pytest

from iccas_device import ERRORS, PREFIX
from iccas_sim import Station, definitions
from server import add_nodes, build_server, register_ns_padding


def make(kind="filter", path=None):
    server = build_server("opc.tcp://127.0.0.1:0/test/")
    ns = register_ns_padding(server, 4, "urn:iccas:sim")
    st = Station(kind, add_nodes(server, ns, definitions(kind, device_model=True)),
                 device_model=True, device_state=path, delay=0.1)
    st._test_time = 0
    return st


def step(st, seconds=0.2, faults=None):
    st._test_time += seconds
    st.tick(st._test_time, faults=faults)


def start(st, base, params=None, command=None, faults=None):
    for name, value in (params or {}).items():
        st.write(name, value)
    if command is None:
        command = st.equipment.records[base]["accepted"] + 1
    st.write(PREFIX + base + ".命令号", command)
    st.write(base + "触发PCtoPLC", True)
    step(st, faults=faults)
    return st.equipment.records[base]


def reset(st, base):
    st.write(base + "触发PCtoPLC", False)
    st.write(PREFIX + base + ".取消", False)
    step(st)


def execute(st, base, params=None):
    record = start(st, base, params)
    assert record["state"] == "running", record
    step(st)
    assert st.read(base + "完成PLCtoPC"), record
    reset(st, base)


def recover(st, locations=None):
    for ch in st.channels:
        st.write(ch.trigger, False)
        st.write(PREFIX + ch.trigger.removesuffix("触发PCtoPLC") + ".取消", False)
    st.write(PREFIX + "恢复位置JSON", json.dumps(locations or st.equipment.locations))
    st.write(PREFIX + "恢复请求号", st.equipment.recovery_seen + 1)
    step(st)


def test_initial_material_witnesses_and_gripper_interlock():
    st = make()
    assert st.read("烧杯仓位置1") is True
    record = start(st, "烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1})
    assert st.read("烧杯仓位置1") is True  # not committed at Trigger
    step(st)
    assert st.read("烧杯仓位置1") is False
    assert st.read("机器人夹爪状态PLCtoPC") == 1
    reset(st, "烧杯仓取料")
    record = start(st, "原料罐仓取料", {"原料罐仓取料号码PCtoPLC": 1})
    assert record["code"] == ERRORS["OCCUPIED"]
    assert not st.read("原料罐仓取料完成PLCtoPC")


def test_parameter_latched_and_wrong_container_rejected():
    st = make()
    start(st, "烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1})
    st.write("烧杯仓取料号码PCtoPLC", 2)
    step(st)
    assert st.equipment.locations["gripper"] == "beaker:01"
    reset(st, "烧杯仓取料")
    record = start(st, "原料罐工作位放料")
    assert record["code"] == ERRORS["INVALID"]
    assert st.equipment.locations["gripper"] == "beaker:01"


def test_filter_complete_material_cycle_and_process_parallelism():
    st = make()
    for cn in ("原料罐", "烧杯", "过滤罐"):
        execute(st, cn + "仓取料", {cn + "仓取料号码PCtoPLC": 1})
        execute(st, cn + "工作位放料")
    assert start(st, "抽原液启动")["state"] == "running"
    assert start(st, "吸液针外壁清洗")["state"] == "running"
    step(st)
    reset(st, "抽原液启动")
    reset(st, "吸液针外壁清洗")
    assert st.equipment.effects["beaker:01"]["filtered"]
    for cn in ("烧杯", "原料罐", "过滤罐"):
        execute(st, cn + "工作位取料")
        execute(st, cn + "仓放料", {"烧杯仓放料号码PCtoPLC": 1} if cn == "烧杯" else {})
    execute(st, "烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1})
    execute(st, "真空箱放料", {"真空箱放料号码PCtoPLC": 12})
    assert st.read("真空箱仓位置12")
    execute(st, "真空箱除泡启动", {"真空箱放置烧杯数量PCtoPLC": 1})
    execute(st, "真空箱取料", {"真空箱取料号码PCtoPLC": 12})
    execute(st, "烧杯仓放料", {"烧杯仓放料号码PCtoPLC": 1})
    assert st.equipment.locations == st.equipment.initial_locations("filter")
    assert st.equipment.effects["beaker:01"]["defoamed"]
    assert not st.read("真空箱仓位置12")


def test_conflicting_process_and_motion_rejected():
    st = make()
    for cn in ("原料罐", "烧杯", "过滤罐"):
        execute(st, cn + "仓取料", {cn + "仓取料号码PCtoPLC": 1})
        execute(st, cn + "工作位放料")
    start(st, "抽原液启动", faults={"抽原液启动触发PCtoPLC": "hang"})
    record = start(st, "烧杯工作位取料")
    assert record["code"] == ERRORS["BUSY"]
    assert st.equipment.locations["work/beaker"] == "beaker:01"


def test_spray_ab_occupancy_and_parallel_runs():
    st = make("spray")
    for lane, slot in (("A", 1), ("B", 2)):
        for cn in ("原料罐", "反应罐"):
            execute(st, lane + "工位" + cn + "库位取料", {lane + "工位" + cn + "号码": slot})
            execute(st, lane + "工位" + cn + "工作位放料")
    start(st, "A工位后处理雾化", faults={"A工位后处理雾化触发PCtoPLC": "hang"})
    assert start(st, "B工位后处理雾化")["state"] == "running"
    step(st)
    assert st.read("B工位实验完成PLCtoPC")
    assert not st.read("A工位实验完成PLCtoPC")
    assert st.equipment.locations["work/B/reaction"] == "reaction:02"


@pytest.mark.parametrize("mode,lane,allowed", [(0, "A", True), (0, "B", True),
                                              (1, "A", True), (1, "B", False),
                                              (2, "A", False), (2, "B", True),
                                              (-1, "A", False), (3, "B", False)])
def test_spray_mode_controls_lane_admission(mode, lane, allowed):
    st = make("spray")
    st.write("实验工位选择0_AB1_A2_BPCtoPLC", mode)
    before = dict(st.equipment.locations)
    base = lane + "工位原料罐库位取料"
    record = start(st, base, {lane + "工位原料罐号码": 1})
    assert record["state"] == ("running" if allowed else "rejected")
    if not allowed:
        assert record["code"] == ERRORS["NOT_READY"]
        assert st.equipment.locations == before
        assert not st.read(base + "完成PLCtoPC")


def test_disabling_active_spray_lane_requires_recovery():
    st = make("spray")
    base = "B工位原料罐库位取料"
    start(st, base, {"B工位原料罐号码": 1})
    st.write("实验工位选择0_AB1_A2_BPCtoPLC", 1)
    step(st)
    assert st.equipment.records[base]["state"] == "error"
    assert st.equipment.recovery
    assert not st.read(base + "完成PLCtoPC")
    assert st.equipment.locations["gripper"] is None


@pytest.mark.parametrize("cause", ["cancel", "lower_trigger", "heartbeat", "alarm", "fault"])
def test_interrupted_move_requires_explicit_recovery(cause):
    st = make()
    base = "烧杯仓取料"
    start(st, base, {base + "号码PCtoPLC": 1}, faults={base + "触发PCtoPLC": "hang"})
    if cause == "cancel":
        st.write(PREFIX + base + ".取消", True)
    elif cause == "lower_trigger":
        st.write(base + "触发PCtoPLC", False)
    elif cause == "heartbeat":
        st.write(PREFIX + "心跳监控启用", True)
        step(st)
        step(st, 16)
    elif cause == "alarm":
        st.write("门开报警PLCtoPC", True)
    step(st, faults={base + "触发PCtoPLC": "error"} if cause == "fault" else None)
    assert st.read(PREFIX + "需要恢复")
    assert not st.read(base + "完成PLCtoPC")
    assert st.equipment.uncertain == {"rack/beaker/01", "gripper"}
    assert st.equipment.locations["rack/beaker/01"] == "beaker:01"  # last confirmed, not presumed rollback
    st.write("门开报警PLCtoPC", False)
    st.write(PREFIX + "心跳监控启用", False)
    reset(st, base)
    assert start(st, "原料罐仓取料", {"原料罐仓取料号码PCtoPLC": 1})["code"] == ERRORS["RECOVERY"]
    recover(st)
    assert not st.read(PREFIX + "需要恢复")
    assert json.loads(st.read(PREFIX + base + ".上次结果JSON"))["state"] == "error"
    execute(st, base, {base + "号码PCtoPLC": 1})


def test_recovery_cannot_duplicate_material_and_failed_request_does_not_retry():
    st = make()
    bad = dict(st.equipment.locations)
    bad["gripper"] = "beaker:01"
    recover(st, bad)
    assert st.read(PREFIX + "恢复应答号") == 0
    assert st.read(PREFIX + "恢复错误")
    st.write(PREFIX + "恢复位置JSON", json.dumps(st.equipment.locations))
    step(st)
    assert st.read(PREFIX + "恢复应答号") == 0
    recover(st)
    assert st.read(PREFIX + "恢复应答号") == 2
    assert st.read(PREFIX + "恢复已处理号") == 2
    st.write(PREFIX + "恢复请求号", 1)
    step(st)
    st.write(PREFIX + "恢复请求号", 2)
    before = st.equipment.revision
    step(st)
    assert st.equipment.revision == before


def test_completed_command_and_world_survive_restart_without_replay(tmp_path):
    path = tmp_path / "equipment.json"
    st = make(path=path)
    execute(st, "烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1})
    st.equipment.close()
    restored = make(path=path)
    assert restored.equipment.locations["gripper"] == "beaker:01"
    record = start(restored, "烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1}, command=1)
    assert record["code"] == ERRORS["COMMAND"]
    previous = json.loads(restored.read(PREFIX + "烧杯仓取料.上次结果JSON"))
    assert previous["accepted"] == 1 and previous["state"] == "complete"
    restored.equipment.close()


def test_running_restart_requires_reconciliation_and_journal_is_exclusive(tmp_path):
    path = tmp_path / "equipment.json"
    st = make(path=path)
    start(st, "烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1})
    with pytest.raises(RuntimeError, match="占用"):
        make(path=path)
    st.equipment.close()  # simulated process crash after accepted journal
    restored = make(path=path)
    assert restored.equipment.recovery
    assert restored.equipment.records["烧杯仓取料"]["code"] == ERRORS["RESTART"]
    assert not restored.read("烧杯仓取料完成PLCtoPC")
    restored.equipment.close()


def test_empty_and_bad_batch_rejected():
    st = make()
    assert start(st, "真空箱除泡启动", {"真空箱放置烧杯数量PCtoPLC": 1})["code"] == ERRORS["EMPTY"]
    reset(st, "真空箱除泡启动")
    execute(st, "烧杯仓取料", {"烧杯仓取料号码PCtoPLC": 1})
    execute(st, "真空箱放料", {"真空箱放料号码PCtoPLC": 1})
    assert start(st, "真空箱除泡启动", {"真空箱放置烧杯数量PCtoPLC": 2})["code"] == ERRORS["INVALID"]


def test_spray_two_batches_return_all_vessels():
    st = make('spray')
    for slot in (1, 2):
        for lane in 'AB':
            for cn in ('原料罐', '反应罐'):
                execute(st, f'{lane}工位{cn}库位取料', {f'{lane}工位{cn}号码': slot})
                execute(st, f'{lane}工位{cn}工作位放料')
            execute(st, f'{lane}工位后处理雾化')
            for cn, typ in (('原料罐', 'feed'), ('反应罐', 'reaction')):
                execute(st, f'{lane}工位{cn}工作位取料')
                execute(st, f'{lane}工位{cn}库位放料')
                assert st.equipment.locations[f'work/{lane}/{typ}'] is None
                assert st.equipment.locations[f'rack/{typ}/{slot:02d}']
            assert st.equipment.locations['gripper'] is None


def test_spray_journal_adds_only_return_channels(tmp_path):
    path = tmp_path / 'spray.json'
    st = make('spray', path)
    execute(st, 'A工位原料罐库位取料', {'A工位原料罐号码': 1})
    st.equipment.close()
    saved = json.loads(path.read_text())
    additions = {lane+'工位'+vessel+action for lane in 'AB' for vessel in ('原料罐','反应罐')
                 for action in ('工作位取料','库位放料')}
    for base in additions:
        del saved['records'][base]
    path.write_text(json.dumps(saved))
    restored = make('spray', path)
    try:
        assert restored.equipment.locations['gripper'] == 'feed:01'
        assert len(restored.equipment.records) == 21
        assert restored.equipment.records['A工位原料罐库位取料']['accepted'] == 1
    finally:
        restored.equipment.close()
