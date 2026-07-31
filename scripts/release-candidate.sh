#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-quick}"
PRODUCTION_WAS_RUNNING=false
PRODUCTION_PROFILE=latency
CANDIDATE_STARTED=false
CANDIDATE_ACCEPTANCE_PASSED=false
umask 077
RELEASE_LOG_DIR="$ROOT_DIR/logs/releases"
mkdir -p "$RELEASE_LOG_DIR"
RELEASE_LOG="$RELEASE_LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-candidate-$MODE.log"
exec > >(tee "$RELEASE_LOG") 2>&1

if [[ "$MODE" != "quick" && "$MODE" != "long" ]]; then
  printf 'Usage: %s {quick|long}\n' "$0" >&2
  exit 2
fi

# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"
load_deployment_env "$ROOT_DIR"
acquire_runtime_lock "$ROOT_DIR"

MODELPORT_DIR="${MODELPORT_PROJECT_DIR:-}"
if [[ -z "$MODELPORT_DIR" || ! -x "$MODELPORT_DIR/scripts/tool-use-acceptance.sh" ]]; then
  printf 'MODELPORT_PROJECT_DIR must point to a compatible checkout before downtime begins.\n' >&2
  exit 2
fi
python3 "$ROOT_DIR/scripts/compatibility-check.py" \
  --modelport-project "$MODELPORT_DIR"

recover() {
  local status=$?
  local recovery_status=0
  trap - EXIT INT TERM
  set +e
  if [[ "$CANDIDATE_STARTED" == true ]]; then
    "$ROOT_DIR/scripts/candidate-runtime.sh" stop || recovery_status=1
  fi
  if [[ "$PRODUCTION_WAS_RUNNING" == true ]]; then
    "$ROOT_DIR/scripts/runtime.sh" start "$PRODUCTION_PROFILE" \
      || recovery_status=1
  fi
  if [[ "$status" -eq 0 && "$recovery_status" -ne 0 ]]; then
    status="$recovery_status"
  fi
  if [[ "$status" -eq 0 && "$CANDIDATE_ACCEPTANCE_PASSED" == true ]]; then
    printf 'Candidate acceptance passed and production recovery completed.\n'
  else
    printf 'Candidate workflow failed with status %s; recovery status=%s.\n' \
      "$status" "$recovery_status" >&2
  fi
  printf 'Release evidence: %s\n' "$RELEASE_LOG"
  exit "$status"
}
trap recover EXIT INT TERM

cd "$ROOT_DIR"
if docker inspect --format '{{.State.Running}}' "$QWEN_CONTAINER_NAME" \
  2>/dev/null | grep -qx true; then
  PRODUCTION_WAS_RUNNING=true
  PRODUCTION_PROFILE="$(
    curl --noproxy '*' -fsS http://127.0.0.1:18080/slots \
      | python3 -c 'import json,sys; slots=json.load(sys.stdin); count=len(slots); count in (1, 2) or sys.exit(f"unsupported production slot count: {count}"); print("throughput" if count == 2 else "latency")'
  )"
fi

"$ROOT_DIR/scripts/verify-models.sh" --active --cached
"$ROOT_DIR/scripts/runtime.sh" stop
CANDIDATE_STARTED=true
"$ROOT_DIR/scripts/candidate-runtime.sh" start
"$ROOT_DIR/scripts/candidate-runtime.sh" accept

if [[ "$MODE" == "long" ]]; then
  LLAMA_BASE_URL=http://127.0.0.1:18081 \
    python3 "$ROOT_DIR/scripts/context-acceptance.py"
  LLAMA_BASE_URL=http://127.0.0.1:18081 DECODE_CONTEXT_TOKENS=92000 \
    python3 "$ROOT_DIR/scripts/decode-benchmark.py"
  LLAMA_BASE_URL=http://127.0.0.1:18081 \
    "$ROOT_DIR/scripts/modelport-context-acceptance.sh"
  python3 "$ROOT_DIR/scripts/quality-eval.py" --trials 3
fi

CANDIDATE_ACCEPTANCE_PASSED=true
