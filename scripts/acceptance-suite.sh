#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELPORT_DIR="${MODELPORT_PROJECT_DIR:-}"
MODE="quick"
RECORD="true"
BASELINE_ONLY="false"
PERFORMANCE_ARGS=()
ACCEPTANCE_PROFILE="${QWEN_ACCEPTANCE_PROFILE:-latency}"
STARTED_AT="$(date --iso-8601=seconds)"
STARTED_EPOCH="$(date +%s)"
CURRENT_STEP="initialization"
RUN_MANIFEST=""
ACCEPTANCE_RUNNER_TOKEN=""
PERFORMANCE_STARTED_AT=""
PERFORMANCE_FINISHED_AT=""
PERFORMANCE_DURATION="0"

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
    --baseline-only)
      BASELINE_ONLY="true"
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

if [[ "$BASELINE_ONLY" == "true" && "$MODE" != "full" ]]; then
  printf '%s\n' '--baseline-only is restricted to the full performance suite.' >&2
  exit 2
fi

if [[ "$BASELINE_ONLY" == "true" ]]; then
  PERFORMANCE_ARGS+=(--baseline-only)
  RECORD="false"
  printf '%s\n' \
    'Baseline-only mode collects private performance evidence and cannot write host acceptance evidence.'
fi

if [[ "$ACCEPTANCE_PROFILE" != "latency" ]]; then
  printf 'Host acceptance evidence is restricted to the validated latency profile.\n' >&2
  exit 2
fi

# A deploy-triggered quick smoke is part of the approved transaction, not host
# qualification.  Bind it to the current strict Catalog and selected profile
# before opening an evidence record or sending a model request.
assert_approved_catalog_spec "$ROOT_DIR" true

# A pending/failed performance policy is a pre-admission failure: it must stop a
# normal full run before acceptance evidence is opened or any model request runs.
if [[ "$MODE" == "full" ]]; then
  CURRENT_STEP="Performance policy preflight"
  PERFORMANCE_STARTED_AT="$(date --iso-8601=seconds)"
  performance_started_epoch="$(date +%s)"
  printf '\n[%s] %s\n' "$PERFORMANCE_STARTED_AT" "$CURRENT_STEP"
  set +e
  python3 "$ROOT_DIR/src/local_inference_stack/performance.py" \
    "${PERFORMANCE_ARGS[@]}"
  performance_status=$?
  set -e
  PERFORMANCE_FINISHED_AT="$(date --iso-8601=seconds)"
  performance_finished_epoch="$(date +%s)"
  PERFORMANCE_DURATION=$((performance_finished_epoch - performance_started_epoch))
  if [[ $performance_status -ne 0 ]]; then
    exit "$performance_status"
  fi
fi

if [[ "$RECORD" == "true" && "$MODE" != "help" ]]; then
  umask 077
  RECORD_DIR="$ROOT_DIR/logs/acceptance"
  mkdir -p "$RECORD_DIR"
  RECORD_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  RECORD_BASE="$RECORD_DIR/$RECORD_STAMP-$MODE"
  RUN_MANIFEST="$RECORD_BASE.run.json"
  ACCEPTANCE_RUNNER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  exec > >(tee "$RECORD_BASE.log") 2>&1
  LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN="$ACCEPTANCE_RUNNER_TOKEN" \
    python3 "$ROOT_DIR/scripts/acceptance-evidence.py" run-start \
    --output "$RUN_MANIFEST" \
    --mode "$MODE" \
    --catalog-model-id "$QWEN_CATALOG_ID" \
    --profile "$ACCEPTANCE_PROFILE" \
    --started-at "$STARTED_AT"
fi

record_exit() {
  local status=$?
  if [[ "$RECORD" != "true" || "$MODE" == "help" ]]; then
    return "$status"
  fi
  set +e
  local finished_at finished_epoch duration
  finished_at="$(date --iso-8601=seconds)"
  finished_epoch="$(date +%s)"
  duration=$((finished_epoch - STARTED_EPOCH))
  if ! LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN="$ACCEPTANCE_RUNNER_TOKEN" \
    python3 "$ROOT_DIR/scripts/acceptance-evidence.py" run-finish \
    --manifest "$RUN_MANIFEST" \
    --finished-at "$finished_at" \
    --duration-seconds "$duration" \
    --exit-code "$status" \
    --failed-at-step "$CURRENT_STEP"; then
    printf 'Failed to finalize acceptance run manifest.\n' >&2
    [[ $status -ne 0 ]] || status=1
  elif ! LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN="$ACCEPTANCE_RUNNER_TOKEN" \
    python3 "$ROOT_DIR/scripts/acceptance-evidence.py" write \
    --output "$RECORD_BASE.json" \
    --run-manifest "$RUN_MANIFEST"; then
    printf 'Failed to write acceptance evidence.\n' >&2
    [[ $status -ne 0 ]] || status=1
  fi
  chmod 600 "$RECORD_BASE.log"
  printf '\nAcceptance evidence: %s.json\n' "$RECORD_BASE"
  return "$status"
}

trap record_exit EXIT

usage() {
  printf 'Usage: %s {quick|standard|full} [--no-record] [--baseline-only]\n' "$0"
  printf '  quick     local tests, runtime health, generation, and reasoning\n'
  printf '  standard  quick + ModelPort contract, token count, dashboard, Tool Use\n'
  printf '  full      standard + 118K/92K context and performance benchmarks\n'
  printf '  --baseline-only  full-only, private non-promotable baseline collection\n'
}

run_step() {
  local name="$1"
  shift
  local step_started_at step_started_epoch step_finished_at step_finished_epoch
  local step_duration step_status
  CURRENT_STEP="$name"
  step_started_at="$(date --iso-8601=seconds)"
  step_started_epoch="$(date +%s)"
  printf '\n[%s] %s\n' "$step_started_at" "$name"
  # A rollout quick smoke spans several external processes.  Recheck the
  # persisted transaction/spec/action immediately before and after every step
  # so a fenced, replaced, or advanced transaction cannot inherit a passing
  # result from work performed for another subject.  Outside a transaction the
  # helper is intentionally a no-op.
  assert_approved_catalog_spec "$ROOT_DIR" true
  set +e
  "$@"
  step_status=$?
  set -e
  assert_approved_catalog_spec "$ROOT_DIR" true
  step_finished_at="$(date --iso-8601=seconds)"
  step_finished_epoch="$(date +%s)"
  step_duration=$((step_finished_epoch - step_started_epoch))
  if [[ "$RECORD" == "true" ]]; then
    LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN="$ACCEPTANCE_RUNNER_TOKEN" \
      python3 "$ROOT_DIR/scripts/acceptance-evidence.py" run-step \
      --manifest "$RUN_MANIFEST" \
      --name "$name" \
      --started-at "$step_started_at" \
      --finished-at "$step_finished_at" \
      --duration-seconds "$step_duration" \
      --exit-code "$step_status"
  fi
  return "$step_status"
}

quick_suite() {
  run_step "Local unit tests" \
    python3 -m unittest discover -s "$ROOT_DIR/tests" -p 'test_*.py' -v
  run_step "Artifact integrity" \
    "$ROOT_DIR/scripts/model-manager.py" verify --cached
  run_step "Runtime status" "$ROOT_DIR/scripts/runtime.sh" status
  run_step "Canonical runtime profile" \
    "$ROOT_DIR/scripts/runtime.sh" assert-profile "$ACCEPTANCE_PROFILE"
  run_step "Direct generation" "$ROOT_DIR/scripts/smoke-test.sh"
  run_step "Direct reasoning" "$ROOT_DIR/scripts/reasoning-smoke.sh"
}

standard_suite() {
  local node_path node_major modelport_endpoint
  if [[ "$QWEN_CATALOG_ID" != "qwen35-9b-q5km" ]]; then
    printf 'standard currently validates the versioned ModelPort contract for qwen35-9b-q5km; selected=%s\n' "$QWEN_CATALOG_ID" >&2
    exit 2
  fi
  if [[ -z "$MODELPORT_DIR" || ! -x "$MODELPORT_DIR/scripts/provider-matrix.sh" ]]; then
    printf 'standard requires MODELPORT_PROJECT_DIR pointing to a compatible ModelPort checkout.\n' >&2
    exit 2
  fi
  if ! command -v node >/dev/null 2>&1; then
    printf 'standard requires Linux Node.js 24 on PATH for ModelPort checks.\n' >&2
    printf '%s\n' "With NVM, run: source \"\${NVM_DIR:-\$HOME/.nvm}/nvm.sh\" && nvm use 24" >&2
    exit 2
  fi
  node_path="$(command -v node)"
  case "$node_path" in
    /mnt/*|*.exe)
      printf 'standard requires a Linux Node.js binary; resolved=%s\n' "$node_path" >&2
      exit 2
      ;;
  esac
  node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
  if [[ "$node_major" != "24" ]]; then
    printf 'standard requires the project-tested Node.js 24 major; resolved=%s (%s).\n' \
      "$node_path" "$(node --version 2>/dev/null || printf unknown)" >&2
    printf '%s\n' "With NVM, run: source \"\${NVM_DIR:-\$HOME/.nvm}/nvm.sh\" && nvm install && nvm use" >&2
    exit 2
  fi
  modelport_endpoint="${MODELPORT_BASE_URL:-${ANTHROPIC_BASE_URL:-http://127.0.0.1:38082}}"
  if ! curl --noproxy '*' --fail --silent --show-error \
    --connect-timeout 3 --max-time 10 "$modelport_endpoint/livez" >/dev/null; then
    printf 'standard requires a healthy compatible ModelPort at %s.\n' "$modelport_endpoint" >&2
    exit 2
  fi
  run_step "Operations dashboard preflight" \
    python3 "$ROOT_DIR/scripts/dashboard-smoke.py"
  quick_suite
  run_step "Cross-repository provider contract" \
    python3 "$ROOT_DIR/scripts/compatibility-check.py" \
      --modelport-project "$MODELPORT_DIR"
  run_step "ModelPort Messages" "$ROOT_DIR/scripts/modelport-smoke.sh"
  run_step "Exact token counting" "$ROOT_DIR/scripts/modelport-token-count-smoke.sh"
  run_step "ModelPort context admission" \
    "$ROOT_DIR/scripts/modelport-context-admission-smoke.sh"
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
  if [[ "$RECORD" == "true" ]]; then
    LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN="$ACCEPTANCE_RUNNER_TOKEN" \
      python3 "$ROOT_DIR/scripts/acceptance-evidence.py" run-step \
      --manifest "$RUN_MANIFEST" \
      --name "Performance policy preflight" \
      --started-at "$PERFORMANCE_STARTED_AT" \
      --finished-at "$PERFORMANCE_FINISHED_AT" \
      --duration-seconds "$PERFORMANCE_DURATION" \
      --exit-code 0
  fi
  standard_suite
  run_step "Full artifact rehash" \
    "$ROOT_DIR/scripts/model-manager.py" verify --full
  run_step "118K direct context" python3 "$ROOT_DIR/scripts/context-acceptance.py"
  run_step "92K ModelPort reasoning context" \
    "$ROOT_DIR/scripts/modelport-context-acceptance.sh"
  run_step "Decode benchmark" \
    python3 "$ROOT_DIR/scripts/decode-benchmark.py" "${PERFORMANCE_ARGS[@]}"
  run_step "Concurrency benchmark" \
    python3 "$ROOT_DIR/scripts/concurrency-benchmark.py" "${PERFORMANCE_ARGS[@]}"
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
