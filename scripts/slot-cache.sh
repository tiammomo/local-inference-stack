#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${LLAMA_BASE_URL:-http://127.0.0.1:18080}"
SLOT_ID="${SLOT_ID:-0}"
ACTION="${1:-list}"
NAME="${2:-}"
SLOT_DIR="$ROOT_DIR/cache/slots"

validate_name() {
  if [[ -z "$NAME" || ! "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]]; then
    printf 'Snapshot name must match [A-Za-z0-9][A-Za-z0-9._-]{0,95}.\n' >&2
    exit 2
  fi
}

call_slot() {
  local operation="$1"
  curl --noproxy '*' -fsS -X POST \
    "$BASE_URL/slots/$SLOT_ID?action=$operation" \
    -H 'Content-Type: application/json' \
    --data-binary "{\"filename\":\"$NAME\"}"
  printf '\n'
}

mkdir -p "$SLOT_DIR"
chmod 700 "$SLOT_DIR"

case "$ACTION" in
  list)
    find "$SLOT_DIR" -maxdepth 1 -type f -printf '%f\t%s bytes\t%TY-%Tm-%Td %TH:%TM:%TS\n' | sort
    ;;
  save)
    validate_name
    printf 'WARNING: a KV snapshot can encode prompt content; keep it local and treat it as sensitive.\n'
    call_slot save
    chmod 600 "$SLOT_DIR/$NAME" 2>/dev/null || true
    ;;
  restore)
    validate_name
    if [[ ! -f "$SLOT_DIR/$NAME" ]]; then
      printf 'Snapshot does not exist: %s\n' "$SLOT_DIR/$NAME" >&2
      exit 2
    fi
    call_slot restore
    ;;
  *)
    printf 'Usage: %s {list|save NAME|restore NAME}\n' "$0" >&2
    exit 2
    ;;
esac
