#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELPORT_DIR="${MODELPORT_DIR:-/home/tiammomo/projects/dev/ModelPort}"
ENV_FILE="${MODELPORT_ENV_FILE:-$MODELPORT_DIR/.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${MODELPORT_AUTH_TOKEN:?MODELPORT_AUTH_TOKEN is required}"
export CONTEXT_BACKEND=modelport
export MODELPORT_BASE_URL="${MODELPORT_CONTEXT_URL:-${ANTHROPIC_BASE_URL:-http://127.0.0.1:38082}}"
export TARGET_TOKENS="${MODELPORT_CONTEXT_TARGET_TOKENS:-92000}"
export MAX_TOKENS="${MODELPORT_CONTEXT_MAX_TOKENS:-32768}"
export FILLER_PREFIX="${MODELPORT_CONTEXT_FILLER_PREFIX:-这是ModelPort长上下文冷缓存验收的独立文本前缀。}"

exec python3 "$ROOT_DIR/scripts/context-acceptance.py"
