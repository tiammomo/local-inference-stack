"""Tamper-evident offline bundles that never select or start imported models."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .paths import ProjectPaths
from .result import ConfigError, IntegrityError


SCHEMA_VERSION = 1
MAX_MEMBERS = 64


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_catalog(paths: ProjectPaths) -> dict[str, Any]:
    return json.loads((paths.root / "catalog/models.json").read_text(encoding="utf-8"))


def _model(catalog: dict[str, Any], model_id: str) -> dict[str, Any]:
    for model in catalog.get("models", []):
        if model.get("id") == model_id:
            return model
    raise ConfigError(f"unknown catalog model: {model_id}")


def create(
    paths: ProjectPaths,
    output: Path,
    model_id: str,
    *,
    include_model: bool,
    image_archive: Path | None = None,
) -> dict[str, Any]:
    catalog = _load_catalog(paths)
    model = _model(catalog, model_id)
    with tempfile.TemporaryDirectory(prefix="stack-bundle-") as temporary:
        staging = Path(temporary)
        files: dict[str, dict[str, Any]] = {}

        subset = {
            "schemaVersion": catalog["schemaVersion"],
            "updatedAt": catalog["updatedAt"],
            "artifactPolicy": catalog.get("artifactPolicy"),
            "defaultModel": model_id,
            "models": [model],
        }
        sources: list[tuple[str, bytes | Path]] = [
            ("catalog/models.json", json.dumps(subset, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"),
            ("config/runtime-profiles.json", paths.config_path),
            ("compose.yaml", paths.root / "compose.yaml"),
        ]
        acquisition_dir = paths.root / "cache" / "acquisitions"
        if acquisition_dir.is_dir():
            for record in sorted(acquisition_dir.glob(f"{model_id}--*.json")):
                if record.is_file() and not record.is_symlink():
                    sources.append((f"provenance/{record.name}", record))
        if image_archive is not None:
            if not image_archive.is_file() or image_archive.is_symlink():
                raise ConfigError(f"runtime image archive is missing or unsafe: {image_archive}")
            sources.append(("images/runtime-image.tar", image_archive))
        if include_model:
            for artifact in model["artifacts"]:
                if not artifact.get("required"):
                    continue
                source = paths.root / "models" / model["modelDirectory"] / artifact["filename"]
                if not source.is_file():
                    raise ConfigError(f"required artifact is not available: {source}")
                if source.stat().st_size != artifact["bytes"] or sha256_file(source) != artifact["sha256"]:
                    raise IntegrityError(f"catalog identity mismatch: {source.name}")
                sources.append((f"artifacts/{model_id}/{artifact['filename']}", source))

        for relative, source in sources:
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(source, bytes):
                destination.write_bytes(source)
            else:
                shutil.copyfile(source, destination)
            files[relative] = {"bytes": destination.stat().st_size, "sha256": sha256_file(destination)}

        manifest = {
            "schemaVersion": SCHEMA_VERSION,
            "kind": "local-inference-stack/offline-bundle",
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "modelId": model_id,
            "containsModelArtifacts": include_model,
            "runtimeImage": _compose_image(paths.root / "compose.yaml"),
            "containsRuntimeImageArchive": image_archive is not None,
            "files": files,
            "importPolicy": {"selectModel": False, "startRuntime": False, "hostAdmissionRequired": True},
        }
        (staging / "bundle-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_tar = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        os.close(descriptor)
        try:
            with tarfile.open(temporary_tar, "w") as archive:
                for item in sorted(staging.rglob("*")):
                    if item.is_file():
                        archive.add(item, arcname=item.relative_to(staging).as_posix(), recursive=False)
            os.replace(temporary_tar, output)
        finally:
            if os.path.exists(temporary_tar):
                os.unlink(temporary_tar)
    return {
        "path": str(output),
        "modelId": model_id,
        "files": len(files),
        "containsModelArtifacts": include_model,
        "containsRuntimeImageArchive": image_archive is not None,
    }


def _compose_image(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("image:"):
            return stripped.split(":", 1)[1].strip()
    return None


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if len(members) > MAX_MEMBERS:
        raise IntegrityError("offline bundle has too many members")
    seen: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if member.name in seen or path.is_absolute() or ".." in path.parts or "" in path.parts:
            raise IntegrityError(f"unsafe offline bundle member: {member.name!r}")
        if not member.isfile():
            raise IntegrityError(f"offline bundle contains a non-regular member: {member.name!r}")
        seen.add(member.name)
    if "bundle-manifest.json" not in seen:
        raise IntegrityError("offline bundle has no manifest")
    return members


def verify(path: Path) -> dict[str, Any]:
    try:
        with tarfile.open(path, "r") as archive:
            members = _safe_members(archive)
            manifest_handle = archive.extractfile("bundle-manifest.json")
            if manifest_handle is None:
                raise IntegrityError("offline bundle manifest is unreadable")
            manifest = json.loads(manifest_handle.read())
            if manifest.get("schemaVersion") != SCHEMA_VERSION:
                raise IntegrityError("unsupported offline bundle schema")
            declared = manifest.get("files")
            if not isinstance(declared, dict):
                raise IntegrityError("offline bundle has no file inventory")
            actual_names = {member.name for member in members} - {"bundle-manifest.json"}
            if actual_names != set(declared):
                raise IntegrityError("offline bundle member inventory mismatch")
            for name, identity in declared.items():
                handle = archive.extractfile(name)
                if handle is None:
                    raise IntegrityError(f"offline bundle member is unreadable: {name}")
                digest = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                if size != identity.get("bytes") or digest.hexdigest() != identity.get("sha256"):
                    raise IntegrityError(f"offline bundle identity mismatch: {name}")
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"cannot verify offline bundle: {exc}") from exc
    return {
        "modelId": manifest.get("modelId"),
        "files": len(declared),
        "containsModelArtifacts": manifest.get("containsModelArtifacts") is True,
        "hostAdmissionRequired": True,
    }


def import_artifacts(paths: ProjectPaths, bundle_path: Path) -> dict[str, Any]:
    verified = verify(bundle_path)
    imported: list[str] = []
    with tarfile.open(bundle_path, "r") as archive:
        manifest = json.loads(archive.extractfile("bundle-manifest.json").read())  # type: ignore[union-attr]
        catalog = json.loads(archive.extractfile("catalog/models.json").read())  # type: ignore[union-attr]
        model = _model(catalog, verified["modelId"])
        local_model = _model(_load_catalog(paths), verified["modelId"])
        identity_fields = (
            "id",
            "modelDirectory",
            "artifactRepository",
            "artifactRevision",
            "artifacts",
            "license",
        )
        if any(model.get(field) != local_model.get(field) for field in identity_fields):
            raise IntegrityError(
                "bundle model identity does not match the reviewed local catalog"
            )
        for artifact in model["artifacts"]:
            relative = f"artifacts/{model['id']}/{artifact['filename']}"
            if relative not in manifest["files"]:
                continue
            destination_dir = paths.root / "models" / model["modelDirectory"]
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / artifact["filename"]
            if destination.exists():
                if destination.stat().st_size == artifact["bytes"] and sha256_file(destination) == artifact["sha256"]:
                    continue
                raise IntegrityError(f"refusing to overwrite a different local artifact: {destination}")
            source = archive.extractfile(relative)
            if source is None:
                raise IntegrityError(f"bundle artifact is unreadable: {relative}")
            if shutil.disk_usage(destination_dir).free < artifact["bytes"]:
                raise ConfigError(
                    f"insufficient free disk to import {artifact['filename']}"
                )
            descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part", dir=destination_dir)
            try:
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(descriptor, "wb") as handle:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if size != artifact["bytes"] or digest.hexdigest() != artifact["sha256"]:
                    raise IntegrityError(f"imported artifact identity mismatch: {artifact['filename']}")
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            imported.append(str(destination.relative_to(paths.root)))
    return {"imported": imported, "selected": False, "runtimeStarted": False, "hostAdmissionRequired": True}
