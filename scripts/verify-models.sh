#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARGS=()

usage() {
  printf 'Usage: %s [--full|--active] [--cached]\n' "$0"
  printf '  --full    hash every managed artifact (default)\n'
  printf '  --active  verify only artifacts used by the active text runtime\n'
  printf '  --cached  reuse a SHA256 result while file identity and metadata match\n'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)
      ARGS+=(--full)
      ;;
    --active)
      ;;
    --model)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      ARGS+=(--model "$2")
      shift
      ;;
    --cached)
      ARGS+=(--cached)
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done

exec python3 "$ROOT_DIR/scripts/model-manager.py" verify "${ARGS[@]}"
