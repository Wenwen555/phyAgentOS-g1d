"""Parameterized arm Tool contracts and native/mock persistent control backends."""

import ctypes
import math
import os
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..contracts import StrictModel
from .elbow_frames import resolve_angle, world_elevation_deg

NAMES = [
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
    "left_dex1",
    "right_dex1",
]
IDS = dict(zip(NAMES, [*range(15, 29), 31, 33], strict=True))
# Vendor XR teleop mapping constant for the Dex1 fingers, not a mechanical
# specification: the measured stall can sit well below it. The real robot narrows
# this through `configure_limits` from the operator-confirmed device settings.
DEX1_DEFAULT_MAX_OPEN_RAD = 5.4
LIMITS = dict(
    zip(
        NAMES,
        [
            (-math.pi / 2, math.pi / 2),
            (-0.2, 1.2),
            (-0.5, 0.5),
            (-0.2, 2.0),
            (-math.pi / 2, math.pi / 2),
            (-0.5, 0.5),
            (-0.5, 0.5),
            (-math.pi / 2, math.pi / 2),
            (-1.2, 0.2),
            (-0.5, 0.5),
            (-0.2, 2.0),
            (-math.pi / 2, math.pi / 2),
            (-0.5, 0.5),
            (-0.5, 0.5),
            (0.0, DEX1_DEFAULT_MAX_OPEN_RAD),
            (0.0, DEX1_DEFAULT_MAX_OPEN_RAD),
        ],
        strict=True,
    )
)


def envelope_limits(dex1=None, *, mock=False):
    """Application envelopes, narrowed to the confirmed Dex1 travel when known.

    The mock runtime never reads device.yaml, so it keeps the defaults; only the
    real node configures the measured values.
    """
    limits = dict(LIMITS)
    if dex1 is None or mock:
        return limits
    for side in ("left", "right"):
        limits[f"{side}_dex1"] = (0.0, dex1.max_open(side))
    return limits


def configure_limits(dex1):
    """Narrow the module-level envelopes used by MoveRequest validation."""
    LIMITS.update(envelope_limits(dex1))


JointName = Literal[
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
    "left_dex1",
    "right_dex1",
]


class JointTarget(StrictModel):
    joint: JointName
    position_rad: float


class ElbowTarget(StrictModel):
    side: Literal["left", "right"]
    mode: Literal["upper_arm", "world"]
    angle_deg: float = Field(ge=-90, le=180, allow_inf_nan=False)


class ArmStateRequest(StrictModel):
    """Arm/Dex1 state request.

    It carries the same freshness bound as `g1d.state`, but its joint names are the
    arm's own axes, which include the two internal Dex1 fingers. Validating against
    the waist/arm joint list instead would reject a request for the very axes a grasp
    workflow reads.
    """

    max_age_ms: int = Field(ge=1, le=1000)
    joints: list[JointName] | None = Field(default=None, max_length=len(NAMES))

    @model_validator(mode="after")
    def check_joints(self):
        if self.joints is not None and len(set(self.joints)) != len(self.joints):
            raise ValueError("duplicate joint")
        return self


class MoveRequest(StrictModel):
    targets: list[JointTarget] = Field(default_factory=list, max_length=16)
    elbows: list[ElbowTarget] = Field(default_factory=list, max_length=2)
    duration_s: float = Field(ge=0.5, le=30)
    timeout_s: float = Field(ge=1, le=60)

    @model_validator(mode="after")
    def validate_targets(self):
        names = [t.joint for t in self.targets]
        names += [f"{t.side}_elbow" for t in self.elbows]
        if not names or len(names) > 16:
            raise ValueError("provide 1..16 joint targets or elbow targets")
        if len(set(names)) != len(names):
            raise ValueError("duplicate joint")
        for t in self.targets:
            lo, hi = LIMITS[t.joint]
            if not lo <= t.position_rad <= hi:
                raise ValueError(f"{t.joint} outside application envelope {lo}..{hi}")
        for t in self.elbows:
            if t.mode == "upper_arm":
                resolve_angle(t.side, t.mode, t.angle_deg, {})
            elif t.angle_deg > 90:
                raise ValueError("world elevation must be between -90 and +90 degrees")
            if t.mode == "world" and any(n.startswith(f"{t.side}_shoulder_") for n in names):
                raise ValueError("world elbow: move the shoulder first, then submit the elbow")
        if self.timeout_s < self.duration_s + 0.3:
            raise ValueError("timeout must include trajectory and settling")
        return self

    def resolve(self, state):
        targets = list(self.targets)
        for target in self.elbows:
            if target.mode == "world" and (
                state.torso_rpy_rad is None or not 0 <= state.torso_age_s <= 0.1
            ):
                raise ValueError("world mode requires torso IMU feedback newer than 100 ms")
            targets.append(
                JointTarget(
                    joint=f"{target.side}_elbow",
                    position_rad=resolve_angle(
                        target.side,
                        target.mode,
                        target.angle_deg,
                        state.positions_rad,
                        state.torso_rpy_rad,
                    ),
                )
            )
        return MoveRequest(targets=targets, duration_s=self.duration_s, timeout_s=self.timeout_s)


class ArmOutput(StrictModel):
    positions_rad: dict[str, float]
    references_rad: dict[str, float]
    age_s: float
    phase: str
    effective_duration_s: float
    simulated: bool
    reference_held: bool
    torso_rpy_rad: tuple[float, float, float] | None = None
    torso_age_s: float = -1.0
    elbow_bend_deg: dict[str, float] = Field(default_factory=dict)
    forearm_world_elevation_deg: dict[str, float] = Field(default_factory=dict)
    requested_elbows: list[ElbowTarget] = Field(default_factory=list)
    resolved_targets: list[JointTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def angle_feedback(self):
        self.elbow_bend_deg = {
            side: 90 - math.degrees(self.positions_rad[f"{side}_elbow"])
            for side in ("left", "right")
        }
        self.forearm_world_elevation_deg = {}
        if (
            self.torso_rpy_rad is not None
            and 0 <= self.torso_age_s <= 0.1
            and 0 <= self.age_s <= 0.1
        ):
            self.forearm_world_elevation_deg = {
                side: world_elevation_deg(side, self.positions_rad, self.torso_rpy_rad)
                for side in ("left", "right")
            }
        return self


ARM_TOOLS = {
    "g1d.arm_state": ("g1d.arm_state", "get", "query", ArmStateRequest, ArmOutput),
    "g1d.move_joints": ("g1d.arm", "move", "action", MoveRequest, ArmOutput),
}


def project_arm_state(state: ArmOutput, joints: list[str] | None) -> dict:
    """Narrow a dumped arm state to the requested joints.

    ArmOutput derives its angle feedback from the full pose inside its validator, so
    the projection happens after the model is built: the declared schema already
    allows any subset of the position/reference maps, and the caller pays only for
    the joints it asked about.
    """
    outputs = state.model_dump()
    if joints is None:
        return outputs
    wanted = set(joints)
    for key in ("positions_rad", "references_rad"):
        outputs[key] = {name: value for name, value in outputs[key].items() if name in wanted}
    for side in ("left", "right"):
        if f"{side}_elbow" not in wanted:
            outputs["elbow_bend_deg"].pop(side, None)
            outputs["forearm_world_elevation_deg"].pop(side, None)
    return outputs


def speed(name):
    return (
        7.0
        if "dex1" in name
        else 1.1
        if name in ("left_elbow", "right_elbow", "left_wrist_roll", "right_wrist_roll")
        else 0.65
    )


def tolerance(name):
    return 0.15 if "shoulder" in name or "elbow" in name or "dex1" in name else 0.08


class NativeArm:
    def __init__(self, device):
        self.device = device
        lib = device.lib
        ptr = ctypes.POINTER(ctypes.c_double)
        lib.g1d_arm_move.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ptr,
            ctypes.c_int,
            ctypes.c_double,
        ]
        lib.g1d_arm_move.restype = ctypes.c_int
        lib.g1d_arm_read.argtypes = [
            ctypes.c_void_p,
            ptr,
            ptr,
            ptr,
            ptr,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.g1d_arm_read.restype = ctypes.c_int
        lib.g1d_arm_cancel.argtypes = [ctypes.c_void_p]
        lib.g1d_arm_cancel.restype = ctypes.c_int
        lib.g1d_torso_read.argtypes = [ctypes.c_void_p, ptr, ptr]
        lib.g1d_torso_read.restype = ctypes.c_int

    def read(self):
        q = (ctypes.c_double * 35)()
        r = (ctypes.c_double * 35)()
        age = ctypes.c_double()
        duration = ctypes.c_double()
        phase = ctypes.c_int()
        if (
            self.device.lib.g1d_arm_read(
                self.device.handle,
                q,
                r,
                ctypes.byref(age),
                ctypes.byref(duration),
                ctypes.byref(phase),
            )
            != 0
        ):
            raise RuntimeError("native arm read failed")
        torso = (ctypes.c_double * 3)()
        torso_age = ctypes.c_double(-1)
        if self.device.lib.g1d_torso_read(self.device.handle, torso, ctypes.byref(torso_age)):
            raise RuntimeError("native torso read failed")
        return ArmOutput(
            positions_rad={n: q[j] for n, j in IDS.items()},
            references_rad={n: r[j] for n, j in IDS.items()},
            age_s=age.value,
            phase=["idle", "running", "succeeded", "cancelled", "failed"][phase.value],
            effective_duration_s=duration.value,
            simulated=False,
            reference_held=phase.value != 0,
            torso_rpy_rad=tuple(torso) if torso_age.value >= 0 else None,
            torso_age_s=torso_age.value,
        )

    def move(self, args):
        # Known local SDK test programs do not participate in the native owner lock.
        # Refuse admission if one is running; never stop another controller here.
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit() or int(proc.name) == os.getpid():
                continue
            try:
                executable = (proc / "exe").readlink().name.removesuffix(" (deleted)")
            except OSError:
                continue
            if executable in {
                "arm_sequence",
                "left_forward",
                "right_forward",
                "both_forward",
                "elbows_90",
                "shoulder_probe",
            }:
                raise RuntimeError(
                    f"Existing SDK arm controller is running: {executable} pid={proc.name}"
                )
        ids = (ctypes.c_int * len(args.targets))(*(IDS[t.joint] for t in args.targets))
        values = (ctypes.c_double * len(args.targets))(*(t.position_rad for t in args.targets))
        rc = self.device.lib.g1d_arm_move(
            self.device.handle, ids, values, len(args.targets), args.duration_s
        )
        if rc:
            raise RuntimeError(f"native arm admission failed: {rc}")

    def cancel(self):
        if self.device.lib.g1d_arm_cancel(self.device.handle):
            raise RuntimeError("native arm cancel failed")


class MockArm:
    """Ideal tracking simulation; exercises sequencing, not physical accuracy."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.q = dict.fromkeys(NAMES, 0.0)
        self.q["left_elbow"] = self.q["right_elbow"] = math.pi / 2
        self.origin = self.q.copy()
        self.target = self.q.copy()
        self.phase = "idle"
        self.duration = 0.0
        self.started = clock()
        self.history = []

    def read(self):
        if self.phase == "running":
            elapsed = self.clock() - self.started
            u = min(1.0, elapsed / self.duration)
            b = u**3 * (10 + u * (-15 + 6 * u))
            self.q = {n: self.origin[n] + (self.target[n] - self.origin[n]) * b for n in NAMES}
            if u == 1.0:
                self.q = self.target.copy()
            if elapsed >= self.duration + 0.3:
                self.phase = "succeeded"
        return ArmOutput(
            positions_rad=self.q.copy(),
            references_rad=self.q.copy(),
            age_s=0.0,
            phase=self.phase,
            effective_duration_s=self.duration,
            simulated=True,
            reference_held=self.phase != "idle",
            torso_rpy_rad=(0.0, 0.0, 0.0),
            torso_age_s=0.0,
        )

    def move(self, args):
        self.read()
        if self.phase == "running":
            raise RuntimeError("busy")
        self.origin = self.q.copy()
        self.target = self.q.copy()
        self.duration = args.duration_s
        for t in args.targets:
            self.target[t.joint] = t.position_rad
            self.duration = max(
                self.duration, 1.875 * abs(t.position_rad - self.q[t.joint]) / speed(t.joint)
            )
        self.phase = "running"
        self.started = self.clock()
        self.history.append(args.model_dump())

    def cancel(self):
        self.read()
        if self.phase == "running":
            self.phase = "cancelled"
            self.target = self.q.copy()
