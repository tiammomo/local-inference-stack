#!/usr/bin/env python3
"""Maintain a runner-owned acceptance manifest and write host-bound evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from local_inference_stack.acceptance import (  # noqa: E402
    RUN_KIND,
    RUNNER_PATH,
    RUN_SCHEMA_VERSION,
    STEP_PLANS,
    VALIDATION_INPUT_POLICY,
    expected_steps,
    run_record_valid,
    sha256_document,
    step_plan_sha256,
    validation_input,
)
from local_inference_stack.catalog import (  # noqa: E402
    CatalogError,
    load_catalog,
    model_by_id,
)
from local_inference_stack.host import environment_kind  # noqa: E402

try:
    from scripts.env_utils import atomic_write_private_json
    from scripts.runtime_identity import (
        acceptance_configuration,
        local_artifact_identity,
        live_runtime_sha256,
        runtime_mismatches,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from env_utils import atomic_write_private_json
    from runtime_identity import (
        acceptance_configuration,
        local_artifact_identity,
        live_runtime_sha256,
        runtime_mismatches,
    )


CATALOG_PATH = ROOT_DIR / "catalog" / "models.json"
MODELS_DIR = ROOT_DIR / "models"
INTEGRITY_DIR = ROOT_DIR / "cache" / "integrity"
SUITE_PATH = ROOT_DIR / RUNNER_PATH
HOST_FINGERPRINT_TYPE = "machine-id-sha256-v1"
HOST_FINGERPRINT_CONTEXT = b"local-inference-stack.acceptance-host.v1\0"
MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
RUNNER_TOKEN_ENV = "LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN"


def reviewed_catalog_model(model_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load one model through the repository's strict Catalog boundary."""

    try:
        catalog = load_catalog(CATALOG_PATH)
        return catalog, model_by_id(catalog, model_id)
    except CatalogError as exc:
        raise RuntimeError("acceptance references an invalid or unknown Catalog model") from exc


def payload_sha256(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "selfSha256"}
    return sha256_document(unsigned)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def command_output(command: list[str], timeout: int = 30) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return None
    return result.stdout.strip()


def command_json(command: list[str]) -> Any | None:
    output = command_output(command)
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def total_ram_gib() -> float:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def gpu_inventory() -> list[dict[str, Any]]:
    output = command_output(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if not output:
        return []
    gpus: list[dict[str, Any]] = []
    for line in output.splitlines():
        fields = [field.strip() for field in line.split(",", 3)]
        if len(fields) != 4:
            continue
        try:
            gpus.append(
                {
                    "index": int(fields[0]),
                    "name": fields[1],
                    "vramGiB": round(float(fields[2]) / 1024, 1),
                    "driver": fields[3],
                }
            )
        except ValueError:
            continue
    return gpus


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("acceptance runner timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("acceptance runner timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("acceptance runner timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _private_json(path: Path) -> dict[str, Any]:
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
            raise RuntimeError("acceptance run manifest is not a private regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            document = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"cannot read acceptance run manifest: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(document, dict):
        raise RuntimeError("acceptance run manifest must be a JSON object")
    return document


def _manifest_self_hash(manifest: dict[str, Any]) -> str:
    return sha256_document(
        {key: value for key, value in manifest.items() if key != "selfSha256"}
    )


def _runner_capability_sha256() -> str:
    token = os.environ.get(RUNNER_TOKEN_ENV, "")
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise RuntimeError("acceptance evidence operations require the runner capability")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _validated_manifest(path: Path, *, require_final: bool = False) -> dict[str, Any]:
    _relative_manifest_path(path)
    manifest = _private_json(path)
    if (
        manifest.get("schemaVersion") != RUN_SCHEMA_VERSION
        or manifest.get("kind") != RUN_KIND
        or manifest.get("selfSha256") != _manifest_self_hash(manifest)
        or manifest.get("runner")
        != {
            "path": RUNNER_PATH,
            "sha256": sha256_file(SUITE_PATH),
            "capabilitySha256": _runner_capability_sha256(),
        }
        or manifest.get("plan")
        != {
            "stepNames": list(expected_steps(manifest.get("mode"))),
            "sha256": step_plan_sha256(manifest.get("mode")),
        }
        or set(manifest.get("validationInput") or {})
        != {"policy", "catalogSha256", "repositorySha256", "sha256"}
        or any(
            not SHA256_PATTERN.fullmatch(
                str((manifest.get("validationInput") or {}).get(key, ""))
            )
            for key in ("catalogSha256", "repositorySha256", "sha256")
        )
        or (manifest.get("validationInput") or {}).get("policy")
        != VALIDATION_INPUT_POLICY
    ):
        raise RuntimeError("acceptance run manifest identity is invalid")
    if require_final and manifest.get("finalized") is not True:
        raise RuntimeError("acceptance run manifest is not finalized")
    return manifest


def initialize_manifest(
    path: Path,
    *,
    mode: str,
    profile: str,
    catalog_model_id: str,
    started_at: str,
) -> dict[str, Any]:
    _relative_manifest_path(path)
    _parse_time(started_at)
    catalog, model = reviewed_catalog_model(catalog_model_id)
    configuration = acceptance_configuration(model, mode, profile)
    manifest = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "kind": RUN_KIND,
        "runId": str(uuid.uuid4()),
        "mode": mode,
        "profile": profile,
        "catalogModelId": catalog_model_id,
        "runner": {
            "path": RUNNER_PATH,
            "sha256": sha256_file(SUITE_PATH),
            "capabilitySha256": _runner_capability_sha256(),
        },
        "plan": {
            "stepNames": list(expected_steps(mode)),
            "sha256": step_plan_sha256(mode),
        },
        "validationInput": validation_input(catalog, model, configuration),
        "startedAt": started_at,
        "finishedAt": None,
        "durationSeconds": None,
        "status": "running",
        "exitCode": None,
        "failedAtStep": None,
        "terminalStep": None,
        "stepResults": [],
        "finalized": False,
    }
    manifest["selfSha256"] = _manifest_self_hash(manifest)
    atomic_write_private_json(path, manifest)
    return manifest


def append_step(
    path: Path,
    *,
    name: str,
    started_at: str,
    finished_at: str,
    duration_seconds: int,
    exit_code: int,
) -> dict[str, Any]:
    manifest = _validated_manifest(path)
    if manifest.get("finalized") is True or manifest.get("status") != "running":
        raise RuntimeError("acceptance run manifest is already finalized")
    ordinal = len(manifest["stepResults"]) + 1
    planned = expected_steps(manifest["mode"])
    if ordinal > len(planned) or name != planned[ordinal - 1]:
        raise RuntimeError("acceptance step is missing, duplicated, or out of order")
    started = _parse_time(started_at)
    finished = _parse_time(finished_at)
    if (
        duration_seconds < 0
        or started > finished
        or abs((finished - started).total_seconds() - duration_seconds) > 5
    ):
        raise RuntimeError("acceptance step duration is invalid")
    manifest["stepResults"].append(
        {
            "ordinal": ordinal,
            "name": name,
            "startedAt": started_at,
            "finishedAt": finished_at,
            "durationSeconds": duration_seconds,
            "exitCode": exit_code,
            "status": "passed" if exit_code == 0 else "failed",
        }
    )
    manifest["terminalStep"] = name
    if exit_code != 0:
        manifest["failedAtStep"] = name
    manifest["selfSha256"] = _manifest_self_hash(manifest)
    atomic_write_private_json(path, manifest)
    return manifest


def finalize_manifest(
    path: Path,
    *,
    finished_at: str,
    duration_seconds: int,
    exit_code: int,
    failed_at_step: str,
) -> dict[str, Any]:
    manifest = _validated_manifest(path)
    if manifest.get("finalized") is True:
        raise RuntimeError("acceptance run manifest is already finalized")
    started = _parse_time(manifest["startedAt"])
    finished = _parse_time(finished_at)
    if (
        duration_seconds < 0
        or started > finished
        or abs((finished - started).total_seconds() - duration_seconds) > 5
    ):
        raise RuntimeError("acceptance run duration is invalid")
    status = "passed" if exit_code == 0 else "failed"
    if status == "passed" and (
        [item["name"] for item in manifest["stepResults"]]
        != list(expected_steps(manifest["mode"]))
        or any(item["exitCode"] != 0 for item in manifest["stepResults"])
    ):
        raise RuntimeError("a partial or failed acceptance run cannot be finalized as passed")
    manifest.update(
        {
            "finishedAt": finished_at,
            "durationSeconds": duration_seconds,
            "status": status,
            "exitCode": exit_code,
            "failedAtStep": None if status == "passed" else failed_at_step,
            "terminalStep": (
                manifest.get("terminalStep")
                or (None if status == "passed" else failed_at_step)
            ),
            "finalized": True,
        }
    )
    manifest["selfSha256"] = _manifest_self_hash(manifest)
    atomic_write_private_json(path, manifest)
    return manifest


def _relative_manifest_path(path: Path) -> str:
    try:
        relative = path.absolute().relative_to(ROOT_DIR.absolute())
    except ValueError as exc:
        raise RuntimeError("acceptance run manifest must be inside the project") from exc
    if (
        relative.parts[:2] != ("logs", "acceptance")
        or not relative.name.endswith(".run.json")
        or ".." in relative.parts
    ):
        raise RuntimeError("acceptance run manifest must be logs/acceptance/*.json")
    return relative.as_posix()


def _run_evidence(
    manifest_path: Path,
    manifest: dict[str, Any],
    configuration: dict[str, Any],
) -> dict[str, Any]:
    run = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "kind": RUN_KIND,
        "runId": manifest["runId"],
        "mode": manifest["mode"],
        "runner": manifest["runner"],
        "plan": manifest["plan"],
        "stepResults": manifest["stepResults"],
        "failedAtStep": manifest["failedAtStep"],
        "terminalStep": manifest["terminalStep"],
        "manifest": {
            "schemaVersion": RUN_SCHEMA_VERSION,
            "sourcePath": _relative_manifest_path(manifest_path),
            "sourceSha256": sha256_file(manifest_path),
            "selfSha256": manifest["selfSha256"],
        },
    }
    if not run_record_valid(
        run,
        mode=manifest["mode"],
        overall_status=manifest["status"],
        overall_exit_code=manifest["exitCode"],
        configuration=configuration,
    ):
        raise RuntimeError("acceptance run manifest does not prove the claimed result")
    return run


def build_payload(output: Path, manifest_path: Path) -> dict[str, Any]:
    try:
        output_relative = output.absolute().relative_to(ROOT_DIR.absolute())
    except ValueError as exc:
        raise RuntimeError("acceptance evidence output must be inside the project") from exc
    if (
        output_relative.parts[:2] != ("logs", "acceptance")
        or output_relative.suffix != ".json"
        or output_relative.name.endswith(".run.json")
        or ".." in output_relative.parts
    ):
        raise RuntimeError("acceptance evidence output must be logs/acceptance/*.json")
    manifest = _validated_manifest(manifest_path, require_final=True)
    catalog, model = reviewed_catalog_model(manifest["catalogModelId"])
    configuration = acceptance_configuration(
        model, manifest["mode"], manifest["profile"]
    )
    current_validation_input = validation_input(catalog, model, configuration)
    if (
        manifest["status"] == "passed"
        and manifest["validationInput"] != current_validation_input
    ):
        raise RuntimeError("catalog validation inputs changed during the acceptance run")
    artifact = next(item for item in model["artifacts"] if item["role"] == "model")
    try:
        artifact_identity = local_artifact_identity(
            MODELS_DIR / model["modelDirectory"] / artifact["filename"],
            INTEGRITY_DIR / f"{model['id']}--{artifact['filename']}.sha256.stamp",
            expected_bytes=artifact["bytes"],
            expected_sha256=artifact["sha256"],
            project_root=ROOT_DIR,
        )
    except RuntimeError:
        artifact_identity = {
            "schemaVersion": 1,
            "verification": "unavailable",
            "requiresFullSha256Verification": True,
        }
    if (
        manifest["status"] == "passed"
        and artifact_identity.get("requiresFullSha256Verification") is not False
    ):
        raise RuntimeError(
            "passed acceptance evidence requires a current secure artifact integrity "
            "stamp; run the full SHA256 verifier"
        )
    container_name = os.environ.get("QWEN_CONTAINER_NAME", model["id"])
    inspected = command_json(["docker", "inspect", container_name])
    container = inspected[0] if isinstance(inspected, list) and inspected else {}
    mismatches = (
        runtime_mismatches(container, manifest["profile"])
        if container
        else ["missing"]
    )
    if manifest["status"] == "passed" and mismatches:
        raise RuntimeError(
            "live runtime does not match the canonical rendered Compose configuration: "
            + ", ".join(mismatches)
        )
    fingerprint = host_fingerprint()
    if not fingerprint:
        raise RuntimeError(
            "cannot create host-bound acceptance evidence without a machine id"
        )
    run = _run_evidence(manifest_path, manifest, configuration)
    git_commit = command_output(["git", "-C", str(ROOT_DIR), "rev-parse", "HEAD"])
    git_status = command_output(
        ["git", "-C", str(ROOT_DIR), "status", "--porcelain"]
    )
    payload = {
        "schemaVersion": 4,
        "evidenceId": output.stem,
        "mode": manifest["mode"],
        "profile": manifest["profile"],
        "status": manifest["status"],
        "exitCode": manifest["exitCode"],
        "failedAtStep": manifest["failedAtStep"],
        "terminalStep": manifest["terminalStep"],
        "startedAt": manifest["startedAt"],
        "finishedAt": manifest["finishedAt"],
        "durationSeconds": manifest["durationSeconds"],
        "gitCommit": git_commit or "uncommitted",
        "gitState": "clean" if git_commit and git_status == "" else "dirty",
        "catalogModelId": model["id"],
        "validationInput": current_validation_input,
        "run": run,
        "host": {
            "platform": platform.system().lower(),
            "environmentKind": environment_kind(),
            "architecture": platform.machine(),
            "ramGiB": total_ram_gib(),
            "gpus": gpu_inventory(),
            "fingerprintType": HOST_FINGERPRINT_TYPE,
            "fingerprint": fingerprint,
        },
        "artifact": {
            "filename": artifact["filename"],
            "bytes": artifact["bytes"],
            "sha256": artifact["sha256"],
            "modelRevision": model["modelRevision"],
            "artifactRevision": model["artifactRevision"],
            "integrityVerified": manifest["status"] == "passed",
            "localIdentity": artifact_identity,
        },
        "runtime": {
            "containerName": container_name,
            "configuredImage": (container.get("Config") or {}).get("Image"),
            "imageId": container.get("Image"),
            "containerConfigSha256": (
                live_runtime_sha256(container) if container else None
            ),
            "state": (container.get("State") or {}).get("Status"),
            "health": ((container.get("State") or {}).get("Health") or {}).get(
                "Status"
            ),
        },
        "configuration": configuration,
        "freshnessPolicy": {"maxAgeDays": 30, "futureSkewSeconds": 300},
        "privacy": (
            "synthetic acceptance traffic only; application-scoped hash of machine "
            "id; no raw machine id, hostname, prompt, response, tool arguments, "
            "credentials, or raw container environment"
        ),
    }
    payload["selfSha256"] = payload_sha256(payload)
    return payload


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_private_json(path, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run-start")
    start.add_argument("--output", type=Path, required=True)
    start.add_argument("--mode", choices=tuple(STEP_PLANS), required=True)
    start.add_argument("--profile", choices=("latency", "throughput"), default="latency")
    start.add_argument("--catalog-model-id", required=True)
    start.add_argument("--started-at", required=True)
    step = commands.add_parser("run-step")
    step.add_argument("--manifest", type=Path, required=True)
    step.add_argument("--name", required=True)
    step.add_argument("--started-at", required=True)
    step.add_argument("--finished-at", required=True)
    step.add_argument("--duration-seconds", type=int, required=True)
    step.add_argument("--exit-code", type=int, required=True)
    finish = commands.add_parser("run-finish")
    finish.add_argument("--manifest", type=Path, required=True)
    finish.add_argument("--finished-at", required=True)
    finish.add_argument("--duration-seconds", type=int, required=True)
    finish.add_argument("--exit-code", type=int, required=True)
    finish.add_argument("--failed-at-step", required=True)
    write = commands.add_parser("write")
    write.add_argument("--output", type=Path, required=True)
    write.add_argument("--run-manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "run-start":
        initialize_manifest(
            args.output,
            mode=args.mode,
            profile=args.profile,
            catalog_model_id=args.catalog_model_id,
            started_at=args.started_at,
        )
    elif args.command == "run-step":
        append_step(
            args.manifest,
            name=args.name,
            started_at=args.started_at,
            finished_at=args.finished_at,
            duration_seconds=args.duration_seconds,
            exit_code=args.exit_code,
        )
    elif args.command == "run-finish":
        finalize_manifest(
            args.manifest,
            finished_at=args.finished_at,
            duration_seconds=args.duration_seconds,
            exit_code=args.exit_code,
            failed_at_step=args.failed_at_step,
        )
    else:
        write_atomic(args.output, build_payload(args.output, args.run_manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
