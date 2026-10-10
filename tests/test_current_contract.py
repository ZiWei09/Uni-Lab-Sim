"""当前点表、物料证据与两种握手周期的回归测试。"""
import pytest
from current_contract import CurrentStation, current_definitions
from server import add_nodes, build_server, register_ns_padding


def make(tmp_path, kind):
    server=build_server('opc.tcp://127.0.0.1:0/test/')
    ns=register_ns_padding(server,4,'urn:test:current')
    nodes=add_nodes(server,ns,current_definitions(kind))
    return CurrentStation(kind,nodes,tmp_path,delay=.4)


@pytest.mark.parametrize('kind,count,prefix',[('spray',326,'ns=4;s=OPC|'),('filter',176,'ns=4;s=OPC_UA过滤除泡_new|')])
def test_current_contract_has_only_real_nodes(kind,count,prefix):
    defs=current_definitions(kind)
    assert len(defs)==count
    assert all(d.node_id.startswith(prefix) for d in defs)
    assert not any('ICCAS_SIM' in d.node_id for d in defs)


def test_spray_accept_complete_reset_and_no_replay(tmp_path):
    st=make(tmp_path,'spray');base='A工位原料罐库位取料'
    try:
        st.write('A工位原料罐号码',1)
        st.write('动作控制.'+base+'.命令号',1)
        st.write(base+'触发PCtoPLC',True)
        st.tick(1)
        assert st.read('动作控制.'+base+'.已接受命令号')==1
        assert not st.read(base+'完成PLCtoPC')
        st.tick(2)
        assert st.read(base+'完成PLCtoPC')
        assert st.model.equipment.locations['gripper']=='feed:01'
        st.write(base+'触发PCtoPLC',False);st.tick(3)
        assert not st.read(base+'完成PLCtoPC')
        st.write(base+'触发PCtoPLC',True);st.tick(4);st.tick(5)
        assert st.model.equipment.records[base]['count']==1
    finally: st.close()


def test_filter_plc_clears_trigger_and_stale_complete_is_not_reused(tmp_path):
    st=make(tmp_path,'filter');base='吸液针外壁清洗'
    try:
        for t in (1,4):
            st.write(base+'触发PCtoPLC',True);st.tick(t)
            assert not st.read(base+'触发PCtoPLC')
            assert not st.read(base+'完成PLCtoPC')
            st.tick(t+1)
            assert st.read(base+'完成PLCtoPC')
            st.tick(t+2)
            assert st.read(base+'完成PLCtoPC')
        assert st.model.equipment.records[base]['count']==2
        assert st.requests[base+'触发PCtoPLC']==2
    finally: st.close()


def test_filter_material_updates_before_complete(tmp_path):
    st=make(tmp_path,'filter');base='烧杯仓空烧杯取料'
    try:
        st.write('烧杯仓取空烧杯号码PCtoPLC',1)
        st.write(base+'触发PCtoPLC',True);st.tick(1)
        assert st.read('烧杯仓位置1')
        st.tick(2)
        assert st.read(base+'完成PLCtoPC')
        assert not st.read('烧杯仓位置1')
        assert st.model.equipment.locations['gripper']=='beaker:01'
        assert st.read('机器人夹爪状态PLCtoPC')==1
        # 不插入空拍：完成后立刻执行下一搬运，不能被上一周期的锁误拒绝。
        st.write('烧杯工作位放料触发PCtoPLC',True);st.tick(2.01);st.tick(3)
        assert st.read('烧杯工作位放料完成PLCtoPC')
        assert st.model.equipment.locations['work/beaker']=='beaker:01'
        assert st.model.equipment.locations['gripper'] is None
    finally: st.close()


def test_filter_process_effect_and_restart_preserve_material(tmp_path):
    st=make(tmp_path,'filter')
    try:
        time=0
        actions=[('原料罐仓取料','原料罐仓取放号码PCtoPLC'),('原料罐工作位放料',None),
                 ('过滤罐仓取料','过滤罐仓取放号码PCtoPLC'),('过滤罐工作位放料',None),
                 ('烧杯仓空烧杯取料','烧杯仓取空烧杯号码PCtoPLC'),('烧杯工作位放料',None),
                 ('抽原液启动',None)]
        for base,param in actions:
            if param:st.write(param,1)
            st.write(base+'触发PCtoPLC',True);st.tick(time);st.tick(time+1);time+=2
            assert st.read(base+'完成PLCtoPC')
        assert st.model.equipment.effects['beaker:01']['filtered']
        locations=dict(st.model.equipment.locations)
    finally:st.close()
    restarted=make(tmp_path,'filter')
    try:
        assert restarted.model.equipment.locations==locations
        assert restarted.model.equipment.records['抽原液启动']['count']==1
        assert restarted.model.equipment.effects['beaker:01']['filtered']
    finally:restarted.close()


def test_filter_rejected_move_exposes_alarm_without_false_complete(tmp_path):
    st=make(tmp_path,'filter')
    try:
        st.write('烧杯工作位取料触发PCtoPLC',True);st.tick(1)
        assert not st.read('烧杯工作位取料完成PLCtoPC')
        assert st.read('NMP桶空报警PLCtoPC')
        assert st.model.equipment.records['烧杯工作位取料']['count']==0
        assert st.model.equipment.locations['gripper'] is None
    finally:st.close()
