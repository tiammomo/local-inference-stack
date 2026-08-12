#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/profiles/operations.secrets.env"
# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"
load_deployment_env "$ROOT_DIR"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

if [[ "${LOCAL_INFERENCE_BOUND_QUALIFICATION:-}" == "1" ]]; then
  ENV_FILE="${MODELPORT_ENV_FILE:?Bound ModelPort credential capability is required}"
  QWEN_SERVED_MODEL_ID="${LOCAL_INFERENCE_SERVED_MODEL_ID:?}"
  CODE_MODEL="${LOCAL_INFERENCE_LOGICAL_CODE_MODEL:?}"
else
  ENV_FILE="${MODELPORT_ENV_FILE:-$ENV_FILE}"
  CODE_MODEL="qwen3.5-code"
fi
if [[ -f "$ENV_FILE" || -L "$ENV_FILE" ]]; then
  load_private_modelport_token "$ROOT_DIR" "$ENV_FILE"
fi

: "${MODELPORT_AUTH_TOKEN:?MODELPORT_AUTH_TOKEN is required}"
if [[ "${LOCAL_INFERENCE_BOUND_QUALIFICATION:-}" == "1" ]]; then
  MODELPORT_ENDPOINT="http://127.0.0.1:38082"
  QWEN_ENDPOINT="http://127.0.0.1:18080"
else
  MODELPORT_ENDPOINT="${MODELPORT_BASE_URL:-${ANTHROPIC_BASE_URL:-http://127.0.0.1:38082}}"
  QWEN_ENDPOINT="${QWEN_BASE_URL:-http://127.0.0.1:18080}"
fi

python3 - "$TEMP_DIR/direct-request.json" "$TEMP_DIR/modelport-request.json" \
  "$QWEN_SERVED_MODEL_ID" "$CODE_MODEL" <<'PY'
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
for path, model in zip(sys.argv[1:3], (sys.argv[3], sys.argv[4])):
    payload = {"model": model, **base}
    # ModelPort's local_qwen policy defaults logical code requests to
    # enable_thinking=true. Count the equivalent rendered direct template.
    if model != sys.argv[4]:
        payload["chat_template_kwargs"] = {"enable_thinking": True}
    pathlib.Path(path).write_text(json.dumps(payload, ensure_ascii=False))
PY

curl --noproxy '*' -fsS "$QWEN_ENDPOINT/v1/messages/count_tokens" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  --data-binary "@$TEMP_DIR/direct-request.json" > "$TEMP_DIR/direct-response.json"

curl --noproxy '*' -fsS "$MODELPORT_ENDPOINT/v1/messages/count_tokens" \
  -H "x-api-key: $MODELPORT_AUTH_TOKEN" \
  -H 'x-modelport-traffic-class: synthetic' \
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
