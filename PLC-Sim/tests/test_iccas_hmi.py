import json
import time

from fastapi.testclient import TestClient
from iccas_hmi import Panel, create_app
from iccas_device import PREFIX
from test_iccas_device_network import equipment_server


def test_hmi_real_opc_commands(equipment_server):
    endpoint, launch = equipment_server
    launch()
    panel = Panel('filter', endpoint)
    panel.start()
    try:
        deadline = time.monotonic() + 8
        while not panel.snapshot()['online'] and time.monotonic() < deadline:
            time.sleep(.05)
        assert panel.snapshot()['online']
        app = create_app()
        app.state.panels['filter'] = panel
        # Routes capture the same mapping; avoid lifespan opening the real demo endpoints.
        client = TestClient(app)
        assert client.get('/filter.html').status_code == 200
        assert client.get('/spray.html').status_code == 200
        assert client.post('/api/filter/parameters', json={'values': {'烧杯仓位置1': False}}).status_code == 409
        assert client.post('/api/filter/start', json={'action': '烧杯仓取料', 'position': 0}).status_code == 409
        assert client.post('/api/filter/start', json={'action': '烧杯仓取料', 'position': 1}, headers={'Origin': 'https://other.example'}).status_code == 403
        result = client.post('/api/filter/start', json={'action': '烧杯仓取料', 'position': 1})
        assert result.status_code == 200, result.text
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = client.get('/api/filter/state').json()['values']
            if state.get(PREFIX+'烧杯仓取料.已接受命令号') == 1 and state.get(PREFIX+'烧杯仓取料.状态') == 'idle':
                break
            time.sleep(.05)
        assert state[PREFIX+'烧杯仓取料.状态'] == 'idle'
        assert state['烧杯仓取料触发PCtoPLC'] is False
        assert state['烧杯仓位置1'] is False
        world = json.loads(state[PREFIX+'物料位置JSON'])
        assert world['locations']['gripper']
        assert world['locations']['rack/beaker/01'] is None
        assert client.post('/api/filter/start', json={'action': '烧杯仓取料', 'position': 2}).status_code == 200
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            events = client.get('/api/filter/state').json()['events']
            failures = [e for e in events if e.get('state') == 'rejected']
            if failures:
                break
            time.sleep(.05)
        assert failures[0]['code'] == 1003
        assert failures[0]['action'] == '烧杯仓取料'
        assert failures[0]['id']
        assert '占用' in failures[0]['reason']
    finally:
        panel.close()


def test_offline_and_disabled_lane_are_not_written():
    import pytest
    panel = Panel('spray', 'opc.tcp://127.0.0.1:1/')
    with pytest.raises(ValueError, match='离线'):
        panel.operate('start', {'action': 'B工位后处理雾化'})
    panel.online = True
    panel.refresh = lambda: None
    base = 'B工位后处理雾化'
    panel.values = {PREFIX+'需要恢复': False, base+'触发PCtoPLC': False,
                    base+'完成PLCtoPC': False, PREFIX+base+'.取消': False,
                    '远程模式PLCtoPC': True, '设备准备就绪PLCtoPC': True,
                    '实验工位选择0_AB1_A2_BPCtoPLC': 1}
    panel.write = lambda *args: pytest.fail('Disabled lane must not write OPC UA')
    with pytest.raises(ValueError, match='未启用'):
        panel.operate('start', {'action': base})
