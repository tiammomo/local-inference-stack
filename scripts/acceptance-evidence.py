#!/usr/bin/env python3
"""Write privacy-preserving, host-bound acceptance evidence atomically."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

try:
    from scripts.env_utils import atomic_write_private_json
    from scripts.runtime_identity import (
        acceptance_configuration,
        live_runtime_sha256,
        runtime_mismatches,
    )
except ModuleNotFoundError:  # Direct execution from scripts/.
    from env_utils import atomic_write_private_json
    from runtime_identity import (
        acceptance_configuration,
        live_runtime_sha256,
        runtime_mismatches,
    )


ROOT_DIR = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT_DIR / "catalog" / "models.json"
HOST_FINGERPRINT_TYPE = "machine-id-sha256-v1"
HOST_FINGERPRINT_CONTEXT = b"local-inference-stack.acceptance-host.v1\0"
MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))


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
    parser.add_argument("--profile", choices=("latency", "throughput"), default="latency")
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
    mismatches = runtime_mismatches(container, args.profile) if container else ["missing"]
    if args.status == "passed" and mismatches:
        raise RuntimeError(
            "live runtime does not match the canonical rendered Compose configuration: "
            + ", ".join(mismatches)
        )
    fingerprint = host_fingerprint()
    if not fingerprint:
        raise RuntimeError(
            "cannot create host-bound acceptance evidence without a machine id"
        )
    payload = {
        "schemaVersion": 4,
        "evidenceId": args.output.stem,
        "mode": args.mode,
        "profile": args.profile,
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
            "containerConfigSha256": (
                live_runtime_sha256(container) if container else None
            ),
            "state": (container.get("State") or {}).get("Status"),
            "health": ((container.get("State") or {}).get("Health") or {}).get(
                "Status"
            ),
        },
        "configuration": acceptance_configuration(model, args.mode, args.profile),
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


def main() -> int:
    args = parse_args()
    write_atomic(args.output, build_payload(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
