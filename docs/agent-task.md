# DeepSeek AgentTask 升降验收

## 原样 prompt 入口

新增 --prompt 模式，下面传给 AgentLoop.process_direct 的用户内容严格为 `上升 5 cm`，
不再拼接目标坐标、工具调用步骤或预先写好的验收条款：

```bash
./scripts/core-env.sh paos skill start g1d-lift --profile mock
./scripts/core-env.sh python scripts/diagnostics/check_agent_task.py --profile mock --prompt '上升 5 cm'
./scripts/core-env.sh paos skill stop g1d-lift
```

在现场条件与本次动作已确认时，将两个 mock 改为 real 即执行真机动作。
每次新运行均表示从当时位置再移动指定距离，不可用于自动重试结果不明的旧动作。

工作流规范来自安装后的 skills/g1d-lift/SKILL.md：模型读取实时状态，换算单位、
计算绝对目标、创建 enforce 验收条件并自行选择工具。脚本不预先调用任务创建或运动工具。
入口不解析 prompt 的句式、关键词、单位或距离，也不预先计算模型应选的目标。
“下降至最低点”“下降 15 cm”以及询问、解释类输入都会原样交给 Agent。
原来的 relative_intent.py 和固定 10.5 cm 位移限制已删除。
输入自由不等于新增了设备能力：本入口仍只暴露现有的 G1-D 升降与观察工具。

检查层限制一次 Action、目标必须在安装后 ToolSpec 的设备限位内、2 mm 到位容差、
最长动作时间、任务绑定、enforce 模式和完整的三路图像/状态证据。原样模式的验收条款
由模型生成；脚本核对实际高度是否达到模型提交的目标及停止结果。
prompt 含义与动作目标是否一致由 Agent 和语义验收判断，不再声称脚本独立理解了用户意图。
若模型遗漏必需参数，工具会返回拒绝，模型可以在尚未提交动作时修正。
report.json 的 prompt_mode=verbatim_user_input，prompt 字段保留原文；action_target_m
记录模型提交的目标。Agent 若只解释或澄清，返回 no_task_created、passed=null，不提交动作。

### 原样“上升 5 cm”的历史调试结果（移除句式检查之前）

- mock：task_a7be439cbff0473b，succeeded / verdict=success；模型自行读取 0.500 m，
  计算 0.550 m。首次任务创建遗漏 evidence_policy，被检查层拒绝后修正；仅一次 Action。
  记录：dist/mock-agent-task-20260908T075438.344468Z/。
- real：输入严格为 `上升 5 cm`，未向模型提供预先算好的目标或模板验收条款。
  模型读取约 0.0027047 m，自行计算并发送目标 0.0527047 m，选择 timeout_s=10.0、
  Gateway timeout_ms=20000，实际动作约 6.78 秒。仅一次 Action。
- Action 终态柱高 0.05092084780335426 m；验收结束后独立 Query 为 0.05076616257429123 m，
  相对本次起始值 0.0027047337498515844 m 上升 48.0614 mm，误差约 1.9386 mm，
  在 2 mm 容差内。停止指令接受且新鲜反馈稳定，simulated=false。
- AgentTask：task_7a8431239b0a41f3，PlanRevision：revision_fe2fbf40d6dc45da；
  enforce verdict=success，模型生成的 8 项验收条件全部 satisfied。
- 真机记录：dist/real-agent-task-20260908T075652.081314Z/transcript.md，以及同目录
  report.json、task.json、events.json。完成后运动 Runtime 已正常关闭，无第二次补偿或回落动作。

这验证了项目短指令入口加 Skill 规范和独立检查层的组合，不表示已经对任意自然语言
输入提供同等保证。独立检查未替代模型规划，也未替代 Core 的证据校验与语义验收。

入口 scripts/diagnostics/check_agent_task.py 复用 Core AgentLoop、SkillActivationManager、
AgentTaskCoordinator、ForgeToolClient 和 ForgeTaskVerifier。Core 源码未修改。

执行链：DeepSeek 生成计划并选择原生工具 → activate_skill → forge_task_create
（创建初始 PlanRevision、冻结 Skill/Runtime/ToolSpec 绑定）→ task-bound Query/Action
→ 官方 Gateway → Dora → G1-D 节点 → Unitree SDK2 → 升降柱。

动作完成后，Agent 读取 Action result 和最终状态，再调用 forge_task_finalize。
Core 自动收集动作前后各三张 RGB 图像和 robot_state，生成带摘要与关联信息的 Evidence Bundle；
独立的本地验收服务调用 DeepSeek，按任务成功条件生成结构化 verdict。
本流程要求 enforce 模式，因此动作 succeeded 不代表 AgentTask 已通过。

## 执行边界

- 模型自行选择工具和调用顺序，脚本不代替模型提交 Action。
- 脚本固定已授权目标、2 mm 到位误差和最长动作时间，并限制一次 Action 提交尝试。
- Query/Action 必须绑定本次唯一的 AgentTask；禁止追加运动、自动补偿或重新发送未知结果的动作。
- 模型只能使用 Skill 读取、激活、任务和 Forge 工具，没有 shell、文件写入或直接 SDK 接口。
- 发动作前再次读取真实反馈，若规划期间立柱变化超过 1 mm，就拒绝发送旧目标。
- Core 的停止观察依据为新鲜柱高反馈稳定，不等于独立制动器状态确认。
- 证据关联为 best_effort，图像只作场景上下文，高度误差以柱高反馈计算。
- 脚本外的操作员负责通过 Core CLI 启停 Runtime；结束后应关闭运动 Runtime。

## 命令

从 PhyAgentOS-g1d 执行。负 delta 表示下降，每次新运行都会从当时读数重新计算目标。
不要通过重复运行脚本来重试结果不明的物理动作。

```bash
./scripts/core-env.sh paos skill start g1d-lift --profile mock
./scripts/core-env.sh python scripts/diagnostics/check_agent_task.py --profile mock --delta-height-m=-0.05
./scripts/core-env.sh paos skill stop g1d-lift
```

现场已经明确授权本次下降 5 cm 后，使用 real profile：

```bash
./scripts/core-env.sh paos skill start g1d-lift --profile real
./scripts/core-env.sh python scripts/diagnostics/check_agent_task.py --profile real --delta-height-m=-0.05
./scripts/core-env.sh paos skill stop g1d-lift
```

也支持 --target-height-m 指定绝对柱高；它和 --delta-height-m 互斥。
真机必须显式给出 --prompt、--target-height-m 或 --delta-height-m 之一。
数值参数模式仍检查目标是否位于安装后的设备限位内，没有固定的 10.5 cm 位移上限。

## 本地验收提示补充

首次 mock 的动作成功、8 项前后证据完整，但验收模型生成了 `tool:tool_...` 引用，
该值不在 Core 构建的 valid_evidence_refs 内。Core 正确拒绝该 verdict，AgentTask 记为 failed。
原始记录：dist/mock-agent-task-20260908T072917.077277Z/。没有篡改或清除失败记录。

src/paos_g1d/verification.py 继承 Core 的 VerificationRequestBuilder：先完整调用原生证据
校验和请求构建，再附加允许引用的 ID 清单及原样复制要求。没有修改图像、执行事实、
验收成功条件或 Core 的引用校验，也不将模型错误的引用自动映射成合法值。
tests_core/test_agent_verification.py 验证原始请求内容不变，非法引用仍由 Core 拒绝。

## 记录位置

- dist/{mock|real}-agent-task-{UTC时间}/report.json：目标、工具调用、模型答复与独立检查结果。
- 同目录 task.json、events.json：Core 原生 AgentTask、PlanRevision、执行绑定、验收及事件副本。
- 同目录 transcript.md：可读的模型执行消息、完整工具调用和返回（后续运行生成）。
- workspace/artifacts/agent_tasks/{task_id}/：Core 原生前后证据与 Evidence Bundle。
- Core 原生任务数据库保留任务历史；本次启用 evidence_retention=all，不清理成功证据。

## 2026-09-08 实测结果

修正证据引用提示后，mock 的下降 5 cm AgentTask 已通过 enforce 验收：
task_6b718e9f870c4337，verdict=success；报告为
dist/mock-agent-task-20260908T073408.681254Z/。原始失败任务仍保留。

用户将真机目标明确为“下降 5 cm”后，完成同一条 Agent 全链路：

| 项目 | 真机结果 |
| --- | --- |
| 模型 | deepseek/deepseek-v4-flash-vision-exp |
| 起始柱高 | 0.0988779291510582 m |
| 目标柱高 | 0.048877929151058194 m |
| Action 结束柱高 | 0.05064162239432335 m |
| 验收结束后 Query 柱高 | 0.05078085511922836 m |
| 验收后实测下降量 | 48.097074031829834 mm |
| 目标误差 | 1.902925968170166 mm，在 2 mm 容差内 |
| 动作耗时 | 6.459897767010261 s |
| Action | 仅 1 次，succeeded，reached_goal=true，simulated=false |
| 停止反馈 | stop_command_accepted=true、stopped_observed=true |
| AgentTask | task_9659384a96fc4357，status=succeeded |
| PlanRevision | revision_b5ef3e6d68304c0a，仅初始 revision |
| Gateway invocation | gateway-45996ac349a14a4a994320f8885300d6 |
| 独立验收 | enforce；一次验证，verdict=success，3 项 criteria 全部 satisfied |
| 证据 | 前后各 3 张 RGB 图像和 1 份 robot_state，共 8 项，complete=true，无缺失/过期 |

19 次模型选择的工具调用保存在完整记录中；没有第二次 Action、自动补偿或回升。
Action 结束、Agent 的后置查询与验收后查询的柱高略有变化，上表分别列出，
不将瞬时到位值当作后续所有时刻的读数。各次检查均在本次容差内。
本次停止依据仍是柱高稳定观察；完成后 g1d-lift Runtime 已正常关闭。

完整记录：dist/real-agent-task-20260908T073653.145250Z/transcript.md。
结构化报告、原生任务与事件：同目录 report.json、task.json、events.json。
原生证据：workspace/artifacts/agent_tasks/task_9659384a96fc4357/evidence_bundle.json。

这是在明确目标、限定动作数量和行程范围下的真实 AgentTask 验收，
不代表已实现任意机械臂/底盘任务或无人值守规划。Core 源码保持未修改。
