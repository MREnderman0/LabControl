<!-- Generated from an independent device package; edit its README.md. -->

维护源：[包内 README](../../../src/lab_control/instruments/devices/liquid_moku_pro/README.md)。修改维护源后重新生成。

# Liquid Instruments Moku:Pro

## 能力与协议

`liquid.moku_pro` 是一个资产、一个会话。前三个物理槽位分别装载 Waveform Generator、Oscilloscope、Spectrum Analyzer，顺序不限。参见厂商 [MiM](https://apis.liquidinstruments.com/api/getting-started/starting-mim.html)、[REST](https://apis.liquidinstruments.com/api/getting-started/starting-curl.html) 和 [4.4 变更](https://apis.liquidinstruments.com/api/changelog/)。软件不调用隐式部署的仪器构造函数。

| 功能 | 范围 |
|---|---|
| 采集与保存 | 两个测量面板独立选择 ch1～ch4；同一物理输入共用前端；在一个设备工作线程交替读完整帧，并非硬件触发同步。|
| 离线回放与导出 | 示波器（s、V）/频谱仪（Hz、dBm）；逐帧保留角色、物理通道、坐标和前端快照。|
| MCP 能力与参数 | 通道、保守范围及确认门控由能力表提供；不把写回执当作物理测量。|
| MCP 远程采集 / 指标 | 每种测量一次一个通道；未进行远程端到端验收；未声明远程指标。|

## 连接与驱动

通过 `_moku._tcp.local.` 找到候选，以 REST 读取完整型号、序列号和 MokuOS 4.4。广播端口不是 REST 端口。网络要求：同一实验网的 mDNS（UDP 5353）和设备 HTTP（TCP 80）。动态地址由发现取得；链路本地 IPv6 保留网卡作用域。应用不改网、不走代理、不跟随重定向。Windows 官方上位机材料登记为 `liquid-moku-desktop`；Python 版本材料见下载资源页。仅复用已部署配置，不下载或刷写 bitstream，不需要 MokuCLI。若要首次布置槽位，请先在厂商上位机备份原配置、确认可恢复，手工部署并退出控制会话。

## 单位与限制

三个面板统一显示 ch1、ch2、ch3、ch4。信号源通道对应同号物理输出，并不暗含回环。零偏置正弦的软件范围为 1 Hz～1 MHz、4～100 mVpp；要求独占的信号源至物理输出路由、0 dB 增益及 50 Ω 终端设置，并由操作者确认实际负载。选择通道不发命令；点“开启输出”或“开始监测 / 逐帧记录”时自动核对现有四槽并分配所选端口，不必先手工接通道。首次使用需确认已有可恢复备份；不自动部署、不强占。开始监测不会开启输出，关闭输出也不会创建路由。记录期间参数锁生效：另一测量面板可以加入已经接好的输入，需改路由时提示先停止记录。信号源仅连接已读回为关闭且未驱动其他目的端的空闲逻辑通道，不替换现有其他来源。已有示波器显示源须启用。不同角色不使用第四槽。关闭面板不释放整机所有权；最后在设备池断开设备。断开不等于关闭输出。

## 可调采集参数

经典示波器面板使用“水平、垂直、触发、采集”四个页签。时基按每格秒数输入，与水平偏移、触发相对时窗联动。十格时轴与八格电压网格沿用项目的示波器图表，显示当前通道的每格电压、峰峰值、有效值和估算频率。手动显示档位与垂直中心调整本地视图，输入衰减、带宽和触发设置写入设备。

经典频谱面板把频率、幅度、带宽和软件 Marker 直接列在参数卡上。两组频率输入联动。显示参考电平控制本地 dBm 纵轴的上边界，手动范围宽度为 100 dB；输入衰减控制物理前端。固定 Marker 显示指定频点的读数，跟踪 Marker 在选定搜索范围内逐帧寻峰。Marker 按设备和物理通道保存。两个面板都支持刷新设备参数、保留未提交的草稿和按一次“应用修改”依次写入已改项目。

两个面板均可设置物理输入的阻抗、耦合、衰减和 300 / 600 MHz 模拟带宽，默认保留已有带宽。应用后立即读回，随后两个面板的采集帧也携带相同物理输入的前端快照。[四槽模式模拟前端](https://apis.liquidinstruments.com/api/reference/mim/set_frontend.html)

示波器面板可设触发点两侧的时窗和帧点数上限（128 至 16384 的二次幂）、普通或精密采集、Linear / SinX / Gaussian 插值。边沿触发支持当前物理输入对应的显示通道或外部触发源、Auto / Normal、上升沿 / 下降沿 / 双沿和 −20 至 +20 V 电平。面板独立读回时窗、采样率、采集模式与插值方式，实际点数取自返回帧。触发设置显示写入回执。[厂商时基](https://apis.liquidinstruments.com/api/reference/oscilloscope/set_timebase.html) · [边沿触发](https://apis.liquidinstruments.com/api/reference/oscilloscope/set_trigger.html) · [示波器读回](https://apis.liquidinstruments.com/api/reference/oscilloscope/getters.html)

频谱仪支持按起止频率或中心频率与跨度设置频段。厂商的频段函数页列出 Pro 的常规范围 0 至 300 MHz；4.1.1 版本说明及 API 总览说明了 600 MHz 扩展模式。面板在发送超过 300 MHz 的请求前，先读回所选物理输入的带宽；只有确认前端为 600 MHz 时才发送，随后核对仪器槽的实际频段。[频段函数](https://apis.liquidinstruments.com/api/reference/specan/set_span.html) · [频谱扩展说明](https://apis.liquidinstruments.com/api/reference/) · [4.1.1 变更记录](https://apis.liquidinstruments.com/api/changelog/changelog.html)

RBW 可选自动、手动或最小，面板显示设备取整后的实际带宽。可选九种窗函数，也可按秒设置帧平均的目标时长。频段、RBW 和窗函数有独立读回，平均时长显示写入回执。时基、频段等设置作用于所在仪器槽的全部通道。[RBW](https://apis.liquidinstruments.com/api/reference/specan/set_rbw.html) · [窗函数](https://apis.liquidinstruments.com/api/reference/specan/set_window.html) · [帧平均](https://apis.liquidinstruments.com/api/reference/specan/set_averaging.html)

## 文件与公共依赖

[协议](../../../src/lab_control/instruments/devices/liquid_moku_pro/protocol.py)、[适配器](../../../src/lab_control/instruments/devices/liquid_moku_pro/adapter.py)、[DNS-SD](../../../src/lab_control/instruments/devices/liquid_moku_pro/discovery.py)、[工作页](../../../src/lab_control/instruments/devices/liquid_moku_pro/workspace.py)、[实机计划](../../../src/lab_control/instruments/devices/liquid_moku_pro/hardware/README.md)。锁、调度、记录、回放与文件布局复用公共层。

## 测试入口与实机状态

经典工作区从信号源、示波器和频谱仪三个分类打开同一台 Moku:Pro。运行
`python -m pytest src/lab_control/instruments/devices/liquid_moku_pro/tests/test_classic_loopback.py`
可复现 CH1 模拟回接。用例通过面板设置 100 mVpp 的 10 kHz 和 20 kHz 正弦波，验证时窗、下降沿触发点、频段、手动 RBW 读回、独立停止记录与关闭输出后的对照，在 pytest 临时目录保存面板截图和模拟读数。

| 功能 / 通道 | 证据 | 分项结果 |
|---|---|---|
| 型号 / 序列号 | 正式网口 REST 只读预检；原始回包仅在运行目录 | 已核对 |
| 三分类面板 | 使用正式工作区从分类卡片打开同一台实机，核对面板、通道及共享会话 | 只读通过；未取得四槽控制权 |
| 自动分配、双路采集与记录 | 脚本化设备配合正式面板按钮、记录与导出回归 | 软件通过，不代表实机测量通过 |
| 信号源 ch1 → 示波器 ch1 / 频谱仪 ch1 | 未确认可恢复的原配置和输出状态 | 未发信号、未测 |
| ch2、ch3、ch4 物理输出测量 | 不在本次授权范围 | 未测 |

## 已知限制与待测

首版仅支持复用已有四槽配置；通道选择不抢占、不隐式开输出。不能确认每一条相关路由、数据源或完整帧时拒绝操作。共享前端的显式写入仅允许已路由到测量槽的物理通道，并保留带宽、逐项读回。未开放自动部署、厂商二进制高速帧或跨仪器同步采样。用户操作需遵循采集期间公共参数锁。

## 扩展与移除

修改本包并重新生成型号文档。移除唯一注册项后保留用户资产与历史记录。

## 注册能力（自动生成）

范围、单位及命令来自当前注册定义；资产级实测结果见运行目录。

| 操作 | 参数 / 单位 / 范围 | 作用域 | 方向 | 额外确认 | 状态 |
| --- | --- | --- | --- | --- | --- |
| `connect_generator_channel` | 无 | 通道 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `shared_frontend` | impedance: enum  50OHM/1MOHM; coupling: enum  DC/AC; attenuation: enum  0DB/20DB/40DB; bandwidth: enum  300MHZ/600MHZ | 通道 | 读/写 | 需要 | 软件已实现；实测按资产查证 |
| `attach_workspace` | 无 | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `scope_samplerate` | 无 | 整机 | 读 | 无需 | 软件已实现；实测按资产查证 |
| `scope_timebase` | t1: number s [None, None]; t2: number s [None, None]; max_length: integer  128/256/512/1024/2048/4096/8192/16384 | 整机 | 读/写 | 需要 | 软件已实现；实测按资产查证 |
| `scope_trigger` | source: enum  CURRENT/EXTERNAL; mode: enum  AUTO/NORMAL; edge: enum  RISING/FALLING/BOTH; level: number V [-20, 20] | 通道 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `scope_acquisition` | mode: enum  NORMAL/PRECISION | 整机 | 读/写 | 需要 | 软件已实现；实测按资产查证 |
| `scope_interpolation` | interpolation: enum  LINEAR/SINX/GAUSSIAN | 整机 | 读/写 | 需要 | 软件已实现；实测按资产查证 |
| `spectrum_span` | frequency1: number Hz [0, 600000000.0]; frequency2: number Hz [0, 600000000.0] | 整机 | 读/写 | 需要 | 软件已实现；实测按资产查证 |
| `spectrum_rbw` | mode: enum  AUTO/MANUAL/MINIMUM; rbw_value: number Hz [0, None] | 整机 | 读/写 | 需要 | 软件已实现；实测按资产查证 |
| `spectrum_window` | window: enum  RECTANGULAR/BARTLETT/HAMMING/HANN/BLACKMANHARRIS/FLATTOP/NUTTALL/GAUSSIAN/KAISER | 整机 | 读/写 | 需要 | 软件已实现；实测按资产查证 |
| `spectrum_averaging` | target_duration: number s [0, None] | 整机 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `standard_waveform` | frequency: number Hz [1, 1000000.0]; amplitude: number Vpp [0.004, 0.1] | 通道 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `waveform_stop` | 无 | 通道 | /写 | 需要 | 软件已实现；实测按资产查证 |
| `select_measurement_input` | role: enum  WAVEFORM/SPECTRUM | 通道 | /写 | 需要 | 软件已实现；实测按资产查证 |
