"""Canonical reusable-validation drafts and detached signature verification."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from .acceptance import (
    VALIDATION_INPUT_POLICY,
    expected_steps,
    run_manifest_matches_evidence,
    run_record_valid,
    sha256_document,
    validation_input,
)
from .paths import ProjectPaths
from .materials import canonical_bytes
from .result import ConfigError, IntegrityError
from .runner import run


SCHEMA_VERSION = 2
KIND = "local-inference-stack/reusable-validation"
ELIGIBILITY_POLICY = "local-inference-stack/full-host-validation-v2"
TRUST_POLICY = "local-inference-stack/reusable-validation-trust-v2"
SIGNATURE_TRUST_MODEL = "externally-managed-key-with-sha256-fingerprint"
SUBJECT_SCHEMA_VERSION = 1
SUBJECT_POLICY = "local-inference-stack/evidence-derived-subject-v1"
EVIDENCE_SCHEMA_VERSION = 4
EVIDENCE_MAX_AGE_DAYS = 30
FULL_TERMINAL_STEP = expected_steps("full")[-1]
ALLOWED_SIGNATURE_TOOLS = ("cosign", "minisign")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
REVISION_PATTERN = re.compile(r"[0-9a-f]{40,64}")
ELIGIBILITY_CHECKS = (
    "sourcePrivateAcceptanceRecord",
    "schemaV4",
    "passed",
    "fullMode",
    "latencyProfile",
    "runnerOwnedStructuredFullRun",
    "stableValidationInput",
    "cleanValidationSource",
    "currentHostArtifactRuntimeConfig",
)


def _sha256_file(path: Path, *, maximum_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                total += len(chunk)
                if maximum_bytes is not None and total > maximum_bytes:
                    raise IntegrityError(f"file exceeds the verification size limit: {path}")
                digest.update(chunk)
    except OSError as exc:
        raise IntegrityError(f"cannot read file for identity verification: {path}: {exc}") from exc
    return digest.hexdigest()


def _verification_input_snapshot(
    path: Path,
    *,
    label: str,
    maximum_bytes: int = 1024 * 1024,
) -> tuple[Path, str]:
    """Copy one verifier input once, then hash and verify that same snapshot."""

    source_descriptor: int | None = None
    snapshot_descriptor: int | None = None
    snapshot_name: str | None = None
    try:
        source_descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(source_descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or mode & 0o022
            or mode & 0o7000
            or before.st_size > maximum_bytes
        ):
            raise IntegrityError(
                f"{label} must be a safe current-user regular file with one link, "
                "no group/other write permission, and a bounded size"
            )
        snapshot_descriptor, snapshot_name = tempfile.mkstemp(
            prefix=f"stack-attestation-{label.replace(' ', '-')}-",
            suffix=".snapshot",
        )
        os.fchmod(snapshot_descriptor, 0o600)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source_descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise IntegrityError(f"{label} exceeds the verification size limit")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(snapshot_descriptor, view)
                if written <= 0:
                    raise IntegrityError(f"cannot snapshot {label}")
                view = view[written:]
        after = os.fstat(source_descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
        )
        if total != before.st_size or identity_after != identity_before:
            raise IntegrityError(f"{label} changed while it was snapshotted")
        os.fsync(snapshot_descriptor)
        os.close(snapshot_descriptor)
        snapshot_descriptor = None
        return Path(snapshot_name), digest.hexdigest()
    except IntegrityError:
        raise
    except OSError as exc:
        raise IntegrityError(f"cannot safely snapshot {label}: {exc}") from exc
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if snapshot_descriptor is not None:
            os.close(snapshot_descriptor)
        if snapshot_name is not None and snapshot_descriptor is not None:
            Path(snapshot_name).unlink(missing_ok=True)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso8601(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_facts(
    paths: ProjectPaths, *, ignored_paths: tuple[Path, ...] = ()
) -> dict[str, Any]:
    revision = run(["git", "rev-parse", "HEAD"], cwd=paths.root).stdout.strip()
    status_command = ["git", "status", "--porcelain", "--", "."]
    for ignored in ignored_paths:
        try:
            relative = ignored.absolute().relative_to(paths.root.absolute())
        except ValueError:
            continue
        if relative.parts and ".." not in relative.parts:
            status_command.append(
                ":(top,exclude,literal)" + relative.as_posix()
            )
    dirty = bool(run(status_command, cwd=paths.root).stdout.strip())
    return {"revision": revision, "dirty": dirty}


def _private_json(path: Path) -> dict[str, Any] | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size > 1024 * 1024
        ):
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            document = json.load(handle)
        return document if isinstance(document, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _relative_acceptance_path(paths: ProjectPaths, evidence_path: Path) -> str | None:
    try:
        relative = evidence_path.absolute().relative_to(paths.root.absolute())
    except ValueError:
        return None
    if len(relative.parts) < 3 or relative.parts[:2] != ("logs", "acceptance"):
        return None
    if relative.suffix != ".json" or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    return relative.as_posix()


def _runner_manifest_source_valid(
    paths: ProjectPaths, evidence: dict[str, Any]
) -> bool:
    run_record = evidence.get("run")
    identity = run_record.get("manifest") if isinstance(run_record, dict) else None
    source = identity.get("sourcePath") if isinstance(identity, dict) else None
    if not isinstance(source, str):
        return False
    relative = Path(source)
    if (
        relative.is_absolute()
        or relative.parts[:2] != ("logs", "acceptance")
        or not relative.name.endswith(".run.json")
        or ".." in relative.parts
    ):
        return False
    path = paths.root / relative
    manifest = _private_json(path)
    if manifest is None:
        return False
    try:
        source_sha = _sha256_file(path, maximum_bytes=1024 * 1024)
    except IntegrityError:
        return False
    return bool(
        source_sha == identity.get("sourceSha256")
        and run_manifest_matches_evidence(manifest, run_record, evidence)
    )


def _load_model_manager(paths: ProjectPaths) -> ModuleType:
    source = paths.root / "scripts" / "model-manager.py"
    spec = importlib.util.spec_from_file_location("_stack_attestation_model_manager", source)
    if spec is None or spec.loader is None:
        raise ConfigError("cannot load the model manager for attestation eligibility")
    module = importlib.util.module_from_spec(spec)
    root_text = str(paths.root)
    inserted = root_text not in sys.path
    if inserted:
        sys.path.insert(0, root_text)
    try:
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(root_text)
    return module


def _evidence_self_hash(evidence: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in evidence.items() if key != "selfSha256"}
    return hashlib.sha256(canonical_bytes(unsigned)).hexdigest()


def _evidence_subject(
    paths: ProjectPaths,
    evidence_path: Path,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    relative_source = _relative_acceptance_path(paths, evidence_path)
    if relative_source is None:
        raise IntegrityError("acceptance subject source path is invalid")
    completed_at = _parse_time(evidence.get("finishedAt"))
    if completed_at is None:
        raise IntegrityError("acceptance subject completion time is invalid")
    configuration = evidence.get("configuration")
    run_record = evidence.get("run")
    host = evidence.get("host")
    artifact = evidence.get("artifact")
    runtime = evidence.get("runtime")
    if not all(
        isinstance(value, dict)
        for value in (configuration, run_record, host, artifact, runtime)
    ):
        raise IntegrityError("acceptance subject fields are incomplete")
    return {
        "schemaVersion": SUBJECT_SCHEMA_VERSION,
        "policy": SUBJECT_POLICY,
        "project": {
            "revision": evidence.get("gitCommit"),
            "dirty": evidence.get("gitState") != "clean",
        },
        "acceptance": {
            "sourcePath": relative_source,
            "sourceSha256": _sha256_file(
                evidence_path, maximum_bytes=1024 * 1024
            ),
            "schemaVersion": evidence.get("schemaVersion"),
            "selfSha256": _evidence_self_hash(evidence),
            "configurationSha256": hashlib.sha256(
                canonical_bytes(configuration)
            ).hexdigest(),
            "modelId": evidence.get("catalogModelId"),
            "mode": evidence.get("mode"),
            "profile": evidence.get("profile"),
            "status": evidence.get("status"),
            "completedAt": evidence.get("finishedAt"),
            "durationSeconds": evidence.get("durationSeconds"),
            "terminalStep": evidence.get("terminalStep"),
            "validationInputSha256": evidence.get("validationInput", {}).get(
                "sha256"
            ),
            "runManifestSha256": run_record.get("manifest", {}).get(
                "sourceSha256"
            ),
            "stepResultsSha256": sha256_document(
                run_record.get("stepResults")
            ),
        },
        "validationInput": evidence.get("validationInput"),
        "hardware": {
            "architecture": host.get("architecture"),
            "environmentKind": host.get("environmentKind"),
            "platform": host.get("platform"),
            "ramGiB": host.get("ramGiB"),
            "gpus": host.get("gpus"),
        },
        "artifact": artifact,
        "runtime": {
            "configuredImage": runtime.get("configuredImage"),
            "imageId": runtime.get("imageId"),
            "containerConfigSha256": runtime.get("containerConfigSha256"),
        },
        "run": run_record,
        "lifecycle": {
            "validity": {
                "notBefore": evidence.get("finishedAt"),
                "expiresAt": _iso8601(
                    completed_at + timedelta(days=EVIDENCE_MAX_AGE_DAYS)
                ),
            },
            "revocation": {"revokedAt": None, "reason": None},
            "supersession": {"supersedes": [], "supersededBy": None},
        },
    }


def _subject_claims_match(
    payload: dict[str, Any], subject: dict[str, Any]
) -> bool:
    return all(
        payload.get(key) == subject.get(key)
        for key in (
            "project",
            "acceptance",
            "hardware",
            "artifact",
            "runtime",
            "lifecycle",
        )
    )


def _eligibility_claims_match(
    recorded: dict[str, Any], current: dict[str, Any]
) -> bool:
    keys = ("policy", "eligible", "requiredEvidence", "checks", "reasonCodes")
    return all(recorded.get(key) == current.get(key) for key in keys)


def _eligibility(
    paths: ProjectPaths,
    evidence_path: Path,
    evidence: dict[str, Any],
    git: dict[str, Any],
) -> dict[str, Any]:
    relative_source = _relative_acceptance_path(paths, evidence_path)
    self_hash = _evidence_self_hash(evidence)
    configuration = evidence.get("configuration")
    complete = (
        evidence.get("terminalStep") == FULL_TERMINAL_STEP
        and isinstance(evidence.get("durationSeconds"), int)
        and not isinstance(evidence.get("durationSeconds"), bool)
        and evidence.get("durationSeconds", -1) >= 0
        and _parse_time(evidence.get("startedAt")) is not None
        and _parse_time(evidence.get("finishedAt")) is not None
        and isinstance(evidence.get("artifact"), dict)
        and isinstance(evidence.get("runtime"), dict)
        and isinstance(configuration, dict)
        and evidence.get("selfSha256") == self_hash
        and run_record_valid(
            evidence.get("run"),
            mode="full",
            overall_status=evidence.get("status"),
            overall_exit_code=evidence.get("exitCode"),
            configuration=configuration,
        )
        and _runner_manifest_source_valid(paths, evidence)
    )
    validation_input = evidence.get("validationInput")
    checks = {
        "sourcePrivateAcceptanceRecord": relative_source is not None,
        "schemaV4": evidence.get("schemaVersion") == EVIDENCE_SCHEMA_VERSION,
        "passed": (
            evidence.get("status") == "passed"
            and evidence.get("exitCode") == 0
            and evidence.get("failedAtStep") is None
        ),
        "fullMode": evidence.get("mode") == "full",
        "latencyProfile": evidence.get("profile") == "latency",
        "runnerOwnedStructuredFullRun": complete,
        "stableValidationInput": (
            isinstance(validation_input, dict)
            and set(validation_input)
            == {"policy", "catalogSha256", "repositorySha256", "sha256"}
            and validation_input.get("policy") == VALIDATION_INPUT_POLICY
            and all(
                isinstance(validation_input.get(key), str)
                and SHA256_PATTERN.fullmatch(validation_input[key]) is not None
                for key in ("catalogSha256", "repositorySha256", "sha256")
            )
        ),
        "cleanValidationSource": (
            git.get("dirty") is False
            and evidence.get("gitState") == "clean"
            and isinstance(evidence.get("gitCommit"), str)
            and REVISION_PATTERN.fullmatch(evidence["gitCommit"]) is not None
        ),
        "currentHostArtifactRuntimeConfig": False,
    }
    if all(checks[key] for key in ELIGIBILITY_CHECKS[:-1]):
        try:
            manager = _load_model_manager(paths)
            catalog = manager.load_catalog()
            model = manager.model_by_id(catalog, evidence.get("catalogModelId"))
            host = manager.host_assessment(None, None)
            checks["currentHostArtifactRuntimeConfig"] = bool(
                model and manager.acceptance_matches_host(model, host, evidence)
            )
        except (Exception, SystemExit):
            checks["currentHostArtifactRuntimeConfig"] = False
    reasons = [key for key in ELIGIBILITY_CHECKS if not checks[key]]
    return {
        "policy": ELIGIBILITY_POLICY,
        "eligible": not reasons,
        "evaluatedAt": _iso8601(datetime.now(timezone.utc)),
        "requiredEvidence": {
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "mode": "full",
            "profile": "latency",
            "maxAgeDays": EVIDENCE_MAX_AGE_DAYS,
            "runnerManifestSchemaVersion": 1,
            "validationInputPolicy": VALIDATION_INPUT_POLICY,
        },
        "checks": checks,
        "reasonCodes": reasons,
    }


def create_draft(paths: ProjectPaths, evidence_path: Path) -> dict[str, Any]:
    evidence = _private_json(evidence_path)
    if evidence is None:
        raise ConfigError(
            "local acceptance evidence must be a private current-user schema-v4 JSON file"
        )
    if evidence.get("schemaVersion") != EVIDENCE_SCHEMA_VERSION:
        raise ConfigError("reusable attestation requires local acceptance schema v4")
    evidence_digest = _evidence_self_hash(evidence)
    if evidence.get("selfSha256") != evidence_digest:
        raise IntegrityError("local acceptance evidence self-hash mismatch")
    if evidence.get("status") != "passed" or evidence.get("exitCode") != 0:
        raise ConfigError("reusable attestation requires passed local acceptance")
    relative_source = _relative_acceptance_path(paths, evidence_path)
    if relative_source is None:
        raise ConfigError("reusable attestation source must be logs/acceptance/*.json")
    git = _git_facts(paths)
    eligibility = _eligibility(paths, evidence_path, evidence, git)
    subject = _evidence_subject(paths, evidence_path, evidence)
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "createdAt": _iso8601(datetime.now(timezone.utc)),
        "project": subject["project"],
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "release": platform.release(),
        },
        "eligibility": eligibility,
        "acceptance": subject["acceptance"],
        "hardware": subject["hardware"],
        "artifact": subject["artifact"],
        "runtime": subject["runtime"],
        "evidenceSubject": subject,
        "evidenceSubjectSha256": sha256_document(subject),
        "trust": {
            "policy": TRUST_POLICY,
            "artifactIdentity": "catalog-sha256-plus-current-secure-stat-stamp",
            "runtimeIdentity": "exact-rendered-compose-and-live-container",
            "publisherMetadata": "catalog-metadata-not-cryptographically-verified",
            "signature": {
                "requiredForPromotion": True,
                "trustModel": SIGNATURE_TRUST_MODEL,
                "allowedTools": list(ALLOWED_SIGNATURE_TOOLS),
                "keyFingerprint": {
                    "algorithm": "sha256",
                    "expectedValueRequired": True,
                    "source": "external-trust-anchor",
                },
            },
        },
        "lifecycle": subject["lifecycle"],
        "privacy": {
            "hostFingerprintIncluded": False,
            "promptsIncluded": False,
            "responsesIncluded": False,
            "credentialsIncluded": False,
        },
    }
    digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    return {
        "schemaVersion": SCHEMA_VERSION,
        "payload": payload,
        "payloadSha256": digest,
        "signature": None,
        "validationStatus": "draft-eligible-unsigned" if eligibility["eligible"] else "draft-ineligible",
    }


def write_private_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validated_lifecycle(payload: dict[str, Any], *, now: datetime) -> dict[str, bool]:
    lifecycle = payload.get("lifecycle")
    if not isinstance(lifecycle, dict):
        raise IntegrityError("reusable attestation lifecycle metadata is invalid")
    validity = lifecycle.get("validity")
    revocation = lifecycle.get("revocation")
    supersession = lifecycle.get("supersession")
    if not isinstance(validity, dict) or set(validity) != {"notBefore", "expiresAt"}:
        raise IntegrityError("reusable attestation validity metadata is invalid")
    not_before = _parse_time(validity.get("notBefore"))
    expires_at = _parse_time(validity.get("expiresAt"))
    eligible = (payload.get("eligibility") or {}).get("eligible") is True
    if eligible and (not_before is None or expires_at is None or expires_at <= not_before):
        raise IntegrityError("eligible reusable attestation has invalid validity bounds")
    if not eligible and any(value is not None for value in validity.values()) and (
        not_before is None or expires_at is None or expires_at <= not_before
    ):
        raise IntegrityError("reusable attestation has invalid validity bounds")
    if not isinstance(revocation, dict) or set(revocation) != {"revokedAt", "reason"}:
        raise IntegrityError("reusable attestation revocation metadata is invalid")
    revoked_at = revocation.get("revokedAt")
    if revoked_at is not None and _parse_time(revoked_at) is None:
        raise IntegrityError("reusable attestation revokedAt is invalid")
    if revocation.get("reason") is not None and not isinstance(revocation.get("reason"), str):
        raise IntegrityError("reusable attestation revocation reason is invalid")
    if not isinstance(supersession, dict) or set(supersession) != {"supersedes", "supersededBy"}:
        raise IntegrityError("reusable attestation supersession metadata is invalid")
    supersedes = supersession.get("supersedes")
    superseded_by = supersession.get("supersededBy")
    if not isinstance(supersedes, list) or not all(
        isinstance(item, str) and SHA256_PATTERN.fullmatch(item) for item in supersedes
    ):
        raise IntegrityError("reusable attestation supersedes metadata is invalid")
    if superseded_by is not None and (
        not isinstance(superseded_by, str) or not SHA256_PATTERN.fullmatch(superseded_by)
    ):
        raise IntegrityError("reusable attestation supersededBy metadata is invalid")
    return {
        "notYetValid": not_before is not None and now < not_before,
        "expired": expires_at is not None and now > expires_at,
        "revoked": revoked_at is not None,
        "superseded": superseded_by is not None,
    }


def _validate_signature_metadata(signature: Any, payload_sha256: str) -> bool:
    if signature is None:
        return False
    required = {
        "schemaVersion",
        "tool",
        "detachedFile",
        "sha256",
        "payloadSha256",
        "signedAt",
        "trustModel",
    }
    if not isinstance(signature, dict) or set(signature) != required:
        raise IntegrityError("detached signature metadata is invalid")
    detached_file = signature.get("detachedFile")
    if (
        signature.get("schemaVersion") != 1
        or signature.get("tool") not in ALLOWED_SIGNATURE_TOOLS
        or not isinstance(detached_file, str)
        or not detached_file
        or Path(detached_file).name != detached_file
        or not isinstance(signature.get("sha256"), str)
        or not SHA256_PATTERN.fullmatch(signature["sha256"])
        or signature.get("payloadSha256") != payload_sha256
        or _parse_time(signature.get("signedAt")) is None
        or signature.get("trustModel") != SIGNATURE_TRUST_MODEL
    ):
        raise IntegrityError("detached signature metadata is invalid")
    return True


def verify_document(
    document: Any,
    *,
    require_signature: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        raise IntegrityError("unsupported reusable attestation schema")
    payload = document.get("payload")
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != SCHEMA_VERSION
        or payload.get("kind") != KIND
    ):
        raise IntegrityError("reusable attestation payload type is invalid")
    expected = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    if document.get("payloadSha256") != expected:
        raise IntegrityError("reusable attestation payload hash mismatch")
    subject = payload.get("evidenceSubject")
    if (
        not isinstance(subject, dict)
        or subject.get("schemaVersion") != SUBJECT_SCHEMA_VERSION
        or subject.get("policy") != SUBJECT_POLICY
        or payload.get("evidenceSubjectSha256") != sha256_document(subject)
        or not _subject_claims_match(payload, subject)
        or not isinstance(subject.get("acceptance"), dict)
        or not isinstance(subject.get("validationInput"), dict)
        or subject["acceptance"].get("validationInputSha256")
        != subject["validationInput"].get("sha256")
        or not isinstance(subject.get("run"), dict)
        or not isinstance(subject["run"].get("manifest"), dict)
        or subject["acceptance"].get("runManifestSha256")
        != subject["run"].get("manifest", {}).get("sourceSha256")
        or subject["acceptance"].get("stepResultsSha256")
        != sha256_document(subject["run"].get("stepResults"))
    ):
        raise IntegrityError("reusable attestation evidence-derived subject is invalid")
    project = payload.get("project")
    if (
        not isinstance(project, dict)
        or not isinstance(project.get("dirty"), bool)
        or not isinstance(project.get("revision"), str)
        or not REVISION_PATTERN.fullmatch(project["revision"])
    ):
        raise IntegrityError("reusable attestation Git revision metadata is invalid")
    acceptance = payload.get("acceptance")
    if not isinstance(acceptance, dict):
        raise IntegrityError("reusable attestation acceptance metadata is invalid")
    source_path = acceptance.get("sourcePath")
    if (
        not isinstance(source_path, str)
        or not source_path.startswith("logs/acceptance/")
        or Path(source_path).is_absolute()
        or ".." in Path(source_path).parts
        or not SHA256_PATTERN.fullmatch(str(acceptance.get("sourceSha256", "")))
        or acceptance.get("schemaVersion") != EVIDENCE_SCHEMA_VERSION
        or not SHA256_PATTERN.fullmatch(str(acceptance.get("selfSha256", "")))
        or not SHA256_PATTERN.fullmatch(str(acceptance.get("configurationSha256", "")))
        or not SHA256_PATTERN.fullmatch(
            str(acceptance.get("validationInputSha256", ""))
        )
        or not SHA256_PATTERN.fullmatch(str(acceptance.get("runManifestSha256", "")))
        or not SHA256_PATTERN.fullmatch(str(acceptance.get("stepResultsSha256", "")))
        or not isinstance(acceptance.get("modelId"), str)
        or not acceptance.get("modelId")
        or acceptance.get("status") != "passed"
    ):
        raise IntegrityError("reusable attestation acceptance metadata is invalid")
    eligibility = payload.get("eligibility")
    checks = eligibility.get("checks") if isinstance(eligibility, dict) else None
    reason_codes = eligibility.get("reasonCodes") if isinstance(eligibility, dict) else None
    if (
        not isinstance(eligibility, dict)
        or eligibility.get("policy") != ELIGIBILITY_POLICY
        or not isinstance(eligibility.get("eligible"), bool)
        or _parse_time(eligibility.get("evaluatedAt")) is None
        or eligibility.get("requiredEvidence")
        != {
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "mode": "full",
            "profile": "latency",
            "maxAgeDays": EVIDENCE_MAX_AGE_DAYS,
            "runnerManifestSchemaVersion": 1,
            "validationInputPolicy": VALIDATION_INPUT_POLICY,
        }
        or not isinstance(checks, dict)
        or set(checks) != set(ELIGIBILITY_CHECKS)
        or not all(isinstance(value, bool) for value in checks.values())
        or eligibility["eligible"] is not all(checks.values())
        or reason_codes != [key for key in ELIGIBILITY_CHECKS if not checks[key]]
    ):
        raise IntegrityError("reusable attestation eligibility metadata is invalid")
    if eligibility["eligible"] and (
        project.get("dirty") is not False
        or
        acceptance.get("mode") != "full"
        or acceptance.get("profile") != "latency"
        or acceptance.get("terminalStep") != FULL_TERMINAL_STEP
    ):
        raise IntegrityError("eligible reusable attestation is not complete full evidence")
    trust = payload.get("trust")
    if trust != {
        "policy": TRUST_POLICY,
        "artifactIdentity": "catalog-sha256-plus-current-secure-stat-stamp",
        "runtimeIdentity": "exact-rendered-compose-and-live-container",
        "publisherMetadata": "catalog-metadata-not-cryptographically-verified",
        "signature": {
            "requiredForPromotion": True,
            "trustModel": SIGNATURE_TRUST_MODEL,
            "allowedTools": list(ALLOWED_SIGNATURE_TOOLS),
            "keyFingerprint": {
                "algorithm": "sha256",
                "expectedValueRequired": True,
                "source": "external-trust-anchor",
            },
        },
    }:
        raise IntegrityError("reusable attestation trust metadata is invalid")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lifecycle = _validated_lifecycle(payload, now=current_time)
    signature = document.get("signature")
    signature_metadata_valid = _validate_signature_metadata(signature, expected)
    if require_signature and not signature_metadata_valid:
        raise IntegrityError("a detached trusted signature is required")
    expected_status = "signed-unverified" if signature_metadata_valid else (
        "draft-eligible-unsigned" if eligibility["eligible"] else "draft-ineligible"
    )
    if document.get("validationStatus") != expected_status:
        raise IntegrityError("reusable attestation validation status is inconsistent")
    return {
        "payloadSha256": expected,
        "cleanTree": project["dirty"] is False,
        "subject": {
            "modelId": acceptance.get("modelId"),
            "mode": acceptance.get("mode"),
            "profile": acceptance.get("profile"),
            "validationInputSha256": acceptance.get("validationInputSha256"),
            "hardware": subject.get("hardware"),
        },
        "eligibility": eligibility,
        "lifecycle": lifecycle,
        "trust": {
            "policy": TRUST_POLICY,
            "signatureRequiredForPromotion": True,
            "trustedSignatureVerified": False,
        },
        "signature": {
            "present": signature is not None,
            "metadataValid": signature_metadata_valid,
            "cryptographicallyVerified": False,
            "tool": signature.get("tool") if isinstance(signature, dict) else None,
        },
        # Metadata alone is never promotion authority. Only verify_detached can
        # set this after cryptographic verification and a fresh current re-check.
        "promotionEligible": False,
    }


def verify_file(path: Path, *, require_signature: bool = False) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"cannot read reusable attestation: {exc}") from exc
    return verify_document(document, require_signature=require_signature)


def _payload_file(document: dict[str, Any]) -> tuple[int, str]:
    descriptor, name = tempfile.mkstemp(prefix="stack-attestation-", suffix=".json")
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(canonical_bytes(document["payload"]))
        handle.flush()
        os.fsync(handle.fileno())
    return descriptor, name


def _current_source_eligibility(
    paths: ProjectPaths,
    document: dict[str, Any],
    *,
    ignored_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    source_path = paths.root / document["payload"]["acceptance"]["sourcePath"]
    evidence = _private_json(source_path)
    if evidence is None:
        raise IntegrityError("acceptance source is missing or no longer a private regular file")
    source_sha = _sha256_file(source_path, maximum_bytes=1024 * 1024)
    if source_sha != document["payload"]["acceptance"]["sourceSha256"]:
        raise IntegrityError("acceptance source identity changed after the draft was created")
    git = _git_facts(paths, ignored_paths=ignored_paths)
    current = _eligibility(paths, source_path, evidence, git)
    if not current["eligible"]:
        raise IntegrityError(
            "reusable attestation is not currently eligible for signing: "
            + ", ".join(current["reasonCodes"])
        )
    return current


def _evidence_subject_recheck(
    paths: ProjectPaths, document: dict[str, Any]
) -> str:
    """Rebuild every evidence-derived claim from the retained private source."""

    source_path = paths.root / document["payload"]["acceptance"]["sourcePath"]
    if not source_path.exists() and not source_path.is_symlink():
        return "unavailable"
    evidence = _private_json(source_path)
    if evidence is None:
        return "failed"
    if evidence.get("selfSha256") != _evidence_self_hash(evidence):
        return "failed"
    try:
        expected = _evidence_subject(paths, source_path, evidence)
    except IntegrityError:
        return "failed"
    recorded = document["payload"].get("evidenceSubject")
    return "passed" if (
        recorded == expected
        and document["payload"].get("evidenceSubjectSha256")
        == sha256_document(expected)
        and _subject_claims_match(document["payload"], expected)
    ) else "failed"


def _current_validation_input(
    paths: ProjectPaths, document: dict[str, Any]
) -> dict[str, Any]:
    manager = _load_model_manager(paths)
    catalog = manager.load_catalog()
    model_id = document["payload"]["acceptance"].get("modelId")
    model = manager.model_by_id(catalog, model_id)
    if model is None:
        raise IntegrityError("attested catalog model is no longer present")
    try:
        configuration = manager.acceptance_configuration(model, "full", "latency")
    except Exception as exc:
        raise IntegrityError(
            f"cannot evaluate current stable validation inputs: {exc}"
        ) from exc
    current = validation_input(catalog, model, configuration)
    expected = document["payload"]["acceptance"].get("validationInputSha256")
    return {
        "policy": VALIDATION_INPUT_POLICY,
        "expectedSha256": expected,
        "currentSha256": current["sha256"],
        "matches": expected == current["sha256"],
        "catalogSha256": current["catalogSha256"],
        "repositorySha256": current["repositorySha256"],
    }


def sign_file(
    paths: ProjectPaths,
    attestation_path: Path,
    signature_path: Path,
    secret_key: Path,
    tool: str,
) -> dict[str, Any]:
    try:
        document = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"cannot read reusable attestation: {exc}") from exc
    facts = verify_document(document)
    if document.get("signature") is not None:
        raise IntegrityError("refusing to replace an existing attestation signature")
    if not facts["eligibility"]["eligible"]:
        raise IntegrityError("refusing to sign an ineligible reusable attestation")
    if any(facts["lifecycle"].values()):
        raise IntegrityError("refusing to sign an expired, revoked, or superseded attestation")
    current_eligibility = _current_source_eligibility(
        paths,
        document,
        ignored_paths=(attestation_path, signature_path),
    )
    if not _eligibility_claims_match(
        document["payload"]["eligibility"], current_eligibility
    ):
        raise IntegrityError(
            "refusing to sign: eligibility claims differ from retained evidence"
        )
    if _evidence_subject_recheck(paths, document) != "passed":
        raise IntegrityError(
            "refusing to sign: evidence-derived subject differs from retained evidence"
        )
    _, payload_name = _payload_file(document)
    payload_path = Path(payload_name)
    try:
        if tool == "minisign":
            argv = ["minisign", "-S", "-s", str(secret_key), "-m", str(payload_path), "-x", str(signature_path)]
        elif tool == "cosign":
            argv = ["cosign", "sign-blob", "--yes", "--key", str(secret_key), "--output-signature", str(signature_path), str(payload_path)]
        else:
            raise ConfigError(f"unsupported attestation signer: {tool}")
        run(argv, cwd=paths.root, timeout=120)
    finally:
        payload_path.unlink(missing_ok=True)
    document["signature"] = {
        "schemaVersion": 1,
        "tool": tool,
        "detachedFile": signature_path.name,
        "sha256": _sha256_file(signature_path, maximum_bytes=1024 * 1024),
        "payloadSha256": document["payloadSha256"],
        "signedAt": _iso8601(datetime.now(timezone.utc)),
        "trustModel": SIGNATURE_TRUST_MODEL,
    }
    document["validationStatus"] = "signed-unverified"
    write_private_json(attestation_path, document)
    return {
        "tool": tool,
        "signature": str(signature_path),
        "attestationUpdated": True,
        "payloadSha256": document["payloadSha256"],
        "trustModel": SIGNATURE_TRUST_MODEL,
    }


def verify_detached(
    paths: ProjectPaths,
    attestation_path: Path,
    signature_path: Path,
    public_key: Path,
    tool: str,
    *,
    trusted_key_sha256: str | None = None,
    require_promotion: bool = False,
) -> dict[str, Any]:
    try:
        document = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"cannot read reusable attestation: {exc}") from exc
    facts = verify_document(document, require_signature=True)
    metadata = document["signature"]
    signature_snapshot: Path | None = None
    public_key_snapshot: Path | None = None
    try:
        signature_snapshot, signature_sha = _verification_input_snapshot(
            signature_path, label="detached signature"
        )
        public_key_snapshot, verification_key_sha = _verification_input_snapshot(
            public_key, label="public key"
        )
        if trusted_key_sha256 is not None and not SHA256_PATTERN.fullmatch(
            trusted_key_sha256
        ):
            raise ConfigError(
                "trusted verification-key fingerprint must be a SHA256 hex digest"
            )
        if (
            metadata.get("tool") != tool
            or metadata.get("detachedFile") != signature_path.name
            or metadata.get("sha256") != signature_sha
            or metadata.get("payloadSha256") != document.get("payloadSha256")
        ):
            raise IntegrityError(
                "detached signature identity does not match attestation metadata"
            )
        _, payload_name = _payload_file(document)
        payload_path = Path(payload_name)
        try:
            if tool == "minisign":
                argv = [
                    "minisign",
                    "-V",
                    "-p",
                    str(public_key_snapshot),
                    "-m",
                    str(payload_path),
                    "-x",
                    str(signature_snapshot),
                ]
            elif tool == "cosign":
                argv = [
                    "cosign",
                    "verify-blob",
                    "--key",
                    str(public_key_snapshot),
                    "--signature",
                    str(signature_snapshot),
                    str(payload_path),
                ]
            else:
                raise ConfigError(f"unsupported attestation verifier: {tool}")
            run(argv, cwd=paths.root, timeout=120)
        finally:
            payload_path.unlink(missing_ok=True)
    finally:
        if signature_snapshot is not None:
            signature_snapshot.unlink(missing_ok=True)
        if public_key_snapshot is not None:
            public_key_snapshot.unlink(missing_ok=True)
    current_eligibility: dict[str, Any] | None = None
    source_recheck = _evidence_subject_recheck(paths, document)
    if source_recheck == "passed":
        try:
            current_eligibility = _current_source_eligibility(paths, document)
        except (IntegrityError, ConfigError):
            current_eligibility = None
    current_validation: dict[str, Any] | None = None
    try:
        current_validation = _current_validation_input(paths, document)
    except (IntegrityError, ConfigError):
        current_validation = None
    trusted_key = bool(
        trusted_key_sha256 is not None
        and verification_key_sha == trusted_key_sha256
    )
    git = _git_facts(paths)
    facts["signature"] = {
        "present": True,
        "metadataValid": True,
        "cryptographicallyVerified": True,
        "tool": tool,
        "detachedSha256": signature_sha,
        "verificationKeySha256": verification_key_sha,
        "expectedTrustedKeySha256": trusted_key_sha256,
        "trustedKeyFingerprint": trusted_key,
    }
    facts["trust"] = {
        "policy": TRUST_POLICY,
        "signatureRequiredForPromotion": True,
        "trustedSignatureVerified": trusted_key,
        "keyProvidedExternally": True,
        "keyFingerprintProvidedExternally": trusted_key_sha256 is not None,
        "trustModel": SIGNATURE_TRUST_MODEL,
    }
    facts["currentEligibility"] = current_eligibility
    facts["currentValidationInput"] = current_validation
    promotion_checks = {
        "cryptographicSignature": True,
        "trustedKeyFingerprint": trusted_key,
        "signedEligibility": facts["eligibility"]["eligible"] is True,
        "currentValidationInput": bool(
            current_validation and current_validation["matches"]
        ),
        "cleanRepository": git.get("dirty") is False,
        "currentLifecycle": not any(facts["lifecycle"].values()),
        "canonicalEvidenceSubject": source_recheck == "passed",
        "currentEligibilityClaims": bool(
            current_eligibility
            and _eligibility_claims_match(
                document["payload"]["eligibility"], current_eligibility
            )
        ),
    }
    facts["sourceEvidenceRecheck"] = source_recheck
    facts["promotion"] = {
        "eligible": all(promotion_checks.values()),
        "checks": promotion_checks,
        "reasonCodes": [
            key for key, value in promotion_checks.items() if not value
        ],
    }
    facts["promotionEligible"] = facts["promotion"]["eligible"]
    if require_promotion and not facts["promotionEligible"]:
        raise IntegrityError(
            "detached signature is cryptographically valid but not valid for promotion: "
            + ", ".join(facts["promotion"]["reasonCodes"]),
            facts=facts,
        )
    return facts
