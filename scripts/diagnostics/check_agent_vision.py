"""Read-only Agent diagnostic: observe -> native Forge Query -> native ImageTool."""

import argparse
import asyncio
import json
import os
import sys
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


async def main(profile="mock"):
    set_config_path(CONFIG)
    logger.disable("PhyAgentOS")
    config = load_config()
    state = RuntimeStateStore().load("g1d-observe")
    if state is None or state.profile != profile or state.status != "running":
        raise RuntimeError(f"Start g1d-observe --profile {profile} first")
    simulated = profile == "mock"
    install()
    from PhyAgentOS.agent.loop import AgentLoop

    provider = _make_provider(config)
    client, invocations, coordinator, available = _make_forge_components(config, provider)
    agent = AgentLoop(
        MessageBus(),
        provider,
        config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=12,
        forge_tool_client=client,
        forge_tool_invocation_ids=invocations,
        forge_task_coordinator=coordinator,
        runtime_availability_provider=available,
    )
    # This diagnostic exposes only observation tools, with native implementations intact.
    allowed = {"forge_tool_context", "forge_tool_query", "image"}
    for name in list(agent.tools.tool_names):
        if name not in allowed:
            agent.tools.unregister(name)
    assert set(agent.tools.tool_names) == allowed
    trace = []
    execute = agent.tools.execute

    async def record(name, params):
        if name == "forge_tool_query" and params.get("tool_id") != "g1d.camera_snapshot":
            raise RuntimeError("This diagnostic only permits camera snapshot Queries")
        if name == "image" and params.get("mode") != "vision":
            raise RuntimeError("This diagnostic only permits vision mode")
        if name == "image":
            snapshot_paths = {
                json.loads(item["result"])["data"]["response"]["result"]["outputs"]["image_path"]
                for item in trace
                if item["tool"] == "forge_tool_query"
            }
            if params.get("image_path") not in snapshot_paths:
                raise RuntimeError("Only snapshots returned by this diagnostic may be uploaded")
        result = await execute(name, params)
        if name == "forge_tool_query":
            output = json.loads(result)["data"]["response"]["result"]["outputs"]
            if output["simulated"] is not simulated:
                raise RuntimeError("Snapshot identity differs from requested profile")
        trace.append({"tool": name, "arguments": params, "result": result})
        return result

    agent.tools.execute = record
    try:
        # Verify simulation identity before any image can be submitted to the model.
        state_query = await client.invoke_query_tool("g1d.state", {"max_age_ms": 500})
        assert state_query["data"]["response"]["result"]["outputs"]["simulated"] is simulated
        prompt = (
            (
                "这是模拟相机的无任务诊断，请勿创建 AgentTask。先用 forge_tool_context 查看 "
                "g1d.camera_snapshot，再用 forge_tool_query 获取 head 相机的新快照（max_age_ms=1000）。"
                "然后必须调用 image 工具的 vision 模式查看返回的 image_path，读取图片中的文字并描述背景颜色。"
                "最后用中文回答，明确说明这是模拟画面。不要仅从文件名推断图片内容。"
            )
            if simulated
            else (
                "这是 G1-D 真机的只读诊断，请勿创建 AgentTask，也不要执行任何动作。"
                "先用 forge_tool_context 查看 g1d.camera_snapshot，再用 forge_tool_query 分别获取 "
                "head、left_wrist、right_wrist 三路快照（max_age_ms=1000）。必须对每张返回的 image_path "
                "调用 image 工具 vision 模式。用中文分别描述可见场景、是否有明显模糊或遮挡。"
                "无法看清的内容请明确说明，不要猜测文字、尺寸、距离或可执行动作。最后说明这是真机画面。"
            )
        )
        answer = await asyncio.wait_for(
            agent.process_direct(
                prompt,
                session_key=f"cli:{profile}-vision-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"),
            ),
            timeout=180,
        )
        snapshots = [
            json.loads(item["result"])["data"]["response"]["result"]["outputs"]
            for item in trace
            if item["tool"] == "forge_tool_query"
        ]
        images = [item for item in trace if item["tool"] == "image"]
        assert snapshots and all(item["simulated"] is simulated for item in snapshots)
        assert images and images[-1]["arguments"]["image_path"] in {
            item["image_path"] for item in snapshots
        }
        visual_answer = images[-1]["result"]
        assert not visual_answer.startswith("Error"), visual_answer
        background_correct = "蓝" in visual_answer or "blue" in visual_answer.lower()
        label_exact = "SIMULATION" in visual_answer.upper() and "HEAD" in visual_answer.upper()
        if simulated:
            assert "模拟" in answer, answer
        else:
            assert {item["source"] for item in snapshots} == {"head", "left_wrist", "right_wrist"}
            assert {item["image_path"] for item in snapshots} <= {
                item["arguments"]["image_path"] for item in images
            }
            assert all(not item["result"].startswith("Error") for item in images)
        report = {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "model": config.agents.defaults.model,
            "profile": profile,
            "integration_passed": True,
            "visual_checks": {
                "blue_background_recognized": background_correct,
                "small_label_exact": label_exact,
                "expected_label": "SIMULATION / head",
            }
            if simulated
            else {"three_cameras_analyzed": True, "accuracy_manually_unverified": True},
            "scope": "native AgentLoop diagnostic with unbound Query; not AgentTask/real robot acceptance",
            "answer": answer,
            "trace": trace,
        }
        output_path = ROOT / (
            "dist/agent-vision-report.json" if simulated else "dist/real-agent-vision-report.json"
        )
        output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(answer)
        print(f"Saved {output_path}")
    finally:
        await agent.close_mcp()
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("mock", "real"), default="mock")
    asyncio.run(main(parser.parse_args().profile))
