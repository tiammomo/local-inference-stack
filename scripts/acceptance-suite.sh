#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELPORT_DIR="${MODELPORT_PROJECT_DIR:-}"
MODE="quick"
RECORD="true"
ACCEPTANCE_PROFILE="${QWEN_ACCEPTANCE_PROFILE:-latency}"
STARTED_AT="$(date --iso-8601=seconds)"
STARTED_EPOCH="$(date +%s)"
CURRENT_STEP="initialization"

# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"
load_deployment_env "$ROOT_DIR"

while [[ $# -gt 0 ]]; do
  case "$1" in
    quick|standard|full)
      MODE="$1"
      ;;
    --no-record)
      RECORD="false"
      ;;
    -h|--help|help)
      MODE="help"
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "$ACCEPTANCE_PROFILE" != "latency" ]]; then
  printf 'Host acceptance evidence is restricted to the validated latency profile.\n' >&2
  exit 2
fi

if [[ "$RECORD" == "true" && "$MODE" != "help" ]]; then
  umask 077
  RECORD_DIR="$ROOT_DIR/logs/acceptance"
  mkdir -p "$RECORD_DIR"
  RECORD_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  RECORD_BASE="$RECORD_DIR/$RECORD_STAMP-$MODE"
  exec > >(tee "$RECORD_BASE.log") 2>&1
fi

record_exit() {
  local status=$?
  if [[ "$RECORD" != "true" || "$MODE" == "help" ]]; then
    return "$status"
  fi
  set +e
  local finished_at finished_epoch duration result_status
  finished_at="$(date --iso-8601=seconds)"
  finished_epoch="$(date +%s)"
  duration=$((finished_epoch - STARTED_EPOCH))
  result_status="$([[ $status -eq 0 ]] && printf passed || printf failed)"
  if ! python3 "$ROOT_DIR/scripts/acceptance-evidence.py" \
    --output "$RECORD_BASE.json" \
    --mode "$MODE" \
    --status "$result_status" \
    --exit-code "$status" \
    --failed-at-step "$CURRENT_STEP" \
    --started-at "$STARTED_AT" \
    --finished-at "$finished_at" \
    --duration-seconds "$duration" \
    --catalog-model-id "$QWEN_CATALOG_ID" \
    --profile "$ACCEPTANCE_PROFILE"; then
    printf 'Failed to write acceptance evidence.\n' >&2
    [[ $status -ne 0 ]] || status=1
  fi
  chmod 600 "$RECORD_BASE.log"
  printf '\nAcceptance evidence: %s.json\n' "$RECORD_BASE"
  return "$status"
}

trap record_exit EXIT

usage() {
  printf 'Usage: %s {quick|standard|full}\n' "$0"
  printf '  quick     local tests, runtime health, generation, and reasoning\n'
  printf '  standard  quick + ModelPort contract, token count, dashboard, Tool Use\n'
  printf '  full      standard + 118K/92K context and performance benchmarks\n'
}

run_step() {
  local name="$1"
  shift
  CURRENT_STEP="$name"
  printf '\n[%s] %s\n' "$(date --iso-8601=seconds)" "$name"
  "$@"
}

quick_suite() {
  run_step "Local unit tests" "$ROOT_DIR/scripts/unit-tests.sh"
  run_step "Artifact integrity" "$ROOT_DIR/scripts/verify-models.sh" --active --cached
  run_step "Runtime status" "$ROOT_DIR/scripts/runtime.sh" status
  run_step "Canonical runtime profile" \
    "$ROOT_DIR/scripts/runtime.sh" assert-profile "$ACCEPTANCE_PROFILE"
  run_step "Direct generation" "$ROOT_DIR/scripts/smoke-test.sh"
  run_step "Direct reasoning" "$ROOT_DIR/scripts/reasoning-smoke.sh"
}

standard_suite() {
  if [[ "$QWEN_CATALOG_ID" != "qwen35-9b-q5km" ]]; then
    printf 'standard currently validates the versioned ModelPort contract for qwen35-9b-q5km; selected=%s\n' "$QWEN_CATALOG_ID" >&2
    exit 2
  fi
  if [[ -z "$MODELPORT_DIR" || ! -x "$MODELPORT_DIR/scripts/provider-matrix.sh" ]]; then
    printf 'standard requires MODELPORT_PROJECT_DIR pointing to a compatible ModelPort checkout.\n' >&2
    exit 2
  fi
  if ! command -v node >/dev/null 2>&1; then
    printf 'standard requires Linux Node.js on PATH for ModelPort checks (Node 24 recommended).\n' >&2
    printf 'With NVM, run: source "${NVM_DIR:-$HOME/.nvm}/nvm.sh" && nvm use 24\n' >&2
    exit 2
  fi
  quick_suite
  run_step "Cross-repository provider contract" \
    python3 "$ROOT_DIR/scripts/compatibility-check.py" \
      --modelport-project "$MODELPORT_DIR"
  run_step "ModelPort Messages" "$ROOT_DIR/scripts/modelport-smoke.sh"
  run_step "Exact token counting" "$ROOT_DIR/scripts/modelport-token-count-smoke.sh"
  run_step "ModelPort context admission" \
    "$ROOT_DIR/scripts/modelport-context-admission-smoke.sh"
  run_step "Operations dashboard" \
    python3 "$ROOT_DIR/scripts/dashboard-smoke.py"
  printf '\n'
  run_step "ModelPort reasoning mapping" \
    "$ROOT_DIR/scripts/modelport-reasoning-smoke.sh"
  run_step "ModelPort provider matrix" \
    "$MODELPORT_DIR/scripts/provider-matrix.sh" --model qwen3.5-code
  run_step "ModelPort Tool Use" \
    "$MODELPORT_DIR/scripts/tool-use-acceptance.sh" --upstream --max-tokens 2048
  run_step "Closed-loop Tool Use smoke" \
    python3 "$ROOT_DIR/scripts/tool-workflow-eval.py" --smoke
  run_step "Tool resilience smoke" \
    python3 "$ROOT_DIR/scripts/tool-workflow-eval.py" \
      --cases "$ROOT_DIR/quality/tool-resilience-workflows.json" --smoke
  run_step "Synthetic quality smoke" \
    python3 "$ROOT_DIR/scripts/quality-eval.py" --smoke
}

full_suite() {
  standard_suite
  run_step "Full artifact rehash" "$ROOT_DIR/scripts/verify-models.sh" --full
  run_step "118K direct context" python3 "$ROOT_DIR/scripts/context-acceptance.py"
  run_step "92K ModelPort reasoning context" \
    "$ROOT_DIR/scripts/modelport-context-acceptance.sh"
  run_step "Decode benchmark" python3 "$ROOT_DIR/scripts/decode-benchmark.py"
  run_step "Concurrency benchmark" \
    python3 "$ROOT_DIR/scripts/concurrency-benchmark.py"
  run_step "Repeated synthetic quality suite" \
    python3 "$ROOT_DIR/scripts/quality-eval.py" --trials 3
  run_step "Forty-case closed-loop Tool Use suite" \
    python3 "$ROOT_DIR/scripts/tool-workflow-eval.py"
  run_step "Multi-step and adversarial Tool Use suite" \
    python3 "$ROOT_DIR/scripts/tool-workflow-eval.py" \
      --cases "$ROOT_DIR/quality/tool-resilience-workflows.json"
}

case "$MODE" in
  quick)
    quick_suite
    ;;
  standard)
    standard_suite
    ;;
  full)
    full_suite
    ;;
  help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
