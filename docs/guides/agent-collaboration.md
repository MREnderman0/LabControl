# MCP 接入

在采数电脑打开“系统设置 → MCP 接入”。本机客户端通过此服务访问已核验的仪器、采集任务和实验结果。首次安装默认关闭。

## 首次连接

1. 按[控制与监测](control-and-monitoring.md)完成设备发现及资产核对。确认完整型号、序列号与稳定资产 ID；不能读取序列号时，本机核对铭牌与地址映射。
2. 打开“启用服务”。状态显示“运行中”后，点击“复制地址”即可取得本机 MCP 地址，默认是 `http://127.0.0.1:8765/mcp`。
3. 如需改端口，输入 1024–65535 之间的端口，按回车或点击“应用”。成功后重新复制地址或导出配置。若新端口被占用，原地址会继续响应。重启沿用上次保存的启用状态和端口。
4. “导出配置 → 连接 JSON”供本机连接器使用。“Codex 配置”一次生成 TOML 片段和同目录的连接 JSON。将片段加入客户端 MCP 配置，并保留其引用的 JSON 文件。导出目标重名时请选择新文件名。
5. 卡片显示已自动授权的设备数量。桌面签发的全授权覆盖已识别的受支持设备，包含参数写入、输出、触发、采集和结果读取。新识别的设备自动加入，身份冲突的设备跳过。客户端先调用 `get_server_info` 取得当前授权 ID，再用它操作设备。

OFC Host v0.1.6 的“远程设备控制”支持从示波器或频谱仪当前选定通道执行“获取单帧图像”。在命令列表选中并加入队列即可。LabControl 根据采集帧的实际数据绘制 PNG，保存在采数机的 `E:\OFC-Share\单帧图像\YYYY-MM-DD\`。文件名为“时-分-秒-毫秒-槽位名-型号-通道或轨迹-波形或频谱.png”，缺少槽位时省略槽位段，重名从 `-02` 开始依次编号。OFC 的成功状态表示 PNG 已发布到该目录，任务结果的 `path` 为完整路径。

示波器的“获取单帧图像”、波形 `acquire_once` 和需要新采单帧的指标要求通道采集授权及 `output_trigger`。服务发起原生 Single，等待本次触发完成，再校验点数、采样间隔和波形覆盖。长时基或平均采集可能超过 30 秒，OFC 按任务状态等待并支持取消。任务结束时恢复原来的运行或停止状态。示波器在记录或收尾期间会拒绝新的 Single。

在示波器页点「开始监测」右侧箭头可选择「Single · 单次采集」或取消正在等待的采集。图上右键可将当前实测帧保存为白底 PNG，或保存原始数据。手动导出的部分波形会注明实测区间。实时监测、相噪和历史回看的曲线图都使用数据处理页的导图配色。

当前桌面自动授权带有 `full_device_access: true`，其中空的 `channels` 和 `operations` 表示授权范围采用型号支持的全部通道和参数能力，具体能力用 `get_capabilities` 查询。

发行连接器与 GUI 共用 `launcher.main`，但在导入 Qt、创建 Runtime 或加载硬件前进入连接器分支。`LabControl.exe` 保持无控制台；`LabControl-Agent.exe` 提供标准输入输出。旧入口 `TF-LabControl-Agent.exe` 也可使用。GUI 仍由唯一 `run.bat` 或正式安装入口启动。

```powershell
.\LabControl-Agent.exe --agent-connector --connection .\lab-control-connection.json --check
# 源码开发环境中的同一分发入口
.venv-dev\Scripts\python.exe -m lab_control --agent-connector --connection .\lab-control-connection.json --check
```

本机也可在 Codex 中直接配置 Streamable HTTP：

```toml
[mcp_servers.lab_control]
url = "http://127.0.0.1:8765/mcp"
```

Codex 的工具审批独立于软件内的实验授权。默认导出保留客户端原有审批策略；若要按本机已授予的实验范围连续调用，可仅在该 MCP 服务配置中设置 `default_tools_approval_mode = "approve"`。服务器仍逐次校验授权状态、范围、输出权限和配置版本。选项依据 [Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。

服务器校验 Host 和 Origin，单请求上限 1 MiB，每个监听最多 32 个在途 HTTP 请求。服务只监听本机回环地址。升级会忽略旧配置中的实验网地址和端口。

## 一次协作实验

建议让 Agent 执行以下流程，并明确本次实验的设备、范围与结束条件：

1. `get_server_info` 获取当前有效的共享授权；`list_devices` 选定稳定资产 ID。
2. `connect_device` 连接或复用会话。默认不进行改变采集格式的初始化；需要时显式请求 `configure_acquisition` 并具备相应授权。已存在的只读会话需明确断开后重新连接才能升级初始化权限。
3. `get_capabilities` 获取型号能力、通道、单位、范围、数据源及采集默认动作；`get_device_state` 获取缓存状态；`read_parameters` 读回需要的设置。型号声明需要确认的采集同样要求输出/触发权限，不能用 `initiate=false` 绕过。
4. `preview_change` 生成五分钟有效的单项修改预览，然后 `apply_change` 执行。预览绑定会话和配置版本；GUI 或其他协作者改变配置后，返回 `configuration_conflict`，重新读取与规划。
5. `acquire_once` 单次采集并保存，或 `start_monitoring` / `start_recording` 交由软件持续调度。`get_latest_measurement` 返回质量、真实时间、帧序号、配置版本、年龄和有限摘要；大数组保存在数据目录。
6. `list_tasks` / `get_task_status` 跟踪实际结果。指定 `stop_task` 停止后等待二进制排空与 CSV 转换；`list_artifacts` / `get_artifact` 获取结果引用。

对于数据源含 `duration` 的完整定时采集，使用 `start_recording` 并授予记录及所需输出/触发权限。服务调用型号的有限采集入口，等待硬件、二进制记录和 CSV 收尾，核实完整结束帧后发布 `recording_id` 和 `result.complete=true`。停止或缺失结束帧时报告取消或失败。客户端用完成记录调用 `start_experiment`。相噪偏频处理支持此类多帧记录，取最后一份完整结束帧中的相噪曲线。采集请求仅使用接口声明的字段，`confirmed` 由服务端根据授权设置。

所有有副作用的请求须携带在本台服务的内部接入中唯一的 `request_id`，客户端可生成 UUID。相同 ID 和参数返回原任务，相同 ID 配不同参数会被拒绝。超时、部分完成或状态未知时，按原 ID 查询任务。已接受的设备操作在网络请求取消后仍会执行并记录结果。

设备 worker 对 GUI 与多个客户端串行执行。设备已有监测/记录时，新启动返回 `task_conflict`，可通过任务列表找到当前任务（本地界面任务 ID 以 `gui-` 开头）。使用具备该设备和通道停止权限的授权即可停止任务。需要改变数据源或从监测转为记录时，明确停止原任务、等待收尾，再启动新任务。

状态包括 `accepted`、`running`、`stopping`、`binary_saved`、`converting`、`completed`、`partial`、`failed` 与重启后的 `interrupted`。`phase_history` 保存观察到的阶段。短暂阶段可能在相邻查询间完成，不能据此认为没有保存。记录参数锁直到二进制收尾才释放；CSV 转换不占用仪器。沿用公共[采集和记录规则](../architecture/live-acquisition.md)，无效帧不保存，慢磁盘满缓冲明确暂停并排空。

## 按功能槽位选择设备

外部控制端可以先选设备类别，再选功能槽位；槽位代表逻辑设备角色，不是仪器的物理通道。接口复用桌面软件的槽位定义和绑定，不需要再维护一份设备映射。

| 工具 | 参数 | 返回 |
| --- | --- | --- |
| `list_slots` | 必填 `grant_id`，可选 `category`，`workspace` 默认 `general` | 当前授权内已绑定设备的槽位列表；设备未连接也可列出 |
| `resolve_slot` | 必填 `grant_id`、`slot_id`，`workspace` 默认 `general` | 该槽位当前绑定的单个快照 |

每个快照含 `slot_id`、`label`、`category`、`workspace`、`device_id`、`identity`、`state`、`cached=true` 和 `binding_fingerprint`。类别 ID 沿用 [工作区定义](../../src/lab_control/domain/workspace.py)；工作区支持 `general` 及兼容工作区 `electronic`、`optical`。`state` 是缓存会话状态，未连接时可为空，不表示远端仪器响应成功。查询不扫描、不连接、不读写硬件。

列表仅包含授权设备的有效绑定，无匹配项返回空列表。无效或撤销的授权沿用错误码 `grant_expired`。解析未绑定、已删除或无权访问的槽位统一返回 `slot_unavailable`。已授权设备的身份发生冲突时，须在本机重新核对资产。

持久化使用工作区和槽位 ID，名称只用于显示。同名删除重建是新槽位。绑定指纹依据工作区、槽位 ID、设备 ID 与设备身份计算，用于检测换绑，不是占用锁，也不是授权凭据。一次查询取得一致快照；返回后绑定仍可能变化。

后续控制应在执行前重新解析、核对目标，再通过已有 `get_capabilities`、参数预览/执行及读回接口操作实际 `device_id`。不能将缓存状态当成读回，不能仅凭旧槽位快照发令。多操作条目的占用及绑定原子校验尚未提供，需在接入整条目执行时补齐。

OFC 自动化外壳目前先接入连接配置与待配置条目，实际 SSH 联机、参数编辑和仪器执行另行实现。服务接口保持原设备 ID 工具兼容。软件检查结果与尚未完成的实机验收见[槽位查询验证记录](../reviews/slot-query-2026-11-05.md)。

## SSH 命令与指标任务

另一台电脑可以通过 SSH 在采数电脑执行现有连接器命令，使用 UTF-8 JSON 收发。采数电脑的桌面软件须已运行并启用 MCP 服务。连接 JSON 使用导出的本机地址配置，SSH 登录沿用系统配置。

以下是 SSH **远端执行**的命令；先将工作目录设为项目目录，发行版可替换为完整发行目录内的 `LabControl-Agent.exe`：

```powershell
.venv-dev\Scripts\python.exe -m lab_control --agent-connector --connection .\connection.json --call get_server_info
.venv-dev\Scripts\python.exe -m lab_control --agent-connector --connection .\connection.json --call get_processing_capabilities --arguments-file .\request.json
```

`request.json` 是工具参数对象，例如 `{"grant_id":"授权ID","device_id":"设备ID"}`。也可使用 `--arguments-file -`，由远端程序把 UTF-8 JSON 写入 SSH 标准输入；SSH 不分配交互终端（`-T`），标准输出是一条 JSON 响应。退出码 0 表示工具调用成功（异步任务可能仍在运行），1 表示工具拒绝，2 表示连接器配置/传输失败。查询成功返回 failed 任务时仍是调用成功，须读取任务 state。传输失败不自动重发写入，保留 request_id 并查询任务。

先用 `get_processing_capabilities` 确认该设备支持的处理项及参数。使用本次桌面会话签发的自动授权。等待采集任务完成并取得 `recording_id`，也可用 `list_recordings` 选择已有规范记录。将记录 ID 放进 `recording_ids` 提交给 `start_experiment`。支持多份记录的处理项可使用同一设备的多次单帧采集。用 `get_task_status` 等计算完成，再将任务的 `result_id` 传给 `get_processing_result`。单份记录也可通过 `start_processing` 提交。

例如，示波器已保存一帧电压波形时，给 `start_experiment` 发送以下参数。读回结果后从 `data.metrics["ch1.rms"]` 和 `data.metrics["ch1.std"]` 取 CH1 的数值、单位及质量。

```json
{
  "grant_id": "<授权 ID>",
  "device_id": "<示波器 ID>",
  "request_id": "<本次请求 ID>",
  "recording_ids": ["<波形记录 ID>"],
  "profile_id": "voltage.waveform_statistics",
  "parameters": {}
}
```

已算结果可通过 list_processing_results 查找，再直接 get_processing_result，不会再次计算；重新提交相同输入默认复用，force 明确重新计算并保留旧结果。返回的每项指标有数值、单位及质量，原始记录不完整、模拟结果和 unavailable 都不能当作合格判据。完整参数、型号契约及失败语义见[远程指标处理规范](../architecture/remote-metrics.md)。不在此流程自动配置 SSH、修改防火墙或启动无人值守服务。

## 文件下载

`get_artifact` 返回下载路径。下载请求按 `grant_id` 再次校验设备和通道结果授权。文件以已登记的产物 ID 引用。可用连接器独立下载：

```powershell
.\LabControl-Agent.exe --agent-connector --connection .\lab-control-connection.json --download <产物ID> --grant <当前授权ID> --output .\experiment.csv
```

已有目标文件拒绝覆盖；下载失败保留 `.partial`，不能当作完整结果。授权包含多个通道的文件要求调用者拥有其中所有通道的权限。软件记录成功、设备读回成功和物理输出测量是不同结论，Agent 必须分别报告。

## 停止、退出和留档

卡片仅列出当前 MCP 任务。点击对应行的“停止并保存”可结束采集并等待记录收尾。完成记录按日期写入软件运行目录的 `logs` 文件夹。关闭接入仅停止网络访问，已经接受的任务仍由桌面端跟踪。重启后用 `get_server_info` 获取新授权，未确认的旧任务保留为中断状态。

关闭软件先停止接入和任务，再等待保存、释放会话与进程锁。停止、撤销和退出均沿用现有输出行为，不自动关闭或恢复设备输出；未知状态不能解释为关闭。驱动内部阻塞仍受现有 I/O 超时和驱动退出能力约束。

运行目录中的数据库保存任务去重、授权模板和产物索引。审计将远程请求归为内部 MCP，记录授权、请求、设备身份、修改前状态、读回与最终结果。必要审计失败时拒绝新写入，停止和清理仍可执行。审计或索引失败会保留错误。实际地址、序列号、证书私钥和原始通信记录均不提交源码。

## 开发验证与实现入口

```powershell
# 仅使用隔离 Mock，验证官方 SDK、本机 HTTP、stdio、旧协议及下载
.venv-dev\Scripts\python.exe scripts/check_agent.py
# 指定已安装 Codex CLI，进行真实 Agent 工具调用；不更改用户 Codex 配置
.venv-dev\Scripts\python.exe scripts/check_agent.py --codex <codex.exe路径>
# 完整非硬件检查、目录构建、隔离发行目录冻结 worker 与连接器
.venv-dev\Scripts\python.exe scripts/check_project.py --desktop
```

实现入口是 [Agent 门面](../../src/lab_control/application/agents/service.py)、[授权策略](../../src/lab_control/application/agents/policy.py)、[MCP 传输](../../src/lab_control/agent/server.py)、[工具定义](../../src/lab_control/agent/tools.py)、[连接器](../../src/lab_control/agent/connector.py)和[本地面板](../../src/lab_control/ui/pages/agent_collaboration.py)。网络线程不操作 Qt，不创建第二个硬件 Runtime，设备工作仍走原会话、租约与记录流水线。

协议使用固定的 [官方 Python MCP SDK 2.3.0](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)，旧协议由 SDK 兼容层处理；Codex 配置依据[官方 MCP 说明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。材料维护见[生成下载资源页](../generated/download-resources.md)。本次软件证据和待验收项见[协作实验验证记录](../reviews/agent-collaboration-2026-10-29.md)。
