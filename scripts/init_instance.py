"""Initialize the project using Core's config model and bundled workspace templates."""

import json
from pathlib import Path

from PhyAgentOS.config.loader import save_config, set_config_path
from PhyAgentOS.config.schema import Config
from PhyAgentOS.utils.helpers import sync_workspace_templates

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".paos-instance/config.json"


def initialize():
    workspace = ROOT / "workspace"
    if CONFIG.exists():
        # Validate explicitly: the generic loader falls back to defaults for broken JSON.
        config = Config.model_validate(json.loads(CONFIG.read_text()))
        if config.workspace_path.resolve() != workspace:
            raise ValueError("Existing configuration uses another workspace; preserved unchanged")
    else:
        data = json.loads((ROOT / "configs/paos.example.json").read_text())
        data["agents"]["defaults"]["workspace"] = str(workspace)
        config = Config.model_validate(data)
        CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        CONFIG.touch(mode=0o600, exist_ok=False)
        save_config(config, CONFIG)
    set_config_path(CONFIG)
    workspace.mkdir(parents=True, exist_ok=True)
    added = sync_workspace_templates(workspace)
    (workspace / "g1d_snapshots").mkdir(exist_ok=True)
    print(f"Config: {CONFIG}")
    print(f"Workspace: {workspace}")
    print(f"New templates: {len(added)}; existing configuration and files preserved")


if __name__ == "__main__":
    initialize()
