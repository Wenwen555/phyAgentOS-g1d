import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from forge_tool import ToolEndpointError
from pydantic import ValidationError

from paos_g1d.contracts import Dex1Settings
from paos_g1d.manipulation.arm import MockArm
from paos_g1d.manipulation.arm_endpoint import JointAction
from paos_g1d.manipulation.grasp_action import GraspAction, GraspRequest

# Short confirmed travel keeps the mock's real-time trajectories brief while still
# exercising the configured target, the rest position and the release return leg.
SHORT_TRAVEL = Dex1Settings(
    limits_confirmed=True,
    left_max_open_rad=2.0,
    right_max_open_rad=2.0,
    left_closed_rest_rad=0.0254,
    right_closed_rest_rad=0.0207,
)


class ObjectArm(MockArm):
    """Synthetic object blocks closing at 1 rad; reference still finishes at zero."""

    def __init__(self, side):
        super().__init__()
        self.side = side

    def read(self):
        state = super().read()
        joint = self.side + "_dex1"
        if self.history and self.target[joint] == 0:
            state.positions_rad[joint] = max(1.0, state.positions_rad[joint])
        return state


async def execute(action, args, key="one", wait=6):
    await action.start(
        SimpleNamespace(arguments=args),
        SimpleNamespace(execution_key=key, deadline_ms=None),
        SimpleNamespace(emit=AsyncMock()),
    )
    await asyncio.wait_for(action.goals[key].task, wait)
    return action.goals[key].result


@pytest.mark.parametrize("side", ["left", "right"])
async def test_direct_open_close_contact_without_cameras_or_verifier(side):
    arm = ObjectArm(side)
    joints = JointAction(arm, True)
    action = GraspAction(joints)
    initial = arm.read().positions_rad.copy()
    result = await execute(action, dict(side=side, operation="open", duration_s=0.5), "open")
    assert result.status == "succeeded"
    assert result.outputs["mechanical_outcome"] == "opened"
    assert len(arm.history) == 1  # Opening does not schedule a later close.
    assert arm.read().positions_rad[side + "_dex1"] == 5.4
    assert arm.read().reference_held
    # No camera, precheck report, model provider, or observation ID is available.
    result = await execute(action, dict(side=side, operation="close", duration_s=0.5), "close")
    assert result.status == "succeeded"
    assert result.outputs["mechanical_outcome"] == "contact_detected"
    assert result.outputs["blockage_rad"] == pytest.approx(1.0, abs=0.05)
    # The contact is held by re-commanding the fingers at the stall angle.
    assert result.outputs["reason"] == "contact_detected_reference_held"
    assert len(arm.history) == 3  # open, close, hold
    assert result.outputs["verification_status"] == "disabled"
    assert result.outputs["grasp_verified"] is False
    assert result.outputs["simulated"] is True
    assert result.outputs["arm_state"]["reference_held"]
    assert result.outputs["before_observation"] is None
    assert result.outputs["after_observation"] is None
    for step in arm.history:
        assert [t["joint"] for t in step["targets"]] == [side + "_dex1"]
    for joint, value in initial.items():
        if joint != side + "_dex1":
            assert arm.read().positions_rad[joint] == value
    assert joints.reservation is None
    assert not joints.faulted


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("opening", [0.0, 5.4])
async def test_direct_empty_close_and_already_closed_are_not_contact(side, opening):
    arm = MockArm()
    arm.q[side + "_dex1"] = opening
    result = await execute(
        GraspAction(JointAction(arm, True)),
        dict(side=side, operation="close", duration_s=0.5),
    )
    assert result.status == "succeeded"
    assert result.outputs["mechanical_outcome"] == "fully_closed"
    assert result.outputs["grasp_verified"] is False


@pytest.mark.parametrize("side", ["left", "right"])
async def test_release_opens_then_returns_to_the_closed_rest(side):
    arm = MockArm()
    joints = JointAction(arm, True)
    action = GraspAction(joints, SHORT_TRAVEL)
    result = await execute(
        action, dict(side=side, operation="release", duration_s=0.5, timeout_s=5.0), wait=8
    )
    assert result.status == "succeeded"
    assert result.outputs["mechanical_outcome"] == "fully_closed"
    assert result.outputs["blockage_rad"] is None
    # The open leg and the closing leg it schedules by itself: the gripper is never
    # left open after letting an object go.
    assert result.outputs["reason"] == "released_then_closed"
    assert [t["joint"] for step in arm.history for t in step["targets"]] == [side + "_dex1"] * 2
    assert arm.history[0]["targets"][0]["position_rad"] == SHORT_TRAVEL.max_open(side)
    assert arm.history[1]["targets"][0]["position_rad"] == 0.0
    # Back at the closed command; the real fingers rest a few hundredths above zero,
    # which is what closed_rest records and what the close-classification uses.
    assert arm.read().positions_rad[side + "_dex1"] == pytest.approx(0.0, abs=0.01)
    assert SHORT_TRAVEL.closed_rest(side) < SHORT_TRAVEL.max_open(side)
    assert arm.read().reference_held
    assert joints.reservation is None
    assert not joints.faulted


@pytest.mark.parametrize("side", ["left", "right"])
async def test_release_that_still_meets_the_object_reports_contact_again(side):
    arm = ObjectArm(side)
    result = await execute(
        GraspAction(JointAction(arm, True), SHORT_TRAVEL),
        dict(side=side, operation="release", duration_s=0.5, timeout_s=5.0),
        wait=8,
    )
    assert result.status == "succeeded"
    # The finger never cleared the object, so the closing leg stalls on it again and
    # that stall angle stays held: nothing was dropped by the command itself.
    assert result.outputs["mechanical_outcome"] == "contact_detected"
    assert result.outputs["blockage_rad"] == pytest.approx(1.0, abs=0.05)
    assert result.outputs["reason"] == "contact_detected_reference_held"


async def test_release_deadline_must_cover_both_legs():
    arm = MockArm()
    action = GraspAction(JointAction(arm, True), SHORT_TRAVEL)
    with pytest.raises(ToolEndpointError):
        await action.start(
            SimpleNamespace(
                arguments=dict(side="left", operation="release", duration_s=0.5, timeout_s=2.0)
            ),
            SimpleNamespace(execution_key="release", deadline_ms=None),
            SimpleNamespace(emit=AsyncMock()),
        )
    assert not arm.history
    assert action.joints.reservation is None


async def test_shared_owner_and_cancel_release_reservation(cameras):
    arm = MockArm()
    joints = JointAction(arm, True)
    action = GraspAction(joints)
    events = SimpleNamespace(emit=AsyncMock())
    await action.start(
        SimpleNamespace(arguments=dict(side="right", operation="open")),
        SimpleNamespace(execution_key="grasp", deadline_ms=None),
        events,
    )
    with pytest.raises(ToolEndpointError):
        await joints.start(
            SimpleNamespace(
                arguments=dict(
                    targets=[{"joint": "left_elbow", "position_rad": 0.0}],
                    duration_s=3.0,
                    timeout_s=10.0,
                )
            ),
            SimpleNamespace(execution_key="move", deadline_ms=None),
            events,
        )
    await action.cancel("grasp")
    await action.goals["grasp"].task
    assert action.goals["grasp"].result.status == "cancelled"
    assert joints.reservation is None
    assert arm.history == []



@pytest.mark.parametrize("interruption", ["cancel", "link", "stale"])
async def test_interruption_freezes_reference_without_opening_other_hand(cameras, interruption):
    arm = MockArm()
    joints = JointAction(arm, True)
    action = GraspAction(joints)
    await action.start(
        SimpleNamespace(arguments=dict(side="right", operation="open")),
        SimpleNamespace(execution_key="opening", deadline_ms=None),
        SimpleNamespace(emit=AsyncMock()),
    )
    await asyncio.sleep(0.06)
    assert arm.history
    if interruption == "cancel":
        await action.cancel("opening")
    elif interruption == "link":
        joints.link_ok = lambda: False
    else:
        original_read = arm.read

        def stale_read():
            state = original_read()
            state.age_s = 0.2
            return state

        arm.read = stale_read
    await asyncio.wait_for(action.goals["opening"].task, 2)
    status = action.goals["opening"].result.status
    assert status == {"cancel": "cancelled", "link": "failed", "stale": "unknown"}[interruption]
    frozen = arm.read().positions_rad.copy()
    await asyncio.sleep(0.03)
    assert arm.read().positions_rad == frozen
    assert frozen["left_dex1"] == 0
    assert joints.reservation is None
    assert joints.faulted is (interruption == "stale")



@pytest.mark.parametrize("condition", ["disabled", "stale", "link", "faulted"])
async def test_direct_close_still_requires_available_control(condition):
    arm = MockArm()
    joints = JointAction(arm, condition != "disabled")
    if condition == "stale":
        original_read = arm.read
        def stale_read():
            state = original_read()
            state.age_s = 0.2
            return state
        arm.read = stale_read
    if condition == "link":
        joints.link_ok = lambda: False
    joints.faulted = condition == "faulted"
    action = GraspAction(joints)
    with pytest.raises(ToolEndpointError):
        await action.start(
            SimpleNamespace(arguments=dict(side="left", operation="close")),
            SimpleNamespace(execution_key="close", deadline_ms=None),
            SimpleNamespace(emit=AsyncMock()),
        )
    assert not arm.history
    assert joints.reservation is None


@pytest.mark.parametrize("values", [
    dict(operation="open"),
    dict(side="both", operation="open"),
    dict(side="left", operation="close", duration_s=5.0, timeout_s=2.0),
])
def test_explicit_side_and_motion_deadline_still_required(values):
    with pytest.raises(ValidationError):
        GraspRequest(**values)
