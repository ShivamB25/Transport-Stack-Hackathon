#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PS1_PYTHON:-/home/factory-user/ps1-scratch/venv/bin/python}"
export PS1_DATA_DIR="${PS1_DATA_DIR:-/home/factory-user/ps1-scratch/data}"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON" -m ps1.live_poller "$@"
