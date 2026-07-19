#!/usr/bin/env bash
set -euo pipefail

MODELPORT_DIR="${MODELPORT_DIR:-/home/tiammomo/projects/dev/ModelPort}"
ENV_FILE="${MODELPORT_ENV_FILE:-$MODELPORT_DIR/.env}"
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${MODELPORT_AUTH_TOKEN:?MODELPORT_AUTH_TOKEN is required}"
MODELPORT_ENDPOINT="${MODELPORT_BASE_URL:-${ANTHROPIC_BASE_URL:-http://127.0.0.1:38082}}"

curl --noproxy '*' -fsS "$MODELPORT_ENDPOINT/livez"
printf '\n'

curl --noproxy '*' -fsS "$MODELPORT_ENDPOINT/v1/messages" \
  -H "x-api-key: $MODELPORT_AUTH_TOKEN" \
  -H 'x-modelport-traffic-class: synthetic' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "local_qwen:qwen3.5-9b-q5km",
    "max_tokens": 512,
    "temperature": 0,
    "messages": [{"role": "user", "content": "只回复：ModelPort 已连接本地 Qwen3.5"}]
  }' > "$BODY_FILE"

python3 - "$BODY_FILE" <<'PY'
import json
import pathlib
import sys

body = json.loads(pathlib.Path(sys.argv[1]).read_text())
text = "".join(block.get("text", "") for block in body.get("content", []) if block.get("type") == "text")
print(text)
print(json.dumps(body.get("usage", {}), ensure_ascii=False))
if "ModelPort 已连接本地 Qwen3.5" not in text:
    raise SystemExit("ModelPort smoke test failed: expected final content was not returned")
PY
