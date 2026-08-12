#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${QWEN_BOOT_PROFILE:-latency}"
RESTORE_PRODUCTION="${QWEN_RESTORE_PRODUCTION:-true}"
FAILED_CONTAINER="${QWEN_FAILED_CONTAINER_NAME:-}"
export QWEN_START_ATTEMPTS="${QWEN_RECONCILE_ATTEMPTS:-180}"
# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"
load_deployment_env "$ROOT_DIR"
acquire_runtime_lock "$ROOT_DIR"

if [[ -n "${LOCAL_INFERENCE_RUNTIME_PULL_POLICY:-}" \
  && -z "${QWEN_CONTROL_TRANSACTION_ID:-}" ]]; then
  printf 'Controlled runtime pull policy requires a rollout recovery transaction.\n' >&2
  exit 2
fi

if [[ -n "$FAILED_CONTAINER" \
  && ! "$FAILED_CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  printf 'Unsafe failed runtime container name: %s\n' "$FAILED_CONTAINER" >&2
  exit 2
fi

if [[ -n "${QWEN_CONTROL_TRANSACTION_ID:-}" ]]; then
  python3 - \
    "$ROOT_DIR/cache/control-plane/transaction.json" \
    "$QWEN_CONTROL_TRANSACTION_ID" \
    "${QWEN_ALLOW_LEGACY_RECONCILIATION:-false}" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
transaction_id = sys.argv[2]
allow_legacy = sys.argv[3] == "true"
document = json.loads(path.read_text(encoding="utf-8"))
if document.get("id") != transaction_id:
    raise SystemExit("reconciliation transaction identity changed")
schema = document.get("schemaVersion")
state = document.get("state")
operation = document.get("operation")
pull_policy = os.environ.get("LOCAL_INFERENCE_RUNTIME_PULL_POLICY")
if schema == 2 and state != "production_restoring":
    raise SystemExit(
        f"schema v2 reconciliation requires production_restoring; observed={state}"
    )
if schema == 1 and not allow_legacy:
    raise SystemExit("legacy reconciliation requires explicit reviewed authorization")
if schema not in {1, 2}:
    raise SystemExit(f"unsupported reconciliation transaction schema: {schema}")
rollout_recovery = schema == 2 and operation in {"upgrade", "rollback"}
if rollout_recovery and pull_policy != "never":
    raise SystemExit("rollout recovery requires the controlled no-pull policy")
if pull_policy is not None and not rollout_recovery:
    raise SystemExit("controlled no-pull policy is not authorized for this recovery")
PY
fi

CANDIDATE_CONTAINER="$(
  sed -n 's/^QWEN_CONTAINER_NAME=//p' "$ROOT_DIR/profiles/candidate.env" | head -n 1
)"
if [[ -n "$CANDIDATE_CONTAINER" ]] \
  && docker inspect "$CANDIDATE_CONTAINER" >/dev/null 2>&1; then
  "$ROOT_DIR/scripts/candidate-runtime.sh" stop
fi

if [[ -n "$FAILED_CONTAINER" && "$FAILED_CONTAINER" != "$QWEN_CONTAINER_NAME" ]] \
  && docker inspect --format '{{.State.Running}}' "$FAILED_CONTAINER" \
    2>/dev/null | grep -qx true; then
  failed_working_dir="$(
    docker inspect --format \
      '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
      "$FAILED_CONTAINER"
  )"
  if [[ "$failed_working_dir" != "$ROOT_DIR" ]]; then
    printf 'Refusing to stop a failed runtime outside this project: %s\n' \
      "$FAILED_CONTAINER" >&2
    exit 1
  fi
  docker stop --time 120 "$FAILED_CONTAINER" >/dev/null
fi

if [[ "$RESTORE_PRODUCTION" == false ]]; then
  if docker inspect --format '{{.State.Running}}' "$QWEN_CONTAINER_NAME" \
    2>/dev/null | grep -qx true; then
    "$ROOT_DIR/scripts/runtime.sh" stop
  fi
  printf 'Runtime cleanup completed; production was not running before the transaction.\n'
  exit 0
fi

# A v2 transaction recovery must recreate the recorded profile even if the
# failed replacement currently answers /health. Healthy is not acceptance and
# cannot supersede the pre-transaction runtime identity.
if [[ -n "${QWEN_CONTROL_TRANSACTION_ID:-}" ]]; then
  exec "$ROOT_DIR/scripts/runtime.sh" profile "$PROFILE"
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
