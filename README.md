# ICCAS 双站仿真

本分支只保留 ICCAS 后处理双站仿真、它的运行依赖以及相关交付物。

- 主程序：[`PLC-Sim/`](./PLC-Sim/)
- 设备接口包：[`deploy/iccas-devices/`](./deploy/iccas-devices/)
- Prosys 导出：[`deploy/prosys-iccas/`](./deploy/prosys-iccas/)
- 完整保留包：[`deploy/后处理双站仿真保留包_2026-09-14/`](./deploy/后处理双站仿真保留包_2026-09-14/)

快速启动（macOS/Linux）：

```bash
cd PLC-Sim
./start_iccas_devices.command
```

另开终端启动 HMI：

```bash
cd PLC-Sim
./start_iccas_hmi.command
```

喷雾站 HMI：<http://127.0.0.1:18866/spray.html>
过滤站 HMI：<http://127.0.0.1:18866/filter.html>
