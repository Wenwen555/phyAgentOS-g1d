"""One explicit 10 cm rise through Core's Tool API; default is a read-only preview."""

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from init_instance import CONFIG, ROOT
from PhyAgentOS.config.loader import set_config_path
from PhyAgentOS.forge.tool_client import ForgeToolClient
from PhyAgentOS.skill_runtime.state import RuntimeStateStore


async def run(execute):
    set_config_path(CONFIG)
    runtime = RuntimeStateStore().load("g1d-lift")
    if runtime is None or runtime.profile != "real" or runtime.status != "running":
        raise RuntimeError("A running g1d-lift/real runtime is required")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = ROOT / "dist" / f"lift-10cm-{stamp}"
    directory.mkdir(parents=True, exist_ok=False)
    report = {"started_at_utc": stamp, "execute": execute, "rise_m": 0.1,
              "action_submissions": 0, "samples": []}
    path = directory / "report.json"

    def save():
        path.write_text(json.dumps(report, indent=2) + "\n")

    async with ForgeToolClient(runtime.gateway_url, timeout_s=3) as client:
        async def state():
            response = await client.invoke_query_tool("g1d.state", {"max_age_ms": 300})
            result = response["data"]["response"]["result"]
            if result["status"] != "succeeded":
                raise RuntimeError(f"State query failed: {result}")
            value = result["outputs"]
            if value["simulated"] is not False or not math.isfinite(value["height_m"]):
                raise RuntimeError("Finite real height feedback required")
            return value

        async def snapshots():
            outputs = {}
            for source in ("head", "left_wrist", "right_wrist"):
                response = await client.invoke_query_tool(
                    "g1d.camera_snapshot", {"source": source, "max_age_ms": 1000}
                )
                result = response["data"]["response"]["result"]
                if result["status"] != "succeeded" or result["outputs"]["simulated"]:
                    raise RuntimeError(f"Real snapshot unavailable: {source}")
                outputs[source] = result["outputs"]
            return outputs

        invocation = None
        terminal = False
        try:
            context = await client.get_tool_context("g1d.set_height")
            report["context"] = context
            if not context["data"]["ready"]:
                raise RuntimeError("Lift endpoint is not ready")
            # Core's installed Tool schema carries the confirmed physical limits.
            spec = (await client.get_tool("g1d.set_height"))["data"]
            bounds = spec["input_schema"]["properties"]["target_height_m"]
            if bounds.get("minimum") != 0.0 or bounds.get("maximum") != 0.42:
                raise RuntimeError("Installed limits differ from the confirmed 0–0.42 m range")
            report["before_images"] = await snapshots()
            initial_samples = []
            for _ in range(5):
                initial_samples.append(await state())
                await asyncio.sleep(0.1)
            heights = [s["height_m"] for s in initial_samples]
            sequences = [s["height_sequence"] for s in initial_samples]
            if max(heights) - min(heights) > 0.0005 or any(
                b <= a for a, b in zip(sequences, sequences[1:])
            ):
                raise RuntimeError("Require stable height and advancing feedback before motion")
            initial = heights[-1]
            target = initial + 0.1
            if not 0 <= initial < target <= 0.415:
                raise RuntimeError("Rise must fit within confirmed travel with 5 mm upper margin")
            report.update(initial_samples=initial_samples, initial_height_m=initial,
                          target_height_m=target, tolerance_m=0.002, timeout_s=30.0)
            save()
            print(f"Initial={initial:.6f} m, target={target:.6f} m; report={path}", flush=True)
            if not execute:
                return
            # Exactly one admission attempt: never retry an ambiguous action request.
            report["action_submissions"] = 1
            save()
            admission = await client.invoke_action(
                "g1d.set_height",
                {"target_height_m": target, "tolerance_m": 0.002, "timeout_s": 30.0},
                caller_id="g1d-operator-10cm", timeout_ms=40000,
            )
            report["admission"] = admission
            invocation = admission["data"]["invocation_id"]
            save()
            deadline = time.monotonic() + 36
            while time.monotonic() < deadline:
                response = await client.invocation_result(invocation)
                result = response["data"].get("result")
                if result is not None:
                    terminal = True
                    report["result"] = result
                    break
                sample = await state()
                report["samples"].append(sample)
                height = sample["height_m"]
                print(f"height={height:.6f} m, rise={(height-initial)*1000:.1f} mm", flush=True)
                save()
                if height < initial - 0.002 or height > target + 0.003:
                    raise RuntimeError("Unexpected direction or target overshoot; cancelling")
                await client.invocation_status(invocation)
                await asyncio.sleep(0.25)
            if not terminal:
                raise RuntimeError("Action result deadline exceeded; cancelling")
            report["after_state"] = await state()
            report["actual_rise_m"] = report["after_state"]["height_m"] - initial
            report["after_images"] = await snapshots()
            output = report["result"].get("outputs", {})
            report["passed"] = (
                report["result"]["status"] == "succeeded"
                and output.get("reached_goal") is True
                and output.get("stop_command_accepted") is True
                and output.get("stopped_observed") is True
                and output.get("simulated") is False
                and abs(report["actual_rise_m"] - 0.1) <= 0.002
            )
            print(json.dumps({k: report[k] for k in ("result", "actual_rise_m", "passed")}), flush=True)
            if not report["passed"]:
                raise RuntimeError("Action did not meet completion and observed-stop criteria")
        except BaseException as error:
            report["error"] = f"{type(error).__name__}: {error}"
            report["passed"] = False
            if invocation is not None and not terminal:
                try:
                    report["cancel_response"] = await client.cancel_invocation(invocation)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        response = await client.invocation_result(invocation)
                        if response["data"].get("result") is not None:
                            report["result_after_cancel"] = response["data"]["result"]
                            break
                        await asyncio.sleep(0.2)
                except Exception as cancel_error:
                    report["cancel_error"] = str(cancel_error)
            raise
        finally:
            save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Submit one real 10 cm rise")
    asyncio.run(run(parser.parse_args().execute))
