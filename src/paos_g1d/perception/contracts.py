"""Strict Tool schemas for continuous camera capture and latest-state reads."""

from typing import Literal

from pydantic import Field, model_validator

from ..contracts import StrictModel
from ..observe import ObservedImage

Source = Literal["head", "left_wrist", "right_wrist"]

LIMITATIONS = [
    "Native camera JPEGs only; no object detection, depth or calibrated 3D position is provided.",
    "Image pixels are not robot target coordinates; interpret them with the Agent vision model.",
    "Host receive-time ordering only; sensor exposure timestamps are unavailable.",
    "Only the newest frame per source is returned; the store keeps no earlier frames, so a"
    " caller that needs the recent past must watch across repeated reads.",
]


class PerceptionSessionRequest(StrictModel):
    sources: list[Source] = Field(default_factory=lambda: ["head"], min_length=1, max_length=3)
    # A placement watch has to outlive the human's placing action, which routinely takes
    # over a minute; the Gateway invoke timeout is raised to match (see scripts/build.py).
    duration_s: float = Field(default=30.0, gt=0, le=300)
    interval_s: float = Field(default=0.5, ge=0.1, le=2.0)
    max_age_ms: int = Field(default=500, ge=1, le=1000)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("duplicate camera sources")
        return self


class PerceptionStateRequest(StrictModel):
    max_age_ms: int = Field(default=500, ge=1, le=1000)


class PerceptionFrame(ObservedImage):
    """One captured frame plus whether it differs from the previous read's frame.

    The store keeps no history, so a watcher cannot diff two frames itself without
    holding state across calls. This flag compares the frame returned now with the
    one this same source returned on the previous successful read, which lets a
    caller skip a vision pass on a frame it has already judged. It compares
    ``sha256``, so it is exact: equal digests mean byte-identical content. A first
    read, a newly added source, or a read after a stale failure all report True.
    """

    changed_since_last_read: bool


class PerceptionStateOutput(StrictModel):
    updated_monotonic_s: float = Field(ge=0)
    store_age_ms: float = Field(ge=0)
    session_active: bool
    # Exactly one frame per requested source: the newest the session captured.
    frames: list[PerceptionFrame] = Field(min_length=1)
    simulated: bool
    limitations: list[str]


class PerceptionSessionOutput(StrictModel):
    sources: list[Source] = Field(min_length=1)
    duration_s: float = Field(gt=0)
    elapsed_s: float = Field(ge=0)
    frames_captured: dict[str, int]
    last_sequences: dict[str, int]
    reason: str
    simulated: bool
    limitations: list[str]


PERCEPTION_TOOLS = {
    "g1d.perception_state": (
        "g1d.perception_state",
        "get",
        "query",
        PerceptionStateRequest,
        PerceptionStateOutput,
    ),
    "g1d.perception_session": (
        "g1d.perception_session",
        "run",
        "action",
        PerceptionSessionRequest,
        PerceptionSessionOutput,
    ),
}
