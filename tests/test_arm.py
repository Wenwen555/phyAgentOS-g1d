import asyncio
import math
from types import SimpleNamespace

import pytest
from forge_tool import ToolRequest
from pydantic import ValidationError

from paos_g1d.manipulation.arm import NAMES, MockArm, MoveRequest, tolerance
from paos_g1d.manipulation.arm_endpoint import ArmQuery, JointAction
from paos_g1d.manipulation.arm_recipe import recipe


def test_recipe():
    now = [0.0]
    arm = MockArm(lambda: now[0])
    arm.q["left_dex1"] = 0.6
    initial = arm.q.copy()
    results = []
    for step in recipe(initial):
        args = MoveRequest.model_validate(step["arguments"])
        arm.move(args)
        assert arm.duration == args.duration_s
        now[0] += arm.duration + 0.301
        state = arm.read()
        assert state.phase == "succeeded"
        assert all(
            abs(state.positions_rad[t.joint] - t.position_rad) <= tolerance(t.joint)
            for t in args.targets
        )
        assert state.positions_rad["right_dex1"] == initial["right_dex1"]
        results.append(state)
    assert results[2].positions_rad["left_wrist_roll"] == pytest.approx(math.radians(50))
    assert results[3].positions_rad["left_dex1"] == 5.4
    assert arm.q == initial


@pytest.mark.parametrize(
    "targets",
    [
        [{"joint": "waist_yaw", "position_rad": 0.0}],
        [{"joint": "left_elbow", "position_rad": float("nan")}],
        [{"joint": "left_dex1", "position_rad": 5.5}],
        [{"joint": "left_elbow", "position_rad": 0.0}] * 2,
    ],
)
def test_reject(targets):
    with pytest.raises(ValidationError):
        MoveRequest(targets=targets, duration_s=3.0, timeout_s=10.0)


async def test_action_and_cancel():
    now = [0.0]
    arm = MockArm(lambda: now[0])
    action = JointAction(arm, True)

    class Events:
        async def emit(self, event):
            pass

    args = recipe(arm.q)[1]["arguments"]
    context = SimpleNamespace(execution_key="one", deadline_ms=None)
    await action.start(SimpleNamespace(arguments=args), context, Events())
    await asyncio.sleep(0.03)
    with pytest.raises(Exception):
        await action.start(
            SimpleNamespace(arguments=args),
            SimpleNamespace(execution_key="two", deadline_ms=None),
            Events(),
        )
    now[0] = 1.0
    await action.cancel("one")
    await action.goals["one"].task
    assert action.goals["one"].result.status == "cancelled"
    q = arm.read().positions_rad
    now[0] = 10.0
    assert arm.read().positions_rad == q
    assert arm.read().reference_held


async def test_disabled_and_short_deadline():
    action = JointAction(MockArm(), False)
    args = recipe(action.arm.q)[1]["arguments"]
    with pytest.raises(Exception):
        await action.start(
            SimpleNamespace(arguments=args),
            SimpleNamespace(execution_key="one", deadline_ms=None),
            None,
        )
    action.enabled = True
    with pytest.raises(Exception):
        await action.start(
            SimpleNamespace(arguments=args),
            SimpleNamespace(execution_key="one", deadline_ms=0),
            None,
        )


def test_arbitrary_pose_and_speed_limited_duration():
    now = [0.0]
    arm = MockArm(lambda: now[0])
    original = arm.q.copy()
    # This pose is not one of the arm_sequence stages.
    args = MoveRequest(
        targets=[
            {"joint": "right_shoulder_pitch", "position_rad": -0.7},
            {"joint": "left_wrist_roll", "position_rad": -1.2},
        ],
        duration_s=0.5,
        timeout_s=10.0,
    )
    arm.move(args)
    assert arm.duration > args.duration_s
    now[0] = arm.duration + 0.301
    state = arm.read()
    assert state.phase == "succeeded"
    assert state.positions_rad["right_shoulder_pitch"] == -0.7
    assert state.positions_rad["left_wrist_roll"] == -1.2
    for name in original.keys() - {"right_shoulder_pitch", "left_wrist_roll"}:
        assert state.positions_rad[name] == original[name]


async def test_arm_state_query_projects_requested_joints():
    query = ArmQuery(MockArm(lambda: 0.0))

    full = await query.query(ToolRequest(arguments={"max_age_ms": 100}), None)
    assert set(full.outputs["positions_rad"]) == set(NAMES)
    assert set(full.outputs["references_rad"]) == set(NAMES)
    assert set(full.outputs["elbow_bend_deg"]) == {"left", "right"}

    narrow = await query.query(
        ToolRequest(arguments={"max_age_ms": 100, "joints": ["left_elbow"]}), None
    )
    assert set(narrow.outputs["positions_rad"]) == {"left_elbow"}
    assert set(narrow.outputs["references_rad"]) == {"left_elbow"}
    # Angle feedback is derived from the full pose first, then narrowed with its joint.
    assert set(narrow.outputs["elbow_bend_deg"]) == {"left"}
    assert set(narrow.outputs["forearm_world_elevation_deg"]) == {"left"}

    no_joints = await query.query(ToolRequest(arguments={"max_age_ms": 100, "joints": []}), None)
    assert no_joints.outputs["positions_rad"] == {}
    assert no_joints.outputs["elbow_bend_deg"] == {}
    assert no_joints.outputs["forearm_world_elevation_deg"] == {}
    assert no_joints.outputs["phase"]  # controller state still reported


async def test_arm_state_accepts_the_dex1_axes_the_grasp_workflow_reads():
    # The arm's own joint names include the internal fingers; validating them against
    # the waist/arm list would reject the axes a grasp workflow asks for.
    query = ArmQuery(MockArm(lambda: 0.0))
    state = await query.query(
        ToolRequest(arguments={"max_age_ms": 100, "joints": ["left_dex1", "right_dex1"]}), None
    )
    assert set(state.outputs["positions_rad"]) == {"left_dex1", "right_dex1"}
    assert set(state.outputs["references_rad"]) == {"left_dex1", "right_dex1"}


async def test_arm_state_and_state_agree_on_the_shared_axes():
    from paos_g1d.contracts import JOINTS

    shared = ["left_shoulder_pitch", "right_elbow", "right_wrist_yaw"]
    assert set(shared) <= set(JOINTS)
    for name in shared:
        await ArmQuery(MockArm(lambda: 0.0)).query(
            ToolRequest(arguments={"max_age_ms": 100, "joints": [name]}), None
        )


async def test_arm_state_rejects_a_repeated_joint():
    with pytest.raises(Exception):
        await ArmQuery(MockArm(lambda: 0.0)).query(
            ToolRequest(arguments={"max_age_ms": 100, "joints": ["left_elbow", "left_elbow"]}), None
        )
