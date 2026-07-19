#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ALERT_PROFILE="${OPERATIONS_ALERT_PROFILE_FILE:-$ROOT_DIR/profiles/alerting.local.env}"
STATE_DIR="$ROOT_DIR/logs/alerts"
ACTION="${1:-}"
UNIT="${2:-}"

if [[ -f "$ALERT_PROFILE" ]]; then
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

case "$ACTION" in
  clear)
    rm -f -- "$MARKER"
    ;;
  fire)
    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"
    python3 - "$MARKER" "$UNIT" <<'PY'
import json
import os
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

target = Path(sys.argv[1])
payload = {
    "schemaVersion": 1,
    "event": "local_inference_service_failure",
    "unit": sys.argv[2],
    "host": socket.gethostname(),
    "generatedAt": datetime.now(timezone.utc).isoformat(),
}
with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    handle.write("\n")
    temporary = Path(handle.name)
os.chmod(temporary, 0o600)
temporary.replace(target)
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
