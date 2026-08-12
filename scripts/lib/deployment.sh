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

load_private_modelport_token() {
  local root_dir="$1"
  local env_file="$2"
  local token
  token="$(
    unset QWEN_CONTROL_TRANSACTION_ID \
      LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256 \
      LOCAL_INFERENCE_ROLLOUT_SUBJECT \
      LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL \
      LOCAL_INFERENCE_ROLLOUT_ACTION_KIND \
      LOCAL_INFERENCE_RUNTIME_PULL_POLICY \
      QWEN_RUNTIME_LOCK_HELD \
      LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN \
      MODELPORT_AUTH_TOKEN
    python3 - "$root_dir" "$env_file" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from scripts.env_utils import read_private_env_values

values = read_private_env_values(
    Path(sys.argv[2]), allowed_keys=frozenset({"MODELPORT_AUTH_TOKEN"})
)
token = values.get("MODELPORT_AUTH_TOKEN")
if not token:
    raise SystemExit("private ModelPort credential file has no authentication token")
if "\n" in token or "\r" in token or "\0" in token:
    raise SystemExit("private ModelPort authentication token is invalid")
sys.stdout.write(token)
PY
  )" || return
  export MODELPORT_AUTH_TOKEN="$token"
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
if (
    document.get("schemaVersion") == 2
    and document.get("operation") in {"deploy", "upgrade", "rollback"}
    and document.get("state") not in {"recovery_required", "production_restoring"}
):
    raise SystemExit(
        "active Catalog-bound transaction is missing its approved Catalog spec SHA256"
    )
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
  local mutation_purpose="${2:-unspecified}"
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
    "$root_dir" \
    "${QWEN_CONTROL_TRANSACTION_ID:-}" \
    "${LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256:-}" \
    "$QWEN_CATALOG_ID" \
    "${LOCAL_INFERENCE_ROLLOUT_SUBJECT:-}" \
    "${LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL:-}" \
    "${LOCAL_INFERENCE_ROLLOUT_ACTION_KIND:-}" \
    "${LOCAL_INFERENCE_RUNTIME_PULL_POLICY:-}" \
    "$mutation_purpose" <<'PY'
import sys
import uuid
from pathlib import Path

package_root = Path(sys.argv[1])
project_root = Path(sys.argv[2])
sys.path.insert(0, str(package_root / "src"))
from local_inference_stack.paths import ProjectPaths
from local_inference_stack.result import RecoveryError
from local_inference_stack.transactions import TransactionStore, is_terminal

provided = sys.argv[3]
catalog_spec_sha256 = sys.argv[4]
catalog_id = sys.argv[5]
rollout_subject = sys.argv[6] or None
encoded_ordinal = sys.argv[7]
action_kind = sys.argv[8] or None
pull_policy = sys.argv[9] or None
mutation_purpose = sys.argv[10]
action_ordinal = None
if pull_policy not in {None, "never"}:
    raise SystemExit("unsupported controlled runtime pull policy")
if pull_policy is not None and not provided:
    raise SystemExit("controlled runtime pull policy requires a rollout transaction")
if provided:
    try:
        if str(uuid.UUID(provided)) != provided:
            raise ValueError
    except ValueError:
        raise SystemExit("QWEN_CONTROL_TRANSACTION_ID must be a canonical UUID")
if encoded_ordinal:
    if (
        not encoded_ordinal.isascii()
        or not encoded_ordinal.isdecimal()
        or (len(encoded_ordinal) > 1 and encoded_ordinal.startswith("0"))
    ):
        raise SystemExit("rollout action ordinal must be a decimal integer")
    action_ordinal = int(encoded_ordinal)

store = TransactionStore(ProjectPaths(project_root))
try:
    document = store.read()
except RecoveryError as error:
    raise SystemExit(str(error)) from error
if document is None:
    if provided:
        raise SystemExit("the authorized control transaction no longer exists")
    if pull_policy is not None:
        raise SystemExit(
            "controlled runtime pull policy requires a rollout transaction"
        )
    raise SystemExit(0)

schema = document.get("schemaVersion")
state = document.get("state")
if is_terminal(document):
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
operation = document.get("operation")
rollback_start = (
    schema == 2
    and operation == "rollback"
    and state == "candidate_starting"
    and rollout_subject == "target"
    and action_kind == "start-target"
    and action_ordinal is not None
)
rollout_recovery = (
    schema == 2
    and operation in {"upgrade", "rollback"}
    and state == "production_restoring"
    and not catalog_spec_sha256
    and rollout_subject is None
    and action_ordinal is None
    and action_kind is None
)
if (rollback_start or rollout_recovery) and pull_policy != "never":
    raise SystemExit(
        "rollback start-target and rollout recovery require --pull never"
    )
if pull_policy is not None and not (rollback_start or rollout_recovery):
    raise SystemExit(
        "controlled no-pull startup is restricted to rollback start-target "
        "or rollout recovery"
    )
binding_required = (
    schema == 2
    and document.get("operation") in {"deploy", "upgrade", "rollback"}
    and state not in {"recovery_required", "production_restoring"}
)
if (
    binding_required
    or catalog_spec_sha256
    or rollout_subject is not None
    or action_ordinal is not None
    or action_kind is not None
):
    if not catalog_spec_sha256:
        raise SystemExit(
            "active Catalog-bound transaction is missing its approved Catalog spec SHA256"
        )
    if schema == 2 and operation in {"upgrade", "rollback"} and state not in {
        "recovery_required",
        "production_restoring",
    }:
        expected_action = {
            "start": "start-target",
            "stop": "stop-source",
            "profile": None,
            "restart": None,
        }.get(mutation_purpose)
        if expected_action is None or action_kind != expected_action:
            raise SystemExit(
                "runtime mutation purpose is not authorized by the pending rollout action"
            )
    try:
        store.assert_approved_deployment(
            transaction_id=provided,
            catalog_spec_sha256=catalog_spec_sha256,
            catalog_id=catalog_id,
            rollout_subject=rollout_subject,
            action_ordinal=action_ordinal,
            action_kind=action_kind,
            inherited_locks=True,
        )
    except RecoveryError as error:
        raise SystemExit(str(error)) from error
PY
  then
    return 1
  fi
  export QWEN_RUNTIME_LOCK_HELD=1
}
