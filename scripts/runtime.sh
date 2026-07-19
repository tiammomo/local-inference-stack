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
  local args=(--env-file "$PROFILE_DIR/latency.env")
  if [[ -f "$LOCAL_PROFILE" ]]; then
    args+=(--env-file "$LOCAL_PROFILE")
  fi
  docker compose "${args[@]}" "$@"
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
  if ! docker network inspect "$MODELPORT_NETWORK_NAME" >/dev/null 2>&1; then
    docker network create "$MODELPORT_NETWORK_NAME" >/dev/null
    printf 'Created shared runtime network: %s\n' "$MODELPORT_NETWORK_NAME"
  fi
  local compose_args=(--env-file "$profile_file")
  if [[ -f "$LOCAL_PROFILE" ]]; then
    compose_args+=(--env-file "$LOCAL_PROFILE")
  fi
  if [[ "$force_recreate" == "true" ]]; then
    docker compose "${compose_args[@]}" up -d --force-recreate qwen35
  else
    docker compose "${compose_args[@]}" up -d qwen35
  fi
  printf 'Activated Qwen runtime profile: %s\n' "$profile"
}

cd "$ROOT_DIR"

case "$ACTION" in
  start)
    apply_profile "$PROFILE" false
    ;;
  profile)
    apply_profile "$PROFILE" true
    ;;
  stop)
    compose stop
    ;;
  restart)
    compose restart qwen35
    ;;
  status)
    compose ps
    curl --noproxy '*' -fsS http://127.0.0.1:18080/health || true
    printf '\n'
    curl --noproxy '*' -fsS http://127.0.0.1:18080/slots \
      | python3 -c 'import json,sys; x=json.load(sys.stdin); p="latency" if len(x)==1 else "throughput" if len(x)==2 else "custom"; print("profile=%s slots=%s n_ctx_per_slot=%s" % (p, len(x), [s.get("n_ctx") for s in x]))' \
      || true
    nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw --format=csv,noheader
    ;;
  logs)
    compose logs --tail=200 -f qwen35
    ;;
  *)
    printf 'Usage: %s {start [latency|throughput]|profile {latency|throughput}|stop|restart|status|logs}\n' "$0" >&2
    exit 2
    ;;
esac
