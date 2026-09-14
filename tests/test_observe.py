import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from paos_g1d.manipulation.arm import MockArm
from paos_g1d.observe import ObserveQuery, ObserveRequest


def request(**kwargs):
    return SimpleNamespace(arguments=kwargs)


async def test_bundle_contains_exact_images_and_state_without_motion(cameras):
    arm = MockArm()
    result = await ObserveQuery(cameras, arm).query(request(sources=["head", "left_wrist"]), None)
    assert result.status == "succeeded"
    output = result.outputs
    assert output["association"] == "best_effort_receive_time"
    assert output["spatial_calibration"] == "not_provided"
    assert output["simulated"] is True
    assert len(output["robot_state"]["positions_rad"]) == 16
    assert [i["source"] for i in output["images"]] == ["head", "left_wrist"]
    from pathlib import Path

    assert json.loads(Path(output["manifest_path"]).read_text()) == output
    for image in output["images"]:
        assert hashlib.sha256(Path(image["image_path"]).read_bytes()).hexdigest() == image["sha256"]
    assert arm.history == []
    assert arm.phase == "idle"


async def test_unrequested_missing_camera_does_not_block(cameras):
    cameras.frames.pop("right_wrist")
    result = await ObserveQuery(cameras, MockArm()).query(request(), None)
    assert result.status == "succeeded"
    result = await ObserveQuery(cameras, MockArm()).query(request(sources=["right_wrist"]), None)
    assert result.status == "failed"


@pytest.mark.parametrize("change", ["stale", "dark", "skew"])
async def test_invalid_frames_fail_without_manifest(cameras, change):
    frame = cameras.frames["head"]
    if change == "stale":
        frame = replace(frame, received=frame.received - 2)
    elif change == "dark":
        frame = replace(frame, brightness=0)
    else:
        frame = replace(frame, received=frame.received - 0.3)
    cameras.frames["head"] = frame
    result = await ObserveQuery(cameras, MockArm()).query(request(), None)
    assert result.status == "failed"
    assert not list(cameras.snapshot_dir.glob("observation-*.json"))


@pytest.mark.parametrize("update", [{"age_s": 1.0}, {"simulated": False}])
async def test_bad_state_rejected(cameras, update):
    state = MockArm().read().model_copy(update=update)
    arm = SimpleNamespace(read=lambda: state)
    result = await ObserveQuery(cameras, arm).query(request(), None)
    assert result.status == "failed"


async def test_frame_pinned_across_state_read(cameras):
    original = cameras.frames["head"]
    state = MockArm().read()

    def read():
        cameras.frames["head"] = replace(original, sequence=original.sequence + 100)
        return state

    result = await ObserveQuery(cameras, SimpleNamespace(read=read)).query(request(), None)
    assert result.status == "succeeded"
    assert result.outputs["images"][0]["sequence"] == original.sequence


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sources": []},
        {"sources": ["head", "head"]},
        {"sources": ["unknown"]},
        {"max_age_ms": 0},
        {"max_skew_ms": -1},
        {"max_age_ms": float("nan")},
    ],
)
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        ObserveRequest(**kwargs)
