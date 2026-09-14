from pathlib import Path

import pytest
import yaml
from forge_gateway.config import GatewayConfig
from PhyAgentOS.skill_runtime.archive import sha256_file
from PhyAgentOS.skill_runtime.installer import NodeInstaller, SkillInstaller
from PhyAgentOS.skill_runtime.state import RuntimeStateStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["g1d-observe", "g1d-lift", "g1d-robot"])
def test_generated_skill_installs_with_native_validator(name, tmp_path):
    version = yaml.safe_load((ROOT / "skills" / name / "skill.yaml").read_text())["version"]
    archive = ROOT / "dist/skills" / f"{name}-{version}.tar.gz"
    if not archive.is_file():
        pytest.skip("build the Skill bundles first")
    store = RuntimeStateStore(tmp_path / "state")
    manifest = SkillInstaller(tmp_path / "skills", state_store=store).install(
        archive, expected_sha256=sha256_file(archive)
    )
    for profile in manifest.profiles.values():
        for asset in profile.required_assets:
            assert manifest.resolve_bundle_path(asset).is_file()
        path = manifest.resolve_bundle_path(profile.dataflow)
        flow = yaml.safe_load(path.read_text())
        config = yaml.safe_load((path.parent / "gateway.yaml").read_text())
        GatewayConfig.from_dict(config)
        assert config["agent"]["enabled"] is False
        declared = {item["tool_id"] for item in config["tools"]["specs"]}
        assert set(manifest.required_tools) == declared
        if name == "g1d-observe":
            assert all(item["semantics"] == "query" for item in config["tools"]["specs"])
            assert "--allow-height" not in flow["nodes"][1]["args"]
    assert manifest.artifacts.nodes["g1d"].sha256 == sha256_file(
        ROOT / "dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz"
    )


def test_real_device_defaults_cannot_enable_motion():
    from paos_g1d.contracts import DeviceSettings, HeightSettings

    defaults = HeightSettings()
    assert defaults.enabled is False
    assert defaults.limits_confirmed is False
    # A commissioned local configuration may enable motion only with valid limits.
    config = yaml.safe_load((ROOT / "configs/device.yaml").read_text())
    DeviceSettings.model_validate(config)


def test_generated_node_installs_with_real_digest(tmp_path):
    from PhyAgentOS.skill_runtime.manifest import load_manifest

    manifest = load_manifest(ROOT / "skills/g1d-observe/skill.yaml")
    store = RuntimeStateStore(tmp_path / "state")
    installer = NodeInstaller(tmp_path / "runtime", state_store=store)
    lock = manifest.artifacts.nodes["g1d"]
    path = installer.install(ROOT / "dist/nodes/g1d-node-0.1.0-linux-x86_64.tar.gz", lock)
    assert installer.load(lock) == path
