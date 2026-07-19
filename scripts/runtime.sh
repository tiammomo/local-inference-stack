#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-status}"
PROFILE="${2:-latency}"
PROFILE_DIR="$ROOT_DIR/profiles"

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
  docker network inspect "${MODELPORT_NETWORK_NAME:-modelport_default}" >/dev/null
  if [[ "$force_recreate" == "true" ]]; then
    docker compose --env-file "$profile_file" up -d --force-recreate qwen35
  else
    docker compose --env-file "$profile_file" up -d qwen35
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
    docker compose stop
    ;;
  restart)
    docker compose restart qwen35
    ;;
  status)
    docker compose ps
    curl --noproxy '*' -fsS http://127.0.0.1:18080/health || true
    printf '\n'
    curl --noproxy '*' -fsS http://127.0.0.1:18080/slots \
      | python3 -c 'import json,sys; x=json.load(sys.stdin); p="latency" if len(x)==1 else "throughput" if len(x)==2 else "custom"; print("profile=%s slots=%s n_ctx_per_slot=%s" % (p, len(x), [s.get("n_ctx") for s in x]))' \
      || true
    nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw --format=csv,noheader
    ;;
  logs)
    docker compose logs --tail=200 -f qwen35
    ;;
  *)
    printf 'Usage: %s {start [latency|throughput]|profile {latency|throughput}|stop|restart|status|logs}\n' "$0" >&2
    exit 2
    ;;
esac
