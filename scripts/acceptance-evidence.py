#!/usr/bin/env python3
"""Maintain a runner-owned acceptance manifest and write host-bound evidence."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from local_inference_stack.acceptance import (  # noqa: E402
    MODELPORT_SOURCE_MATERIAL_PATHS,
    MODELPORT_SOURCE_IDENTITY_POLICY,
    QUALIFICATION_INPUT_POLICY,
    REVIEWED_LOGICAL_MODELS,
    REVIEWED_MODELPORT_GOVERNANCE,
    REVIEWED_MODELPORT_TOOL_USE,
    ROLLOUT_BINDING_KEYS,
    ROLLOUT_EVIDENCE_SCHEMA_VERSION,
    ROLLOUT_FROZEN_INPUTS_POLICY,
    ROLLOUT_RUN_SCHEMA_VERSION,
    RUN_KIND,
    RUNNER_PATH,
    RUN_SCHEMA_VERSION,
    STEP_PLANS,
    VALIDATION_INPUT_POLICY,
    expected_steps,
    modelport_source_identity_valid,
    qualification_input_valid,
    rollout_binding_valid,
    rollout_frozen_inputs_valid,
    run_record_valid,
    sha256_document,
    step_plan_sha256,
    validation_input,
)
from local_inference_stack.catalog import (  # noqa: E402
    CatalogError,
    load_catalog,
    model_by_id,
)
from local_inference_stack.deployment import (  # noqa: E402
    CatalogDeploymentSpec,
    DeploymentSpecError,
)
from local_inference_stack.host import environment_kind  # noqa: E402
from local_inference_stack.materials import (  # noqa: E402
    MaterialError,
    cleanup_interrupted_noreplace_link_at,
    read_file_bytes,
    sha256_file as secure_sha256_file,
)
from local_inference_stack.paths import ProjectPaths  # noqa: E402
from local_inference_stack.transactions import (  # noqa: E402
    RecoveryError,
    TransactionStore,
)

try:
    from scripts.env_utils import atomic_write_private_json, read_private_env_values
    from scripts.local_http import direct_urlopen
    from scripts.runtime_identity import (
        _open_project_parent,
        acceptance_configuration,
        ensure_private_project_directory,
        local_artifact_identity,
        live_runtime_sha256,
        open_private_project_file,
        runtime_mismatches,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from env_utils import atomic_write_private_json, read_private_env_values
    from local_http import direct_urlopen
    from runtime_identity import (
        _open_project_parent,
        acceptance_configuration,
        ensure_private_project_directory,
        local_artifact_identity,
        live_runtime_sha256,
        open_private_project_file,
        runtime_mismatches,
    )


CATALOG_PATH = ROOT_DIR / "catalog" / "models.json"
MODELS_DIR = ROOT_DIR / "models"
INTEGRITY_DIR = ROOT_DIR / "cache" / "integrity"
SUITE_PATH = ROOT_DIR / RUNNER_PATH
HOST_FINGERPRINT_TYPE = "machine-id-sha256-v1"
HOST_FINGERPRINT_CONTEXT = b"local-inference-stack.acceptance-host.v1\0"
MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
RUNNER_TOKEN_ENV = "LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN"
ROLLOUT_AUTHORITY_ENV = {
    "transactionId": "QWEN_CONTROL_TRANSACTION_ID",
    "catalogSpecSha256": "LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256",
    "subject": "LOCAL_INFERENCE_ROLLOUT_SUBJECT",
    "actionOrdinal": "LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL",
    "actionKind": "LOCAL_INFERENCE_ROLLOUT_ACTION_KIND",
}
CHILD_AUTHORITY_ENV = frozenset(
    {
        *ROLLOUT_AUTHORITY_ENV.values(),
        RUNNER_TOKEN_ENV,
        "QWEN_RUNTIME_LOCK_HELD",
        "LOCAL_INFERENCE_RUNTIME_PULL_POLICY",
    }
)
MODELPORT_MATERIAL_PATHS = MODELPORT_SOURCE_MATERIAL_PATHS
PROVIDER_CONTRACT_PATH = ROOT_DIR / "contracts" / "local-qwen-provider-v1.json"
OPERATIONS_SECRETS_PATH = ROOT_DIR / "profiles" / "operations.secrets.env"
MAX_PRIVATE_JSON_BYTES = 4 * 1024 * 1024
MAX_MODEL_REGISTRY_BYTES = 1024 * 1024
SAFE_HELPER_ENV_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "TERM",
        "TZ",
        "USER",
        "WSL_DISTRO_NAME",
    }
)
RUN_MANIFEST_KEYS = frozenset(
    {
        "schemaVersion",
        "kind",
        "runId",
        "mode",
        "profile",
        "catalogModelId",
        "runner",
        "plan",
        "validationInput",
        "startedAt",
        "finishedAt",
        "durationSeconds",
        "status",
        "exitCode",
        "failedAtStep",
        "terminalStep",
        "stepResults",
        "finalized",
        "selfSha256",
    }
)


@contextlib.contextmanager
def _without_child_authority():
    """Temporarily hide control capabilities from imported subprocess helpers."""

    held = {
        key: os.environ.pop(key)
        for key in CHILD_AUTHORITY_ENV
        if key in os.environ
    }
    try:
        yield
    finally:
        os.environ.update(held)


def _acceptance_configuration(
    model: dict[str, Any], mode: str, profile: str
) -> dict[str, Any]:
    with _without_child_authority():
        return acceptance_configuration(model, mode, profile)


def reviewed_catalog_model(model_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load one model through the repository's strict Catalog boundary."""

    try:
        catalog = load_catalog(CATALOG_PATH)
        return catalog, model_by_id(catalog, model_id)
    except CatalogError as exc:
        raise RuntimeError("acceptance references an invalid or unknown Catalog model") from exc


def payload_sha256(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "selfSha256"}
    return sha256_document(unsigned)


def sha256_file(path: Path) -> str:
    return secure_sha256_file(path)


def host_fingerprint(paths: tuple[Path, ...] = MACHINE_ID_PATHS) -> str | None:
    for path in paths:
        try:
            machine_id = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if machine_id and len(machine_id) <= 256:
            return hashlib.sha256(
                HOST_FINGERPRINT_CONTEXT + machine_id.encode("utf-8")
            ).hexdigest()
    return None


def command_output(command: list[str], timeout: int = 30) -> str | None:
    child_environment = {
        key: value
        for key, value in os.environ.items()
        if key in SAFE_HELPER_ENV_KEYS
    }
    child_environment.update({"NO_PROXY": "*", "no_proxy": "*"})
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_environment,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None
    return result.stdout.strip()


def command_json(command: list[str]) -> Any | None:
    output = command_output(command)
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def _rollout_authority() -> dict[str, Any] | None:
    values = {
        key: os.environ.get(environment_name)
        for key, environment_name in ROLLOUT_AUTHORITY_ENV.items()
    }
    supplied = {key for key, value in values.items() if value is not None}
    if not supplied:
        return None
    if supplied != set(ROLLOUT_AUTHORITY_ENV) or any(value == "" for value in values.values()):
        raise RuntimeError("rollout qualification authority is incomplete")
    try:
        transaction_id = str(uuid.UUID(str(values["transactionId"])))
    except ValueError as exc:
        raise RuntimeError("rollout transaction id is not a canonical UUID") from exc
    if transaction_id != values["transactionId"]:
        raise RuntimeError("rollout transaction id is not a canonical UUID")
    encoded_ordinal = str(values["actionOrdinal"])
    if (
        not encoded_ordinal.isascii()
        or not encoded_ordinal.isdecimal()
        or (len(encoded_ordinal) > 1 and encoded_ordinal.startswith("0"))
    ):
        raise RuntimeError("rollout action ordinal is invalid")
    if (
        values["subject"] != "target"
        or values["actionKind"] != "target-full"
        or SHA256_PATTERN.fullmatch(str(values["catalogSpecSha256"])) is None
    ):
        raise RuntimeError("acceptance recording has invalid target-full authority")
    return {
        "transactionId": transaction_id,
        "catalogSpecSha256": values["catalogSpecSha256"],
        "subject": "target",
        "actionOrdinal": int(encoded_ordinal),
        "actionKind": "target-full",
    }


def _pending_rollout_binding(catalog_model_id: str) -> dict[str, Any] | None:
    authority = _rollout_authority()
    if authority is None:
        return None
    try:
        binding = TransactionStore(ProjectPaths(ROOT_DIR)).pending_rollout_qualification_binding(
            transaction_id=authority["transactionId"],
            catalog_spec_sha256=authority["catalogSpecSha256"],
            catalog_id=catalog_model_id,
            rollout_subject=authority["subject"],
            action_ordinal=authority["actionOrdinal"],
            action_kind=authority["actionKind"],
        )
    except RecoveryError as exc:
        raise RuntimeError(f"rollout qualification authority is not pending: {exc}") from exc
    if set(binding) != ROLLOUT_BINDING_KEYS or not rollout_binding_valid(binding):
        raise RuntimeError("transaction returned an invalid rollout qualification binding")
    return binding


def _required_command_output(command: list[str], *, label: str) -> str:
    output = command_output(command)
    if output is None:
        raise RuntimeError(f"cannot determine {label}")
    return output


def _modelport_source_identity(project: Path | None = None) -> dict[str, Any]:
    encoded = os.fspath(project) if project is not None else os.environ.get(
        "MODELPORT_PROJECT_DIR", ""
    )
    if not encoded or not Path(encoded).is_absolute():
        raise RuntimeError(
            "rollout qualification requires an absolute MODELPORT_PROJECT_DIR"
        )
    lexical = Path(os.path.abspath(encoded))
    try:
        root = lexical.resolve(strict=True)
        root_metadata = root.stat()
    except OSError as exc:
        raise RuntimeError("MODELPORT_PROJECT_DIR is unavailable") from exc
    if (
        root != lexical
        or not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != os.getuid()
        or stat.S_IMODE(root_metadata.st_mode) & 0o022
    ):
        raise RuntimeError(
            "MODELPORT_PROJECT_DIR must be a stable current-user, non-writable checkout path"
        )

    def git(*arguments: str) -> str:
        return _required_command_output(
            ["git", "-C", str(root), *arguments], label="ModelPort Git identity"
        )

    before_commit = git("rev-parse", "HEAD")
    before_tree = git("rev-parse", "HEAD^{tree}")
    if (
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", before_commit) is None
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", before_tree) is None
        or Path(git("rev-parse", "--show-toplevel")).resolve(strict=True) != root
        or git("status", "--porcelain=v1", "--untracked-files=all") != ""
    ):
        raise RuntimeError("ModelPort source must be one exact clean Git checkout")

    materials: list[dict[str, str]] = []
    for relative in sorted(MODELPORT_MATERIAL_PATHS):
        path = root / relative
        if relative.startswith("scripts/") and not os.access(path, os.X_OK):
            raise RuntimeError(f"required ModelPort executable is unavailable: {relative}")
        try:
            body = read_file_bytes(path, maximum_bytes=16 * 1024 * 1024)
            digest = hashlib.sha256(body).hexdigest()
            head_blob = git("rev-parse", f"HEAD:{relative}")
            algorithm = (
                hashlib.sha1 if len(head_blob) == 40 else hashlib.sha256
            )
            worktree_blob = algorithm(
                f"blob {len(body)}\0".encode("ascii") + body
            ).hexdigest()
            if worktree_blob != head_blob:
                raise RuntimeError("worktree bytes do not match HEAD")
        except (MaterialError, OSError, RuntimeError) as exc:
            raise RuntimeError(f"cannot bind ModelPort material: {relative}") from exc
        materials.append({"path": relative, "sha256": digest})

    after_commit = git("rev-parse", "HEAD")
    after_tree = git("rev-parse", "HEAD^{tree}")
    if (
        after_commit != before_commit
        or after_tree != before_tree
        or git("status", "--porcelain=v1", "--untracked-files=all") != ""
    ):
        raise RuntimeError("ModelPort source changed while its identity was captured")
    endpoint = "http://127.0.0.1:38082/livez"
    live = command_json(
        [
            "curl",
            "--noproxy",
            "*",
            "--fail",
            "--silent",
            "--show-error",
            "--connect-timeout",
            "3",
            "--max-time",
            "10",
            endpoint,
        ]
    )
    build = live.get("build") if isinstance(live, dict) else None
    config_sha256 = next(
        item["sha256"] for item in materials if item["path"] == "config.toml"
    )
    if (
        not isinstance(build, dict)
        or live.get("service") != "model-port"
        or live.get("status") != "ok"
        or build.get("revision") != before_commit
        or build.get("sourceState") != "clean"
        or not isinstance(build.get("version"), str)
        or not build["version"]
        or len(build["version"]) > 128
        or build.get("configSha256") != config_sha256
    ):
        raise RuntimeError(
            "ModelPort live identity does not match the clean source checkout"
        )
    if (
        git("rev-parse", "HEAD") != before_commit
        or git("rev-parse", "HEAD^{tree}") != before_tree
        or git("status", "--porcelain=v1", "--untracked-files=all") != ""
    ):
        raise RuntimeError("ModelPort source changed during its live identity check")
    return {
        "policyId": MODELPORT_SOURCE_IDENTITY_POLICY,
        "gitCommit": before_commit,
        "gitTree": before_tree,
        "sourceState": "clean",
        "materials": materials,
        "materialsSha256": sha256_document(materials),
        "liveServiceIdentity": {
            "endpoint": endpoint,
            "service": "model-port",
            "status": "ok",
            "build": {
                "revision": before_commit,
                "sourceState": "clean",
                "version": build["version"],
                "configSha256": config_sha256,
            },
        },
    }


def _strict_json_bytes(body: bytes, *, label: str) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-standard JSON number")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _strict_json_material(path: Path, *, maximum_bytes: int) -> tuple[dict[str, Any], str]:
    """Parse one repository material without ambiguous JSON or a path race."""

    try:
        body = read_file_bytes(path, maximum_bytes=maximum_bytes)
    except MaterialError as exc:
        raise RuntimeError("cannot safely read provider contract") from exc
    value = _strict_json_bytes(body, label="provider contract")
    return value, hashlib.sha256(body).hexdigest()


def _authenticated_modelport_registry(
    token: str,
    logical_models: dict[str, Any],
    provider_id: str,
) -> dict[str, Any]:
    """Bind the authenticated live alias registry without retaining a secret."""

    if (
        not token
        or len(token) > 4096
        or any(character in token for character in ("\0", "\r", "\n"))
        or logical_models != REVIEWED_LOGICAL_MODELS
        or provider_id != "local_qwen"
    ):
        raise RuntimeError("ModelPort model registry inputs are invalid")
    request = urllib.request.Request(
        "http://127.0.0.1:38082/v1/models",
        headers={"x-api-key": token},
        method="GET",
    )
    try:
        with direct_urlopen(request, timeout=10) as response:
            body = response.read(MAX_MODEL_REGISTRY_BYTES + 1)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError("authenticated ModelPort model registry is unavailable") from exc
    if len(body) > MAX_MODEL_REGISTRY_BYTES:
        raise RuntimeError("authenticated ModelPort model registry is oversized")
    document = _strict_json_bytes(body, label="ModelPort model registry")
    data = document.get("data")
    if document.get("object") != "list" or not isinstance(data, list):
        raise RuntimeError("authenticated ModelPort model registry is invalid")
    observed: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            raise RuntimeError("authenticated ModelPort model registry is invalid")
        model_id = item.get("id")
        owner = item.get("owned_by")
        if (
            not isinstance(model_id, str)
            or not model_id
            or not isinstance(owner, str)
            or not owner
            or model_id in observed
        ):
            raise RuntimeError("authenticated ModelPort model registry is invalid")
        observed[model_id] = owner
    required = sorted(logical_models)
    if any(observed.get(model_id) != provider_id for model_id in required):
        raise RuntimeError(
            "authenticated ModelPort model registry does not expose the reviewed local aliases"
        )
    registry = {
        "policyId": "local-inference-stack/modelport-model-registry-v1",
        "provider": provider_id,
        "logicalModels": required,
        "entries": [
            {"id": model_id, "ownedBy": observed[model_id]}
            for model_id in required
        ],
    }
    registry["sha256"] = sha256_document(registry)
    return registry


def _target_provider_qualification_identity(
    *,
    catalog_model_id: str,
    catalog_spec_sha256: str,
    modelport_project: Path,
    modelport_source_identity: dict[str, Any],
) -> dict[str, Any]:
    """Prove all predictable, non-target-runtime full-suite prerequisites.

    This runs before a rollout transaction exists.  The returned identity is
    secret-free and stable; live target generation remains a post-switch gate.
    """

    _catalog, model = reviewed_catalog_model(catalog_model_id)
    try:
        target = CatalogDeploymentSpec.from_catalog_model(model)
    except DeploymentSpecError as exc:
        raise RuntimeError("qualification target has an invalid Catalog spec") from exc
    if target.sha256 != catalog_spec_sha256:
        raise RuntimeError("qualification target Catalog spec changed before preflight")
    if not modelport_source_identity_valid(modelport_source_identity):
        raise RuntimeError(
            "ModelPort live service does not expose the reviewed configuration identity"
        )

    contract, contract_sha256 = _strict_json_material(
        PROVIDER_CONTRACT_PATH, maximum_bytes=1024 * 1024
    )
    runtime = contract.get("runtime")
    limits = contract.get("limits")
    acceptance = contract.get("acceptance")
    application = contract.get("application")
    capabilities = contract.get("capabilities")
    governance = contract.get("governance")
    model_runtime = model.get("runtime")
    logical_models = (
        application.get("logicalModels") if isinstance(application, dict) else None
    )
    tool_use = (
        capabilities.get("toolUse") if isinstance(capabilities, dict) else None
    )
    tool_use_local_provider_ready = bool(
        contract.get("provider") == "local_qwen"
        and governance == REVIEWED_MODELPORT_GOVERNANCE
        and tool_use == REVIEWED_MODELPORT_TOOL_USE
    )
    if (
        contract.get("schemaVersion") != 1
        or contract.get("contractId") != "local-qwen-provider-v1"
        or contract.get("provider") != "local_qwen"
        or not isinstance(runtime, dict)
        or set(runtime)
        != {"baseUrl", "servedModelId", "protocol"}
        or runtime.get("baseUrl") != "http://qwen-runtime:8080/v1"
        or runtime.get("protocol") != "openai-compatible-chat-completions"
        or runtime.get("servedModelId") != model.get("servedModelId")
        or not isinstance(limits, dict)
        or set(limits)
        != {
            "contextTokens",
            "recommendedReasoningInputTokens",
            "maxOutputTokens",
        }
        or not isinstance(acceptance, dict)
        or set(acceptance)
        != {
            "localSuite",
            "providerMatrixModel",
            "toolUseMaxTokens",
            "compatibilityCheck",
        }
        or acceptance.get("localSuite")
        != "scripts/acceptance-suite.sh standard"
        or acceptance.get("providerMatrixModel") != "qwen3.5-code"
        or acceptance.get("toolUseMaxTokens") != 2048
        or acceptance.get("compatibilityCheck")
        != "scripts/compatibility-check.py --modelport-project <path>"
        or logical_models != REVIEWED_LOGICAL_MODELS
        or not isinstance(tool_use, dict)
        or tool_use.get("supported") is not True
        or tool_use.get("responseValidation") != "strict"
        or not tool_use_local_provider_ready
        or not isinstance(model_runtime, dict)
    ):
        raise RuntimeError("provider contract is not the reviewed full-suite contract")
    numeric_limits = (
        ("contextTokens", "contextTokens"),
        ("recommendedReasoningInputTokens", "recommendedInputTokens"),
        ("maxOutputTokens", "maxOutputTokens"),
    )
    for contract_key, model_key in numeric_limits:
        expected = limits.get(contract_key)
        actual = model_runtime.get(model_key)
        if (
            not isinstance(expected, int)
            or isinstance(expected, bool)
            or expected <= 0
            or not isinstance(actual, int)
            or isinstance(actual, bool)
            or actual < expected
        ):
            raise RuntimeError("qualification target is below provider contract limits")
    if limits["recommendedReasoningInputTokens"] + limits["maxOutputTokens"] > limits[
        "contextTokens"
    ]:
        raise RuntimeError("provider contract token limits are internally inconsistent")

    node_path = shutil.which("node")
    if (
        not node_path
        or not Path(node_path).is_absolute()
        or node_path.startswith("/mnt/")
        or node_path.lower().endswith(".exe")
        or command_output([node_path, "-p", 'process.versions.node.split(".")[0]'])
        != "24"
    ):
        raise RuntimeError("full qualification requires a Linux Node.js 24 binary")

    try:
        credentials = read_private_env_values(
            OPERATIONS_SECRETS_PATH,
            allowed_keys=frozenset({"MODELPORT_AUTH_TOKEN"}),
        )
    except ValueError as exc:
        raise RuntimeError("full qualification credentials are unavailable") from exc
    token = credentials.get("MODELPORT_AUTH_TOKEN", "")
    if (
        not token
        or len(token) > 4096
        or any(character in token for character in ("\0", "\r", "\n"))
    ):
        raise RuntimeError("full qualification credentials are unavailable")

    registry = _authenticated_modelport_registry(
        token,
        logical_models,
        contract["provider"],
    )

    compatibility = command_json(
        [
            sys.executable,
            str(ROOT_DIR / "scripts" / "compatibility-check.py"),
            "--modelport-project",
            str(modelport_project),
            "--json",
        ]
    )
    source_materials = {
        item["path"]: item["sha256"]
        for item in modelport_source_identity["materials"]
    }
    if (
        not isinstance(compatibility, dict)
        or compatibility.get("contractId") != contract["contractId"]
        or compatibility.get("status") != "passed"
        or not isinstance(compatibility.get("summary"), dict)
        or compatibility["summary"].get("failed") != 0
        or compatibility.get("materialIdentity")
        != {
            "contractSha256": contract_sha256,
            "configSha256": source_materials["config.toml"],
            "governanceSha256": {
                "src/governance.rs": source_materials["src/governance.rs"],
                "src/routes.rs": source_materials["src/routes.rs"],
            },
        }
    ):
        raise RuntimeError("ModelPort configuration does not satisfy the provider contract")
    dashboard = command_json(
        [sys.executable, str(ROOT_DIR / "scripts" / "dashboard-smoke.py")]
    )
    if not isinstance(dashboard, dict) or dashboard.get("status") != "ok":
        raise RuntimeError("operations dashboard is not ready for full qualification")

    identity = {
        "policyId": QUALIFICATION_INPUT_POLICY,
        "targetCatalogSpecSha256": target.sha256,
        "providerContractId": contract["contractId"],
        "providerContractSha256": contract_sha256,
        "servedModelId": runtime["servedModelId"],
        "limitsSha256": sha256_document(limits),
        "acceptanceSha256": sha256_document(acceptance),
        "logicalModels": logical_models,
        "providerMatrixModel": acceptance["providerMatrixModel"],
        "toolUseMaxTokens": acceptance["toolUseMaxTokens"],
        "directContextTokens": 118000,
        "modelPortContextTokens": 92000,
        "modelPortContextMaxTokens": 32768,
        "decodeTokens": 512,
        "decodeContextTokens": 0,
        "concurrency": 2,
        "concurrencyTokens": 512,
        "modelPortSourceIdentitySha256": sha256_document(
            modelport_source_identity
        ),
        "liveModelRegistrySha256": registry["sha256"],
        "toolUseLocalProviderReady": tool_use_local_provider_ready,
    }
    identity["sha256"] = sha256_document(identity)
    if not qualification_input_valid(identity):
        raise RuntimeError("full qualification workload is not the reviewed v1 contract")
    return identity


def _live_target_runtime_identity(
    model: dict[str, Any], profile: str
) -> dict[str, str]:
    container_name = os.environ.get("QWEN_CONTAINER_NAME", model["id"])
    inspected = command_json(["docker", "inspect", container_name])
    container = inspected[0] if isinstance(inspected, list) and inspected else None
    if not isinstance(container, dict):
        raise RuntimeError("rollout qualification target runtime is unavailable")
    # ``runtime_mismatches`` renders the expected Compose configuration and
    # therefore launches Docker Compose indirectly.  Keep the transaction and
    # runner capabilities in this process only; an inspected helper executable
    # must never inherit mutation authority.
    with _without_child_authority():
        mismatches = runtime_mismatches(container, profile)
    state = container.get("State") or {}
    health = state.get("Health") or {}
    if (
        mismatches
        or state.get("Status") != "running"
        or (health and health.get("Status") != "healthy")
    ):
        detail = ", ".join(mismatches) if mismatches else "not healthy"
        raise RuntimeError(
            "rollout qualification target runtime is not canonical: " + detail
        )
    container_id = container.get("Id")
    image_id = container.get("Image")
    started_at = state.get("StartedAt")
    configuration_sha256 = live_runtime_sha256(container)
    if (
        re.fullmatch(r"[0-9a-f]{64}", str(container_id or "")) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(image_id or "")) is None
        or not isinstance(started_at, str)
        or not started_at
    ):
        raise RuntimeError("rollout qualification runtime process identity is incomplete")
    return {
        "containerId": container_id,
        "startedAt": started_at,
        "imageId": image_id,
        "containerConfigSha256": configuration_sha256,
    }


def _rollout_frozen_inputs(
    *,
    binding: dict[str, Any],
    model: dict[str, Any],
    configuration: dict[str, Any],
) -> dict[str, Any]:
    runtime_identity = _live_target_runtime_identity(model, "latency")
    modelport_identity = _modelport_source_identity()
    modelport_project = Path(os.environ.get("MODELPORT_PROJECT_DIR", ""))
    qualification_input = _target_provider_qualification_identity(
        catalog_model_id=model["id"],
        catalog_spec_sha256=binding["targetCatalogSpecSha256"],
        modelport_project=modelport_project,
        modelport_source_identity=modelport_identity,
    )
    if qualification_input["sha256"] != binding["qualificationInputSha256"]:
        raise RuntimeError("rollout qualification workload identity changed")
    frozen = {
        "policyId": ROLLOUT_FROZEN_INPUTS_POLICY,
        "catalogModelId": model["id"],
        "targetCatalogSpecSha256": binding["targetCatalogSpecSha256"],
        "liveRuntimeIdentitySha256": runtime_identity["containerConfigSha256"],
        "runtimeIdentity": runtime_identity,
        "acceptanceConfigurationSha256": sha256_document(configuration),
        "controllerMaterialIdentity": {
            "materialPolicy": configuration.get("materialPolicy"),
            "fileSetMaterialPolicy": configuration.get("fileSetMaterialPolicy"),
            "controlPlanePackageSha256": configuration.get(
                "controlPlanePackageSha256"
            ),
            "providerContractSha256": configuration.get("contractSha256"),
        },
        "modelPortSourceIdentity": modelport_identity,
    }
    frozen["sha256"] = sha256_document(frozen)
    if not rollout_frozen_inputs_valid(
        frozen, rollout_binding=binding, configuration=configuration
    ):
        raise RuntimeError("rollout qualification frozen inputs are invalid")
    return frozen


def _assert_bound_record_path(
    path: Path, binding: dict[str, Any], *, manifest: bool
) -> str:
    suffix = ".run.json" if manifest else ".json"
    expected_name = (
        f"qualification-{binding['transactionId']}-"
        f"{binding['actionOrdinal']}{suffix}"
    )
    relative = _relative_manifest_path(path) if manifest else _relative_evidence_path(path)
    if Path(relative).name != expected_name:
        raise RuntimeError(
            "rollout qualification record path does not match its transaction action"
        )
    return relative


def total_ram_gib() -> float:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def gpu_inventory() -> list[dict[str, Any]]:
    output = command_output(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if not output:
        return []
    gpus: list[dict[str, Any]] = []
    for line in output.splitlines():
        fields = [field.strip() for field in line.split(",", 3)]
        if len(fields) != 4:
            continue
        try:
            gpus.append(
                {
                    "index": int(fields[0]),
                    "name": fields[1],
                    "vramGiB": round(float(fields[2]) / 1024, 1),
                    "driver": fields[3],
                }
            )
        except ValueError:
            continue
    return gpus


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("acceptance runner timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("acceptance runner timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("acceptance runner timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _private_json(path: Path) -> dict[str, Any]:
    descriptor: int | None = None
    try:
        descriptor, metadata, _relative = open_private_project_file(
            path,
            project_root=ROOT_DIR,
            maximum_bytes=MAX_PRIVATE_JSON_BYTES,
        )
        if (
            stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_PRIVATE_JSON_BYTES
        ):
            raise RuntimeError("acceptance run manifest is not a private regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            document = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"cannot read acceptance run manifest: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(document, dict):
        raise RuntimeError("acceptance run manifest must be a JSON object")
    return document


def _manifest_self_hash(manifest: dict[str, Any]) -> str:
    return sha256_document(
        {key: value for key, value in manifest.items() if key != "selfSha256"}
    )


def _runner_capability_sha256() -> str:
    token = os.environ.get(RUNNER_TOKEN_ENV, "")
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise RuntimeError("acceptance evidence operations require the runner capability")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _validated_manifest(path: Path, *, require_final: bool = False) -> dict[str, Any]:
    _relative_manifest_path(path)
    manifest = _private_json(path)
    schema_version = manifest.get("schemaVersion")
    if (
        schema_version not in {RUN_SCHEMA_VERSION, ROLLOUT_RUN_SCHEMA_VERSION}
        or set(manifest)
        != RUN_MANIFEST_KEYS
        | (
            {"rolloutBinding", "frozenInputs"}
            if schema_version == ROLLOUT_RUN_SCHEMA_VERSION
            else set()
        )
        or manifest.get("kind") != RUN_KIND
        or manifest.get("selfSha256") != _manifest_self_hash(manifest)
        or manifest.get("runner")
        != {
            "path": RUNNER_PATH,
            "sha256": sha256_file(SUITE_PATH),
            "capabilitySha256": _runner_capability_sha256(),
        }
        or manifest.get("plan")
        != {
            "stepNames": list(expected_steps(manifest.get("mode"))),
            "sha256": step_plan_sha256(manifest.get("mode")),
        }
        or set(manifest.get("validationInput") or {})
        != {"policy", "catalogSha256", "repositorySha256", "sha256"}
        or any(
            not SHA256_PATTERN.fullmatch(
                str((manifest.get("validationInput") or {}).get(key, ""))
            )
            for key in ("catalogSha256", "repositorySha256", "sha256")
        )
        or (manifest.get("validationInput") or {}).get("policy")
        != VALIDATION_INPUT_POLICY
    ):
        raise RuntimeError("acceptance run manifest identity is invalid")
    if schema_version == ROLLOUT_RUN_SCHEMA_VERSION:
        if manifest.get("mode") != "full" or manifest.get("profile") != "latency":
            raise RuntimeError(
                "rollout qualification is restricted to full latency acceptance"
            )
        catalog, model = reviewed_catalog_model(manifest["catalogModelId"])
        configuration = _acceptance_configuration(model, "full", "latency")
        binding = _pending_rollout_binding(model["id"])
        if binding is None:
            raise RuntimeError("rollout qualification authority disappeared")
        _assert_bound_record_path(path, binding, manifest=True)
        frozen = _rollout_frozen_inputs(
            binding=binding,
            model=model,
            configuration=configuration,
        )
        if (
            manifest.get("rolloutBinding") != binding
            or manifest.get("frozenInputs") != frozen
            or manifest.get("validationInput")
            != validation_input(catalog, model, configuration)
        ):
            raise RuntimeError(
                "rollout qualification binding or frozen input identity changed"
            )
    if require_final and manifest.get("finalized") is not True:
        raise RuntimeError("acceptance run manifest is not finalized")
    return manifest


def initialize_manifest(
    path: Path,
    *,
    mode: str,
    profile: str,
    catalog_model_id: str,
    started_at: str,
) -> dict[str, Any]:
    _relative_manifest_path(path)
    _parse_time(started_at)
    catalog, model = reviewed_catalog_model(catalog_model_id)
    binding = _pending_rollout_binding(catalog_model_id)
    if binding is not None and (mode != "full" or profile != "latency"):
        raise RuntimeError(
            "rollout qualification is restricted to recorded full latency acceptance"
        )
    configuration = _acceptance_configuration(model, mode, profile)
    schema_version = (
        ROLLOUT_RUN_SCHEMA_VERSION if binding is not None else RUN_SCHEMA_VERSION
    )
    frozen_inputs = None
    if binding is not None:
        _assert_bound_record_path(path, binding, manifest=True)
        frozen_inputs = _rollout_frozen_inputs(
            binding=binding,
            model=model,
            configuration=configuration,
        )
    manifest = {
        "schemaVersion": schema_version,
        "kind": RUN_KIND,
        "runId": str(uuid.uuid4()),
        "mode": mode,
        "profile": profile,
        "catalogModelId": catalog_model_id,
        "runner": {
            "path": RUNNER_PATH,
            "sha256": sha256_file(SUITE_PATH),
            "capabilitySha256": _runner_capability_sha256(),
        },
        "plan": {
            "stepNames": list(expected_steps(mode)),
            "sha256": step_plan_sha256(mode),
        },
        "validationInput": validation_input(catalog, model, configuration),
        "startedAt": started_at,
        "finishedAt": None,
        "durationSeconds": None,
        "status": "running",
        "exitCode": None,
        "failedAtStep": None,
        "terminalStep": None,
        "stepResults": [],
        "finalized": False,
    }
    if binding is not None:
        manifest["rolloutBinding"] = binding
        manifest["frozenInputs"] = frozen_inputs
    manifest["selfSha256"] = _manifest_self_hash(manifest)
    if binding is not None:
        write_noreplace(path, manifest)
    else:
        atomic_write_private_json(path, manifest)
    return manifest


def append_step(
    path: Path,
    *,
    name: str,
    started_at: str,
    finished_at: str,
    duration_seconds: int,
    exit_code: int,
) -> dict[str, Any]:
    manifest = _validated_manifest(path)
    previous_self_sha256 = manifest["selfSha256"]
    if manifest.get("finalized") is True or manifest.get("status") != "running":
        raise RuntimeError("acceptance run manifest is already finalized")
    ordinal = len(manifest["stepResults"]) + 1
    planned = expected_steps(manifest["mode"])
    if ordinal > len(planned) or name != planned[ordinal - 1]:
        raise RuntimeError("acceptance step is missing, duplicated, or out of order")
    started = _parse_time(started_at)
    finished = _parse_time(finished_at)
    if (
        duration_seconds < 0
        or started > finished
        or abs((finished - started).total_seconds() - duration_seconds) > 5
    ):
        raise RuntimeError("acceptance step duration is invalid")
    manifest["stepResults"].append(
        {
            "ordinal": ordinal,
            "name": name,
            "startedAt": started_at,
            "finishedAt": finished_at,
            "durationSeconds": duration_seconds,
            "exitCode": exit_code,
            "status": "passed" if exit_code == 0 else "failed",
        }
    )
    manifest["terminalStep"] = name
    if exit_code != 0:
        manifest["failedAtStep"] = name
    manifest["selfSha256"] = _manifest_self_hash(manifest)
    if manifest["schemaVersion"] == ROLLOUT_RUN_SCHEMA_VERSION:
        _replace_bound_manifest(
            path,
            manifest,
            expected_self_sha256=previous_self_sha256,
        )
    else:
        atomic_write_private_json(path, manifest)
    return manifest


def finalize_manifest(
    path: Path,
    *,
    finished_at: str,
    duration_seconds: int,
    exit_code: int,
    failed_at_step: str,
) -> dict[str, Any]:
    manifest = _validated_manifest(path)
    previous_self_sha256 = manifest["selfSha256"]
    if manifest.get("finalized") is True:
        raise RuntimeError("acceptance run manifest is already finalized")
    started = _parse_time(manifest["startedAt"])
    finished = _parse_time(finished_at)
    if (
        duration_seconds < 0
        or started > finished
        or abs((finished - started).total_seconds() - duration_seconds) > 5
    ):
        raise RuntimeError("acceptance run duration is invalid")
    status = "passed" if exit_code == 0 else "failed"
    if status == "passed" and (
        [item["name"] for item in manifest["stepResults"]]
        != list(expected_steps(manifest["mode"]))
        or any(item["exitCode"] != 0 for item in manifest["stepResults"])
    ):
        raise RuntimeError("a partial or failed acceptance run cannot be finalized as passed")
    manifest.update(
        {
            "finishedAt": finished_at,
            "durationSeconds": duration_seconds,
            "status": status,
            "exitCode": exit_code,
            "failedAtStep": None if status == "passed" else failed_at_step,
            "terminalStep": (
                manifest.get("terminalStep")
                or (None if status == "passed" else failed_at_step)
            ),
            "finalized": True,
        }
    )
    manifest["selfSha256"] = _manifest_self_hash(manifest)
    if manifest["schemaVersion"] == ROLLOUT_RUN_SCHEMA_VERSION:
        _replace_bound_manifest(
            path,
            manifest,
            expected_self_sha256=previous_self_sha256,
        )
    else:
        atomic_write_private_json(path, manifest)
    return manifest


def _relative_manifest_path(path: Path) -> str:
    try:
        relative = path.absolute().relative_to(ROOT_DIR.absolute())
    except ValueError as exc:
        raise RuntimeError("acceptance run manifest must be inside the project") from exc
    if (
        relative.parts[:2] != ("logs", "acceptance")
        or not relative.name.endswith(".run.json")
        or ".." in relative.parts
    ):
        raise RuntimeError("acceptance run manifest must be logs/acceptance/*.json")
    return relative.as_posix()


def _relative_evidence_path(path: Path) -> str:
    try:
        relative = path.absolute().relative_to(ROOT_DIR.absolute())
    except ValueError as exc:
        raise RuntimeError("acceptance evidence output must be inside the project") from exc
    if (
        relative.parts[:2] != ("logs", "acceptance")
        or relative.suffix != ".json"
        or relative.name.endswith(".run.json")
        or ".." in relative.parts
    ):
        raise RuntimeError("acceptance evidence output must be logs/acceptance/*.json")
    return relative.as_posix()


def write_noreplace(path: Path, payload: dict[str, Any]) -> None:
    """Atomically publish one private JSON record without replacing a name."""

    body = (
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    )
    if len(body) > MAX_PRIVATE_JSON_BYTES:
        raise RuntimeError("acceptance record exceeds the bounded size policy")
    ensure_private_project_directory(path.parent, project_root=ROOT_DIR)
    directory_descriptor, filename, _relative = _open_project_parent(
        path, ROOT_DIR
    )
    temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        metadata = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RuntimeError(
                "acceptance record directory is not current-user owned"
            )
        cleanup_interrupted_noreplace_link_at(directory_descriptor, filename)
        try:
            os.stat(filename, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("acceptance record already exists")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("acceptance record write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary_name,
                filename,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise RuntimeError("acceptance record already exists") from exc
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_name = ""
        os.fsync(directory_descriptor)
        published = os.stat(
            filename, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_uid != os.getuid()
            or published.st_nlink != 1
            or stat.S_IMODE(published.st_mode) != 0o600
        ):
            raise RuntimeError("published acceptance record identity is invalid")
        verification_parent, _name, _relative = _open_project_parent(path, ROOT_DIR)
        try:
            current_parent = os.fstat(verification_parent)
            if (current_parent.st_dev, current_parent.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise RuntimeError(
                    "acceptance record directory changed during publication"
                )
        finally:
            os.close(verification_parent)
    except OSError as exc:
        raise RuntimeError("cannot publish private acceptance record") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        os.close(directory_descriptor)


def _replace_bound_manifest(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_self_sha256: str,
) -> None:
    """Replace a bound manifest only if the same parent and record are current."""

    body = (
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    )
    if len(body) > MAX_PRIVATE_JSON_BYTES:
        raise RuntimeError("acceptance run manifest exceeds the bounded size policy")
    directory_descriptor, filename, _relative = _open_project_parent(path, ROOT_DIR)
    source_descriptor: int | None = None
    temporary_descriptor: int | None = None
    temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
    try:
        parent_metadata = os.fstat(directory_descriptor)
        if (
            parent_metadata.st_uid != os.getuid()
            or stat.S_IMODE(parent_metadata.st_mode) & 0o077
        ):
            raise RuntimeError(
                "acceptance run manifest directory is not private"
            )
        source_descriptor = os.open(
            filename,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        source_metadata = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_uid != os.getuid()
            or source_metadata.st_nlink != 1
            or stat.S_IMODE(source_metadata.st_mode) != 0o600
            or source_metadata.st_size > MAX_PRIVATE_JSON_BYTES
        ):
            raise RuntimeError("acceptance run manifest identity changed")
        with os.fdopen(os.dup(source_descriptor), "r", encoding="utf-8") as handle:
            current = json.load(handle)
        if (
            not isinstance(current, dict)
            or current.get("selfSha256") != expected_self_sha256
            or current.get("selfSha256") != _manifest_self_hash(current)
        ):
            raise RuntimeError("acceptance run manifest changed before update")
        named = os.stat(filename, dir_fd=directory_descriptor, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (
            source_metadata.st_dev,
            source_metadata.st_ino,
        ):
            raise RuntimeError("acceptance run manifest path changed before update")
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        view = memoryview(body)
        while view:
            written = os.write(temporary_descriptor, view)
            if written <= 0:
                raise RuntimeError("acceptance run manifest write made no progress")
            view = view[written:]
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None
        final_named = os.stat(
            filename, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (final_named.st_dev, final_named.st_ino) != (
            source_metadata.st_dev,
            source_metadata.st_ino,
        ):
            raise RuntimeError("acceptance run manifest changed during update")
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporary_name = ""
        os.fsync(directory_descriptor)
        verification_parent, _name, _relative = _open_project_parent(path, ROOT_DIR)
        try:
            current_parent = os.fstat(verification_parent)
            if (current_parent.st_dev, current_parent.st_ino) != (
                parent_metadata.st_dev,
                parent_metadata.st_ino,
            ):
                raise RuntimeError(
                    "acceptance run manifest directory changed during update"
                )
        finally:
            os.close(verification_parent)
        if _private_json(path) != payload:
            raise RuntimeError("acceptance run manifest write verification failed")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("cannot safely update acceptance run manifest") from exc
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        os.close(directory_descriptor)


def _run_evidence(
    manifest_path: Path,
    manifest: dict[str, Any],
    configuration: dict[str, Any],
) -> dict[str, Any]:
    schema_version = manifest["schemaVersion"]
    run = {
        "schemaVersion": schema_version,
        "kind": RUN_KIND,
        "runId": manifest["runId"],
        "mode": manifest["mode"],
        "runner": manifest["runner"],
        "plan": manifest["plan"],
        "stepResults": manifest["stepResults"],
        "failedAtStep": manifest["failedAtStep"],
        "terminalStep": manifest["terminalStep"],
        "manifest": {
            "schemaVersion": schema_version,
            "sourcePath": _relative_manifest_path(manifest_path),
            "sourceSha256": sha256_file(manifest_path),
            "selfSha256": manifest["selfSha256"],
        },
    }
    if schema_version == ROLLOUT_RUN_SCHEMA_VERSION:
        run.update(
            {
                "profile": manifest["profile"],
                "catalogModelId": manifest["catalogModelId"],
                "rolloutBinding": manifest["rolloutBinding"],
                "frozenInputs": manifest["frozenInputs"],
            }
        )
    if not run_record_valid(
        run,
        mode=manifest["mode"],
        overall_status=manifest["status"],
        overall_exit_code=manifest["exitCode"],
        configuration=configuration,
    ):
        raise RuntimeError("acceptance run manifest does not prove the claimed result")
    return run


def build_payload(output: Path, manifest_path: Path) -> dict[str, Any]:
    _relative_evidence_path(output)
    manifest = _validated_manifest(manifest_path, require_final=True)
    bound = manifest["schemaVersion"] == ROLLOUT_RUN_SCHEMA_VERSION
    if bound:
        _assert_bound_record_path(output, manifest["rolloutBinding"], manifest=False)
    catalog, model = reviewed_catalog_model(manifest["catalogModelId"])
    configuration = _acceptance_configuration(
        model, manifest["mode"], manifest["profile"]
    )
    current_validation_input = validation_input(catalog, model, configuration)
    if (
        manifest["status"] == "passed"
        and manifest["validationInput"] != current_validation_input
    ):
        raise RuntimeError("catalog validation inputs changed during the acceptance run")
    artifact = next(item for item in model["artifacts"] if item["role"] == "model")
    try:
        artifact_identity = local_artifact_identity(
            MODELS_DIR / model["modelDirectory"] / artifact["filename"],
            INTEGRITY_DIR / f"{model['id']}--{artifact['filename']}.sha256.stamp",
            expected_bytes=artifact["bytes"],
            expected_sha256=artifact["sha256"],
            project_root=ROOT_DIR,
        )
    except RuntimeError:
        artifact_identity = {
            "schemaVersion": 1,
            "verification": "unavailable",
            "requiresFullSha256Verification": True,
        }
    if (
        manifest["status"] == "passed"
        and artifact_identity.get("requiresFullSha256Verification") is not False
    ):
        raise RuntimeError(
            "passed acceptance evidence requires a current secure artifact integrity "
            "stamp; run the full SHA256 verifier"
        )
    container_name = os.environ.get("QWEN_CONTAINER_NAME", model["id"])
    inspected = command_json(["docker", "inspect", container_name])
    container = inspected[0] if isinstance(inspected, list) and inspected else {}
    if container:
        with _without_child_authority():
            mismatches = runtime_mismatches(container, manifest["profile"])
    else:
        mismatches = ["missing"]
    if manifest["status"] == "passed" and mismatches:
        raise RuntimeError(
            "live runtime does not match the canonical rendered Compose configuration: "
            + ", ".join(mismatches)
        )
    runtime_identity = live_runtime_sha256(container) if container else None
    if bound and runtime_identity != manifest["frozenInputs"][
        "liveRuntimeIdentitySha256"
    ]:
        raise RuntimeError(
            "rollout qualification runtime identity changed before evidence write"
        )
    fingerprint = host_fingerprint()
    if not fingerprint:
        raise RuntimeError(
            "cannot create host-bound acceptance evidence without a machine id"
        )
    run = _run_evidence(manifest_path, manifest, configuration)
    git_commit = command_output(["git", "-C", str(ROOT_DIR), "rev-parse", "HEAD"])
    git_status = command_output(
        ["git", "-C", str(ROOT_DIR), "status", "--porcelain"]
    )
    payload = {
        "schemaVersion": (
            ROLLOUT_EVIDENCE_SCHEMA_VERSION if bound else 4
        ),
        "evidenceId": output.stem,
        "mode": manifest["mode"],
        "profile": manifest["profile"],
        "status": manifest["status"],
        "exitCode": manifest["exitCode"],
        "failedAtStep": manifest["failedAtStep"],
        "terminalStep": manifest["terminalStep"],
        "startedAt": manifest["startedAt"],
        "finishedAt": manifest["finishedAt"],
        "durationSeconds": manifest["durationSeconds"],
        "gitCommit": git_commit or "uncommitted",
        "gitState": "clean" if git_commit and git_status == "" else "dirty",
        "catalogModelId": model["id"],
        "validationInput": current_validation_input,
        "run": run,
        "host": {
            "platform": platform.system().lower(),
            "environmentKind": environment_kind(),
            "architecture": platform.machine(),
            "ramGiB": total_ram_gib(),
            "gpus": gpu_inventory(),
            "fingerprintType": HOST_FINGERPRINT_TYPE,
            "fingerprint": fingerprint,
        },
        "artifact": {
            "filename": artifact["filename"],
            "bytes": artifact["bytes"],
            "sha256": artifact["sha256"],
            "modelRevision": model["modelRevision"],
            "artifactRevision": model["artifactRevision"],
            "integrityVerified": manifest["status"] == "passed",
            "localIdentity": artifact_identity,
        },
        "runtime": {
            "containerName": container_name,
            "configuredImage": (container.get("Config") or {}).get("Image"),
            "imageId": container.get("Image"),
            "containerConfigSha256": (
                runtime_identity
            ),
            "state": (container.get("State") or {}).get("Status"),
            "health": ((container.get("State") or {}).get("Health") or {}).get(
                "Status"
            ),
        },
        "configuration": configuration,
        "freshnessPolicy": {"maxAgeDays": 30, "futureSkewSeconds": 300},
        "privacy": (
            "synthetic acceptance traffic only; application-scoped hash of machine "
            "id; no raw machine id, hostname, prompt, response, tool arguments, "
            "credentials, or raw container environment"
        ),
    }
    if bound:
        payload["rolloutBinding"] = manifest["rolloutBinding"]
        payload["frozenInputs"] = manifest["frozenInputs"]
        # Close the build window as well as the runner-step windows.  The
        # transaction, controller, target runtime, and ModelPort identities
        # must still be the exact pending qualification subject.
        if _validated_manifest(manifest_path, require_final=True) != manifest:
            raise RuntimeError(
                "rollout qualification identity changed while evidence was built"
            )
    payload["selfSha256"] = payload_sha256(payload)
    return payload


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_private_json(path, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run-start")
    start.add_argument("--output", type=Path, required=True)
    start.add_argument("--mode", choices=tuple(STEP_PLANS), required=True)
    start.add_argument("--profile", choices=("latency", "throughput"), default="latency")
    start.add_argument("--catalog-model-id", required=True)
    start.add_argument("--started-at", required=True)
    step = commands.add_parser("run-step")
    step.add_argument("--manifest", type=Path, required=True)
    step.add_argument("--name", required=True)
    step.add_argument("--started-at", required=True)
    step.add_argument("--finished-at", required=True)
    step.add_argument("--duration-seconds", type=int, required=True)
    step.add_argument("--exit-code", type=int, required=True)
    finish = commands.add_parser("run-finish")
    finish.add_argument("--manifest", type=Path, required=True)
    finish.add_argument("--finished-at", required=True)
    finish.add_argument("--duration-seconds", type=int, required=True)
    finish.add_argument("--exit-code", type=int, required=True)
    finish.add_argument("--failed-at-step", required=True)
    write = commands.add_parser("write")
    write.add_argument("--output", type=Path, required=True)
    write.add_argument("--run-manifest", type=Path, required=True)
    preflight = commands.add_parser("qualification-preflight")
    preflight.add_argument("--modelport-project", type=Path, required=True)
    preflight.add_argument("--catalog-model-id", required=True)
    preflight.add_argument("--catalog-spec-sha256", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "run-start":
        initialize_manifest(
            args.output,
            mode=args.mode,
            profile=args.profile,
            catalog_model_id=args.catalog_model_id,
            started_at=args.started_at,
        )
    elif args.command == "run-step":
        append_step(
            args.manifest,
            name=args.name,
            started_at=args.started_at,
            finished_at=args.finished_at,
            duration_seconds=args.duration_seconds,
            exit_code=args.exit_code,
        )
    elif args.command == "run-finish":
        finalize_manifest(
            args.manifest,
            finished_at=args.finished_at,
            duration_seconds=args.duration_seconds,
            exit_code=args.exit_code,
            failed_at_step=args.failed_at_step,
        )
    elif args.command == "write":
        payload = build_payload(args.output, args.run_manifest)
        if payload["schemaVersion"] == ROLLOUT_EVIDENCE_SCHEMA_VERSION:
            write_noreplace(args.output, payload)
        else:
            write_atomic(args.output, payload)
        print(f"Acceptance evidence: {_relative_evidence_path(args.output)}")
    else:
        identity = _modelport_source_identity(args.modelport_project)
        qualification_input = _target_provider_qualification_identity(
            catalog_model_id=args.catalog_model_id,
            catalog_spec_sha256=args.catalog_spec_sha256,
            modelport_project=args.modelport_project,
            modelport_source_identity=identity,
        )
        print(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "status": "ready",
                    "modelPortSourceIdentity": identity,
                    "qualificationInput": qualification_input,
                    "prerequisites": {
                        "nodeMajor": 24,
                        "credentials": "available",
                        "providerCompatibility": "passed",
                        "dashboard": "ok",
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
