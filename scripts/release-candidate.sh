#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="quick"
PRODUCTION_WAS_RUNNING=false
PRODUCTION_PROFILE=latency
CANDIDATE_STARTED=false
CANDIDATE_ACCEPTANCE_PASSED=false
RELEASE_LOG=""
RECOVERY_FAILURE_EXIT=70

release_result_status() {
  local workflow_status="$1"
  local recovery_status="$2"
  if [[ "$recovery_status" -ne 0 ]]; then
    printf '%s\n' "$RECOVERY_FAILURE_EXIT"
  else
    printf '%s\n' "$workflow_status"
  fi
}

recover() {
  local workflow_status="${1:-$?}"
  local recovery_status=0
  local final_status
  trap - EXIT INT TERM
  set +e
  if [[ "$CANDIDATE_STARTED" == true ]]; then
    "$ROOT_DIR/scripts/candidate-runtime.sh" stop || recovery_status=$?
  fi
  if [[ "$PRODUCTION_WAS_RUNNING" == true ]]; then
    "$ROOT_DIR/scripts/runtime.sh" start "$PRODUCTION_PROFILE" \
      || recovery_status=$?
  fi
  final_status="$(release_result_status "$workflow_status" "$recovery_status")"
  if [[ "$final_status" -eq 0 && "$CANDIDATE_ACCEPTANCE_PASSED" == true ]]; then
    printf 'Candidate acceptance passed and production recovery completed.\n'
  else
    printf 'Candidate workflow failed with status %s; recovery status=%s; final status=%s.\n' \
      "$workflow_status" "$recovery_status" "$final_status" >&2
  fi
  if [[ -n "$RELEASE_LOG" ]]; then
    printf 'Release evidence: %s\n' "$RELEASE_LOG"
  fi
  exit "$final_status"
}

install_release_traps() {
  trap 'recover $?' EXIT
  trap 'recover 130' INT
  trap 'recover 143' TERM
}

require_catalog_deployment_eligible() {
  local plan
  if ! plan="$(
    "$ROOT_DIR/scripts/model-manager.py" plan \
      --model "$QWEN_CATALOG_ID" --json
  )"; then
    printf 'Cannot assess catalog release eligibility for %s.\n' \
      "$QWEN_CATALOG_ID" >&2
    return 3
  fi
  if ! python3 -c '
import json
import sys

plan = json.load(sys.stdin)
if plan.get("catalogDeploymentEligible") is not True:
    raise SystemExit(1)
' <<<"$plan"; then
    printf 'Candidate release blocked: catalog entry %s is not deployment-eligible.\n' \
      "$QWEN_CATALOG_ID" >&2
    return 3
  fi
}

main() {
  MODE="${1:-quick}"
  if [[ "$MODE" != "quick" && "$MODE" != "long" ]]; then
    printf 'Usage: %s {quick|long}\n' "$0" >&2
    return 2
  fi

  umask 077
  local release_log_dir="$ROOT_DIR/logs/releases"
  mkdir -p "$release_log_dir"
  RELEASE_LOG="$release_log_dir/$(date -u +%Y%m%dT%H%M%SZ)-candidate-$MODE.log"
  exec > >(tee "$RELEASE_LOG") 2>&1

  # shellcheck source=scripts/lib/deployment.sh
  source "$ROOT_DIR/scripts/lib/deployment.sh"
  load_deployment_env "$ROOT_DIR"
  acquire_runtime_lock "$ROOT_DIR"

  local modelport_dir="${MODELPORT_PROJECT_DIR:-}"
  if [[ -z "$modelport_dir" || ! -x "$modelport_dir/scripts/tool-use-acceptance.sh" ]]; then
    printf 'MODELPORT_PROJECT_DIR must point to a compatible checkout before downtime begins.\n' >&2
    return 2
  fi
  python3 "$ROOT_DIR/scripts/compatibility-check.py" \
    --modelport-project "$modelport_dir"
  require_catalog_deployment_eligible

  install_release_traps

  cd "$ROOT_DIR"
  if docker inspect --format '{{.State.Running}}' "$QWEN_CONTAINER_NAME" \
    2>/dev/null | grep -qx true; then
    PRODUCTION_WAS_RUNNING=true
    PRODUCTION_PROFILE="$(
      curl --noproxy '*' -fsS http://127.0.0.1:18080/slots \
        | python3 -c 'import json,sys; slots=json.load(sys.stdin); count=len(slots); count in (1, 2) or sys.exit(f"unsupported production slot count: {count}"); print("throughput" if count == 2 else "latency")'
    )"
  fi

  "$ROOT_DIR/scripts/model-manager.py" verify --cached
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
      python3 "$ROOT_DIR/scripts/concurrency-benchmark.py"
    LLAMA_BASE_URL=http://127.0.0.1:18081 \
      "$ROOT_DIR/scripts/modelport-context-acceptance.sh"
    python3 "$ROOT_DIR/scripts/quality-eval.py" --trials 3
  fi

  CANDIDATE_ACCEPTANCE_PASSED=true
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
