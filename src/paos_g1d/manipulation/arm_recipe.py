"""Reference replay inputs, not an extra robot tool or an LLM substitute."""

import math

from .arm import DEX1_DEFAULT_MAX_OPEN_RAD, NAMES

PROMPT = "抬起双肘90度，左臂旋转50度，夹爪打开，然后逆向此过程"


def recipe(initial, left_dex1_open=DEX1_DEFAULT_MAX_OPEN_RAD):
    """Replay steps; the Dex1 open target must stay inside the confirmed envelope."""
    down = {n: 0.0 for n in NAMES if "dex1" not in n}
    down["left_elbow"] = down["right_elbow"] = math.pi / 2

    def move(label, targets, seconds):
        return {
            "label": label,
            "tool_id": "g1d.move_joints",
            "arguments": {
                "targets": [{"joint": n, "position_rad": v} for n, v in targets.items()],
                "duration_s": seconds,
                "timeout_s": 20.0,
            },
        }

    return [
        move("initialize", down, 5.0),
        move("raise_elbows", {"left_elbow": 0.0, "right_elbow": 0.0}, 3.0),
        move("rotate_left_wrist", {"left_wrist_roll": math.radians(50)}, 1.5),
        move("open_left_dex1", {"left_dex1": left_dex1_open}, 1.5),
        move("restore_left_dex1", {"left_dex1": initial["left_dex1"]}, 1.5),
        move("restore_left_wrist", {"left_wrist_roll": 0.0}, 1.5),
        move("lower_elbows", {"left_elbow": math.pi / 2, "right_elbow": math.pi / 2}, 3.0),
    ]
