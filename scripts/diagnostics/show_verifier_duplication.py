"""Show, using unmodified Core code, that a verifier request repeats the same records three times.

Run from the project root against a real AgentTask record:

    ./scripts/core-env.sh python scripts/diagnostics/show_verifier_duplication.py \
        dist/real-agent-task-20260908T085652.772393Z/task.json

This wraps nothing: it loads the record with Core's own ``AgentTaskRecord``, calls Core's own
``VerificationRequestBuilder.build_agent_task``, and dissects the context Core produced. It is a
reproducer for ``docs/issues/core-verifier-record-duplication.md`` and sends no model request.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / ".deps/forge-gateway/src"), str(ROOT.parent / "PhyAgentOS-core")]

from PhyAgentOS.forge.task import AgentTaskRecord  # noqa: E402
from PhyAgentOS.verification.request_builder import VerificationRequestBuilder  # noqa: E402

size = lambda value: len(json.dumps(value, ensure_ascii=False))  # noqa: E731


def heading(text):
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "task",
        nargs="?",
        default="dist/real-agent-task-20260908T085652.772393Z/task.json",
        help="AgentTask record JSON to inspect",
    )
    parser.add_argument("--workspace", default="workspace", help="workspace root holding evidence")
    args = parser.parse_args()

    task = AgentTaskRecord.model_validate(json.loads(Path(args.task).read_text(encoding="utf-8")))

    # ---------------------------------------------------------------- 1. the nesting itself
    heading("1. 嵌套本身：两处 property 指向同一批对象")
    from_revisions = [r for revision in task.revisions for r in revision.execution_records]
    from_task = task.execution_records
    print(f"  task.revisions[0].execution_records   有 {len(task.revisions[0].execution_records)} 条")
    print(f"  task.execution_records                有 {len(from_task)} 条")
    print("  task.execution_records 的定义（forge/task.py:180）:")
    print("      return [item for revision in self.revisions for item in revision.execution_records]")
    identical = all(a is b for a, b in zip(from_revisions, from_task))
    print(f"\n  两个列表里是【同一批 Python 对象】: {identical}")
    for index, (a, b) in enumerate(zip(from_revisions, from_task)):
        print(
            f"    [{index}] id={id(a):#x} == id={id(b):#x}  {a.tool_id:22s} {a.status}"
            f"  {'同一对象' if a is b else '不同对象'}"
        )

    # ------------------------------------------------- 2. Core's context duplicates that batch
    heading("2. Core 的 context 把这一批对象序列化进三个键")
    builder = VerificationRequestBuilder(args.workspace)
    request = builder.build_agent_task(task, events=[], lessons="[]")
    text = request.content[0]["text"]
    context = json.loads(text[text.index("{") :])

    record_only = size([r.model_dump(mode="json") for r in from_task])
    print(f"  同一批记录只 dump 一次应当是            {record_only:>8,} 字符\n")
    places = [
        ("plan_revisions", context["plan_revisions"]),
        ("tool_execution_records", context["tool_execution_records"]),
        ("gateway_terminal_results", context["gateway_terminal_results"]),
    ]
    total = 0
    for name, value in places:
        n = size(value)
        total += n
        print(f"  context[{name!r}]{' ' * (30 - len(name))}{n:>8,} 字符")
    print(f"  {'三处相加':<32}{total:>8,} 字符")
    print(f"  {'其中纯重复（三处减一份）':<30}{total - record_only:>8,} 字符")

    # Break each key into "record payload" versus "everything else", so it is obvious that the
    # record payload is what repeats and the revision metadata is not the cost.
    print("\n  每个键里「记录载荷」与「其它」各占多少：")
    revision_payload = sum(
        size(revision.get("execution_records") or []) for revision in context["plan_revisions"]
    )
    rows = [
        ("plan_revisions", revision_payload, size(context["plan_revisions"]) - revision_payload),
        ("tool_execution_records", record_only, 0),
        ("gateway_terminal_results", size(context["gateway_terminal_results"]), 0),
    ]
    print(f"    {'键':<28}{'记录载荷':>12}{'其它':>10}")
    for name, payload, other in rows:
        print(f"    {name:<28}{payload:>12,}{other:>10,}")

    print("\n  同样的 9 条记录，其载荷在三个键里各出现一次：")
    print(f"    {'record_id':<26}{'单条体积':>10}  tool_id")
    for revision in task.revisions:
        for record in revision.execution_records:
            dumped = record.model_dump(mode="json")
            print(f"    {record.record_id:<26}{size(dumped):>10,}  {record.tool_id}")
    print(f"    {'合计（= 一份的载荷）':<26}{record_only:>10,}")

    print("\n  血缘元数据字段（Core 已在 _validate_agent_task_lineage 校验完毕，对验证器无用）：")
    fields = list(from_task[0].model_dump(mode="json"))
    metadata = [
        f
        for f in fields
        if f in {"version", "skill_binding_id", "tool_spec_sha256", "caller_id", "ownership",
                 "created_at", "updated_at"}
    ]
    print(f"    记录共 {len(fields)} 个字段，其中 {len(metadata)} 个是血缘元数据: {metadata}")
    in_records = size(context["tool_execution_records"])
    metadata_share = sum(
        len(json.dumps(r.get(f), ensure_ascii=False)) + len(f) + 6
        for r in context["tool_execution_records"]
        for f in metadata
    )
    print(f"    它们在 tool_execution_records 里占 {metadata_share:,} 字符"
          f"（该键 {in_records:,} 字符的 {metadata_share * 100 // in_records}%）")

    # ------------------------------------------------- 3. what the model actually receives
    heading("3. Core 原生 build_agent_task 的产物")
    images = [b for b in request.content if b.get("type") == "image_url"]
    print(f"  content 块数        {len(request.content)}  (1 个文本块 + {len(images)} 张图)")
    print(f"  文本块字符          {len(text):>8,}")
    print(f"  图片 base64 字符    {sum(len(b['image_url']['url']) for b in images):>8,}")
    print(f"  context 总字符      {size(context):>8,}")
    print(f"  valid_evidence_refs {len(request.valid_evidence_refs)} 个: {sorted(request.valid_evidence_refs)}")

    print("\n  文本块里 context 各键的体积（降序）：")
    for n, key in sorted(((size(v), k) for k, v in context.items()), reverse=True)[:6]:
        print(f"    {n:>8,} 字符  {key}")

    heading("结论")
    print(f"  同一批 {len(from_task)} 条执行记录被序列化 {total / record_only:.2f} 次" if record_only else "")
    print(f"  纯重复 {total - record_only:,} 字符，占三处合计的 "
          f"{(total - record_only) * 100 // total}%，占整个 context 的 "
          f"{(total - record_only) * 100 // size(context)}%")
    print("  修复建议见 docs/issues/core-verifier-record-duplication.md")


if __name__ == "__main__":
    main()
