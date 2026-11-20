# 经典版仪器界面接入规范

本页适用于 `master` 的 PySide6 仪器页面。接入型号时，先从同类设备进入实际页面，核对常用操作的位置、单位和状态，再决定需要扩展哪些控件。首页和主题沿用已确认的深色琥珀分类卡片。[视觉基线和截图](../../direction-approved.md) · [前端架构](frontend-framework.md)

## 选择接入方式

| 设备的工作方式 | 接入位置 | 例子 |
| --- | --- | --- |
| 属于现有类别，通道、可写参数和采集数据源均可声明 | 在型号包声明能力与数据源，使用公共类别页。接入时从设备池验证控制或监测入口 | RIGOL DSA815 使用公共频谱页和型号声明的 RBW、VBW 档位 |
| 同类设备都需要一个当前缺少的操作 | 在公共类别页或共享控件扩展，依据通道能力决定是否显示、何时可用。协议和量化规则留在型号包 | 示波器的采集点数按型号提供的范围编辑 |
| 一台物理设备承担多个角色，各自选择通道或占用路由 | 在型号注册项声明 `role_categories` 和 `role_page_factory`，各角色复用对应类别的图表与参数布局，并共用设备会话 | Moku:Pro 的信号源入口打开控制页，示波器和频谱仪入口打开对应测量页 |

页面入口由公共导航分发。型号 README 写明所用类别页、与同类设备的差异，以及这些差异对应的硬件或工作流原因。[导航实现](../../src/lab_control/ui/workspace/navigation.py) · [型号模块规范](device-modules.md)

## 经典页面的布局和控件

从其他分支移植功能时，把操作放回经典页已有的位置。例如，频谱记录用图表工具栏上的记录按钮，Marker 用频谱参数区的标记列表。需要补充公共样式时，在运行主题里为对应 Qt 控件或状态属性添加规则。

| 位置 | 接入时沿用的做法 |
| --- | --- |
| 首页与设备池 | 分类卡片使用细边框、琥珀顶边和重点数字，按窗口宽度排列。点击卡片进入设备池，从选定设备进入控制或监测。浏览页面和切换通道应保留当前输出状态。 |
| 测量页顶栏 | 用 `DeviceSelector` 选择设备，旁边放连接状态和开始／暂停监测。记录按钮放在图表工具栏内，频谱页在同一栏选择采集间隔，示波器在此提供曲线工具。 |
| 示波器与频谱图表 | 用 `MeasurementChart` 组织标题、工具栏、曲线和底部状态。图表占主要空间，参数区放在分隔器另一侧。缩窄窗口时检查参数滚动、单位、标记列表以及底部按钮是否可达。 |
| 输入与按钮 | 量纲、可选单位、范围提示优先复用 `ParameterEditor`。范围来自选定设备与通道的能力。分组标题、辅助文字和主操作按钮使用主题已有的 `objectName`，如 `spectrumChartTitle`、`rangeHint`、`primaryButton`。 |
| 状态 | 深色底、灰白正文和琥珀重点色沿用公共 QSS。连接、警告、异常、未连接各有语义色，并配文字。记录按钮和描边复用公共记录组件，根据准备、记录和二进制收尾阶段更新。 |

对应实现见[运行主题](../../src/lab_control/ui/styles/card_workspace.qss)、[设备选择器](../../src/lab_control/ui/widgets/device_selector.py)、[测量页布局](../../src/lab_control/ui/pages/monitoring/measurement_layout.py)、[参数编辑器](../../src/lab_control/ui/pages/device_control/editors.py)和[记录状态](../../src/lab_control/ui/pages/monitoring/recording_state.py)。

## 三类仪器的对齐方式

**示波器。** 左侧显示时域波形，右侧按水平、通道垂直、触发和采集设置组织。常用调节保留每格单位，精确输入和对应滑块保持一致。型号只提供部分触发模式或采集档位时，选项随能力收窄。Moku 的垂直显示档位和中心调整本地视图，输入衰减、模拟带宽和触发设置则作用于设备。验证时分别检查纵轴变化、写入回执或独立读回。[公共示波器页](../../src/lab_control/ui/pages/monitoring/oscilloscope.py) · [Moku 面板说明](../devices/oscilloscopes/moku-pro.md)

**频谱仪。** 参数使用双列布局。将起始／终止频率与中心／扫宽联动，把参考电平、衰减和分辨率带宽放在同类用户熟悉的位置。DSA815 的手动 RBW、VBW 使用对应离散档位。Moku 提供 RBW、窗函数和帧平均时长，其参考电平调整本地 dBm 显示上界，输入衰减会改变物理前端。软件 Marker 复用公共标记列表与曲线操作，固定标记读指定频点，跟踪标记在所选范围内寻峰。[公共频谱页和标记操作](../../src/lab_control/ui/pages/monitoring/spectrum.py) · [DSA815 型号说明](../../src/lab_control/instruments/devices/rigol_dsa815/README.md)

**信号源。** 以物理输出通道组织频率、幅度、单位和输出开关。使用者要能分清正在编辑的参数、上一次设备回执和实际测量值。Moku 的正弦输出在确认信息中列出物理通道、频率、mVpp、零偏置和 50 Ω 负载条件。打开页面、修改草稿和切换通道都不应发送开启输出命令。[公共信号源页](../../src/lab_control/ui/pages/device_control/signal_source.py) · [Moku 角色页](../../src/lab_control/instruments/devices/liquid_moku_pro/workspace.py)

同一设备有多个角色时，写明共享资源及其影响。以 Moku 为例，示波器和频谱仪可以分别选择物理输入；选中同一个输入时，两者共享阻抗、耦合、衰减和模拟带宽。读数、导出数据和记录状态应能追溯到实际角色与通道。[设备接入流程](../testing/onboarding.md) · [采集与记录规则](live-acquisition.md)

## 验收

这些项目用于新增或改版面板。已有型号的覆盖情况以该分支的测试用例和设备台账为准。

1. 从首页分类卡片和设备池打开型号，核对实际资产、角色与通道。通过模拟通信记录检查，单纯导航和取消确认不会写设备。
2. 选一个本地显示参数和一个可写仪器参数（按型号适用）。检查单位换算、范围、刷新时保留草稿，以及超时或回执不符时的提示。对可能已经执行的输出命令，显示最终状态待核实，保留其他通道的已知状态，并检查通信记录中只有一次写入。
3. 开始监测、开始记录，再停止最后一个记录角色。核对图表、记录按钮和参数锁与写盘阶段一致。多角色设备还应覆盖“一个角色停止、另一个继续采集”。
4. 用同一模拟输入分别打开同类公共页与型号页，核对控件位置和可达性。Moku 的经典版回接用例覆盖 1430 × 900 和缩窄到 1100 × 700 的窗口，验证时域读数、频谱峰值、软件 Marker，并在测试临时目录保存截图。实机结果按资产和测试批次单独登记。

可直接运行以下软件回归。各型号按本次涉及的类别选取用例。[示波器用例](../../tests/integration/test_oscilloscope_workspace.py) · [频谱用例](../../tests/integration/test_spectrum_panel.py) · [Moku 模拟回接](../../src/lab_control/instruments/devices/liquid_moku_pro/tests/test_classic_loopback.py)

```powershell
.venv-dev\Scripts\python.exe -m pytest -q tests/integration/test_oscilloscope_workspace.py tests/integration/test_spectrum_panel.py
.venv-dev\Scripts\python.exe -m pytest -q src/lab_control/instruments/devices/liquid_moku_pro/tests/test_classic_loopback.py
```
