#!/bin/bash
set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
. "$PROJECT_ROOT/scripts/unix_common.sh"
plcsim_ensure_venv "$PROJECT_ROOT"
cd "$PROJECT_ROOT"
exec "$PLCSIM_PY" iccas_sim.py "$@"
