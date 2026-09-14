#!/usr/bin/env bash
# Manage the Core runtime independently of the device build/test environment.
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
core_environment="$project_root/environments/core"
core_uv="$project_root/.tools/uv"
core_python="$project_root/.python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12"

case "${1:-help}" in
  dora)
    shift
    exec "$project_root/.tools/dora" "$@"
    ;;
  sync)
    if [[ ! -x "$core_uv" || ! -x "$core_python" ]]; then
      echo 'Missing local uv or Python; see docs/core-environment.md.' >&2
      exit 1
    fi
    exec "$core_uv" sync --project "$core_environment" --frozen --python "$core_python"
    ;;
  check)
    exec "$core_uv" pip check --python "$core_environment/.venv/bin/python"
    ;;
  paos)
    shift
    exec "$core_environment/.venv/bin/python" "$project_root/scripts/project_cli.py" "$@"
    ;;
  python)
    core_program="$1"
    shift
    exec "$core_environment/.venv/bin/$core_program" "$@"
    ;;
  help|-h|--help)
    echo 'Usage: scripts/core-env.sh {sync|check|paos [args...]|python [args...]|dora [args...]}'
    ;;
  *)
    echo 'Unknown command. Use scripts/core-env.sh --help.' >&2
    exit 2
    ;;
esac
