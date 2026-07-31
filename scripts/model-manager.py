#!/usr/bin/env python3
"""Assess a host and safely materialize a catalog-backed local deployment."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from scripts.env_utils import atomic_write_private_text
    from scripts.runtime_identity import (
        acceptance_configuration,
        configured_runtime_image,
        live_container,
        live_runtime_sha256,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from env_utils import atomic_write_private_text
    from runtime_identity import (
        acceptance_configuration,
        configured_runtime_image,
        live_container,
        live_runtime_sha256,
    )


ROOT_DIR = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT_DIR / "catalog" / "models.json"
LOCAL_PROFILE = ROOT_DIR / "profiles" / "deployment.local.env"
MODELS_DIR = ROOT_DIR / "models"
INTEGRITY_DIR = ROOT_DIR / "cache" / "integrity"
ACQUISITION_DIR = ROOT_DIR / "cache" / "acquisitions"
ACCEPTANCE_DIR = ROOT_DIR / "logs" / "acceptance"
ACCEPTANCE_SCHEMA_VERSION = 4
ACCEPTANCE_MAX_AGE = timedelta(days=30)
ACCEPTANCE_FUTURE_SKEW = timedelta(seconds=300)
HOST_FINGERPRINT_TYPE = "machine-id-sha256-v1"
HOST_FINGERPRINT_CONTEXT = b"local-inference-stack.acceptance-host.v1\0"
MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))


def load_catalog() -> dict[str, Any]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if catalog.get("schemaVersion") != 1 or not catalog.get("models"):
        raise SystemExit(f"unsupported or empty catalog: {CATALOG_PATH}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", catalog.get("updatedAt", "")):
        raise SystemExit("catalog updatedAt must be an ISO calendar date")
    policy = catalog.get("artifactPolicy", {})
    if policy.get("licenseReviewRequired") is not True:
        raise SystemExit("catalog must require an explicit third-party license review")
    ids: set[str] = set()
    for model in catalog["models"]:
        model_id = model.get("id", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]+", model_id) or model_id in ids:
            raise SystemExit(f"invalid or duplicate model id in catalog: {model_id!r}")
        ids.add(model_id)
        for text_field in (
            "displayName",
            "quantization",
            "purpose",
            "servedModelId",
        ):
            if not isinstance(model.get(text_field), str) or not model[text_field].strip():
                raise SystemExit(f"invalid {text_field} for {model_id}")
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]+", model.get("servedModelId", "")
        ):
            raise SystemExit(f"unsafe servedModelId for {model_id}")
        directory = Path(model.get("modelDirectory", ""))
        if directory.is_absolute() or ".." in directory.parts or len(directory.parts) != 1:
            raise SystemExit(f"unsafe model directory for {model_id}: {directory}")
        if model.get("status") not in {"estimated", "validated"}:
            raise SystemExit(f"invalid evidence status for {model_id}: {model.get('status')!r}")
        if model.get("status") == "validated":
            signature = model.get("validatedHardware", {})
            if (
                not isinstance(model.get("validation"), str)
                or not model["validation"].strip()
                or not isinstance(signature.get("gpuName"), str)
                or not signature["gpuName"].strip()
                or not isinstance(signature.get("gpuCount"), int)
                or isinstance(signature.get("gpuCount"), bool)
                or signature["gpuCount"] != 1
                or not isinstance(signature.get("minVramGiB"), (int, float))
                or isinstance(signature.get("minVramGiB"), bool)
                or not math.isfinite(signature["minVramGiB"])
                or signature["minVramGiB"] <= 0
                or not isinstance(signature.get("minRamGiB"), (int, float))
                or isinstance(signature.get("minRamGiB"), bool)
                or not math.isfinite(signature["minRamGiB"])
                or signature["minRamGiB"] <= 0
            ):
                raise SystemExit(f"incomplete validated hardware metadata for {model_id}")
        for revision_field in ("modelRevision", "artifactRevision"):
            if not re.fullmatch(r"[0-9a-f]{40}", model.get(revision_field, "")):
                raise SystemExit(f"invalid {revision_field} for {model_id}")
        for repository_field in ("modelRepository", "artifactRepository"):
            if not re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                model.get(repository_field, ""),
            ):
                raise SystemExit(f"invalid {repository_field} for {model_id}")
        license_metadata = model.get("license", {})
        expected_license_source = (
            f"https://huggingface.co/{model.get('modelRepository', '')}/blob/"
            f"{model.get('modelRevision', '')}/LICENSE"
        )
        if (
            not re.fullmatch(r"[A-Za-z0-9.+-]+", license_metadata.get("spdx", ""))
            or license_metadata.get("source") != expected_license_source
            or license_metadata.get("reviewRequired") is not True
            or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", license_metadata.get("metadataVerifiedAt", "")
            )
        ):
            raise SystemExit(f"incomplete license metadata for {model_id}")
        requirements = model.get("requirements", {})
        required_capacity = (
            "minVramGiB",
            "recommendedVramGiB",
            "minRamGiB",
            "minFreeDiskGiB",
        )
        if any(
            not isinstance(requirements.get(field), (int, float))
            or isinstance(requirements.get(field), bool)
            or not math.isfinite(requirements[field])
            or requirements[field] <= 0
            for field in required_capacity
        ):
            raise SystemExit(f"invalid hardware requirements for {model_id}")
        if requirements["recommendedVramGiB"] < requirements["minVramGiB"]:
            raise SystemExit(f"recommended VRAM is below minimum for {model_id}")
        runtime = model.get("runtime", {})
        runtime_fields = (
            "contextTokens",
            "recommendedInputTokens",
            "maxOutputTokens",
            "cacheRamMiB",
            "batchSize",
            "ubatchSize",
        )
        if any(
            not isinstance(runtime.get(field), int)
            or isinstance(runtime.get(field), bool)
            or runtime[field] <= 0
            for field in runtime_fields
        ):
            raise SystemExit(f"invalid runtime capacity for {model_id}")
        if (
            runtime["recommendedInputTokens"] + runtime["maxOutputTokens"]
            >= runtime["contextTokens"]
        ):
            raise SystemExit(f"runtime token budget has no safety margin for {model_id}")
        primaries = [item for item in model.get("artifacts", []) if item.get("role") == "model"]
        if len(primaries) != 1:
            raise SystemExit(f"{model_id} must define exactly one primary model artifact")
        filenames: set[str] = set()
        for artifact in model["artifacts"]:
            filename = Path(artifact.get("filename", ""))
            url = urlparse(artifact.get("url", ""))
            if filename.is_absolute() or len(filename.parts) != 1 or filename.name != str(filename):
                raise SystemExit(f"unsafe artifact filename for {model_id}: {filename}")
            if url.scheme != "https" or url.hostname != "huggingface.co":
                raise SystemExit(f"unapproved artifact URL for {model_id}: {url.geturl()}")
            if url.query != "download=true" or url.fragment:
                raise SystemExit(
                    f"artifact URL must use only the reviewed download query for {model_id}"
                )
            if str(filename) in filenames:
                raise SystemExit(f"duplicate artifact filename for {model_id}: {filename}")
            filenames.add(str(filename))
            if not isinstance(artifact.get("required"), bool):
                raise SystemExit(f"artifact required flag must be boolean for {model_id}")
            if not isinstance(artifact.get("role"), str) or not artifact["role"].strip():
                raise SystemExit(f"invalid artifact role for {model_id}")
            expected_path = (
                f"/{model['artifactRepository']}/resolve/"
                f"{model['artifactRevision']}/{filename}"
            )
            if url.path != expected_path:
                raise SystemExit(
                    f"artifact URL is not pinned to the reviewed revision for "
                    f"{model_id}/{filename}"
                )
            if not re.fullmatch(r"[0-9a-f]{64}", artifact.get("sha256", "")):
                raise SystemExit(f"invalid SHA256 for {model_id}/{filename}")
            if not isinstance(artifact.get("bytes"), int) or artifact["bytes"] <= 0:
                raise SystemExit(f"invalid artifact size for {model_id}/{filename}")
        required_bytes = sum(
            artifact["bytes"] for artifact in model["artifacts"] if artifact["required"]
        )
        if requirements["minFreeDiskGiB"] * 1024**3 < required_bytes:
            raise SystemExit(f"minimum free disk is below required artifacts for {model_id}")
    if catalog.get("defaultModel") not in ids:
        raise SystemExit("catalog defaultModel does not reference a reviewed entry")
    return catalog


def model_by_id(catalog: dict[str, Any], model_id: str) -> dict[str, Any]:
    for model in catalog["models"]:
        if model["id"] == model_id:
            return model
    choices = ", ".join(model["id"] for model in catalog["models"])
    raise SystemExit(f"unknown model {model_id!r}; catalog choices: {choices}")


def command_output(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=15
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip()


def memory_inventory() -> tuple[float, float]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return (
        values.get("MemTotal", 0) / 1024 / 1024,
        values.get("MemAvailable", 0) / 1024 / 1024,
    )


def total_ram_gib() -> float:
    return round(memory_inventory()[0], 1)


def gpu_inventory() -> list[dict[str, Any]]:
    output = command_output(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.free,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if not output:
        return []
    gpus: list[dict[str, Any]] = []
    for line in output.splitlines():
        fields = [field.strip() for field in line.split(",", 4)]
        if len(fields) != 5:
            continue
        try:
            memory_gib = round(float(fields[2]) / 1024, 1)
            free_memory_gib = round(float(fields[3]) / 1024, 1)
        except ValueError:
            continue
        gpus.append(
            {
                "index": int(fields[0]),
                "name": fields[1],
                "vramGiB": memory_gib,
                "freeVramGiB": free_memory_gib,
                "driver": fields[4],
            }
        )
    return gpus


def host_assessment(vram_override: float | None, ram_override: float | None) -> dict[str, Any]:
    gpus = gpu_inventory()
    if vram_override is not None:
        gpus = [
            {
                "index": 0,
                "name": "override",
                "vramGiB": vram_override,
                "freeVramGiB": vram_override,
                "driver": "override",
            }
        ]
    detected_ram_gib, available_ram_gib = memory_inventory()
    ram_gib = ram_override if ram_override is not None else detected_ram_gib
    if ram_override is not None:
        available_ram_gib = ram_override
    disk = shutil.disk_usage(ROOT_DIR)
    docker_version = command_output(["docker", "version", "--format", "{{.Server.Version}}"])
    compose_version = command_output(["docker", "compose", "version", "--short"])
    runtimes = command_output(["docker", "info", "--format", "{{json .Runtimes}}"])
    curl_version = command_output(["curl", "--version"])
    compose_compatible = command_output(
        [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT_DIR),
            "--env-file",
            str(ROOT_DIR / "profiles" / "latency.env"),
            "-f",
            str(ROOT_DIR / "compose.yaml"),
            "config",
            "--quiet",
        ]
    )
    return {
        "platform": sys.platform,
        "architecture": os.uname().machine,
        "gpus": gpus,
        "totalVramGiB": round(sum(gpu["vramGiB"] for gpu in gpus), 1),
        "largestGpuVramGiB": max((gpu["vramGiB"] for gpu in gpus), default=0),
        "totalFreeVramGiB": round(
            sum(gpu.get("freeVramGiB", gpu["vramGiB"]) for gpu in gpus), 1
        ),
        "largestFreeVramGiB": max(
            (gpu.get("freeVramGiB", gpu["vramGiB"]) for gpu in gpus), default=0
        ),
        "ramGiB": round(ram_gib, 1),
        "availableRamGiB": round(available_ram_gib, 1),
        "freeDiskGiB": round(disk.free / 1024**3, 1),
        "docker": {"available": docker_version is not None, "version": docker_version},
        "dockerCompose": {
            "available": compose_version is not None,
            "version": compose_version,
            "configurationCompatible": compose_compatible is not None,
        },
        "nvidiaContainerRuntime": bool(runtimes and '"nvidia"' in runtimes),
        "curl": {
            "available": curl_version is not None,
            "version": curl_version.splitlines()[0] if curl_version else None,
        },
        "python": {
            "version": sys.version.split()[0],
            "supported": sys.version_info >= (3, 11),
        },
        "commands": {"flock": shutil.which("flock") is not None},
        "user": {"uid": os.getuid(), "gid": os.getgid()},
    }


def fits(model: dict[str, Any], host: dict[str, Any]) -> bool:
    requirements = model["requirements"]
    return (
        host.get("platform", "linux") == "linux"
        and host.get("architecture", "x86_64") in {"x86_64", "amd64"}
        and host["largestGpuVramGiB"] >= requirements["minVramGiB"]
        and host["ramGiB"] >= requirements["minRamGiB"]
        and host["freeDiskGiB"] >= requirements["minFreeDiskGiB"]
    )


def resources_available_now(model: dict[str, Any], host: dict[str, Any]) -> bool:
    return (
        fits(model, host)
        and host.get("largestFreeVramGiB", host["largestGpuVramGiB"])
        >= model["requirements"]["minVramGiB"]
        and host.get("availableRamGiB", host["ramGiB"])
        >= model["requirements"]["minRamGiB"]
    )


def automatic_deployment_supported(host: dict[str, Any]) -> bool:
    return len(host["gpus"]) == 1


def recommend(catalog: dict[str, Any], host: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [model for model in catalog["models"] if fits(model, host)]
    if not candidates:
        return None
    return max(candidates, key=lambda model: model["requirements"]["minVramGiB"])


def matches_validated_hardware_profile(
    model: dict[str, Any] | None, host: dict[str, Any]
) -> bool:
    if not model or model.get("status") != "validated" or "validatedHardware" not in model:
        return False
    signature = model["validatedHardware"]
    return (
        len(host["gpus"]) == signature["gpuCount"]
        and all(gpu["name"] == signature["gpuName"] for gpu in host["gpus"])
        and host["largestGpuVramGiB"] >= signature["minVramGiB"]
        and host["ramGiB"] >= signature["minRamGiB"]
    )


def caveats(
    host: dict[str, Any],
    model: dict[str, Any] | None,
    host_acceptance: dict[str, Any] | None = None,
) -> list[str]:
    notes: list[str] = []
    if not host["gpus"]:
        notes.append("No NVIDIA GPU was detected; automatic deployment is intentionally disabled.")
    if len(host["gpus"]) > 1:
        notes.append(
            "Multi-GPU automatic deployment is disabled; design and validate a reviewed "
            "tensor-split profile before changing runtime state."
        )
    if not host["docker"]["available"]:
        notes.append("Docker Engine is unavailable.")
    if not host["dockerCompose"]["available"]:
        notes.append("Docker Compose v2 is unavailable.")
    elif not host["dockerCompose"].get("configurationCompatible", False):
        notes.append("Docker Compose cannot render the required runtime configuration.")
    if not host.get("nvidiaContainerRuntime", False):
        notes.append("Docker does not report an NVIDIA container runtime.")
    if not host.get("curl", {}).get("available", False):
        notes.append("curl is unavailable; verified HTTPS downloads cannot run.")
    if not host.get("python", {}).get("supported", False):
        notes.append("Python 3.11 or newer is required.")
    if not host.get("commands", {}).get("flock", False):
        notes.append("util-linux flock is unavailable; runtime mutations cannot be serialized.")
    if host.get("platform") != "linux" or host.get("architecture") not in {"x86_64", "amd64"}:
        notes.append("Automatic deployment currently supports Linux/WSL x86_64 only.")
    if (
        model
        and matches_validated_hardware_profile(model, host)
        and not host_acceptance
    ):
        notes.append(
            "This hardware matches a recorded validated profile, but a read-only plan "
            "does not prove that this host has passed acceptance."
        )
    elif model and not matches_validated_hardware_profile(model, host):
        notes.append(
            "This hardware does not match a recorded validation profile; treat the "
            "candidate as estimated until host acceptance passes."
        )
    if model and host["largestGpuVramGiB"] < model["requirements"]["minVramGiB"]:
        notes.append(
            "No single GPU meets the minimum; automatic deployment will not aggregate "
            "VRAM across GPUs."
        )
    if model and fits(model, host) and not resources_available_now(model, host):
        if (
            host.get("largestFreeVramGiB", 0)
            < model["requirements"]["minVramGiB"]
        ):
            notes.append(
                f"Largest free VRAM ({host.get('largestFreeVramGiB', 0):.1f} GiB) is below "
                f"the {model['requirements']['minVramGiB']} GiB deployment threshold; "
                "stop or review existing GPU workloads before deployment."
            )
        if host.get("availableRamGiB", 0) < model["requirements"]["minRamGiB"]:
            notes.append(
                f"Available RAM ({host.get('availableRamGiB', 0):.1f} GiB) is below "
                f"the {model['requirements']['minRamGiB']} GiB deployment threshold."
            )
    return notes


def payload_sha256(payload: dict[str, Any]) -> str:
    unsigned = {
        key: value for key, value in payload.items() if key != "selfSha256"
    }
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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


def parse_evidence_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def acceptance_matches_host(
    model: dict[str, Any],
    host: dict[str, Any],
    evidence: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    if (
        evidence.get("schemaVersion") != ACCEPTANCE_SCHEMA_VERSION
        or evidence.get("status") != "passed"
        or evidence.get("exitCode") != 0
        or evidence.get("failedAtStep") is not None
        or evidence.get("mode") not in {"quick", "standard", "full"}
        or evidence.get("profile") != "latency"
        or evidence.get("catalogModelId") != model["id"]
        or evidence.get("selfSha256") != payload_sha256(evidence)
    ):
        return False
    terminal_step = evidence.get("terminalStep")
    duration = evidence.get("durationSeconds")
    if (
        not isinstance(terminal_step, str)
        or not terminal_step
        or not isinstance(duration, int)
        or isinstance(duration, bool)
        or duration < 0
    ):
        return False
    started_at = parse_evidence_time(evidence.get("startedAt"))
    finished_at = parse_evidence_time(evidence.get("finishedAt"))
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (
        not started_at
        or not finished_at
        or started_at > finished_at
        or abs((finished_at - started_at).total_seconds() - duration) > 5
        or finished_at > current_time + ACCEPTANCE_FUTURE_SKEW
        or current_time - finished_at > ACCEPTANCE_MAX_AGE
        or evidence.get("freshnessPolicy")
        != {"maxAgeDays": 30, "futureSkewSeconds": 300}
    ):
        return False
    artifact = next(item for item in model["artifacts"] if item["role"] == "model")
    recorded_artifact = evidence.get("artifact", {})
    if (
        recorded_artifact.get("filename") != artifact["filename"]
        or recorded_artifact.get("bytes") != artifact["bytes"]
        or recorded_artifact.get("sha256") != artifact["sha256"]
        or recorded_artifact.get("modelRevision") != model["modelRevision"]
        or recorded_artifact.get("artifactRevision") != model["artifactRevision"]
        or recorded_artifact.get("integrityVerified") is not True
    ):
        return False
    expected_configuration = acceptance_configuration(
        model,
        evidence["mode"],
        evidence["profile"],
    )
    recorded_configuration = evidence.get("configuration", {})
    if any(
        recorded_configuration.get(key) != value
        for key, value in expected_configuration.items()
    ):
        return False
    recorded_host = evidence.get("host", {})
    current_fingerprint = host_fingerprint()
    if (
        recorded_host.get("platform") != host.get("platform")
        or recorded_host.get("architecture") != host.get("architecture")
        or recorded_host.get("ramGiB", 0) < model["requirements"]["minRamGiB"]
        or host.get("ramGiB", 0) < model["requirements"]["minRamGiB"]
        or recorded_host.get("fingerprintType") != HOST_FINGERPRINT_TYPE
        or not current_fingerprint
        or recorded_host.get("fingerprint") != current_fingerprint
    ):
        return False
    current_gpus = sorted(host.get("gpus", []), key=lambda gpu: gpu["index"])
    recorded_gpus = sorted(
        recorded_host.get("gpus", []), key=lambda gpu: gpu["index"]
    )
    if len(current_gpus) != len(recorded_gpus):
        return False
    for current, recorded in zip(current_gpus, recorded_gpus, strict=True):
        if (
            current.get("index") != recorded.get("index")
            or current.get("name") != recorded.get("name")
            or current.get("driver") != recorded.get("driver")
            or abs(current.get("vramGiB", 0) - recorded.get("vramGiB", 0)) > 0.2
        ):
            return False
    runtime = evidence.get("runtime", {})
    current_container = live_container(model["id"])
    current_state = (current_container or {}).get("State") or {}
    current_health = current_state.get("Health") or {}
    return bool(
        runtime.get("containerName") == model["id"]
        and runtime.get("configuredImage")
        and runtime.get("configuredImage") == configured_runtime_image(
            evidence["profile"]
        )
        and runtime.get("imageId")
        and current_container
        and current_state.get("Status") == "running"
        and current_health.get("Status") == "healthy"
        and current_container.get("Image") == runtime.get("imageId")
        and live_runtime_sha256(current_container)
        == runtime.get("containerConfigSha256")
    )


def read_secure_evidence(path: Path) -> dict[str, Any] | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
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
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def discover_host_acceptance(
    model: dict[str, Any] | None, host: dict[str, Any]
) -> dict[str, Any] | None:
    if not model:
        return None
    try:
        directory = ACCEPTANCE_DIR.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or stat.S_IMODE(directory.st_mode) & 0o077
            or directory.st_uid != os.getuid()
        ):
            return None
        candidates: list[tuple[int, Path]] = []
        for path in ACCEPTANCE_DIR.iterdir():
            metadata = path.lstat()
            if (
                path.suffix == ".json"
                and stat.S_ISREG(metadata.st_mode)
                and metadata.st_size <= 1024 * 1024
            ):
                candidates.append((metadata.st_mtime_ns, path))
    except OSError:
        return None
    for _, path in sorted(candidates, reverse=True):
        evidence = read_secure_evidence(path)
        if not evidence or evidence.get("evidenceId") != path.stem:
            continue
        if acceptance_matches_host(model, host, evidence):
            return {
                "status": "passed-current-configuration",
                "evidence": str(path.relative_to(ROOT_DIR)),
                "finishedAt": evidence.get("finishedAt"),
                "mode": evidence.get("mode"),
            }
    return None


def plan_for_host(
    args: argparse.Namespace,
    catalog: dict[str, Any],
    host: dict[str, Any],
    host_acceptance: dict[str, Any] | None = None,
    *,
    simulation: bool = False,
) -> dict[str, Any]:
    selected = model_by_id(catalog, args.model) if args.model else recommend(catalog, host)
    hardware_fits = bool(selected and fits(selected, host))
    resources_available = bool(selected and resources_available_now(selected, host))
    automatic_supported = automatic_deployment_supported(host)
    hardware_profile_match = matches_validated_hardware_profile(selected, host)
    ready = bool(
        hardware_fits
        and resources_available
        and automatic_supported
        and host["docker"]["available"]
        and host["dockerCompose"]["available"]
        and host["dockerCompose"].get("configurationCompatible", False)
        and host["nvidiaContainerRuntime"]
        and host.get("curl", {}).get("available", False)
        and host.get("python", {}).get("supported", False)
        and host.get("commands", {}).get("flock", False)
        and not simulation
    )
    plan_caveats = caveats(host, selected, host_acceptance)
    if simulation:
        plan_caveats.append(
            "Capacity overrides are simulation-only; they never authorize deployment commands."
        )
    return {
        "schemaVersion": 1,
        "mode": "read-only-plan",
        "simulatedHost": simulation,
        "catalogUpdatedAt": catalog["updatedAt"],
        "artifactPolicy": catalog["artifactPolicy"],
        "host": host,
        "recommendation": selected,
        "evidenceStatus": (
            "validated-on-this-host"
            if host_acceptance
            else (
                "validated-hardware-profile-match"
                if hardware_profile_match
                else "estimated-on-this-host"
            )
        ),
        "catalogEvidenceStatus": (
            "validated-profile"
            if selected and selected.get("status") == "validated"
            else "estimated-profile"
        ),
        "hardwareProfileMatch": hardware_profile_match,
        "hostAcceptancePolicy": {
            "schemaVersion": ACCEPTANCE_SCHEMA_VERSION,
            "maxAgeDays": int(ACCEPTANCE_MAX_AGE.total_seconds() // 86400),
            "futureSkewSeconds": int(ACCEPTANCE_FUTURE_SKEW.total_seconds()),
            "requiresMachineFingerprint": True,
            "requiresSecureEvidenceFile": True,
        },
        "hostAcceptanceStatus": (
            host_acceptance["status"]
            if host_acceptance
            else "not-evaluated-by-read-only-plan"
        ),
        "hostAcceptanceEvidence": host_acceptance,
        "fits": hardware_fits,
        "resourceAvailableNow": resources_available,
        "automaticDeploymentSupported": automatic_supported,
        "readyToDeploy": ready,
        "caveats": plan_caveats,
        "nextCommands": (
            [
                f"./scripts/model-manager.py download --model {selected['id']} --yes",
                f"./scripts/model-manager.py select --model {selected['id']} --yes",
                "./scripts/runtime.sh start latency",
                "./scripts/acceptance-suite.sh quick",
            ]
            if selected and ready
            else []
        ),
    }


def plan_payload(args: argparse.Namespace, catalog: dict[str, Any]) -> dict[str, Any]:
    host = host_assessment(args.vram_gib, args.ram_gib)
    selected = model_by_id(catalog, args.model) if args.model else recommend(catalog, host)
    host_acceptance = discover_host_acceptance(selected, host)
    return plan_for_host(
        args,
        catalog,
        host,
        host_acceptance,
        simulation=args.vram_gib is not None or args.ram_gib is not None,
    )


def print_plan(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    host = payload["host"]
    print("Host assessment (read-only)")
    if host["gpus"]:
        for gpu in host["gpus"]:
            print(
                f"  GPU {gpu['index']}: {gpu['name']} "
                f"({gpu['vramGiB']} GiB total / "
                f"{gpu.get('freeVramGiB', gpu['vramGiB'])} GiB free)"
            )
    else:
        print("  GPU: no NVIDIA GPU detected")
    print(
        f"  RAM: {host['ramGiB']} GiB total / "
        f"{host.get('availableRamGiB', 0)} GiB available; "
        f"free disk: {host['freeDiskGiB']} GiB"
    )
    print(
        f"  Prerequisites: Docker={host['docker']['version']}; "
        f"Compose={host['dockerCompose']['version']}; "
        f"Python={host.get('python', {}).get('version')}; "
        f"curl={host.get('curl', {}).get('available', False)}; "
        f"flock={host.get('commands', {}).get('flock', False)}"
    )
    recommendation = payload["recommendation"]
    if recommendation:
        req = recommendation["requirements"]
        print(
            f"Recommendation: {recommendation['id']} [{payload['evidenceStatus']}] "
            f"ctx={recommendation['runtime']['contextTokens']} "
            f"(minimum {req['minVramGiB']} GiB VRAM / {req['minRamGiB']} GiB RAM)"
        )
        print(
            f"  Evidence: catalog={payload['catalogEvidenceStatus']}; "
            f"hostAcceptance={payload['hostAcceptanceStatus']}"
        )
        print(
            f"  Provenance: model={recommendation['modelRevision']}; "
            f"artifact={recommendation['artifactRevision']}; "
            f"license={recommendation['license']['spdx']} "
            f"(reviewRequired={str(recommendation['license']['reviewRequired']).lower()})"
        )
        for artifact in recommendation["artifacts"]:
            print(
                f"  Artifact: {artifact['filename']} "
                f"({artifact['bytes'] / 1024**3:.2f} GiB); "
                f"SHA256={artifact['sha256']}"
            )
        print(
            f"  Deployment admission: fits={str(payload['fits']).lower()}; "
            f"resourcesAvailableNow={str(payload['resourceAvailableNow']).lower()}; "
            f"automaticDeploymentSupported="
            f"{str(payload['automaticDeploymentSupported']).lower()}; "
            f"readyToDeploy={str(payload['readyToDeploy']).lower()}"
        )
    else:
        print("Recommendation: none; this catalog only automates NVIDIA CUDA hosts with >=2 GiB VRAM")
    for note in payload["caveats"]:
        print(f"  NOTE: {note}")
    if payload["nextCommands"]:
        print("No state was changed. After reviewing size, status, license, and source:")
        for command in payload["nextCommands"]:
            print(f"  {command}")
    else:
        print("No state was changed. Deployment commands are withheld by the admission policy.")


def admission_payload(catalog: dict[str, Any], model_id: str) -> dict[str, Any]:
    args = argparse.Namespace(model=model_id)
    host = host_assessment(None, None)
    model = model_by_id(catalog, model_id)
    acceptance = discover_host_acceptance(model, host)
    return plan_for_host(args, catalog, host, acceptance)


def require_deployment_admission(
    catalog: dict[str, Any],
    model_id: str,
    action: str,
) -> dict[str, Any]:
    payload = admission_payload(catalog, model_id)
    if payload["readyToDeploy"]:
        return payload
    notes = "; ".join(payload["caveats"]) or "host admission requirements are not met"
    raise SystemExit(f"{action} blocked by deployment admission: {notes}")


def confirmation_required(args: argparse.Namespace, action: str) -> None:
    if not args.yes:
        raise SystemExit(
            f"{action} changes local state; inspect `plan --model {args.model}` and rerun with --yes"
        )


def model_path(model: dict[str, Any]) -> Path:
    return MODELS_DIR / model["modelDirectory"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def exclusive_local_lock(name: str) -> Any:
    lock_dir = ROOT_DIR / "cache" / "locks"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_dir.chmod(0o700)
    path = lock_dir / f"{name}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit(f"another operation already holds the local lock: {path}") from error
        yield
    finally:
        os.close(descriptor)


def verify_artifact(
    path: Path,
    artifact: dict[str, Any],
    *,
    cached: bool = False,
    cache_key: str | None = None,
) -> bool:
    size = path.stat().st_size
    if size != artifact["bytes"]:
        raise SystemExit(f"size mismatch for {path}: got {size}, expected {artifact['bytes']}")
    metadata = path.stat()
    fingerprint = (
        f"{metadata.st_dev}:{metadata.st_ino}:{metadata.st_size}:"
        f"{metadata.st_mtime_ns}:{metadata.st_ctime_ns}"
    )
    stamp_path = INTEGRITY_DIR / f"{cache_key or artifact['filename']}.sha256.stamp"
    expected_stamp = f"{artifact['sha256']}|{fingerprint}"
    if cached and stamp_path.is_file() and stamp_path.read_text(encoding="utf-8").strip() == expected_stamp:
        return True
    actual = sha256(path)
    if actual != artifact["sha256"]:
        raise SystemExit(f"SHA256 mismatch for {path}: got {actual}")
    atomic_write_private_text(stamp_path, expected_stamp + "\n")
    return False


def record_acquisition(
    model: dict[str, Any],
    artifact: dict[str, Any],
    *,
    method: str,
) -> None:
    """Record provenance status after exact local identity verification."""
    document = {
        "schemaVersion": 1,
        "kind": "local-inference-stack/artifact-acquisition",
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "modelId": model["id"],
        "artifact": {
            "filename": artifact["filename"],
            "bytes": artifact["bytes"],
            "sha256": artifact["sha256"],
            "url": artifact["url"],
            "repository": model["artifactRepository"],
            "revision": model["artifactRevision"],
        },
        "acquisitionMethod": method,
        "transport": "https-pinned-host-revision",
        "verification": {
            "identity": "verified-byte-size-and-sha256",
            "publisher": "not-cryptographically-verified",
            "license": "metadata-recorded-review-required",
        },
        "license": model["license"],
        "tooling": {"curl": command_output(["curl", "--version"])},
    }
    path = ACQUISITION_DIR / f"{model['id']}--{artifact['filename']}.json"
    atomic_write_private_text(
        path,
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def download_model(args: argparse.Namespace, catalog: dict[str, Any]) -> None:
    confirmation_required(args, "download")
    model = model_by_id(catalog, args.model)
    require_deployment_admission(catalog, model["id"], "download")
    with exclusive_local_lock(f"download-{model['id']}"):
        destination = model_path(model)
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.chmod(0o700)
        artifacts = [
            artifact
            for artifact in model["artifacts"]
            if artifact["required"] or args.all_artifacts
        ]
        for artifact in artifacts:
            final = destination / artifact["filename"]
            if final.is_symlink():
                raise SystemExit(f"unsafe existing artifact symlink: {final}")
            if final.exists():
                metadata = final.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1
                ):
                    raise SystemExit(f"unsafe existing artifact path: {final}")
                verify_artifact(final, artifact)
                final.chmod(0o600)
                record_acquisition(model, artifact, method="verified-existing-local")
                print(f"verified existing artifact: {final}")
                continue
            partial = final.with_suffix(final.suffix + ".part")
            if partial.exists():
                metadata = partial.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1
                    or metadata.st_size > artifact["bytes"]
                ):
                    raise SystemExit(f"unsafe partial artifact path: {partial}")
                partial_size = metadata.st_size
            else:
                partial_size = 0
            remaining = artifact["bytes"] - partial_size
            if shutil.disk_usage(destination).free < remaining:
                raise SystemExit(
                    f"insufficient free disk for {artifact['filename']}: "
                    f"need at least {remaining} additional bytes"
                )
            print(
                f"downloading {artifact['bytes'] / 1024**3:.2f} GiB: "
                f"{artifact['filename']}"
            )
            try:
                subprocess.run(
                    [
                        "curl",
                        "--fail",
                        "--location",
                        "--proto",
                        "=https",
                        "--proto-redir",
                        "=https",
                        "--retry",
                        "8",
                        "--retry-all-errors",
                        "--continue-at",
                        "-",
                        "--max-filesize",
                        str(artifact["bytes"]),
                        "--output",
                        str(partial),
                        artifact["url"],
                    ],
                    check=True,
                )
                partial.chmod(0o600)
                verify_artifact(partial, artifact)
                with partial.open("rb+") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                partial.replace(final)
                final.chmod(0o600)
                directory_descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except (subprocess.CalledProcessError, OSError):
                print(
                    f"partial download retained for safe resume: {partial}",
                    file=sys.stderr,
                )
                raise
            record_acquisition(model, artifact, method="https-download")
            print(f"downloaded and verified: {final}")


def deployment_env(model: dict[str, Any]) -> str:
    runtime = model["runtime"]
    artifact = next(item for item in model["artifacts"] if item["role"] == "model")
    values = {
        "QWEN_CATALOG_ID": model["id"],
        "QWEN_MODEL_DIR": f"./models/{model['modelDirectory']}",
        "QWEN_MODEL_FILE": artifact["filename"],
        "QWEN_MODEL_DISPLAY_NAME": model["displayName"],
        "QWEN_QUANTIZATION": model["quantization"],
        "QWEN_SERVED_MODEL_ID": model["servedModelId"],
        "QWEN_CONTAINER_NAME": model["id"],
        "QWEN_RUNTIME_UID": os.getuid(),
        "QWEN_RUNTIME_GID": os.getgid(),
        "MODELPORT_NETWORK_NAME": "modelport_default",
        "QWEN_CTX_SIZE": runtime["contextTokens"],
        "QWEN_RECOMMENDED_INPUT_TOKENS": runtime["recommendedInputTokens"],
        "QWEN_N_PREDICT": runtime["maxOutputTokens"],
        "QWEN_CACHE_RAM": runtime["cacheRamMiB"],
        "QWEN_BATCH_SIZE": runtime["batchSize"],
        "QWEN_UBATCH_SIZE": runtime["ubatchSize"],
    }
    return "# Generated by scripts/model-manager.py; local and intentionally untracked.\n" + "".join(
        f"{key}={shlex.quote(str(value))}\n" for key, value in values.items()
    )


def select_model(args: argparse.Namespace, catalog: dict[str, Any]) -> None:
    confirmation_required(args, "select")
    model = model_by_id(catalog, args.model)
    require_deployment_admission(catalog, model["id"], "select")
    for artifact in model["artifacts"]:
        if not artifact["required"]:
            continue
        path = model_path(model) / artifact["filename"]
        if not path.is_file():
            raise SystemExit(f"cannot select a model with a missing artifact: {path}")
        verify_artifact(
            path,
            artifact,
            cached=True,
            cache_key=f"{model['id']}--{artifact['filename']}",
        )
    atomic_write_private_text(LOCAL_PROFILE, deployment_env(model))
    print(f"selected {model['id']}: {LOCAL_PROFILE}")


def selected_model(catalog: dict[str, Any], explicit: str | None) -> dict[str, Any]:
    if explicit:
        return model_by_id(catalog, explicit)
    if LOCAL_PROFILE.is_file():
        for line in LOCAL_PROFILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("QWEN_CATALOG_ID="):
                return model_by_id(catalog, line.split("=", 1)[1])
    return model_by_id(catalog, catalog["defaultModel"])


def verify_model(args: argparse.Namespace, catalog: dict[str, Any]) -> None:
    model = selected_model(catalog, args.model)
    found = 0
    for artifact in model["artifacts"]:
        path = model_path(model) / artifact["filename"]
        if not path.is_file():
            if artifact["required"]:
                raise SystemExit(f"missing required artifact: {path}")
            if args.full:
                print(f"optional artifact absent: {path}")
            continue
        if not args.full and not artifact["required"]:
            continue
        was_cached = verify_artifact(
            path,
            artifact,
            cached=args.cached,
            cache_key=f"{model['id']}--{artifact['filename']}",
        )
        found += 1
        suffix = " (cached)" if was_cached else ""
        print(f"{artifact['filename']}: OK{suffix}")
    if not found:
        raise SystemExit(f"no artifacts verified for {model['id']}")


def list_models(catalog: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(catalog["models"], ensure_ascii=False, indent=2))
        return
    print("MODEL                         STATUS      MIN VRAM  MIN RAM  CONTEXT  DOWNLOAD")
    for model in catalog["models"]:
        primary = next(item for item in model["artifacts"] if item["role"] == "model")
        print(
            f"{model['id']:<29} {model['status']:<11} "
            f"{model['requirements']['minVramGiB']:>4} GiB  "
            f"{model['requirements']['minRamGiB']:>4} GiB  "
            f"{model['runtime']['contextTokens']:>7}  {primary['bytes'] / 1024**3:>5.1f} GiB"
        )


def parse_http_headers(output: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in output.splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        headers[key.strip().lower()] = value.strip().strip('"')
    return headers


def audit_sources(catalog: dict[str, Any], as_json: bool) -> int:
    checks: list[dict[str, Any]] = []
    for model in catalog["models"]:
        license_result = subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--head",
                "--max-time",
                "30",
                model["license"]["source"],
            ],
            capture_output=True,
            text=True,
        )
        checks.append(
            {
                "kind": "official-model-license-document",
                "modelId": model["id"],
                "filename": "LICENSE",
                "passed": license_result.returncode == 0,
                "expectedRevision": model["modelRevision"],
                "actualRevision": None,
                "expectedSha256": None,
                "actualSha256": None,
                "error": license_result.stderr.strip() or None,
            }
        )
        for artifact in model["artifacts"]:
            result = subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--head",
                    "--max-time",
                    "30",
                    artifact["url"],
                ],
                capture_output=True,
                text=True,
            )
            headers = parse_http_headers(result.stdout)
            actual_revision = headers.get("x-repo-commit")
            actual_sha256 = headers.get("x-linked-etag")
            passed = (
                result.returncode == 0
                and actual_revision == model["artifactRevision"]
                and actual_sha256 == artifact["sha256"]
            )
            checks.append(
                {
                    "kind": "gguf-artifact",
                    "modelId": model["id"],
                    "filename": artifact["filename"],
                    "passed": passed,
                    "expectedRevision": model["artifactRevision"],
                    "actualRevision": actual_revision,
                    "expectedSha256": artifact["sha256"],
                    "actualSha256": actual_sha256,
                    "error": result.stderr.strip() or None,
                }
            )
    failed = [check for check in checks if not check["passed"]]
    payload = {
        "schemaVersion": 1,
        "mode": "read-only-upstream-source-audit",
        "status": "passed" if not failed else "failed",
        "summary": {"passed": len(checks) - len(failed), "failed": len(failed)},
        "checks": checks,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            marker = "PASS" if check["passed"] else "FAIL"
            if check["kind"] == "official-model-license-document":
                detail = f"revision={check['expectedRevision']} license=available"
            else:
                detail = (
                    f"revision={check['actualRevision']} "
                    f"sha256={check['actualSha256']}"
                )
            print(f"[{marker}] {check['modelId']}/{check['filename']}: {detail}")
        print(
            f"Upstream source audit {payload['status']}: "
            f"{payload['summary']['passed']} passed, "
            f"{payload['summary']['failed']} failed"
        )
    return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="list reviewed catalog entries")
    list_parser.add_argument("--json", action="store_true")
    audit_parser = subparsers.add_parser(
        "audit-sources",
        help="read-only network check of pinned revisions and Hugging Face LFS hashes",
    )
    audit_parser.add_argument("--json", action="store_true")
    admit_parser = subparsers.add_parser(
        "admit",
        help="read-only deployment admission for one reviewed catalog model",
    )
    admit_parser.add_argument("--model", required=True)
    admit_parser.add_argument("--json", action="store_true")
    plan_parser = subparsers.add_parser("plan", help="read-only host assessment and recommendation")
    plan_parser.add_argument("--model", help="evaluate an explicit catalog model")
    plan_parser.add_argument("--json", action="store_true")
    plan_parser.add_argument("--vram-gib", type=float, help="test-only capacity override")
    plan_parser.add_argument("--ram-gib", type=float, help="test-only capacity override")
    for name in ("download", "select"):
        action_parser = subparsers.add_parser(name)
        action_parser.add_argument("--model", required=True)
        action_parser.add_argument("--yes", action="store_true")
        if name == "download":
            action_parser.add_argument("--all-artifacts", action="store_true")
    verify_parser = subparsers.add_parser("verify", help="verify the selected model against the catalog")
    verify_parser.add_argument("--model")
    verify_parser.add_argument("--full", action="store_true", help="also verify present optional artifacts")
    verify_parser.add_argument("--cached", action="store_true", help="reuse a hash when file identity and metadata match")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog = load_catalog()
    if args.command == "list":
        list_models(catalog, args.json)
    elif args.command == "audit-sources":
        return audit_sources(catalog, args.json)
    elif args.command == "admit":
        payload = admission_payload(catalog, args.model)
        print_plan(payload, args.json)
        return 0 if payload["readyToDeploy"] else 3
    elif args.command == "plan":
        print_plan(plan_payload(args, catalog), args.json)
    elif args.command == "download":
        download_model(args, catalog)
    elif args.command == "select":
        select_model(args, catalog)
    elif args.command == "verify":
        verify_model(args, catalog)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
