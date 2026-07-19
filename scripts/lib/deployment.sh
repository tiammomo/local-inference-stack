#!/usr/bin/env bash

load_deployment_env() {
  local root_dir="$1"
  local local_profile="$root_dir/profiles/deployment.local.env"
  if [[ -f "$local_profile" ]]; then
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
