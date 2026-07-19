#!/usr/bin/env bash
set -euo pipefail

MODELPORT_DIR="${MODELPORT_DIR:-/home/tiammomo/projects/dev/ModelPort}"
ENV_FILE="${MODELPORT_ENV_FILE:-$MODELPORT_DIR/.env}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

exec python3 "$SCRIPT_DIR/modelport-reasoning-smoke.py"
