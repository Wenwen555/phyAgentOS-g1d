from pathlib import Path

import pytest
import yaml

from paos_g1d.cameras import Cameras
from paos_g1d.contracts import DeviceSettings, HeightSettings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings():
    return DeviceSettings(
        network_interface="mock",
        height=HeightSettings(
            enabled=True,
            limits_confirmed=True,
            min_height_m=0.0,
            max_height_m=1.0,
        ),
    )


@pytest.fixture
def cameras(tmp_path, settings):
    config = yaml.safe_load((ROOT / "configs/cameras.yaml").read_text())
    result = Cameras(config, settings, tmp_path / "snapshots", simulated=True)
    result.poll()
    yield result
    result.close()
