"""Elbow bend and ground-relative forearm elevation (degrees at the UI boundary).

Rotation origins/axes from Unitree xr_teleoperate assets/g1/g1_body29_hand14.urdf.
Forearm direction is the wrist-roll longitudinal +X axis, NOT a hand position.
Ground Z is supplied by the torso IMU; no external position/yaw localization is needed.
"""

import math


def multiply(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def rotation(axis, angle):
    c, s = math.cos(angle), math.sin(angle)
    if axis == "x":
        return [[1, 0, 0], [0, c, -s], [0, s, c]]
    if axis == "y":
        return [[c, 0, s], [0, 1, 0], [-s, 0, c]]
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def rpy_matrix(rpy):
    roll, pitch, yaw = rpy
    return multiply(multiply(rotation("z", yaw), rotation("y", pitch)), rotation("x", roll))


def elbow_frame(side, positions, torso_rpy):
    """World orientation of the elbow's parent, including shoulder mounting rotations."""
    if side not in ("left", "right"):
        raise ValueError("side must be left or right")
    values = [*torso_rpy, *(positions[f"{side}_shoulder_{a}"] for a in ("pitch", "roll", "yaw"))]
    if not all(math.isfinite(v) for v in values):
        raise ValueError("nonfinite orientation feedback")
    sign = 1 if side == "left" else -1
    matrices = [
        rpy_matrix(torso_rpy),
        rpy_matrix((sign * 0.27931, 5.4949e-05, -sign * 0.00019159)),
        rotation("y", positions[f"{side}_shoulder_pitch"]),
        rotation("x", -sign * 0.27925),
        rotation("x", positions[f"{side}_shoulder_roll"]),
        rotation("z", positions[f"{side}_shoulder_yaw"]),
    ]
    result = matrices[0]
    for matrix in matrices[1:]:
        result = multiply(result, matrix)
    return result


def world_elevation_deg(side, positions, torso_rpy):
    r = elbow_frame(side, positions, torso_rpy)
    q = positions[f"{side}_elbow"]
    z = r[2][0] * math.cos(q) - r[2][2] * math.sin(q)
    return math.degrees(math.asin(max(-1.0, min(1.0, z))))


def resolve_angle(side, mode, angle_deg, positions, torso_rpy=None):
    """Solve only the elbow; preserve shoulder pose and choose nearest feasible branch."""
    if not math.isfinite(angle_deg):
        raise ValueError("angle must be finite")
    low, high = -0.2, 2.0  # existing commissioned elbow envelope
    if mode == "upper_arm":
        if not 0 <= angle_deg <= 180:
            raise ValueError("upper_arm bend must be between 0 and 180 degrees")
        q = math.pi / 2 - math.radians(angle_deg)
        if not low <= q <= high:
            raise ValueError("bend exceeds the commissioned elbow envelope")
        return q
    if mode != "world" or not -90 <= angle_deg <= 90:
        raise ValueError("world elevation must be between -90 and +90 degrees")
    if torso_rpy is None:
        raise ValueError("world mode requires fresh torso IMU orientation")
    r = elbow_frame(side, positions, torso_rpy)
    a, b = r[2][0], -r[2][2]
    amplitude = math.hypot(a, b)
    wanted = math.sin(math.radians(angle_deg))
    if amplitude < 1e-8:
        raise ValueError("elbow axis cannot control elevation in this shoulder pose")
    if abs(wanted) > amplitude + 1e-10:
        raise ValueError("requested elevation is unreachable with the shoulders fixed")
    offset = math.atan2(b, a)
    delta = math.acos(max(-1.0, min(1.0, wanted / amplitude)))
    candidates = [
        offset + sign * delta + turns * 2 * math.pi
        for sign in (-1, 1)
        for turns in (-1, 0, 1)
        if low <= offset + sign * delta + turns * 2 * math.pi <= high
    ]
    if not candidates:
        raise ValueError("requested elevation exceeds the commissioned elbow envelope")
    return min(candidates, key=lambda q: abs(q - positions[f"{side}_elbow"]))
