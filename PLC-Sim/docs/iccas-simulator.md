# ICCAS 工站协议仿真机

该模型复用 PLC-Sim 的 OPC UA Server 与类型映射，独立运行，不依赖 Uni-Lab-OS。
四份供应商点表快照来自本工作区 Uni-Lab-ICCAS 的
`unilab_iccas/config/address_tables/`，原始字节保存在 `data/iccas_*.csv`。

## 启动

安装 PLC-Sim 后：

```bash
plc-sim iccas --list
plc-sim iccas --state-file /tmp/iccas-state.json
```

源码运行：

```bash
cd PLC-Sim
./start_iccas.command
# 已有环境可直接运行：
.venv/bin/python iccas_sim.py --state-file /tmp/iccas-state.json
```

Windows 先运行 `setup_venv.bat`，再运行 `start_iccas.bat`。
Ctrl+C 停止本次启动的全部服务。端口被占用时启动失败，不会终止已有进程。
该入口直接启动 OPC UA Server 与扫描循环，无需其他代理进程。


| 工站                                 | 默认端口   | 点数    | 已建模握手数 |
| -------------------------------------- | ------------ | --------- | -------------- |
| 喷雾`spray`                          | 4855       | 173     | 13           |
| 过滤除泡`filter`                     | 4856       | 158     | 18           |
| 合成本体`body`                       | 4857       | 17      | 2            |
| 合成反应站`reaction_1`～`reaction_5` | 4858～4862 | 每站 45 | 每站 3       |

全部端点路径为 `/iccas_sim/`，默认仅监听 `127.0.0.1`。
总计 8 个端点、573 个变量、48 个握手通道。
喷雾/过滤 NodeId 为 `ns=4;s=上位机通讯|<变量名>`，合成为 `ns=4;s=<变量名>`，
与当前 ICCAS 部署图的 OPC UA 配置一致。协议是匿名 NoSecurity。

`--station spray|filter|synthesis|body|reaction_1|...|all` 可选择启动范围。
`--base-port 14855` 将整组端口平移；单站选择保留原偏移，例如 filter 为 14856。
`--host` 设置监听地址，`--delay` 设置默认动作秒数（默认 0.5），
`--time-scale` 设置正数倍率（默认 1）。修改端口后，客户端部署图也需对应修改。

## 握手行为

- Trigger 拉高后进入执行，延时结束置 Complete；Trigger 保持高电平不会重复执行。
- Complete 保持为真直到客户端拉低 Trigger；提前拉低取消本次模拟动作。
- 合成 Running 从接受触发保持到 Trigger 复位，保证客户端不会漏读短动作。
- 喷雾 A/B 及过滤清洗通道可并发；同一合成 PLC 同时只接受一个动作，各反应站独立。
- 喷雾/过滤预置远程、就绪状态，报警默认为假；未就绪或有报警时不反馈成功。
- 合成本体校验机器人程序 1～27、目标机台 1～5；反应站校验移液来源 1～6、
  目标 1～2、正数移液量。拒绝保持到 Trigger 复位，不会在条件改善后自动重放。
- 过滤抽原液和真空除泡使用点表毫秒参数及倍率，最低为默认动作时长。
  喷雾复合过程和合成动作使用默认延时；未推测合成搅拌时间的单位。

## 状态和故障注入

`--state-file` 原子写入每个端点的点数、各握手 phase、完成计数和最近 200 条事件。
时间戳使用进程单调时钟，不是日历时间。状态文件是观测输出，不用于重启恢复。

通过 `--fault-file /tmp/iccas-faults.json` 可在运行期注入：

```json
{
  "spray": {"A工位后处理雾化触发PCtoPLC": "hang"},
  "reaction_1": {"PC_TO_PLC.移液触发": "reject"}
}
```

`hang` 接受下一次触发但不完成，用于验证客户端超时；`reject` 拒绝下一次触发。
修改文件使用临时文件加原子替换，避免扫描读到半份 JSON。未知工站、点名、模式或
无效 JSON 会使服务报错退出，避免错误配置悄悄失效。恢复时清空对应配置，并先将
Trigger 拉低；取消注入不会自动重放已经拒绝或挂起的命令。

报警和准备状态可以通过 OPC UA 客户端写入现有变量。后处理双站可使用
`start_iccas_hmi.command` 启动本地 HMI。

## 明确边界

这是协议联调模型，不是物理数字孪生：容器传感器、条码、TIP 消耗、液量、机械运动、
机器人互锁和化学结果没有建立动态模型，不能据完成信号推断物料实际到位或库存结算。
非握手传感器保持默认零值，可供客户端人工设置；不会自动预置满仓。

过滤除泡的“烧杯仓放料”和“真空箱取料”不在 158 点合同中，默认模式未创建这些节点。
使用 `--filter-extensions` 可显式增加两组仿真握手（号码 Int16、触发/完成 Boolean）
及只读标识 `ICCAS_SIM.过滤除泡扩展合同=filter-return-v1`，均使用相同 NodeId 前缀。
此时过滤站为 165 点/20 通道，全机为 580 点/50 通道。两组号码均为 1～12，
错误号码不完成；支持故障注入。原始供应商点表不变。
驱动需设置 `simulation_extensions=true`；可选择 ICCAS 仓库的
`deployment/graphs/iccas-filter-defoam-extended-sim.json`。默认实机缺口门禁仍保留。
合成本体的机器人初始化、机器人复位没有独立完成合同，本版仅创建原始点位，
不为它们编造握手。目标条码原表类型空白，本版显式使用仿真专用 String 占位；
不将它视为供应商确认类型，当前驱动也不写此点。

仿真不会解决 ICCAS 设备包已有的 `action_contract_invalid` 编译阻塞；
完整 Workflow、库存和实机验收仍需分别取得证据。

## 验证

```bash
ICCAS_REFERENCE_ROOT=/path/to/Uni-Lab-ICCAS \
  python -m pytest tests/test_iccas_sim.py -q
```

覆盖点表字节漂移、NodeId/类型、缺失合同、并发、复位、取消、报警、故障、参数拒绝，
以及四类 Server 上所有已建模握手的真实 OPC UA 客户端读写。
不提供参考目录时只跳过跨仓库点表字节比对。

2026-09-14 扩展后本机验证：上述两个测试文件共 27 项通过，包含八端点子进程同时启动、
逐端点握手和 SIGTERM 后端口释放。使用 Python 3.11，设置
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 避免本机 ROS pytest 插件冲突。
这是协议层验证，未宣称完整 Uni-Lab-OS Workflow 验收通过。
