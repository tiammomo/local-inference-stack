#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${QWEN_BOOT_PROFILE:-latency}"
RESTORE_PRODUCTION="${QWEN_RESTORE_PRODUCTION:-true}"
export QWEN_START_ATTEMPTS="${QWEN_RECONCILE_ATTEMPTS:-180}"
# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"
load_deployment_env "$ROOT_DIR"

CANDIDATE_CONTAINER="$(
  sed -n 's/^QWEN_CONTAINER_NAME=//p' "$ROOT_DIR/profiles/candidate.env" | head -n 1
)"
if [[ -n "$CANDIDATE_CONTAINER" ]] \
  && docker inspect "$CANDIDATE_CONTAINER" >/dev/null 2>&1; then
  "$ROOT_DIR/scripts/candidate-runtime.sh" stop
fi

if [[ "$RESTORE_PRODUCTION" == false ]]; then
  if docker inspect --format '{{.State.Running}}' "$QWEN_CONTAINER_NAME" \
    2>/dev/null | grep -qx true; then
    "$ROOT_DIR/scripts/runtime.sh" stop
  fi
  printf 'Runtime cleanup completed; production was not running before the transaction.\n'
  exit 0
fi

if curl --noproxy '*' --connect-timeout 1 --max-time 3 -fsS \
  http://127.0.0.1:18080/health >/dev/null 2>&1; then
  printf 'Qwen runtime is already healthy.\n'
  exit 0
fi

if docker inspect --format '{{.State.Running}}' "$QWEN_CONTAINER_NAME" \
  2>/dev/null | grep -qx true; then
  exec "$ROOT_DIR/scripts/runtime.sh" restart
fi

exec "$ROOT_DIR/scripts/runtime.sh" start "$PROFILE"
