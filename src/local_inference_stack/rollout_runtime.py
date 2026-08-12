"""Host adapters for the same-controller rollback policy.

The immutable schema and store live in :mod:`local_inference_stack.rollout`.
This module is the narrow adapter that captures and re-verifies the local host
facts consumed by that schema.  It never downloads an artifact, pulls an image,
changes Git state, or starts/stops a container.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import stat
from pathlib import Path
from typing import Any

from . import configuration
from .catalog import CatalogError, load_catalog, model_by_id
from .deployment import CatalogDeploymentSpec, DeploymentSpecError
from .host import environment_kind
from .materials import canonical_sha256, read_file_bytes, sha256_file
from .paths import ProjectPaths
from .result import ConfigError, IntegrityError, RecoveryError
from .rollout import (
    CONTROLLER_MATERIAL_POLICY,
    RollbackSpec,
    RollbackSpecError,
)
from .runner import run
from .transactions import RECOVERY_DEPLOYMENT_KEYS, recovery_original_is_safe


HOST_FINGERPRINT_CONTEXT = b"local-inference-stack.acceptance-host.v1\0"
MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
CONTROLLER_FILES = (
    "catalog/models.json",
    "compose.yaml",
    "config/runtime-profiles.json",
    "profiles/latency.env",
    "stack",
    "scripts/acceptance-evidence.py",
    "scripts/acceptance-suite.sh",
    "scripts/env_utils.py",
    "scripts/lib/deployment.sh",
    "scripts/model-manager.py",
    "scripts/runtime-reconcile.sh",
    "scripts/runtime.sh",
    "scripts/runtime_identity.py",
)
_GIT_REVISION = re.compile(r"[0-9a-f]{40}")


def _host_identity() -> dict[str, str]:
    machine_id: str | None = None
    for path in MACHINE_ID_PATHS:
        try:
            candidate = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if candidate and len(candidate) <= 256:
            machine_id = candidate
            break
    if machine_id is None:
        raise RecoveryError("cannot establish the rollback host fingerprint")
    architecture = platform.machine()
    if architecture == "amd64":
        architecture = "x86_64"
    return {
        "fingerprintType": "machine-id-sha256-v1",
        "fingerprint": hashlib.sha256(
            HOST_FINGERPRINT_CONTEXT + machine_id.encode("utf-8")
        ).hexdigest(),
        "environmentKind": environment_kind(
            system=platform.system().lower(), kernel_release=platform.release()
        ),
        "architecture": architecture,
    }


def controller_snapshot(paths: ProjectPaths) -> tuple[str, dict[str, str]]:
    """Capture the exact executor material set from a clean tracked tree."""

    status = run(
        ["git", "status", "--porcelain=v1", "--untracked-files=no"],
        cwd=paths.root,
        timeout=20,
        check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise ConfigError(
            "rollout requires a clean tracked worktree so its controller can be reproduced"
        )
    revision = run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=paths.root,
        timeout=20,
    ).stdout.strip()
    if _GIT_REVISION.fullmatch(revision) is None:
        raise ConfigError("rollout cannot establish the current Git revision")
    relative_paths = set(CONTROLLER_FILES)
    relative_paths.update(
        path.relative_to(paths.root).as_posix()
        for path in (paths.root / "src" / "local_inference_stack").glob("*.py")
    )
    materials = {
        relative: sha256_file(paths.root / relative)
        for relative in sorted(relative_paths)
    }
    return revision, materials


def _strict_json(path: Path, *, maximum_bytes: int = 1024 * 1024) -> dict[str, Any]:
    body = read_file_bytes(path, maximum_bytes=maximum_bytes)

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise IntegrityError("rollback evidence contains a duplicate JSON key")
            document[key] = value
        return document

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-standard number")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise IntegrityError("rollback evidence is not strict JSON") from error
    if not isinstance(value, dict):
        raise IntegrityError("rollback evidence must be a JSON object")
    return value


def acceptance_identity(
    paths: ProjectPaths, model_id: str, admission: dict[str, Any]
) -> dict[str, Any]:
    """Freeze the fresh host evidence already selected by strict admission."""

    discovered = admission.get("hostAcceptanceEvidence")
    relative_value = discovered.get("evidence") if isinstance(discovered, dict) else None
    if not isinstance(relative_value, str):
        raise RecoveryError("rollback source has no fresh matching host acceptance evidence")
    relative = Path(relative_value)
    if (
        relative.is_absolute()
        or relative.parts[:2] != ("logs", "acceptance")
        or ".." in relative.parts
        or relative.suffix != ".json"
        or relative.name.endswith(".run.json")
    ):
        raise IntegrityError("rollback acceptance evidence path is outside the private log store")
    path = paths.root / relative
    try:
        metadata = path.lstat()
    except OSError as error:
        raise IntegrityError("rollback acceptance evidence is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise IntegrityError("rollback acceptance evidence is not a private single-link file")
    evidence = _strict_json(path)
    admitted_sha256 = discovered.get("evidenceSha256")
    admitted_self_sha256 = discovered.get("evidenceSelfSha256")
    evidence_sha256 = sha256_file(path)
    expected_self = canonical_sha256(
        {key: value for key, value in evidence.items() if key != "selfSha256"}
    )
    if (
        not isinstance(admitted_sha256, str)
        or admitted_sha256 != evidence_sha256
        or not isinstance(admitted_self_sha256, str)
        or admitted_self_sha256 != evidence.get("selfSha256")
        or evidence.get("selfSha256") != expected_self
        or evidence.get("catalogModelId") != model_id
        or evidence.get("status") != "passed"
        or evidence.get("mode") not in {"quick", "full"}
        or not isinstance(evidence.get("finishedAt"), str)
    ):
        raise IntegrityError("rollback acceptance evidence subject is incomplete or changed")
    return {
        "mode": evidence["mode"],
        "status": "passed",
        "evidencePath": relative.as_posix(),
        "evidenceSha256": evidence_sha256,
        "evidenceSelfSha256": evidence["selfSha256"],
        "finishedAt": evidence["finishedAt"],
    }


def _catalog_model(paths: ProjectPaths, model_id: str) -> tuple[dict[str, Any], CatalogDeploymentSpec]:
    try:
        catalog = load_catalog(paths.root / "catalog" / "models.json")
        model = model_by_id(catalog, model_id)
        return model, CatalogDeploymentSpec.from_catalog_model(model)
    except (CatalogError, DeploymentSpecError) as error:
        raise ConfigError("rollout references an invalid Catalog model") from error


def _artifact_identities(
    paths: ProjectPaths, spec: CatalogDeploymentSpec
) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    for artifact in spec.artifacts:
        if not artifact.required:
            continue
        relative = Path("models") / spec.model_directory / artifact.filename
        path = paths.root / relative
        try:
            metadata = path.lstat()
        except OSError as error:
            raise IntegrityError("rollback artifact is missing") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size != artifact.bytes
            or sha256_file(path) != artifact.sha256
        ):
            raise IntegrityError("rollback artifact identity does not match its Catalog spec")
        identities.append(
            {
                "role": artifact.role,
                "relativePath": relative.as_posix(),
                "bytes": artifact.bytes,
                "sha256": artifact.sha256,
            }
        )
    return identities


def _rendered_compose_sha256(
    paths: ProjectPaths, selection: dict[str, str]
) -> str:
    environment = {**selection, "QWEN_PARALLEL": "1"}
    result = run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(paths.root),
            "-f",
            str(paths.root / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        cwd=paths.root,
        timeout=60,
        env=environment,
    )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise IntegrityError("Docker Compose returned an invalid rollback rendering") from error
    if not isinstance(document, dict):
        raise IntegrityError("Docker Compose returned a non-object rollback rendering")
    return canonical_sha256(document)


def capture_rollback_spec(
    paths: ProjectPaths,
    *,
    transaction_id: str,
    captured_at: str,
    original: dict[str, Any],
    source_admission: dict[str, Any],
) -> RollbackSpec:
    """Capture a verified latency source; callers hold runtime then transaction locks."""

    if not recovery_original_is_safe(original) or not original.get("healthy"):
        raise RecoveryError("upgrade source is not a complete healthy recovery subject")
    if original.get("profile") != "latency":
        raise RecoveryError("rollback-spec v1 is restricted to the qualified latency profile")
    profile_record = original.get("deploymentProfile") or {}
    selection = profile_record.get("values")
    if not isinstance(selection, dict) or set(selection) != RECOVERY_DEPLOYMENT_KEYS:
        raise RecoveryError("upgrade source selection is not the exact current projection")
    model_id = selection.get("QWEN_CATALOG_ID")
    if not isinstance(model_id, str):
        raise RecoveryError("upgrade source selection has no Catalog ID")
    model, catalog_spec = _catalog_model(paths, model_id)
    expected_selection = configuration.catalog_deployment_environment(model)
    if selection != expected_selection:
        raise RecoveryError("upgrade source selection does not equal its strict Catalog projection")
    if (
        model.get("status") != "validated"
        or model.get("lifecycleRole") != "rollback"
        or source_admission.get("catalogRecoveryEligible") is not True
        or source_admission.get("readyToStartExisting") is not True
    ):
        raise RecoveryError("upgrade source is not a trusted validated rollback entry")
    runtime_identity = original.get("runtimeIdentity") or {}
    runtime_configuration = runtime_identity.get("configuration")
    if not isinstance(runtime_configuration, dict):
        raise RecoveryError("upgrade source has no exact live runtime configuration")
    configured_image = runtime_configuration.get("image")
    image_id = runtime_configuration.get("imageId")
    if not isinstance(configured_image, str) or not isinstance(image_id, str):
        raise RecoveryError("upgrade source image identity is incomplete")
    revision, materials = controller_snapshot(paths)
    profile_configuration = configuration.load(paths)["profiles"]["latency"]["environment"]
    try:
        return RollbackSpec.from_verified_components(
            catalog_spec=catalog_spec,
            selection_values=selection,
            runtime_profile_name="latency",
            runtime_profile_environment=profile_configuration,
            artifacts=_artifact_identities(paths, catalog_spec),
            runtime={
                "configuredImage": configured_image,
                "imageId": image_id,
                "effectiveComposeSha256": _rendered_compose_sha256(paths, selection),
                "expectedRuntimeIdentitySha256": runtime_identity["sha256"],
                "expectedRuntimeConfiguration": runtime_configuration,
            },
            controller_git_commit=revision,
            controller_materials=materials,
            host=_host_identity(),
            acceptance=acceptance_identity(paths, model_id, source_admission),
            source_transaction_id=transaction_id,
            captured_at=captured_at,
        )
    except (KeyError, RollbackSpecError) as error:
        raise RecoveryError("upgrade source cannot form an immutable rollback spec") from error


def recovery_original(spec: RollbackSpec) -> dict[str, Any]:
    """Project a validated immutable anchor into the existing recovery adapter."""

    document = spec.document()
    selection = document["selection"]
    runtime = document["runtime"]
    return {
        "healthy": True,
        "containerHealthy": True,
        "profile": document["runtimeProfile"]["name"],
        "containerName": selection["values"]["QWEN_CONTAINER_NAME"],
        "runtimeIdentity": {
            "sha256": runtime["expectedRuntimeIdentitySha256"],
            "configuration": runtime["expectedRuntimeConfiguration"],
        },
        "deploymentProfile": {**selection, "present": True},
        "capturedWithoutSecrets": True,
    }


def verify_rollback_spec(paths: ProjectPaths, spec: RollbackSpec) -> dict[str, Any]:
    """Recheck every local input without fetching, pulling, or mutating runtime."""

    document = spec.document()
    model_id = document["catalogSpec"]["catalogId"]
    model, current_spec = _catalog_model(paths, model_id)
    if (
        current_spec.sha256 != spec.catalog_spec_sha256
        or model.get("status") != "validated"
        or model.get("lifecycleRole") != "rollback"
    ):
        raise IntegrityError("current Catalog no longer contains the exact rollback anchor")
    admission_result = run(
        [
            "python3",
            "scripts/model-manager.py",
            "admit",
            "--model",
            model_id,
            "--existing-selection",
            "--json",
        ],
        cwd=paths.root,
        timeout=60,
        check=False,
    )
    try:
        admission = json.loads(admission_result.stdout)
    except json.JSONDecodeError as error:
        raise IntegrityError("rollback Catalog trust check returned invalid JSON") from error
    recommendation = admission.get("recommendation")
    if (
        admission_result.returncode not in {0, 3}
        or admission.get("mode") != "read-only-existing-selection-admission"
        or not isinstance(recommendation, dict)
        or recommendation.get("id") != model_id
        or admission.get("catalogRecoveryEligible") is not True
        or admission.get("recoveryHostAdmissionPassed") is not True
    ):
        raise IntegrityError("rollback Catalog trust is no longer current")
    revision, materials = controller_snapshot(paths)
    controller = document["controller"]
    expected_materials = {
        item["path"]: item["sha256"] for item in controller["materials"]
    }
    if (
        revision != controller["gitCommit"]
        or materials != expected_materials
        or controller["materialPolicy"] != CONTROLLER_MATERIAL_POLICY
    ):
        raise IntegrityError("rollback controller materials changed; restore the recorded checkout")
    if _host_identity() != document["host"]:
        raise IntegrityError("rollback anchor belongs to another host or environment")
    if _artifact_identities(paths, current_spec) != document["artifacts"]:
        raise IntegrityError("rollback artifacts changed")
    selection = document["selection"]["values"]
    if _rendered_compose_sha256(paths, selection) != document["runtime"]["effectiveComposeSha256"]:
        raise IntegrityError("rollback effective Compose identity changed")
    image = run(
        ["docker", "image", "inspect", document["runtime"]["configuredImage"]],
        cwd=paths.root,
        timeout=30,
        check=False,
    )
    try:
        image_payload = json.loads(image.stdout)
    except json.JSONDecodeError as error:
        raise IntegrityError("rollback image is unavailable locally") from error
    if (
        image.returncode != 0
        or not isinstance(image_payload, list)
        or len(image_payload) != 1
        or image_payload[0].get("Id") != document["runtime"]["imageId"]
    ):
        raise IntegrityError("rollback image identity is unavailable locally")
    evidence_path = paths.root / document["acceptance"]["evidencePath"]
    try:
        evidence_metadata = evidence_path.lstat()
    except OSError as error:
        raise IntegrityError("rollback acceptance evidence is unavailable") from error
    if (
        not stat.S_ISREG(evidence_metadata.st_mode)
        or evidence_metadata.st_uid != os.getuid()
        or evidence_metadata.st_nlink != 1
        or stat.S_IMODE(evidence_metadata.st_mode) != 0o600
        or
        sha256_file(evidence_path) != document["acceptance"]["evidenceSha256"]
        or _strict_json(evidence_path).get("selfSha256")
        != document["acceptance"]["evidenceSelfSha256"]
    ):
        raise IntegrityError("rollback acceptance evidence changed")
    return {
        "scopePolicy": document["scopePolicy"],
        "rollbackSpecSha256": spec.sha256,
        "catalogId": model_id,
        "controllerRevision": revision,
        "localMaterialsVerified": True,
        "networkAcquisitionAllowed": False,
    }


def write_anchor_selection(paths: ProjectPaths, spec: RollbackSpec) -> None:
    """Atomically write only the validated allowlisted selection from an anchor."""

    from scripts.env_utils import atomic_write_private_text

    values = spec.document()["selection"]["values"]
    content = (
        "# Restored from immutable rollback-spec v1; local and intentionally untracked.\n"
        + "".join(f"{key}={shlex.quote(values[key])}\n" for key in sorted(values))
    )
    atomic_write_private_text(paths.root / "profiles" / "deployment.local.env", content)
