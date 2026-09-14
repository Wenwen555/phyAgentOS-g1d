"""Bounded continuous capture Action plus the read-only latest-state Query.

The session keeps fresh native camera JPEGs on the shared filesystem and publishes
them through the state store; it never detects, localizes or moves anything. The
official session semantics are not dispatchable by the pinned Gateway 1.0.2
handler, so a bounded, cancellable Action is used instead and the Agent reads the
latest state with a separate Query.
"""

import asyncio
import time
from collections import OrderedDict

from forge_tool import (
    ToolAccepted,
    ToolControlResponse,
    ToolError,
    ToolEvent,
    ToolExecutionStatus,
    ToolResult,
    ToolResultResponse,
)

from ..endpoints import _Goal, parse, reject
from ..observe import ObservedImage
from .contracts import (
    LIMITATIONS,
    PerceptionSessionOutput,
    PerceptionSessionRequest,
    PerceptionStateRequest,
)


class PerceptionStateQuery:
    def __init__(self, store):
        self.store = store

    async def query(self, request, context):
        args = parse(PerceptionStateRequest, request)
        output = self.store.snapshot(args.max_age_ms / 1000)
        if output is None:
            return ToolResult(
                status="failed",
                error=ToolError(
                    code="PERCEPTION_STATE_STALE",
                    message="no fresh perception state; run g1d.perception_session first",
                ),
            )
        return ToolResult(status="succeeded", outputs=output.model_dump())


class PerceptionSession:
    """One bounded capture session; keeps the newest frames and never commands motion."""

    def __init__(
        self, cameras, store, *, clock=time.monotonic, wall_clock=time.time, sleep=asyncio.sleep
    ):
        self.cameras, self.store = cameras, store
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.goals = OrderedDict()
        self.active = None
        self.lock = asyncio.Lock()
        self.link_ok = lambda: True

    async def start(self, request, context, events):
        args = parse(PerceptionSessionRequest, request)
        duration = args.duration_s
        if context.deadline_ms is not None:
            duration = min(duration, context.deadline_ms / 1000 - self.wall_clock())
        if duration <= 0:
            reject("PERCEPTION_DEADLINE_EXPIRED", "the Gateway deadline has already expired")
        async with self.lock:
            if self.active is not None:
                reject("PERCEPTION_BUSY", "one session owner; stop the previous one first")
            key = context.execution_key
            if key in self.goals:
                reject("PERCEPTION_DUPLICATE", "execution key already retained")
            # Refuse admission when the requested streams are already unusable, so the
            # caller learns immediately instead of receiving a session that cannot run.
            try:
                self._frames(args.sources, args.max_age_ms / 1000)
            except (ValueError, OSError) as error:
                reject("PERCEPTION_IMAGE_UNAVAILABLE", str(error))
            goal = _Goal()
            self.goals[key] = goal
            self.active = key
            while len(self.goals) > 64:
                self.goals.popitem(last=False)
            goal.task = asyncio.create_task(self._run(goal, args, duration, events))
        return ToolAccepted(details={"simulated": self.cameras.simulated, "sources": args.sources})

    def _frames(self, sources, max_age_s):
        """Pin one fresh frame per source without polling the streams.

        Node tick() owns cameras.poll(); this raises instead of fetching so a stalled
        camera fails the session rather than silently reusing an old frame.
        """
        return {source: self.cameras.frame(source, max_age_s) for source in sources}

    async def _run(self, goal, args, duration, events):
        started = self.clock()
        deadline = started + duration
        reason, outcome = "execution_timeout", "failed"
        frames_captured = dict.fromkeys(args.sources, 0)
        last_sequences = dict.fromkeys(args.sources, 0)
        last_progress = None
        max_age_s = args.max_age_ms / 1000
        goal.phase = "running"
        try:
            while True:
                if goal.cancel.is_set():
                    reason, outcome = "stop_requested", "cancelled"
                    break
                if self.clock() >= deadline:
                    reason, outcome = "duration_elapsed", "succeeded"
                    break
                if not self.link_ok():
                    reason = "gateway_lease_path_lost"
                    break
                try:
                    frames = self._frames(args.sources, max_age_s)
                except (ValueError, OSError) as error:
                    reason = f"camera_unavailable:{error}"
                    break
                observed = []
                for source, frame in frames.items():
                    observed.append(
                        ObservedImage(
                            **self.cameras.snapshot_frame(source, frame),
                            received_monotonic_s=frame.received,
                        )
                    )
                    if frame.sequence != last_sequences[source]:
                        frames_captured[source] += 1
                        last_sequences[source] = frame.sequence
                self.store.update(observed, active=True, simulated=self.cameras.simulated)
                if last_progress is None or self.clock() - last_progress >= 1.0:
                    last_progress = self.clock()
                    await events.emit(
                        ToolEvent(
                            type="progress",
                            data={
                                "elapsed_s": self.clock() - started,
                                "frames_captured": dict(frames_captured),
                                "last_sequences": dict(last_sequences),
                            },
                        )
                    )
                await self.sleep(args.interval_s)
        except Exception:
            reason, outcome = "capture_exception", "unknown"
        finally:
            self.store.mark_inactive()
            output = PerceptionSessionOutput(
                sources=args.sources,
                duration_s=duration,
                elapsed_s=self.clock() - started,
                frames_captured=frames_captured,
                last_sequences=last_sequences,
                reason=reason,
                simulated=self.cameras.simulated,
                limitations=list(LIMITATIONS),
            )
            error = (
                ToolError(code="PERCEPTION_" + outcome.upper(), message=reason)
                if outcome in ("failed", "unknown")
                else None
            )
            goal.result = ToolResult(status=outcome, outputs=output.model_dump(), error=error)
            goal.phase = "completed" if outcome == "succeeded" else outcome
            # Read-only capture leaves no unresolved physical state, so a failed session
            # is simply terminal: the next start re-checks freshness at admission.
            self.active = None
            event_type = {
                "succeeded": "executor_completed",
                "failed": "executor_failed",
                "cancelled": "cancelled",
            }.get(outcome)
            if event_type:
                await events.emit(ToolEvent(type=event_type, data={"reason": reason}))

    async def cancel(self, key, reason=None):
        goal = self.goals.get(key)
        if goal is None:
            return ToolControlResponse(
                command="cancel",
                status="rejected",
                error=ToolError(code="PERCEPTION_NOT_FOUND", message="session not retained"),
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
                    code="PERCEPTION_NOT_FOUND",
                    message="session was not retained by this device process",
                ),
            )
        return ToolExecutionStatus(phase=goal.phase, error=goal.result.error if goal.result else None)

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
