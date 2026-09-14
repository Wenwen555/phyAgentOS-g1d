import asyncio
import math
from types import SimpleNamespace

import pytest

from paos_g1d.manipulation.arm import MockArm, MoveRequest
from paos_g1d.manipulation.arm_endpoint import JointAction
from paos_g1d.manipulation.elbow_frames import resolve_angle, world_elevation_deg


def test_upper_arm_bend_is_independent_of_shoulder():
    state = MockArm().read()
    for pitch in (0, -0.5, -1):
        state.positions_rad["left_shoulder_pitch"] = pitch
        assert resolve_angle("left", "upper_arm", 90, state.positions_rad) == 0
        assert resolve_angle("left", "upper_arm", 0, state.positions_rad) == math.pi / 2


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("rpy", [(0, 0, 0), (0.12, -0.15, 0.8)])
def test_world_horizontal_in_3d(side, rpy):
    q = MockArm().read().positions_rad
    q[f"{side}_shoulder_pitch"] = -0.6
    q[f"{side}_shoulder_roll"] = 0.15 if side == "left" else -0.15
    q[f"{side}_shoulder_yaw"] = 0.2
    target = resolve_angle(side, "world", 0, q, rpy)
    assert 0.2 < target < 1.0
    q[f"{side}_elbow"] = target
    assert abs(world_elevation_deg(side, q, rpy)) < 1e-8
    # Rotating world heading does not change ground-relative elevation.
    assert abs(world_elevation_deg(side, q, (*rpy[:2], rpy[2] + 1.0))) < 1e-8


def test_known_neutral_forward_and_down_directions():
    q = MockArm().read().positions_rad
    for side in ("left", "right"):
        q[f"{side}_elbow"] = 0
        assert world_elevation_deg(side, q, (0, 0, 0)) == pytest.approx(0, abs=0.01)
        q[f"{side}_elbow"] = math.pi / 2
        assert world_elevation_deg(side, q, (0, 0, 0)) == pytest.approx(-90, abs=0.01)


def test_world_requires_imu_and_reachable_target():
    state = MockArm().read()
    request = MoveRequest(
        elbows=[{"side": "left", "mode": "world", "angle_deg": 0}], duration_s=3, timeout_s=20
    )
    state.torso_age_s = 0.2
    with pytest.raises(ValueError, match="IMU"):
        request.resolve(state)
    with pytest.raises(ValueError, match="envelope|unreachable"):
        resolve_angle("left", "world", 80, state.positions_rad, (0, 0, 0))
    with pytest.raises(ValueError, match="IMU"):
        resolve_angle("left", "world", 0, state.positions_rad)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"elbows": [{"side": "left", "mode": "world", "angle_deg": 100}]},
        {"elbows": [{"side": "left", "mode": "upper_arm", "angle_deg": 180}]},
        {"elbows": [{"side": "left", "mode": "world", "angle_deg": float("nan")}]},
        {
            "elbows": [{"side": "left", "mode": "upper_arm", "angle_deg": 90}],
            "targets": [{"joint": "left_elbow", "position_rad": 0}],
        },
        {
            "elbows": [{"side": "left", "mode": "world", "angle_deg": 0}],
            "targets": [{"joint": "left_shoulder_pitch", "position_rad": -0.5}],
        },
        {},
    ],
)
def test_reject_ambiguous_or_invalid_requests(kwargs):
    with pytest.raises(ValueError):
        MoveRequest(**kwargs, duration_s=3, timeout_s=20)


async def test_world_action_resolves_and_reports_angle():
    now = [0.0]
    arm = MockArm(lambda: now[0])
    arm.q["left_shoulder_pitch"] = -0.5
    original_right = arm.q["right_elbow"]
    action = JointAction(arm, True)

    class Events:
        async def emit(self, event):
            pass

    request = SimpleNamespace(
        arguments={
            "elbows": [{"side": "left", "mode": "world", "angle_deg": 0}],
            "duration_s": 3,
            "timeout_s": 20,
        }
    )
    await action.start(request, SimpleNamespace(execution_key="world", deadline_ms=None), Events())
    await asyncio.sleep(0.03)
    now[0] = 4
    await asyncio.wait_for(action.goals["world"].task, 1)
    result = action.goals["world"].result
    assert result.status == "succeeded"
    assert abs(result.outputs["forearm_world_elevation_deg"]["left"]) < 1e-8
    assert result.outputs["resolved_targets"][0]["position_rad"] > 0.4
    assert arm.q["right_elbow"] == original_right


async def test_unreachable_world_rejected_without_motion():
    arm = MockArm()
    action = JointAction(arm, True)
    request = SimpleNamespace(
        arguments={
            "elbows": [{"side": "left", "mode": "world", "angle_deg": 80}],
            "duration_s": 3,
            "timeout_s": 20,
        }
    )
    with pytest.raises(Exception, match="envelope|unreachable"):
        await action.start(request, SimpleNamespace(execution_key="bad", deadline_ms=None), None)
    assert arm.history == []
    assert action.active is None


async def test_world_frame_checked_after_encoder_success():
    now = [0.0]
    arm = MockArm(lambda: now[0])
    arm.q["left_shoulder_pitch"] = -0.5
    action = JointAction(arm, True)

    class Events:
        async def emit(self, event):
            pass

    request = SimpleNamespace(
        arguments={
            "elbows": [{"side": "left", "mode": "world", "angle_deg": 0}],
            "duration_s": 3,
            "timeout_s": 20,
        }
    )
    await action.start(request, SimpleNamespace(execution_key="drift", deadline_ms=None), Events())
    await asyncio.sleep(0.03)
    # Simulate an external shoulder displacement while elbow encoder reaches its target.
    arm.target["left_shoulder_pitch"] = 0.0
    now[0] = 4
    await asyncio.wait_for(action.goals["drift"].task, 1)
    assert action.goals["drift"].result.status == "failed"
    assert action.goals["drift"].result.error.message == "elbow_frame_target_not_reached"
