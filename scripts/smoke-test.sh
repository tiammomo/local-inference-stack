#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${LLAMA_BASE_URL:-http://127.0.0.1:18080}"
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

curl --noproxy '*' -fsS "$BASE_URL/health"
printf '\n'
curl --noproxy '*' -fsS "$BASE_URL/v1/models"
printf '\n'

curl --noproxy '*' -fsS "$BASE_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.5-9b-q5km",
    "messages": [{"role": "user", "content": "只回复：本地 Qwen3.5 部署成功"}],
    "max_tokens": 512,
    "temperature": 0
  }' > "$BODY_FILE"

python3 - "$BODY_FILE" <<'PY'
import json
import pathlib
import sys

body = json.loads(pathlib.Path(sys.argv[1]).read_text())
choice = body["choices"][0]
message = choice["message"]
print(message.get("content", ""))
print(json.dumps(body.get("usage", {}), ensure_ascii=False))
if "本地 Qwen3.5 部署成功" not in message.get("content", ""):
    raise SystemExit("direct smoke test failed: expected final content was not returned")
PY
