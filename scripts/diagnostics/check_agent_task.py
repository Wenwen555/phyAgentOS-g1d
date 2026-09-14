"""Native DeepSeek Agent -> bound Forge Action -> enforce-mode AgentTask verification."""

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from init_instance import CONFIG, ROOT

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ["PATH"] = str(ROOT / ".tools") + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, str(ROOT / "src"))

from loguru import logger  # noqa: E402
from PhyAgentOS.bus.queue import MessageBus  # noqa: E402
from PhyAgentOS.cli.commands import _make_forge_components, _make_provider  # noqa: E402
from PhyAgentOS.config.loader import load_config, set_config_path  # noqa: E402
from PhyAgentOS.skill_runtime.state import RuntimeStateStore  # noqa: E402

from paos_g1d.agent_integration import install  # noqa: E402
from paos_g1d.verification import G1dVerificationRequestBuilder  # noqa: E402


async def main(profile, target_height_m, delta_height_m, raw_prompt=None):
    set_config_path(CONFIG)
    logger.disable("PhyAgentOS")
    config = load_config()
    config.agents.verification.evidence_retention = "all"
    config.agents.verification.max_replans_per_episode = 0
    runtime = RuntimeStateStore().load("g1d-lift")
    if runtime is None or runtime.profile != profile or runtime.status != "running":
        raise RuntimeError(f"Start g1d-lift --profile {profile} first")
    install()
    from PhyAgentOS.agent.loop import AgentLoop

    provider = _make_provider(config)
    client, invocations, coordinator, available = _make_forge_components(config, provider)
    if coordinator.store.active() is not None:
        await client.close()
        raise RuntimeError("An existing AgentTask must be reconciled before starting a new one")
    if coordinator.verifier is None:
        await client.close()
        raise RuntimeError("Core verification service must be enabled")
    coordinator.verifier.request_builder = G1dVerificationRequestBuilder(config.workspace_path)
    agent = AgentLoop(
        MessageBus(), provider, config.workspace_path, model=config.agents.defaults.model,
        max_iterations=40, forge_tool_client=client, forge_tool_invocation_ids=invocations,
        forge_task_coordinator=coordinator, runtime_availability_provider=available,
    )
    allowed = {
        "activate_skill", "read_file", "forge_tool_context", "forge_task_create", "forge_task_get",
        "forge_tool_query", "forge_tool_start_action", "forge_tool_action_status",
        "forge_tool_action_result", "forge_tool_cancel_action", "forge_task_cancel",
        "forge_task_finalize",
    }
    for name in list(agent.tools.tool_names):
        if name not in allowed:
            agent.tools.unregister(name)
    if set(agent.tools.tool_names) != allowed:
        raise RuntimeError("Native Agent tool registration is incomplete")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = ROOT / "dist" / f"{profile}-agent-task-{stamp}"
    directory.mkdir(parents=True, exist_ok=False)
    report = {"started_at_utc": stamp, "profile": profile, "model": config.agents.defaults.model,
              "trace": [], "agent_messages": [], "action_submissions": 0, "passed": False}
    task_id = None
    execute = agent.tools.execute

    def save():
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if task_id is not None:
            task = coordinator.get_task(task_id)
            (directory / "task.json").write_text(task.model_dump_json(indent=2) + "\n")
            (directory / "events.json").write_text(
                json.dumps(coordinator.store.events(task_id), ensure_ascii=False, indent=2) + "\n"
            )

    async def state():
        result = (await client.invoke_query_tool("g1d.state", {"max_age_ms": 300}))["data"]["response"]["result"]
        if result["status"] != "succeeded" or result["outputs"]["simulated"] is not (profile == "mock"):
            raise RuntimeError("Fresh state with matching simulation identity required")
        return result["outputs"]

    async def record(name, params):
        nonlocal task_id, expected_target
        entry = {"tool": name, "arguments": params, "started_at_utc": datetime.now(timezone.utc).isoformat()}
        report["trace"].append(entry)
        save()
        print(f"Agent tool: {name}", flush=True)
        try:
            if name not in allowed:
                raise ValueError("Use only the registered tools for this diagnostic")
            if name == "read_file":
                skill_path = ROOT / ".paos-instance/skills/g1d-lift/SKILL.md"
                if Path(params.get("path", "")).resolve() != skill_path.resolve():
                    raise ValueError("Only the installed g1d-lift SKILL.md may be read")
            if name == "activate_skill" and (params.get("name") != "g1d-lift" or params.get("role", "primary") != "primary"):
                raise ValueError("This run only permits the g1d-lift primary Skill")
            if name == "forge_task_create":
                if task_id is not None:
                    raise ValueError("Only one AgentTask is permitted in this run")
                verification = params.get("verification", {})
                if verification.get("mode") != "enforce":
                    raise ValueError("Use enforce verification; execution success alone is insufficient")
                policy = verification.get("evidence_policy", {})
                if set(policy.get("required_kinds", [])) != {"rgb_image", "robot_state"} or set(policy.get("required_sources", [])) != {"head", "left_wrist", "right_wrist"}:
                    raise ValueError("Require before/after robot_state and all three camera sources")
                if policy.get("minimum_association") != "best_effort":
                    raise ValueError("This adapter supports best_effort evidence association")
                if raw_prompt is None and verification.get("success_criteria") != criteria:
                    raise ValueError("Use the supplied measurable success criteria without weakening them")
                if raw_prompt is not None and len(verification.get("success_criteria", [])) < 3:
                    raise ValueError("Specify goal/displacement, stopped result, and complete evidence criteria")
            if name not in {"activate_skill", "read_file", "forge_tool_context", "forge_task_create"}:
                preflight_query = (raw_prompt is not None and task_id is None and name == "forge_tool_query"
                                   and params.get("tool_id") == "g1d.state" and not params.get("task_id"))
                if not preflight_query and (task_id is None or params.get("task_id") != task_id):
                    raise ValueError("All execution and lifecycle tools must bind to this run's task_id")
            if name == "forge_tool_query" and params.get("tool_id") not in {"g1d.state", "g1d.camera_snapshot"}:
                raise ValueError("Only state and camera Queries are permitted")
            if name == "forge_tool_start_action":
                if report["action_submissions"]:
                    raise ValueError("One Action admission attempt only; reconcile the existing invocation")
                args = params.get("arguments", {})
                target = args.get("target_height_m")
                if params.get("tool_id") != "g1d.set_height" or isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target):
                    raise ValueError("Action must specify a finite numeric g1d.set_height target")
                if expected_target is not None and abs(target - expected_target) > 0.0001:
                    raise ValueError("Action must match the explicit numeric target argument")
                spec = (await client.get_tool("g1d.set_height"))["data"]
                bounds = spec["input_schema"]["properties"]["target_height_m"]
                if not bounds["minimum"] <= target <= bounds["maximum"]:
                    raise ValueError("Action target is outside the installed device limits")
                requested_timeout = args.get("timeout_s")
                if args.get("tolerance_m") != 0.002 or not isinstance(requested_timeout, (int, float)) or isinstance(requested_timeout, bool) or not 0 < requested_timeout <= duration:
                    raise ValueError(f"Use tolerance_m=0.002 and timeout_s within (0,{duration}]")
                if params.get("timeout_ms") != int((requested_timeout + 10) * 1000):
                    raise ValueError("Gateway timeout_ms must equal (timeout_s + 10) * 1000")
                current = await state()
                if abs(current["height_m"] - initial["height_m"]) > 0.001:
                    raise RuntimeError("Robot moved during planning; do not dispatch the stale plan")
                expected_target = target
                report["action_target_m"] = target
                report["target_source"] = "agent_tool_arguments" if raw_prompt is not None else "numeric_cli_arguments"
                report["action_submissions"] = 1
                save()
            result = await execute(name, params)
            entry["result"] = result
            if name == "forge_task_create":
                parsed = json.loads(result)
                if parsed.get("ok"):
                    task_id = parsed["data"]["task_id"]
                    report["task_id"] = task_id
            save()
            return result
        except ValueError as error:
            # Parameter rejection is fed back to the Agent, with no physical dispatch.
            entry["result"] = json.dumps({"ok": False, "error": str(error)})
            save()
            return entry["result"]

    agent.tools.execute = record

    async def progress(message, **kwargs):
        report["agent_messages"].append({"text": message, "tool_hint": kwargs.get("tool_hint", False)})
        save()

    try:
        initial = await state()
        expected_target = None
        criteria = []
        duration = 15.0 if profile == "mock" else 30.0
        gateway_timeout_ms = 25000 if profile == "mock" else 40000
        report["initial_state"] = initial
        if raw_prompt is None:
            delta = 0.1 if delta_height_m is None else delta_height_m
            expected_target = initial["height_m"] + delta if target_height_m is None else target_height_m
            spec = (await client.get_tool("g1d.set_height"))["data"]
            bounds = spec["input_schema"]["properties"]["target_height_m"]
            if not math.isfinite(expected_target) or not bounds["minimum"] <= expected_target <= bounds["maximum"]:
                raise RuntimeError("Numeric target is outside the installed device limits")
            criteria = [
                f"Exactly one g1d.set_height Action targets {expected_target:.9f} m with tolerance_m=0.002, and finishes succeeded with reached_goal=true and simulated={str(profile == 'mock').lower()}.",
                f"The final height is within 0.002 m of {expected_target:.9f} m; stop_command_accepted and stopped_observed are both true (stop means fresh column feedback stability).",
                "Before and after evidence contains robot_state and RGB images from head, left_wrist and right_wrist, with complete best_effort association. Images provide context, not metric height measurement.",
            ]
            report.update(authorized_target_m=expected_target,
                          requested_delta_m=expected_target-initial["height_m"], success_criteria=criteria)
        print(f"{profile}: initial={initial['height_m']:.9f} m; report={directory}", flush=True)
        prompt = raw_prompt if raw_prompt is not None else (
            f"请独立规划并完成一次 G1-D {'模拟' if profile == 'mock' else '真实'}柱高任务。"
            f"将立柱移动到绝对反馈坐标 {expected_target:.9f} m，当前参考值 {initial['height_m']:.9f} m。"
            "先简述执行计划，再根据已注册 Skill 和工具说明自行选择工具完成任务。"
            "必须使用 g1d-lift 的 primary activation 和 Core AgentTask；所有 Query/Action 绑定同一个 task_id。"
            "现场坐标和本次运动已确认：最低点 0 m、向上为正；真机上限 0.42 m；现场有人看护、可急停且行程无障碍。"
            f"最多执行一个升降 Action，参数 tolerance_m=0.002、timeout_s={duration}，Gateway timeout_ms={gateway_timeout_ms}。"
            "不得重试或追加补偿运动；动作异常则取消、核对结果并如实报告。"
            "验收必须为 enforce，不能以 Action 成功替代 AgentTask 验收。"
            "evidence_policy 使用 required_kinds=[rgb_image,robot_state]，required_sources=[head,left_wrist,right_wrist]，"
            "minimum_association=best_effort。Core 在动作前后自动采集原生证据，独立验收模型会查看图像和状态。"
            "请在 verification.success_criteria 原样使用以下条款：" + json.dumps(criteria, ensure_ascii=False)
            + "。执行后读取原生 Action result、查询最终状态、调用任务 finalize，再读取任务状态和 verdict。"
            "只根据真实工具结果用中文汇报目标、最终高度、停止反馈、task_id、PlanRevision、invocation_id 和验收结论。"
        )
        report["prompt"] = prompt
        report["prompt_mode"] = "verbatim_user_input" if raw_prompt is not None else "script_template"
        save()
        await coordinator.verifier.start()
        report["answer"] = await asyncio.wait_for(
            agent.process_direct(prompt, session_key=f"cli:{profile}-agent-task-{stamp}", on_progress=progress),
            timeout=480,
        )
        if task_id is None:
            report["outcome"] = "no_task_created"
            report["passed"] = None
            print(report["answer"], flush=True)
            print(f"No task or motion submitted; report={directory}", flush=True)
            return
        task = coordinator.get_task(task_id)
        actions = [r for r in task.execution_records if r.semantics == "action"]
        action_output = {}
        if len(actions) == 1 and actions[0].response:
            action_output = actions[0].response.get("data", {}).get("result", {}).get("outputs", {})
        report["after_state"] = await state()
        report["passed"] = (
            task.status == "succeeded" and task.verdict is not None and task.verdict.verdict == "success"
            and task.verification.mode == "enforce" and len(task.verification_attempts) >= 1
            and task.primary_skill_binding is not None and len(task.revisions) == 1
            and len(actions) == 1 and actions[0].status == "succeeded"
            and action_output.get("reached_goal") is True
            and action_output.get("stop_command_accepted") is True
            and action_output.get("stopped_observed") is True
            and action_output.get("simulated") is (profile == "mock")
            and abs(report["after_state"]["height_m"] - expected_target) <= 0.002
            and {"activate_skill", "forge_task_create", "forge_tool_start_action", "forge_task_finalize"}
            <= {entry["tool"] for entry in report["trace"]}
        )
        print(report["answer"], flush=True)
        print(f"AgentTask passed={report['passed']}; report={directory}", flush=True)
        if not report["passed"]:
            raise RuntimeError("Full AgentTask acceptance did not pass; do not repeat the physical action")
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        if task_id is not None:
            task = coordinator.get_task(task_id)
            if not task.terminal:
                await coordinator.cancel_task(task_id, reason="agent_diagnostic_aborted")
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    task = await coordinator.reconcile_nonterminal()
                    if task is None or all(r.terminal for r in task.execution_records):
                        break
                    await asyncio.sleep(0.2)
                task = coordinator.get_task(task_id)
                if not task.terminal and task.execution_records and all(r.terminal for r in task.execution_records):
                    await coordinator.finalize_task(task_id)
        raise
    finally:
        save()
        lines = [f"# G1-D {profile} AgentTask 全链路记录", "", "## 用户任务", "",
                 report.get("prompt", "尚未生成"), "", "## Agent 执行消息", ""]
        lines.extend(item["text"] + "\n" for item in report["agent_messages"] if not item["tool_hint"])
        lines += ["## 工具调用与返回", ""]
        for index, entry in enumerate(report["trace"], 1):
            lines += [f"### {index}. {entry['tool']}", "", "参数：", "```json",
                      json.dumps(entry["arguments"], ensure_ascii=False, indent=2), "```", "",
                      "返回：", "```json", entry.get("result", "未返回"), "```", ""]
        lines += ["## Agent 最终答复", "", report.get("answer", "尚无最终答复"), "",
                  f"脚本独立检查：passed={report['passed']}", "", report.get("error", "")]
        (directory / "transcript.md").write_text("\n".join(lines) + "\n")
        coordinator.verifier.stop()
        await agent.close_mcp()
        await client.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("mock", "real"), default="mock")
    motion = parser.add_mutually_exclusive_group()
    motion.add_argument("--target-height-m", type=float)
    motion.add_argument("--delta-height-m", type=float, help="Signed relative height; negative descends")
    motion.add_argument("--prompt", help="Pass user text verbatim to AgentLoop without parsing its wording or intent")
    args = parser.parse_args(argv)
    if args.profile == "real" and args.target_height_m is None and args.delta_height_m is None and args.prompt is None:
        parser.error("Specify --prompt, --target-height-m or --delta-height-m for a real run")
    return args


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.profile, args.target_height_m, args.delta_height_m, args.prompt))
