#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELPORT_ENV_FILE="${MODELPORT_OPERATIONS_ENV_FILE:-}"
if [[ -z "$MODELPORT_ENV_FILE" && -n "${CREDENTIALS_DIRECTORY:-}" \
  && -f "$CREDENTIALS_DIRECTORY/operations.env" ]]; then
  MODELPORT_ENV_FILE="$CREDENTIALS_DIRECTORY/operations.env"
fi
MODELPORT_ENV_FILE="${MODELPORT_ENV_FILE:-$ROOT_DIR/profiles/operations.secrets.env}"
OPERATIONS_PROFILE_FILE="${OPERATIONS_PROFILE_FILE:-$ROOT_DIR/profiles/operations.env}"
# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"

if [[ ! -f "$MODELPORT_ENV_FILE" ]]; then
  printf 'Operations credential file not found: %s\n' "$MODELPORT_ENV_FILE" >&2
  printf 'Run scripts/provision-operations-secrets.py first.\n' >&2
  exit 2
fi
validate_private_env_file "$MODELPORT_ENV_FILE"

set -a
# shellcheck disable=SC1090
source "$MODELPORT_ENV_FILE"
set +a

if [[ -f "$OPERATIONS_PROFILE_FILE" ]]; then
  [[ ! -L "$OPERATIONS_PROFILE_FILE" ]] || {
    printf 'Operations profile must not be a symlink: %s\n' "$OPERATIONS_PROFILE_FILE" >&2
    exit 2
  }
  set -a
  # shellcheck disable=SC1090
  source "$OPERATIONS_PROFILE_FILE"
  set +a
fi

if [[ -n "${CREDENTIALS_DIRECTORY:-}" \
  && -f "$CREDENTIALS_DIRECTORY/backup.env" ]]; then
  validate_private_env_file "$CREDENTIALS_DIRECTORY/backup.env"
  set -a
  # shellcheck disable=SC1091
  source "$CREDENTIALS_DIRECTORY/backup.env"
  set +a
fi

export MODELPORT_BASE_URL="${MODELPORT_BASE_URL:-http://127.0.0.1:38082}"
export QWEN_RUNTIME_URL="${QWEN_RUNTIME_URL:-http://127.0.0.1:18080}"

if [[ "${1:-}" == "--dashboard-snapshots" ]]; then
  shift
  exec python3 "$ROOT_DIR/scripts/operations-collector.py" "$@"
fi

exec python3 "$ROOT_DIR/scripts/operations-report.py" "$@"
