"""Device configuration and strict Tool schemas shared by the node and Skill builder."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SOURCES = ("head", "left_wrist", "right_wrist")
JOINTS = (
    "waist_yaw",
    "waist_pitch",
    "left_shoulder_pitch",
    "left_shoulder_roll",
    "left_shoulder_yaw",
    "left_elbow",
    "left_wrist_roll",
    "left_wrist_pitch",
    "left_wrist_yaw",
    "right_shoulder_pitch",
    "right_shoulder_roll",
    "right_shoulder_yaw",
    "right_elbow",
    "right_wrist_roll",
    "right_wrist_pitch",
    "right_wrist_yaw",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class HeightSettings(StrictModel):
    enabled: bool = False
    limits_confirmed: bool = False
    min_height_m: float | None = None
    max_height_m: float | None = None
    max_command: float = Field(default=0.15, gt=0, le=1)
    kp: float = Field(default=10.0, gt=0, le=20)
    feedback_timeout_s: float = Field(default=0.3, gt=0, le=0.3)
    max_duration_s: float = Field(default=15.0, gt=0, le=60)
    settle_s: float = Field(default=0.3, ge=0.2, le=2)
    stop_timeout_s: float = Field(default=2.0, ge=0.5, le=5)
    stop_tolerance_m: float = Field(default=0.0005, gt=0, le=0.001)

    @model_validator(mode="after")
    def check_limits(self):
        if self.enabled:
            if not self.limits_confirmed or self.min_height_m is None or self.max_height_m is None:
                raise ValueError("height control requires explicitly confirmed mechanical limits")
            if self.min_height_m >= self.max_height_m:
                raise ValueError("min_height_m must be less than max_height_m")
        return self


class Dex1Settings(StrictModel):
    """Operator-confirmed Dex1 travel, measured on this robot.

    The vendor XR teleop mapping uses (0.0, 5.4) for these fingers, which is a
    mapping constant rather than a mechanical specification: the fingers stall
    below it, and an unreachable target stalls silently instead of failing.
    Unconfirmed values keep the historical 5.4/0.0 defaults so the mock runtime
    and the unit tests behave as before; the real robot uses the measured
    commandable open target and the empty-close rest position.
    """

    limits_confirmed: bool = False
    left_max_open_rad: float | None = Field(default=None, gt=0, le=5.4)
    right_max_open_rad: float | None = Field(default=None, gt=0, le=5.4)
    left_closed_rest_rad: float | None = Field(default=None, ge=0, le=5.4)
    right_closed_rest_rad: float | None = Field(default=None, ge=0, le=5.4)
    contact_margin_rad: float = Field(default=0.03, gt=0, le=0.2)

    @model_validator(mode="after")
    def check_travel(self):
        if self.limits_confirmed and self.left_max_open_rad is None:
            raise ValueError("Dex1 control requires explicitly confirmed travel values")
        for side in ("left", "right"):
            maximum = getattr(self, f"{side}_max_open_rad")
            rest = getattr(self, f"{side}_closed_rest_rad")
            if maximum is None or rest is None:
                continue
            if rest + self.contact_margin_rad >= maximum:
                raise ValueError(
                    f"{side} Dex1 contact threshold must stay below the commandable open target"
                )
        return self

    def max_open(self, side: str) -> float:
        value = getattr(self, f"{side}_max_open_rad")
        return 5.4 if value is None else value

    def closed_rest(self, side: str) -> float:
        value = getattr(self, f"{side}_closed_rest_rad")
        return 0.0 if value is None else value


class DeviceSettings(StrictModel):
    network_interface: str = Field(min_length=1)
    domain_id: int = Field(default=0, ge=0, le=232)
    height: HeightSettings = Field(default_factory=HeightSettings)
    dex1: Dex1Settings = Field(default_factory=Dex1Settings)
    camera_max_age_s: float = Field(default=1.0, gt=0, le=5)
    max_jpeg_bytes: int = Field(default=8388608, ge=1024, le=8388608)
    min_brightness: float = Field(default=5.0, ge=0, le=255)


class StateRequest(StrictModel):
    max_age_ms: int = Field(ge=1, le=1000)
    # Optional projection so a caller pays only for the joints it will use. The
    # lift workflow reads height_m on every step and would otherwise carry 16 joint
    # names, positions and velocities through the whole conversation context.
    # None keeps the full detail, so existing callers are unchanged; an empty list
    # asks for height only.
    joints: list[str] | None = Field(default=None, max_length=len(JOINTS))

    @model_validator(mode="after")
    def check_joints(self):
        if self.joints is None:
            return self
        if len(set(self.joints)) != len(self.joints):
            raise ValueError("duplicate joint")
        unknown = [name for name in self.joints if name not in JOINTS]
        if unknown:
            raise ValueError(f"unknown joint: {', '.join(unknown)}")
        return self


class StateOutput(StrictModel):
    height_m: float
    height_sequence: int = Field(ge=1)
    height_age_ms: float = Field(ge=0)
    joint_names: list[str]
    positions_rad: list[float]
    velocities_rad_s: list[float]
    joint_sequence: int = Field(ge=1)
    joint_age_ms: float = Field(ge=0)
    mode_machine: int
    time_basis: Literal["device_receive_monotonic"] = "device_receive_monotonic"
    simulated: bool


class SnapshotRequest(StrictModel):
    source: Literal["head", "left_wrist", "right_wrist"]
    max_age_ms: int = Field(ge=1, le=1000)


class SnapshotOutput(StrictModel):
    source: Literal["head", "left_wrist", "right_wrist"]
    image_path: str
    sha256: str
    sequence: int = Field(ge=1)
    age_ms: float = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    binocular: bool
    mean_brightness: float = Field(ge=0, le=255)
    time_basis: Literal["adapter_receive_monotonic"] = "adapter_receive_monotonic"
    simulated: bool


class HeightRequest(StrictModel):
    target_height_m: float
    tolerance_m: float = Field(ge=0.001, le=0.01)
    timeout_s: float = Field(gt=0, le=60)


class HeightOutput(StrictModel):
    target_height_m: float
    final_height_m: float | None
    tolerance_m: float
    reached_goal: bool
    stop_command_accepted: bool
    stopped_observed: bool
    stop_observation_basis: Literal["fresh_height_stability"] = "fresh_height_stability"
    elapsed_s: float = Field(ge=0)
    reason: str
    simulated: bool


TOOLS = {
    "g1d.state": ("g1d.state", "get", "query", StateRequest, StateOutput),
    "g1d.camera_snapshot": ("g1d.cameras", "snapshot", "query", SnapshotRequest, SnapshotOutput),
    "g1d.set_height": ("g1d.lift", "set_height", "action", HeightRequest, HeightOutput),
}
