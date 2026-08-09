#!/usr/bin/env python3
"""Assess a host and safely materialize a catalog-backed local deployment."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
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

try:
    from scripts.env_utils import atomic_write_private_text
    from scripts.runtime_identity import (
        acceptance_configuration,
        artifact_stat_fingerprint,
        atomic_write_private_project_text,
        configured_runtime_image,
        deployment_values,
        ensure_private_project_directory,
        local_artifact_identity,
        live_container,
        live_runtime_sha256,
        open_private_project_file,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from env_utils import atomic_write_private_text
    from runtime_identity import (
        acceptance_configuration,
        artifact_stat_fingerprint,
        atomic_write_private_project_text,
        configured_runtime_image,
        deployment_values,
        ensure_private_project_directory,
        local_artifact_identity,
        live_container,
        live_runtime_sha256,
        open_private_project_file,
    )


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from local_inference_stack.paths import ProjectPaths  # noqa: E402
from local_inference_stack.result import RecoveryError  # noqa: E402
from local_inference_stack.transactions import TransactionStore  # noqa: E402
from local_inference_stack.catalog import (  # noqa: E402
    CatalogError,
    automatic_deployment_supported,
    fits,
    load_catalog as read_catalog,
    matches_recorded_hardware_profile,
    matches_validated_hardware_profile,
    model_by_id as catalog_model_by_id,
    recommend,
    resolve_catalog_path,
    resources_available_now,
)
from local_inference_stack.deployment import (  # noqa: E402
    CatalogDeploymentSpec,
    DeploymentSpecError,
    bind_approved_catalog_spec,
    build_deployment_plan,
)
from local_inference_stack.configuration import (  # noqa: E402
    catalog_deployment_environment,
    selected_deployment_values_mode,
)
from local_inference_stack.host import environment_kind  # noqa: E402
from local_inference_stack.acceptance import (  # noqa: E402
    run_manifest_matches_evidence,
    run_record_valid,
    validation_input,
)

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
TRUSTED_ATTESTATION_KEY_ENV = "LOCAL_INFERENCE_TRUSTED_ATTESTATION_KEY"
TRUSTED_ATTESTATION_KEY_SHA256_ENV = (
    "LOCAL_INFERENCE_TRUSTED_ATTESTATION_KEY_SHA256"
)


def load_catalog() -> dict[str, Any]:
    try:
        return read_catalog(CATALOG_PATH)
    except CatalogError as exc:
        raise SystemExit(str(exc)) from exc


def model_by_id(catalog: dict[str, Any], model_id: str) -> dict[str, Any]:
    try:
        return catalog_model_by_id(catalog, model_id)
    except CatalogError as exc:
        raise SystemExit(str(exc)) from exc


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
        "environmentKind": environment_kind(
            system=sys.platform,
            kernel_release=os.uname().release,
        ),
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


def catalog_attestation_verification(
    model: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Cryptographically verify catalog promotion against an external trust root.

    Catalog booleans are descriptive metadata and are never authorization. The
    public-key path and its expected SHA256 must be supplied outside the catalog.
    """

    if not model or model.get("status") != "validated":
        return None
    metadata = model.get("validationAttestation")
    if not isinstance(metadata, dict):
        return None
    key_name = os.environ.get(TRUSTED_ATTESTATION_KEY_ENV)
    trusted_fingerprint = os.environ.get(TRUSTED_ATTESTATION_KEY_SHA256_ENV)
    key_path = Path(key_name) if key_name else None
    if (
        key_path is None
        or not key_path.is_absolute()
        or not trusted_fingerprint
        or not re.fullmatch(r"[0-9a-f]{64}", trusted_fingerprint)
        or trusted_fingerprint != metadata.get("trustedKeySha256")
    ):
        return None
    document_path = resolve_catalog_path(
        ROOT_DIR, metadata.get("documentPath"), suffixes=(".json",)
    )
    signature_path = resolve_catalog_path(
        ROOT_DIR,
        metadata.get("signaturePath"),
        suffixes=(".sig", ".minisig", ".signature"),
    )
    if document_path is None or signature_path is None:
        return None
    try:
        from local_inference_stack import attestation as attestation_policy

        facts = attestation_policy.verify_detached(
            ProjectPaths(ROOT_DIR),
            document_path,
            signature_path,
            key_path.resolve(),
            metadata.get("tool"),
            trusted_key_sha256=trusted_fingerprint,
            require_promotion=True,
        )
    except (Exception, SystemExit):
        return None
    if (
        facts.get("payloadSha256") != metadata.get("payloadSha256")
        or facts.get("promotionEligible") is not True
        or (facts.get("subject") or {}).get("modelId") != model.get("id")
        or (facts.get("subject") or {}).get("mode") != "full"
        or (facts.get("subject") or {}).get("profile") != "latency"
        or not _attested_hardware_matches_profile(
            model, (facts.get("subject") or {}).get("hardware")
        )
        or (facts.get("signature") or {}).get("trustedKeyFingerprint") is not True
    ):
        return None
    return facts


def _attested_hardware_matches_profile(
    model: dict[str, Any], hardware: Any
) -> bool:
    """Bind promotion metadata to the host facts carried by the signature."""

    profile = model.get("validatedHardware")
    if not isinstance(profile, dict) or not isinstance(hardware, dict):
        return False
    gpus = hardware.get("gpus")
    if not isinstance(gpus, list) or len(gpus) != profile.get("gpuCount"):
        return False
    return bool(
        hardware.get("environmentKind") == profile.get("environmentKind")
        and hardware.get("architecture") == profile.get("architecture")
        and isinstance(hardware.get("ramGiB"), (int, float))
        and not isinstance(hardware.get("ramGiB"), bool)
        and hardware["ramGiB"] >= profile.get("minRamGiB", float("inf"))
        and all(
            isinstance(gpu, dict)
            and gpu.get("name") == profile.get("gpuName")
            and isinstance(gpu.get("vramGiB"), (int, float))
            and not isinstance(gpu.get("vramGiB"), bool)
            and gpu["vramGiB"] >= profile.get("minVramGiB", float("inf"))
            for gpu in gpus
        )
    )


def catalog_deployment_eligible(model: dict[str, Any] | None) -> bool:
    if (
        not model
        or model.get("status") != "validated"
        or model.get("lifecycleRole") != "lts"
    ):
        return False
    eligibility = model.get("deploymentEligibility", {})
    metadata = model.get("validationAttestation", {})
    if (
        eligibility.get("automatic") is not True
        or metadata.get("mode") != "full"
    ):
        return False
    # A static signatureVerified=true (including in a locally edited catalog)
    # deliberately has no effect. Only fresh detached verification can authorize.
    return catalog_attestation_verification(model) is not None


def caveats(
    host: dict[str, Any],
    model: dict[str, Any] | None,
    host_acceptance: dict[str, Any] | None = None,
    *,
    catalog_eligible: bool | None = None,
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
            "This host has no matching current validated Tier-1 profile; the Catalog "
            "record may be provisional or the environment/GPU may differ. Keep it "
            "read-only until qualification and promotion pass."
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
    if model and not (
        catalog_deployment_eligible(model)
        if catalog_eligible is None
        else catalog_eligible
    ):
        reason = (model.get("deploymentEligibility") or {}).get(
            "reason", "signed-full-attestation-required"
        )
        notes.append(
            "Catalog entry is a read-only candidate and cannot produce deployment "
            f"commands ({reason})."
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
    try:
        current_artifact_identity = local_artifact_identity(
            model_path(model) / artifact["filename"],
            INTEGRITY_DIR / f"{model['id']}--{artifact['filename']}.sha256.stamp",
            expected_bytes=artifact["bytes"],
            expected_sha256=artifact["sha256"],
            project_root=ROOT_DIR,
        )
    except RuntimeError:
        # A stale/missing stamp never triggers a read-only planner rehash. The
        # normal verify/quick path will perform the required full SHA256 check.
        return False
    if recorded_artifact.get("localIdentity") != current_artifact_identity:
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
        if key != "catalogSha256"
    ):
        return False
    try:
        current_catalog = read_catalog(CATALOG_PATH)
        current_model = next(
            item for item in current_catalog["models"] if item["id"] == model["id"]
        )
        expected_validation_input = validation_input(
            current_catalog, current_model, expected_configuration
        )
    except (CatalogError, KeyError, StopIteration, ValueError):
        return False
    if evidence.get("validationInput") != expected_validation_input:
        return False
    if not run_record_valid(
        evidence.get("run"),
        mode=evidence["mode"],
        overall_status=evidence["status"],
        overall_exit_code=evidence["exitCode"],
        configuration=recorded_configuration,
    ):
        return False
    if not acceptance_run_manifest_matches(evidence):
        return False
    recorded_host = evidence.get("host", {})
    current_fingerprint = host_fingerprint()
    if (
        recorded_host.get("platform") != host.get("platform")
        or recorded_host.get("environmentKind") != host.get("environmentKind")
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


def secure_evidence_sha256(path: Path) -> str | None:
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
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def acceptance_run_manifest_matches(evidence: dict[str, Any]) -> bool:
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
    path = ROOT_DIR / relative
    manifest = read_secure_evidence(path)
    source_sha = secure_evidence_sha256(path)
    return bool(
        manifest
        and source_sha
        and source_sha == identity.get("sourceSha256")
        and run_manifest_matches_evidence(manifest, run_record, evidence)
    )


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
    catalog_eligible = catalog_deployment_eligible(selected)
    hardware_profile_match = matches_validated_hardware_profile(selected, host)
    host_admitted = bool(
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
        and hardware_profile_match
        and not simulation
    )
    ready = bool(host_admitted and catalog_eligible)
    action_plan = None
    if selected and ready:
        deployment_spec = CatalogDeploymentSpec.from_catalog_model(selected)
        action_plan = build_deployment_plan(
            deployment_spec,
            admission_granted=True,
        ).document()
    plan_caveats = caveats(
        host,
        selected,
        host_acceptance,
        catalog_eligible=catalog_eligible,
    )
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
            else (
                "provisional-legacy-profile"
                if selected and selected.get("status") == "provisional"
                else "estimated-profile"
            )
        ),
        "catalogDeploymentEligible": catalog_eligible,
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
        "hostAdmissionPassed": host_admitted,
        "automaticDeploymentSupported": automatic_supported,
        "readyToDeploy": ready,
        "actionPlan": action_plan,
        "caveats": plan_caveats,
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
            f"catalogDeploymentEligible="
            f"{str(payload['catalogDeploymentEligible']).lower()}; "
            f"readyToDeploy={str(payload['readyToDeploy']).lower()}"
        )
        if "readyToStartExisting" in payload:
            print(
                "  Existing selection recovery: "
                f"readyToStartExisting={str(payload['readyToStartExisting']).lower()}"
            )
    else:
        print("Recommendation: none; no evidence-backed Catalog entry fits this host")
    for note in payload["caveats"]:
        print(f"  NOTE: {note}")
    if payload["actionPlan"]:
        print(
            "No state was changed. A typed deployment plan is available; "
            "after review, use ./stack deploy --model "
            f"{recommendation['id']} --yes"
        )
    else:
        print("No state was changed. Deployment commands are withheld by the admission policy.")


def admission_payload(
    catalog: dict[str, Any],
    model_id: str,
    *,
    existing_selection: bool = False,
) -> dict[str, Any]:
    args = argparse.Namespace(model=model_id)
    host = host_assessment(None, None)
    model = model_by_id(catalog, model_id)
    acceptance = discover_host_acceptance(model, host)
    payload = plan_for_host(args, catalog, host, acceptance)
    if existing_selection:
        selected = selected_model(catalog, None) if LOCAL_PROFILE.is_file() else None
        try:
            selected_values = deployment_values() if selected else {}
        except (OSError, RuntimeError, ValueError):
            selected_values = {}
        selection_mode = selected_deployment_values_mode(model, selected_values)
        selection_matches = bool(
            selected
            and selected["id"] == model_id
            and selection_mode == "exact-current-projection"
        )
        recovery_hardware_match = matches_recorded_hardware_profile(model, host)
        recovery_resources_available = resources_available_now(model, host)
        recovery_host_admitted = bool(
            fits(model, host)
            and automatic_deployment_supported(host)
            and host["docker"]["available"]
            and host["dockerCompose"]["available"]
            and host["dockerCompose"].get("configurationCompatible", False)
            and host["nvidiaContainerRuntime"]
            and host.get("curl", {}).get("available", False)
            and host.get("python", {}).get("supported", False)
            and host.get("commands", {}).get("flock", False)
            and recovery_hardware_match
        )
        payload["mode"] = "read-only-existing-selection-admission"
        payload["selectedConfigurationMatchesCatalog"] = selection_matches
        payload["selectedConfigurationMode"] = selection_mode
        payload["recoveryHardwareProfileMatch"] = recovery_hardware_match
        payload["recoveryResourcesAvailableNow"] = recovery_resources_available
        payload["recoveryHostAdmissionPassed"] = recovery_host_admitted
        payload["readyToStartExisting"] = bool(
            selection_matches and recovery_host_admitted
        )
        if (
            selected
            and selected["id"] == model_id
            and selection_mode == "legacy-compatible-current-defaults"
        ):
            payload["caveats"].append(
                "The selected private profile is a recognized legacy migration "
                "source, not runtime start authority. Run ./stack migrate --yes "
                "to fully verify the artifact and normalize the profile first."
            )
        if payload["readyToStartExisting"] and not recovery_resources_available:
            payload["caveats"].append(
                "Current free VRAM or RAM is below the new-deployment threshold. "
                "Existing-selection recovery may still attempt this exact previously "
                "selected artifact and host profile, but runtime health remains a hard "
                "post-start requirement; this does not authorize a new deployment."
            )
        if payload["readyToStartExisting"]:
            payload["caveats"].append(
                "Recovery is authorized only for the already selected private "
                "Catalog projection on this exact recorded host; it does not "
                "authorize download, selection, or a new deployment."
            )
    return payload


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


def approved_catalog_action(
    args: argparse.Namespace,
    catalog: dict[str, Any],
    *,
    require_artifact: bool = False,
) -> tuple[dict[str, Any], Any | None]:
    """Bind a mutating child action to the active Catalog-spec approval."""

    model = model_by_id(catalog, args.model)
    catalog_spec_sha256 = getattr(args, "catalog_spec_sha256", None)
    artifact_sha256 = getattr(args, "artifact_sha256", None)
    transaction_id = os.environ.get("QWEN_CONTROL_TRANSACTION_ID")
    if catalog_spec_sha256 is None:
        if artifact_sha256 is not None or transaction_id:
            raise SystemExit(
                "transactional model action requires --catalog-spec-sha256"
            )
        return model, None
    if not transaction_id:
        raise SystemExit(
            "--catalog-spec-sha256 requires QWEN_CONTROL_TRANSACTION_ID"
        )
    if require_artifact and artifact_sha256 is None:
        raise SystemExit("transactional download requires --artifact-sha256")
    try:
        spec, artifact = bind_approved_catalog_spec(
            catalog,
            model["id"],
            catalog_spec_sha256,
            artifact_sha256=artifact_sha256,
        )
        TransactionStore(ProjectPaths(ROOT_DIR)).assert_approved_deployment(
            transaction_id=transaction_id,
            catalog_spec_sha256=spec.sha256,
            catalog_id=spec.catalog_id,
            artifact_sha256=artifact_sha256,
            inherited_locks=os.environ.get("QWEN_RUNTIME_LOCK_HELD") == "1",
        )
    except (DeploymentSpecError, RecoveryError) as error:
        raise SystemExit(str(error)) from error
    return model, artifact


def assert_deployment_binding(args: argparse.Namespace, catalog: dict[str, Any]) -> None:
    """Verify current Catalog, transaction approval, and selected local profile."""

    model, _artifact = approved_catalog_action(args, catalog)
    if args.selected:
        try:
            actual = deployment_values()
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit(f"cannot read selected deployment profile safely: {error}") from error
        expected = deployment_environment(model)
        if actual != expected:
            raise SystemExit(
                "selected deployment profile does not match the approved Catalog spec"
            )
    print(f"approved_catalog_spec={args.catalog_spec_sha256} model={model['id']}")


@contextlib.contextmanager
def authorized_download_boundary(
    args: argparse.Namespace,
    model: dict[str, Any],
    approved_artifact: Any | None,
) -> Any:
    if args.catalog_spec_sha256 is None:
        yield
        return
    try:
        with TransactionStore(ProjectPaths(ROOT_DIR)).authorized_runtime_mutation(
            os.environ.get("QWEN_CONTROL_TRANSACTION_ID"),
            catalog_spec_sha256=args.catalog_spec_sha256,
            catalog_id=model["id"],
            artifact_sha256=(
                approved_artifact.sha256 if approved_artifact is not None else None
            ),
        ):
            yield
    except RecoveryError as error:
        raise SystemExit(str(error)) from error


def model_path(model: dict[str, Any]) -> Path:
    return MODELS_DIR / model["modelDirectory"]


def sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def exclusive_local_lock(name: str) -> Any:
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise SystemExit(f"unsafe local lock name: {name!r}")
    lock_dir = ROOT_DIR / "cache" / "locks"
    path = lock_dir / f"{name}.lock"
    directory_descriptor: int | None = None
    descriptor: int | None = None
    try:
        ensure_private_project_directory(lock_dir, project_root=ROOT_DIR)
        directory_descriptor = open_private_project_directory(lock_dir)
        descriptor = os.open(
            path.name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        metadata = os.fstat(descriptor)
        named = os.stat(
            path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            os.close(descriptor)
            raise SystemExit(f"unsafe local lock file: {path}")
        os.fchmod(descriptor, 0o600)
    except RuntimeError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise SystemExit(str(error)) from error
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise SystemExit(f"cannot safely open local lock: {path}: {error}") from error
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    try:
        assert descriptor is not None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit(f"another operation already holds the local lock: {path}") from error
        yield
    finally:
        os.close(descriptor)


def open_private_project_directory(path: Path) -> int:
    """Open a project directory one no-follow component at a time."""

    project_root = Path(os.path.abspath(ROOT_DIR))
    absolute = Path(os.path.abspath(path))
    try:
        absolute.relative_to(project_root)
    except ValueError as error:
        raise SystemExit(f"download directory escapes the project root: {path}") from error
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise SystemExit(
                f"download directory is not private and current-user-owned: {path}"
            )
        result = descriptor
        descriptor = -1
        return result
    except OSError as error:
        raise SystemExit(f"cannot safely open download directory: {path}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextlib.contextmanager
def secure_partial_file(
    partial: Path, *, maximum_bytes: int
) -> Any:
    """Create/open a resumable partial through a private no-follow dirfd."""

    directory_descriptor = open_private_project_directory(partial.parent)
    descriptor: int | None = None
    try:
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(
            partial.name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        metadata = os.fstat(descriptor)
        named = os.stat(
            partial.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > maximum_bytes
            or (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise SystemExit(f"unsafe partial artifact path: {partial}")
        os.fchmod(descriptor, 0o600)
        yield descriptor, directory_descriptor, metadata.st_size
    except OSError as error:
        raise SystemExit(f"cannot safely open partial artifact: {partial}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_descriptor)


def verify_artifact(
    path: Path,
    artifact: dict[str, Any],
    *,
    cached: bool = False,
    cache_key: str | None = None,
    write_stamp: bool = True,
) -> bool:
    stamp_key = cache_key or artifact["filename"]
    if not isinstance(stamp_key, str) or Path(stamp_key).name != stamp_key:
        raise SystemExit(f"unsafe integrity stamp key: {stamp_key!r}")
    stamp_path = INTEGRITY_DIR / f"{stamp_key}.sha256.stamp"
    if write_stamp:
        try:
            ensure_private_project_directory(INTEGRITY_DIR, project_root=ROOT_DIR)
        except RuntimeError as error:
            raise SystemExit(str(error)) from error

    descriptor: int | None = None
    try:
        descriptor, metadata, _relative = open_private_project_file(
            path, project_root=ROOT_DIR
        )
    except FileNotFoundError as error:
        raise SystemExit(f"missing artifact: {path}") from error
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    try:
        if metadata.st_size != artifact["bytes"]:
            raise SystemExit(
                f"size mismatch for {path}: got {metadata.st_size}, "
                f"expected {artifact['bytes']}"
            )
        fingerprint = artifact_stat_fingerprint(metadata)
        expected_stamp = f"{artifact['sha256']}|{fingerprint}"

        if cached:
            stamp_descriptor: int | None = None
            try:
                stamp_descriptor, stamp_metadata, _stamp_relative = (
                    open_private_project_file(
                        stamp_path,
                        project_root=ROOT_DIR,
                        maximum_bytes=4096,
                    )
                )
                with os.fdopen(os.dup(stamp_descriptor), "rb") as handle:
                    stamp_body = handle.read(4097)
                if artifact_stat_fingerprint(os.fstat(stamp_descriptor)) != (
                    artifact_stat_fingerprint(stamp_metadata)
                ):
                    raise SystemExit(
                        f"integrity stamp changed while it was inspected: {stamp_path}"
                    )
                try:
                    stamp_value = stamp_body.decode("utf-8").strip()
                except UnicodeDecodeError:
                    stamp_value = ""
                if stamp_value == expected_stamp:
                    if artifact_stat_fingerprint(os.fstat(descriptor)) != fingerprint:
                        raise SystemExit(
                            f"artifact changed while its cached identity was inspected: {path}"
                        )
                    return True
            except FileNotFoundError:
                pass
            except RuntimeError as error:
                raise SystemExit(str(error)) from error
            finally:
                if stamp_descriptor is not None:
                    os.close(stamp_descriptor)

        actual = sha256_descriptor(descriptor)
        if artifact_stat_fingerprint(os.fstat(descriptor)) != fingerprint:
            raise SystemExit(f"artifact changed during SHA256 verification: {path}")
        if actual != artifact["sha256"]:
            raise SystemExit(f"SHA256 mismatch for {path}: got {actual}")
        if write_stamp:
            try:
                atomic_write_private_project_text(
                    stamp_path,
                    expected_stamp + "\n",
                    project_root=ROOT_DIR,
                )
            except RuntimeError as error:
                raise SystemExit(str(error)) from error
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def fully_verify_artifact_descriptor(
    descriptor: int,
    path: Path,
    artifact: dict[str, Any],
) -> os.stat_result:
    """Hash one already-safe descriptor and reject any in-place mutation."""

    metadata = os.fstat(descriptor)
    if metadata.st_size != artifact["bytes"]:
        raise SystemExit(
            f"size mismatch for {path}: got {metadata.st_size}, "
            f"expected {artifact['bytes']}"
        )
    fingerprint = artifact_stat_fingerprint(metadata)
    actual = sha256_descriptor(descriptor)
    final_metadata = os.fstat(descriptor)
    if artifact_stat_fingerprint(final_metadata) != fingerprint:
        raise SystemExit(f"artifact changed during SHA256 verification: {path}")
    if actual != artifact["sha256"]:
        raise SystemExit(f"SHA256 mismatch for {path}: got {actual}")
    return final_metadata


def open_fully_verified_artifact(
    path: Path, artifact: dict[str, Any]
) -> tuple[int, os.stat_result]:
    """Open and hash one artifact while binding verification to its inode."""

    try:
        descriptor, _metadata, _relative = open_private_project_file(
            path, project_root=ROOT_DIR
        )
    except FileNotFoundError as error:
        raise SystemExit(f"missing artifact during promotion: {path}") from error
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    try:
        final_metadata = fully_verify_artifact_descriptor(
            descriptor, path, artifact
        )
        return descriptor, final_metadata
    except BaseException:
        os.close(descriptor)
        raise


def promote_verified_partial(
    partial: Path,
    final: Path,
    artifact: dict[str, Any],
    *,
    source_descriptor: int | None = None,
    directory_descriptor: int | None = None,
) -> None:
    """Atomically promote exactly the inode that was verified, then rehash it."""

    if source_descriptor is None:
        source_descriptor, source_metadata = open_fully_verified_artifact(
            partial, artifact
        )
    else:
        source_metadata = fully_verify_artifact_descriptor(
            source_descriptor, partial, artifact
        )
    promoted_descriptor: int | None = None
    try:
        os.fsync(source_descriptor)
        if directory_descriptor is None:
            directory_descriptor = open_private_project_directory(partial.parent)
        named_source = os.stat(
            partial.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(named_source.st_mode)
            or (named_source.st_dev, named_source.st_ino)
            != (source_metadata.st_dev, source_metadata.st_ino)
        ):
            raise SystemExit(
                f"partial artifact path changed before promotion: {partial}"
            )
        os.replace(
            partial.name,
            final.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        promoted_descriptor, promoted_metadata = open_fully_verified_artifact(
            final, artifact
        )
        if (promoted_metadata.st_dev, promoted_metadata.st_ino) != (
            source_metadata.st_dev,
            source_metadata.st_ino,
        ):
            raise SystemExit(
                f"promoted artifact is not the verified source inode: {final}"
            )
        named_final = os.stat(
            final.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (named_final.st_dev, named_final.st_ino) != (
            promoted_metadata.st_dev,
            promoted_metadata.st_ino,
        ):
            raise SystemExit(f"artifact path changed after promotion: {final}")
        os.fsync(promoted_descriptor)
        os.fsync(directory_descriptor)
    except OSError as error:
        raise SystemExit(f"cannot safely promote downloaded artifact: {error}") from error
    finally:
        if promoted_descriptor is not None:
            os.close(promoted_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        os.close(source_descriptor)


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
    model, approved_artifact = approved_catalog_action(
        args, catalog, require_artifact=True
    )
    if approved_artifact is not None and args.all_artifacts:
        raise SystemExit(
            "--all-artifacts cannot be combined with an approved artifact identity"
        )
    require_deployment_admission(catalog, model["id"], "download")
    with authorized_download_boundary(
        args, model, approved_artifact
    ), exclusive_local_lock(f"download-{model['id']}"):
        destination = model_path(model)
        try:
            ensure_private_project_directory(destination, project_root=ROOT_DIR)
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        artifacts = (
            [
                artifact
                for artifact in model["artifacts"]
                if artifact["sha256"] == approved_artifact.sha256
            ]
            if approved_artifact is not None
            else [
                artifact
                for artifact in model["artifacts"]
                if artifact["required"] or args.all_artifacts
            ]
        )
        for artifact in artifacts:
            final = destination / artifact["filename"]
            existing_descriptor: int | None = None
            try:
                existing_descriptor, _metadata, _relative = open_private_project_file(
                    final, project_root=ROOT_DIR
                )
            except FileNotFoundError:
                pass
            except RuntimeError as error:
                raise SystemExit(str(error)) from error
            if existing_descriptor is not None:
                os.fchmod(existing_descriptor, 0o600)
                os.close(existing_descriptor)
                verify_artifact(final, artifact)
                record_acquisition(model, artifact, method="verified-existing-local")
                print(f"verified existing artifact: {final}")
                continue
            partial = final.with_suffix(final.suffix + ".part")
            with secure_partial_file(
                partial, maximum_bytes=artifact["bytes"]
            ) as (partial_descriptor, directory_descriptor, partial_size):
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
                            f"/proc/self/fd/{partial_descriptor}",
                            artifact["url"],
                        ],
                        check=True,
                        pass_fds=(partial_descriptor,),
                    )
                    os.fsync(partial_descriptor)
                    promote_verified_partial(
                        partial,
                        final,
                        artifact,
                        source_descriptor=os.dup(partial_descriptor),
                        directory_descriptor=os.dup(directory_descriptor),
                    )
                except (subprocess.CalledProcessError, OSError):
                    print(
                        f"partial download retained for safe resume: {partial}",
                        file=sys.stderr,
                    )
                    raise
            record_acquisition(model, artifact, method="https-download")
            print(f"downloaded and verified: {final}")


def deployment_environment(model: dict[str, Any]) -> dict[str, str]:
    return catalog_deployment_environment(model)


def deployment_env(model: dict[str, Any]) -> str:
    values = deployment_environment(model)
    return "# Generated by scripts/model-manager.py; local and intentionally untracked.\n" + "".join(
        f"{key}={shlex.quote(value)}\n" for key, value in values.items()
    )


def select_model(args: argparse.Namespace, catalog: dict[str, Any]) -> None:
    confirmation_required(args, "select")
    model, _artifact = approved_catalog_action(args, catalog)
    require_deployment_admission(catalog, model["id"], "select")
    store = TransactionStore(ProjectPaths(ROOT_DIR))
    try:
        boundary = store.authorized_runtime_mutation(
            os.environ.get("QWEN_CONTROL_TRANSACTION_ID"),
            catalog_spec_sha256=args.catalog_spec_sha256,
            catalog_id=model["id"] if args.catalog_spec_sha256 else None,
        )
        with boundary:
            for artifact in model["artifacts"]:
                if not artifact["required"]:
                    continue
                path = model_path(model) / artifact["filename"]
                if not path.is_file():
                    raise SystemExit(
                        f"cannot select a model with a missing artifact: {path}"
                    )
                verify_artifact(
                    path,
                    artifact,
                    cached=True,
                    cache_key=f"{model['id']}--{artifact['filename']}",
                )
            atomic_write_private_text(LOCAL_PROFILE, deployment_env(model))
    except RecoveryError as error:
        raise SystemExit(str(error)) from error
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
            write_stamp=not args.read_only,
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
    admit_parser.add_argument(
        "--existing-selection",
        action="store_true",
        help="admit recovery of an already selected local model without authorizing a new deployment",
    )
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
        action_parser.add_argument(
            "--catalog-spec-sha256",
            help="bind a transactional action to its approved Catalog spec",
        )
        if name == "download":
            action_parser.add_argument("--all-artifacts", action="store_true")
            action_parser.add_argument(
                "--artifact-sha256",
                help="materialize exactly one approved required artifact",
            )
    assert_parser = subparsers.add_parser(
        "assert-deployment",
        help="verify current Catalog and transaction binding without mutation",
    )
    assert_parser.add_argument("--model", required=True)
    assert_parser.add_argument("--catalog-spec-sha256", required=True)
    assert_parser.add_argument("--selected", action="store_true")
    verify_parser = subparsers.add_parser("verify", help="verify the selected model against the catalog")
    verify_parser.add_argument("--model")
    verify_parser.add_argument("--full", action="store_true", help="also verify present optional artifacts")
    verify_parser.add_argument("--cached", action="store_true", help="reuse a hash when file identity and metadata match")
    verify_parser.add_argument(
        "--read-only",
        action="store_true",
        help="never create or refresh the local integrity stamp",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog = load_catalog()
    if args.command == "list":
        list_models(catalog, args.json)
    elif args.command == "audit-sources":
        return audit_sources(catalog, args.json)
    elif args.command == "admit":
        payload = admission_payload(
            catalog, args.model, existing_selection=args.existing_selection
        )
        print_plan(payload, args.json)
        admitted = (
            payload.get("readyToStartExisting", False)
            if args.existing_selection
            else payload["readyToDeploy"]
        )
        return 0 if admitted else 3
    elif args.command == "plan":
        print_plan(plan_payload(args, catalog), args.json)
    elif args.command == "download":
        download_model(args, catalog)
    elif args.command == "select":
        select_model(args, catalog)
    elif args.command == "assert-deployment":
        assert_deployment_binding(args, catalog)
    elif args.command == "verify":
        verify_model(args, catalog)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
