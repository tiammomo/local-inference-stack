#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="$ROOT_DIR/models/qwen3.5-9b"
CACHE_DIR="$ROOT_DIR/cache/integrity"
MODE="full"
USE_CACHE="false"

usage() {
  printf 'Usage: %s [--full|--active] [--cached]\n' "$0"
  printf '  --full    hash every managed artifact (default)\n'
  printf '  --active  verify only artifacts used by the active text runtime\n'
  printf '  --cached  reuse a SHA256 result while file identity and metadata match\n'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)
      MODE="full"
      ;;
    --active)
      MODE="active"
      ;;
    --cached)
      USE_CACHE="true"
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

declare -A EXPECTED=(
  [Qwen3.5-9B-Q5_K_M.gguf]="dc2a39aef291f91a9116ad214058da0d86eb648743a124bd8c333787c4b9c91c"
  [Qwen3.5-9B-MTP-Q5_K_M.gguf]="1732d6616554b102be9bc41684cd094f471e1b3067f5e5a89eb5a86a5a4f2a6c"
  [mmproj-BF16.gguf]="853698ce7aa6c7ba732478bad280240969ddf7b0fcbf93900046f63903a83383"
)

verify_one() {
  local filename="$1"
  local expected="${EXPECTED[$filename]:-}"
  local path="$MODEL_DIR/$filename"
  local fingerprint stamp expected_stamp temporary
  if [[ -z "$expected" ]]; then
    printf 'No pinned SHA256 for artifact: %s\n' "$filename" >&2
    return 2
  fi
  if [[ ! -f "$path" ]]; then
    printf 'Missing artifact: %s\n' "$path" >&2
    return 1
  fi
  fingerprint="$(stat -Lc '%d:%i:%s:%y:%z' "$path")"
  stamp="$CACHE_DIR/$filename.sha256.stamp"
  expected_stamp="$expected|$fingerprint"
  if [[ "$USE_CACHE" == "true" && -r "$stamp" && "$(<"$stamp")" == "$expected_stamp" ]]; then
    printf '%s: OK (cached)\n' "$filename"
    return 0
  fi
  (
    cd "$MODEL_DIR"
    printf '%s  %s\n' "$expected" "$filename" | sha256sum --check -
  )
  mkdir -p "$CACHE_DIR"
  temporary="$(mktemp "$CACHE_DIR/.${filename}.XXXXXX")"
  chmod 600 "$temporary"
  printf '%s\n' "$expected_stamp" >"$temporary"
  mv -f "$temporary" "$stamp"
}

if [[ "$MODE" == "active" ]]; then
  verify_one "${QWEN_MODEL_FILE:-Qwen3.5-9B-Q5_K_M.gguf}"
  if [[ -n "${QWEN_MMPROJ_FILE:-}" ]]; then
    verify_one "$QWEN_MMPROJ_FILE"
  fi
else
  verify_one Qwen3.5-9B-Q5_K_M.gguf
  verify_one Qwen3.5-9B-MTP-Q5_K_M.gguf
  verify_one mmproj-BF16.gguf
fi
