#!/usr/bin/env python3
"""Write privacy-preserving, host-bound acceptance evidence atomically."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT_DIR / "catalog" / "models.json"
CONTRACT_PATH = ROOT_DIR / "contracts" / "local-qwen-provider-v1.json"
MANIFEST_PATH = (
    ROOT_DIR / "deployments" / "qwen3.5-9b-rtx5070ti" / "manifest.json"
)
HOST_FINGERPRINT_TYPE = "machine-id-sha256-v1"
HOST_FINGERPRINT_CONTEXT = b"local-inference-stack.acceptance-host.v1\0"
MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def acceptance_configuration(model: dict[str, Any]) -> dict[str, str]:
    paths = {
        "composeSha256": ROOT_DIR / "compose.yaml",
        "contractSha256": CONTRACT_PATH,
        "catalogSha256": CATALOG_PATH,
        "modelManagerSha256": ROOT_DIR / "scripts" / "model-manager.py",
        "acceptanceSuiteSha256": ROOT_DIR / "scripts" / "acceptance-suite.sh",
        "acceptanceEvidenceWriterSha256": Path(__file__).resolve(),
        "deploymentLibrarySha256": ROOT_DIR / "scripts" / "lib" / "deployment.sh",
        "unitTestsSha256": ROOT_DIR / "scripts" / "unit-tests.sh",
        "verifyModelsSha256": ROOT_DIR / "scripts" / "verify-models.sh",
        "runtimeControllerSha256": ROOT_DIR / "scripts" / "runtime.sh",
        "smokeTestSha256": ROOT_DIR / "scripts" / "smoke-test.sh",
        "reasoningSmokeSha256": ROOT_DIR / "scripts" / "reasoning-smoke.sh",
    }
    configuration = {key: sha256(path) for key, path in paths.items()}
    configuration["manifestSha256"] = (
        sha256(MANIFEST_PATH)
        if model["id"] == "qwen35-9b-q5km"
        else "unvalidated-catalog-profile"
    )
    return configuration


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("quick", "standard", "full"), required=True)
    parser.add_argument("--status", choices=("passed", "failed"), required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--failed-at-step", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--finished-at", required=True)
    parser.add_argument("--duration-seconds", type=int, required=True)
    parser.add_argument("--catalog-model-id", required=True)
    return parser.parse_args()


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    model = next(
        item for item in catalog["models"] if item["id"] == args.catalog_model_id
    )
    artifact = next(
        item for item in model["artifacts"] if item["role"] == "model"
    )
    container_name = os.environ.get("QWEN_CONTAINER_NAME", args.catalog_model_id)
    inspected = command_json(["docker", "inspect", container_name])
    container = inspected[0] if isinstance(inspected, list) and inspected else {}
    fingerprint = host_fingerprint()
    if not fingerprint:
        raise RuntimeError(
            "cannot create host-bound acceptance evidence without a machine id"
        )
    payload = {
        "schemaVersion": 3,
        "evidenceId": args.output.stem,
        "mode": args.mode,
        "status": args.status,
        "exitCode": args.exit_code,
        "failedAtStep": args.failed_at_step if args.status == "failed" else None,
        "terminalStep": args.failed_at_step,
        "startedAt": args.started_at,
        "finishedAt": args.finished_at,
        "durationSeconds": args.duration_seconds,
        "gitCommit": command_output(
            ["git", "-C", str(ROOT_DIR), "rev-parse", "HEAD"]
        )
        or "uncommitted",
        "gitState": (
            "dirty"
            if command_output(
                ["git", "-C", str(ROOT_DIR), "status", "--porcelain"]
            )
            else "clean"
        ),
        "catalogModelId": args.catalog_model_id,
        "host": {
            "platform": platform.system().lower(),
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
            "integrityVerified": args.status == "passed",
        },
        "runtime": {
            "containerName": container_name,
            "configuredImage": (container.get("Config") or {}).get("Image"),
            "imageId": container.get("Image"),
        },
        "configuration": acceptance_configuration(model),
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
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise RuntimeError(f"unsafe evidence directory: {path.parent}")
    parent_stat = path.parent.stat()
    if parent_stat.st_uid != os.getuid():
        raise RuntimeError(f"evidence directory has a different owner: {path.parent}")
    if parent_stat.st_mode & 0o077:
        path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    write_atomic(args.output, build_payload(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
