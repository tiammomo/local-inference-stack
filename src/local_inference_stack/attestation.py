"""Canonical reusable-validation drafts and detached signature verification."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import ProjectPaths
from .result import ConfigError, IntegrityError
from .runner import run


SCHEMA_VERSION = 1


def canonical_bytes(document: Any) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _git_facts(paths: ProjectPaths) -> dict[str, Any]:
    revision = run(["git", "rev-parse", "HEAD"], cwd=paths.root).stdout.strip()
    dirty = bool(run(["git", "status", "--porcelain"], cwd=paths.root).stdout.strip())
    return {"revision": revision, "dirty": dirty}


def create_draft(paths: ProjectPaths, evidence_path: Path) -> dict[str, Any]:
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read local acceptance evidence: {exc}") from exc
    if evidence.get("schemaVersion") != 4:
        raise ConfigError("reusable attestation requires local acceptance schema v4")
    unsigned_evidence = {key: value for key, value in evidence.items() if key != "selfSha256"}
    evidence_digest = hashlib.sha256(canonical_bytes(unsigned_evidence)).hexdigest()
    if evidence.get("selfSha256") != evidence_digest:
        raise IntegrityError("local acceptance evidence self-hash mismatch")
    if evidence.get("status") != "passed" or evidence.get("exitCode") != 0:
        raise ConfigError("reusable attestation requires passed local acceptance")
    git = _git_facts(paths)
    configuration = evidence.get("configuration", {})
    host = evidence.get("host", {})
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "local-inference-stack/reusable-validation",
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "project": git,
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "release": platform.release(),
        },
        "acceptance": {
            "sourceSha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            "selfSha256": evidence_digest,
            "configurationSha256": hashlib.sha256(canonical_bytes(configuration)).hexdigest(),
            "modelId": evidence.get("catalogModelId"),
            "mode": evidence.get("mode"),
            "status": evidence.get("status"),
            "completedAt": evidence.get("finishedAt"),
            "durationSeconds": evidence.get("durationSeconds"),
            "terminalStep": evidence.get("terminalStep"),
        },
        "hardware": {
            "architecture": host.get("architecture"),
            "platform": host.get("platform"),
            "ramGiB": host.get("ramGiB"),
            "gpus": host.get("gpus"),
        },
        "artifact": evidence.get("artifact"),
        "runtime": {
            "configuredImage": evidence.get("runtime", {}).get("configuredImage"),
            "imageId": evidence.get("runtime", {}).get("imageId"),
            "containerConfigSha256": evidence.get("runtime", {}).get("containerConfigSha256"),
        },
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
        "validationStatus": "draft-dirty-tree" if git["dirty"] else "draft-unsigned",
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


def verify_document(document: Any, *, require_signature: bool = False) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        raise IntegrityError("unsupported reusable attestation schema")
    payload = document.get("payload")
    expected = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    if document.get("payloadSha256") != expected:
        raise IntegrityError("reusable attestation payload hash mismatch")
    project = payload.get("project", {}) if isinstance(payload, dict) else {}
    if project.get("dirty") is not False:
        raise IntegrityError("reusable attestation was not created from a clean Git tree")
    signature = document.get("signature")
    if require_signature and not signature:
        raise IntegrityError("a detached trusted signature is required")
    if require_signature and (
        not isinstance(signature, dict)
        or signature.get("tool") not in {"minisign", "cosign"}
        or not isinstance(signature.get("sha256"), str)
        or len(signature["sha256"]) != 64
    ):
        raise IntegrityError("detached signature metadata is invalid")
    return {"payloadSha256": expected, "signaturePresent": bool(signature), "cleanTree": True}


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
    verify_document(document)
    if document["payload"]["project"].get("dirty") is not False:
        raise IntegrityError("refusing to sign evidence created from a dirty Git tree")
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
        "tool": tool,
        "detachedFile": signature_path.name,
        "sha256": hashlib.sha256(signature_path.read_bytes()).hexdigest(),
    }
    document["validationStatus"] = "signed-unverified"
    write_private_json(attestation_path, document)
    return {"tool": tool, "signature": str(signature_path), "attestationUpdated": True}


def verify_detached(
    paths: ProjectPaths,
    attestation_path: Path,
    signature_path: Path,
    public_key: Path,
    tool: str,
) -> dict[str, Any]:
    try:
        document = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"cannot read reusable attestation: {exc}") from exc
    facts = verify_document(document, require_signature=True)
    metadata = document["signature"]
    if metadata.get("tool") != tool or metadata.get("sha256") != hashlib.sha256(signature_path.read_bytes()).hexdigest():
        raise IntegrityError("detached signature identity does not match attestation metadata")
    _, payload_name = _payload_file(document)
    payload_path = Path(payload_name)
    try:
        if tool == "minisign":
            argv = ["minisign", "-V", "-p", str(public_key), "-m", str(payload_path), "-x", str(signature_path)]
        elif tool == "cosign":
            argv = ["cosign", "verify-blob", "--key", str(public_key), "--signature", str(signature_path), str(payload_path)]
        else:
            raise ConfigError(f"unsupported attestation verifier: {tool}")
        run(argv, cwd=paths.root, timeout=120)
    finally:
        payload_path.unlink(missing_ok=True)
    facts.update({"signatureVerified": True, "tool": tool})
    return facts
