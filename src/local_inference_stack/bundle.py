"""Tamper-evident offline bundles that never select or start imported models."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .catalog import (
    CatalogError,
    load_catalog,
    model_by_id,
    parse_catalog_json_bytes,
    validate_catalog,
)
from .paths import ProjectPaths
from .result import ConfigError, IntegrityError


SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
READABLE_SCHEMA_VERSIONS = {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
MAX_MEMBERS = 64
MAX_IMAGE_MEMBERS = 10000
MAX_IMAGE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_IMAGE_CONFIG_BYTES = 16 * 1024 * 1024
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
BUNDLE_KIND = "local-inference-stack/offline-bundle"
IMPORT_POLICY = {
    "selectModel": False,
    "startRuntime": False,
    "hostAdmissionRequired": True,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_image_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(
        name
        and not path.is_absolute()
        and ".." not in path.parts
        and "" not in path.parts
    )


def _image_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members = archive.getmembers()
    if len(members) > MAX_IMAGE_MEMBERS:
        raise IntegrityError("runtime image archive has too many members")
    seen: set[str] = set()
    regular: dict[str, tarfile.TarInfo] = {}
    for member in members:
        if member.name in seen or not _safe_image_member_name(member.name):
            raise IntegrityError(
                f"unsafe or duplicate runtime image archive member: {member.name!r}"
            )
        seen.add(member.name)
        if member.isdir():
            continue
        if not member.isfile():
            raise IntegrityError(
                f"runtime image archive contains a non-regular member: {member.name!r}"
            )
        regular[member.name] = member
    if "manifest.json" not in regular:
        raise IntegrityError("runtime image archive has no Docker manifest.json")
    return regular


def _read_image_member(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
    *,
    maximum_bytes: int,
) -> bytes:
    if not _safe_image_member_name(name):
        raise IntegrityError(f"unsafe runtime image archive path: {name!r}")
    member = members.get(name)
    if member is None:
        raise IntegrityError(f"runtime image archive member is missing: {name}")
    if member.size > maximum_bytes:
        raise IntegrityError(f"runtime image archive metadata is oversized: {name}")
    handle = archive.extractfile(member)
    if handle is None:
        raise IntegrityError(f"runtime image archive member is unreadable: {name}")
    body = handle.read(maximum_bytes + 1)
    if len(body) != member.size or len(body) > maximum_bytes:
        raise IntegrityError(f"runtime image archive member size changed: {name}")
    return body


def _layer_diff_id(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
) -> tuple[str, str]:
    handle = archive.extractfile(member)
    if handle is None:
        raise IntegrityError(
            f"runtime image layer is unreadable: {member.name}"
        )
    prefix = handle.read(4)
    handle.seek(0)
    if prefix.startswith(b"\x1f\x8b"):
        stream: BinaryIO = gzip.GzipFile(fileobj=handle, mode="rb")
        compression = "gzip"
    elif prefix == b"\x28\xb5\x2f\xfd":
        raise IntegrityError(
            "zstd-compressed runtime image layers are not supported for diff_id verification"
        )
    else:
        stream = handle
        compression = "none"
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    if stream is not handle:
        stream.close()
    return f"sha256:{digest.hexdigest()}", compression


def _open_image_archive(source: Path | BinaryIO) -> tarfile.TarFile:
    if isinstance(source, Path):
        return tarfile.open(source, mode="r:*")
    return tarfile.open(fileobj=source, mode="r:*")


def _archive_image_identity(
    source: Path | BinaryIO,
    *,
    expected_config_digest: str,
    expected_diff_ids: list[str],
) -> dict[str, Any]:
    try:
        with _open_image_archive(source) as archive:
            members = _image_members(archive)
            manifest_body = _read_image_member(
                archive,
                members,
                "manifest.json",
                maximum_bytes=MAX_IMAGE_MANIFEST_BYTES,
            )
            docker_manifest = json.loads(manifest_body)
            if (
                not isinstance(docker_manifest, list)
                or not docker_manifest
                or len(docker_manifest) > 128
                or any(not isinstance(item, dict) for item in docker_manifest)
            ):
                raise IntegrityError("runtime image Docker manifest is invalid")

            matches: list[tuple[dict[str, Any], str, bytes]] = []
            for entry in docker_manifest:
                config_name = entry.get("Config")
                if not isinstance(config_name, str):
                    continue
                config_body = _read_image_member(
                    archive,
                    members,
                    config_name,
                    maximum_bytes=MAX_IMAGE_CONFIG_BYTES,
                )
                config_digest = f"sha256:{hashlib.sha256(config_body).hexdigest()}"
                if config_digest == expected_config_digest:
                    matches.append((entry, config_name, config_body))
            if len(matches) != 1:
                raise IntegrityError(
                    "runtime image archive must contain exactly one manifest entry "
                    "matching the inspected config digest"
                )

            entry, config_name, config_body = matches[0]
            config = json.loads(config_body)
            if not isinstance(config, dict):
                raise IntegrityError("runtime image config is not an object")
            rootfs = config.get("rootfs")
            diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
            if (
                not isinstance(rootfs, dict)
                or rootfs.get("type") != "layers"
                or not isinstance(diff_ids, list)
                or any(
                    not isinstance(item, str) or SHA256_DIGEST.fullmatch(item) is None
                    for item in diff_ids
                )
            ):
                raise IntegrityError("runtime image config has invalid rootfs diff_ids")
            if diff_ids != expected_diff_ids:
                raise IntegrityError(
                    "runtime image config diff_ids do not match local Docker inspection"
                )
            layers = entry.get("Layers")
            if (
                not isinstance(layers, list)
                or len(layers) != len(diff_ids)
                or any(not isinstance(item, str) for item in layers)
            ):
                raise IntegrityError(
                    "runtime image manifest layer count does not match config diff_ids"
                )

            layer_identities = []
            for index, (layer_name, expected_diff_id) in enumerate(
                zip(layers, diff_ids, strict=True)
            ):
                if not _safe_image_member_name(layer_name):
                    raise IntegrityError(
                        f"unsafe runtime image layer path: {layer_name!r}"
                    )
                member = members.get(layer_name)
                if member is None:
                    raise IntegrityError(
                        f"runtime image layer is missing: {layer_name}"
                    )
                actual_diff_id, compression = _layer_diff_id(archive, member)
                if actual_diff_id != expected_diff_id:
                    raise IntegrityError(
                        f"runtime image layer diff_id mismatch at index {index}"
                    )
                layer_identities.append(
                    {
                        "index": index,
                        "path": layer_name,
                        "archiveBytes": member.size,
                        "diffId": actual_diff_id,
                        "compression": compression,
                    }
                )
            return {
                "format": "docker-save-v1",
                "config": {
                    "path": config_name,
                    "bytes": len(config_body),
                    "digest": expected_config_digest,
                },
                "layers": layer_identities,
            }
    except IntegrityError:
        raise
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"cannot inspect runtime image archive: {exc}") from exc


def _pinned_image(reference: str | None) -> tuple[str, str, str]:
    if not isinstance(reference, str):
        raise ValueError("Compose runtime image is missing")
    named, separator, digest = reference.rpartition("@")
    if not separator or not named or SHA256_DIGEST.fullmatch(digest) is None:
        raise ValueError("Compose runtime image must be pinned by sha256 digest")
    last_slash = named.rfind("/")
    last_colon = named.rfind(":")
    repository = named[:last_colon] if last_colon > last_slash else named
    if not repository:
        raise ValueError("Compose runtime image repository is invalid")
    return repository, digest, named


def _local_image_identity(reference: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", reference],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"cannot inspect the local runtime image: {exc}") from exc
    if completed.returncode != 0:
        raise ConfigError(
            "the Compose-pinned runtime image is not available for local Docker inspection"
        )
    try:
        inspected = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConfigError("local Docker image inspection returned invalid JSON") from exc
    if (
        not isinstance(inspected, list)
        or len(inspected) != 1
        or not isinstance(inspected[0], dict)
    ):
        raise ConfigError("local Docker image inspection returned an invalid inventory")
    document = inspected[0]
    try:
        repository, manifest_digest, _named = _pinned_image(reference)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    config_digest = document.get("Id")
    if not isinstance(config_digest, str) or SHA256_DIGEST.fullmatch(config_digest) is None:
        raise ConfigError("local Docker image has no valid config digest")
    repo_digests = document.get("RepoDigests")
    if not isinstance(repo_digests, list):
        raise ConfigError("local Docker image has no repository digest inventory")
    matching_repo_digests = []
    for item in repo_digests:
        if not isinstance(item, str):
            continue
        item_repository, separator, item_digest = item.rpartition("@")
        if (
            separator
            and item_repository == repository
            and item_digest == manifest_digest
        ):
            matching_repo_digests.append(item)
    if len(matching_repo_digests) != 1:
        raise IntegrityError(
            "Compose pinned digest is not uniquely present in local Docker RepoDigests"
        )
    rootfs = document.get("RootFS")
    diff_ids = rootfs.get("Layers") if isinstance(rootfs, dict) else None
    if (
        not isinstance(rootfs, dict)
        or rootfs.get("Type") != "layers"
        or not isinstance(diff_ids, list)
        or any(
            not isinstance(item, str) or SHA256_DIGEST.fullmatch(item) is None
            for item in diff_ids
        )
    ):
        raise ConfigError("local Docker image has invalid RootFS layer identities")
    return {
        "repoDigest": matching_repo_digests[0],
        "configDigest": config_digest,
        "rootfsDiffIds": diff_ids,
    }


def _runtime_image_identity(reference: str, image_archive: Path) -> dict[str, Any]:
    try:
        _repository, manifest_digest, _named = _pinned_image(reference)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    local = _local_image_identity(reference)
    archive = _archive_image_identity(
        image_archive,
        expected_config_digest=local["configDigest"],
        expected_diff_ids=local["rootfsDiffIds"],
    )
    return {
        "schemaVersion": 1,
        "composeReference": reference,
        "pinnedManifestDigest": manifest_digest,
        "localImage": local,
        "archive": archive,
    }


def _load_catalog(paths: ProjectPaths) -> dict[str, Any]:
    try:
        return load_catalog(paths.root / "catalog/models.json")
    except CatalogError as exc:
        raise ConfigError("cannot read the reviewed local Catalog") from exc


def _model(catalog: dict[str, Any], model_id: str) -> dict[str, Any]:
    try:
        return model_by_id(catalog, model_id)
    except CatalogError as exc:
        raise ConfigError(f"unknown catalog model: {model_id}") from exc


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
    if model.get("lifecycleRole") != "lts":
        raise ConfigError(
            "offline bundle creation currently supports only the LTS lifecycle role"
        )
    runtime_image = _compose_image(paths.root / "compose.yaml")
    runtime_image_identity = None
    if image_archive is not None:
        if not image_archive.is_file() or image_archive.is_symlink():
            raise ConfigError(f"runtime image archive is missing or unsafe: {image_archive}")
        if runtime_image is None:
            raise ConfigError("Compose runtime image is missing")
        runtime_image_identity = _runtime_image_identity(
            runtime_image,
            image_archive,
        )
    with tempfile.TemporaryDirectory(prefix="stack-bundle-") as temporary:
        staging = Path(temporary)
        files: dict[str, dict[str, Any]] = {}

        subset = {
            "schemaVersion": catalog["schemaVersion"],
            "updatedAt": catalog["updatedAt"],
            "scope": catalog["scope"],
            "artifactPolicy": catalog["artifactPolicy"],
            "deploymentPolicy": catalog["deploymentPolicy"],
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
            "kind": BUNDLE_KIND,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "modelId": model_id,
            "containsModelArtifacts": include_model,
            "runtimeImage": runtime_image,
            "containsRuntimeImageArchive": image_archive is not None,
            "files": files,
            "importPolicy": IMPORT_POLICY,
        }
        if runtime_image_identity is not None:
            manifest["runtimeImageIdentity"] = runtime_image_identity
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
    result = {
        "path": str(output),
        "modelId": model_id,
        "files": len(files),
        "containsModelArtifacts": include_model,
        "containsRuntimeImageArchive": image_archive is not None,
    }
    if runtime_image_identity is not None:
        result["runtimeImageIdentity"] = runtime_image_identity
    return result


def _compose_image(path: Path) -> str | None:
    return _compose_image_text(path.read_text(encoding="utf-8"))


def _compose_image_text(document: str) -> str | None:
    for line in document.splitlines():
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


def _read_bundle_bytes(
    archive: tarfile.TarFile,
    name: str,
    *,
    maximum_bytes: int = MAX_IMAGE_MANIFEST_BYTES,
) -> bytes:
    try:
        member = archive.getmember(name)
    except KeyError as exc:
        raise IntegrityError(f"offline bundle member is missing: {name}") from exc
    if member.size > maximum_bytes:
        raise IntegrityError(f"offline bundle JSON member is oversized: {name}")
    handle = archive.extractfile(member)
    if handle is None:
        raise IntegrityError(f"offline bundle member is unreadable: {name}")
    body = handle.read(maximum_bytes + 1)
    if len(body) != member.size or len(body) > maximum_bytes:
        raise IntegrityError(f"offline bundle JSON member size changed: {name}")
    return body


def _read_bundle_json(
    archive: tarfile.TarFile,
    name: str,
    *,
    maximum_bytes: int = MAX_IMAGE_MANIFEST_BYTES,
) -> Any:
    body = _read_bundle_bytes(archive, name, maximum_bytes=maximum_bytes)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IntegrityError(f"offline bundle JSON member is invalid: {name}") from exc


def _validate_file_inventory(declared: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(declared, dict):
        raise IntegrityError("offline bundle has no file inventory")
    for name, identity in declared.items():
        if (
            not isinstance(name, str)
            or not isinstance(identity, dict)
            or not isinstance(identity.get("bytes"), int)
            or isinstance(identity.get("bytes"), bool)
            or identity["bytes"] < 0
            or not isinstance(identity.get("sha256"), str)
            or SHA256_HEX.fullmatch(identity["sha256"]) is None
        ):
            raise IntegrityError(
                f"offline bundle file identity record is invalid: {name!r}"
            )
    return declared


def _validate_legacy_bundle_catalog(
    catalog: dict[str, Any], model: dict[str, Any]
) -> None:
    """Validate the quarantined schema-v1 subset used by artifact-only bundles."""

    updated_at = catalog.get("updatedAt")
    model_directory = model.get("modelDirectory")
    directory_path = (
        PurePosixPath(model_directory) if isinstance(model_directory, str) else None
    )
    license_metadata = model.get("license")
    artifacts = model.get("artifacts")
    try:
        updated_at_valid = (
            isinstance(updated_at, str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", updated_at) is not None
            and date.fromisoformat(updated_at) is not None
        )
    except ValueError:
        updated_at_valid = False
    if (
        catalog.get("schemaVersion") != LEGACY_SCHEMA_VERSION
        or not updated_at_valid
        or not isinstance(catalog.get("artifactPolicy"), dict)
        or directory_path is None
        or len(directory_path.parts) != 1
        or directory_path.parts[0] != model_directory
        or model_directory in {".", ".."}
        or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            str(model.get("artifactRepository", "")),
        )
        or re.fullmatch(r"[0-9a-f]{40}", str(model.get("artifactRevision", "")))
        is None
        or not isinstance(license_metadata, dict)
        or not isinstance(license_metadata.get("spdx"), str)
        or not license_metadata["spdx"].strip()
        or license_metadata.get("reviewRequired") is not True
        or not isinstance(artifacts, list)
        or not artifacts
        or len(
            [
                artifact
                for artifact in artifacts
                if isinstance(artifact, dict)
                and artifact.get("role") == "model"
                and artifact.get("required") is True
            ]
        )
        != 1
    ):
        raise IntegrityError("legacy offline bundle Catalog is invalid")


def _validate_bundle_semantics(
    archive: tarfile.TarFile,
    manifest: dict[str, Any],
    declared: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Bind top-level claims, the bundled catalog, and artifact inventory."""

    if manifest.get("kind") != BUNDLE_KIND:
        raise IntegrityError("offline bundle kind is invalid")
    if manifest.get("importPolicy") != IMPORT_POLICY:
        raise IntegrityError("offline bundle import policy is invalid")
    model_id = manifest.get("modelId")
    model_path = PurePosixPath(model_id) if isinstance(model_id, str) else None
    if (
        model_path is None
        or len(model_path.parts) != 1
        or model_path.parts[0] != model_id
    ):
        raise IntegrityError("offline bundle model ID is invalid")

    try:
        catalog = parse_catalog_json_bytes(
            _read_bundle_bytes(archive, "catalog/models.json")
        )
    except CatalogError as exc:
        raise IntegrityError("offline bundle Catalog JSON is invalid") from exc
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if (
        not isinstance(catalog, dict)
        or catalog.get("defaultModel") != model_id
        or not isinstance(models, list)
        or len(models) != 1
        or not isinstance(models[0], dict)
        or models[0].get("id") != model_id
    ):
        raise IntegrityError(
            "offline bundle model ID does not match its single-model catalog"
        )
    model = models[0]
    artifacts = model.get("artifacts")
    if not isinstance(artifacts, list):
        raise IntegrityError("offline bundle catalog artifact inventory is invalid")

    catalog_artifacts: dict[str, dict[str, Any]] = {}
    required_paths: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise IntegrityError("offline bundle catalog artifact identity is invalid")
        filename = artifact.get("filename")
        artifact_bytes = artifact.get("bytes")
        artifact_sha256 = artifact.get("sha256")
        required = artifact.get("required")
        filename_path = PurePosixPath(filename) if isinstance(filename, str) else None
        if (
            filename_path is None
            or len(filename_path.parts) != 1
            or filename_path.parts[0] != filename
            or not isinstance(artifact_bytes, int)
            or isinstance(artifact_bytes, bool)
            or artifact_bytes < 0
            or not isinstance(artifact_sha256, str)
            or SHA256_HEX.fullmatch(artifact_sha256) is None
            or not isinstance(required, bool)
        ):
            raise IntegrityError("offline bundle catalog artifact identity is invalid")
        relative = f"artifacts/{model_id}/{filename}"
        if relative in catalog_artifacts:
            raise IntegrityError("offline bundle catalog has duplicate artifact paths")
        catalog_artifacts[relative] = artifact
        if required:
            required_paths.add(relative)

    present_paths = {
        name
        for name in declared
        if PurePosixPath(name).parts[:1] == ("artifacts",)
    }
    unexpected = present_paths - set(catalog_artifacts)
    if unexpected:
        raise IntegrityError(
            "offline bundle contains artifacts absent from its catalog: "
            + ", ".join(sorted(unexpected))
        )
    contains_artifacts = manifest.get("containsModelArtifacts")
    if not isinstance(contains_artifacts, bool):
        raise IntegrityError("offline bundle model artifact declaration is invalid")
    if contains_artifacts:
        missing = required_paths - present_paths
        if missing:
            raise IntegrityError(
                "offline bundle is missing required model artifacts: "
                + ", ".join(sorted(missing))
            )
    elif present_paths:
        raise IntegrityError(
            "offline bundle contains model artifacts while declaring none"
        )

    for relative in present_paths:
        identity = declared[relative]
        artifact = catalog_artifacts[relative]
        if (
            identity.get("bytes") != artifact["bytes"]
            or identity.get("sha256") != artifact["sha256"]
        ):
            raise IntegrityError(
                f"offline bundle artifact identity does not match its catalog: {relative}"
            )
    if catalog.get("schemaVersion") == LEGACY_SCHEMA_VERSION:
        if manifest.get("schemaVersion") != LEGACY_SCHEMA_VERSION:
            raise IntegrityError(
                "current offline bundle cannot embed a legacy Catalog"
            )
        _validate_legacy_bundle_catalog(catalog, model)
    else:
        try:
            validate_catalog(catalog)
        except CatalogError as exc:
            raise IntegrityError("offline bundle Catalog is invalid") from exc
    return model


def _verify_runtime_image_identity(
    bundle_archive: tarfile.TarFile,
    bundle_manifest: dict[str, Any],
    identity: dict[str, Any],
) -> None:
    runtime_image = bundle_manifest.get("runtimeImage")
    try:
        repository, manifest_digest, _named = _pinned_image(runtime_image)
    except ValueError as exc:
        raise IntegrityError(str(exc)) from exc
    if (
        identity.get("schemaVersion") != 1
        or identity.get("composeReference") != runtime_image
        or identity.get("pinnedManifestDigest") != manifest_digest
    ):
        raise IntegrityError("runtime image identity does not match Compose reference")

    compose_handle = bundle_archive.extractfile("compose.yaml")
    if compose_handle is None:
        raise IntegrityError("bundled Compose configuration is unreadable")
    compose_body = compose_handle.read(MAX_IMAGE_MANIFEST_BYTES + 1)
    if len(compose_body) > MAX_IMAGE_MANIFEST_BYTES:
        raise IntegrityError("bundled Compose configuration is oversized")
    try:
        bundled_reference = _compose_image_text(compose_body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise IntegrityError("bundled Compose configuration is not UTF-8") from exc
    if bundled_reference != runtime_image:
        raise IntegrityError(
            "bundled Compose image does not match runtime image identity"
        )

    local = identity.get("localImage")
    if not isinstance(local, dict) or set(local) != {
        "repoDigest",
        "configDigest",
        "rootfsDiffIds",
    }:
        raise IntegrityError("runtime image local identity is invalid")
    repo_digest = local.get("repoDigest")
    expected_repo_digest = f"{repository}@{manifest_digest}"
    if repo_digest != expected_repo_digest:
        raise IntegrityError(
            "runtime image RepoDigest does not match the Compose pinned digest"
        )
    config_digest = local.get("configDigest")
    diff_ids = local.get("rootfsDiffIds")
    if (
        not isinstance(config_digest, str)
        or SHA256_DIGEST.fullmatch(config_digest) is None
        or not isinstance(diff_ids, list)
        or any(
            not isinstance(item, str) or SHA256_DIGEST.fullmatch(item) is None
            for item in diff_ids
        )
    ):
        raise IntegrityError("runtime image config or layer identity is invalid")

    image_handle = bundle_archive.extractfile("images/runtime-image.tar")
    if image_handle is None:
        raise IntegrityError("bundled runtime image archive is unreadable")
    computed_archive_identity = _archive_image_identity(
        image_handle,
        expected_config_digest=config_digest,
        expected_diff_ids=diff_ids,
    )
    if computed_archive_identity != identity.get("archive"):
        raise IntegrityError("runtime image archive identity record mismatch")


def verify(path: Path) -> dict[str, Any]:
    runtime_image_identity = None
    contains_image_archive = False
    try:
        with tarfile.open(path, "r") as archive:
            members = _safe_members(archive)
            manifest = _read_bundle_json(archive, "bundle-manifest.json")
            if (
                not isinstance(manifest, dict)
                or manifest.get("schemaVersion") not in READABLE_SCHEMA_VERSIONS
            ):
                raise IntegrityError("unsupported offline bundle schema")
            bundle_schema = manifest["schemaVersion"]
            declared = _validate_file_inventory(manifest.get("files"))
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

            _validate_bundle_semantics(archive, manifest, declared)

            contains_image_archive = "images/runtime-image.tar" in actual_names
            if (
                manifest.get("containsRuntimeImageArchive") is True
            ) != contains_image_archive:
                raise IntegrityError("runtime image archive declaration mismatch")
            if contains_image_archive:
                if bundle_schema == LEGACY_SCHEMA_VERSION:
                    raise IntegrityError(
                        "legacy schema-v1 runtime image bundle has no trusted image "
                        "identity binding; recreate it with the current bundle command"
                    )
                runtime_image_identity = manifest.get("runtimeImageIdentity")
                if not isinstance(runtime_image_identity, dict):
                    raise IntegrityError(
                        "runtime image archive has no bound image identity"
                    )
                _verify_runtime_image_identity(
                    archive,
                    manifest,
                    runtime_image_identity,
                )
            elif manifest.get("runtimeImageIdentity") is not None:
                raise IntegrityError(
                    "runtime image identity is present without an image archive"
                )
    except (OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"cannot verify offline bundle: {exc}") from exc
    result = {
        "modelId": manifest.get("modelId"),
        "files": len(declared),
        "containsModelArtifacts": manifest.get("containsModelArtifacts") is True,
        "hostAdmissionRequired": True,
    }
    if contains_image_archive:
        result["containsRuntimeImageArchive"] = True
        result["runtimeImageIdentity"] = runtime_image_identity
    return result


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ConfigError("secure bundle import requires O_NOFOLLOW support")
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | nofollow


def _open_private_model_directory(
    paths: ProjectPaths, model_directory: Any
) -> tuple[int, str]:
    component = (
        PurePosixPath(model_directory) if isinstance(model_directory, str) else None
    )
    if (
        component is None
        or len(component.parts) != 1
        or component.parts[0] != model_directory
        or model_directory in {".", ".."}
    ):
        raise IntegrityError("bundle model directory is unsafe")

    root = Path(os.path.abspath(os.fspath(paths.root)))
    root_descriptor: int | None = None
    models_descriptor: int | None = None
    model_descriptor: int | None = None
    flags = _directory_flags()
    try:
        root_descriptor = os.open(root.anchor, flags)
        for part in root.parts[1:]:
            child = os.open(part, flags, dir_fd=root_descriptor)
            os.close(root_descriptor)
            root_descriptor = child

        try:
            models_descriptor = os.open("models", flags, dir_fd=root_descriptor)
        except FileNotFoundError:
            os.mkdir("models", mode=0o700, dir_fd=root_descriptor)
            os.fsync(root_descriptor)
            models_descriptor = os.open("models", flags, dir_fd=root_descriptor)
        models_metadata = os.fstat(models_descriptor)
        if (
            not stat.S_ISDIR(models_metadata.st_mode)
            or models_metadata.st_uid != os.getuid()
            or stat.S_IMODE(models_metadata.st_mode) & 0o077
        ):
            raise IntegrityError(
                "models directory must be private and current-user-owned"
            )

        try:
            model_descriptor = os.open(
                model_directory, flags, dir_fd=models_descriptor
            )
        except FileNotFoundError:
            os.mkdir(model_directory, mode=0o700, dir_fd=models_descriptor)
            os.fsync(models_descriptor)
            model_descriptor = os.open(
                model_directory, flags, dir_fd=models_descriptor
            )
        model_metadata = os.fstat(model_descriptor)
        if (
            not stat.S_ISDIR(model_metadata.st_mode)
            or model_metadata.st_uid != os.getuid()
            or stat.S_IMODE(model_metadata.st_mode) & 0o077
        ):
            raise IntegrityError(
                "model import directory must be private and current-user-owned"
            )
        result = model_descriptor, f"models/{model_directory}"
        model_descriptor = None
        return result
    except (ConfigError, IntegrityError):
        raise
    except OSError as exc:
        raise IntegrityError(
            "model import directory is missing, unsafe, or contains a symbolic link"
        ) from exc
    finally:
        if model_descriptor is not None:
            os.close(model_descriptor)
        if models_descriptor is not None:
            os.close(models_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


def _existing_artifact_matches(
    directory_descriptor: int,
    filename: str,
    artifact: dict[str, Any],
) -> bool | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IntegrityError(f"unsafe existing model artifact: {filename}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise IntegrityError(f"unsafe existing model artifact: {filename}")
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: os.read(descriptor, 1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
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
        if identity_after != identity_before or size != before.st_size:
            raise IntegrityError(f"model artifact changed while inspected: {filename}")
        return size == artifact["bytes"] and digest.hexdigest() == artifact["sha256"]
    finally:
        os.close(descriptor)


def _write_imported_artifact(
    directory_descriptor: int,
    filename: str,
    artifact: dict[str, Any],
    source: BinaryIO,
) -> bool:
    existing_matches = _existing_artifact_matches(
        directory_descriptor, filename, artifact
    )
    if existing_matches is True:
        return False
    if existing_matches is False:
        raise IntegrityError(
            f"refusing to overwrite a different local artifact: {filename}"
        )
    filesystem = os.fstatvfs(directory_descriptor)
    if filesystem.f_bavail * filesystem.f_frsize < artifact["bytes"]:
        raise ConfigError(f"insufficient free disk to import {filename}")

    temporary_name: str | None = None
    temporary_descriptor: int | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _attempt in range(8):
            candidate = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}.part"
            try:
                temporary_descriptor = os.open(
                    candidate,
                    flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                temporary_name = candidate
                break
            except FileExistsError:
                continue
        if temporary_descriptor is None or temporary_name is None:
            raise IntegrityError(
                f"cannot allocate a private temporary artifact: {filename}"
            )

        digest = hashlib.sha256()
        size = 0
        output = os.fdopen(temporary_descriptor, "wb")
        temporary_descriptor = None
        with output:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size != artifact["bytes"] or digest.hexdigest() != artifact["sha256"]:
            raise IntegrityError(f"imported artifact identity mismatch: {filename}")
        try:
            os.stat(filename, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise IntegrityError(
                f"refusing to overwrite an artifact created during import: {filename}"
            )
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporary_name = None
        os.fsync(directory_descriptor)
        return True
    except (ConfigError, IntegrityError):
        raise
    except OSError as exc:
        raise IntegrityError(f"cannot safely import model artifact: {filename}") from exc
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


def import_artifacts(paths: ProjectPaths, bundle_path: Path) -> dict[str, Any]:
    verified = verify(bundle_path)
    imported: list[str] = []
    with tarfile.open(bundle_path, "r") as archive:
        _safe_members(archive)
        manifest = _read_bundle_json(archive, "bundle-manifest.json")
        if not isinstance(manifest, dict):
            raise IntegrityError("unsupported offline bundle schema")
        declared = _validate_file_inventory(manifest.get("files"))
        model = _validate_bundle_semantics(archive, manifest, declared)
        if model.get("id") != verified["modelId"]:
            raise IntegrityError("offline bundle identity changed before import")
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
        directory_descriptor: int | None = None
        try:
            for artifact in model["artifacts"]:
                relative = f"artifacts/{model['id']}/{artifact['filename']}"
                if relative not in manifest["files"]:
                    if manifest["containsModelArtifacts"] and artifact["required"]:
                        raise IntegrityError(
                            f"offline bundle is missing required model artifact: {relative}"
                        )
                    continue
                source = archive.extractfile(relative)
                if source is None:
                    raise IntegrityError(f"bundle artifact is unreadable: {relative}")
                if directory_descriptor is None:
                    directory_descriptor, destination_relative = (
                        _open_private_model_directory(
                            paths, model.get("modelDirectory")
                        )
                    )
                if _write_imported_artifact(
                    directory_descriptor,
                    artifact["filename"],
                    artifact,
                    source,
                ):
                    imported.append(
                        f"{destination_relative}/{artifact['filename']}"
                    )
        finally:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
    return {"imported": imported, "selected": False, "runtimeStarted": False, "hostAdmissionRequired": True}
