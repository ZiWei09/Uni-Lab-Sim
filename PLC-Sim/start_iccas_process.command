#!/bin/bash
set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$PROJECT_ROOT/start_iccas.command" --station postprocessing --timing-mode process --time-scale 60 \
  --seed-process-recipe --filter-extensions \
  --state-file "$PROJECT_ROOT/data/runtime/iccas-process-state.json" "$@"
