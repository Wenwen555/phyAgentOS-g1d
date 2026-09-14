"""Forge Action lifecycle for the single persistent native arm controller."""

import asyncio
import math
import time
from collections import OrderedDict

from forge_tool import ToolAccepted, ToolError, ToolEvent, ToolResult

from ..endpoints import LiftAction, _Goal, parse, reject
from .arm import ArmStateRequest, MoveRequest, project_arm_state, speed, tolerance


class ArmQuery:
    def __init__(self, arm):
        self.arm = arm

    async def query(self, request, context):
        args = parse(ArmStateRequest, request)
        state = await asyncio.to_thread(self.arm.read)
        if not 0 <= state.age_s <= args.max_age_ms / 1000:
            return ToolResult(
                status="failed",
                error=ToolError(code="ARM_STALE", message="fresh arm feedback unavailable"),
            )
        return ToolResult(status="succeeded", outputs=project_arm_state(state, args.joints))


class JointAction(LiftAction):
    # Reuse only status/result/cancel; physical behavior belongs to this backend.
    def __init__(self, arm, enabled):
        self.arm = arm
        self.enabled = enabled
        self.goals = OrderedDict()
        self.active = None
        self.reservation = None
        self.faulted = False
        self.lock = asyncio.Lock()
        self.link_ok = lambda: True

    async def start(self, request, context, events):
        args = parse(MoveRequest, request)
        if not self.enabled:
            reject("ARM_DISABLED", "arm motion not enabled for this runtime")
        deadline = time.monotonic() + args.timeout_s
        if context.deadline_ms is not None:
            deadline = min(deadline, time.monotonic() + context.deadline_ms / 1000 - time.time())
        async with self.lock:
            if self.active is not None or self.reservation or self.faulted:
                reject("ARM_BUSY_OR_UNCERTAIN", "one arm owner; reconcile previous motion")
            key = context.execution_key
            if key in self.goals:
                reject("ARM_DUPLICATE", "execution key already retained")
            state = await asyncio.to_thread(self.arm.read)
            if not 0 <= state.age_s <= 0.1:
                reject("ARM_STALE", "fresh arm feedback required")
            requested_elbows = args.elbows
            try:
                args = args.resolve(state)
            except ValueError as exc:
                reject("ELBOW_FRAME", str(exc))
            effective = max(
                [args.duration_s]
                + [
                    1.875 * abs(t.position_rad - state.references_rad[t.joint]) / speed(t.joint)
                    for t in args.targets
                ]
            )
            if deadline - time.monotonic() < effective + 0.3:
                reject("ARM_DEADLINE", "deadline too short for speed-limited motion and settling")
            goal = _Goal()
            self.goals[key] = goal
            self.active = key
            while len(self.goals) > 64:
                self.goals.popitem(last=False)
            goal.task = asyncio.create_task(
                self._run_arm(goal, args, deadline, events, requested_elbows)
            )
        return ToolAccepted(
            details={
                "simulated": state.simulated,
                "effective_duration_s": effective,
                "resolved_targets": [t.model_dump() for t in args.targets],
            }
        )

    async def _run_arm(self, goal, args, deadline, events, requested_elbows=()):
        status = "failed"
        reason = "unknown"
        state = None
        try:
            if goal.cancel.is_set():
                status = "cancelled"
                reason = "cancelled_before_motion"
            else:
                await asyncio.to_thread(self.arm.move, args)
                goal.phase = "running"
                while True:
                    state = await asyncio.to_thread(self.arm.read)
                    if any(t.mode == "world" for t in requested_elbows) and (
                        state.torso_rpy_rad is None or not 0 <= state.torso_age_s <= 0.1
                    ):
                        await asyncio.to_thread(self.arm.cancel)
                        state = await asyncio.to_thread(self.arm.read)
                        status = (
                            "failed" if state.phase in ("cancelled", "succeeded") else "unknown"
                        )
                        reason = "world_orientation_stale_reference_frozen"
                        break
                    if goal.cancel.is_set() or time.monotonic() >= deadline or not self.link_ok():
                        await asyncio.to_thread(self.arm.cancel)
                        state = await asyncio.to_thread(self.arm.read)
                        status = "cancelled" if goal.cancel.is_set() else "failed"
                        reason = "reference_frozen"
                        if (
                            state.phase not in ("cancelled", "succeeded")
                            or not 0 <= state.age_s <= 0.1
                        ):
                            status = "unknown"
                        break
                    if state.phase in ("failed", "cancelled"):
                        status = "unknown" if state.phase == "failed" else "cancelled"
                        reason = state.phase
                        break
                    if state.phase == "succeeded":
                        reached = all(
                            abs(state.positions_rad[t.joint] - t.position_rad) <= tolerance(t.joint)
                            for t in args.targets
                        )
                        status = "succeeded" if reached and 0 <= state.age_s <= 0.1 else "unknown"
                        reason = (
                            "target_reached" if status == "succeeded" else "feedback_unconfirmed"
                        )
                        for target in requested_elbows:
                            angles = (
                                state.forearm_world_elevation_deg
                                if target.mode == "world"
                                else state.elbow_bend_deg
                            )
                            actual = angles.get(target.side)
                            if actual is None or abs(actual - target.angle_deg) > math.degrees(
                                tolerance(f"{target.side}_elbow")
                            ):
                                status = "failed"
                                reason = "elbow_frame_target_not_reached"
                        break
                    await asyncio.sleep(0.02)
        except Exception as error:
            reason = str(error)
            status = "unknown"
            try:
                await asyncio.to_thread(self.arm.cancel)
            except Exception:
                pass
        if state is None:
            try:
                state = await asyncio.to_thread(self.arm.read)
            except Exception:
                pass
        self.faulted = status == "unknown"
        if state is not None:
            state.requested_elbows = list(requested_elbows)
            state.resolved_targets = args.targets
        goal.result = ToolResult(
            status=status,
            outputs=state.model_dump() if state else {},
            error=ToolError(code="ARM_" + status.upper(), message=reason)
            if status in ("failed", "unknown")
            else None,
        )
        goal.phase = "completed" if status == "succeeded" else status
        self.active = None
        event = {
            "succeeded": "executor_completed",
            "failed": "executor_failed",
            "cancelled": "cancelled",
        }.get(status)
        if event:
            await events.emit(ToolEvent(type=event, data={"reason": reason}))
