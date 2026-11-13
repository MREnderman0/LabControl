# Keysight E36313A

## 能力与协议

`keysight.e36313a` 精确匹配 Keysight Technologies 的 E36313A。原生身份模式使用 SCPI 显式通道列表。在设备管理总览连接后，从实时监测的电源页选择设备、查看三路读数及通道设置。依据 [Keysight E36300 Series Programming Guide](https://www.keysight.com/us/en/assets/9018-04577/programming-guides/9018-04577.pdf) 的 CURRent、VOLTage、OUTPut 和 MEASure 子系统。

## 连接与驱动

使用已有的 64 位 VISA Runtime（`rs-visa`）和 LAN/VXI-11。按序列号区分资产，经当前网络地址连接。无需专用驱动。

## 单位与限制

CH1 的标称设定范围为 0–6 V、0–10 A，CH2 和 CH3 为 0–25 V、0–2 A。独立通道支持电压设定、电流限值和需确认的输出开关。实测电压、电流使用 MEAS 查询，单位分别为 V、A。输出开关查询为仪器报告的使能状态，启用了延迟时可能早于实际输出。独立控制不操作配对、跟踪和同步配置；相关联动状态由写前查询复核。超时不重发。物理精度及写入效果待单独验收。

## 文件与公共依赖

型号命令及限制在 `adapter.py`，三路采样沿用公共电源采集器，面板与记录格式沿用现有电源。测试与脱敏响应位于 `tests/`。

## 测试入口与实机状态

[硬件计划](hardware/README.md) 按资产核对身份、读回、采集、存储和重连。运行目录保存实机批次。离线用例核对命令字节、越界、错误回包及通道隔离。
