#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_FILE="$ROOT_DIR/profiles/operations.secrets.env"
BASE_URL="http://127.0.0.1:38082"
BODY_FILE="$(mktemp)"
REQUEST_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE" "$REQUEST_FILE"' EXIT

# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"
if [[ "${LOCAL_INFERENCE_BOUND_QUALIFICATION:-}" == "1" ]]; then
  SECRETS_FILE="${MODELPORT_ENV_FILE:?Bound ModelPort credential capability is required}"
else
  SECRETS_FILE="${OPERATIONS_SECRETS_FILE:-$SECRETS_FILE}"
  BASE_URL="${MODELPORT_BASE_URL:-$BASE_URL}"
fi
if [[ -f "$SECRETS_FILE" || -L "$SECRETS_FILE" ]]; then
  load_private_modelport_token "$ROOT_DIR" "$SECRETS_FILE"
fi
: "${MODELPORT_AUTH_TOKEN:?MODELPORT_AUTH_TOKEN is required}"

FAST_MODEL="${LOCAL_INFERENCE_LOGICAL_FAST_MODEL:-qwen3.5-fast}"
python3 - "$REQUEST_FILE" "$FAST_MODEL" <<'PY'
import json
import pathlib
import sys

# Keep max_tokens within qwen3.5-fast's logical-model output limit. The
# high-entropy numbered prompt makes the input itself large enough to exercise
# the aggregate context admission guard instead of the earlier output guard.
prompt = " ".join(f"context_boundary_probe_{index:06d}" for index in range(14_000))
payload = {
    "model": sys.argv[2],
    "max_tokens": 4096,
    "thinking": {"type": "disabled"},
    "messages": [{"role": "user", "content": prompt}],
}
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(payload, ensure_ascii=False),
    encoding="utf-8",
)
PY

status="$(curl --noproxy '*' -sS -o "$BODY_FILE" -w '%{http_code}' \
  -X POST "$BASE_URL/v1/messages" \
  -H "x-api-key: $MODELPORT_AUTH_TOKEN" \
  -H 'x-modelport-traffic-class: synthetic' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  --data-binary "@$REQUEST_FILE")"

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
