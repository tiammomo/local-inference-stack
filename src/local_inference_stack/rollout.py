"""Immutable rollback identities and private content-addressed storage.

This module is deliberately limited to pure validation and repository-local
state.  It does not inspect Docker, Git, hardware, or the network, and it never
mutates a runtime.  Callers must pass components that they have already
observed and verified.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from .deployment import CatalogDeploymentSpec, DeploymentSpecError
from .materials import (
    canonical_bytes,
    canonical_sha256,
    cleanup_interrupted_noreplace_link_at,
)
from .paths import ProjectPaths


ROLLBACK_SPEC_SCHEMA_VERSION = 1
ROLLBACK_SPEC_KIND = "local-inference-stack/rollback-spec"
ROLLBACK_POINTER_SCHEMA_VERSION = 1
ROLLBACK_POINTER_KIND = "local-inference-stack/rollback-pointer"
ROLLBACK_SCOPE_POLICY = "same-controller-same-catalog-anchor-v1"
CONTROLLER_MATERIAL_POLICY = (
    "local-inference-stack/rollback-controller-materials-v1"
)
MAX_ROLLBACK_SPEC_BYTES = 4 * 1024 * 1024
MAX_ROLLBACK_POINTER_BYTES = 64 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}")
_IMAGE_REFERENCE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?Z"
)

_SPEC_KEYS = {
    "schemaVersion",
    "kind",
    "scopePolicy",
    "catalogSpecSha256",
    "catalogSpec",
    "selection",
    "runtimeProfile",
    "artifacts",
    "runtime",
    "controller",
    "host",
    "acceptance",
    "sourceTransactionId",
    "capturedAt",
    "selfSha256",
}
_SELECTION_KEYS = {
    "QWEN_CATALOG_ID",
    "QWEN_MODEL_DIR",
    "QWEN_MODEL_FILE",
    "QWEN_MODEL_DISPLAY_NAME",
    "QWEN_QUANTIZATION",
    "QWEN_SERVED_MODEL_ID",
    "QWEN_CONTAINER_NAME",
    "QWEN_RUNTIME_UID",
    "QWEN_RUNTIME_GID",
    "MODELPORT_NETWORK_NAME",
    "QWEN_CTX_SIZE",
    "QWEN_RECOMMENDED_INPUT_TOKENS",
    "QWEN_N_PREDICT",
    "QWEN_CACHE_RAM",
    "QWEN_CACHE_TYPE_K",
    "QWEN_CACHE_TYPE_V",
    "QWEN_BATCH_SIZE",
    "QWEN_UBATCH_SIZE",
}
_RUNTIME_ENVIRONMENT_FIELDS = {
    "QWEN_CTX_SIZE": "contextTokens",
    "QWEN_RECOMMENDED_INPUT_TOKENS": "recommendedInputTokens",
    "QWEN_N_PREDICT": "maxOutputTokens",
    "QWEN_CACHE_RAM": "cacheRamMiB",
    "QWEN_CACHE_TYPE_K": "cacheTypeK",
    "QWEN_CACHE_TYPE_V": "cacheTypeV",
    "QWEN_BATCH_SIZE": "batchSize",
    "QWEN_UBATCH_SIZE": "ubatchSize",
}
_SELECTION_DOCUMENT_KEYS = {
    "format",
    "values",
    "sha256",
    "containsCredentials",
}
_PROFILE_KEYS = {"name", "environment", "sha256"}
_ARTIFACT_KEYS = {"role", "relativePath", "bytes", "sha256"}
_RUNTIME_KEYS = {
    "configuredImage",
    "imageId",
    "effectiveComposeSha256",
    "expectedRuntimeIdentitySha256",
    "expectedRuntimeConfiguration",
}
_RUNTIME_CONFIGURATION_KEYS = {
    "image",
    "imageId",
    "user",
    "entrypoint",
    "command",
    "workingDirectory",
    "healthcheck",
    "environmentKeys",
    "environmentSha256",
    "privileged",
    "readOnly",
    "capAdd",
    "capDrop",
    "securityOpt",
    "pidsLimit",
    "init",
    "shmSize",
    "tmpfs",
    "restart",
    "logging",
    "publishAllPorts",
    "portBindings",
    "networkMode",
    "pidMode",
    "ipcMode",
    "utsMode",
    "usernsMode",
    "cgroupnsMode",
    "networks",
    "mounts",
    "binds",
    "devices",
    "deviceRequests",
    "groupAdd",
    "extraHosts",
    "links",
    "dns",
    "dnsOptions",
    "dnsSearch",
    "sysctls",
}
_RESTART_KEYS = {"name", "maximumRetryCount"}
_PORT_BINDING_KEYS = {"HostIp", "HostPort"}
_MOUNT_KEYS = {
    "type",
    "source",
    "destination",
    "rw",
    "mode",
    "propagation",
}
_DEVICE_KEYS = {"pathOnHost", "pathInContainer", "cgroupPermissions"}
_DEVICE_REQUEST_KEYS = {
    "driver",
    "count",
    "deviceIds",
    "capabilities",
    "options",
}
_CONTROLLER_KEYS = {
    "gitCommit",
    "trackedDirty",
    "materialPolicy",
    "materials",
    "materialsSha256",
}
_MATERIAL_KEYS = {"path", "sha256"}
_HOST_KEYS = {
    "fingerprintType",
    "fingerprint",
    "environmentKind",
    "architecture",
}
_ACCEPTANCE_KEYS = {
    "mode",
    "status",
    "evidencePath",
    "evidenceSha256",
    "evidenceSelfSha256",
    "finishedAt",
}
_POINTER_KEYS = {
    "schemaVersion",
    "kind",
    "scopePolicy",
    "generation",
    "activeSpecSha256",
    "previousSpecSha256",
    "updatedAt",
    "updatedByTransactionId",
}


class RolloutError(RuntimeError):
    """Base error for rollback identities and storage."""


class RollbackSpecError(RolloutError):
    """A rollback identity is incomplete, ambiguous, or internally inconsistent."""


class RollbackStoreError(RolloutError):
    """Private rollback state cannot be read or published safely."""


class RollbackCASMismatch(RollbackStoreError):
    """The active rollback pointer changed since the caller observed it."""


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise RollbackSpecError(f"{label} has an invalid shape")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RollbackSpecError(f"{label} must be a lowercase SHA256")
    return value


def _canonical_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise RollbackSpecError(f"{label} must be a canonical UUID")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError as error:
        raise RollbackSpecError(f"{label} must be a canonical UUID") from error
    if parsed != value:
        raise RollbackSpecError(f"{label} must be a canonical UUID")
    return value


def _utc_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise RollbackSpecError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RollbackSpecError(
            f"{label} must be an RFC3339 UTC timestamp"
        ) from error
    return value


def _safe_relative_path(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise RollbackSpecError(f"{label} must be a safe relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RollbackSpecError(f"{label} must be a safe relative POSIX path")
    return value


def _json_clone(value: Any, label: str) -> Any:
    try:
        return json.loads(canonical_bytes(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RollbackSpecError(f"{label} is not canonical JSON data") from error


def _validate_selection(
    value: Any, catalog_spec: CatalogDeploymentSpec
) -> dict[str, Any]:
    selection = _exact_keys(value, _SELECTION_DOCUMENT_KEYS, "rollback selection")
    values = selection.get("values")
    if not isinstance(values, dict) or set(values) != _SELECTION_KEYS:
        raise RollbackSpecError("rollback selection has an invalid key set")
    if not all(
        isinstance(key, str)
        and isinstance(item, str)
        and item
        and "\n" not in item
        and "\r" not in item
        for key, item in values.items()
    ):
        raise RollbackSpecError("rollback selection contains an unsafe value")
    if (
        selection.get("format") != "allowlisted-env-v1"
        or selection.get("containsCredentials") is not False
        or selection.get("sha256") != canonical_sha256(values)
    ):
        raise RollbackSpecError("rollback selection identity is invalid")

    primary = [
        artifact
        for artifact in catalog_spec.artifacts
        if artifact.required and artifact.role == "model"
    ]
    if len(primary) != 1:
        raise RollbackSpecError("rollback Catalog spec has no unique primary artifact")
    expected = {
        "QWEN_CATALOG_ID": catalog_spec.catalog_id,
        "QWEN_MODEL_DIR": f"./models/{catalog_spec.model_directory}",
        "QWEN_MODEL_FILE": primary[0].filename,
        "QWEN_QUANTIZATION": catalog_spec.quantization,
        "QWEN_SERVED_MODEL_ID": catalog_spec.served_model_id,
        "QWEN_CONTAINER_NAME": catalog_spec.catalog_id,
        "MODELPORT_NETWORK_NAME": "modelport_default",
    }
    runtime_document = catalog_spec.runtime.document()
    expected.update(
        {
            environment_key: str(runtime_document[field])
            for environment_key, field in _RUNTIME_ENVIRONMENT_FIELDS.items()
        }
    )
    if any(values.get(key) != item for key, item in expected.items()):
        raise RollbackSpecError(
            "rollback selection does not match its immutable Catalog spec"
        )
    if not values["QWEN_RUNTIME_UID"].isdigit() or not values[
        "QWEN_RUNTIME_GID"
    ].isdigit():
        raise RollbackSpecError("rollback runtime uid/gid must be decimal integers")
    return selection


def _validate_profile(value: Any) -> dict[str, Any]:
    profile = _exact_keys(value, _PROFILE_KEYS, "rollback runtime profile")
    name = profile.get("name")
    environment = profile.get("environment")
    if (
        name != "latency"
        or environment != {"QWEN_PARALLEL": "1"}
        or profile.get("sha256")
        != canonical_sha256({"name": name, "environment": environment})
    ):
        raise RollbackSpecError("rollback runtime profile identity is invalid")
    return profile


def _validate_artifacts(
    value: Any, catalog_spec: CatalogDeploymentSpec
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RollbackSpecError("rollback artifacts must be a list")
    expected = [
        {
            "role": artifact.role,
            "relativePath": (
                f"models/{catalog_spec.model_directory}/{artifact.filename}"
            ),
            "bytes": artifact.bytes,
            "sha256": artifact.sha256,
        }
        for artifact in catalog_spec.artifacts
        if artifact.required
    ]
    for item in value:
        artifact = _exact_keys(item, _ARTIFACT_KEYS, "rollback artifact")
        _safe_relative_path(artifact.get("relativePath"), "rollback artifact path")
        if (
            not isinstance(artifact.get("role"), str)
            or not artifact["role"]
            or not isinstance(artifact.get("bytes"), int)
            or isinstance(artifact.get("bytes"), bool)
            or artifact["bytes"] <= 0
        ):
            raise RollbackSpecError("rollback artifact metadata is invalid")
        _sha256(artifact.get("sha256"), "rollback artifact SHA256")
    if value != expected:
        raise RollbackSpecError(
            "rollback artifacts do not match the required Catalog artifacts"
        )
    return value


def _string_list(
    value: Any,
    label: str,
    *,
    sorted_values: bool = False,
    unique: bool = False,
) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise RollbackSpecError(f"{label} must be a string list")
    if sorted_values and value != sorted(value):
        raise RollbackSpecError(f"{label} must be sorted")
    if unique and len(value) != len(set(value)):
        raise RollbackSpecError(f"{label} must not contain duplicates")
    return value


def _string_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise RollbackSpecError(f"{label} must be a string map")
    return value


def _validate_runtime_configuration(value: Any) -> dict[str, Any]:
    configuration = _exact_keys(
        value,
        _RUNTIME_CONFIGURATION_KEYS,
        "expected runtime configuration",
    )
    if (
        not isinstance(configuration["image"], str)
        or _IMAGE_REFERENCE.fullmatch(configuration["image"]) is None
        or not isinstance(configuration["imageId"], str)
        or _IMAGE_ID.fullmatch(configuration["imageId"]) is None
        or (
            configuration["user"] is not None
            and not isinstance(configuration["user"], str)
        )
        or not isinstance(configuration["workingDirectory"], str)
        or not isinstance(configuration["healthcheck"], dict)
        or not isinstance(configuration["logging"], dict)
        or not all(
            type(configuration[field]) is bool
            for field in ("privileged", "readOnly", "init", "publishAllPorts")
        )
        or not all(
            configuration[field] is None
            or (
                isinstance(configuration[field], int)
                and not isinstance(configuration[field], bool)
            )
            for field in ("pidsLimit", "shmSize")
        )
        or not all(
            isinstance(configuration[field], str)
            for field in (
                "networkMode",
                "pidMode",
                "ipcMode",
                "utsMode",
                "usernsMode",
                "cgroupnsMode",
            )
        )
    ):
        raise RollbackSpecError(
            "expected runtime configuration contains an invalid scalar or object"
        )

    _string_list(configuration["entrypoint"], "runtime entrypoint")
    _string_list(configuration["command"], "runtime command")
    _string_list(
        configuration["environmentKeys"],
        "runtime environment keys",
        sorted_values=True,
        unique=True,
    )
    _sha256(configuration["environmentSha256"], "runtime environment SHA256")
    for field in ("capAdd", "capDrop", "securityOpt"):
        _string_list(
            configuration[field],
            f"runtime {field}",
            sorted_values=True,
        )
    for field in (
        "binds",
        "groupAdd",
        "extraHosts",
        "links",
        "dns",
        "dnsOptions",
        "dnsSearch",
    ):
        _string_list(
            configuration[field],
            f"runtime {field}",
            sorted_values=True,
        )

    restart = _exact_keys(
        configuration["restart"], _RESTART_KEYS, "runtime restart policy"
    )
    if (
        not isinstance(restart["name"], str)
        or not isinstance(restart["maximumRetryCount"], int)
        or isinstance(restart["maximumRetryCount"], bool)
    ):
        raise RollbackSpecError("runtime restart policy contains an invalid value")

    tmpfs = configuration["tmpfs"]
    if not isinstance(tmpfs, dict) or not all(
        isinstance(path, str)
        and _string_list(
            options,
            "runtime tmpfs options",
            sorted_values=True,
        )
        is options
        for path, options in tmpfs.items()
    ):
        raise RollbackSpecError("runtime tmpfs must be a string-to-string-list map")

    port_bindings = configuration["portBindings"]
    if not isinstance(port_bindings, dict):
        raise RollbackSpecError("runtime port bindings must be an object")
    for port, bindings in port_bindings.items():
        if not isinstance(port, str) or not isinstance(bindings, list):
            raise RollbackSpecError("runtime port bindings have an invalid shape")
        normalized_bindings: list[tuple[str, str]] = []
        for item in bindings:
            binding = _exact_keys(
                item, _PORT_BINDING_KEYS, "runtime port binding"
            )
            if not all(isinstance(binding[key], str) for key in _PORT_BINDING_KEYS):
                raise RollbackSpecError("runtime port binding contains an invalid value")
            normalized_bindings.append((binding["HostIp"], binding["HostPort"]))
        if normalized_bindings != sorted(normalized_bindings):
            raise RollbackSpecError("runtime port bindings must be sorted")

    networks = configuration["networks"]
    if not isinstance(networks, dict):
        raise RollbackSpecError("runtime networks must be an object")
    for name, aliases in networks.items():
        if not isinstance(name, str):
            raise RollbackSpecError("runtime network name must be a string")
        _string_list(
            aliases,
            "runtime network aliases",
            sorted_values=True,
        )

    mounts = configuration["mounts"]
    if not isinstance(mounts, list):
        raise RollbackSpecError("runtime mounts must be a list")
    mount_order: list[tuple[str, str, str]] = []
    for item in mounts:
        mount = _exact_keys(item, _MOUNT_KEYS, "runtime mount")
        if (
            not all(
                isinstance(mount[field], str)
                for field in ("type", "source", "destination", "mode", "propagation")
            )
            or type(mount["rw"]) is not bool
        ):
            raise RollbackSpecError("runtime mount contains an invalid value")
        mount_order.append((mount["destination"], mount["source"], mount["type"]))
    if mount_order != sorted(mount_order):
        raise RollbackSpecError("runtime mounts must be sorted")

    devices = configuration["devices"]
    if not isinstance(devices, list):
        raise RollbackSpecError("runtime devices must be a list")
    device_order: list[tuple[str, str, str]] = []
    for item in devices:
        device = _exact_keys(item, _DEVICE_KEYS, "runtime device")
        if not all(isinstance(device[field], str) for field in _DEVICE_KEYS):
            raise RollbackSpecError("runtime device contains an invalid value")
        device_order.append(
            (
                device["pathInContainer"],
                device["pathOnHost"],
                device["cgroupPermissions"],
            )
        )
    if device_order != sorted(device_order):
        raise RollbackSpecError("runtime devices must be sorted")

    requests = configuration["deviceRequests"]
    if not isinstance(requests, list):
        raise RollbackSpecError("runtime device requests must be a list")
    for item in requests:
        request = _exact_keys(
            item, _DEVICE_REQUEST_KEYS, "runtime device request"
        )
        if (
            not isinstance(request["driver"], str)
            or not isinstance(request["count"], int)
            or isinstance(request["count"], bool)
        ):
            raise RollbackSpecError("runtime device request contains an invalid value")
        _string_list(
            request["deviceIds"],
            "runtime device request ids",
            sorted_values=True,
        )
        capabilities = request["capabilities"]
        if not isinstance(capabilities, list):
            raise RollbackSpecError(
                "runtime device request capabilities must be a list"
            )
        for group in capabilities:
            _string_list(
                group,
                "runtime device request capability group",
                sorted_values=True,
            )
        if capabilities != sorted(capabilities):
            raise RollbackSpecError(
                "runtime device request capabilities must be sorted"
            )
        _string_map(request["options"], "runtime device request options")
    request_order = [canonical_bytes(item) for item in requests]
    if request_order != sorted(request_order):
        raise RollbackSpecError("runtime device requests must be sorted")

    _string_map(configuration["sysctls"], "runtime sysctls")
    return configuration


def _validate_runtime(value: Any) -> dict[str, Any]:
    runtime = _exact_keys(value, _RUNTIME_KEYS, "rollback runtime")
    configuration = _validate_runtime_configuration(
        runtime.get("expectedRuntimeConfiguration")
    )
    if (
        not isinstance(runtime.get("configuredImage"), str)
        or _IMAGE_REFERENCE.fullmatch(runtime["configuredImage"]) is None
        or not isinstance(runtime.get("imageId"), str)
        or _IMAGE_ID.fullmatch(runtime["imageId"]) is None
    ):
        raise RollbackSpecError("rollback runtime image identity is invalid")
    _sha256(runtime.get("effectiveComposeSha256"), "effective Compose SHA256")
    identity_sha256 = _sha256(
        runtime.get("expectedRuntimeIdentitySha256"),
        "expected runtime identity SHA256",
    )
    if canonical_sha256(configuration) != identity_sha256:
        raise RollbackSpecError(
            "rollback runtime configuration does not match its identity SHA256"
        )
    if (
        configuration.get("image") != runtime["configuredImage"]
        or configuration.get("imageId") != runtime["imageId"]
    ):
        raise RollbackSpecError(
            "rollback runtime configuration does not match its image identity"
        )
    return runtime


def _validate_controller(value: Any) -> dict[str, Any]:
    controller = _exact_keys(value, _CONTROLLER_KEYS, "rollback controller")
    commit = controller.get("gitCommit")
    materials = controller.get("materials")
    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        or controller.get("trackedDirty") is not False
        or controller.get("materialPolicy") != CONTROLLER_MATERIAL_POLICY
        or not isinstance(materials, list)
        or not materials
    ):
        raise RollbackSpecError("rollback controller identity is invalid")
    previous_path: str | None = None
    for item in materials:
        material = _exact_keys(item, _MATERIAL_KEYS, "rollback controller material")
        path = _safe_relative_path(material.get("path"), "controller material path")
        _sha256(material.get("sha256"), "controller material SHA256")
        if previous_path is not None and path <= previous_path:
            raise RollbackSpecError(
                "rollback controller materials must be uniquely sorted by path"
            )
        previous_path = path
    if controller.get("materialsSha256") != canonical_sha256(materials):
        raise RollbackSpecError("rollback controller material digest is invalid")
    return controller


def _validate_host(value: Any) -> dict[str, Any]:
    host = _exact_keys(value, _HOST_KEYS, "rollback host")
    if (
        host.get("fingerprintType") != "machine-id-sha256-v1"
        or _SHA256.fullmatch(str(host.get("fingerprint", ""))) is None
        or host.get("environmentKind") != "wsl2"
        or host.get("architecture") != "x86_64"
    ):
        raise RollbackSpecError("rollback host identity is outside the v1 scope")
    return host


def _validate_acceptance(value: Any) -> dict[str, Any]:
    acceptance = _exact_keys(value, _ACCEPTANCE_KEYS, "rollback acceptance")
    path = _safe_relative_path(
        acceptance.get("evidencePath"), "rollback acceptance evidence path"
    )
    if (
        acceptance.get("mode") not in {"quick", "full"}
        or acceptance.get("status") != "passed"
        or not path.startswith("logs/acceptance/")
        or not path.endswith(".json")
        or path.endswith(".run.json")
    ):
        raise RollbackSpecError("rollback acceptance identity is invalid")
    _sha256(acceptance.get("evidenceSha256"), "acceptance evidence SHA256")
    _sha256(
        acceptance.get("evidenceSelfSha256"),
        "acceptance evidence self SHA256",
    )
    _utc_timestamp(acceptance.get("finishedAt"), "acceptance finishedAt")
    return acceptance


def _validate_spec_document(value: Any) -> dict[str, Any]:
    document = _exact_keys(value, _SPEC_KEYS, "rollback spec")
    if (
        document.get("schemaVersion") != ROLLBACK_SPEC_SCHEMA_VERSION
        or document.get("kind") != ROLLBACK_SPEC_KIND
        or document.get("scopePolicy") != ROLLBACK_SCOPE_POLICY
    ):
        raise RollbackSpecError("rollback spec policy or schema is unsupported")
    try:
        catalog_spec = CatalogDeploymentSpec.from_document(document.get("catalogSpec"))
    except DeploymentSpecError as error:
        raise RollbackSpecError(f"rollback Catalog spec is invalid: {error}") from error
    if document.get("catalogSpecSha256") != catalog_spec.sha256:
        raise RollbackSpecError("rollback Catalog spec digest does not match")
    _validate_selection(document.get("selection"), catalog_spec)
    _validate_profile(document.get("runtimeProfile"))
    _validate_artifacts(document.get("artifacts"), catalog_spec)
    _validate_runtime(document.get("runtime"))
    _validate_controller(document.get("controller"))
    _validate_host(document.get("host"))
    _validate_acceptance(document.get("acceptance"))
    _canonical_uuid(document.get("sourceTransactionId"), "source transaction id")
    _utc_timestamp(document.get("capturedAt"), "rollback capturedAt")
    expected_digest = canonical_sha256(
        {key: item for key, item in document.items() if key != "selfSha256"}
    )
    if document.get("selfSha256") != expected_digest:
        raise RollbackSpecError("rollback spec self digest does not match")
    return document


@dataclass(frozen=True, slots=True)
class RollbackSpec:
    """A validated immutable source runtime identity."""

    _canonical_document: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._canonical_document, bytes):
            raise RollbackSpecError("rollback spec canonical document must be bytes")
        try:
            document = json.loads(self._canonical_document)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RollbackSpecError("rollback spec canonical document is invalid") from error
        _validate_spec_document(document)
        if canonical_bytes(document) != self._canonical_document:
            raise RollbackSpecError("rollback spec document is not canonically encoded")

    @classmethod
    def from_document(cls, value: Any) -> "RollbackSpec":
        document = _json_clone(value, "rollback spec")
        _validate_spec_document(document)
        return cls(canonical_bytes(document))

    @classmethod
    def from_verified_components(
        cls,
        *,
        catalog_spec: CatalogDeploymentSpec,
        selection_values: Mapping[str, str],
        runtime_profile_name: str,
        runtime_profile_environment: Mapping[str, str],
        artifacts: Sequence[Mapping[str, Any]],
        runtime: Mapping[str, Any],
        controller_git_commit: str,
        controller_materials: Mapping[str, str]
        | Sequence[Mapping[str, str]],
        host: Mapping[str, Any],
        acceptance: Mapping[str, Any],
        source_transaction_id: str,
        captured_at: str,
    ) -> "RollbackSpec":
        """Build a spec only from caller-supplied, already verified components."""

        if not isinstance(catalog_spec, CatalogDeploymentSpec):
            raise RollbackSpecError(
                "catalog_spec must be a validated CatalogDeploymentSpec"
            )
        selection = dict(selection_values)
        profile_environment = dict(runtime_profile_environment)
        if isinstance(controller_materials, Mapping):
            material_entries = [
                {"path": path, "sha256": digest}
                for path, digest in sorted(controller_materials.items())
            ]
        else:
            try:
                material_entries = sorted(
                    (dict(item) for item in controller_materials),
                    key=lambda item: str(item.get("path", "")),
                )
            except (TypeError, ValueError) as error:
                raise RollbackSpecError(
                    "controller materials must be verified path/SHA256 objects"
                ) from error
        document: dict[str, Any] = {
            "schemaVersion": ROLLBACK_SPEC_SCHEMA_VERSION,
            "kind": ROLLBACK_SPEC_KIND,
            "scopePolicy": ROLLBACK_SCOPE_POLICY,
            "catalogSpecSha256": catalog_spec.sha256,
            "catalogSpec": catalog_spec.document(),
            "selection": {
                "format": "allowlisted-env-v1",
                "values": selection,
                "sha256": canonical_sha256(selection),
                "containsCredentials": False,
            },
            "runtimeProfile": {
                "name": runtime_profile_name,
                "environment": profile_environment,
                "sha256": canonical_sha256(
                    {
                        "name": runtime_profile_name,
                        "environment": profile_environment,
                    }
                ),
            },
            "artifacts": [dict(item) for item in artifacts],
            "runtime": dict(runtime),
            "controller": {
                "gitCommit": controller_git_commit,
                "trackedDirty": False,
                "materialPolicy": CONTROLLER_MATERIAL_POLICY,
                "materials": material_entries,
                "materialsSha256": canonical_sha256(material_entries),
            },
            "host": dict(host),
            "acceptance": dict(acceptance),
            "sourceTransactionId": source_transaction_id,
            "capturedAt": captured_at,
        }
        document["selfSha256"] = canonical_sha256(document)
        return cls.from_document(document)

    def document(self) -> dict[str, Any]:
        return json.loads(self._canonical_document)

    @property
    def sha256(self) -> str:
        return self.document()["selfSha256"]

    @property
    def catalog_spec_sha256(self) -> str:
        return self.document()["catalogSpecSha256"]


def _validate_pointer_document(value: Any) -> dict[str, Any]:
    pointer = _exact_keys(value, _POINTER_KEYS, "rollback pointer")
    generation = pointer.get("generation")
    active = pointer.get("activeSpecSha256")
    previous = pointer.get("previousSpecSha256")
    if (
        pointer.get("schemaVersion") != ROLLBACK_POINTER_SCHEMA_VERSION
        or pointer.get("kind") != ROLLBACK_POINTER_KIND
        or pointer.get("scopePolicy") != ROLLBACK_SCOPE_POLICY
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
        or (active is not None and _SHA256.fullmatch(str(active)) is None)
        or (previous is not None and _SHA256.fullmatch(str(previous)) is None)
        or (active is None and previous is None)
        or (generation == 1 and previous is not None)
        or (active is not None and active == previous)
    ):
        raise RollbackSpecError("rollback pointer state is invalid")
    _utc_timestamp(pointer.get("updatedAt"), "rollback pointer updatedAt")
    _canonical_uuid(
        pointer.get("updatedByTransactionId"),
        "rollback pointer transaction id",
    )
    return pointer


@dataclass(frozen=True, slots=True)
class RollbackPointer:
    """Monotonic active-anchor pointer; an inactive value is a durable tombstone."""

    _canonical_document: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._canonical_document, bytes):
            raise RollbackSpecError("rollback pointer canonical document must be bytes")
        try:
            document = json.loads(self._canonical_document)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RollbackSpecError(
                "rollback pointer canonical document is invalid"
            ) from error
        _validate_pointer_document(document)
        if canonical_bytes(document) != self._canonical_document:
            raise RollbackSpecError("rollback pointer is not canonically encoded")

    @classmethod
    def from_document(cls, value: Any) -> "RollbackPointer":
        document = _json_clone(value, "rollback pointer")
        _validate_pointer_document(document)
        return cls(canonical_bytes(document))

    def document(self) -> dict[str, Any]:
        return json.loads(self._canonical_document)

    @property
    def generation(self) -> int:
        return self.document()["generation"]

    @property
    def active_spec_sha256(self) -> str | None:
        return self.document()["activeSpecSha256"]

    @property
    def previous_spec_sha256(self) -> str | None:
        return self.document()["previousSpecSha256"]


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RollbackStoreError("private rollback storage requires O_NOFOLLOW")
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | nofollow


def _file_flags(mode: int) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RollbackStoreError("private rollback storage requires O_NOFOLLOW")
    return mode | getattr(os, "O_CLOEXEC", 0) | nofollow


def _validate_directory(
    descriptor: int,
    *,
    path: Path | None = None,
    parent_descriptor: int | None = None,
    name: str | None = None,
    label: str,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    try:
        named = (
            path.lstat()
            if path is not None
            else os.stat(
                str(name),
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        )
    except OSError as error:
        raise RollbackStoreError(f"cannot inspect {label}: {error}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        raise RollbackStoreError(
            f"{label} must be a private current-user 0700 directory"
        )
    return metadata


def _open_absolute_directory(path: Path) -> int:
    """Open every component of an absolute directory without following links."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, _directory_flags())
        for component in absolute.parts[1:]:
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise RollbackStoreError(
            f"cannot open project root without following links: {error}"
        ) from error


def _open_managed_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
    private: bool,
    label: str,
) -> int:
    created = False
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise RollbackStoreError(f"cannot create {label}: {error}") from error
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise RollbackStoreError(f"cannot open {label}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or (mode != 0o700 if private else bool(mode & 0o022 or mode & 0o7000))
            or (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            requirement = "private current-user 0700" if private else (
                "current-user-owned and not group/other-writable"
            )
            raise RollbackStoreError(f"{label} must be {requirement}")
        if created:
            os.fsync(parent_descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_private_child_directory(
    parent_descriptor: int, name: str, *, create: bool
) -> int:
    return _open_managed_child_directory(
        parent_descriptor,
        name,
        create=create,
        private=True,
        label="rollback object directory",
    )


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
    )


def _read_private_json_at(
    directory_descriptor: int,
    name: str,
    *,
    maximum_bytes: int,
    label: str,
) -> dict[str, Any]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            _file_flags(os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)),
            dir_fd=directory_descriptor,
        )
        before = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > maximum_bytes
            or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise RollbackStoreError(
                f"{label} must be a bounded private current-user 0600 regular file"
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        after = os.fstat(descriptor)
        named_after = os.stat(
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (
            len(body) > maximum_bytes
            or _stat_identity(after) != _stat_identity(before)
            or (named_after.st_dev, named_after.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise RollbackStoreError(f"{label} changed while it was read")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RollbackStoreError(f"{label} is not valid JSON: {error}") from error
        if not isinstance(document, dict):
            raise RollbackStoreError(f"{label} must contain a JSON object")
        return document
    except FileNotFoundError:
        raise
    except RollbackStoreError:
        raise
    except OSError as error:
        raise RollbackStoreError(f"cannot read {label}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_all(descriptor: int, body: bytes) -> None:
    view = memoryview(body)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise RollbackStoreError("private rollback write made no progress")
        view = view[written:]


def _temporary_name(name: str) -> str:
    return f".{name}.{uuid.uuid4().hex}.tmp"


def _publish_noreplace_at(
    directory_descriptor: int,
    name: str,
    body: bytes,
) -> bool:
    temporary = _temporary_name(name)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, body)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            created = True
        except FileExistsError:
            created = False
        os.unlink(temporary, dir_fd=directory_descriptor)
        temporary = ""
        os.fsync(directory_descriptor)
        return created
    except RollbackStoreError:
        raise
    except OSError as error:
        raise RollbackStoreError(
            f"cannot publish immutable rollback object: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


def _replace_at(
    directory_descriptor: int,
    name: str,
    body: bytes,
) -> None:
    temporary = _temporary_name(name)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
            dir_fd=directory_descriptor,
        )
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, body)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporary = ""
        os.fsync(directory_descriptor)
    except RollbackStoreError:
        raise
    except OSError as error:
        raise RollbackStoreError(
            f"cannot replace the rollback pointer safely: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


class RollbackStore:
    """Private immutable specs plus one monotonic active rollback pointer."""

    OBJECT_DIRECTORY_NAME = "rollback-specs"
    POINTER_NAME = "rollback.json"
    LOCK_NAME = "rollback.lock"

    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        self.state_dir = paths.state_dir
        self.object_dir = self.state_dir / self.OBJECT_DIRECTORY_NAME
        self.pointer_path = self.state_dir / self.POINTER_NAME
        self.lock_path = self.state_dir / self.LOCK_NAME

    def spec_path(self, spec_sha256: str) -> Path:
        _sha256(spec_sha256, "rollback spec SHA256")
        return self.object_dir / f"{spec_sha256}.json"

    @contextlib.contextmanager
    def _state(self, *, create: bool) -> Iterator[int]:
        project_descriptor = _open_absolute_directory(self.paths.root)
        cache_descriptor: int | None = None
        descriptor: int | None = None
        try:
            project_metadata = os.fstat(project_descriptor)
            if (
                project_metadata.st_uid != os.getuid()
                or stat.S_IMODE(project_metadata.st_mode) & 0o022
                or stat.S_IMODE(project_metadata.st_mode) & 0o7000
            ):
                raise RollbackStoreError(
                    "project root must be current-user-owned and not group/other-writable"
                )
            cache_descriptor = _open_managed_child_directory(
                project_descriptor,
                "cache",
                create=create,
                private=False,
                label="cache directory",
            )
            descriptor = _open_managed_child_directory(
                cache_descriptor,
                "control-plane",
                create=create,
                private=True,
                label="rollback state directory",
            )
            yield descriptor
            _validate_directory(
                descriptor,
                parent_descriptor=cache_descriptor,
                name="control-plane",
                label="rollback state directory",
            )
            cache_metadata = os.fstat(cache_descriptor)
            named_cache = os.stat(
                "cache",
                dir_fd=project_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(cache_metadata.st_mode)
                or cache_metadata.st_uid != os.getuid()
                or stat.S_IMODE(cache_metadata.st_mode) & 0o022
                or stat.S_IMODE(cache_metadata.st_mode) & 0o7000
                or (named_cache.st_dev, named_cache.st_ino)
                != (cache_metadata.st_dev, cache_metadata.st_ino)
            ):
                raise RollbackStoreError("cache directory changed during rollback I/O")
            named_project = self.paths.root.lstat()
            if (named_project.st_dev, named_project.st_ino) != (
                project_metadata.st_dev,
                project_metadata.st_ino,
            ):
                raise RollbackStoreError("project root changed during rollback I/O")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if cache_descriptor is not None:
                os.close(cache_descriptor)
            os.close(project_descriptor)

    @contextlib.contextmanager
    def _objects(
        self, state_descriptor: int, *, create: bool
    ) -> Iterator[int]:
        descriptor = _open_private_child_directory(
            state_descriptor,
            self.OBJECT_DIRECTORY_NAME,
            create=create,
        )
        try:
            yield descriptor
            _validate_directory(
                descriptor,
                parent_descriptor=state_descriptor,
                name=self.OBJECT_DIRECTORY_NAME,
                label="rollback object directory",
            )
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def _locked_state(self) -> Iterator[int]:
        with self._state(create=True) as state_descriptor:
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    self.LOCK_NAME,
                    _file_flags(os.O_RDWR | os.O_CREAT),
                    0o600,
                    dir_fd=state_descriptor,
                )
                metadata = os.fstat(descriptor)
                named = os.stat(
                    self.LOCK_NAME,
                    dir_fd=state_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or (named.st_dev, named.st_ino)
                    != (metadata.st_dev, metadata.st_ino)
                ):
                    raise RollbackStoreError(
                        "rollback lock must be a private current-user 0600 regular file"
                    )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield state_descriptor
            except RollbackStoreError:
                raise
            except OSError as error:
                raise RollbackStoreError(
                    f"cannot lock rollback state: {error}"
                ) from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    @staticmethod
    def _body(document: dict[str, Any]) -> bytes:
        return canonical_bytes(document) + b"\n"

    def put(self, spec: RollbackSpec) -> Path:
        if not isinstance(spec, RollbackSpec):
            raise RollbackSpecError("rollback store only accepts RollbackSpec values")
        document = spec.document()
        body = self._body(document)
        if len(body) > MAX_ROLLBACK_SPEC_BYTES:
            raise RollbackStoreError("rollback spec exceeds the bounded size policy")
        name = f"{spec.sha256}.json"
        with self._locked_state() as state_descriptor:
            with self._objects(state_descriptor, create=True) as object_descriptor:
                try:
                    cleanup_interrupted_noreplace_link_at(object_descriptor, name)
                except OSError as error:
                    raise RollbackStoreError(
                        "cannot repair an interrupted rollback object publish"
                    ) from error
                _publish_noreplace_at(object_descriptor, name, body)
                stored_document = _read_private_json_at(
                    object_descriptor,
                    name,
                    maximum_bytes=MAX_ROLLBACK_SPEC_BYTES,
                    label="rollback spec object",
                )
                try:
                    stored = RollbackSpec.from_document(stored_document)
                except RollbackSpecError as error:
                    raise RollbackStoreError(
                        f"stored rollback spec is invalid: {error}"
                    ) from error
                if stored.sha256 != spec.sha256 or stored.document() != document:
                    raise RollbackStoreError(
                        "rollback spec object conflicts with its content address"
                    )
        return self.spec_path(spec.sha256)

    def _read_spec_from_state(
        self, state_descriptor: int, spec_sha256: str
    ) -> RollbackSpec:
        _sha256(spec_sha256, "rollback spec SHA256")
        name = f"{spec_sha256}.json"
        try:
            with self._objects(state_descriptor, create=False) as object_descriptor:
                document = _read_private_json_at(
                    object_descriptor,
                    name,
                    maximum_bytes=MAX_ROLLBACK_SPEC_BYTES,
                    label="rollback spec object",
                )
        except FileNotFoundError as error:
            raise RollbackStoreError(
                f"rollback spec object is missing: {spec_sha256}"
            ) from error
        try:
            spec = RollbackSpec.from_document(document)
        except RollbackSpecError as error:
            raise RollbackStoreError(f"rollback spec object is invalid: {error}") from error
        if spec.sha256 != spec_sha256:
            raise RollbackStoreError(
                "rollback spec object does not match its content address"
            )
        return spec

    def read_spec(self, spec_sha256: str) -> RollbackSpec:
        try:
            with self._state(create=False) as state_descriptor:
                return self._read_spec_from_state(state_descriptor, spec_sha256)
        except FileNotFoundError as error:
            raise RollbackStoreError("rollback state does not exist") from error

    @staticmethod
    def _pointer_from_state(state_descriptor: int) -> RollbackPointer | None:
        try:
            document = _read_private_json_at(
                state_descriptor,
                RollbackStore.POINTER_NAME,
                maximum_bytes=MAX_ROLLBACK_POINTER_BYTES,
                label="rollback pointer",
            )
        except FileNotFoundError:
            return None
        try:
            return RollbackPointer.from_document(document)
        except RollbackSpecError as error:
            raise RollbackStoreError(f"rollback pointer is invalid: {error}") from error

    def read_pointer(self) -> RollbackPointer | None:
        try:
            with self._state(create=False) as state_descriptor:
                return self._pointer_from_state(state_descriptor)
        except FileNotFoundError:
            return None

    def read_active(self) -> RollbackSpec | None:
        pointer = self.read_pointer()
        if pointer is None or pointer.active_spec_sha256 is None:
            return None
        return self.read_spec(pointer.active_spec_sha256)

    @staticmethod
    def _assert_cas(
        current: RollbackPointer | None,
        *,
        expected_generation: int,
        expected_previous_sha256: str | None,
    ) -> None:
        if (
            not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or expected_generation < 0
        ):
            raise RollbackCASMismatch("expected pointer generation is invalid")
        if expected_previous_sha256 is not None:
            try:
                _sha256(expected_previous_sha256, "expected rollback spec SHA256")
            except RollbackSpecError as error:
                raise RollbackCASMismatch(str(error)) from error
        actual_generation = current.generation if current is not None else 0
        actual_active = (
            current.active_spec_sha256 if current is not None else None
        )
        if (
            actual_generation != expected_generation
            or actual_active != expected_previous_sha256
        ):
            raise RollbackCASMismatch(
                "rollback pointer compare-and-swap precondition failed"
            )

    def publish(
        self,
        spec: RollbackSpec,
        *,
        expected_generation: int,
        expected_previous_sha256: str | None,
        transaction_id: str,
        updated_at: str,
    ) -> RollbackPointer:
        """Publish ``spec`` as active after an exact generation/previous CAS."""

        self.put(spec)
        _canonical_uuid(transaction_id, "rollback pointer transaction id")
        _utc_timestamp(updated_at, "rollback pointer updatedAt")
        with self._locked_state() as state_descriptor:
            current = self._pointer_from_state(state_descriptor)
            self._assert_cas(
                current,
                expected_generation=expected_generation,
                expected_previous_sha256=expected_previous_sha256,
            )
            if expected_previous_sha256 == spec.sha256:
                raise RollbackCASMismatch(
                    "rollback pointer already identifies the requested spec"
                )
            self._read_spec_from_state(state_descriptor, spec.sha256)
            if expected_previous_sha256 is not None:
                self._read_spec_from_state(
                    state_descriptor, expected_previous_sha256
                )
            pointer = RollbackPointer.from_document(
                {
                    "schemaVersion": ROLLBACK_POINTER_SCHEMA_VERSION,
                    "kind": ROLLBACK_POINTER_KIND,
                    "scopePolicy": ROLLBACK_SCOPE_POLICY,
                    "generation": expected_generation + 1,
                    "activeSpecSha256": spec.sha256,
                    "previousSpecSha256": expected_previous_sha256,
                    "updatedAt": updated_at,
                    "updatedByTransactionId": transaction_id,
                }
            )
            body = self._body(pointer.document())
            if len(body) > MAX_ROLLBACK_POINTER_BYTES:
                raise RollbackStoreError(
                    "rollback pointer exceeds the bounded size policy"
                )
            _replace_at(state_descriptor, self.POINTER_NAME, body)
            stored = self._pointer_from_state(state_descriptor)
            if stored is None or stored.document() != pointer.document():
                raise RollbackStoreError("rollback pointer failed post-write verification")
            return stored

    def clear(
        self,
        *,
        expected_generation: int,
        expected_previous_sha256: str,
        transaction_id: str,
        updated_at: str,
    ) -> RollbackPointer:
        """Clear the active anchor while retaining a monotonic CAS tombstone."""

        _canonical_uuid(transaction_id, "rollback pointer transaction id")
        _utc_timestamp(updated_at, "rollback pointer updatedAt")
        with self._locked_state() as state_descriptor:
            current = self._pointer_from_state(state_descriptor)
            self._assert_cas(
                current,
                expected_generation=expected_generation,
                expected_previous_sha256=expected_previous_sha256,
            )
            self._read_spec_from_state(
                state_descriptor, expected_previous_sha256
            )
            pointer = RollbackPointer.from_document(
                {
                    "schemaVersion": ROLLBACK_POINTER_SCHEMA_VERSION,
                    "kind": ROLLBACK_POINTER_KIND,
                    "scopePolicy": ROLLBACK_SCOPE_POLICY,
                    "generation": expected_generation + 1,
                    "activeSpecSha256": None,
                    "previousSpecSha256": expected_previous_sha256,
                    "updatedAt": updated_at,
                    "updatedByTransactionId": transaction_id,
                }
            )
            _replace_at(
                state_descriptor,
                self.POINTER_NAME,
                self._body(pointer.document()),
            )
            stored = self._pointer_from_state(state_descriptor)
            if stored is None or stored.document() != pointer.document():
                raise RollbackStoreError("rollback pointer failed post-clear verification")
            return stored

    def restore(
        self,
        previous_pointer: RollbackPointer | None,
        *,
        expected_current_generation: int,
        expected_current_sha256: str | None,
        transaction_id: str,
        updated_at: str,
    ) -> RollbackPointer:
        """Restore the logical pointer state preceding a publish or clear.

        Restoration is itself a new monotonic generation.  A previous active
        anchor is republished; an absent or tombstoned previous pointer clears
        the current anchor.  The current generation, active digest, and link to
        the supplied predecessor must all match while the store lock is held.
        """

        if previous_pointer is not None and not isinstance(
            previous_pointer, RollbackPointer
        ):
            raise RollbackCASMismatch(
                "previous rollback pointer must be a validated pointer or null"
            )
        try:
            if expected_current_sha256 is not None:
                _sha256(expected_current_sha256, "current rollback spec SHA256")
            _canonical_uuid(transaction_id, "rollback pointer transaction id")
            _utc_timestamp(updated_at, "rollback pointer updatedAt")
        except RollbackSpecError as error:
            raise RollbackCASMismatch(str(error)) from error

        with self._locked_state() as state_descriptor:
            current = self._pointer_from_state(state_descriptor)
            self._assert_cas(
                current,
                expected_generation=expected_current_generation,
                expected_previous_sha256=expected_current_sha256,
            )
            if current is None:  # Defensive: successful CAS above requires one.
                raise RollbackCASMismatch("rollback pointer disappeared")

            previous_active = (
                previous_pointer.active_spec_sha256
                if previous_pointer is not None
                else None
            )
            expected_predecessor_generation = (
                previous_pointer.generation if previous_pointer is not None else 0
            )
            if (
                expected_predecessor_generation + 1
                != expected_current_generation
                or current.previous_spec_sha256 != previous_active
            ):
                raise RollbackCASMismatch(
                    "supplied rollback pointer is not the current pointer's exact predecessor"
                )

            if expected_current_sha256 is not None:
                self._read_spec_from_state(
                    state_descriptor, expected_current_sha256
                )
            if previous_active is not None:
                self._read_spec_from_state(state_descriptor, previous_active)

            pointer = RollbackPointer.from_document(
                {
                    "schemaVersion": ROLLBACK_POINTER_SCHEMA_VERSION,
                    "kind": ROLLBACK_POINTER_KIND,
                    "scopePolicy": ROLLBACK_SCOPE_POLICY,
                    "generation": expected_current_generation + 1,
                    "activeSpecSha256": previous_active,
                    "previousSpecSha256": expected_current_sha256,
                    "updatedAt": updated_at,
                    "updatedByTransactionId": transaction_id,
                }
            )
            body = self._body(pointer.document())
            if len(body) > MAX_ROLLBACK_POINTER_BYTES:
                raise RollbackStoreError(
                    "rollback pointer exceeds the bounded size policy"
                )
            _replace_at(state_descriptor, self.POINTER_NAME, body)
            stored = self._pointer_from_state(state_descriptor)
            if stored is None or stored.document() != pointer.document():
                raise RollbackStoreError(
                    "rollback pointer failed post-restore verification"
                )
            return stored
