#!/bin/bash
set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$PROJECT_ROOT/start_iccas_process.command" --device-model \
  --device-state-dir "$PROJECT_ROOT/data/runtime/iccas-devices" "$@"
