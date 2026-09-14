# [Core] 语义验证请求把同一批执行记录发送三次，占 context 的九成

**组件**：`PhyAgentOS/verification/request_builder.py`
**版本**：PhyAgentOS 1.0.0 / Forge Gateway 1.0.2（本地 checkout）
**类型**：性能 / 资源浪费（不改变判定结果，但显著放大输入并推高截断概率）

---

## 摘要

`VerificationRequestBuilder.build_agent_task()` 构造的 `context` 里，**同一个 AgentTask 的执行记录被序列化了三份**：

| context 键 | 来源 | 内含 |
| --- | --- | --- |
| `plan_revisions` | `task.revisions` | 每个 `PlanRevision` 内嵌 `execution_records` → **副本 1** |
| `tool_execution_records` | `task.execution_records` | 每条记录的完整 `model_dump()` → **副本 2** |
| `gateway_terminal_results` | 同上过滤 `terminal` | 再次带 `response` / `error` / `evidence_refs` → **副本 3** |

三者是**包含关系**（`plan_revisions ⊇ tool_execution_records ⊇ gateway_terminal_results`），
所以后两份没有携带任何新信息。

## 影响（真机实测）

任务 `task_f5422f85d30f4b07`（升降，9 条执行记录：1×`set_height` + 2×`state` + 6×`camera_snapshot`）：

| 指标 | 数值 |
| --- | --- |
| `context` 总字符 | **43,252** |
| 其中三份记录合计 | **39,611（92%）** |
| 其中纯重复（三份减一份） | **25,944（60%）** |
| 序列化后文本 `prompt_tokens` | **18,368** |
| 验证器输出上限 `max_tokens` | **4,096** |

对比：同一请求的 6 张真实 evidence 图（431 KB base64）只占 **1,917 token**，
即文本输入是图片的 **9.6 倍**。

**输入 18,368 / 输出 4,096 的不对称是可直接观察到的故障原因**：在
`PhyAgentOS-g1d` 的真机记录中，9 个任务里有 3 个 Action 实际成功却被判 failed，
失败原因是 `verifier model returned no content` 与
`Unterminated string starting at: line 40 column 9`——推理模型把输出预算耗在
reasoning 上，正文被截断或为空（见该仓库 `docs/verifier-and-models.md` §4）。

放大输入不会让判定更准：验证器只需要「目标 + 判据 + 每条记录的结果 + 证据引用」，
不需要每条记录的全部血缘元数据，更不需要同样的数据三份。

## 复现

```python
import json
from PhyAgentOS.forge.task import AgentTaskRecord

task = AgentTaskRecord.model_validate(json.load(open("<task.json>")))
records = task.execution_records
S = lambda v: len(json.dumps(v, ensure_ascii=False))

print(S([r.model_dump(mode="json") for r in records]))          # 单份完整记录
print(S([r.model_dump(mode="json") for r in task.revisions]))   # 内含同一批记录
print(S([{"record_id": r.record_id, "response": r.response,
          "error": r.error, "evidence_refs": r.evidence_refs,
          "tool_id": r.tool_id, "semantics": r.semantics, "status": r.status,
          "invocation_id": r.invocation_id, "attempt_id": r.attempt_id}
         for r in records if r.terminal]))                        # 第三份
# 期望：三者相加远大于第一行；实测 13,667 + 16,631 + 9,313 = 39,611
```

## 代码调用链路

```
Agent 工具调用 forge_task_finalize
│
└─ AgentTaskCoordinator.finalize_task()                    forge/task.py:884
   │
   └─ ForgeTaskVerifier.verify_agent_task()                agent/session_verifier.py:96
      │
      ├─ self.request_builder.build_agent_task()           agent/session_verifier.py:105
      │  │   （builder 在 __init__ 里构造：session_verifier.py:61）
      │  │
      │  └─ VerificationRequestBuilder.build_agent_task()  verification/request_builder.py:87
      │     │
      │     ├─ 构造 context                                verification/request_builder.py:120-164
      │     │   ├─ "plan_revisions"          :141  → PlanRevision.model_dump()
      │     │   │        PlanRevision.execution_records 内嵌整批记录   ← 副本 1
      │     │   │        （字段定义：forge/task.py:125）
      │     │   ├─ "tool_execution_records"  :142  → record.model_dump()
      │     │   │        19 个字段，含 version/skill_binding_id/
      │     │   │        tool_spec_sha256/caller_id/ownership/时间戳   ← 副本 2
      │     │   └─ "gateway_terminal_results" :143-157
      │     │            再次带 response/error/evidence_refs            ← 副本 3
      │     │
      │     └─ self._build_request(context=…)              request_builder.py:165 → 定义在 :307
      │        ├─ json.dumps(context, indent=2) 塞进单个 text block   :314-325
      │        └─ 逐个追加 base64 图片                                :326-338
      │
      └─ _verify_content()                                 agent/session_verifier.py:119
         └─ _start_and_verify()                            agent/session_verifier.py:151
            └─ VerificationServiceProcess.verify_task()    verification/service.py:127
               │   （HTTP POST 到本地验证服务）
               │
               └─ VerificationEngine.complete()            verification/service.py:217
                  │                                        → verification/engine.py:16
                  └─ provider.chat_with_retry()            verification/engine.py:18
```

**重复在 `request_builder.py:141-157` 三行之间产生，在 `:314-325` 被原样序列化。**

## 根因

`PlanRevision` 内嵌 `execution_records`（`forge/task.py:125`），而
`AgentTaskRecord.execution_records` 又是所有 revision 记录的扁平化视图
（`forge/task.py:180-181`）：

```python
@property
def execution_records(self) -> list[ToolExecutionRecord]:
    return [item for revision in self.revisions for item in revision.execution_records]
```

于是 `plan_revisions` 与 `tool_execution_records` **必然是同一批数据的两种视图**，
同时 dump 两者就等于重复。`gateway_terminal_results` 又在其上叠了第三层。

雪上加霜的是 `record.model_dump()` 带出 **18 个字段**，其中 **7 个是血缘元数据**
（`version` / `skill_binding_id` / `tool_spec_sha256` / `caller_id` / `ownership` /
`created_at` / `updated_at`）。这些字段的唯一用途已经在
`_validate_agent_task_lineage()`（`request_builder.py:266-305`）里用掉了——
**到验证器手上时，它们只剩体积，没有语义。**

## 建议修复

在 `_build_request` 之前投影 `context`，让完整记录只存在于一个地方：

```python
# request_builder.py，替换 :141-157

# 记录只保留验证器能据以判断的字段；血缘字段已在上方校验完毕
RECORD_FIELDS = (
    "record_id", "revision_id", "tool_id", "semantics", "status",
    "invocation_id", "attempt_id", "arguments", "response", "error", "evidence_refs",
)

"plan_revisions": [
    {**rev.model_dump(mode="json"),
     # 记录本体已在 tool_execution_records，这里只留成员关系
     "execution_records": [r.record_id for r in rev.execution_records]}
    for rev in task.revisions
],
"tool_execution_records": [
    {k: v for k, v in r.model_dump(mode="json").items() if k in RECORD_FIELDS}
    for r in records
],
# 与 tool_execution_records 完全重复，只保留标识
"gateway_terminal_results": [
    {"record_id": r.record_id, "tool_id": r.tool_id, "status": r.status}
    for r in records if r.terminal
],
```

### 必须守住的不变量

`valid_evidence_refs = validated.artifact_ids | execution_refs`
（`request_builder.py:118`），其中 `execution_refs` 是**每条记录自身 `evidence_refs`
字段的并集**（`:298-304`）。因此投影**必须原样保留 `evidence_refs` 与记录标识字段**，
并保持 `evidence_bundle` 不变，否则验证器会引用到无法解析的 ref，
在 `_validate_generic_verdict`（`session_verifier.py:167-178`）处失败。

`criteria` / `goal` / `task_verification_contract` 同样不可改动。

## 预期收益（按上述方案实测）

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| `context` 字符 | 43,252 | **17,877（−58%）** |
| 文本 `prompt_tokens` | 18,368 | **7,015（−62%）** |
| `plan_revisions` | 16,631 | 3,189 |
| `gateway_terminal_results` | 9,313 | 845 |

## 附：为什么调用方不该各自绕过

`PhyAgentOS-g1d` 已在项目侧子类里覆写 `_build_request` 实现了同样的投影
（`src/paos_g1d/verification.py`），因为该集成的前提是"不修改 Core"。
但这是**绕过，不是修复**：

- 重复是 `request_builder` 造的，却要每个下游各自裁一遍；
- 裁剪规则分散在各处，裁错（丢失 ref 载体）只会在验证阶段表现为
  `invalid_response` 或 `verifier must return exactly one result for each success criterion`，
  难以定位到根因；
- 每个下游都要重复实现并维护同一套不变量测试。

建议在 Core 侧修复，下游的覆写即可删除。
