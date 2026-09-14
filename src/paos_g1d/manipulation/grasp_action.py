"""Two-stage Dex1 open/close/release Action using the existing, exclusive arm controller.

The object must already be aligned with the fingers. This endpoint does not move
the arm towards objects. Closing watches the encoder: a finger held by an object
stalls well above the empty-close rest position, and that stall is reported as
contact. The signal is mechanical evidence, not proof of grasp success: the
workflow confirms the held object with the Agent's own vision when it asks for a
visual double check.

Angle handling: a detected contact is held at the measured stall angle until the
next command, so the confirmed grasp angle stays where the object stopped the
fingers. `release` opens to the configured travel and then closes again by itself,
so the gripper is never left open after letting an object go.
"""

import asyncio
import time
from collections import OrderedDict
from typing import Literal

from forge_tool import ToolAccepted, ToolError, ToolEvent, ToolResult
from pydantic import Field, model_validator

from ..contracts import Dex1Settings, StrictModel
from ..endpoints import LiftAction, _Goal, parse, reject
from .arm import ArmOutput, MoveRequest, speed, tolerance


class GraspRequest(StrictModel):
    side: Literal["left", "right"]
    operation: Literal["open", "close", "release"]
    duration_s: float = Field(default=1.5, ge=0.5, le=5)
    timeout_s: float = Field(default=10.0, ge=2, le=20)

    @model_validator(mode="after")
    def validate_stage(self):
        if self.timeout_s < self.duration_s + 1.0:
            raise ValueError("timeout must cover trajectory and feedback settling")
        return self


class GraspOutput(StrictModel):
    side: Literal["left", "right"]
    operation: Literal["open", "close", "release"]
    # release reports the verdict of its closing leg: fully_closed means the object
    # left the fingers and the gripper is back at the closed rest angle.
    mechanical_outcome: Literal["opened", "contact_detected", "fully_closed", "interrupted"]
    # Measured finger angle at a detected contact stall; null when no stall was
    # detected, and also reported when a detected contact could not be held.
    blockage_rad: float | None = None
    arm_state: ArmOutput | None
    before_observation: None = None
    after_observation: None = None
    elapsed_s: float
    simulated: bool
    grasp_verified: Literal[False] = False
    verification_status: Literal["disabled"] = "disabled"
    reason: str


GRASP_TOOLS = {
    "g1d.grasp_target": ("g1d.grasp", "execute", "action", GraspRequest, GraspOutput),
}


class GraspAction(LiftAction):
    def __init__(
        self, joint_action, dex1_settings=None, *, clock=time.monotonic, sleep=asyncio.sleep
    ):
        self.joints = joint_action
        self.arm = joint_action.arm
        self.dex1 = dex1_settings if dex1_settings is not None else Dex1Settings()
        self.clock, self.sleep = clock, sleep
        self.goals = OrderedDict()
        self.active = None

    def _target(self, args):
        # The open target is the operator-confirmed travel, not the vendor 5.4
        # mapping constant: an unreachable target stalls silently instead of failing.
        # release opens with the same travel and then closes again by itself.
        return 0.0 if args.operation == "close" else self.dex1.max_open(args.side)

    async def start(self, request, context, events):
        args = parse(GraspRequest, request)
        if not self.joints.enabled:
            reject("ARM_DISABLED", "arm motion not enabled for this runtime")
        seconds = args.timeout_s
        if context.deadline_ms is not None:
            seconds = min(seconds, context.deadline_ms / 1000 - time.time())
        async with self.joints.lock:
            if self.joints.active is not None or self.joints.reservation or self.joints.faulted:
                reject("ARM_BUSY_OR_UNCERTAIN", "another arm/gripper operation owns control")
            if context.execution_key in self.goals:
                reject("GRASP_DUPLICATE", "execution key already retained")
            state = await asyncio.to_thread(self.arm.read)
            if not 0 <= state.age_s <= 0.1 or not self.joints.link_ok():
                reject("GRASP_STATE_UNAVAILABLE", "fresh feedback and Gateway link required")
            joint = args.side + "_dex1"
            target = self._target(args)
            effective = max(
                args.duration_s, 1.875 * abs(state.references_rad[joint] - target) / speed(joint)
            )
            if args.operation == "release":
                # A release also has to travel back to the closed rest position and
                # settle there, so its deadline must cover both legs.
                effective += max(
                    1.0, 1.875 * abs(target - self.dex1.closed_rest(args.side)) / speed(joint)
                ) + 1.0
            if seconds < effective + 1.0:
                reject("GRASP_DEADLINE", "deadline too short for trajectory and feedback settling")
            key = context.execution_key
            goal = _Goal()
            self.goals[key] = goal
            self.active = key
            self.joints.reservation = key
            while len(self.goals) > 64:
                self.goals.popitem(last=False)
            goal.task = asyncio.create_task(
                self._run_grasp(goal, args, state.simulated, seconds, events)
            )
        return ToolAccepted(
            details={"simulated": state.simulated, "side": args.side, "operation": args.operation}
        )

    async def _run_grasp(self, goal, args, simulated, seconds, events):
        started = self.clock()
        deadline = started + seconds
        state = None
        outcome, status, reason = "interrupted", "failed", "execution_timeout"
        blockage = None
        moved = False
        try:
            joint = args.side + "_dex1"
            target = self._target(args)
            if goal.cancel.is_set():
                status, reason = "cancelled", "cancelled_before_motion"
            else:
                if not self.joints.link_ok() or self.clock() >= deadline:
                    raise RuntimeError("Gateway link or deadline unavailable before motion")
                moved = True  # admission may fail after partially establishing the controller
                await asyncio.to_thread(
                    self.arm.move,
                    MoveRequest(
                        targets=[{"joint": joint, "position_rad": target}],
                        duration_s=args.duration_s,
                        timeout_s=args.timeout_s,
                    ),
                )
                goal.phase = "running"
                anchor, stable_since = None, None
                stage = "close" if args.operation == "close" else "open"
                while self.clock() < deadline - 0.5:
                    state = await asyncio.to_thread(self.arm.read)
                    if goal.cancel.is_set():
                        status, reason = "cancelled", "reference_frozen_on_cancel"
                        break
                    if not self.joints.link_ok() or not 0 <= state.age_s <= 0.1:
                        reason = "feedback_or_gateway_unavailable"
                        break
                    if state.phase in ("failed", "cancelled"):
                        reason = "controller_" + state.phase
                        break
                    q = state.positions_rad[joint]
                    if stage == "open":
                        if state.phase == "succeeded" and abs(q - target) <= tolerance(joint):
                            if args.operation == "open":
                                outcome, status, reason = (
                                    "opened",
                                    "succeeded",
                                    "waiting_for_user_placement",
                                )
                                break
                            # release: the fingers are clear, so close them again and let
                            # the loop report the closing verdict. The gripper must never
                            # be left open after a release.
                            failure = await self._begin_release_close(joint, deadline, goal)
                            if failure is not None:
                                outcome, reason = "opened", failure
                                break
                            stage, anchor, stable_since = "close", None, None
                            continue
                    elif anchor is None or abs(q - anchor) > 0.03:
                        anchor, stable_since = q, self.clock()
                    elif (
                        self.clock() - stable_since >= 0.4
                        and state.references_rad[joint] - q <= 0.18
                    ):
                        # A finger stopped by an object settles well above the
                        # empty-close rest position; that stall is the contact signal.
                        # The one-sided proximity guard keeps the follow-up hold from
                        # pressing outwards while the close plan is still descending.
                        if q - self.dex1.closed_rest(args.side) > self.dex1.contact_margin_rad:
                            blockage, outcome = q, "contact_detected"
                            reason = (
                                "contact_detected_reference_held"
                                if await self._hold_at_contact(joint, q, deadline, goal)
                                else "contact_hold_unconfirmed"
                            )
                        else:
                            outcome = "fully_closed"
                            reason = (
                                "released_then_closed"
                                if args.operation == "release"
                                else "bounded_close_complete"
                            )
                        status = "succeeded"
                        break
                    await self.sleep(0.02)
        except Exception as error:
            reason = str(error)
        finally:
            try:
                if moved:
                    # Keep the native publisher and bounded Dex1 PD hold. Do not reopen
                    # or lower either arm on completion/cancel (could drop an object).
                    await asyncio.to_thread(self.arm.cancel)
                state = await asyncio.to_thread(self.arm.read)
                if moved and (
                    state.phase not in ("cancelled", "succeeded") or not 0 <= state.age_s <= 0.1
                ):
                    status, reason = "unknown", reason + ":reference_freeze_unconfirmed"
                    self.joints.faulted = True
            except Exception as error:
                status, reason = "unknown", reason + ":" + str(error)
                if state is None or not 0 <= state.age_s <= 0.1:
                    self.joints.faulted = True
            if goal.cancel.is_set() and status == "succeeded":
                status, reason = "cancelled", "late_cancel_reference_held"
            output = GraspOutput(
                side=args.side,
                operation=args.operation,
                mechanical_outcome=outcome,
                blockage_rad=blockage,
                arm_state=state,
                elapsed_s=self.clock() - started,
                simulated=simulated,
                reason=reason,
            )
            goal.result = ToolResult(
                status=status,
                outputs=output.model_dump(mode="json"),
                error=ToolError(code="GRASP_" + status.upper(), message=reason)
                if status in ("failed", "unknown")
                else None,
            )
            goal.phase = "completed" if status == "succeeded" else status
            self.active = None
            self.joints.reservation = None
            event = {
                "succeeded": "executor_completed",
                "failed": "executor_failed",
                "cancelled": "cancelled",
            }.get(status)
            if event:
                await events.emit(ToolEvent(type=event, data={"reason": reason}))

    async def _begin_release_close(self, joint, deadline, goal):
        """Start the closing leg of a release; returns None on success, else the reason.

        The open plan is still running, and a running plan rejects a new one, so the
        open is cancelled first: its reference keeps its value and the close starts
        from there. From this point the main loop applies the ordinary close
        detection, so a release that cannot clear the object reports contact again.
        """
        if self.clock() >= deadline - 1.5 or goal.cancel.is_set():
            return "release_close_skipped_deadline"
        try:
            await asyncio.to_thread(self.arm.cancel)
            if goal.cancel.is_set():
                return "release_close_skipped_cancel"
            await asyncio.to_thread(
                self.arm.move,
                MoveRequest(
                    targets=[{"joint": joint, "position_rad": 0.0}],
                    duration_s=1.0,
                    timeout_s=2.0,
                ),
            )
        except Exception as error:
            return "release_close_unavailable:" + str(error)
        return None

    async def _hold_at_contact(self, joint, angle, deadline, goal):
        """Re-command the fingers at the contact angle so the final cancel freezes the
        reference there instead of pressing on towards zero.

        A running plan rejects a new one, so the close is cancelled first: the
        reference keeps its value and the new plan starts from it. Returns False when
        the hold does not fit or stays unconfirmed, and the caller then reports the
        contact without a confirmed hold.
        """
        if self.clock() >= deadline - 1.0 or goal.cancel.is_set():
            return False
        await asyncio.to_thread(self.arm.cancel)
        if goal.cancel.is_set():
            return False
        await asyncio.to_thread(
            self.arm.move,
            MoveRequest(
                targets=[{"joint": joint, "position_rad": angle}],
                duration_s=0.5,
                timeout_s=1.0,
            ),
        )
        while self.clock() < deadline - 1.0:
            state = await asyncio.to_thread(self.arm.read)
            if state.phase == "succeeded":
                return True
            if state.phase in ("failed", "cancelled"):
                return False
            if goal.cancel.is_set() or not self.joints.link_ok():
                return False
            await self.sleep(0.02)
        return False
