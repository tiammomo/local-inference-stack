#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELPORT_ENV_FILE="${MODELPORT_OPERATIONS_ENV_FILE:-$ROOT_DIR/profiles/operations.secrets.env}"
OPERATIONS_PROFILE_FILE="${OPERATIONS_PROFILE_FILE:-$ROOT_DIR/profiles/operations.env}"

if [[ ! -f "$MODELPORT_ENV_FILE" ]]; then
  printf 'Operations credential file not found: %s\n' "$MODELPORT_ENV_FILE" >&2
  printf 'Run scripts/provision-operations-secrets.py first.\n' >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$MODELPORT_ENV_FILE"
set +a

if [[ -f "$OPERATIONS_PROFILE_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$OPERATIONS_PROFILE_FILE"
  set +a
fi

export MODELPORT_BASE_URL="${MODELPORT_BASE_URL:-http://127.0.0.1:38082}"
export QWEN_RUNTIME_URL="${QWEN_RUNTIME_URL:-http://127.0.0.1:18080}"

exec python3 "$ROOT_DIR/scripts/operations-report.py" "$@"
