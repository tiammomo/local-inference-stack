#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALERT_PROFILE="${OPERATIONS_ALERT_PROFILE_FILE:-}"
if [[ -z "$ALERT_PROFILE" && -n "${CREDENTIALS_DIRECTORY:-}" \
  && -f "$CREDENTIALS_DIRECTORY/alerting.env" ]]; then
  ALERT_PROFILE="$CREDENTIALS_DIRECTORY/alerting.env"
fi
ALERT_PROFILE="${ALERT_PROFILE:-$ROOT_DIR/profiles/alerting.local.env}"
STATE_DIR="$ROOT_DIR/logs/alerts"
ACTION="${1:-}"
UNIT="${2:-}"
# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"

if [[ -f "$ALERT_PROFILE" ]]; then
  validate_private_env_file "$ALERT_PROFILE"
  set -a
  # shellcheck disable=SC1090
  source "$ALERT_PROFILE"
  set +a
fi

if [[ ! "$UNIT" =~ ^[A-Za-z0-9_.@-]+$ ]]; then
  printf 'Invalid systemd unit identifier: %s\n' "$UNIT" >&2
  exit 2
fi

MARKER="$STATE_DIR/$UNIT.json"
cd "$ROOT_DIR"

case "$ACTION" in
  clear)
    rm -f -- "$MARKER"
    ;;
  fire)
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"
    if [[ -e "$MARKER" ]]; then
      validate_private_env_file "$MARKER"
      printf 'Alert already recorded: %s\n' "$MARKER"
      exit 0
    fi
    python3 - "$MARKER" "$UNIT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.env_utils import atomic_write_private_json

target = Path(sys.argv[1])
payload = {
    "schemaVersion": 1,
    "event": "local_inference_service_failure",
    "unit": sys.argv[2],
    "generatedAt": datetime.now(timezone.utc).isoformat(),
}
atomic_write_private_json(target, payload)
print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
PY
    webhook="${OPERATIONS_ALERT_WEBHOOK_URL:-}"
    if [[ -n "$webhook" ]]; then
      case "$webhook" in
        https://*|http://127.0.0.1:*|http://localhost:*) ;;
        *)
          printf 'Alert webhook must use HTTPS or loopback HTTP.\n' >&2
          exit 2
          ;;
      esac
      curl --fail --silent --show-error --max-time 10 \
        -H 'Content-Type: application/json' --data-binary "@$MARKER" "$webhook" >/dev/null
    fi
    ;;
  *)
    printf 'Usage: %s {fire|clear} SYSTEMD_UNIT\n' "$0" >&2
    exit 2
    ;;
esac
