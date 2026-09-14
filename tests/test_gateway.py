"""Core HTTP client -> actual upstream Gateway -> upstream Arrow binding -> mock device."""

import asyncio
import importlib.util
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from forge_gateway.adapters.dora_adapter import handle_dora_input
from forge_gateway.adapters.tool_dora import handle_tool_input
from forge_gateway.config import GatewayConfig
from forge_gateway.controllers.tool_controller import register_tool_routes
from forge_gateway.services.runtime_service import GatewayRuntime
from forge_tool.dora import tool_envelope_to_message
from PhyAgentOS.forge.observation import ForgeObservationCollector
from PhyAgentOS.forge.tool_client import ForgeToolClient

from paos_g1d.device import MockDevice
from paos_g1d.node import Endpoints

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("g1d_build", ROOT / "scripts/build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
async def bridge(cameras, settings, request):
    allow_arm = getattr(request, "param", False)
    runtime = GatewayRuntime(
        GatewayConfig.from_dict(builder.gateway_config(True, settings, mock=True, arm=allow_arm))
    )
    app = FastAPI()
    register_tool_routes(app, runtime)

    def publish(output, value):
        if output.endswith("_tool_out"):
            handle_tool_input(runtime, output.removesuffix("_out") + "_in", value)
        else:
            handle_dora_input(runtime, "proprio_state" if output == "state" else output, value)

    device = MockDevice()
    node = Endpoints(device, cameras, settings, publish, allow_height=True, allow_arm=allow_arm)

    async def pump():
        while True:
            await node.tick()
            for _ in range(64):
                message = runtime.tool_gateway.take_outbound()
                if message is None:
                    break
                await node.handle(
                    message.output_id.removesuffix("_out") + "_in",
                    tool_envelope_to_message(message.envelope).to_arrow(),
                )
            runtime.tool_gateway.sweep(now=time.monotonic())
            await asyncio.sleep(0.01)

    task = asyncio.create_task(pump())
    await asyncio.sleep(0.1)
    async with ForgeToolClient("http://g1d-test", transport=httpx.ASGITransport(app=app)) as client:
        try:
            yield client, node, runtime
        finally:
            await node.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            runtime.close()


async def terminal(client, invocation):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = await client.invocation_result(invocation)
        if result["data"].get("result") is not None:
            return result["data"]["result"]
        await client.invocation_status(invocation)
        await asyncio.sleep(0.02)
    raise AssertionError("mock invocation did not reconcile")


async def test_core_query_action_cancel_and_image_evidence(bridge):
    client, node, runtime = bridge
    context = await client.get_tool_context("g1d.state")
    assert context["data"]["endpoint_status"]["state"] == "ready"
    state = await client.invoke_query_tool("g1d.state", {"max_age_ms": 1000})
    assert state["data"]["response"]["result"]["outputs"]["simulated"]
    snapshot = await client.invoke_query_tool(
        "g1d.camera_snapshot", {"source": "head", "max_age_ms": 1000}
    )
    assert Path(snapshot["data"]["response"]["result"]["outputs"]["image_path"]).is_file()

    admitted = await client.invoke_action(
        "g1d.set_height", {"target_height_m": 0.5, "tolerance_m": 0.001, "timeout_s": 2.0}
    )
    result = await terminal(client, admitted["data"]["invocation_id"])
    assert result["status"] == "succeeded"
    assert result["outputs"]["stopped_observed"]

    admitted = await client.invoke_action(
        "g1d.set_height", {"target_height_m": 0.8, "tolerance_m": 0.001, "timeout_s": 4.0}
    )
    await asyncio.sleep(0.1)
    await client.cancel_invocation(admitted["data"]["invocation_id"])
    result = await terminal(client, admitted["data"]["invocation_id"])
    assert result["status"] == "cancelled"
    assert node.device.commands[-1] == 0

    collector = ForgeObservationCollector(
        "http://unused",
        required_image_sources=["head", "left_wrist", "right_wrist"],
        max_artifact_bytes=8388608,
    )
    with runtime.lock:
        frames = list(runtime.images.values())
    for frame in frames:
        await collector._handle_image_message(json.dumps(frame))
    evidence = await collector.wait_for_before(0.1)
    assert set(evidence.images) == {"head", "left_wrist", "right_wrist"}


async def test_observe_through_core_gateway_and_node(bridge):
    client, node, runtime = bridge
    context = await client.get_tool_context("g1d.observe")
    assert context["data"]["ready"] is True
    reply = await client.invoke_query_tool(
        "g1d.observe",
        {
            "sources": ["head", "left_wrist"],
            "max_age_ms": 500,
            "max_skew_ms": 100,
        },
    )
    result = reply["data"]["response"]["result"]
    assert result["status"] == "succeeded"
    assert result["outputs"]["association"] == "best_effort_receive_time"
    assert result["outputs"]["robot_state"]["reference_held"] is False
    assert len(result["outputs"]["images"]) == 2
    assert node.device.commands == []
    assert node.arm.history == []


@pytest.mark.parametrize("bridge", [True], indirect=True)
@pytest.mark.parametrize("side", ["left", "right"])
async def test_grasp_through_core_gateway_node_without_verifier_or_images(bridge, side):
    client, node, runtime = bridge
    context = await client.get_tool_context("g1d.grasp_target")
    assert context["data"]["ready"]
    opened = await client.invoke_action(
        "g1d.grasp_target",
        {
            "side": side,
            "operation": "open",
            "duration_s": 0.5,
            "timeout_s": 5.0,
        },
    )
    assert (await terminal(client, opened["data"]["invocation_id"]))["status"] == "succeeded"
    # No images are available, and a close still completes via the full Tool API.
    node.cameras.frame = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("camera offline"))
    closed = await client.invoke_action(
        "g1d.grasp_target",
        {
            "side": side,
            "operation": "close",
            "duration_s": 0.5,
            "timeout_s": 5.0,
        },
    )
    result = await terminal(client, closed["data"]["invocation_id"])
    assert result["status"] == "succeeded", result
    assert result["outputs"]["verification_status"] == "disabled"
    assert result["outputs"]["grasp_verified"] is False
    assert result["outputs"]["after_observation"] is None
    assert result["outputs"]["mechanical_outcome"] == "fully_closed"
    assert all(t["joint"] == side + "_dex1" for step in node.arm.history for t in step["targets"])
