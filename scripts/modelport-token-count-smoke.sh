#!/usr/bin/env bash
set -euo pipefail

MODELPORT_DIR="${MODELPORT_DIR:-/home/tiammomo/projects/dev/ModelPort}"
ENV_FILE="${MODELPORT_ENV_FILE:-$MODELPORT_DIR/.env}"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${MODELPORT_AUTH_TOKEN:?MODELPORT_AUTH_TOKEN is required}"
MODELPORT_ENDPOINT="${MODELPORT_BASE_URL:-${ANTHROPIC_BASE_URL:-http://127.0.0.1:38082}}"
QWEN_ENDPOINT="${QWEN_BASE_URL:-http://127.0.0.1:18080}"

python3 - "$TEMP_DIR/direct-request.json" "$TEMP_DIR/modelport-request.json" <<'PY'
import json
import pathlib
import sys

base = {
    "system": "你是一个严格的本地代码助手。",
    "messages": [{"role": "user", "content": "你好，world。请检查天气工具参数。"}],
    "tools": [{
        "name": "get_weather",
        "description": "查询城市天气",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }],
}
for path, model in zip(sys.argv[1:], ("qwen3.5-9b-q5km", "qwen3.5-code")):
    payload = {"model": model, **base}
    pathlib.Path(path).write_text(json.dumps(payload, ensure_ascii=False))
PY

curl --noproxy '*' -fsS "$QWEN_ENDPOINT/v1/messages/count_tokens" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  --data-binary "@$TEMP_DIR/direct-request.json" > "$TEMP_DIR/direct-response.json"

curl --noproxy '*' -fsS "$MODELPORT_ENDPOINT/v1/messages/count_tokens" \
  -H "x-api-key: $MODELPORT_AUTH_TOKEN" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  --data-binary "@$TEMP_DIR/modelport-request.json" > "$TEMP_DIR/modelport-response.json"

python3 - "$TEMP_DIR/direct-response.json" "$TEMP_DIR/modelport-response.json" <<'PY'
import json
import pathlib
import sys

direct = json.loads(pathlib.Path(sys.argv[1]).read_text()).get("input_tokens")
gateway = json.loads(pathlib.Path(sys.argv[2]).read_text()).get("input_tokens")
if not isinstance(direct, int) or not isinstance(gateway, int):
    raise SystemExit("token count smoke failed: response is missing integer input_tokens")
if direct != gateway:
    raise SystemExit(f"token count smoke failed: direct={direct}, modelport={gateway}")
print(f"token_count_exact={gateway} direct={direct} modelport={gateway}")
PY
