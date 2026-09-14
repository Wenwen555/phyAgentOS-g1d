"""Read-only observation bundles: immutable images + best-effort associated arm state."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Literal

from forge_tool import ToolError, ToolResult
from pydantic import Field, model_validator

from .contracts import SnapshotOutput, StrictModel
from .endpoints import parse
from .manipulation.arm import ArmOutput


class ObserveRequest(StrictModel):
    sources: list[Literal["head", "left_wrist", "right_wrist"]] = Field(
        default_factory=lambda: ["head"], min_length=1, max_length=3
    )
    max_age_ms: int = Field(default=500, ge=1, le=1000)
    max_skew_ms: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("duplicate camera sources")
        return self


class ObservedImage(SnapshotOutput):
    received_monotonic_s: float = Field(ge=0)


class ObserveOutput(StrictModel):
    observation_id: str
    manifest_path: str
    collected_at_utc: str
    collected_monotonic_s: float = Field(ge=0)
    images: list[ObservedImage]
    robot_state: ArmOutput
    state_received_monotonic_s_estimate: float = Field(ge=0)
    state_read_window_ms: float = Field(ge=0)
    receive_skew_upper_bound_ms: float = Field(ge=0)
    association: Literal["best_effort_receive_time"] = "best_effort_receive_time"
    spatial_calibration: Literal["not_provided"] = "not_provided"
    simulated: bool
    limitations: list[str]


OBSERVE_TOOLS = {
    "g1d.observe": ("g1d.observe", "get", "query", ObserveRequest, ObserveOutput),
}


class ObserveQuery:
    def __init__(self, cameras, arm, *, clock=time.monotonic):
        self.cameras, self.arm, self.clock = cameras, arm, clock

    async def query(self, request, context):
        args = parse(ObserveRequest, request)
        try:
            # Pin immutable frames before yielding to the state read; never re-fetch them.
            frames = {s: self.cameras.frame(s, args.max_age_ms / 1000) for s in args.sources}
            before = self.clock()
            state = await asyncio.to_thread(self.arm.read)
            after = self.clock()
            if not 0 <= state.age_s <= min(0.1, args.max_age_ms / 1000):
                raise ValueError("fresh joint feedback required (at most 100 ms)")
            if state.simulated != self.cameras.simulated:
                raise ValueError("camera and joint feedback simulation identities differ")
            # Native read samples age within this call. Bound its receive timestamp;
            # do not pretend this is a sensor exposure timestamp or hardware synchronization.
            state_low, state_high = before - state.age_s, after - state.age_s
            times = [f.received for f in frames.values()]
            skew = (max(*times, state_high) - min(*times, state_low)) * 1000
            if skew > args.max_skew_ms:
                raise ValueError(f"receive-time skew {skew:.1f} ms exceeds {args.max_skew_ms} ms")
            images = [
                ObservedImage(
                    **self.cameras.snapshot_frame(source, frame),
                    received_monotonic_s=frame.received,
                )
                for source, frame in frames.items()
            ]
            now = self.clock()
            maximum_age = min(args.max_age_ms / 1000, self.cameras.settings.camera_max_age_s)
            if any(not 0 <= now - f.received <= maximum_age for f in frames.values()):
                raise ValueError("camera frame expired while collecting observation")
            if now - state_low > min(0.1, args.max_age_ms / 1000):
                raise ValueError("joint feedback expired while collecting observation")
            for image in images:
                image.age_ms = (now - image.received_monotonic_s) * 1000
            state.age_s += now - after
            if state.torso_age_s >= 0:
                state.torso_age_s += now - after
            if state.torso_age_s > 0.1:
                state.forearm_world_elevation_deg = {}
            identity = "observation-" + uuid.uuid4().hex
            path = self.cameras.snapshot_dir / (identity + ".json")
            output = ObserveOutput(
                observation_id=identity,
                manifest_path=str(path),
                collected_at_utc=datetime.now(timezone.utc).isoformat(),
                collected_monotonic_s=now,
                images=images,
                robot_state=state,
                state_received_monotonic_s_estimate=(state_low + state_high) / 2,
                state_read_window_ms=(after - before) * 1000,
                receive_skew_upper_bound_ms=skew,
                simulated=state.simulated,
                limitations=[
                    "Host receive-time association only; sensor capture timestamps are unavailable.",
                    "No depth, object detection, camera extrinsics or 3D localization is provided.",
                ]
                + (
                    [
                        "Head image is one stereo pair: two 640x480 eyes side by side in a "
                        "1280x480 frame; a single eye cannot be requested on its own."
                    ]
                    if "head" in frames
                    else []
                ),
            )
            with path.open("x") as stream:
                json.dump(output.model_dump(mode="json"), stream, ensure_ascii=False, indent=2)
            return ToolResult(status="succeeded", outputs=output.model_dump(mode="json"))
        except (ValueError, OSError, RuntimeError) as error:
            return ToolResult(
                status="failed", error=ToolError(code="OBSERVATION_UNAVAILABLE", message=str(error))
            )
