"""Strict Catalog boundary and deterministic model selection.

The Catalog is untrusted repository input until :func:`validate_catalog`
returns.  This module deliberately keeps dictionaries at the read boundary;
the deployment layer can freeze one admitted record into its own immutable
runtime specification without coupling that type to the on-disk schema.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any, TypeAlias
from urllib.parse import urlparse

from .materials import MaterialError, read_file_bytes


CatalogDocument: TypeAlias = dict[str, Any]
ModelRecord: TypeAlias = dict[str, Any]
HostFacts: TypeAlias = dict[str, Any]

SCHEMA_VERSION = 2
AUTOMATIC_DEPLOYMENT_REQUIREMENT = (
    "validated profile backed by a reviewed signed full reusable attestation"
)
READ_ONLY_STATUSES = ("estimated", "provisional")
SUPPORTED_STATUSES = {*READ_ONLY_STATUSES, "validated"}
SUPPORTED_KV_CACHE_TYPE = "q8_0"
LIFECYCLE_ROLES = ("lts", "rollback", "candidate")


class CatalogError(ValueError):
    """The Catalog cannot be safely interpreted."""


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError(f"{context} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], context: str) -> None:
    if set(value) != expected:
        raise CatalogError(f"{context} has unsupported or missing fields")


def _non_empty_text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"invalid {context}")
    return value


def _iso_date(value: Any, context: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise CatalogError(f"{context} must be an ISO calendar date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogError(f"{context} must be an ISO calendar date") from exc
    return value


def _positive_number(value: Any, context: str) -> float | int:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value <= 0
        or value > 1_000_000
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        raise CatalogError(f"invalid {context}")
    return value


def _positive_integer(value: Any, context: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > 2**63 - 1
    ):
        raise CatalogError(f"invalid {context}")
    return value


def resolve_catalog_path(
    root: Path,
    value: Any,
    *,
    suffixes: tuple[str, ...],
) -> Path | None:
    """Return a repository-relative Catalog path, or ``None`` if unsafe."""

    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) < 2
        or relative.suffix not in suffixes
    ):
        return None
    return root / relative


def _validate_model(
    model_value: Any,
    *,
    ids: set[str],
    read_only_statuses: tuple[str, ...],
) -> None:
    model = _mapping(model_value, "Catalog model")
    status = model.get("status")
    expected_model_keys = {
        "id",
        "displayName",
        "quantization",
        "status",
        "lifecycleRole",
        "deploymentEligibility",
        "purpose",
        "modelRepository",
        "modelRevision",
        "artifactRepository",
        "artifactRevision",
        "license",
        "modelDirectory",
        "servedModelId",
        "performancePolicy",
        "requirements",
        "runtime",
        "artifacts",
    }
    if status in {"provisional", "validated"}:
        expected_model_keys.update({"validation", "validatedHardware"})
    if status == "validated":
        expected_model_keys.add("validationAttestation")
    _exact_keys(model, expected_model_keys, "Catalog model")
    model_id = model.get("id", "")
    if (
        not isinstance(model_id, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]+", model_id)
        or model_id in ids
    ):
        raise CatalogError(f"invalid or duplicate model id in catalog: {model_id!r}")
    ids.add(model_id)

    for field in ("displayName", "quantization", "purpose", "servedModelId"):
        _non_empty_text(model.get(field), f"{field} for {model_id}")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]+", model.get("servedModelId", "")
    ):
        raise CatalogError(f"unsafe servedModelId for {model_id}")

    performance_policy = _mapping(
        model.get("performancePolicy"), f"performance policy for {model_id}"
    )
    _exact_keys(
        performance_policy,
        {"schemaVersion", "manifestPath"},
        "Catalog performance policy",
    )
    manifest_path = performance_policy.get("manifestPath")
    resolved_manifest = resolve_catalog_path(
        Path("."), manifest_path, suffixes=(".json",)
    )
    if (
        performance_policy.get("schemaVersion") != 1
        or resolved_manifest is None
        or not isinstance(manifest_path, str)
        or Path(manifest_path).as_posix() != manifest_path
        or Path(manifest_path).parts[0] != "deployments"
        or Path(manifest_path).name != "manifest.json"
    ):
        raise CatalogError(f"invalid performance policy reference for {model_id}")

    directory = Path(model.get("modelDirectory", ""))
    if (
        directory.is_absolute()
        or ".." in directory.parts
        or len(directory.parts) != 1
        or str(directory) in {"", "."}
    ):
        raise CatalogError(f"unsafe model directory for {model_id}: {directory}")

    if status not in SUPPORTED_STATUSES:
        raise CatalogError(f"invalid evidence status for {model_id}: {status!r}")
    lifecycle_role = model.get("lifecycleRole")
    if lifecycle_role not in LIFECYCLE_ROLES:
        raise CatalogError(f"invalid lifecycle role for {model_id}: {lifecycle_role!r}")
    eligibility = _mapping(
        model.get("deploymentEligibility"),
        f"deployment eligibility for {model_id}",
    )
    _exact_keys(eligibility, {"automatic", "reason"}, "deployment eligibility")
    automatic = eligibility.get("automatic")
    if (
        not isinstance(automatic, bool)
        or not isinstance(eligibility.get("reason"), str)
        or not eligibility["reason"].strip()
    ):
        raise CatalogError(f"invalid deployment eligibility for {model_id}")
    if status in read_only_statuses and automatic:
        raise CatalogError(
            f"read-only Catalog status cannot deploy automatically: {model_id}"
        )
    if automatic and status != "validated":
        raise CatalogError(f"automatic deployment requires validated status: {model_id}")
    if lifecycle_role != "lts" and automatic:
        raise CatalogError(
            f"only the LTS lifecycle role may be automatically deployed: {model_id}"
        )

    if status in {"provisional", "validated"}:
        _non_empty_text(model.get("validation"), f"validation for {model_id}")
        hardware = _mapping(
            model.get("validatedHardware"),
            f"validated hardware for {model_id}",
        )
        _exact_keys(
            hardware,
            {
                "environmentKind",
                "architecture",
                "gpuName",
                "gpuCount",
                "minVramGiB",
                "minRamGiB",
            },
            "validated hardware",
        )
        _non_empty_text(hardware.get("gpuName"), f"validated GPU for {model_id}")
        if (
            hardware.get("environmentKind") != "wsl2"
            or hardware.get("architecture") != "x86_64"
        ):
            raise CatalogError(
                f"validated hardware must identify the Tier-1 WSL2 x86_64 environment for {model_id}"
            )
        if hardware.get("gpuCount") != 1 or isinstance(hardware.get("gpuCount"), bool):
            raise CatalogError(f"incomplete validated hardware metadata for {model_id}")
        _positive_number(
            hardware.get("minVramGiB"), f"validated VRAM for {model_id}"
        )
        _positive_number(hardware.get("minRamGiB"), f"validated RAM for {model_id}")

    if status == "validated":
        attestation = _mapping(
            model.get("validationAttestation"),
            f"validation attestation for {model_id}",
        )
        _exact_keys(
            attestation,
            {
                "mode",
                "tool",
                "payloadSha256",
                "trustedKeySha256",
                "documentPath",
                "signaturePath",
            },
            "validation attestation",
        )
        if (
            attestation.get("mode") != "full"
            or not re.fullmatch(
                r"[0-9a-f]{64}", attestation.get("payloadSha256", "")
            )
            or attestation.get("tool") not in {"minisign", "cosign"}
            or not re.fullmatch(
                r"[0-9a-f]{64}", attestation.get("trustedKeySha256", "")
            )
            or resolve_catalog_path(
                Path("."), attestation.get("documentPath"), suffixes=(".json",)
            )
            is None
            or resolve_catalog_path(
                Path("."),
                attestation.get("signaturePath"),
                suffixes=(".sig", ".minisig", ".signature"),
            )
            is None
        ):
            raise CatalogError(
                "validated deployment requires a verifiable full attestation "
                f"and explicit trusted-key fingerprint: {model_id}"
            )
    elif "validationAttestation" in model:
        raise CatalogError(
            f"only a validated Catalog entry may carry an attestation: {model_id}"
        )

    for field in ("modelRevision", "artifactRevision"):
        if not re.fullmatch(r"[0-9a-f]{40}", model.get(field, "")):
            raise CatalogError(f"invalid {field} for {model_id}")
    for field in ("modelRepository", "artifactRepository"):
        if not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model.get(field, "")
        ):
            raise CatalogError(f"invalid {field} for {model_id}")

    license_metadata = _mapping(model.get("license"), f"license for {model_id}")
    _exact_keys(
        license_metadata,
        {"spdx", "source", "metadataVerifiedAt", "reviewRequired"},
        "license metadata",
    )
    expected_license_source = (
        f"https://huggingface.co/{model['modelRepository']}/blob/"
        f"{model['modelRevision']}/LICENSE"
    )
    if (
        not re.fullmatch(r"[A-Za-z0-9.+-]+", license_metadata.get("spdx", ""))
        or license_metadata.get("source") != expected_license_source
        or license_metadata.get("reviewRequired") is not True
    ):
        raise CatalogError(f"incomplete license metadata for {model_id}")
    try:
        _iso_date(
            license_metadata.get("metadataVerifiedAt"),
            f"license metadata date for {model_id}",
        )
    except CatalogError as exc:
        raise CatalogError(f"incomplete license metadata for {model_id}") from exc

    requirements = _mapping(
        model.get("requirements"), f"hardware requirements for {model_id}"
    )
    _exact_keys(
        requirements,
        {"minVramGiB", "recommendedVramGiB", "minRamGiB", "minFreeDiskGiB"},
        "hardware requirements",
    )
    for field in (
        "minVramGiB",
        "recommendedVramGiB",
        "minRamGiB",
        "minFreeDiskGiB",
    ):
        _positive_number(requirements.get(field), f"hardware requirements for {model_id}")
    if requirements["recommendedVramGiB"] < requirements["minVramGiB"]:
        raise CatalogError(f"recommended VRAM is below minimum for {model_id}")

    runtime = _mapping(model.get("runtime"), f"runtime capacity for {model_id}")
    _exact_keys(
        runtime,
        {
            "contextTokens",
            "recommendedInputTokens",
            "maxOutputTokens",
            "cacheRamMiB",
            "cacheTypeK",
            "cacheTypeV",
            "batchSize",
            "ubatchSize",
        },
        "runtime capacity",
    )
    for field in (
        "contextTokens",
        "recommendedInputTokens",
        "maxOutputTokens",
        "cacheRamMiB",
        "batchSize",
        "ubatchSize",
    ):
        _positive_integer(runtime.get(field), f"runtime capacity for {model_id}")
    if (
        runtime.get("cacheTypeK") != SUPPORTED_KV_CACHE_TYPE
        or runtime.get("cacheTypeV") != SUPPORTED_KV_CACHE_TYPE
    ):
        raise CatalogError(f"unsupported runtime KV cache type for {model_id}")
    if (
        runtime["recommendedInputTokens"] + runtime["maxOutputTokens"]
        >= runtime["contextTokens"]
    ):
        raise CatalogError(f"runtime token budget has no safety margin for {model_id}")

    artifacts_value = model.get("artifacts")
    if not isinstance(artifacts_value, list) or not artifacts_value:
        raise CatalogError(f"{model_id} must define artifacts")
    artifacts = [_mapping(item, f"artifact for {model_id}") for item in artifacts_value]
    primaries = [item for item in artifacts if item.get("role") == "model"]
    if len(primaries) != 1 or primaries[0].get("required") is not True:
        raise CatalogError(
            f"{model_id} must define exactly one required primary model artifact"
        )

    filenames: set[str] = set()
    for artifact in artifacts:
        _exact_keys(
            artifact,
            {"role", "required", "filename", "bytes", "sha256", "url"},
            "Catalog artifact",
        )
        filename = Path(artifact.get("filename", ""))
        url = urlparse(artifact.get("url", ""))
        if (
            filename.is_absolute()
            or len(filename.parts) != 1
            or filename.name != str(filename)
            or str(filename) in {"", "."}
        ):
            raise CatalogError(f"unsafe artifact filename for {model_id}: {filename}")
        if url.scheme != "https" or url.netloc != "huggingface.co":
            raise CatalogError(f"unapproved artifact URL for {model_id}: {url.geturl()}")
        if url.query != "download=true" or url.fragment:
            raise CatalogError(
                f"artifact URL must use only the reviewed download query for {model_id}"
            )
        if str(filename) in filenames:
            raise CatalogError(f"duplicate artifact filename for {model_id}: {filename}")
        filenames.add(str(filename))
        if not isinstance(artifact.get("required"), bool):
            raise CatalogError(f"artifact required flag must be boolean for {model_id}")
        _non_empty_text(artifact.get("role"), f"artifact role for {model_id}")
        expected_path = (
            f"/{model['artifactRepository']}/resolve/"
            f"{model['artifactRevision']}/{filename}"
        )
        if url.path != expected_path:
            raise CatalogError(
                "artifact URL is not pinned to the reviewed revision for "
                f"{model_id}/{filename}"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", artifact.get("sha256", "")):
            raise CatalogError(f"invalid SHA256 for {model_id}/{filename}")
        _positive_integer(artifact.get("bytes"), f"artifact size for {model_id}/{filename}")

    required_bytes = sum(item["bytes"] for item in artifacts if item["required"])
    if requirements["minFreeDiskGiB"] * 1024**3 < required_bytes:
        raise CatalogError(f"minimum free disk is below required artifacts for {model_id}")


def validate_catalog(document: Any) -> CatalogDocument:
    """Validate a parsed Catalog document and return the same dictionary."""

    catalog = _mapping(document, "Catalog")
    _exact_keys(
        catalog,
        {
            "schemaVersion",
            "updatedAt",
            "scope",
            "artifactPolicy",
            "deploymentPolicy",
            "defaultModel",
            "models",
        },
        "Catalog",
    )
    if catalog.get("schemaVersion") != SCHEMA_VERSION:
        raise CatalogError(f"unsupported Catalog schema: {catalog.get('schemaVersion')!r}")
    _iso_date(catalog.get("updatedAt"), "catalog updatedAt")
    _non_empty_text(catalog.get("scope"), "catalog scope")

    artifact_policy = _mapping(catalog.get("artifactPolicy"), "artifact policy")
    _exact_keys(
        artifact_policy,
        {
            "modelAuthor",
            "artifactPublisher",
            "trust",
            "licenseReviewRequired",
            "licenseMetadata",
        },
        "artifact policy",
    )
    for field in ("modelAuthor", "artifactPublisher", "trust", "licenseMetadata"):
        _non_empty_text(artifact_policy.get(field), f"artifactPolicy.{field}")
    if artifact_policy.get("licenseReviewRequired") is not True:
        raise CatalogError("catalog must require an explicit third-party license review")

    deployment_policy = _mapping(
        catalog.get("deploymentPolicy"), "deployment policy"
    )
    _exact_keys(
        deployment_policy,
        {"automaticDeploymentRequires", "readOnlyStatuses"},
        "deployment policy",
    )
    if (
        deployment_policy.get("automaticDeploymentRequires")
        != AUTOMATIC_DEPLOYMENT_REQUIREMENT
        or deployment_policy.get("readOnlyStatuses") != list(READ_ONLY_STATUSES)
    ):
        raise CatalogError("catalog must fail closed until signed full validation exists")

    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        raise CatalogError("catalog must contain at least one reviewed model")
    ids: set[str] = set()
    for model in models:
        try:
            _validate_model(model, ids=ids, read_only_statuses=READ_ONLY_STATUSES)
        except CatalogError:
            raise
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
            raise CatalogError("malformed Catalog model record") from exc
    roles = [model["lifecycleRole"] for model in models]
    if roles.count("lts") != 1 or roles.count("rollback") > 1 or roles.count("candidate") > 1:
        raise CatalogError(
            "catalog requires exactly one LTS and at most one rollback and candidate"
        )
    default_model = catalog.get("defaultModel")
    if not isinstance(default_model, str) or default_model not in ids:
        raise CatalogError("catalog defaultModel does not reference a reviewed entry")
    if model_by_id(catalog, default_model)["lifecycleRole"] != "lts":
        raise CatalogError("catalog defaultModel must reference the LTS role")
    return catalog


def load_catalog(path: Path) -> CatalogDocument:
    """Read and validate a Catalog JSON document."""

    try:
        body = read_file_bytes(path, maximum_bytes=4 * 1024 * 1024)
        document = parse_catalog_json_bytes(body)
    except CatalogError:
        raise
    except (MaterialError, OSError, RecursionError, UnicodeError, ValueError) as exc:
        raise CatalogError(f"cannot read Catalog {path}: {exc}") from exc
    return validate_catalog(document)


def parse_catalog_json_bytes(body: bytes) -> Any:
    """Parse Catalog JSON without duplicate keys or non-standard numbers."""

    try:
        text = body.decode("utf-8")

        def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise CatalogError(f"Catalog JSON has a duplicate key: {key}")
                value[key] = item
            return value

        def reject_constant(value: str) -> None:
            raise CatalogError(f"Catalog JSON contains a non-standard number: {value}")

        return json.loads(
            text,
            object_pairs_hook=object_without_duplicates,
            parse_constant=reject_constant,
        )
    except CatalogError:
        raise
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise CatalogError(f"Catalog JSON is invalid: {exc}") from exc


def model_by_id(catalog: CatalogDocument, model_id: str) -> ModelRecord:
    """Return one validated Catalog record by exact identifier."""

    for model in catalog["models"]:
        if model["id"] == model_id:
            return model
    choices = ", ".join(model["id"] for model in catalog["models"])
    raise CatalogError(f"unknown model {model_id!r}; catalog choices: {choices}")


def fits(model: ModelRecord, host: HostFacts) -> bool:
    """Return whether immutable host capacity fits a model's minimums."""

    requirements = model["requirements"]
    return (
        host.get("platform", "linux") == "linux"
        and host.get("architecture", "x86_64") in {"x86_64", "amd64"}
        and host["largestGpuVramGiB"] >= requirements["minVramGiB"]
        and host["ramGiB"] >= requirements["minRamGiB"]
        and host["freeDiskGiB"] >= requirements["minFreeDiskGiB"]
    )


def resources_available_now(model: ModelRecord, host: HostFacts) -> bool:
    """Return whether current free capacity still satisfies deployment minimums."""

    return (
        fits(model, host)
        and host.get("largestFreeVramGiB", host["largestGpuVramGiB"])
        >= model["requirements"]["minVramGiB"]
        and host.get("availableRamGiB", host["ramGiB"])
        >= model["requirements"]["minRamGiB"]
    )


def automatic_deployment_supported(host: HostFacts) -> bool:
    """Automatic deployment is limited to exactly one detected GPU."""

    return len(host["gpus"]) == 1


def recommend(
    catalog: CatalogDocument, host: HostFacts
) -> ModelRecord | None:
    """Choose the largest fitting reviewed record, independent of free capacity."""

    candidates = [
        model
        for model in catalog["models"]
        if model["lifecycleRole"] == "lts" and fits(model, host)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda model: model["requirements"]["minVramGiB"])


def matches_validated_hardware_profile(
    model: ModelRecord | None, host: HostFacts
) -> bool:
    """Match only current validated evidence, never provisional history."""

    if (
        not model
        or model.get("status") != "validated"
        or "validatedHardware" not in model
    ):
        return False
    return matches_recorded_hardware_profile(model, host)


def matches_recorded_hardware_profile(
    model: ModelRecord | None, host: HostFacts
) -> bool:
    """Match the exact recorded host shape for an existing-selection recovery.

    This deliberately does not make a provisional Catalog entry deployable.
    It is only a static host-identity predicate used after an already selected
    private profile has been shown to equal the current Catalog projection.
    """

    if not model or "validatedHardware" not in model:
        return False
    hardware = model["validatedHardware"]
    return (
        host.get("environmentKind") == hardware["environmentKind"]
        and host.get("architecture") == hardware["architecture"]
        and
        len(host["gpus"]) == hardware["gpuCount"]
        and all(gpu["name"] == hardware["gpuName"] for gpu in host["gpus"])
        and host["largestGpuVramGiB"] >= hardware["minVramGiB"]
        and host["ramGiB"] >= hardware["minRamGiB"]
    )
