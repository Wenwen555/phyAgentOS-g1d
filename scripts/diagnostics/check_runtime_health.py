"""Read-only sustained health check for the explicitly selected running observe profile."""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from init_instance import CONFIG, ROOT
from PhyAgentOS.config.loader import set_config_path
from PhyAgentOS.forge.tool_client import ForgeToolClient
from PhyAgentOS.skill_runtime.state import RuntimeStateStore


async def check(profile, seconds):
    set_config_path(CONFIG)
    runtime = RuntimeStateStore().load("g1d-observe")
    if runtime is None or runtime.profile != profile or runtime.status != "running":
        raise RuntimeError(f"Start g1d-observe --profile {profile} first")
    report = {
        "profile": profile,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_duration_s": seconds,
        "motion_commands": 0,
        "samples": [],
    }
    started = time.monotonic()
    previous = None
    try:
        async with ForgeToolClient(runtime.gateway_url, timeout_s=3) as client:
            while True:
                contexts = [
                    await client.get_tool_context(name)
                    for name in ("g1d.state", "g1d.camera_snapshot")
                ]
                assert all(item["data"]["ready"] for item in contexts), "Endpoint lost readiness"
                response = await client.invoke_query_tool("g1d.state", {"max_age_ms": 500})
                state = response["data"]["response"]["result"]["outputs"]
                assert state["simulated"] is (profile == "mock")
                sequence = state["height_sequence"]
                assert previous is None or sequence > previous, "Feedback stopped advancing"
                previous = sequence
                elapsed = round(time.monotonic() - started, 2)
                report["samples"].append(
                    {
                        "elapsed_s": elapsed,
                        "height_sequence": sequence,
                        "height_age_ms": state["height_age_ms"],
                        "ready": True,
                    }
                )
                if len(report["samples"]) % 5 == 1:
                    print(
                        f"{profile}: {elapsed}s, state and three cameras ready, sequence={sequence}",
                        flush=True,
                    )
                if elapsed >= seconds:
                    break
                await asyncio.sleep(min(3, seconds - elapsed))
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["error"] = str(error)
        raise
    finally:
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        (ROOT / f"dist/{profile}-runtime-health.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("mock", "real"), required=True)
    parser.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("seconds must be between 1 and 3600")
    asyncio.run(check(args.profile, args.seconds))
