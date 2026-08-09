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

validate_private_lock_path() {
  local path="$1"
  if [[ ! -e "$path" && ! -L "$path" ]]; then
    return 0
  fi
  [[ -f "$path" && ! -L "$path" ]] || {
    printf 'Runtime lock path must be a regular non-symlink file: %s\n' "$path" >&2
    return 1
  }
  local owner mode links
  owner="$(stat -c '%u' "$path")"
  mode="$(stat -c '%a' "$path")"
  links="$(stat -c '%h' "$path")"
  if [[ "$owner" != "$(id -u)" || "$links" != "1" || $((8#$mode & 8#077)) -ne 0 ]]; then
    printf 'Runtime lock file must be private, single-link, and current-user-owned: %s\n' \
      "$path" >&2
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

assert_approved_catalog_spec() {
  local root_dir="$1"
  local require_selected="${2:-false}"
  local package_root
  package_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  local transaction_id="${QWEN_CONTROL_TRANSACTION_ID:-}"
  local catalog_spec_sha256="${LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256:-}"
  if [[ -z "$transaction_id" && -z "$catalog_spec_sha256" ]]; then
    return 0
  fi
  if [[ -z "$transaction_id" ]]; then
    printf 'Approved Catalog spec requires an active control transaction.\n' >&2
    return 1
  fi
  if [[ -z "$catalog_spec_sha256" ]]; then
    python3 - "$package_root" "$root_dir" "$transaction_id" <<'PY'
import sys
from pathlib import Path

package_root = Path(sys.argv[1])
root = Path(sys.argv[2])
sys.path.insert(0, str(package_root / "src"))
from local_inference_stack.paths import ProjectPaths
from local_inference_stack.transactions import TransactionStore, is_terminal

document = TransactionStore(ProjectPaths(root)).read()
if not document or document.get("id") != sys.argv[3] or is_terminal(document):
    raise SystemExit("the active control transaction identity changed")
if document.get("schemaVersion") == 2 and document.get("operation") == "deploy":
    raise SystemExit("active deploy transaction is missing its approved Catalog spec SHA256")
PY
    return
  fi
  local args=(
    assert-deployment
    --model "$QWEN_CATALOG_ID"
    --catalog-spec-sha256 "$catalog_spec_sha256"
  )
  if [[ "$require_selected" == "true" ]]; then
    args+=(--selected)
  fi
  "$root_dir/scripts/model-manager.py" "${args[@]}"
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
  local package_root
  package_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  local lock_dir="$root_dir/cache/locks"
  local control_dir="$root_dir/cache/control-plane"
  local runtime_lock="$lock_dir/runtime.lock"
  local transaction_lock="$control_dir/transaction.lock"
  local inherited_locks=false
  mkdir -p "$lock_dir"
  mkdir -p "$control_dir"
  [[ -d "$lock_dir" && ! -L "$lock_dir" \
    && -d "$control_dir" && ! -L "$control_dir" ]] || {
    printf 'Runtime lock directories must be non-symlink directories.\n' >&2
    return 1
  }
  chmod 700 "$lock_dir"
  chmod 700 "$control_dir"
  validate_private_lock_path "$runtime_lock" || return 1
  validate_private_lock_path "$transaction_lock" || return 1

  if [[ "${QWEN_RUNTIME_LOCK_HELD:-}" == "1" \
    && -e "/proc/$$/fd/203" && -e "/proc/$$/fd/204" ]]; then
    local runtime_fd_path transaction_fd_path
    runtime_fd_path="$(readlink -f "/proc/$$/fd/203")"
    transaction_fd_path="$(readlink -f "/proc/$$/fd/204")"
    if [[ "$runtime_fd_path" != "$(readlink -f "$runtime_lock")" \
      || "$transaction_fd_path" != "$(readlink -f "$transaction_lock")" ]]; then
      printf 'Inherited runtime lock descriptors do not reference the project lock files.\n' >&2
      return 1
    fi
    if ! flock -n 203; then
      printf 'Inherited runtime lock descriptor is not available.\n' >&2
      return 1
    fi
    flock 204
    inherited_locks=true
  fi

  if [[ "$inherited_locks" != true ]]; then
    exec 203>>"$runtime_lock"
    chmod 600 "$runtime_lock"
    if ! flock -n 203; then
      printf 'Another runtime mutation is already in progress: %s\n' \
        "$runtime_lock" >&2
      return 1
    fi
    exec 204>>"$transaction_lock"
    chmod 600 "$transaction_lock"
    flock 204
  fi

  # Authorization is rechecked even for inherited descriptors. The environment
  # sentinel only avoids reopening a parent's lock; it is never authorization.
  if ! python3 - \
    "$package_root" \
    "$control_dir/transaction.json" \
    "${QWEN_CONTROL_TRANSACTION_ID:-}" \
    "${LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256:-}" \
    "$QWEN_CATALOG_ID" <<'PY'
import json
import os
import stat
import sys
import uuid
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "src"))
from local_inference_stack.deployment import DeploymentSpecError, parse_approved_deployment

path = Path(sys.argv[2])
provided = sys.argv[3]
catalog_spec_sha256 = sys.argv[4]
catalog_id = sys.argv[5]
if provided:
    try:
        if str(uuid.UUID(provided)) != provided:
            raise ValueError
    except ValueError:
        raise SystemExit("QWEN_CONTROL_TRANSACTION_ID must be a canonical UUID")

if not path.exists() and not path.is_symlink():
    if provided:
        raise SystemExit("the authorized control transaction no longer exists")
    raise SystemExit(0)

metadata = path.lstat()
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.getuid()
    or metadata.st_nlink != 1
    or stat.S_IMODE(metadata.st_mode) & 0o077
):
    raise SystemExit("transaction state is not a private current-user regular file")
try:
    document = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"transaction state is unreadable: {exc}") from exc

schema = document.get("schemaVersion")
state = document.get("state")
terminal = (
    (schema == 1 and state == "completed")
    or (
        schema == 2
        and state in {"completed", "failed-restored", "superseded-verified"}
    )
)
if terminal:
    if provided:
        raise SystemExit("the authorized control transaction is already terminal")
    raise SystemExit(0)
if schema not in {1, 2}:
    raise SystemExit("transaction state has an unsupported schema")
if not provided or document.get("id") != provided:
    raise SystemExit(
        "an active control transaction blocks this runtime mutation; "
        "use the matching public control-plane command"
    )
binding_required = (
    schema == 2
    and document.get("operation") == "deploy"
    and state in {"planned", "deploying", "accepting"}
)
if binding_required or catalog_spec_sha256:
    if not catalog_spec_sha256:
        raise SystemExit("active deploy transaction is missing its approved Catalog spec SHA256")
    try:
        spec = parse_approved_deployment(
            {
                "schemaVersion": 1,
                "approvedCatalogSpecSha256": document.get(
                    "approvedCatalogSpecSha256"
                ),
                "catalogSpec": document.get("approvedCatalogSpec"),
            }
        )
    except DeploymentSpecError as error:
        raise SystemExit(f"deploy transaction has an invalid approved Catalog spec: {error}") from error
    if spec.sha256 != catalog_spec_sha256 or spec.catalog_id != catalog_id:
        raise SystemExit("runtime mutation does not match the approved Catalog spec")
PY
  then
    return 1
  fi
  export QWEN_RUNTIME_LOCK_HELD=1
}
