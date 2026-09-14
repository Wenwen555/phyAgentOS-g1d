"""Unitree SDK bridge and a deterministic device used only by the mock profile."""

import ctypes
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import DeviceSettings


@dataclass(frozen=True)
class State:
    height: float | None
    height_age_s: float
    height_sequence: int
    positions: tuple[float, ...]
    velocities: tuple[float, ...]
    joint_age_s: float
    joint_sequence: int
    mode_machine: int = 0
    watchdog_tripped: bool = False


class _NativeState(ctypes.Structure):
    _fields_ = [
        ("height", ctypes.c_double),
        ("height_age_s", ctypes.c_double),
        ("joint_age_s", ctypes.c_double),
        ("height_seq", ctypes.c_uint64),
        ("joint_seq", ctypes.c_uint64),
        ("q", ctypes.c_double * 16),
        ("dq", ctypes.c_double * 16),
        ("mode_machine", ctypes.c_int32),
        ("watchdog_tripped", ctypes.c_int32),
    ]


class UnitreeDevice:
    simulated = False

    def __init__(self, library: Path, settings: DeviceSettings):
        self.lib = ctypes.CDLL(str(library.resolve()))
        self.lib.g1d_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int] + [
            ctypes.c_double
        ] * 3
        self.lib.g1d_open.restype = ctypes.c_void_p
        self.lib.g1d_read.argtypes = [ctypes.c_void_p, ctypes.POINTER(_NativeState)]
        self.lib.g1d_read.restype = ctypes.c_int
        self.lib.g1d_height_velocity.argtypes = [ctypes.c_void_p, ctypes.c_double]
        self.lib.g1d_height_velocity.restype = ctypes.c_int
        self.lib.g1d_close.argtypes = [ctypes.c_void_p]
        self.lib.g1d_close.restype = None
        height = settings.height
        self.handle = self.lib.g1d_open(
            settings.network_interface.encode(),
            settings.domain_id,
            height.enabled,
            height.min_height_m or 0.0,
            height.max_height_m or 0.0,
            height.max_command,
        )
        if not self.handle:
            raise RuntimeError("G1-D DDS initialization failed; check NIC and local SDK libraries")

    def read(self) -> State:
        raw = _NativeState()
        if self.lib.g1d_read(self.handle, ctypes.byref(raw)) != 0:
            raise RuntimeError("G1-D state read failed")
        return State(
            raw.height if raw.height_seq else None,
            raw.height_age_s,
            raw.height_seq,
            tuple(raw.q),
            tuple(raw.dq),
            raw.joint_age_s,
            raw.joint_seq,
            raw.mode_machine,
            bool(raw.watchdog_tripped),
        )

    def command(self, velocity: float) -> int:
        return self.lib.g1d_height_velocity(self.handle, velocity)

    def close(self):
        if self.handle:
            self.lib.g1d_close(self.handle)
            self.handle = None


class MockDevice:
    """Local column simulation. No SDK library, DDS socket, or camera connection."""

    simulated = True

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last = clock()
        self.height = 0.5
        self.velocity = 0.0
        self.sequence = 1
        self.commands = []

    def read(self) -> State:
        now = self.clock()
        elapsed = now - self.last
        if elapsed > 0:
            self.height += self.velocity * 0.06 * elapsed
            self.last = now
            self.sequence += 1
        return State(self.height, 0.0, self.sequence, (0.0,) * 16, (0.0,) * 16, 0.0, self.sequence)

    def command(self, velocity: float) -> int:
        self.read()
        self.velocity = velocity
        self.commands.append(velocity)
        return 0

    def close(self):
        self.velocity = 0.0
