#!/usr/bin/env python3
"""Assess a host and safely materialize a catalog-backed local deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT_DIR / "catalog" / "models.json"
LOCAL_PROFILE = ROOT_DIR / "profiles" / "deployment.local.env"
MODELS_DIR = ROOT_DIR / "models"
INTEGRITY_DIR = ROOT_DIR / "cache" / "integrity"


def load_catalog() -> dict[str, Any]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if catalog.get("schemaVersion") != 1 or not catalog.get("models"):
        raise SystemExit(f"unsupported or empty catalog: {CATALOG_PATH}")
    ids: set[str] = set()
    for model in catalog["models"]:
        model_id = model.get("id", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]+", model_id) or model_id in ids:
            raise SystemExit(f"invalid or duplicate model id in catalog: {model_id!r}")
        ids.add(model_id)
        directory = Path(model.get("modelDirectory", ""))
        if directory.is_absolute() or ".." in directory.parts or len(directory.parts) != 1:
            raise SystemExit(f"unsafe model directory for {model_id}: {directory}")
        primaries = [item for item in model.get("artifacts", []) if item.get("role") == "model"]
        if len(primaries) != 1:
            raise SystemExit(f"{model_id} must define exactly one primary model artifact")
        for artifact in model["artifacts"]:
            filename = Path(artifact.get("filename", ""))
            url = urlparse(artifact.get("url", ""))
            if filename.is_absolute() or len(filename.parts) != 1 or filename.name != str(filename):
                raise SystemExit(f"unsafe artifact filename for {model_id}: {filename}")
            if url.scheme != "https" or url.hostname != "huggingface.co":
                raise SystemExit(f"unapproved artifact URL for {model_id}: {url.geturl()}")
            if not re.fullmatch(r"[0-9a-f]{64}", artifact.get("sha256", "")):
                raise SystemExit(f"invalid SHA256 for {model_id}/{filename}")
            if not isinstance(artifact.get("bytes"), int) or artifact["bytes"] <= 0:
                raise SystemExit(f"invalid artifact size for {model_id}/{filename}")
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
            memory_gib = round(float(fields[2]) / 1024, 1)
        except ValueError:
            continue
        gpus.append(
            {
                "index": int(fields[0]),
                "name": fields[1],
                "vramGiB": memory_gib,
                "driver": fields[3],
            }
        )
    return gpus


def host_assessment(vram_override: float | None, ram_override: float | None) -> dict[str, Any]:
    gpus = gpu_inventory()
    if vram_override is not None:
        gpus = [{"index": 0, "name": "override", "vramGiB": vram_override, "driver": "override"}]
    ram_gib = ram_override if ram_override is not None else total_ram_gib()
    disk = shutil.disk_usage(ROOT_DIR)
    docker_version = command_output(["docker", "version", "--format", "{{.Server.Version}}"])
    compose_version = command_output(["docker", "compose", "version", "--short"])
    runtimes = command_output(["docker", "info", "--format", "{{json .Runtimes}}"])
    return {
        "platform": sys.platform,
        "architecture": os.uname().machine,
        "gpus": gpus,
        "totalVramGiB": round(sum(gpu["vramGiB"] for gpu in gpus), 1),
        "largestGpuVramGiB": max((gpu["vramGiB"] for gpu in gpus), default=0),
        "ramGiB": round(ram_gib, 1),
        "freeDiskGiB": round(disk.free / 1024**3, 1),
        "docker": {"available": docker_version is not None, "version": docker_version},
        "dockerCompose": {"available": compose_version is not None, "version": compose_version},
        "nvidiaContainerRuntime": bool(runtimes and '"nvidia"' in runtimes),
    }


def fits(model: dict[str, Any], host: dict[str, Any]) -> bool:
    requirements = model["requirements"]
    return (
        host.get("platform", "linux") == "linux"
        and host.get("architecture", "x86_64") in {"x86_64", "amd64"}
        and host["totalVramGiB"] >= requirements["minVramGiB"]
        and host["ramGiB"] >= requirements["minRamGiB"]
        and host["freeDiskGiB"] >= requirements["minFreeDiskGiB"]
    )


def recommend(catalog: dict[str, Any], host: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [model for model in catalog["models"] if fits(model, host)]
    if not candidates:
        return None
    return max(candidates, key=lambda model: model["requirements"]["minVramGiB"])


def validated_on_host(model: dict[str, Any] | None, host: dict[str, Any]) -> bool:
    if not model or model.get("status") != "validated" or "validatedHardware" not in model:
        return False
    signature = model["validatedHardware"]
    return (
        len(host["gpus"]) == signature["gpuCount"]
        and all(gpu["name"] == signature["gpuName"] for gpu in host["gpus"])
        and host["largestGpuVramGiB"] >= signature["minVramGiB"]
        and host["ramGiB"] >= signature["minRamGiB"]
    )


def caveats(host: dict[str, Any], model: dict[str, Any] | None) -> list[str]:
    notes: list[str] = []
    if not host["gpus"]:
        notes.append("No NVIDIA GPU was detected; automatic deployment is intentionally disabled.")
    if len(host["gpus"]) > 1:
        notes.append("Multi-GPU capacity is an estimate; review tensor split and interconnect before deploy.")
    if not host["docker"]["available"]:
        notes.append("Docker Engine is unavailable.")
    if not host["dockerCompose"]["available"]:
        notes.append("Docker Compose v2 is unavailable.")
    if not host.get("nvidiaContainerRuntime", False):
        notes.append("Docker does not report an NVIDIA container runtime.")
    if host.get("platform") != "linux" or host.get("architecture") not in {"x86_64", "amd64"}:
        notes.append("Automatic deployment currently supports Linux/WSL x86_64 only.")
    if model and not validated_on_host(model, host):
        notes.append("This exact host is not a recorded validation signature; treat the profile as estimated until acceptance passes.")
    if model and host["largestGpuVramGiB"] < model["requirements"]["minVramGiB"]:
        notes.append("No single GPU meets the minimum; this recommendation assumes reviewed multi-GPU offload.")
    return notes


def plan_payload(args: argparse.Namespace, catalog: dict[str, Any]) -> dict[str, Any]:
    host = host_assessment(args.vram_gib, args.ram_gib)
    selected = model_by_id(catalog, args.model) if args.model else recommend(catalog, host)
    hardware_fits = bool(selected and fits(selected, host))
    ready = bool(
        hardware_fits
        and host["docker"]["available"]
        and host["dockerCompose"]["available"]
        and host["nvidiaContainerRuntime"]
    )
    return {
        "schemaVersion": 1,
        "mode": "read-only-plan",
        "catalogUpdatedAt": catalog["updatedAt"],
        "host": host,
        "recommendation": selected,
        "evidenceStatus": "validated-on-this-host" if validated_on_host(selected, host) else "estimated-on-this-host",
        "fits": hardware_fits,
        "readyToDeploy": ready,
        "caveats": caveats(host, selected),
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


def print_plan(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    host = payload["host"]
    print("Host assessment (read-only)")
    if host["gpus"]:
        for gpu in host["gpus"]:
            print(f"  GPU {gpu['index']}: {gpu['name']} ({gpu['vramGiB']} GiB)")
    else:
        print("  GPU: no NVIDIA GPU detected")
    print(f"  RAM: {host['ramGiB']} GiB; free disk: {host['freeDiskGiB']} GiB")
    recommendation = payload["recommendation"]
    if recommendation:
        req = recommendation["requirements"]
        print(
            f"Recommendation: {recommendation['id']} [{payload['evidenceStatus']}] "
            f"ctx={recommendation['runtime']['contextTokens']} "
            f"(minimum {req['minVramGiB']} GiB VRAM / {req['minRamGiB']} GiB RAM)"
        )
    else:
        print("Recommendation: none; this catalog only automates NVIDIA CUDA hosts with >=2 GiB VRAM")
    for note in payload["caveats"]:
        print(f"  NOTE: {note}")
    if payload["nextCommands"]:
        print("No state was changed. After reviewing size, status, license, and source:")
        for command in payload["nextCommands"]:
            print(f"  {command}")


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
    INTEGRITY_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{stamp_path.name}.", dir=INTEGRITY_DIR, text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(expected_stamp + "\n")
        temporary.replace(stamp_path)
    finally:
        temporary.unlink(missing_ok=True)
    return False


def download_model(args: argparse.Namespace, catalog: dict[str, Any]) -> None:
    confirmation_required(args, "download")
    model = model_by_id(catalog, args.model)
    destination = model_path(model)
    destination.mkdir(parents=True, exist_ok=True)
    artifacts = [artifact for artifact in model["artifacts"] if artifact["required"] or args.all_artifacts]
    for artifact in artifacts:
        final = destination / artifact["filename"]
        if final.is_file():
            verify_artifact(final, artifact)
            print(f"verified existing artifact: {final}")
            continue
        partial = final.with_suffix(final.suffix + ".part")
        print(f"downloading {artifact['bytes'] / 1024**3:.2f} GiB: {artifact['filename']}")
        try:
            subprocess.run(
                [
                    "curl", "--fail", "--location", "--retry", "8", "--retry-all-errors",
                    "--continue-at", "-", "--output", str(partial), artifact["url"],
                ],
                check=True,
            )
            verify_artifact(partial, artifact)
            partial.replace(final)
        except (subprocess.CalledProcessError, OSError):
            print(f"partial download retained for safe resume: {partial}", file=sys.stderr)
            raise
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
    LOCAL_PROFILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{LOCAL_PROFILE.name}.", dir=LOCAL_PROFILE.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(deployment_env(model))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(LOCAL_PROFILE)
        LOCAL_PROFILE.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="list reviewed catalog entries")
    list_parser.add_argument("--json", action="store_true")
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
