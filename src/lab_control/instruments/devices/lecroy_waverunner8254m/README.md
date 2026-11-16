# Teledyne LeCroy WaveRunner 8254M

注册标识 `lecroy.8254m`。独立匹配 LeCroy 的 WaveRunner8254M 或 8254M，使用 C1～C4 物理模拟通道。

## 能力与协议

| 项目 | 支持范围 |
| --- | --- |
| 波形采集与保存 | 被动读取所选已开启通道，每个通道查询一次 `WF? ALL`；单帧和监测共用型号入口。通道按顺序读取，各自保留描述符时间。 |
| 离线回放与导出 | 使用帧处理，保留每通道原始时轴和物理量，可按通道导出 PNG。 |
| 参数与远程能力 | 可查询通道开关、垂直刻度与偏移、时基、时延、触发模式和传输格式。通道状态由面板控制。公共远程接口可暴露被动波形数据源。 |
| 参数写入与远程指标 | 本包未声明可写操作、触发启动或指标处理项。 |

协议依据为 [8254M 产品资料](https://www.teledynelecroy.com/oscilloscope/oscilloscopemodel.aspx?modelid=9722)、[WaveRunner 8000 操作手册的 Remote Control 章节](https://cdn.teledynelecroy.com/files/manuals/waverunner-8000-operators-manual.pdf)和 [MAUI Remote Control 手册的波形传输与 WAVEDESC 章节](https://cdn.teledynelecroy.com/files/manuals/maui-remote-control-and-automation-manual.pdf)。

## 连接与驱动

按面板 Utilities → Utilities Setup → Remote 选择连接。TCP/IP 对应 VICP，端口通常为 1861；LXI 对应 VXI-11。面板选 TCP/IP 时，VXI-11 仍可能返回正确身份，再读设置会收到接口警告。此时选择已验证的 VICP 资源。Windows x64 上 VICP 使用标准套接字，VXI-11 复用 PyVISA 和已登记的 `rs-visa` 组件。安装材料、来源及检测见[组件清单](../../../../../deployment/components.json)。

## 单位与限制

每次读取前后核对面板与传输设置。单次扫描的 WAVEDESC 提供有效样点区间、增益、偏移、时间原点和间隔。电压为有符号样码乘增益减偏移，时间为样点编号乘间隔加原点。仅接受完整、无抽点的 BYTE/WORD 二进制波形；每通道最多 1000 万个样点。停扫状态可读出保留波形，读回次数不能作为触发次数。跨通道读取不保证来自同一次触发。

## 文件与公共依赖

`profile.py` 声明查询及通道，`adapter.py` 负责采集与快照一致性。与 9404M 共用 MAUI 波形解码器，各型号维护自己的身份、能力及采集逻辑。脱敏回放在 `tests/`，只读验收和离线记录转换位于 `hardware/`。

## 测试入口与实机状态

| 项目 | 验证范围及结果 |
| --- | --- |
| VICP 连接、重连及参数读取 | 固件 10.6.0 实机通过，连续三次断开重连并比对身份和状态。 |
| VICP 四通道采集与保存 | AUTO、BYTE、小端，每通道取得 2,000,002 个有效样点。保存后重新打开，逐点核对时间和电压。 |
| 桌面监测与离线处理 | CH2 连续三帧的仪器触发时间各异。已用实机记录逐点复算四通道的 WAVEDESC 电压和时间，并核对 CH2 离线回放与 PNG 导出。 |
| Agent 能力与远程记录 | 使用正式 Agent 服务方法和限定 CH2 的授权，核对波形数据源及可见通道，启动并结束被动记录，按本次任务核实两帧完整波形。 |
| VXI-11 | 当前面板选 TCP/IP，实机能识别身份，参数读取受接口选择阻挡。LXI 面板模式待单独测试。 |
| 写控制、主动触发、外部电压精度 | 本次未执行。 |

软件测试使用本包 `tests`，实机验收见[测试入口与计划](hardware/README.md)，各资产结果以运行目录 `hardware-tests` 内的批次为准。

## 已知限制与待测

序列、RIS、截取或抽点传输不在已验证范围。WORD 的数值换算有协议回放覆盖，当前实机保持原有 BYTE 格式。界面和 Agent 的原生 Single 触发未开放；可读取面板已有波形、开始监测或连续记录。

## 扩展与移除

新增命令先核对当前型号和固件，再补相应回放及实机验收。移除本型号时撤销唯一注册项、删除本包并刷新派生文档，保留资产历史。
