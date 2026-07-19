#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${LLAMA_BASE_URL:-http://127.0.0.1:18080}"
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

curl --noproxy '*' -fsS "$BASE_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3.5-9b-q5km",
    "messages": [{"role": "user", "content": "计算 17+25，最终答案只回复 42。"}],
    "max_tokens": 512,
    "temperature": 0,
    "chat_template_kwargs": {"enable_thinking": true}
  }' > "$BODY_FILE"

python3 - "$BODY_FILE" <<'PY'
import json
import pathlib
import sys

body = json.loads(pathlib.Path(sys.argv[1]).read_text())
message = body["choices"][0]["message"]
answer = message.get("content", "").strip()
reasoning = message.get("reasoning_content", "").strip()
print(f"answer={answer}")
print(f"reasoning_tokens_present={bool(reasoning)}")
print(json.dumps(body.get("usage", {}), ensure_ascii=False))
if answer != "42":
    raise SystemExit("reasoning smoke test failed: final answer was not 42")
if not reasoning:
    raise SystemExit("reasoning smoke test failed: reasoning_content was empty")
PY
