#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/profiles/operations.secrets.env"

# shellcheck source=scripts/lib/deployment.sh
source "$ROOT_DIR/scripts/lib/deployment.sh"
if [[ "${LOCAL_INFERENCE_BOUND_QUALIFICATION:-}" == "1" ]]; then
  ENV_FILE="${MODELPORT_ENV_FILE:?Bound ModelPort credential capability is required}"
else
  ENV_FILE="${MODELPORT_ENV_FILE:-$ENV_FILE}"
fi
if [[ -f "$ENV_FILE" || -L "$ENV_FILE" ]]; then
  load_private_modelport_token "$ROOT_DIR" "$ENV_FILE"
fi

: "${MODELPORT_AUTH_TOKEN:?MODELPORT_AUTH_TOKEN is required}"
export CONTEXT_BACKEND=modelport
if [[ "${LOCAL_INFERENCE_BOUND_QUALIFICATION:-}" == "1" ]]; then
  export MODELPORT_BASE_URL="http://127.0.0.1:38082"
  export TARGET_TOKENS="${LOCAL_INFERENCE_MODELPORT_CONTEXT_TOKENS:?}"
  export MAX_TOKENS="${LOCAL_INFERENCE_MODELPORT_CONTEXT_MAX_TOKENS:?}"
  export FILLER_PREFIX="这是ModelPort长上下文冷缓存验收的独立文本前缀。"
else
  export MODELPORT_BASE_URL="${MODELPORT_CONTEXT_URL:-${ANTHROPIC_BASE_URL:-http://127.0.0.1:38082}}"
  export TARGET_TOKENS="${MODELPORT_CONTEXT_TARGET_TOKENS:-92000}"
  export MAX_TOKENS="${MODELPORT_CONTEXT_MAX_TOKENS:-32768}"
  export FILLER_PREFIX="${MODELPORT_CONTEXT_FILLER_PREFIX:-这是ModelPort长上下文冷缓存验收的独立文本前缀。}"
fi

exec python3 "$ROOT_DIR/scripts/context-acceptance.py"
