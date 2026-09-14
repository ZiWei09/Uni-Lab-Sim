# ICCAS OPC UA 仿真器

本目录提供 ICCAS 喷雾析出、过滤除泡和合成站 OPC UA 仿真，以及后处理双站设备模型、工艺计时和本地 HMI。核心仿真不依赖 Uni-Lab-OS 运行时。

## 环境

- Python 3.11
- 依赖见 `requirements.txt`

macOS/Linux 启动器会自动创建 `.venv` 并安装依赖。Windows 可先运行 `setup_venv.bat`。

## 后处理双站设备模式

```bash
./start_iccas_devices.command
```

Windows：

```bat
start_iccas_devices.bat
```

默认启动：

- 喷雾析出站：`opc.tcp://127.0.0.1:4855/iccas_sim/`
- 过滤除泡站：`opc.tcp://127.0.0.1:4856/iccas_sim/`
- 工艺时间加速：60 倍
- 持久状态：`data/runtime/iccas-devices/`

设备模型包含物料位置/夹爪见证、工位互锁、命令号去重、取消、明确错误和断线恢复。接口说明见 [`../deploy/iccas-devices/README.md`](../deploy/iccas-devices/README.md)。

## HMI

先启动设备模式，再运行：

```bash
./start_iccas_hmi.command
```

- 喷雾站：<http://127.0.0.1:18866/spray.html>
- 过滤站：<http://127.0.0.1:18866/filter.html>

详见 [`docs/iccas-hmi.md`](docs/iccas-hmi.md)。

## 其他模式

```bash
# 六 PLC / 八端点的 ICCAS 三工站仿真
./start_iccas.command

# 按工艺阶段计时的后处理双站
./start_iccas_process.command

# 查看端点和握手覆盖
.venv/bin/python iccas_sim.py --list
```

Prosys 对接和 NodeSet 导出见 [`docs/iccas-simulator.md`](docs/iccas-simulator.md)。工艺计时见 [`docs/iccas-process-timing.md`](docs/iccas-process-timing.md)。

## 测试

```bash
python -m pytest tests/test_iccas_*.py
node --test tests/test_hmi_slot_choice.cjs
```
