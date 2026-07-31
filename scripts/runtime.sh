#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-status}"
PROFILE="${2:-latency}"
PROFILE_DIR="$ROOT_DIR/profiles"
LOCAL_PROFILE="$PROFILE_DIR/deployment.local.env"

# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"
load_deployment_env "$ROOT_DIR"

compose() {
  local args=()
  if [[ -f "$LOCAL_PROFILE" ]]; then
    args+=(--env-file "$LOCAL_PROFILE")
  fi
  args+=(--env-file "$PROFILE_DIR/latency.env")
  run_clean_compose "$ROOT_DIR" "${args[@]}" "$@"
}

is_running() {
  docker inspect --format '{{.State.Running}}' "$QWEN_CONTAINER_NAME" \
    2>/dev/null | grep -qx true
}

wait_healthy() {
  local attempts="${QWEN_START_ATTEMPTS:-180}"
  local interval="${QWEN_START_INTERVAL_SECONDS:-2}"
  [[ "$attempts" =~ ^[1-9][0-9]{0,3}$ ]] || {
    printf 'QWEN_START_ATTEMPTS must be an integer in [1, 9999].\n' >&2
    return 2
  }
  [[ "$interval" =~ ^[1-9][0-9]{0,2}$ ]] || {
    printf 'QWEN_START_INTERVAL_SECONDS must be an integer in [1, 999].\n' >&2
    return 2
  }
  local attempt
  for attempt in $(seq 1 "$attempts"); do
    if curl --noproxy '*' --connect-timeout 1 --max-time 3 -fsS \
      http://127.0.0.1:18080/health >/dev/null 2>&1; then
      printf 'Qwen runtime is healthy at http://127.0.0.1:18080.\n'
      return 0
    fi
    if (( attempt % 15 == 0 )); then
      printf 'Waiting for Qwen runtime health (%s/%s)...\n' "$attempt" "$attempts"
    fi
    sleep "$interval"
  done
  printf 'Qwen runtime did not become healthy after %s attempts.\n' "$attempts" >&2
  return 1
}

apply_profile() {
  local profile="$1"
  local force_recreate="${2:-false}"
  local profile_file="$PROFILE_DIR/$profile.env"
  if [[ ! -f "$profile_file" ]]; then
    printf 'Unknown profile: %s\n' "$profile" >&2
    printf 'Available profiles: latency, throughput\n' >&2
    exit 2
  fi
  "$ROOT_DIR/scripts/verify-models.sh" --active --cached
  if ! is_running; then
    "$ROOT_DIR/scripts/model-manager.py" admit \
      --model "$QWEN_CATALOG_ID"
  fi
  if ! docker network inspect "$MODELPORT_NETWORK_NAME" >/dev/null 2>&1; then
    docker network create "$MODELPORT_NETWORK_NAME" >/dev/null
    printf 'Created shared runtime network: %s\n' "$MODELPORT_NETWORK_NAME"
  fi
  local compose_args=()
  if [[ -f "$LOCAL_PROFILE" ]]; then
    compose_args+=(--env-file "$LOCAL_PROFILE")
  fi
  compose_args+=(--env-file "$profile_file")
  if [[ "$force_recreate" == "true" ]]; then
    run_clean_compose "$ROOT_DIR" "${compose_args[@]}" \
      up -d --force-recreate qwen35
  else
    run_clean_compose "$ROOT_DIR" "${compose_args[@]}" up -d qwen35
  fi
  wait_healthy
  printf 'Activated Qwen runtime profile: %s\n' "$profile"
}

assert_profile() {
  local profile="$1"
  local expected_slots
  case "$profile" in
    latency) expected_slots=1 ;;
    throughput) expected_slots=2 ;;
    *)
      printf 'Unknown profile: %s\n' "$profile" >&2
      return 2
      ;;
  esac
  curl --noproxy '*' --connect-timeout 1 --max-time 3 -fsS \
    http://127.0.0.1:18080/health >/dev/null
  curl --noproxy '*' --connect-timeout 1 --max-time 3 -fsS \
    http://127.0.0.1:18080/slots \
    | python3 -c '
import json
import sys

expected = int(sys.argv[1])
slots = json.load(sys.stdin)
if not isinstance(slots, list) or len(slots) != expected:
    raise SystemExit(f"runtime slot mismatch: expected={expected}, actual={len(slots) if isinstance(slots, list) else None}")
contexts = [slot.get("n_ctx") for slot in slots]
print(f"runtime_slots={len(slots)} context_per_slot={contexts}")
' "$expected_slots"
  python3 - "$QWEN_CONTAINER_NAME" "$profile" <<'PY'
import sys
from scripts.runtime_identity import live_container, runtime_mismatches

container = live_container(sys.argv[1])
if not container:
    raise SystemExit(f"runtime container is missing: {sys.argv[1]}")
mismatches = runtime_mismatches(container, sys.argv[2])
if mismatches:
    raise SystemExit("runtime configuration drift: " + ", ".join(mismatches))
print("runtime_configuration=canonical")
PY
}

cd "$ROOT_DIR"

case "$ACTION" in
  start)
    acquire_runtime_lock "$ROOT_DIR"
    apply_profile "$PROFILE" false
    ;;
  profile)
    acquire_runtime_lock "$ROOT_DIR"
    apply_profile "$PROFILE" true
    ;;
  stop)
    acquire_runtime_lock "$ROOT_DIR"
    compose stop
    ;;
  restart)
    acquire_runtime_lock "$ROOT_DIR"
    "$ROOT_DIR/scripts/verify-models.sh" --active --cached
    compose restart qwen35
    wait_healthy
    ;;
  status)
    status=0
    compose ps
    curl --noproxy '*' --connect-timeout 1 --max-time 3 -fsS \
      http://127.0.0.1:18080/health || status=1
    printf '\n'
    curl --noproxy '*' --connect-timeout 1 --max-time 3 -fsS \
      http://127.0.0.1:18080/slots \
      | python3 -c 'import json,sys; x=json.load(sys.stdin); p="latency" if len(x)==1 else "throughput" if len(x)==2 else "custom"; print("profile=%s slots=%s n_ctx_per_slot=%s" % (p, len(x), [s.get("n_ctx") for s in x]))' \
      || status=1
    nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw --format=csv,noheader \
      || status=1
    exit "$status"
    ;;
  assert-profile)
    assert_profile "$PROFILE"
    ;;
  logs)
    compose logs --tail=200 -f qwen35
    ;;
  *)
    printf 'Usage: %s {start [latency|throughput]|profile {latency|throughput}|stop|restart|status|assert-profile {latency|throughput}|logs}\n' "$0" >&2
    exit 2
    ;;
esac
