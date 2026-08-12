#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/profiles/operations.secrets.env"
# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"
load_deployment_env "$ROOT_DIR"
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

if [[ "${LOCAL_INFERENCE_BOUND_QUALIFICATION:-}" == "1" ]]; then
  ENV_FILE="${MODELPORT_ENV_FILE:?Bound ModelPort credential capability is required}"
  QWEN_SERVED_MODEL_ID="${LOCAL_INFERENCE_SERVED_MODEL_ID:?}"
else
  ENV_FILE="${MODELPORT_ENV_FILE:-$ENV_FILE}"
fi
if [[ -f "$ENV_FILE" || -L "$ENV_FILE" ]]; then
  load_private_modelport_token "$ROOT_DIR" "$ENV_FILE"
fi

: "${MODELPORT_AUTH_TOKEN:?MODELPORT_AUTH_TOKEN is required}"
if [[ "${LOCAL_INFERENCE_BOUND_QUALIFICATION:-}" == "1" ]]; then
  MODELPORT_ENDPOINT="http://127.0.0.1:38082"
else
  MODELPORT_ENDPOINT="${MODELPORT_BASE_URL:-${ANTHROPIC_BASE_URL:-http://127.0.0.1:38082}}"
fi

curl --noproxy '*' -fsS "$MODELPORT_ENDPOINT/livez"
printf '\n'

curl --noproxy '*' -fsS "$MODELPORT_ENDPOINT/v1/messages" \
  -H "x-api-key: $MODELPORT_AUTH_TOKEN" \
  -H 'x-modelport-traffic-class: synthetic' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  --data-binary @- > "$BODY_FILE" <<JSON
{
  "model": "local_qwen:$QWEN_SERVED_MODEL_ID",
  "max_tokens": 512,
  "temperature": 0,
  "messages": [{"role": "user", "content": "只回复：ModelPort 已连接本地 Qwen3.5"}]
}
JSON

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
