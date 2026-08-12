#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/profiles/operations.secrets.env"

# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"

if [[ "${LOCAL_INFERENCE_BOUND_QUALIFICATION:-}" == "1" ]]; then
  ENV_FILE="${MODELPORT_ENV_FILE:?Bound ModelPort credential capability is required}"
else
  ENV_FILE="${MODELPORT_ENV_FILE:-$ENV_FILE}"
fi
if [[ -f "$ENV_FILE" || -L "$ENV_FILE" ]]; then
  load_private_modelport_token "$ROOT_DIR" "$ENV_FILE"
fi
if [[ "${LOCAL_INFERENCE_BOUND_QUALIFICATION:-}" == "1" ]]; then
  export MODELPORT_BASE_URL="http://127.0.0.1:38082"
  unset ANTHROPIC_BASE_URL
fi

exec python3 "$SCRIPT_DIR/modelport-reasoning-smoke.py"
