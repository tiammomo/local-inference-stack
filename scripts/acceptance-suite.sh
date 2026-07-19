#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELPORT_DIR="${MODELPORT_PROJECT_DIR:-/home/tiammomo/projects/dev/ModelPort}"
MODE="quick"
RECORD="true"
STARTED_AT="$(date --iso-8601=seconds)"
STARTED_EPOCH="$(date +%s)"
CURRENT_STEP="initialization"

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
  local finished_at finished_epoch duration git_commit compose_sha contract_sha manifest_sha image_ref
  finished_at="$(date --iso-8601=seconds)"
  finished_epoch="$(date +%s)"
  duration=$((finished_epoch - STARTED_EPOCH))
  git_commit="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || printf 'uncommitted')"
  compose_sha="$(sha256sum "$ROOT_DIR/compose.yaml" | cut -d' ' -f1)"
  contract_sha="$(sha256sum "$ROOT_DIR/contracts/local-qwen-provider-v1.json" | cut -d' ' -f1)"
  manifest_sha="$(sha256sum "$ROOT_DIR/deployments/qwen3.5-9b-rtx5070ti/manifest.json" | cut -d' ' -f1)"
  image_ref="$(docker inspect qwen35-9b-q5km --format '{{.Config.Image}}' 2>/dev/null || printf 'unavailable')"
  printf '{\n' >"$RECORD_BASE.json"
  printf '  "schemaVersion": 1,\n' >>"$RECORD_BASE.json"
  printf '  "mode": "%s",\n' "$MODE" >>"$RECORD_BASE.json"
  printf '  "status": "%s",\n' "$([[ $status -eq 0 ]] && printf passed || printf failed)" >>"$RECORD_BASE.json"
  printf '  "exitCode": %d,\n' "$status" >>"$RECORD_BASE.json"
  printf '  "failedAtStep": "%s",\n' "$CURRENT_STEP" >>"$RECORD_BASE.json"
  printf '  "startedAt": "%s",\n' "$STARTED_AT" >>"$RECORD_BASE.json"
  printf '  "finishedAt": "%s",\n' "$finished_at" >>"$RECORD_BASE.json"
  printf '  "durationSeconds": %d,\n' "$duration" >>"$RECORD_BASE.json"
  printf '  "gitCommit": "%s",\n' "$git_commit" >>"$RECORD_BASE.json"
  printf '  "runtimeImage": "%s",\n' "$image_ref" >>"$RECORD_BASE.json"
  printf '  "configuration": {\n' >>"$RECORD_BASE.json"
  printf '    "composeSha256": "%s",\n' "$compose_sha" >>"$RECORD_BASE.json"
  printf '    "contractSha256": "%s",\n' "$contract_sha" >>"$RECORD_BASE.json"
  printf '    "manifestSha256": "%s"\n' "$manifest_sha" >>"$RECORD_BASE.json"
  printf '  },\n' >>"$RECORD_BASE.json"
  printf '  "privacy": "synthetic acceptance traffic only; log file mode 0600"\n' >>"$RECORD_BASE.json"
  printf '}\n' >>"$RECORD_BASE.json"
  chmod 600 "$RECORD_BASE.json" "$RECORD_BASE.log"
  printf '\nAcceptance evidence: %s.json\n' "$RECORD_BASE"
  return "$status"
}

trap record_exit EXIT

usage() {
  printf 'Usage: %s {quick|standard|full}\n' "$0"
  printf '  quick     health, generation, reasoning, ModelPort, token count, dashboard\n'
  printf '  standard  quick + artifacts, reasoning adapter, provider matrix, Tool Use\n'
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
  run_step "Runtime status" "$ROOT_DIR/scripts/runtime.sh" status
  run_step "Direct generation" "$ROOT_DIR/scripts/smoke-test.sh"
  run_step "Direct reasoning" "$ROOT_DIR/scripts/reasoning-smoke.sh"
  run_step "ModelPort Messages" "$ROOT_DIR/scripts/modelport-smoke.sh"
  run_step "Exact token counting" "$ROOT_DIR/scripts/modelport-token-count-smoke.sh"
  run_step "ModelPort context admission" \
    "$ROOT_DIR/scripts/modelport-context-admission-smoke.sh"
  run_step "Operations dashboard" \
    curl --noproxy '*' -fsS http://127.0.0.1:33004/api/health
  printf '\n'
}

standard_suite() {
  quick_suite
  run_step "Artifact integrity" "$ROOT_DIR/scripts/verify-models.sh" --full --cached
  run_step "ModelPort reasoning mapping" \
    "$ROOT_DIR/scripts/modelport-reasoning-smoke.sh"
  run_step "ModelPort provider matrix" \
    "$MODELPORT_DIR/scripts/provider-matrix.sh" --model qwen3.5-code
  run_step "ModelPort Tool Use" \
    "$MODELPORT_DIR/scripts/tool-use-acceptance.sh" --upstream --max-tokens 2048
  run_step "Closed-loop Tool Use smoke" \
    python3 "$ROOT_DIR/scripts/tool-workflow-eval.py" --smoke
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
