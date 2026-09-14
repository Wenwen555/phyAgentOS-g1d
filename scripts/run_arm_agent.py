#!/usr/bin/env python3
"""Send a user prompt and explicit elbow-coordinate options to the native PAOS Agent.
No scripted motion recipe is injected; the Agent must select native Forge tools.
"""

import argparse
import asyncio
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT.parent / "PhyAgentOS-core")]
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
# Match project_cli.py: Core discovers running flows through the project Dora binary.
os.environ["PATH"] = str(ROOT / ".tools") + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault("PAOS_G1D_SNAPSHOT_DIR", str(ROOT / "workspace/g1d_snapshots"))
from PhyAgentOS.agent.loop import AgentLoop  # noqa: E402
from PhyAgentOS.bus.queue import MessageBus  # noqa: E402
from PhyAgentOS.cli.commands import _make_forge_components, _make_provider  # noqa: E402
from PhyAgentOS.config.loader import load_config, set_config_path  # noqa: E402
from PhyAgentOS.skill_runtime.state import RuntimeStateStore  # noqa: E402

from paos_g1d.manipulation.arm import tolerance  # noqa: E402
from paos_g1d.manipulation.arm_recipe import PROMPT  # noqa: E402


def action_result_summary(payload):
    """Expose execution failures in the terminal, using actual returned feedback."""
    data = payload.get("data", {})
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    outputs = result.get("outputs", {})
    positions = outputs.get("positions_rad", {})
    references = outputs.get("references_rad", {})
    deviations = []
    for joint, target in references.items():
        if joint in positions:
            error = abs(positions[joint] - target)
            deviations.append(
                f"{joint}: measured={positions[joint]:.4f} reference={target:.4f} "
                f"error={error:.4f} rad ({math.degrees(error):.2f} deg) "
                f"tolerance={tolerance(joint):.4f} rad"
            )
    error = result.get("error") or {}
    return (
        f"Action result: {result.get('status')} {error.get('message', '')}"
        + ("; reference deviations: " + "; ".join(deviations) if deviations else "")
        + (f"; elbow_bend_deg={outputs['elbow_bend_deg']}" if outputs.get("elbow_bend_deg") else "")
        + (
            f"; world_elevation_deg={outputs['forearm_world_elevation_deg']}"
            if outputs.get("forearm_world_elevation_deg")
            else ""
        )
    )


def elbow_mode_prompt(prompt, mode, angle):
    override = (
        f"Forward elbow angle override: {angle} degrees."
        if angle is not None
        else "Read the forward elbow angle from the user prompt."
    )
    return (
        prompt
        + f"""

[Explicit elbow coordinate options]
elbow_mode={mode}. {override}
upper_arm: commissioned bend convention, straight=0 deg, right-angle bend=90 deg.
world: forearm longitudinal-axis elevation relative to gravity, down=-90 deg,
horizontal=0 deg, up=+90 deg. This controls elevation only, not XYZ position or azimuth.
For forward elbow moves use g1d.move_joints with elbows=[{{"side":"left" or "right",
"mode":"{mode}","angle_deg":requested angle}}]. Do NOT convert the angle to encoder
radians yourself or use raw elbow targets for the forward move. Move the shoulder first
in a separate Action; the runtime uses fresh measured shoulder and torso IMU orientation.
Keep shoulders and base still during world elbow motion. No persistent world lock is provided
after later shoulder/base motion. If the angle is unreachable, report it; do not substitute
another angle or move extra joints. Reverse with raw targets restoring saved encoder values,
not with the forward angle override. If live tool schema lacks elbows, stop and request upgrade.
"""
    )


def supports_elbow_modes(value):
    if isinstance(value, dict):
        if "elbows" in value.get("properties", {}):
            return True
        return any(supports_elbow_modes(child) for child in value.values())
    if isinstance(value, list):
        return any(supports_elbow_modes(child) for child in value)
    return False


async def reconcile_cancelled_task(coordinator, client):
    """Finish an already-cancelled task only after confirmed terminal GET results."""
    task = coordinator.store.active()
    if task is None or not task.cancellation_requested:
        return task
    for record in task.execution_records:
        if record.semantics == "query" or record.terminal:
            continue
        if not record.invocation_id:
            return task
        # Failed GETs must not be converted into evidence that execution stopped.
        try:
            response = await client.invocation_status(record.invocation_id)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot reconcile {task.task_id}: cannot read {record.invocation_id}; "
                "retain task until its execution result is recovered"
            ) from exc
        observe = (
            coordinator.observe_session
            if record.semantics == "session"
            else coordinator.observe_action
        )
        observe(task.task_id, record.invocation_id, response)
    task = coordinator.store.get(task.task_id)
    if task.execution_records and all(
        record.terminal and record.status != "unknown"
        for record in task.execution_records
        if record.semantics != "query"
    ):
        finished = await coordinator.finalize_task(task.task_id)
        print(f"Reconciled previous task: {finished.task_id} status={finished.status.value}")
    return coordinator.store.active()


async def run(args):
    set_config_path(args.config.resolve())
    config = load_config()
    from PhyAgentOS.skill_runtime.manager import RuntimeManager

    RuntimeManager().status("g1d-robot")
    runtime = RuntimeStateStore().load("g1d-robot")
    if runtime is None or runtime.profile != args.profile or runtime.status != "running":
        raise RuntimeError(f"Start the installed g1d-robot runtime with profile {args.profile} first")
    if args.check_only:
        from PhyAgentOS.forge.tool_client import ForgeToolClient

        async with ForgeToolClient(runtime.gateway_url) as probe:
            for name in ("g1d.arm_state", "g1d.move_joints"):
                context = await probe.get_tool_context(name)
                if not context["data"]["ready"]:
                    raise RuntimeError(f"Tool not ready: {name}")
            response = await probe.invoke_query_tool("g1d.arm_state", {"max_age_ms": 100})
            result = response["data"]["response"]["result"]
            if result["status"] != "succeeded" or result["outputs"]["simulated"] is not (
                args.profile == "mock"
            ):
                raise RuntimeError("Arm feedback or profile mismatch")
            if not supports_elbow_modes(await probe.get_tool("g1d.move_joints")):
                raise RuntimeError("Install g1d-robot 0.1.5 and its locked Node for elbow modes")
            if args.elbow_mode == "world":
                outputs = result["outputs"]
                if (
                    outputs.get("torso_rpy_rad") is None
                    or not 0 <= outputs.get("torso_age_s", -1) <= 0.1
                ):
                    raise RuntimeError("World mode needs fresh torso IMU; no motion submitted")
        print(
            f"READY: g1d-robot profile={args.profile}; tools and fresh feedback verified; no motion requested"
        )
        return
    provider = _make_provider(config)
    client, invocations, coordinator, available = _make_forge_components(config, provider)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    effective_prompt = elbow_mode_prompt(args.prompt, args.elbow_mode, args.elbow_angle_deg)
    report = {
        "prompt": args.prompt,
        "effective_prompt": effective_prompt,
        "elbow_mode": args.elbow_mode,
        "elbow_angle_deg": args.elbow_angle_deg,
        "profile": args.profile,
        "llm_used": True,
        "trace": [],
    }
    path = ROOT / "dist" / f"arm-agent-{stamp}.json"

    def save():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    try:
        active = await reconcile_cancelled_task(coordinator, client)
        if active is not None:
            raise RuntimeError(
                f"Existing AgentTask {active.task_id} is {active.status.value}; "
                "finish or cancel and reconcile it before starting another motion"
            )
        response = await client.invoke_query_tool("g1d.arm_state", {"max_age_ms": 100})
        initial = response["data"]["response"]["result"]
        if initial["status"] != "succeeded" or initial["outputs"]["simulated"] is not (
            args.profile == "mock"
        ):
            raise RuntimeError("Arm feedback or runtime identity mismatch")
        report["initial"] = initial
        context = await client.get_tool("g1d.move_joints")
        if not supports_elbow_modes(context):
            raise RuntimeError("Install g1d-robot 0.1.5 and its locked Node before using elbow modes")
        if args.elbow_mode == "world":
            outputs = initial["outputs"]
            if (
                outputs.get("torso_rpy_rad") is None
                or not 0 <= outputs.get("torso_age_s", -1) <= 0.1
            ):
                raise RuntimeError("World mode needs fresh torso IMU; no motion submitted")
        agent = AgentLoop(
            MessageBus(),
            provider,
            config.workspace_path,
            model=config.agents.defaults.model,
            max_iterations=100,
            forge_tool_client=client,
            forge_tool_invocation_ids=invocations,
            forge_task_coordinator=coordinator,
            runtime_availability_provider=available,
        )
        allowed = {
            "activate_skill",
            "forge_tool_context",
            "forge_task_create",
            "forge_task_get",
            "forge_tool_query",
            "forge_tool_start_action",
            "forge_tool_action_status",
            "forge_tool_action_result",
            "forge_tool_cancel_action",
            "forge_task_finalize",
            "forge_task_cancel",
        }
        for name in list(agent.tools.tool_names):
            if name not in allowed:
                agent.tools.unregister(name)
        catalog = await client.list_tools()

        def tool_ids(value):
            if isinstance(value, dict):
                own = [value["tool_id"]] if isinstance(value.get("tool_id"), str) else []
                return own + [name for child in value.values() for name in tool_ids(child)]
            if isinstance(value, list):
                return [name for child in value for name in tool_ids(child)]
            return []

        available_ids = sorted(set(tool_ids(catalog)))
        execute = agent.tools.execute

        async def record(name, params):
            if name == "activate_skill" and params.get("name") != "g1d-robot":
                return json.dumps({"ok": False, "error": "Use g1d-robot primary Skill"})
            if name == "forge_tool_start_action" and params.get("tool_id") != "g1d.move_joints":
                return json.dumps(
                    {"ok": False, "error": "Only g1d.move_joints is available for this run"}
                )
            if name == "forge_tool_start_action":
                for elbow in params.get("arguments", {}).get("elbows", []):
                    if elbow.get("mode") != args.elbow_mode or (
                        args.elbow_angle_deg is not None
                        and elbow.get("angle_deg") != args.elbow_angle_deg
                    ):
                        return json.dumps(
                            {
                                "ok": False,
                                "error": (
                                    f"Use elbow mode={args.elbow_mode}, forward angle override="
                                    f"{args.elbow_angle_deg}; reverse using saved encoder targets"
                                ),
                            }
                        )
            entry = {"tool": name, "arguments": params}
            print(f"Agent tool: {name}", flush=True)
            report["trace"].append(entry)
            save()
            result = await execute(name, params)
            if name == "forge_tool_context":
                try:
                    described = json.loads(result)
                    described["available_tool_ids"] = available_ids
                    result = json.dumps(described, ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
            entry["result"] = result
            save()
            if name in ("forge_tool_action_result", "forge_tool_action_status"):
                try:
                    summary = action_result_summary(json.loads(result))
                except (ValueError, TypeError, AttributeError):
                    summary = None
                if summary:
                    print(summary, flush=True)
            return result

        agent.tools.execute = record
        if coordinator.verifier:
            await coordinator.verifier.start()
        report["answer"] = await asyncio.wait_for(
            agent.process_direct(effective_prompt, session_key="cli:arm-" + stamp), timeout=600
        )
        report["final"] = await client.invoke_query_tool("g1d.arm_state", {"max_age_ms": 100})
        # Preserve tool/AgentTask facts; do not label an Agent answer alone as successful verification.
        print(report["answer"])
        print(path)
    finally:
        save()
        if coordinator.verifier:
            coordinator.verifier.stop()
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".paos-instance/config.json")
    parser.add_argument(
        "--prompt",
        default=PROMPT,
        help="User instruction passed verbatim to the Agent; defaults to the original sequence",
    )
    parser.add_argument("--profile", choices=("mock", "real"), default="mock")
    parser.add_argument(
        "--elbow-mode",
        choices=("upper_arm", "world"),
        default="upper_arm",
        help="Elbow bend relative to upper arm, or ground-relative forearm elevation",
    )
    parser.add_argument(
        "--elbow-angle-deg",
        type=float,
        help="Override forward elbow angle in degrees; reverse uses saved encoders",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check Runtime and fresh arm feedback without model calls or motion",
    )
    args = parser.parse_args()
    if not args.prompt.strip():
        parser.error("--prompt must not be empty")
    if args.elbow_angle_deg is not None:
        lo, hi = (0, 180) if args.elbow_mode == "upper_arm" else (-90, 90)
        if not math.isfinite(args.elbow_angle_deg) or not lo <= args.elbow_angle_deg <= hi:
            parser.error(f"--elbow-angle-deg must be finite and in [{lo}, {hi}] for this mode")
    asyncio.run(run(args))
