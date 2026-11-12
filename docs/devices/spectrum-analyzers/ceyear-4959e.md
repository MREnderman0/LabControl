<!-- Generated from an independent device package; edit its README.md. -->

维护源：[包内 README](../../../src/lab_control/instruments/devices/ceyear_4959e/README.md)。修改维护源后重新生成。

# 思仪 4959E 微波综合测试仪

## 能力与协议

`ceyear.4959e` 使用既有频谱分析仪界面，按型号、厂家和资产序列号识别。频谱分析模式（SA）可读取当前 Trace 1。可见轨迹支持刷新、最大保持、最小保持和保持。软件采集读取屏幕已有数据，采集频次不代表仪器产生新扫描的频次。

使用 SCPI 查询当前模式、数据格式、扫频方式、点数、轨迹类型、分辨率带宽以及起止频率。线性扫描返回 101–4001 点 dBm 轨迹和 Hz 坐标。零扫宽返回 dBm 时间序列，首点为零秒，末点为仪器读回的扫描时间。实现以采集前后两次配置读回核对每帧，配置变化时丢弃该帧。[厂家程控手册 C，第 3.3 节，印刷页 26–28、39、314](https://www.ceyear.com/Public/Uploads/uploadfile/files/20260709/4959chengkongshouce.pdf)

## 连接与驱动

使用 Windows 64 位 VISA Runtime 与 PyVISA，选择实际租约对应的 LAN VXI-11 资源。相同序列号可能对应多个发现地址。只读预检通过项目的资源锁和型号工厂执行，参数设置仅在手动控制时发送。

## 范围与约束

起始、终止和中心频率以及扫宽按 Hz 表示。程控频率上限为 26.5 GHz，起止频率须有序，中心频率和扫宽须组成有效区间。刻度范围为每格 0.2–40 dB。项目对写入值执行范围校验和读回比较。厂家的程控起始频率允许设为 0 Hz，标称频谱性能范围从 9 kHz 起。[厂家产品说明](https://www.ceyear.com/product-center/measurement/integrated-tester/331.html)

## 测试与证据

- 离线回归命令为 `python -m pytest src/lab_control/instruments/devices/ceyear_4959e/tests`。
- 身份、状态、重连和实际 Trace 1 的只读验收使用本包的[硬件测试入口](../../../src/lab_control/instruments/devices/ceyear_4959e/hardware/README.md)。每台资产的进度、原始回包和接线情况以运行目录本次测试批次为准。
- 普通频谱可以使用项目既有的峰值和信噪比分析。零扫宽数据通过时间轴保存。

## 已知限制

固件 2.6.14 实机在 SA 模式、标准检波下报告 1001 个扫描点，Trace 1 实际返回 2002 个幅值，扫描方式查询返回 `Error!`。厂家说明标准检波同时显示每个扫描点的最大值和最小值，程控手册未定义两组值的传输排列。软件拒绝把这条回复当作已验证的单条曲线。相同固件使用单值检波方式时，如果回包严格符合点数且扫描方式仍不能读回，软件只能提供从 0 开始的样点序号，暂不具备 Hz 坐标；此路径经过离线协议测试。[用户手册，第 3.6.3.1 节](https://www.ceyear.com/Public/Uploads/uploadfile/files/20260709/4959xilieweibozongheceshiyishiyongshuomingshu-1.pdf)

当前开放 ASCII 轨迹。厂家规定 HEX 格式每点为四字节浮点数，支持该格式还需确认实机字节序与同一条静止轨迹的 ASCII 结果一致。列表扫描须另行建立分段坐标。VNA、实时频谱与相噪等模式使用不同命令和数据含义。物理精度需要已知输入信号与参考值另行验收。

## 注册能力（自动生成）

范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `start_frequency` | value: number Hz [0, 26500000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `stop_frequency` | value: number Hz [0, 26500000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `center_frequency` | value: number Hz [0, 26500000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `span` | value: number Hz [0, 26500000000.0] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `display_scale` | value: number dB/div [0.2, 40] | 整机 | 读/写 | 无需 | 软件已实现；实测按资产查证 |
| `measurement_mode` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `available_modes` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `transfer_format` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `sweep_points` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `sweep_mode` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `trace_mode` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `detector` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `sweep_time` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `rbw` | value: number Hz [None, None] | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
