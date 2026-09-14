#!/usr/bin/env bash
# Patch the broken Iterator polyfill guard shipped in @deepseek-ai/dsh bundles.
#
# Root cause (deepseek-harness discussion #6083): in every published dsh
# version the file @deepseek-ai/dsh-client-ui-sidebar-documentpreview/lib/client.js
# contains, at module top level:
#
#   if (typeof Iterator.prototype.join !== "function") Iterator.prototype.join = ...
#
# On engines without the ES2025 `Iterator` global (Firefox < 131, Safari < 18.2,
# Chrome < 122) evaluating the guard itself throws
#   ReferenceError / "Can't find variable: Iterator"
# and the whole web UI module fails to load.
#
# This script rewrites the guard to check the global first. Re-run it whenever
# dsh is reinstalled (npx cache refresh, version bump, npm cache clean).
set -euo pipefail

python3 - <<'PY'
from pathlib import Path
import subprocess

OLD = 'if (typeof Iterator.prototype.join !== "function") Iterator.prototype.join = function(separator) {'
NEW = 'if (typeof Iterator !== "undefined" && typeof Iterator.prototype.join !== "function") Iterator.prototype.join = function(separator) {'

roots = [Path.home() / ".npm/_npx"]
try:
    g = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    if g:
        roots.append(Path(g))
except Exception:
    pass

hits = []
for root in roots:
    if not root.is_dir():
        continue
    for p in root.rglob("client.js"):
        if "dsh-client-ui-sidebar-documentpreview" not in p.parts:
            continue
        try:
            text = p.read_text()
        except OSError:
            continue
        if OLD in text:
            p.write_text(text.replace(OLD, NEW))
            hits.append(str(p))

if not hits:
    print("patch-dsh-iterator: no unpatched dsh bundles found")
else:
    print("patch-dsh-iterator: patched:")
    for h in hits:
        print("  " + h)
PY
