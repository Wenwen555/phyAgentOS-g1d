# 模型选择与验证器可靠性

记录可切换的模型、各自的实测能力，以及验证器在这套部署上的已知失效模式。
本文所有延迟与 token 数都是本机实测（2026-09-13，`httpx` 直连，绕过环境代理），
不是规格推断。

## 1. 两个端点

| 端点 | provider 名 | 当前用途 |
| --- | --- | --- |
| `https://api.deepseek.com` | `deepseek` | Agent 主模型 + 图片理解 + 验证器 |
| `https://api.modelarts-maas.com/openai/v1` | `custom` | **已关闭**（2026-09-14，见 §5）；且不能用于任何看图路径 |

## 2. `api.deepseek.com` 可用模型

`GET /models` 只列出 2 个，但实际可调用 3 个：

| 模型 | `/models` 列出 | 文本（验证器形态） | 裸 JSON | **能读图** |
| --- | --- | --- | --- | --- |
| `deepseek-flash` | ✅ | 1.78 s | ✅ | ✅ 读出图内文字 |
| `deepseek-v4-flash-vision-exp` | ❌（未列出但可用） | 1.86 s | ✅ | ✅ 读出图内文字 |
| `deepseek-v4-pro` | ✅ | 3.24 s | ✅ | ❌ **图片被丢弃** |

「能读图」的判据是 `prompt_tokens`：带同一张图的请求，能读图的模型是 247，
`deepseek-v4-pro` 只有 103 —— 图片根本没进模型，它回答"I can't read the text in the image"。

⚠️ 模型名带 provider 前缀会被这个端点**直接拒绝**：

```
The supported API model names are deepseek-flash, deepseek-v4-pro,
but you passed deepseek/deepseek-v4-flash-vision-exp
```

配置里写 `deepseek/deepseek-v4-flash-vision-exp` 是可以的，因为 Core 走 LiteLLM，
由 LiteLLM 剥掉前缀；但**直连调试时必须写不带前缀的名字**。

### 更正 `docs/agent-vision.md` 的一条旧结论

该文档（2026-09-11）记录 `deepseek-flash` 对图片一律回「没有图片」，只有
`deepseek-v4-flash-vision-exp` 能读。本次复测（320×160、白字 `VISION-OK-7391`、`max_tokens=2000`）：

| 模型 | 命中 | `prompt_tokens` |
| --- | --- | --- |
| `deepseek-v4-flash-vision-exp` | 3/3 | 247 |
| `deepseek-flash` | **3/3** | 247 |
| `deepseek-v4-pro` | 0/3 | 103 |

`deepseek-flash` 现在**可以**读图，旧结论已过期；`deepseek-v4-pro` 不能读图的结论仍然成立。

⚠️ 两点必须说清楚，否则这个更正本身会误导人：

1. **2026-09-11 的观测是有效的**，那是在 `max_tokens=1500` 下得到的**内容**回答
   （"没有图片"，不是空串），与时下的 token 预算无关。这里记录的是**端点行为在
   09-11 与 09-14 之间发生了变化**，不是"当时测错了"。原始记录保留在
   `docs/real-robot-debug.md`，不改写。
2. 同一张图在 `max_tokens=60` 下会得到空内容——那是 reasoning token 吃光额度
   （`finish_reason=length`），**不是**模型不支持图片。这两种失败必须区分，否则会把
   预算问题误判成能力问题。判据是 `prompt_tokens`：247 表示图片进了模型，103 表示被丢弃。

换模型仍必须用一张已知内容的图复验，不要依赖任何文档（包括本文）。

## 3. `api.modelarts-maas.com` 可用模型（端点已关闭，以下为关闭前的探测记录）

`GET /models` 列出 9 个，但当前 key 只有 2 个有权限，其余 7 个返回
`ModelArts.81004 Invalid request because you do not have access to it`：

| 模型 | 状态 | 文本延迟 |
| --- | --- | --- |
| `deepseek-v4-pro` | ✅ 有权限 | 10.86 s |
| `glm-5.2` | ✅ 有权限 | 11.03 s |
| `glm-5.1` | ❌ 无权限 | — |
| `deepseek-v4-flash` | ❌ 无权限 | — |
| `qwen3-30b-a3b` | ❌ 无权限 | — |
| `qwen3-32b` | ❌ 无权限 | — |
| `openpangu-2.0-pro` | ❌ 无权限 | — |
| `openpangu-2.0-flash` | ❌ 无权限 | — |
| `kimi-k2.6` | ❌ 无权限 | — |

**图片在这个端点上被平台层封禁**：任何带图请求返回
`ModelArts.81011 Input image May contain sensitive information`，
`deepseek-v4-pro` 与 `glm-5.2` 均如此。合成测试图也一样，不是图片内容问题，是策略。

结论：ModelArts 只能做**纯文本**任务，且比 deepseek 端点慢约 6 倍。

## 4. 验证器的失效模式（重要）

> **这条不是本次新发现。** `workspace/memory/HISTORY.md` 的 2026-09-10 条目已经诊断出同一根因：
> "`agents.verification.model` is unset so verifier falls back to default agent model
> `deepseek/deepseek-v4-flash-vision-exp`, a REASONING model that consumes the entire max_tokens
> budget in hidden reasoning … leaving `content=''` → JSON parse error"，并且当时就得出
> **"Raising max_tokens does NOT fix it"**。本节复现并量化了它，并补上可复现的失败清单。

Core 给验证器子进程的 `max_tokens` 是硬上限（`cli/commands.py`）：

```python
"max_tokens": min(4096, config.agents.defaults.max_tokens),
```

而这些模型**把 reasoning token 计入该额度**。实测：`max_tokens=60` 时
`finish_reason=length`、`reasoning_tokens=60`、`content=''`；放宽到 2000 才拿到正文。
`agents.defaults.reasoningEffort` 已设为 `"none"`，但该端点未生效。
**reasoning 用量随预算同比例增长**（HISTORY 记录 `max_tokens=8000` 时
`reasoning_tokens=8000`），所以单纯调大上限不能解决，只是把截断点往后推。

后果在真机记录中已经发生 —— `dist/real-agent-task-*/task.json` 里 9 个任务有 **3 个
Action 实际成功却被标记 failed**，全部是验证器自身出错：

| 任务 | Action 状态 | 失败原因 |
| --- | --- | --- |
| 075955 | `set_height` **succeeded** | `Unterminated string starting at: line 40 column 9`（JSON 被截断） |
| 082243 | `set_height` **succeeded** | `verifier model returned no content` |
| 090038 | `set_height` **succeeded** | `verifier model returned no content` |

另外 6 个有 verdict 的任务里，verdict 与执行事实**6/6 完全一致**：
`failure` 全部引用 Action 自己的 `execution_timeout` / `reached_goal=false`。
也就是说，在这个升降工作流里验证器没有提供独立信息，却贡献了全部误判。

### 去掉图片后的实测余量（2026-09-14）

用一条**不含图片**的验证器形态请求（3401 字符 ≈ 850 token 输入、8 条判据、6 条执行记录），
在真实的 4096 上限下各跑 2 次：

| 模型 | 耗时 | `completion_tokens` | `reasoning_tokens` | 结果 |
| --- | --- | --- | --- | --- |
| `deepseek-flash` | 5.2 / 7.7 s | 1633 / 2069 | 203 / 682 | `finish=stop`，裸 JSON 可解析 |
| `deepseek-v4-flash-vision-exp` | 10.0 / 7.8 s | 2744 / 2114 | 1121 / 924 | `finish=stop`，裸 JSON 可解析 |

结论：**去掉图片后请求远未触及上限**，截断风险主要来自那 6 张 base64 图撑大的请求；
同时 `deepseek-flash` 的 reasoning 用量约为另一半、响应更快，是更合适的验证器模型。

因此升降工作流改用 `mode="off"`（见 `skills/g1d-lift/SKILL.md`），
证据策略也从 `["rgb_image","robot_state"]` 收窄为 `["robot_state"]`
（一张 JPEG 无法把柱高验证到 0.002 m）。

## 5. 当前配置（2026-09-14：全部切到 `deepseek-flash`，ModelArts 已关闭）

```json
"agents": {
  "defaults":     { "model": "deepseek-flash", "provider": "deepseek" },
  "modes":        { "models": { "multimodal": { "model": "deepseek-flash" } } },
  "verification": { "model": "deepseek-flash", "provider": "deepseek" },
  "evolution":    { "model": "deepseek-flash" }
},
"providers": { "custom": { "apiKey": "", "apiBase": null } }
```

| 角色 | 模型 | 依据 |
| --- | --- | --- |
| Agent 主推理 | `deepseek-flash` | 工具调用与结果回灌实测通过（0.93 s） |
| 图片理解（ImageTool vision） | `deepseek-flash` | 图内文字 3/3 命中（见 §2） |
| 语义验证器 | `deepseek-flash` | 裸 JSON 合规；与主模型解耦，可独立替换 |
| 演化/经验 | `deepseek-flash` | 纯文本任务 |

### ModelArts 端点的关闭

`providers.custom` 的 `apiBase` 置 `null`、`apiKey` 置 `""`，**主配置与
`config.modelarts-candidate.json` 都已关闭**，项目内不再有任何配置指向
`api.modelarts-maas.com`。`CustomProvider` 在 `api_base` 为空时会退回
`http://localhost:8000/v1`，因此关闭后的表现是连接失败，而不是把请求发到 ModelArts。

候选文件保留了（作为模型 ID 的记录），但它的 `custom` 凭据已清空。
⚠️ **建议在云控制台轮换这把 ModelArts key**：它曾以明文存在于
`workspace/sessions/cli_direct.jsonl`（被 `exec` 打印配置时带出，日志中为前 60 字符的截断形式）。
该日志已清理，同一处的 `deepseek` key 也已清理；但既然离开过文件边界，轮换才是彻底的处置。

## 6. 验证器出错的解决方法

验证器失败会走 Core 的 `_verification_error`，而它的状态判定是：

```python
status = SUCCEEDED if task.verification.mode == "audit" and _execution_facts_succeeded(task) else FAILED
```

也就是说**模式决定验证器故障能否推翻执行事实**。据此分三层处理，全部可在本项目内完成：

| 模式 | 验证器故障时的结果 | 何时用 |
| --- | --- | --- |
| `off` | 不调用验证器，任务由执行事实判定 | 所有判据都是 Action 自己计算并强制的字段 |
| `audit` | 有 verdict 就记录；验证器出错则**回退到执行事实** | 想留下语义判断，但不允许它把成功任务判失败 |
| `enforce` | **任务失败** | 语义判断必须能否决任务时才用 |

验证行为已固化为离线测试 `tests_core/test_verification_failure.py`（3 项）：

- `enforce` + 验证器出错 + Action 成功 → `FAILED`（记录 Core 的现有行为）
- `audit` + 验证器出错 + Action 成功 → `SUCCEEDED`（本项目的缓解手段）
- `audit` + 验证器出错 + Action 失败 → `FAILED`（audit 不是无条件放行）

配套的请求侧收敛（降低截断概率）：去掉图片证据、把判据压到 3–4 条且措辞简短。

**要根治仍需改 Core**，二选一：
1. `VerificationEngine.complete` 对空内容 / `finish_reason="length"` / JSON 解析失败**重试一次**；
2. `finish_reason="length"` 不当作验证失败，而是降级到执行事实。

⚠️ **"调大 `max_tokens` 上限"不是选项**：HISTORY 2026-09-10 已记录 reasoning 用量随预算
同比例增长（`max_tokens=8000` → `reasoning_tokens=8000` → `content=''`），调大只是把截断点
往后推，不会让内容出现。

在这三项落地前，**`off`/`audit` 是唯一能在本项目边界内消除该失效模式的手段**。

### 输入侧：Core 把同一批记录发了三遍（2026-09-14 实测；本项目侧已处置）

上面几个方案都在治"模型吐不出来"，但输入侧的大头**不是图片**。对真实任务
`task_f5422f85d30f4b07`（9 条执行记录）实测：

| 组成 | 字符 | 占 context |
| --- | --- | --- |
| `plan_revisions`（内嵌整批记录） | 16,631 | 32% |
| `tool_execution_records`（完整 dump） | 13,667 | 27% |
| `gateway_terminal_results`（同一批 + response） | 9,313 | 18% |
| **三处合计** | **39,611** | **77%** |
| `evidence_bundle` + `structured_evidence` + 其余键 | 11,757 | 23% |
| **context 合计** | **51,368** | |

其中**纯重复 25,944 字符，占 context 的 50.5%**。整请求 `prompt_tokens` 为 25,084
（含 6 张 evidence 图，图只占 1,917，即 7.6%）。

三者在 `request_builder.py:141-157` 由 `task.revisions` 与 `task.execution_records`
两行构造出来，而 `AgentTaskRecord.execution_records` 本身就是 revisions 的扁平化视图
（`forge/task.py:180-181`），**所以必然是同一批数据的三份拷贝**。完整分析见
[Core issue：验证请求重复三次](issues/core-verifier-record-duplication.md)（**尚未提交上游**）。

**本项目侧的处置**：覆写 `_build_request` 做投影，并在 `install()` 里接到生产路径
（`ForgeTaskVerifier` 在 `session_verifier.py:61` 从模块全局取 builder，替换该名字即可）。
在相同格式、前言与图片下实测：

| 指标 | 修剪前 | 修剪后 |
| --- | --- | --- |
| `context` 字符 | 51,368 | 25,993（−49%） |
| `prompt_tokens` | 25,084 | **13,426（−46%）** |
| 冷启动 TTFT（中位数，n=6） | 无随规模变化的可测差异 | 同左 |
| decode 速率（中位数） | 269 tok/s | 271 tok/s |

⚠️ **不要高估这项修复的收益**：同一端点上 TTFT 由约 1.0 s 的固定开销主导，
5.5k 与 23k prompt token 的 TTFT 没有可区分差别；两臂 decode 速率也相同。
所以这个缺陷的可证实代价是 **token 与上下文占用**，不是延迟——详见 issue。

守住的硬不变量：`valid_evidence_refs` 是每条记录 `evidence_refs` 与
`evidence_bundle` 的 `artifact_id` 的并集，投影必须保留全部 `evidence_refs` 与记录标识，
且不动 `criteria`。该不变量由 `tests_core/test_verification_projection.py` 断言。

⚠️ 这是**绕过 Core 而非修复 Core**。待该 issue 提交并被上游处理后可删除覆写；
在合并之前，覆写是必要的。

## 7. 换模型时必须做的三件事

1. **测文本**：发一个验证器形态的提示（要求裸 JSON），确认能解析且延迟可接受。
2. **测视觉**：用一张已知内容的图，让它读出文字。**必须设 `max_tokens ≥ 1000`**，
   否则 reasoning 会吃光额度、返回空串，看起来像"模型不支持图片"。
3. **确认裸 JSON**：`VerificationEngine` 直接 `json.loads(response.content)`，
   带 markdown 围栏或前后缀会变成 `invalid_response`。

`scripts/diagnostics/check_model.py` 覆盖了文本与工具，但**不覆盖**上面第 2 条的
`max_tokens` 陷阱；视觉请用 `scripts/diagnostics/check_agent_vision.py`。

## 8. 已知未解决

- 验证器 `max_tokens` 上限 4096 写在 Core 里，配置改不动，见 §6 的三个根治选项。
  在 `off`/`audit` 下该上限不再影响任务结论；只有 `enforce` 仍受它约束。
- ModelArts 端点的图片封禁是平台策略，无法从本项目绕过；需要看图就只能用
  `api.deepseek.com`。该端点现已关闭。
- `deepseek-flash` 作为主模型的表现只在工具调用与视觉两项上验证过；长链推理与
  多步规划的稳定性尚未做真机 AgentTask 验收。
