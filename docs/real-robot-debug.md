# G1-D 真机 OS 接入调试记录

日期：2026-09-08。范围为通过 PhyAgentOS 的 g1d-observe/real 读取设备与图像，
进行 Agent 视觉问答及运行稳定性诊断。没有发送真机升降、机械臂、底盘或夹爪动作。

## 真机结果

- USB 网卡 enx9c69d36fbc0b，电脑地址 192.168.123.99/24。
- 192.168.123.161、192.168.123.164 均可达。
- 原生 Core 启动 g1d-observe/real 成功，状态与三路相机工具均 ready。
- 读取到 16 个关节的状态及 rt/hispeed_state 柱高，simulated=false。
- 首次柱高约 0.000765 m，仅表示柱自身反馈坐标，零点及机械行程尚未确认。
- 头部图像 1280×480，左右腕图像各 640×480，均获得新鲜真实 JPEG。
- 原生 Agent 通过 context → camera_snapshot → image 分析了三路真实图像。
  画面主要包含办公桌面、显示器、纸箱、机械结构与近处遮挡。
  模型回答不是逐项人工认证；头部右眼更模糊的现象仍存在。
  （历史记录：该现象已于 2026-09-09/10 前后修复，见文末「头部双目清晰度」更正。）

## 接收阻塞的定位和规避

mock 和 real 最初均在持续运行后失去 Endpoint 注册，节点在停止时也无法响应。
新增 SIGUSR1/faulthandler 诊断后，线程栈显示主线程停在 node.py 的
`await node.recv_async()`，另一个线程位于 asyncio 的线程安全唤醒路径。
证据保存于 build/recv-async-stall-stack.txt。

Dora 0.4.1 将 recv_async 标记为实验性接口；这里的栈证据定位了阻塞边界，
并未证明上游内部锁的全部因果关系。
设备节点现使用 `await asyncio.to_thread(node.next, 0.5)`，沿用官方同步接收接口。
同步 next 实现会释放 GIL，放入工作线程后不占用 Action/取消所在的异步事件循环。
保留 SIGUSR1 栈诊断能力，不修改运动命令和机械限制。

设备节点已重新构建、计算 SHA-256，并通过 Core 原生安装器更新两个 Skill 和共享节点。
真实制品锁见 dist/build-report.json。Core 源码未修改。

## 验证范围

- 16 项设备、Gateway 和 Bundle 自动化测试通过。
- mock 的状态序号持续推进、三路相机 ready：120 秒通过。
- 模拟升降到位、取消和动作前后证据通过。
- real 的状态序号持续推进、三路相机 ready：120 秒通过。
- 真机三路图像 Agent 问答完成，完整输入输出已保存。
- 两个 Skill 均正常停止，新节点未再因停止无响应而被强杀。

这属于有时间边界的接入验收，不等于长时间无人值守或真机运动安全验收。
本次结束后两个 Skill 均停止，Dora 基础进程保留运行。

## 可重复的只读检查

从 PhyAgentOS-g1d 执行：

```bash
./scripts/core-env.sh paos skill start g1d-observe --profile real
./scripts/core-env.sh python scripts/diagnostics/check_runtime_health.py --profile real --seconds 120
./scripts/core-env.sh python scripts/diagnostics/check_agent_vision.py --profile real
./scripts/core-env.sh paos skill stop g1d-observe
```

check_runtime_health.py 仅访问 Gateway 的只读状态/上下文；视觉诊断将真实快照发送给
已配置的 DeepSeek 服务，并在上传前核对 profile、simulated 标记和快照路径来源。

## 记录文件

- dist/real-observe-report.json：首次真实状态与三路相机快照元数据。
- dist/real-agent-vision-report.json：实际工具调用、模型结果和 Agent 回答。
- dist/real-agent-vision-transcript.md：可读的完整视觉诊断记录。
- dist/mock-runtime-health.json、dist/real-runtime-health.json：持续健康检查。
- dist/receive-fix-action-report.json：接收方式调整后的模拟 Action 回归。
- .paos-instance/logs/skills/：Core 与 Dora 原生运行日志。

## 首次只读验收时待确认的信息（历史状态）

configs/device.yaml 仍为 enabled=false、limits_confirmed=false，行程上下限为 null。
继续升降验收前，需要现场确认柱高坐标零点、最小/最大行程和本次目标高度，
以及现场看护和行程内无障碍。不能根据此次近零的反馈值推断机械行程。

## 上升 10 cm 的准备检查（2026-09-08）

用户提供整机最低尺寸 1260×525×570 mm、最高尺寸 1680×525×570 mm，
本次要求从当前位置上升 10 cm。整机高度差为 420 mm；该尺寸差本身不能证明
`rt/hispeed_state.y` 的零点、正方向或可直接用于控制的上下限。

通过 Core 启动 g1d-observe/real，再经 ForgeToolClient 的 g1d.state Query
连续读取 10 次反馈：柱高均为 0.0007648469763807952 m，序号从 685 增至 739，
反馈年龄约 18.6–43.4 ms，simulated=false。若确认升高时反馈增加，
按本次读数计算的临时目标为 0.1007648469763808 m；实际执行前必须重新读取，
使用当时高度加 0.100 m，不能直接下发整机高度 1.36 m。

检查报告保存于 dist/lift-10cm-preflight.json。尚待确认最低位置对应反馈 0 m、
向上为正、反馈可用范围为 0–0.42 m，以及现场有人看护、可急停、上升范围内无障碍。
未将整机尺寸当作已确认的控制限位，未修改 enabled/limits_confirmed，未发送运动命令。

## 用户确认后的首次真实升降（2026-09-08）

用户随后明确确认：最低位置反馈为 0 m、向上为正、可用范围为 0–0.42 m，
现场有人看护并可急停，上升范围内无障碍。本节取代上文准备阶段的待确认状态。
configs/device.yaml 已写入确认的限位并启用升降；保留最大归一化速度指令 0.15，
最长动作时间改为 30 秒。重新生成并使用 Core 原生安装器安装 Bundle，核对实际安装配置。
Core 源码、SDK 桥接库和已验收节点二进制没有改变。16 项测试与修改文件 lint 均通过。

执行链为 Core Runtime → ForgeToolClient → 官方 Gateway → Dora → g1d_node → SDK。
这是操作员指定目标的 Tool API 真机验收，未让模型规划动作，未创建 AgentTask 或进行
Core AgentTask verifier 验收，不能将结果描述为完整 Agent 自主任务验收。

scripts/diagnostics/run_lift_once.py 默认只读预览，加 --execute 才发送一次真实上升 10 cm 请求。
脚本读取安装后 ToolSpec 限位、动作前三路快照与稳定反馈，再将最新高度加 0.100 m。
动作请求不自动重试；期间监测反馈，异常则请求取消并继续核对终态。重复显式运行
--execute 会产生新的相对上升目标，不应把它用于重试结果不明的旧动作。

本次仅发送一个 Action：

| 项目 | 结果 |
| --- | --- |
| 起始柱高 | 0.0007648469763807952 m |
| 目标柱高 | 0.1007648469763808 m |
| Action 最终柱高 | 0.09889949858188629 m |
| 实际上升（动作后 Query 减去起始读数） | 0.09813497198047116 m（98.13 mm） |
| 到位允许误差 | 0.002 m（2 mm） |
| 耗时 | 10.953650987998117 s |
| Action 终态 | succeeded / target_reached，simulated=false |
| 停止结果 | stop_command_accepted=true、stopped_observed=true |

停止确认依据为新鲜柱高反馈持续稳定，不是独立的制动器或驱动器停止应答。
运动结束后 g1d-lift Runtime 已正常关闭，没有继续补偿运动或自动下降。
配置仍保留已确认的升降能力；Runtime 停止，后续动作需显式启动和调用。

完整报告及前后三路图像路径：dist/lift-10cm-20260908T072027.075855Z/report.json。
只读预览：dist/lift-10cm-20260908T072015.269567Z/report.json。

## 后续 DeepSeek Agent 真实下降 5 cm

在用户明确指定下降 5 cm 后，进一步通过 Core 原生 AgentLoop、Skill 激活、AgentTask、
PlanRevision、任务绑定工具调用及独立验收服务跑通真机。这里只读/操作员脚本阶段的范围
限制已由后续验收补充，完整细节见 [AgentTask 升降验收](agent-task.md)。

任务 task_9659384a96fc4357 最终为 succeeded，enforce verdict=success，
三项条件全部 satisfied，8 项前后证据完整。仅一次下降 Action，验收后读取的下降量
约 48.10 mm，目标误差约 1.90 mm，在设定的 2 mm 容差内；运动 Runtime 已关闭。

## 0.1.11 Dex1 行程实测与编码器闭合真机验证（2026-09-11）

0.1.10 首次真机试跑暴露三个问题：开爪目标 5.4 rad 是厂商 XR 遥操作映射常量而非机械行程；
闭合用到达容差 0.15 rad 判接触，夹住白盒（实测 0.1144 rad）被误报为“空或薄”；
抬臂姿态下腕部相机看不到手指。0.1.11 改为编码器接触闭合 + 主相机复核。

操作员在机器人空闲、双手无物、手臂静止条件下同意逐步实测内部 Dex1。只运动手指电机
（左 31 / 右 33），不运动手臂、肘与升降柱，每个目标都是独立 Action 并轮询到终态。

| 项目 | 左 dex1 | 右 dex1 |
| --- | --- | --- |
| 逐步开爪 stall 实测 | 3.356 rad | 5.256 rad |
| 空夹闭合静止位 | 0.0254 rad | 0.0207 rad |
| 写入配置的开爪目标 | 5.21 rad | 5.21 rad |

左侧 3.356 rad 是上次抓取后参数异常状态，不是机械行程（两手夹爪为同一硬件）。配置按两侧
相同写入 5.21 rad（5.256 − 0.05），并在注释中记录左侧故障；参数未恢复前左开爪 Action 会
如实失败，不再静默停在 3.356。证据：dist/dex1-travel-left.json、dist/dex1-travel-right.json。

0.1.11 部署后仅用内部 Dex1 运动做了验证，未运动手臂、肘与升降柱，完整记录见
dist/real-0.1.11-verification.json：

| 检查 | 结果 |
| --- | --- |
| 契约：outcome 枚举 | opened / contact_detected / fully_closed / interrupted，含 blockage_rad |
| 公布包络 | left_dex1=(0.0, 5.21)、right_dex1=(0.0, 5.21)，描述不再称 5.4 |
| 超程目标 left_dex1=5.3 | failed，G1D_INVALID_ARGUMENTS“outside application envelope 0.0..5.21”，手指未动（Δ0.0000 rad） |
| 右开爪到配置目标 | succeeded，实测 5.1875 rad（目标 5.21，容差 0.15） |
| 右空夹闭合 | fully_closed，blockage_rad=null，停位 0.0209（空夹静止位 0.0207） |
| 左空夹闭合 | fully_closed，blockage_rad=null，停位 0.0256（空夹静止位 0.0254） |
| 主相机快照 | succeeded，head 1280×480，age 8 ms |
| 主相机观察回路（head 源） | perception_session 8 s 抓 16 帧；会话进行中 perception_state 读到新鲜 head 帧（1280×480 双目）与 9 帧历史 |
| 主相机能否看到夹爪 | 抬臂姿态历史帧中可见抬起的左臂、腕部相机与所持白盒（画面小） |

未验证项：`contact_detected` 需要真实物体挡在指间，须由操作员放置物体时在场复测；
抬臂姿态下的主相机复核需要在同一次真机流程中确认。这两项不在本次只动手指的验证范围内，
不能据此认为完整抓取闭环已验收。

### 左手 Dex1 停在 3.356 rad 的只读诊断

只为诊断做了一次手指运动（`left_dex1` 目标 3.6 rad，无臂、肘、升降柱运动），其余为只读订阅。

| 检查 | 结果 |
| --- | --- |
| 电机 31 状态（rt/lowstate，unitree_hg） | mode=1、motorstate=0x0（无错误位）、46 V、温度 37–39 °C、静止扭矩 −0.039 N·m |
| 开到 3.6 rad 的实测 | 手指到 3.3559 rad 后 10.21 s 内位置变化 ≤0.010 rad，电机持续约 0.84 N·m，温度不升 |
| Action 终态 | failed / reference_frozen（目标 3.6 超出停止点，未到位，deadline 冻结参考） |
| 同一命令下的右手指 | 5.1875 rad 正常到位，容差 0.15 rad 内 |

结论：3.356 rad 是**硬限位**——电机在持续输出约 0.84 N·m 的情况下位置几乎完全不动、温度不升，
不符合摩擦、过温保护或编码器/电气故障的表现；右手指为同型号硬件且正常开到 5.19 rad。
本仓库可用工具无法读或写电机内部参数（vendored unitree_sdk2_python 与 xr_teleoperate 都只有
运动/状态接口，没有参数读写接口），因此「参数被改错」既无法从这里证实，也无法从这里恢复。
下一步要么做机械检查（断电后查左手指连杆、异物、腕部相机支架是否挡住开爪路径），
要么走厂商工具/服务读取电机参数。证据：dist/dex1-left-finger-stall-torque.json、dist/dex1-travel-left.json。

顺带记录的影响：0.1.11 的左开爪目标按两侧同为 5.21 rad 配置，所以左手指在卡死解除前每次开爪
都会以 0.84 N·m 压到停止点、直到 Action deadline 才失败。若要避免这种长时间按压，
可以照闭合的接触判定思路给开爪也加一个 stall 早停（新增 outcome），属于改变 Action 语义，需先确认。

## 0.1.12 角度管理与松开后回归闭合位（2026-09-11）

操作员修复左手指后，两次只动手指的复核确认故障已消除：左开爪 5.1933 rad、右开爪 5.1852 rad，
两侧空夹闭合均为 `fully_closed`（0.0239 / 0.0218，与记录的空夹静止位一致）。
3.356 rad 一节描述的硬限位现象在本机已不再出现；该节保留为当时的只读诊断记录。

按用户要求加入角度管理（0.1.12）：**夹住即锁定角度**——判定接触后手指保持在测得停位，
`blockage_rad` 即本次确认的夹持角度，在下一条命令之前不变；**松开即回到闭合位**——
新增 `operation:"release"`，先开到配置的开爪目标，确认到位后由 Action 自己再闭回空夹静止位。

| 检查 | 结果 |
| --- | --- |
| 公布契约 | operation 枚举 open/close/release，描述含 release，版本 0.1.12 |
| 左 release（空夹，起点 5.2112） | succeeded，`fully_closed` / `released_then_closed`，终位 0.0232，参考保持，3.54 s |
| 右 release（空夹，起点 0.0236） | succeeded，`fully_closed` / `released_then_closed`，终位 0.0132，参考保持，3.60 s |
| release 只够一段的 timeout_s=2.0 | failed / `GRASP_DEADLINE`，手指未动（Δ−0.00001 rad） |
| 观察回路抓帧（head 源，只读） | 会话进行中 `perception_state {max_age_ms:1000}` 读到新鲜 head 帧 1280×480、age 508 ms、文件存在；会话按自身 duration 结束 |

本次同样只运动内部 Dex1，未运动手臂、肘与升降柱，未让模型规划动作。
证据：dist/real-0.1.12-verification.json、dist/real-0.1.12-loop-readiness.json。

未验证项（需操作员在抬臂姿态下放置物体时在场）：真实物体挡在指间时的 `contact_detected` 停位，
以及抬臂姿态下主相机对夹住物品的复核。首轮闭合用 `release` 复核失败即可自动放回闭合位，
不再需要单独的 open 收尾。

## 0.1.13 观察只取最新 1 帧（2026-09-11）

按用户要求收紧观测契约：`g1d.perception_state` 每源只返回**最新 1 帧**，存储不再保留历史环，
请求侧去掉 `history_s` 与 `max_history`（`PerceptionStateRequest` 现在只有 `max_age_ms`，
`additionalProperties=false`）。要看过程就按 `interval_s` 反复读并比较自己看过的帧；
闭合判定仍保留"连续两轮确认"，只是每轮判的是该轮最新的那一帧。

| 检查 | 结果 |
| --- | --- |
| 公布请求 schema | 仅 `max_age_ms`，`additionalProperties=false` |
| 传 `history_s` | 422 / `G1D_INVALID_ARGUMENTS`（`extra_forbidden`），未执行 |
| 会话中读取（head 源，只读） | 每次返回 1 帧、输出键仅 frames/session_active/store_age_ms/updated_monotonic_s/simulated/limitations，无 history 字段 |
| 两次读取 | sequence 868 → 918（最新帧确实在推进），age 409.9 / 359.1 ms |
| 会话结束后读取 | failed / `PERCEPTION_STATE_STALE` |

本次未发送任何运动命令，未运动手臂、肘与升降柱。证据：dist/real-0.1.13-perception-state.json。
0.1.11 记录中"读到 9 帧历史"是当时的契约行为，0.1.13 起该能力已移除。

## 0.1.14 抓取后复核改用腕部主视角（2026-09-11）

按用户要求：**放置判定的观察源不变**（主相机 `head`，与 0.1.11–0.1.13 相同），
**识别到抓取（闭合返回）后把复核视角切换为腕部为主、头部为辅**。SKILL.md 第 10 步改为
先 `g1d.camera_snapshot {source:"left_wrist"}` 看清指间夹的是什么，再用 `{source:"head"}`
作为辅助视角；腕部画面看不到手指时改判头部视角并说明判自哪一路。第 12 步报告措辞同步
（触发判自主相机、复核判自腕部+头部）。

本次只改 Skill 文档与版本号：`--skip-binary` 构建，节点与 gateway 归档未变
（g1d-node-0.1.0-745692b2cce32776 与 0.1.13 相同）。安装顺序：stop → skill install
0.1.14 → forge-node install gateway/g1d → start --profile real，real profile 全部工具 ready。
**未发送任何运动命令，未运动手臂、肘与升降柱。**
证据：dist/skills/g1d-robot-0.1.14.tar.gz（sha256 01177f662d449de7529e5dd477de3e73980ca0a576dc5bb584ea93674a2a29a7）。

同时改（宿主侧，不进节点）：图片判断阶段模型可单独配置，`.paos-instance/config.json` 的
`agents.modes.models.multimodal.model` 一度设为 `deepseek/deepseek-flash`，主 Agent 仍为
`deepseek/deepseek-v4-flash-vision-exp`。真机帧对比（每格同一批图、同一问法）：
主相机全帧「物在指间=是」3/20 → 9/20；放大双目特写 5/6 → 2/6；
放置前空夹爪帧 0/5 → 0/5。**这个对比结论已作废**（`deepseek-flash` 不接收图像，见文末
「更正：图片判断阶段不能用 deepseek-flash」）：那些「是」是对着纯文本瞎猜的，
不能作为换模型依据；该配置已改回 `deepseek/deepseek-v4-flash-vision-exp`。
数据文件 dist/loop-no-object-20260911/vlm_flash_compare.json 保留为记录，不再引用其结论。

## 0.1.15 闭合前切腕部主视角（2026-09-11）

用户更正 0.1.14 的语义：切换点不在抓取之后，而是**在闭合之前**——「因为主视角不一定分辨地出
要抓取的物体」。抬臂姿态下白色盒子在头部画面里又小又淡，只靠头部那一帧判定就等于让分辨不出
物体的视角决定闭合。0.1.15 起：

- 会话一次起两路：`{sources:["left_wrist","head"], duration_s:300, interval_s:0.5}`，
  头部帧是宽视角的**第一线索**（有人靠近夹爪 / 物品正在放入），腕部帧是**闭合判定视角**。
- SKILL.md 第 7 步明确「头部帧只给第一线索，绝不单凭头部画面的『有』就闭合」。
- 第 9 步：一看到候选物品或人手靠近就把主视角切到腕部（头部转辅助），闭合与否由**腕部连续两轮**
  判定（两帧 sequence 不同、间隔 ≥ 1 s），头部帧作辅助交叉验证；腕部看不到手指时明确退回头部
  视角并说明判自哪一路。
- 第 10 步（0.1.14 引入的抓取后复核）沿用腕部为主、头部为辅——切换发生在闭合前，闭合后自然
  仍然是腕部主视角；第 12 步报告措辞改为「头部只给第一线索、判定与复核都来自腕部视角」。

本次同样只改 Skill 文档与版本号：`--skip-binary` 构建，节点与 gateway 归档未变
（g1d-node-0.1.0-745692b2cce32776 与 0.1.13/0.1.14 相同）。安装顺序：stop → skill install
0.1.15 → forge-node install gateway/g1d → start --profile real，real profile 全部 8 个工具 ready；
安装后的 SKILL.md 与源文件逐字节一致。`./.tools/uv run pytest -q` 84 passed，ruff 全通过。
**未发送任何运动命令，未运动手臂、肘与升降柱。**
证据：dist/skills/g1d-robot-0.1.15.tar.gz（sha256 e865bc41f3606fb0b54bd64659b21085786957493a6e1eb7891c124fc335453e）。

仍待操作员在场验证：`contact_detected` 的实物复测，以及这个「腕部在抬臂姿态下究竟能不能看到
指间」的前提。既有真机证据偏向"看不到"：0.1.10 那一轮就是用 `left_wrist` 单源观察的，
workspace/g1d_snapshots 里 09-11 12:22–12:24 的 198 张 `left_wrist` 帧（该窗口没有任何 head 帧，
说明会话只起了腕部源）逐帧看下来都是机器人自身前臂/机身面板与木门，手指与物品不在画面里；
15:14 那一轮抬臂会话中仅有的一张腕部帧（工作台单张快照而非会话帧）同样是这个画面。
0.1.11 把观察源换成 head 正是因为这一点。所以 0.1.15 的腕部主视角能否成立，取决于当前抬臂
姿态（肘 `mode:"upper_arm"`, 90°）下腕部相机的取景是否与上述几次不同——需要操作员在场、
手臂抬起后取一张腕部快照确认；若仍看不到手指，第 9 步的显式退路（改判头部并说明判自哪一路）
就是实际运行路径，那时"头部画面分辨不出物体"的问题仍未解决（头部单帧要靠放大裁剪才看得清白盒，
而 Agent 侧没有裁剪工具），需要另行决定对策。

## 更正：图片判断阶段不能用 deepseek-flash（2026-09-11 17:00）

上一条把 `agents.modes.models.multimodal.model` 设为 `deepseek/deepseek-flash` 是**错的**，
它就是当晚 paos Agent 报「视觉自动触发这条路暂时走不通」的直接原因。

现象：17:56 那轮 Agent 按 0.1.15 起会话、拿到 left_wrist 与 head 两张有效 JPEG（640×480、
1280×480，`file`/PIL 都验证通过），随后连续 4 次调用 image 工具，模型每次都回「我目前没有收到
图像画面」「看不到图片」这类话。按 0.1.15 第 6 步硬性规则（无法确认夹爪可见就绝不盲闭），
Agent 取消会话、终结任务、没有闭合。

根因（离线复现，同一张 16:56 的 left_wrist 实拍帧）：

| 调用路径 | 结果 |
| --- | --- |
| ImageTool → ProvidersManager(multimodal=deepseek-flash)（生产路径） | 「可见夹爪：无法判断 / 指间有物：无法判断」 |
| ImageTool → 主模型 `deepseek-v4-flash-vision-exp` | 「可见夹爪：否 / 指间有物：否」（真实判断） |
| litellm 直连 `deepseek/deepseek-flash` | 空字符串 |
| litellm 直连 `deepseek/deepseek-v4-flash-vision-exp` | 真实判断 |

小图直测（320×160 合成图，白字 `VISION-OK-7391`，max_tokens=1500，各 3 次）：
`deepseek-flash` 6/6 回「没有图片」，`deepseek-v4-pro` 也回「没有图片」，
`deepseek-v4-flash-vision-exp` 1/1 正确读出 `VISION-OK-7391`。
该端点 `GET /models` 当前只列 `deepseek-flash` 与 `deepseek-v4-pro`，两者都不接收图像；
`deepseek-v4-flash-vision-exp` 未列出但确实可用。本机只有 deepseek 一家凭据，
所以可用的视觉模型只有它一个。

因此 `dist/loop-no-object-20260911/vlm_flash_compare.json` 里「flash 优于 vision_exp」的结论
**作废**：flash 的那些「物体在指间：是」是对着纯文本提示瞎猜的，空答案也是同一个原因，
不能作为模型对比数据（数据保留，仅作记录）。

处置：`.paos-instance/config.json` 的 `multimodal.model` 改回 `deepseek/deepseek-v4-flash-vision-exp`
（等于主模型，`_vision_provider` 直接复用 Agent 自己的 Provider），describe 里写明"必须是真正
接收 image_url 的模型"。改的是宿主侧配置，不需要重装 Skill；重新进入 Agent 即生效。
改后同一张帧在生产路径上恢复真实判断（见上表）。

顺带核对姿态：这张 16:56 的 left_wrist 帧**清楚拍到了白色盒子**（近距离、占了大半画面），
所以"切腕部主视角"的方向是对的；但同一帧里看不到夹爪手指，而同一时刻的 head 帧能看到
（模型也判「可见夹爪：是」）。即当前姿态下两路各看一半：腕部看得清物体、头部看得见手指。
0.1.15 第 9 步的退路（腕部看不到手指就改判头部）在这张帧上会被触发。

### 更正（2026-09-14）：`deepseek-flash` 现在可以读图

上面 2026-09-11 的结论是"该端点两个列出的模型都不接收图像"。**2026-09-14 复测发现
`deepseek-flash` 的行为已经改变**，用完全相同的测法（320×160、白字 `VISION-OK-7391`、
`max_tokens=2000`、各 3 次）：

| 模型 | 命中 | `prompt_tokens` |
| --- | --- | --- |
| `deepseek-flash` | **3/3** | 247（图片进了模型） |
| `deepseek-v4-flash-vision-exp` | 3/3 | 247 |
| `deepseek-v4-pro` | 0/3 | 103（图片被丢弃） |

即 `deepseek-v4-pro` 不能读图的结论仍然成立，但 `deepseek-flash` 已经可以读图。
上面作废 `vlm_flash_compare.json` 的理由（"flash 是对着纯文本瞎猜"）**在本日期之后不再适用**。
判断模型是否真收到图片，看 `prompt_tokens`（247 vs 103）而不是回答措辞。

2026-09-11 的原始记录保留不改，它准确描述了当时的观测。

## 0.1.16 判据拆分：物体认腕部、手指认头部（2026-09-11）

按用户指示「判据改成物体认腕部、手指认头部」。0.1.15 的"闭合前把主视角整体切到腕部"
不成立：当晚 16:56 的实拍帧显示，当前抬臂姿态下**腕部帧拍得到白色盒子（近距离、占大半画面）
但拍不到手指**，而同一时刻**头部帧拍得到手指**（模型判「可见夹爪：是」）。两路各看一半，
所以判据按视角拆开，而不是按先后切换：

| 判什么 | 用哪一路 | 依据 |
| --- | --- | --- |
| 是不是要抓的那个物体、有没有到夹爪口 | `left_wrist` | 头部画面里物体太小太淡，分辨不出；腕部近距离拍得到 |
| 手指是否张开可见、指间是否空着、附近有无人手 | `head` | 抬臂姿态下腕部帧拍不到手指（0.1.10 那轮就是因此换的观察源） |

闭合条件：两半**同一轮都成立、且连续两轮**（两帧 sequence 不同、间隔 ≥ 1 s），
确认读紧接 close Action。判据缺一半就不许闭——某一路不再能判它那一半（头部看不到手指 /
腕部看不到夹爪口）时该轮不成立，持续如此就取消会话、说明是哪一路失效、退回人触发，
不允许拿另一路的结论顶上。第 10 步的抓取后复核沿用同一分工。

本次仍只改 Skill 文档与版本号：`--skip-binary` 构建，节点与 gateway 归档未变
（g1d-node-0.1.0-745692b2cce32776 与 0.1.13–0.1.15 相同）。安装顺序：stop → skill install
0.1.16 → forge-node install gateway/g1d → start --profile real。`./.tools/uv run pytest -q`
84 passed，ruff 全通过。**未发送任何运动命令；手臂仍停在 16:56 的抬起姿态、左爪张开 5.238。**
证据：dist/skills/g1d-robot-0.1.16.tar.gz（sha256 cac3239a65470113855161be063409893ce6d9d35a72ba4f6fbafaff4eea1cb3）。

同一次会话的下一条更正（图片判断阶段模型改回 vision-exp）见上文——那是本轮视觉失败的根因，
判据拆分只有在视觉模型真能看到图之后才有意义。

## 0.1.17 一次读取即确认（2026-09-11）

按用户指示「把规则修改为一次即可确认抓夹操作」。0.1.11 起沿用的"连续两轮确认"（0.1.16 表述为
两帧 sequence 不同、间隔 ≥ 1 s）取消：**同一次读取里两路帧同时成立就下发闭合**，不再等第二轮。
理由由操作员给出——每轮是完整模型轮次 + 两次看图，秒级开销，而人放置物品往往就在某一瞬间完成，
多等一轮只是拖长等待。这是一次明确的取舍：单次误判现在会直接闭合。

保留的守卫一条不减（SKILL.md 第 9 步已写明）：

- 两路缺一不可：腕部帧判物体（是不是目标、有没有到夹爪口），头部帧判手指与人手（张开可见、
  指间状态、附近无人手）；单一视角永远不构成确认。
- 那一次读取必须**紧接** close Action，帧的 `age_ms` 在 `max_age_ms:1000` 界内；不得用没刚读过
  的旧帧或旧图片路径当依据。
- 判据不成立就继续看，不做"第二次确认"；某一路不再能判它那一半（头部看不到手指 / 腕部看不到
  夹爪口）时该轮不成立，持续如此就取消会话、说明是哪一路失效、退回人触发，不降低判据。
- 绝不盲闭：起会话后仍先确认张开的手指在 head 帧里可见。
- 闭合后复核分工不变（腕部判夹住的是什么、头部判手指是否环绕着它），第 11 步先 cancel 会话。

本次仍只改 Skill 文档与版本号：`--skip-binary` 构建，节点与 gateway 归档未变
（g1d-node-0.1.0-745692b2cce32776，与 0.1.13–0.1.16 相同）。安装顺序：stop → skill install
0.1.17 → forge-node install gateway/g1d → start --profile real；安装后已核对 SKILL.md 与源文件
sha256 一致（fc5e5e9c…）、skill.yaml version 0.1.17、八个 tool context 全 ready，图片判断阶段
模型仍是 `deepseek/deepseek-v4-flash-vision-exp`（未被本次安装覆盖）。`./.tools/uv run pytest -q`
84 passed，ruff 全通过。**未发送任何运动命令。**

证据：dist/skills/g1d-robot-0.1.17.tar.gz（sha256 5809933a748db75faeeb4892d5090866d4edcaffdb8b82700cf2566af0ae36a3）。
下一步仍是待操作员的那一项：抬臂姿态下确认腕部取景，并用真实物体复测 `contact_detected`，
跑通 0.1.17 判据的首次闭环（任务 #25）。

## 更正：头部双目清晰度（2026-09-14）

上面 2026-09-08 的首次真机记录写"头部右眼更模糊的现象仍存在"。**该设备问题已修复**，
此处补上可复现的判据与时间点。

用去噪后的边缘能量比衡量两半清晰度（左半 / 右半，比值 >1 表示右半更模糊）：

| 帧日期 | 帧数 | 左/右 中位数 | 范围 |
| --- | --- | --- | --- |
| 2026-09-08 | 26 | **2.06** | 1.32–2.45 |
| 2026-09-10 | 29 | **0.91** | 0.89–0.94 |
| 2026-09-11 | 2408 | 0.92 | 0.87–0.95 |

**修复发生在 09-08 与 09-10 之间。** 09-08 的帧肉眼即可确认：左半的门把手、门牌、
人脸与纸箱文字清晰，右半同样区域整体糊掉、文字不可辨；09-10 起的帧两半清晰度相当。

由此产生的两条注意事项：

1. **09-08 及更早的帧带该缺陷**，不要用它们做清晰度或识别能力的基准；
   本仓库的视觉评测（`scripts/diagnostics/evaluate_vision.py`）只采用 09-11 的帧。
2. 引用本文档上文"右眼模糊"的描述时，须理解为**09-08 当时的状态**，不是当前设备状态。

复现命令（读本地帧，不连接设备）：

```bash
.venv/bin/python - <<'PY'
from PIL import Image, ImageFilter
import statistics, glob, datetime
def sharp(p):
    g = Image.open(p).convert("L")
    W, H = g.size
    f = lambda im: statistics.pvariance(list(
        im.filter(ImageFilter.GaussianBlur(0.8)).filter(ImageFilter.FIND_EDGES).getdata()))
    return f(g.crop((0, 0, W // 2, H))), f(g.crop((W // 2, 0, W, H)))
for p in sorted(glob.glob("workspace/g1d_snapshots/head-*.jpg"))[:1]:
    l, r = sharp(p)
    print(datetime.datetime.fromtimestamp(__import__("os").path.getmtime(p)), l / r)
PY
```
