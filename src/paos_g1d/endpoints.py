"""G1-D business operations implementing upstream QueryToolEndpoint/ActionToolEndpoint."""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from forge_tool import (
    ToolAccepted,
    ToolControlResponse,
    ToolEndpointError,
    ToolError,
    ToolEvent,
    ToolExecutionStatus,
    ToolResult,
    ToolResultResponse,
)
from pydantic import ValidationError

from .contracts import (
    JOINTS,
    HeightOutput,
    HeightRequest,
    SnapshotRequest,
    StateOutput,
    StateRequest,
)


def reject(code, message):
    raise ToolEndpointError(ToolError(code=code, message=message))


def parse(model, request):
    try:
        return model.model_validate(dict(request.arguments))
    except ValidationError as error:
        reject("G1D_INVALID_ARGUMENTS", str(error))


def fresh_height(state, maximum_age):
    return (
        state.height is not None
        and state.height_sequence > 0
        and 0 <= state.height_age_s <= maximum_age
    )


class StateQuery:
    def __init__(self, device):
        self.device = device

    async def query(self, request, context):
        args = parse(StateRequest, request)
        state = await asyncio.to_thread(self.device.read)
        if not fresh_height(state, args.max_age_ms / 1000) or not (
            state.joint_sequence > 0 and 0 <= state.joint_age_s <= args.max_age_ms / 1000
        ):
            return ToolResult(
                status="failed",
                error=ToolError(
                    code="G1D_STATE_STALE", message="fresh height and joint feedback are required"
                ),
            )
        # Freshness is always checked over the full pose; only the returned detail is
        # projected, so a narrow request can never hide a stale joint stream.
        selected = (
            range(len(JOINTS)) if args.joints is None else [JOINTS.index(n) for n in args.joints]
        )
        output = StateOutput(
            height_m=state.height,
            height_sequence=state.height_sequence,
            height_age_ms=state.height_age_s * 1000,
            joint_names=[JOINTS[i] for i in selected],
            positions_rad=[state.positions[i] for i in selected],
            velocities_rad_s=[state.velocities[i] for i in selected],
            joint_sequence=state.joint_sequence,
            joint_age_ms=state.joint_age_s * 1000,
            mode_machine=state.mode_machine,
            simulated=self.device.simulated,
        )
        return ToolResult(status="succeeded", outputs=output.model_dump())


class SnapshotQuery:
    def __init__(self, cameras):
        self.cameras = cameras

    async def query(self, request, context):
        args = parse(SnapshotRequest, request)
        try:
            output = self.cameras.snapshot(args.source, args.max_age_ms / 1000)
        except (ValueError, OSError) as error:
            return ToolResult(
                status="failed", error=ToolError(code="G1D_IMAGE_UNAVAILABLE", message=str(error))
            )
        return ToolResult(status="succeeded", outputs=output)


@dataclass
class _Goal:
    phase: str = "accepted"
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    result: ToolResult | None = None


class LiftAction:
    """One bounded height goal; Gateway and forge_tool retain execution identity and protocol."""

    def __init__(
        self, device, settings, *, clock=time.monotonic, wall_clock=time.time, sleep=asyncio.sleep
    ):
        self.device, self.settings = device, settings
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.goals = OrderedDict()
        self.active = None
        self.faulted = False
        self.lock = asyncio.Lock()
        self.link_ok = lambda: True

    async def start(self, request, context, events):
        args = parse(HeightRequest, request)
        cfg = self.settings
        if not cfg.enabled:
            reject("G1D_CONTROL_DISABLED", "height control is disabled in this profile")
        if not cfg.min_height_m <= args.target_height_m <= cfg.max_height_m:
            reject(
                "G1D_HEIGHT_OUT_OF_RANGE",
                "target is outside the confirmed column coordinate limits",
            )
        if args.timeout_s > cfg.max_duration_s:
            reject("G1D_DEADLINE_LIMIT", "requested duration exceeds the device limit")
        duration = args.timeout_s
        if context.deadline_ms is not None:
            duration = min(duration, context.deadline_ms / 1000 - self.wall_clock())
        if duration <= 0:
            reject("G1D_DEADLINE_EXPIRED", "the Gateway deadline has already expired")
        async with self.lock:
            if self.active is not None or self.faulted:
                reject("G1D_BUSY_OR_UNCERTAIN", "active goal or unresolved physical stop state")
            initial = await asyncio.to_thread(self.device.read)
            if not fresh_height(initial, cfg.feedback_timeout_s) or initial.watchdog_tripped:
                reject(
                    "G1D_STATE_STALE", "fresh feedback and a healthy command watchdog are required"
                )
            if not cfg.min_height_m <= initial.height <= cfg.max_height_m:
                reject(
                    "G1D_HEIGHT_OUT_OF_RANGE", "current feedback is outside the confirmed limits"
                )
            key = context.execution_key
            if key in self.goals:
                reject("G1D_DUPLICATE_GOAL", "an execution key cannot start a second physical goal")
            goal = _Goal()
            self.goals[key] = goal
            self.active = key
            while len(self.goals) > 64:
                self.goals.popitem(last=False)
            goal.task = asyncio.create_task(self._run(key, goal, args, duration, events))
        return ToolAccepted(details={"simulated": self.device.simulated})

    async def _run(self, key, goal, args, duration, events):
        started = self.clock()
        deadline = started + duration
        reason, outcome = "execution_timeout", "failed"
        settled_since, sequence = None, None
        final_height = None
        goal.phase = "running"
        try:
            while True:
                if goal.cancel.is_set():
                    reason, outcome = "cancel_requested", "cancelled"
                    break
                if self.clock() >= deadline:
                    break
                if not self.link_ok():
                    reason = "gateway_lease_path_lost"
                    break
                state = await asyncio.to_thread(self.device.read)
                if not fresh_height(state, self.settings.feedback_timeout_s):
                    reason = "feedback_stale"
                    break
                final_height = state.height
                if state.watchdog_tripped:
                    reason = "command_watchdog_tripped"
                    break
                if not self.settings.min_height_m <= final_height <= self.settings.max_height_m:
                    reason = "feedback_outside_limits"
                    break
                error = args.target_height_m - final_height
                velocity = max(
                    -self.settings.max_command,
                    min(self.settings.max_command, self.settings.kp * error),
                )
                if abs(error) <= args.tolerance_m:
                    velocity = 0.0
                    if state.height_sequence != sequence:
                        if settled_since is None:
                            settled_since = self.clock()
                        elif self.clock() - settled_since >= self.settings.settle_s:
                            reason, outcome = "target_reached", "succeeded"
                            break
                else:
                    settled_since = None
                sequence = state.height_sequence
                # Recheck after state I/O: never emit a new nonzero command after a received cancel.
                if goal.cancel.is_set() or self.clock() >= deadline:
                    continue
                if await asyncio.to_thread(self.device.command, velocity) != 0:
                    reason = "command_failed"
                    break
                await self.sleep(0.05)
        except Exception:
            reason = "device_exception"
        finally:
            goal.phase = "stopping"
            stop_accepted, stopped, observed_height = await self._stop_and_observe()
            if observed_height is not None:
                final_height = observed_height
            if not stop_accepted or not stopped:
                outcome = "unknown"
                reason += ":stop_unconfirmed"
                self.faulted = True
            elif (
                outcome == "succeeded"
                and abs(final_height - args.target_height_m) > args.tolerance_m
            ):
                outcome, reason = "failed", "final_height_outside_tolerance"
            # A late cancellation still controls the result until execution is terminal.
            if goal.cancel.is_set() and outcome == "succeeded":
                outcome, reason = "cancelled", "cancel_requested"
            output = HeightOutput(
                target_height_m=args.target_height_m,
                final_height_m=final_height,
                tolerance_m=args.tolerance_m,
                reached_goal=outcome == "succeeded",
                stop_command_accepted=stop_accepted,
                stopped_observed=stopped,
                elapsed_s=self.clock() - started,
                reason=reason,
                simulated=self.device.simulated,
            )
            error = (
                ToolError(code="G1D_HEIGHT_" + outcome.upper(), message=reason)
                if outcome in ("failed", "unknown")
                else None
            )
            goal.result = ToolResult(status=outcome, outputs=output.model_dump(), error=error)
            goal.phase = "completed" if outcome == "succeeded" else outcome
            self.active = None
            event_type = {
                "succeeded": "executor_completed",
                "failed": "executor_failed",
                "cancelled": "cancelled",
            }.get(outcome)
            if event_type:
                # Result is established before the upstream handler checks the terminal event.
                await events.emit(ToolEvent(type=event_type, data={"reason": reason}))

    async def _stop_and_observe(self):
        accepted, observed, final = False, False, None
        deadline = self.clock() + self.settings.stop_timeout_s
        anchor, stable_since, sequence = None, None, None
        while self.clock() < deadline:
            try:
                if not accepted:
                    accepted = await asyncio.to_thread(self.device.command, 0.0) == 0
                state = await asyncio.to_thread(self.device.read)
                if not fresh_height(state, self.settings.feedback_timeout_s):
                    anchor, stable_since = None, None
                elif accepted and state.height_sequence != sequence:
                    final = state.height
                    if anchor is None or abs(final - anchor) > self.settings.stop_tolerance_m:
                        anchor, stable_since = final, self.clock()
                    elif self.clock() - stable_since >= self.settings.settle_s:
                        observed = True
                        break
                    sequence = state.height_sequence
            except Exception:
                anchor, stable_since = None, None
            await self.sleep(0.05)
        return accepted, observed, final

    async def cancel(self, key, reason=None):
        goal = self.goals.get(key)
        if goal is None:
            return ToolControlResponse(
                command="cancel",
                status="rejected",
                error=ToolError(code="G1D_NOT_FOUND", message="execution is not retained"),
            )
        if goal.result is not None:
            return ToolControlResponse(command="cancel", status="terminal")
        goal.cancel.set()
        return ToolControlResponse(command="cancel", status="accepted")

    async def status(self, key):
        goal = self.goals.get(key)
        if goal is None:
            return ToolExecutionStatus(
                phase="unknown",
                error=ToolError(
                    code="G1D_NOT_FOUND",
                    message="execution was not retained by this device process",
                ),
            )
        return ToolExecutionStatus(
            phase=goal.phase, error=goal.result.error if goal.result else None
        )

    async def result(self, key):
        goal = self.goals.get(key)
        if goal is None:
            return ToolResultResponse(status="not_found")
        return (
            ToolResultResponse(status="available", result=goal.result)
            if goal.result
            else ToolResultResponse(status="pending")
        )

    async def close(self):
        pending = [goal for goal in self.goals.values() if goal.task and not goal.task.done()]
        for goal in pending:
            goal.cancel.set()
        await asyncio.gather(*(goal.task for goal in pending), return_exceptions=True)
