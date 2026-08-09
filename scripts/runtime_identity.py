#!/usr/bin/env python3
"""Canonical identities for rendered Compose configuration and live runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT_DIR / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from local_inference_stack.materials import (  # noqa: E402
    FILE_SET_SHA256_POLICY_ID,
    MaterialSet,
    SnapshotSpec,
    canonical_sha256,
    sha256_file,
)

try:
    from scripts.env_utils import is_private_regular_file, parse_env_file
except ModuleNotFoundError:  # Direct execution from scripts/.
    from env_utils import is_private_regular_file, parse_env_file


COMPOSE_PATH = ROOT_DIR / "compose.yaml"
LOCAL_PROFILE = ROOT_DIR / "profiles" / "deployment.local.env"
CATALOG_PATH = ROOT_DIR / "catalog" / "models.json"
MANIFEST_PATH = (
    ROOT_DIR / "deployments" / "qwen3.5-9b-rtx5070ti" / "manifest.json"
)
COMPOSE_VARIABLE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)")
ACCEPTANCE_MATERIAL_POLICY_ID = (
    "local-inference-stack/runtime-acceptance-materials-v1"
)
CONTROL_PLANE_MATERIALS = MaterialSet(
    key="controlPlanePackageSha256",
    policy_id=FILE_SET_SHA256_POLICY_ID,
    includes=("src/local_inference_stack/*.py",),
)


def performance_policy_sha256(model: dict[str, Any]) -> str:
    """Bind full acceptance to the stable performance gates for this model.

    The deployment manifest also contains validation results and repository
    hashes, which would make the input self-referential.  Only fields consumed
    by ``performance.load_policy`` are included here.
    """
    if model.get("id") != "qwen35-9b-q5km":
        return canonical_sha256(
            {
                "catalogModelId": model.get("id"),
                "performancePolicy": "unvalidated-catalog-profile",
            }
        )
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read performance policy manifest: {error}") from error
    if manifest.get("model", {}).get("catalogId") != model.get("id"):
        raise RuntimeError("performance policy manifest does not match the catalog model")
    performance = manifest.get("performance")
    if not isinstance(performance, dict):
        raise RuntimeError("performance policy manifest has no typed performance object")
    policy = {
        key: performance[key]
        for key in (
            "schemaVersion",
            "policy",
            "calibrationRuns",
            "hardGates",
            "warningGates",
        )
        if key in performance
    }
    return canonical_sha256(
        {"catalogModelId": model.get("id"), "performance": policy}
    )


def artifact_stat_fingerprint(metadata: os.stat_result) -> str:
    """Return the metadata fingerprint used by the integrity-stamp cache."""
    return (
        f"{metadata.st_dev}:{metadata.st_ino}:{metadata.st_size}:"
        f"{metadata.st_mtime_ns}:{metadata.st_ctime_ns}"
    )


def _absolute_project_path(path: Path, project_root: Path) -> Path:
    value = path if path.is_absolute() else project_root / path
    return Path(os.path.abspath(os.fspath(value)))


def _project_relative_path(path: Path, project_root: Path) -> tuple[Path, Path]:
    """Return lexical project paths after proving that the root has no symlink."""
    root = Path(os.path.abspath(os.fspath(project_root)))
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"cannot resolve the project root: {project_root}") from error
    if resolved_root != root:
        raise RuntimeError(f"project root contains a symbolic link: {project_root}")
    candidate = _absolute_project_path(path, root)
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"identity file is outside the project root: {path}") from error
    if not relative.parts:
        raise RuntimeError(f"identity file cannot be the project root: {path}")
    return root, relative


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("secure project-path inspection requires O_NOFOLLOW")
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | nofollow


def _open_absolute_directory(path: Path) -> int:
    """Open an absolute directory without following any component symlink."""
    if not path.is_absolute():
        raise RuntimeError(f"expected an absolute directory: {path}")
    descriptor = os.open(path.anchor, _directory_flags())
    try:
        for component in path.parts[1:]:
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_project_parent(
    path: Path,
    project_root: Path,
    *,
    create: bool = False,
) -> tuple[int, str, Path]:
    root, relative = _project_relative_path(path, project_root)
    descriptor = _open_absolute_directory(root)
    try:
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, relative.name, relative
    except Exception:
        os.close(descriptor)
        raise


def open_private_project_file(
    path: Path,
    *,
    project_root: Path = ROOT_DIR,
    maximum_bytes: int | None = None,
) -> tuple[int, os.stat_result, str]:
    """Open one private file beneath ``project_root`` using no-follow dirfds.

    The caller owns the returned descriptor. Both lexical and resolved containment
    are checked, every directory component is opened with ``O_NOFOLLOW``, and the
    final file must be a current-user-owned, single-link, private regular file.
    """
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent_descriptor, filename, relative = _open_project_parent(
            path, project_root
        )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(filename, flags, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or (maximum_bytes is not None and metadata.st_size > maximum_bytes)
        ):
            raise RuntimeError(
                f"identity file is not a private current-user regular file: {path}"
            )

        root, _ = _project_relative_path(path, project_root)
        try:
            resolved = _absolute_project_path(path, root).resolve(strict=True)
            resolved.relative_to(root.resolve(strict=True))
            resolved_metadata = resolved.stat()
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"identity file resolves outside the project root: {path}"
            ) from error
        if (resolved_metadata.st_dev, resolved_metadata.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise RuntimeError(f"identity file changed while it was opened: {path}")
        result = descriptor, metadata, relative.as_posix()
        descriptor = None
        return result
    except FileNotFoundError:
        raise
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(
            f"cannot safely open private identity file: {path}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def ensure_private_project_directory(
    path: Path,
    *,
    project_root: Path = ROOT_DIR,
) -> None:
    """Create/open a private project directory without following symlinks."""
    root, relative = _project_relative_path(path, project_root)
    descriptor: int | None = None
    try:
        descriptor = _open_absolute_directory(root)
        for component in relative.parts:
            try:
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RuntimeError(
                f"project directory is not private and current-user-owned: {path}"
            )
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"cannot safely open project directory: {path}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def atomic_write_private_project_text(
    path: Path,
    body: str,
    *,
    project_root: Path = ROOT_DIR,
) -> None:
    """Atomically write a private file through a no-follow project dirfd."""
    parent_descriptor: int | None = None
    temporary_name: str | None = None
    temporary_descriptor: int | None = None
    try:
        parent_descriptor, filename, _relative = _open_project_parent(
            path, project_root, create=True
        )
        parent_metadata = os.fstat(parent_descriptor)
        if (
            parent_metadata.st_uid != os.getuid()
            or stat.S_IMODE(parent_metadata.st_mode) & 0o077
        ):
            raise RuntimeError(
                f"identity output directory is not private and current-user-owned: {path.parent}"
            )
        try:
            existing = os.stat(filename, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.getuid()
            or existing.st_nlink != 1
            or stat.S_IMODE(existing.st_mode) & 0o077
        ):
            raise RuntimeError(f"unsafe existing identity file: {path}")

        payload = body.encode("utf-8")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _attempt in range(8):
            temporary_name = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            try:
                temporary_descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                break
            except FileExistsError:
                temporary_name = None
        if temporary_descriptor is None or temporary_name is None:
            raise RuntimeError(f"cannot allocate a private temporary identity file: {path}")
        os.fchmod(temporary_descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(temporary_descriptor, view)
            view = view[written:]
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None
        os.replace(
            temporary_name,
            filename,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_name = None
        os.fsync(parent_descriptor)
        verification_descriptor, _metadata, _relative = open_private_project_file(
            path, project_root=project_root
        )
        os.close(verification_descriptor)
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError(f"cannot safely write identity file: {path}") from error
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None and parent_descriptor is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def local_artifact_identity(
    model_path: Path,
    stamp_path: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
    project_root: Path = ROOT_DIR,
) -> dict[str, Any]:
    """Bind a verified artifact to secure local stat data and its tiny hash stamp.

    This deliberately never reads or hashes the model. A missing or stale stamp is
    a fail-closed signal that the caller must run the normal full-SHA256 verifier.
    """
    try:
        model_descriptor, model_stat, model_relative = open_private_project_file(
            model_path, project_root=project_root
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"artifact identity file is missing: {model_path}") from error
    try:
        if model_stat.st_size != expected_bytes:
            raise RuntimeError("artifact size changed; full SHA256 verification is required")
        fingerprint = artifact_stat_fingerprint(model_stat)
        expected_stamp = f"{expected_sha256}|{fingerprint}"
        try:
            stamp_descriptor, stamp_stat, stamp_relative = open_private_project_file(
                stamp_path,
                project_root=project_root,
                maximum_bytes=4096,
            )
        except FileNotFoundError as error:
            raise RuntimeError(f"integrity stamp is missing: {stamp_path}") from error
        try:
            with os.fdopen(os.dup(stamp_descriptor), "rb") as handle:
                stamp_body = handle.read(4097)
            if len(stamp_body) > 4096:
                raise RuntimeError("integrity stamp is oversized")
            if artifact_stat_fingerprint(os.fstat(stamp_descriptor)) != (
                artifact_stat_fingerprint(stamp_stat)
            ):
                raise RuntimeError(
                    "integrity stamp metadata changed while it was inspected"
                )
        finally:
            os.close(stamp_descriptor)
        try:
            stamp_value = stamp_body.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise RuntimeError("integrity stamp is not UTF-8") from error
        if stamp_value != expected_stamp:
            raise RuntimeError(
                "integrity stamp does not match the current artifact; "
                "full SHA256 verification is required"
            )
        final_model_stat = os.fstat(model_descriptor)
        if artifact_stat_fingerprint(final_model_stat) != fingerprint:
            raise RuntimeError("artifact metadata changed while its identity was inspected")
        return {
            "schemaVersion": 1,
            "verification": "secure-stat-and-integrity-stamp",
            "requiresFullSha256Verification": False,
            "path": model_relative,
            "fingerprint": fingerprint,
            "stat": {
                "device": model_stat.st_dev,
                "inode": model_stat.st_ino,
                "bytes": model_stat.st_size,
                "mtimeNs": model_stat.st_mtime_ns,
                "ctimeNs": model_stat.st_ctime_ns,
                "mode": f"{stat.S_IMODE(model_stat.st_mode):04o}",
                "ownerUid": model_stat.st_uid,
                "linkCount": model_stat.st_nlink,
            },
            "integrityStamp": {
                "path": stamp_relative,
                "contentSha256": hashlib.sha256(stamp_body).hexdigest(),
                "mode": f"{stat.S_IMODE(stamp_stat.st_mode):04o}",
                "ownerUid": stamp_stat.st_uid,
                "linkCount": stamp_stat.st_nlink,
            },
        }
    finally:
        os.close(model_descriptor)


def compose_variable_names() -> set[str]:
    return set(COMPOSE_VARIABLE.findall(COMPOSE_PATH.read_text(encoding="utf-8")))


def clean_compose_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in compose_variable_names():
        environment.pop(key, None)
    environment["MODELPORT_NETWORK_NAME"] = deployment_values().get(
        "MODELPORT_NETWORK_NAME", "modelport_default"
    )
    return environment


def deployment_values() -> dict[str, str]:
    if not LOCAL_PROFILE.exists():
        return {}
    if not is_private_regular_file(LOCAL_PROFILE):
        raise RuntimeError(
            "deployment.local.env must be a current-user-owned regular file "
            "with no group/other permissions"
        )
    return parse_env_file(LOCAL_PROFILE)


def compose_env_files(profile: str) -> list[Path]:
    profile_path = ROOT_DIR / "profiles" / f"{profile}.env"
    if not profile_path.is_file():
        raise ValueError(f"unknown runtime profile: {profile}")
    files: list[Path] = []
    if deployment_values():
        files.append(LOCAL_PROFILE)
    files.append(profile_path)
    return files


def rendered_compose(profile: str = "latency") -> dict[str, Any]:
    command = ["docker", "compose"]
    for path in compose_env_files(profile):
        command.extend(["--env-file", str(path)])
    command.extend(["config", "--format", "json"])
    result = subprocess.run(
        command,
        cwd=ROOT_DIR,
        env=clean_compose_environment(),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Docker Compose rendered a non-object configuration")
    return value


def rendered_compose_sha256(profile: str = "latency") -> str:
    return canonical_sha256(rendered_compose(profile))


def configured_runtime_image(profile: str = "latency") -> str | None:
    return rendered_compose(profile).get("services", {}).get("qwen35", {}).get("image")


ACCEPTANCE_COMMON_FILES = {
    "composeSha256": "compose.yaml",
    "contractSha256": "contracts/local-qwen-provider-v1.json",
    "catalogSha256": "catalog/models.json",
    "modelManagerSha256": "scripts/model-manager.py",
    "acceptanceSuiteSha256": "scripts/acceptance-suite.sh",
    "acceptanceEvidenceWriterSha256": "scripts/acceptance-evidence.py",
    "acceptancePolicySha256": "src/local_inference_stack/acceptance.py",
    "runtimeIdentitySha256": "scripts/runtime_identity.py",
    "localHttpSha256": "scripts/local_http.py",
    "environmentUtilsSha256": "scripts/env_utils.py",
    "deploymentLibrarySha256": "scripts/lib/deployment.sh",
    "runtimeControllerSha256": "scripts/runtime.sh",
    "runtimeSupervisorSha256": "scripts/runtime-supervisor.py",
    "pythonVersionSha256": ".python-version",
    "smokeTestSha256": "scripts/smoke-test.sh",
    "reasoningSmokeSha256": "scripts/reasoning-smoke.sh",
    "stackLauncherSha256": "stack",
    "runtimeProfilesConfigurationSha256": "config/runtime-profiles.json",
    "runtimeProfilesSchemaSha256": "config/schemas/runtime-profiles.schema.json",
}
ACCEPTANCE_STANDARD_FILES = {
    "compatibilityCheckSha256": "scripts/compatibility-check.py",
    "dashboardSmokeSha256": "scripts/dashboard-smoke.py",
    "operationsDashboardSha256": "scripts/operations-dashboard.py",
    "operationsReportSha256": "scripts/operations-report.py",
    "modelportSmokeSha256": "scripts/modelport-smoke.sh",
    "modelportReasoningSmokeSha256": "scripts/modelport-reasoning-smoke.py",
    "modelportReasoningSmokeWrapperSha256": "scripts/modelport-reasoning-smoke.sh",
    "modelportTokenCountSmokeSha256": "scripts/modelport-token-count-smoke.sh",
    "modelportContextAdmissionSha256": "scripts/modelport-context-admission-smoke.sh",
    "qualityEvalSha256": "scripts/quality-eval.py",
    "qualityCasesSha256": "quality/cases.json",
    "toolWorkflowEvalSha256": "scripts/tool-workflow-eval.py",
    "toolWorkflowCasesSha256": "quality/tool-workflows.json",
    "toolResilienceCasesSha256": "quality/tool-resilience-workflows.json",
}
ACCEPTANCE_FULL_FILES = {
    "contextAcceptanceSha256": "scripts/context-acceptance.py",
    "modelportContextAcceptanceSha256": "scripts/modelport-context-acceptance.sh",
    "performancePolicyEvaluatorSha256": "src/local_inference_stack/performance.py",
    "decodeBenchmarkSha256": "scripts/decode-benchmark.py",
    "concurrencyBenchmarkSha256": "scripts/concurrency-benchmark.py",
}


def acceptance_snapshot_spec(mode: str, profile: str) -> SnapshotSpec:
    """Declare the repository materials consumed by one acceptance mode."""

    if mode not in {"quick", "standard", "full"}:
        raise ValueError(f"unsupported acceptance mode: {mode}")
    files = dict(ACCEPTANCE_COMMON_FILES)
    if mode in {"standard", "full"}:
        files.update(ACCEPTANCE_STANDARD_FILES)
    if mode == "full":
        files.update(ACCEPTANCE_FULL_FILES)
    files["runtimeProfileSha256"] = f"profiles/{profile}.env"
    if LOCAL_PROFILE.is_file():
        files["deploymentProfileSha256"] = "profiles/deployment.local.env"
    return SnapshotSpec.from_mapping(
        policy_id=ACCEPTANCE_MATERIAL_POLICY_ID,
        files=files,
        material_sets=(CONTROL_PLANE_MATERIALS,),
    )


def acceptance_configuration(
    model: dict[str, Any],
    mode: str = "quick",
    profile: str = "latency",
) -> dict[str, str]:
    spec = acceptance_snapshot_spec(mode, profile)
    configuration = spec.snapshot(
        ROOT_DIR,
        expected_policy_id=ACCEPTANCE_MATERIAL_POLICY_ID,
        file_hasher=sha256_file,
    )
    configuration.update(
        {
            "materialPolicy": ACCEPTANCE_MATERIAL_POLICY_ID,
            "fileSetMaterialPolicy": FILE_SET_SHA256_POLICY_ID,
        }
    )
    if mode == "full":
        configuration["performancePolicySha256"] = performance_policy_sha256(model)
    configuration.update(
        {
            "runtimeProfile": profile,
            "deploymentProfileSha256": (
                configuration.get("deploymentProfileSha256", "absent")
            ),
            "effectiveComposeSha256": rendered_compose_sha256(profile),
            "manifestSha256": (
                sha256_file(MANIFEST_PATH)
                if model["id"] == "qwen35-9b-q5km"
                else "unvalidated-catalog-profile"
            ),
        }
    )
    return configuration


def command_json(command: list[str]) -> Any | None:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(result.stdout)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None


def live_container(name: str) -> dict[str, Any] | None:
    inspected = command_json(["docker", "inspect", name])
    if isinstance(inspected, list) and inspected and isinstance(inspected[0], dict):
        return inspected[0]
    return None


def _normalized_port_bindings(value: Any) -> dict[str, list[dict[str, str]]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(port): sorted(
            (
                {
                    "HostIp": str(binding.get("HostIp") or ""),
                    "HostPort": str(binding.get("HostPort") or ""),
                }
                for binding in (bindings or [])
                if isinstance(binding, dict)
            ),
            key=lambda binding: (binding["HostIp"], binding["HostPort"]),
        )
        for port, bindings in sorted(value.items())
    }


def _normalized_device_requests(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized = []
    for request in value:
        if not isinstance(request, dict):
            continue
        normalized.append(
            {
                "driver": str(request.get("Driver") or request.get("driver") or ""),
                "count": int(request.get("Count", request.get("count", 0))),
                "deviceIds": sorted(
                    str(item)
                    for item in (request.get("DeviceIDs", request.get("device_ids")) or [])
                ),
                "capabilities": sorted(
                    sorted(str(item) for item in group)
                    for group in (request.get("Capabilities", request.get("capabilities")) or [])
                    if isinstance(group, list)
                ),
                "options": {
                    str(key): str(option)
                    for key, option in sorted(
                        (request.get("Options", request.get("options")) or {}).items()
                    )
                },
            }
        )
    return sorted(
        normalized,
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )


def _normalized_tmpfs(value: Any) -> dict[str, list[str]]:
    if isinstance(value, dict):
        return {
            str(path): sorted(filter(None, str(options or "").split(",")))
            for path, options in sorted(value.items())
        }
    result: dict[str, list[str]] = {}
    for item in value or []:
        path, separator, options = str(item).partition(":")
        result[path] = sorted(filter(None, options.split(","))) if separator else []
    return dict(sorted(result.items()))


def _normalized_networks(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(name): sorted(
            str(alias)
            for alias in ((settings or {}).get("Aliases") or [])
            if alias
        )
        for name, settings in sorted(value.items())
        if isinstance(settings, dict)
    }


def _environment_identity(value: Any) -> tuple[list[str], str]:
    entries = sorted(str(item) for item in (value or []))
    keys = sorted({item.partition("=")[0] for item in entries})
    return keys, canonical_sha256(entries)


def _environment_entries(value: Any) -> dict[str, str]:
    entries: dict[str, str] = {}
    for item in value or []:
        key, separator, content = str(item).partition("=")
        if separator:
            entries[key] = content
    return entries


def _normalized_devices(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return sorted(
        (
            {
                "pathOnHost": str(device.get("PathOnHost") or device.get("path") or ""),
                "pathInContainer": str(
                    device.get("PathInContainer")
                    or device.get("path_in_container")
                    or device.get("PathOnHost")
                    or device.get("path")
                    or ""
                ),
                "cgroupPermissions": str(
                    device.get("CgroupPermissions")
                    or device.get("cgroup_permissions")
                    or "rwm"
                ),
            }
            for device in value
            if isinstance(device, dict)
        ),
        key=lambda item: (
            item["pathInContainer"],
            item["pathOnHost"],
            item["cgroupPermissions"],
        ),
    )


def _image_runtime_defaults(image: str) -> tuple[str, dict[str, Any]]:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    inspected = json.loads(result.stdout)
    if not isinstance(inspected, list) or not inspected or not isinstance(inspected[0], dict):
        raise RuntimeError("Docker returned invalid image identity")
    document = inspected[0]
    config = document.get("Config") or {}
    if not isinstance(config, dict) or not isinstance(document.get("Id"), str):
        raise RuntimeError("Docker image identity is incomplete")
    return document["Id"], config


def normalized_live_runtime(container: dict[str, Any]) -> dict[str, Any]:
    config = container.get("Config") or {}
    host = container.get("HostConfig") or {}
    mounts = sorted(
        (
            {
                "type": mount.get("Type"),
                "source": mount.get("Source"),
                "destination": mount.get("Destination"),
                "rw": mount.get("RW"),
                "mode": mount.get("Mode"),
                "propagation": mount.get("Propagation"),
            }
            for mount in (container.get("Mounts") or [])
            if isinstance(mount, dict)
        ),
        key=lambda item: (
            str(item["destination"]),
            str(item["source"]),
            str(item["type"]),
        ),
    )
    restart = host.get("RestartPolicy") or {}
    environment_keys, environment_sha256 = _environment_identity(config.get("Env"))
    return {
        "image": config.get("Image"),
        "imageId": container.get("Image"),
        "user": config.get("User"),
        "entrypoint": config.get("Entrypoint") or [],
        "command": config.get("Cmd") or [],
        "workingDirectory": str(config.get("WorkingDir") or ""),
        "healthcheck": config.get("Healthcheck") or {},
        "environmentKeys": environment_keys,
        "environmentSha256": environment_sha256,
        "privileged": host.get("Privileged") is True,
        "readOnly": host.get("ReadonlyRootfs") is True,
        "capAdd": sorted(host.get("CapAdd") or []),
        "capDrop": sorted(host.get("CapDrop") or []),
        "securityOpt": sorted(host.get("SecurityOpt") or []),
        "pidsLimit": host.get("PidsLimit"),
        "init": host.get("Init") is True,
        "shmSize": host.get("ShmSize"),
        "tmpfs": _normalized_tmpfs(host.get("Tmpfs") or {}),
        "restart": {
            "name": str(restart.get("Name") or "no"),
            "maximumRetryCount": int(restart.get("MaximumRetryCount") or 0),
        },
        "logging": host.get("LogConfig") or {},
        "publishAllPorts": host.get("PublishAllPorts") is True,
        "portBindings": _normalized_port_bindings(host.get("PortBindings") or {}),
        "networkMode": str(host.get("NetworkMode") or ""),
        "pidMode": str(host.get("PidMode") or ""),
        "ipcMode": str(host.get("IpcMode") or ""),
        "utsMode": str(host.get("UTSMode") or ""),
        "usernsMode": str(host.get("UsernsMode") or ""),
        "cgroupnsMode": str(host.get("CgroupnsMode") or ""),
        "networks": _normalized_networks(
            (container.get("NetworkSettings") or {}).get("Networks") or {}
        ),
        "mounts": mounts,
        "binds": sorted(str(item) for item in (host.get("Binds") or [])),
        "devices": _normalized_devices(host.get("Devices") or []),
        "deviceRequests": _normalized_device_requests(host.get("DeviceRequests") or []),
        "groupAdd": sorted(str(item) for item in (host.get("GroupAdd") or [])),
        "extraHosts": sorted(str(item) for item in (host.get("ExtraHosts") or [])),
        "links": sorted(str(item) for item in (host.get("Links") or [])),
        "dns": sorted(str(item) for item in (host.get("Dns") or [])),
        "dnsOptions": sorted(str(item) for item in (host.get("DnsOptions") or [])),
        "dnsSearch": sorted(str(item) for item in (host.get("DnsSearch") or [])),
        "sysctls": {
            str(key): str(value)
            for key, value in sorted((host.get("Sysctls") or {}).items())
        },
    }


def live_runtime_sha256(container: dict[str, Any]) -> str:
    return canonical_sha256(normalized_live_runtime(container))


def expected_runtime_subset(profile: str = "latency") -> dict[str, Any]:
    compose = rendered_compose(profile)
    service_name = "qwen35"
    service = compose["services"][service_name]
    image_id, image_config = _image_runtime_defaults(str(service.get("image") or ""))
    ports = service.get("ports") or []
    volumes = sorted(
        (
            {
                "type": volume.get("type"),
                "source": str(Path(volume.get("source", "")).resolve()),
                "destination": volume.get("target"),
                "rw": not bool(volume.get("read_only")),
                "mode": "ro" if volume.get("read_only") else "rw",
                "propagation": str(
                    (volume.get("bind") or {}).get("propagation") or "rprivate"
                ),
            }
            for volume in service.get("volumes", [])
        ),
        key=lambda item: str(item["destination"]),
    )
    port_bindings: dict[str, list[dict[str, str]]] = {}
    for port in ports:
        target = f"{port['target']}/{port.get('protocol', 'tcp')}"
        port_bindings[target] = [
            {
                "HostIp": str(port.get("host_ip") or ""),
                "HostPort": str(port.get("published") or ""),
            }
        ]
    logging = service.get("logging") or {}
    service_networks = service.get("networks") or {"default": {}}
    compose_networks = compose.get("networks") or {}
    container_name = str(service.get("container_name") or service_name)
    networks: dict[str, list[str]] = {}
    for logical_name, settings in service_networks.items():
        network = compose_networks.get(logical_name) or {}
        physical_name = str(
            network.get("name")
            or f"{compose.get('name', 'default')}_{logical_name}"
        )
        aliases = {service_name, container_name}
        if isinstance(settings, dict):
            aliases.update(str(alias) for alias in (settings.get("aliases") or []))
        networks[physical_name] = sorted(aliases)
    default_network = compose_networks.get("default") or {}
    network_mode = str(
        default_network.get("name")
        or next(iter(networks), "")
    )
    expected_device_requests = []
    for request in service.get("gpus") or []:
        if not isinstance(request, dict):
            continue
        expected_device_requests.append(
            {
                "Driver": request.get("driver", ""),
                "Count": request.get("count", 0),
                "DeviceIDs": request.get("device_ids") or [],
                "Capabilities": request.get("capabilities") or [["gpu"]],
                "Options": request.get("options") or {},
            }
        )
    restart_name = str(service.get("restart") or "no")
    environment = _environment_entries(image_config.get("Env"))
    environment.update(
        {str(key): str(value) for key, value in (service.get("environment") or {}).items()}
    )
    environment_entries = [f"{key}={value}" for key, value in environment.items()]
    environment_keys, environment_sha256 = _environment_identity(environment_entries)
    entrypoint = service.get("entrypoint")
    if entrypoint is None:
        entrypoint = image_config.get("Entrypoint") or []
    working_directory = service.get("working_dir")
    if working_directory is None:
        working_directory = image_config.get("WorkingDir") or ""
    healthcheck = service.get("healthcheck")
    if healthcheck is None:
        healthcheck = image_config.get("Healthcheck") or {}
    binds = sorted(
        f"{volume['source']}:{volume['destination']}:{'rw' if volume['rw'] else 'ro'}"
        for volume in volumes
    )
    return {
        "image": service.get("image"),
        "imageId": image_id,
        "user": service.get("user"),
        "entrypoint": entrypoint,
        "command": service.get("command") or [],
        "workingDirectory": str(working_directory),
        "healthcheck": healthcheck,
        "environmentKeys": environment_keys,
        "environmentSha256": environment_sha256,
        "privileged": service.get("privileged") is True,
        "readOnly": service.get("read_only") is True,
        "capAdd": sorted(service.get("cap_add") or []),
        "capDrop": sorted(service.get("cap_drop") or []),
        "securityOpt": sorted(service.get("security_opt") or []),
        "pidsLimit": service.get("pids_limit"),
        "init": service.get("init") is True,
        "shmSize": int(service.get("shm_size") or 0),
        "tmpfs": _normalized_tmpfs(service.get("tmpfs") or []),
        "restart": {"name": restart_name, "maximumRetryCount": 0},
        "logging": {
            "Type": logging.get("driver"),
            "Config": {key: str(value) for key, value in (logging.get("options") or {}).items()},
        },
        "publishAllPorts": False,
        "portBindings": port_bindings,
        "networkMode": network_mode,
        "pidMode": str(service.get("pid") or ""),
        "ipcMode": str(service.get("ipc") or "private"),
        "utsMode": str(service.get("uts") or ""),
        "usernsMode": str(service.get("userns_mode") or ""),
        "cgroupnsMode": str(service.get("cgroup") or "private"),
        "networks": dict(sorted(networks.items())),
        "mounts": volumes,
        "binds": binds,
        "devices": _normalized_devices(service.get("devices") or []),
        "deviceRequests": _normalized_device_requests(expected_device_requests),
        "groupAdd": sorted(str(item) for item in (service.get("group_add") or [])),
        "extraHosts": sorted(str(item) for item in (service.get("extra_hosts") or [])),
        "links": sorted(str(item) for item in (service.get("links") or [])),
        "dns": sorted(str(item) for item in (service.get("dns") or [])),
        "dnsOptions": sorted(str(item) for item in (service.get("dns_opt") or [])),
        "dnsSearch": sorted(str(item) for item in (service.get("dns_search") or [])),
        "sysctls": {
            str(key): str(value)
            for key, value in sorted((service.get("sysctls") or {}).items())
        },
    }


def runtime_mismatches(container: dict[str, Any], profile: str = "latency") -> list[str]:
    expected = expected_runtime_subset(profile)
    actual = normalized_live_runtime(container)
    checks = {
        "image": actual["image"],
        "imageId": actual["imageId"],
        "user": actual["user"],
        "entrypoint": actual["entrypoint"],
        "command": actual["command"],
        "workingDirectory": actual["workingDirectory"],
        "healthcheck": actual["healthcheck"],
        "environmentKeys": actual["environmentKeys"],
        "environmentSha256": actual["environmentSha256"],
        "privileged": actual["privileged"],
        "readOnly": actual["readOnly"],
        "capAdd": actual["capAdd"],
        "capDrop": actual["capDrop"],
        "securityOpt": actual["securityOpt"],
        "pidsLimit": actual["pidsLimit"],
        "init": actual["init"],
        "shmSize": actual["shmSize"],
        "tmpfs": actual["tmpfs"],
        "restart": actual["restart"],
        "logging": actual["logging"],
        "publishAllPorts": actual["publishAllPorts"],
        "portBindings": actual["portBindings"],
        "networkMode": actual["networkMode"],
        "pidMode": actual["pidMode"],
        "ipcMode": actual["ipcMode"],
        "utsMode": actual["utsMode"],
        "usernsMode": actual["usernsMode"],
        "cgroupnsMode": actual["cgroupnsMode"],
        "networks": actual["networks"],
        "mounts": actual["mounts"],
        "binds": actual["binds"],
        "devices": actual["devices"],
        "deviceRequests": actual["deviceRequests"],
        "groupAdd": actual["groupAdd"],
        "extraHosts": actual["extraHosts"],
        "links": actual["links"],
        "dns": actual["dns"],
        "dnsOptions": actual["dnsOptions"],
        "dnsSearch": actual["dnsSearch"],
        "sysctls": actual["sysctls"],
    }
    return [key for key, value in checks.items() if value != expected[key]]
