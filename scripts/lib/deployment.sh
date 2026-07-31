#!/usr/bin/env bash

validate_private_env_file() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" ]] || {
    printf 'Required private environment file is not a regular file: %s\n' "$path" >&2
    return 1
  }
  local owner mode links
  owner="$(stat -c '%u' "$path")"
  mode="$(stat -c '%a' "$path")"
  links="$(stat -c '%h' "$path")"
  if [[ "$owner" != "$(id -u)" || "$links" != "1" || $((8#$mode & 8#077)) -ne 0 ]]; then
    printf 'Private environment file must be a single-link file owned by uid %s with no group/other access: %s\n' \
      "$(id -u)" "$path" >&2
    return 1
  fi
}

load_deployment_env() {
  local root_dir="$1"
  local local_profile="$root_dir/profiles/deployment.local.env"
  if [[ -f "$local_profile" ]]; then
    validate_private_env_file "$local_profile"
    set -a
    # This file is generated locally by model-manager.py and never committed.
    # shellcheck disable=SC1090
    source "$local_profile"
    set +a
  fi
  export QWEN_CATALOG_ID="${QWEN_CATALOG_ID:-qwen35-9b-q5km}"
  export QWEN_SERVED_MODEL_ID="${QWEN_SERVED_MODEL_ID:-qwen3.5-9b-q5km}"
  export QWEN_CONTAINER_NAME="${QWEN_CONTAINER_NAME:-qwen35-9b-q5km}"
  export MODELPORT_NETWORK_NAME="${MODELPORT_NETWORK_NAME:-modelport_default}"
}

run_clean_compose() {
  local root_dir="$1"
  shift
  local unset_args=()
  local variable
  while IFS= read -r variable; do
    [[ -n "$variable" ]] && unset_args+=(-u "$variable")
  done < <(
    grep -oE '\$\{[A-Z_][A-Z0-9_]*' "$root_dir/compose.yaml" \
      | cut -c3- \
      | sort -u
  )
  env "${unset_args[@]}" \
    MODELPORT_NETWORK_NAME="$MODELPORT_NETWORK_NAME" \
    docker compose "$@"
}

acquire_runtime_lock() {
  local root_dir="$1"
  if [[ "${QWEN_RUNTIME_LOCK_HELD:-}" == "1" && -e "/proc/$$/fd/203" ]]; then
    return 0
  fi
  local lock_dir="$root_dir/cache/locks"
  mkdir -p "$lock_dir"
  chmod 700 "$lock_dir"
  exec 203>"$lock_dir/runtime.lock"
  chmod 600 "$lock_dir/runtime.lock"
  if ! flock -n 203; then
    printf 'Another runtime mutation is already in progress: %s\n' \
      "$lock_dir/runtime.lock" >&2
    return 1
  fi
  export QWEN_RUNTIME_LOCK_HELD=1
}
