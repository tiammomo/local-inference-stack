#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELPORT_DIR="${MODELPORT_PROJECT_DIR:-/home/tiammomo/projects/dev/ModelPort}"
PROFILE_FILE="$ROOT_DIR/profiles/candidate.env"
ACTION="${1:-status}"
PRODUCTION_CONTAINER="qwen35-9b-q5km"
CANDIDATE_CONTAINER="qwen35-9b-candidate"
CANDIDATE_URL="http://127.0.0.1:18081"

compose() {
  docker compose --env-file "$PROFILE_FILE" "$@"
}

is_running() {
  docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null | grep -qx true
}

wait_healthy() {
  local attempt
  for attempt in $(seq 1 180); do
    if curl --noproxy '*' -fsS "$CANDIDATE_URL/health" >/dev/null 2>&1; then
      printf 'Candidate is healthy at %s\n' "$CANDIDATE_URL"
      return 0
    fi
    sleep 2
  done
  printf 'Candidate did not become healthy within 360 seconds.\n' >&2
  return 1
}

cd "$ROOT_DIR"

case "$ACTION" in
  start)
    if is_running "$PRODUCTION_CONTAINER"; then
      printf 'Production is running. Stop it before starting the serial candidate:\n' >&2
      printf '  ./scripts/runtime.sh stop\n' >&2
      exit 2
    fi
    "$ROOT_DIR/scripts/verify-models.sh" --active --cached
    docker network inspect "${MODELPORT_NETWORK_NAME:-modelport_default}" >/dev/null
    mkdir -p "$ROOT_DIR/cache/candidate/slots"
    compose up -d qwen35
    wait_healthy
    ;;
  accept)
    if ! is_running "$CANDIDATE_CONTAINER"; then
      printf 'Candidate container is not running.\n' >&2
      exit 2
    fi
    LLAMA_BASE_URL="$CANDIDATE_URL" "$ROOT_DIR/scripts/smoke-test.sh"
    LLAMA_BASE_URL="$CANDIDATE_URL" "$ROOT_DIR/scripts/reasoning-smoke.sh"
    "$ROOT_DIR/scripts/modelport-smoke.sh"
    "$ROOT_DIR/scripts/modelport-reasoning-smoke.sh"
    QWEN_BASE_URL="$CANDIDATE_URL" "$ROOT_DIR/scripts/modelport-token-count-smoke.sh"
    "$ROOT_DIR/scripts/modelport-context-admission-smoke.sh"
    "$MODELPORT_DIR/scripts/tool-use-acceptance.sh" --upstream --max-tokens 2048
    python3 "$ROOT_DIR/scripts/quality-eval.py" --smoke
    ;;
  status)
    compose ps
    curl --noproxy '*' -fsS "$CANDIDATE_URL/health" || true
    printf '\n'
    ;;
  stop)
    compose down --remove-orphans
    ;;
  *)
    printf 'Usage: %s {start|accept|status|stop}\n' "$0" >&2
    exit 2
    ;;
esac
