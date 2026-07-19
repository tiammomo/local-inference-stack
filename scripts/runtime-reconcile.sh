#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${QWEN_BOOT_PROFILE:-latency}"
ATTEMPTS="${QWEN_RECONCILE_ATTEMPTS:-12}"

if curl --noproxy '*' -fsS http://127.0.0.1:18080/health >/dev/null 2>&1; then
  printf 'Qwen runtime is already healthy.\n'
  exit 0
fi

for attempt in $(seq 1 "$ATTEMPTS"); do
  if docker network inspect "${MODELPORT_NETWORK_NAME:-modelport_default}" >/dev/null 2>&1; then
    exec "$ROOT_DIR/scripts/runtime.sh" start "$PROFILE"
  fi
  printf 'Waiting for ModelPort Docker network (%s/%s).\n' "$attempt" "$ATTEMPTS"
  sleep 5
done

printf 'ModelPort Docker network is unavailable after %s attempts.\n' "$ATTEMPTS" >&2
exit 1
