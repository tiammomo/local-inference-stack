#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_FILE="${OPERATIONS_SECRETS_FILE:-$ROOT_DIR/profiles/operations.secrets.env}"
BASE_URL="${MODELPORT_BASE_URL:-http://127.0.0.1:38082}"
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

if [[ -f "$SECRETS_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
  set +a
fi
: "${MODELPORT_AUTH_TOKEN:?MODELPORT_AUTH_TOKEN is required}"

status="$(curl --noproxy '*' -sS -o "$BODY_FILE" -w '%{http_code}' \
  -X POST "$BASE_URL/v1/messages" \
  -H "x-api-key: $MODELPORT_AUTH_TOKEN" \
  -H 'x-modelport-traffic-class: synthetic' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  --data-binary '{
    "model": "qwen3.5-fast",
    "max_tokens": 131072,
    "thinking": {"type": "disabled"},
    "messages": [{"role": "user", "content": "context admission probe"}]
  }')"

if [[ "$status" != "400" ]]; then
  printf 'Expected HTTP 400 context rejection, got %s\n' "$status" >&2
  sed -n '1,80p' "$BODY_FILE" >&2
  exit 1
fi

python3 - "$BODY_FILE" <<'PY'
import json
import pathlib
import sys

body = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
message = body.get("error", {}).get("message", "")
if "exceeds context_tokens=131072" not in message:
    raise SystemExit(f"missing context limit evidence: {message}")
if "never silently truncated" not in message:
    raise SystemExit(f"missing no-truncation guarantee: {message}")
print(message)
PY
