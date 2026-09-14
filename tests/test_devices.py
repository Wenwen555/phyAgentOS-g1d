import asyncio
import ctypes
import io
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from forge_tool import ToolContext, ToolEndpointError, ToolExecutionKey, ToolRequest
from PIL import Image
from pydantic import ValidationError

from paos_g1d.cameras import Cameras
from paos_g1d.contracts import JOINTS, SOURCES, HeightSettings
from paos_g1d.device import MockDevice, State
from paos_g1d.endpoints import LiftAction, SnapshotQuery, StateQuery

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        await asyncio.sleep(0)


class Events:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


def context(deadline=None):
    return ToolContext(
        execution_key=ToolExecutionKey("invocation-1", "attempt-1"),
        tool_id="g1d.set_height",
        implementation_id="unitree.g1d",
        endpoint_id="g1d.lift",
        operation="set_height",
        deadline_ms=deadline,
    )


def request(target=0.51, timeout=8.0):
    return ToolRequest(
        arguments={"target_height_m": target, "tolerance_m": 0.001, "timeout_s": timeout}
    )


def action(settings, device=None):
    clock = Clock()
    device = device or MockDevice(clock)
    return LiftAction(
        device, settings.height, clock=clock, wall_clock=clock, sleep=clock.sleep
    ), device


def test_confirmed_limits_required():
    with pytest.raises(ValidationError):
        HeightSettings(enabled=True)
    with pytest.raises(ValidationError):
        HeightSettings(max_command=float("nan"))


async def test_rejected_goal_never_commands_device(settings):
    lift, device = action(settings)
    for invalid in [
        request(1.1),
        request(timeout=30.0),
        ToolRequest(arguments={"target_height_m": True}),
    ]:
        with pytest.raises(ToolEndpointError):
            await lift.start(invalid, context(), Events())
    with pytest.raises(ToolEndpointError):
        await lift.start(request(), context(deadline=1), Events())
    lift.settings.enabled = False
    with pytest.raises(ToolEndpointError):
        await lift.start(request(), context(), Events())
    assert device.commands == []


async def test_bounded_goal_and_final_stop(settings):
    lift, device = action(settings)
    events = Events()
    ctx = context()
    await lift.start(request(), ctx, events)
    assert (await lift.result(ctx.execution_key)).status == "pending"
    await lift.goals[ctx.execution_key].task
    result = (await lift.result(ctx.execution_key)).result
    assert result.status == "succeeded"
    assert result.outputs["reached_goal"]
    assert result.outputs["stopped_observed"]
    assert abs(result.outputs["final_height_m"] - 0.51) <= 0.001
    assert max(abs(command) for command in device.commands) <= settings.height.max_command
    assert device.commands[-1] == 0
    assert events.events[-1].type == "executor_completed"


async def test_cancel_acceptance_precedes_stop_observation(settings):
    lift, device = action(settings)
    ctx = context()
    await lift.start(request(), ctx, Events())
    response = await lift.cancel(ctx.execution_key)
    assert response.status == "accepted"
    assert (await lift.result(ctx.execution_key)).status == "pending"
    await lift.goals[ctx.execution_key].task
    result = (await lift.result(ctx.execution_key)).result
    assert result.status == "cancelled"
    assert result.outputs["stopped_observed"]
    assert all(command == 0 for command in device.commands)
    assert (await lift.cancel(ctx.execution_key)).status == "terminal"


async def test_timeout_stops_and_does_not_claim_success(settings):
    lift, device = action(settings)
    ctx = context()
    await lift.start(request(0.8, timeout=0.15), ctx, Events())
    await lift.goals[ctx.execution_key].task
    result = (await lift.result(ctx.execution_key)).result
    assert result.status == "failed"
    assert result.outputs["reason"] == "execution_timeout"
    assert result.outputs["stopped_observed"]
    assert device.commands[-1] == 0


async def test_failed_stop_is_unknown_and_blocks_next_goal(settings):
    lift, device = action(settings)
    original = device.command
    device.command = lambda velocity: -1 if velocity == 0 else original(velocity)
    ctx = context()
    await lift.start(request(0.8, timeout=0.1), ctx, Events())
    await lift.goals[ctx.execution_key].task
    result = (await lift.result(ctx.execution_key)).result
    assert result.status == "unknown"
    assert not result.outputs["stopped_observed"]
    with pytest.raises(ToolEndpointError, match="UNCERTAIN"):
        await lift.start(request(), ctx, Events())


async def test_stale_feedback_after_admission_stops(settings):
    lift, device = action(settings)
    ctx = context()
    await lift.start(request(), ctx, Events())
    original = device.read
    device.read = lambda: replace(original(), height_age_s=1.0)
    await lift.goals[ctx.execution_key].task
    result = (await lift.result(ctx.execution_key)).result
    assert result.status == "unknown"
    assert "feedback_stale" in result.outputs["reason"]
    assert device.commands and all(command == 0 for command in device.commands)


async def test_frozen_feedback_cannot_confirm_stop(settings):
    lift, device = action(settings)
    ctx = context()
    await lift.start(request(0.5), ctx, Events())
    frozen = device.read()
    device.read = lambda: frozen
    await lift.cancel(ctx.execution_key)
    await lift.goals[ctx.execution_key].task
    assert (await lift.result(ctx.execution_key)).result.status == "unknown"


async def test_camera_stale_dark_corrupt_and_local_path(cameras):
    query = SnapshotQuery(cameras)
    req = ToolRequest(arguments={"source": "head", "max_age_ms": 1000})
    result = await query.query(req, None)
    path = Path(result.outputs["image_path"])
    assert path.is_absolute() and path.read_bytes() == cameras.frames["head"].jpeg
    previous = cameras.frames["head"]
    cameras.frames["head"] = replace(previous, received=previous.received - 5)
    assert (await query.query(req, None)).status == "failed"
    black = io.BytesIO()
    Image.new("RGB", (1280, 480)).save(black, format="JPEG")
    cameras.ingest("head", black.getvalue())
    assert (await query.query(req, None)).status == "failed"
    with pytest.raises(Exception):
        cameras.ingest("head", b"not-a-jpeg")


async def test_stale_state_query_does_not_expose_zero_as_measurement():
    device = MockDevice()
    current = device.read()
    device.read = lambda: replace(current, height=None, height_sequence=0, height_age_s=-1.0)
    result = await StateQuery(device).query(ToolRequest(arguments={"max_age_ms": 100}), None)
    assert result.status == "failed"
    assert not result.outputs


class PositionedDevice:
    """Device whose 16 joints carry distinguishable values, so projection is observable."""

    simulated = True

    def __init__(self):
        self.state = State(
            height=0.25,
            height_age_s=0.0,
            height_sequence=1,
            positions=tuple(index / 100 for index in range(len(JOINTS))),
            velocities=tuple(float(index) for index in range(len(JOINTS))),
            joint_age_s=0.0,
            joint_sequence=1,
        )

    def read(self):
        return self.state


async def test_state_query_projects_requested_joints():
    query = StateQuery(PositionedDevice())
    index = {name: position for position, name in enumerate(JOINTS)}

    full = await query.query(ToolRequest(arguments={"max_age_ms": 100}), None)
    assert full.outputs["joint_names"] == list(JOINTS)
    assert len(full.outputs["positions_rad"]) == len(JOINTS)

    narrow = await query.query(
        ToolRequest(arguments={"max_age_ms": 100, "joints": ["right_elbow", "waist_yaw"]}), None
    )
    assert narrow.outputs["joint_names"] == ["right_elbow", "waist_yaw"]
    assert narrow.outputs["positions_rad"] == [
        index["right_elbow"] / 100,
        index["waist_yaw"] / 100,
    ]
    assert narrow.outputs["velocities_rad_s"] == [
        float(index["right_elbow"]),
        float(index["waist_yaw"]),
    ]
    # Height is never projected away: the lift workflow reads it on every step.
    assert narrow.outputs["height_m"] == 0.25

    height_only = await query.query(ToolRequest(arguments={"max_age_ms": 100, "joints": []}), None)
    assert height_only.outputs["joint_names"] == []
    assert height_only.outputs["positions_rad"] == []
    assert height_only.outputs["velocities_rad_s"] == []


async def test_state_query_rejects_unknown_or_repeated_joints():
    query = StateQuery(PositionedDevice())
    for joints in (["left_wrist_roll", "not_a_joint"], ["waist_yaw", "waist_yaw"]):
        with pytest.raises(ToolEndpointError):
            await query.query(ToolRequest(arguments={"max_age_ms": 100, "joints": joints}), None)


def test_camera_drain_receives_without_decoding_and_poll_matches(tmp_path, settings):
    clock = Clock()
    config = yaml.safe_load((ROOT / "configs/cameras.yaml").read_text())
    cameras = Cameras(config, settings, tmp_path / "snapshots", simulated=True, clock=clock)

    payloads = cameras.drain()
    assert sorted(source for source, _ in payloads) == sorted(SOURCES)
    # Receiving is cheap and must not touch the frame cache: decoding is ingest_all's job.
    assert cameras.frames == {}

    changed = cameras.ingest_all(payloads)
    assert sorted(changed) == sorted(SOURCES)
    assert all(cameras.frames[source].jpeg == jpeg for source, jpeg in payloads)

    # Inside the mock rate limit nothing is received; past it the cached bytes come back,
    # so the simulated runtime never re-encodes a JPEG on the event loop.
    assert cameras.drain() == []
    clock.now += 0.2
    again = cameras.drain()
    assert [jpeg for _, jpeg in again] == [jpeg for _, jpeg in payloads]
    assert cameras.poll()  # the synchronous entry point still receives and decodes


def test_camera_ingest_all_drops_the_frame_it_could_not_decode(tmp_path, settings):
    clock = Clock()
    config = yaml.safe_load((ROOT / "configs/cameras.yaml").read_text())
    cameras = Cameras(config, settings, tmp_path / "snapshots", simulated=True, clock=clock)
    cameras.ingest_all(cameras.drain())
    assert "head" in cameras.frames

    assert cameras.ingest_all([("head", b"not-a-jpeg")]) == []
    assert "head" not in cameras.frames
    assert cameras.errors["head"]


def test_sdk_library_null_calls_have_no_initialization():
    path = Path(__file__).resolve().parents[1] / "build/native/libg1d_sdk.so"
    if not path.exists():
        pytest.skip("build the SDK bridge first")
    # Loading alone must neither initialize ChannelFactory nor send a motor command.
    native_dir = path.parent
    root = Path(__file__).resolve().parents[2] / "unitree_sdk2-main/thirdparty/lib/x86_64"
    ctypes.CDLL(str(root / "libddsc.so.0"), mode=ctypes.RTLD_GLOBAL)
    ctypes.CDLL(str(root / "libddscxx.so.0"), mode=ctypes.RTLD_GLOBAL)
    library = ctypes.CDLL(str(native_dir / path.name))
    library.g1d_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    assert library.g1d_read(None, None) == -1
