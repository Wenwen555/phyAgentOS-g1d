#!/usr/bin/env python3
"""Offline robot test: validated local bundles -> real Dora/Gateway -> mock G1-D only."""

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "PhyAgentOS-core"))

from PhyAgentOS.forge.observation import ForgeObservationCollector  # noqa: E402
from PhyAgentOS.forge.tool_client import ForgeToolClient  # noqa: E402
from PhyAgentOS.skill_runtime.archive import sha256_file  # noqa: E402
from PhyAgentOS.skill_runtime.installer import (  # noqa: E402
    NodeInstaller,
    SkillEnvironmentBuilder,
    SkillInstaller,
)
from PhyAgentOS.skill_runtime.state import RuntimeStateStore  # noqa: E402


async def terminal(client, invocation):
    for _ in range(200):
        response = await client.invocation_result(invocation)
        result = response["data"].get("result")
        if result is not None:
            return result
        await client.invocation_status(invocation)
        await asyncio.sleep(0.05)
    raise RuntimeError("mock Action did not reconcile before test deadline")


async def check():
    async with ForgeToolClient("http://127.0.0.1:19002", timeout_s=1) as client:
        for _ in range(150):
            try:
                contexts = [
                    await client.get_tool_context(name)
                    for name in ("g1d.state", "g1d.camera_snapshot", "g1d.set_height")
                ]
                if all(context["data"]["ready"] for context in contexts):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)
        else:
            raise RuntimeError("mock Runtime did not become ready")
        state = await client.invoke_query_tool("g1d.state", {"max_age_ms": 500})
        assert state["data"]["response"]["result"]["outputs"]["simulated"] is True
        for source in ("head", "left_wrist", "right_wrist"):
            snapshot = await client.invoke_query_tool(
                "g1d.camera_snapshot", {"source": source, "max_age_ms": 1000}
            )
            output = snapshot["data"]["response"]["result"]["outputs"]
            assert output["simulated"] and Path(output["image_path"]).is_file()
        collector = ForgeObservationCollector(
            "http://127.0.0.1:19002",
            required_image_sources=["head", "left_wrist", "right_wrist"],
            max_artifact_bytes=8388608,
            require_state=True,
        )
        await collector.start()
        try:
            before = await collector.wait_for_before(5)
            admission = await client.invoke_action(
                "g1d.set_height",
                {"target_height_m": 0.5015, "tolerance_m": 0.001, "timeout_s": 4.0},
                timeout_ms=8000,
            )
            result = await terminal(client, admission["data"]["invocation_id"])
            assert result["status"] == "succeeded", result
            assert result["outputs"]["stopped_observed"] is True
            from datetime import datetime, timezone

            after = await collector.wait_for_after(
                before, terminal_observed_at=datetime.now(timezone.utc), timeout_s=5
            )
            assert all(
                after.images[source].sequence > before.images[source].sequence
                for source in before.images
            )
            admission = await client.invoke_action(
                "g1d.set_height",
                {"target_height_m": 0.8, "tolerance_m": 0.001, "timeout_s": 4.0},
                timeout_ms=8000,
            )
            await asyncio.sleep(0.15)
            await client.cancel_invocation(admission["data"]["invocation_id"])
            result = await terminal(client, admission["data"]["invocation_id"])
            assert result["status"] == "cancelled", result
            assert result["outputs"]["stopped_observed"] is True
            return {
                "profile": "mock",
                "tool_queries": 4,
                "height_action": "succeeded",
                "cancel": "cancelled",
                "evidence_sources": sorted(after.images),
                "state_evidence": after.state is not None,
            }
        finally:
            await collector.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-archive", required=True, type=Path)
    parser.add_argument("--dora", required=True, type=Path)
    parser.add_argument(
        "--arm", action="store_true", help="Replay parameterized arm tools in mock only"
    )
    parser.add_argument("--port", type=int, default=19002)
    args = parser.parse_args()
    if args.port != 19002 and not args.arm:
        parser.error("custom port currently supported only for arm replay")
    # Do not interfere with a user's existing Gateway on this port.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    version = subprocess.check_output([str(args.dora.resolve()), "--version"], text=True)
    if "dora-cli 0.4.1" not in version or "dora-message: 0.7.0" not in version:
        raise RuntimeError("Dora version does not match the PhyAgentOS compatibility baseline")
    with tempfile.TemporaryDirectory(prefix="paos-g1d-smoke-") as directory:
        test_root = Path(directory)
        store = RuntimeStateStore(test_root / "state")
        installer = SkillInstaller(test_root / "skills", state_store=store)
        archive = ROOT / (
            "dist/skills/g1d-robot-0.1.17.tar.gz" if args.arm else "dist/skills/g1d-lift-0.1.1.tar.gz"
        )
        manifest = installer.install(archive, expected_sha256=sha256_file(archive))
        nodes = NodeInstaller(test_root / "runtime", state_store=store)
        nodes.install(args.gateway_archive.resolve(), manifest.artifacts.nodes["gateway"])
        nodes.install(
            ROOT / "dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz", manifest.artifacts.nodes["g1d"]
        )
        binary_root = SkillEnvironmentBuilder(test_root / "runtime", state_store=store).prepare(
            manifest, "mock"
        )
        flow = binary_root.parent / "launch/profiles/mock/dataflow.yaml"
        if "--backend mock" not in flow.read_text() or "--backend unitree" in flow.read_text():
            raise RuntimeError("the smoke test must never select a hardware profile")
        if args.arm and args.port != 19002:
            # Test-only launch configuration; installed Bundle remains unchanged.
            import yaml

            gateway_path = flow.parent / "gateway.yaml"
            gateway = yaml.safe_load(gateway_path.read_text())
            gateway["port"] = args.port
            gateway_path.write_text(yaml.safe_dump(gateway, sort_keys=False))
        log_path = ROOT / "build/smoke-test.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [str(args.dora.resolve()), "run", str(flow)],
                cwd=flow.parent,
                env={**os.environ, "PAOS_G1D_SNAPSHOT_DIR": str(test_root / "snapshots")},
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                if args.arm:
                    from replay_arm_sequence import check as check_arm

                    report = asyncio.run(check_arm(f"http://127.0.0.1:{args.port}"))
                else:
                    report = asyncio.run(check())
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
        report["bundle_sha256"] = sha256_file(archive)
        report["gateway_sha256"] = manifest.artifacts.nodes["gateway"].sha256
        report["g1d_sha256"] = manifest.artifacts.nodes["g1d"].sha256
        (
            ROOT / ("dist/arm-replay-report.json" if args.arm else "dist/smoke-report.json")
        ).write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
