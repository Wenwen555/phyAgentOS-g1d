"""Select the project instance before invoking the unmodified Core CLI."""

import json
import os
import sys

from init_instance import CONFIG, ROOT, initialize
from PhyAgentOS.config.loader import set_config_path
from PhyAgentOS.config.schema import Config


def normalize_proxy_schemes(environ):
    """Accept the common socks:// alias without disabling the user's proxy."""
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        value = environ.get(name, "")
        if value[:8].lower() == "socks://":
            environ[name] = "socks5://" + value[8:]


def main():
    normalize_proxy_schemes(os.environ)
    if sys.argv[1:] == ["onboard"]:
        initialize()
        return
    if not CONFIG.is_file():
        raise SystemExit("Initialize first: scripts/core-env.sh paos onboard")
    Config.model_validate(json.loads(CONFIG.read_text()))
    set_config_path(CONFIG)
    os.environ["PATH"] = str(ROOT / ".tools") + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("PAOS_G1D_SNAPSHOT_DIR", str(ROOT / "workspace/g1d_snapshots"))
    if sys.argv[1:2] in (["agent"], ["gateway"]):
        sys.path.insert(0, str(ROOT / "src"))
        from paos_g1d.agent_integration import install

        install()
    from PhyAgentOS.cli.commands import app

    app(prog_name="paos")


if __name__ == "__main__":
    main()
