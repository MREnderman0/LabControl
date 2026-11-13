<!-- Generated from an independent device package; edit its README.md. -->

维护源：[包内 README](../../../src/lab_control/instruments/devices/keysight_n9030a/README.md)。修改维护源后重新生成。

# Keysight / Agilent N9030A PXA

## 能力与协议

适配标识 `keysight.n9030a`。通过 `*IDN?` 核对厂商和完整型号，采集 Swept SA 当前的 Trace 1。频谱监测、软件标记、记录和峰值指标沿用项目现有频谱仪页面。依据为 [Keysight N9030A SA 模式编程手册](https://www.keysight.com/me/en/assets/9018-06099/user-manuals/9018-06099.pdf) 的 Swept SA、Format Data、Y Axis Unit 章节。

## 连接与驱动

通过已有的 64 位 VISA Runtime 和 PyVISA，用已登记的 LAN/VXI-11 资源连接。发现时读取身份，以仪器返回的序列号区分资产。

## 范围与约束

| 项目 | 契约 |
| --- | --- |
| 测量模式 | `CONF?` 返回 Swept SA 后，`FETC:SAN1?` 获取现有 XY 对。频谱仪前面板负责启动和触发。 |
| 坐标与单位 | 横轴直接使用仪器返回的坐标。扫频单位为 Hz，零跨度单位为秒。纵轴采用 `UNIT:POW?` 读回的单位。项目的峰值与信噪比指标适用于 Hz/dBm 频谱。 |
| 格式和点数 | 支持当前 ASCII 或 IEEE 488.2 REAL,32 / REAL,64，按当前字节序解码。每帧与读回的扫频点数核对，拒绝异常值、非递增横轴或截断报文。 |
| 面板变化 | 每帧在轨迹前后读取模式、端点、跨度、时间、点数、单位和数据格式。设置变化会丢弃该帧。 |
| 控制 | 本包的设置读取和采集均为查询。用户可在仪器上选择测量模式、频率、带宽和触发条件。 |

## 测试入口与验证范围

包内 `tests/test_contract.py` 验证身份、只读采集、设置变化与异常回包。`tests/test_workspace.py` 通过正式 Qt 页面采集并保存离线样本。实机入口见 [只读测试](../../../src/lab_control/instruments/devices/keysight_n9030a/hardware/README.md)，具体测试结果保存在运行目录的资产批次。

| 验证项 | 已验证范围 |
| --- | --- |
| LAN/VXI-11 | 通过项目启动脚本读取型号和序列号，建立会话，读取原迹并重连核验。 |
| 实机频谱 | 固件 A.14.59，Swept SA、ASCII（ASC,8）、1001 点。该次覆盖 0 至 8.5 GHz，幅度约 −120.74 至 −39.21 dBm。 |
| 记录与显示 | 从实机批次的 SQLite 只读回放到 Qt 频谱监测页，绘图的全部 1001 对坐标与记录逐点相同，轴标签为 Hz、dBm。 |
| 重连与历史回放 | 正式窗口连续三轮断开后重新连接，均核对同一序列号。保存后的 1001 点扫频数据在数据处理页逐点回读通过。 |
| 待单独验证 | 实机 REAL,32 / REAL,64、零跨度、已知输入频点和校准电平。 |

## 已知限制与扩展

当前支持已有的 Trace 1。不支持其他测量应用、I/Q 数据或启动扫描。高中心频率、窄跨度时，REAL,32 的横轴精度可能无法区分邻点；若仪器返回重复坐标，本包拒绝该帧。绝对频率和电平精度需在已知输入、输入衰减及参考值下单独验证。

## 注册能力（自动生成）

范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `measurement_mode` | value: text  [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `start_frequency` | value: number Hz [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `stop_frequency` | value: number Hz [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `center_frequency` | value: number Hz [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `span` | value: number Hz [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `sweep_time` | value: number s [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `sweep_points` | value: integer  [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `amplitude_unit` | value: text  [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `transfer_format` | value: text  [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `byte_order` | value: text  [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
