#!/usr/bin/env python3
"""Read an already-running MOCK Forge Gateway and replay the reference tool calls.
This is deterministic tool integration testing, not LLM planning or real motion.
"""

import asyncio
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT.parent / "PhyAgentOS-core")]
from PhyAgentOS.forge.tool_client import ForgeToolClient  # noqa: E402

from paos_g1d.contracts import DeviceSettings  # noqa: E402
from paos_g1d.manipulation.arm import DEX1_DEFAULT_MAX_OPEN_RAD, tolerance  # noqa: E402
from paos_g1d.manipulation.arm_recipe import PROMPT, recipe  # noqa: E402


def confirmed_left_dex1_open():
    """The real envelope is narrower than the vendor 5.4 mapping constant."""
    settings = DeviceSettings.model_validate(
        yaml.safe_load((ROOT / "configs/device.yaml").read_text())
    )
    return settings.dex1.max_open("left")


async def check(url="http://127.0.0.1:19002", *, real=False):
    left_dex1_open = confirmed_left_dex1_open() if real else DEX1_DEFAULT_MAX_OPEN_RAD
    async with ForgeToolClient(url, timeout_s=2) as client:
        for _ in range(150):
            try:
                ctx = await client.get_tool_context("g1d.move_joints")
                if ctx["data"]["ready"]:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)
        else:
            raise RuntimeError("mock arm Runtime did not become ready")
        response = await client.invoke_query_tool("g1d.arm_state", {"max_age_ms": 100})
        state = response["data"]["response"]["result"]["outputs"]
        if state["simulated"] is not (not real):
            raise RuntimeError("Runtime profile does not match explicitly selected replay mode")
        records = []
        progress_path = ROOT / (
            "dist/arm-real-progress.json" if real else "dist/arm-mock-progress.json"
        )
        for step in recipe(state["positions_rad"], left_dex1_open=left_dex1_open):
            admission = await client.invoke_action(
                step["tool_id"], step["arguments"], timeout_ms=25000
            )
            invocation = admission["data"]["invocation_id"]
            for _ in range(500):
                result_response = await client.invocation_result(invocation)
                result = result_response["data"].get("result")
                if result is not None:
                    break
                await client.invocation_status(invocation)
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("tool result deadline exceeded")
            print(
                step["label"],
                result["status"],
                result.get("outputs", {}).get("positions_rad", {}),
                flush=True,
            )
            progress_path.write_text(
                json.dumps(
                    {
                        "prompt": PROMPT,
                        "completed_steps": records,
                        "latest_step": step,
                        "latest_result": result,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            assert result["status"] == "succeeded", result
            assert result["outputs"]["simulated"] is (not real)
            for t in step["arguments"]["targets"]:
                assert abs(
                    result["outputs"]["positions_rad"][t["joint"]] - t["position_rad"]
                ) <= tolerance(t["joint"])
            records.append({**step, "invocation_id": invocation, "result": result})
        return {
            "prompt": PROMPT,
            "simulated": not real,
            "llm_used": False,
            "passed": True,
            "steps": records,
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Replay parameterized tools; default mock, --real drives the robot"
    )
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(check(real=args.real))
    path = ROOT / (
        "dist/arm-real-replay-report.json" if args.real else "dist/arm-replay-report.json"
    )
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(path)
