#!/usr/bin/env python3
"""Call only g1d.observe through the Forge Gateway; no Agent/model or robot Action."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "PhyAgentOS-core"))
from PhyAgentOS.forge.tool_client import ForgeToolClient  # noqa: E402


async def run(args):
    async with ForgeToolClient(args.gateway) as client:
        response = await client.invoke_query_tool(
            "g1d.observe",
            {
                "sources": args.sources,
                "max_age_ms": args.max_age_ms,
                "max_skew_ms": args.max_skew_ms,
            },
        )
    result = response["data"]["response"]["result"]
    if result["status"] == "succeeded" and result["outputs"]["simulated"] != (
        args.profile == "mock"
    ):
        raise RuntimeError("Observation profile differs from requested profile")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default="http://127.0.0.1:19002")
    parser.add_argument("--profile", choices=["real", "mock"], default="real")
    parser.add_argument(
        "--sources", nargs="+", choices=["head", "left_wrist", "right_wrist"], default=["head"]
    )
    parser.add_argument("--max-age-ms", type=int, default=500)
    parser.add_argument("--max-skew-ms", type=int, default=100)
    asyncio.run(run(parser.parse_args()))
