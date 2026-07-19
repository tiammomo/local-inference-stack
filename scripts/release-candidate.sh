#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-quick}"
PRODUCTION_WAS_RUNNING=false
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

recover() {
  local status=$?
  local recovery_status=0
  local attempt
  trap - EXIT INT TERM
  set +e
  if [[ "$CANDIDATE_STARTED" == true ]]; then
    "$ROOT_DIR/scripts/candidate-runtime.sh" stop || recovery_status=1
  fi
  if [[ "$PRODUCTION_WAS_RUNNING" == true ]]; then
    "$ROOT_DIR/scripts/runtime.sh" start latency || recovery_status=1
    for attempt in $(seq 1 180); do
      if curl --noproxy '*' -fsS http://127.0.0.1:18080/health >/dev/null 2>&1; then
        break
      fi
      sleep 2
    done
    if ! curl --noproxy '*' -fsS http://127.0.0.1:18080/health >/dev/null 2>&1; then
      printf 'Production did not recover at http://127.0.0.1:18080.\n' >&2
      recovery_status=1
    fi
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
if docker inspect --format '{{.State.Running}}' qwen35-9b-q5km 2>/dev/null | grep -qx true; then
  PRODUCTION_WAS_RUNNING=true
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
