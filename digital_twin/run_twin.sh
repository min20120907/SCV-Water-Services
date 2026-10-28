#!/usr/bin/env bash
# SCV Water Digital Twin Runner
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${HOME}/venvs/scv-deploy/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
    PYTHON="python3"
fi

exec "${PYTHON}" "${HERE}/twin_engine.py" "$@"
