# FSPN-26 只读实机核验

在正式运行目录中，使用只包含这台设备的 VISA 配置文件并填写核实过的 `expected_serial`，执行：

```bat
.venv-dev\Scripts\python.exe -m lab_control.instruments.devices.rs_fspn26.hardware.readonly --config <config-file>
```

入口复用项目的硬件锁、型号工厂、会话与记录生成器。核对身份，记录当前应用与传输格式，再读取当前显示的 L(f) 迹线，按点数、单位、偏频顺序与读取前后设置验证，随后检查重连与最终只读快照。

每次批次保存原始通信、阶段结果及现场接线条件。如果只存在 ADEV 表或尚无载波和有效曲线，标记本次曲线待补测。补测时在前面板接入已知 CW 源，完成载波测量，对照同次曲线与偏移频点。参数写入、输出及精度另行实测。
