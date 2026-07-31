#!/usr/bin/env python3
"""Canonical identities for rendered Compose configuration and live runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

try:
    from scripts.env_utils import is_private_regular_file, parse_env_file
except ModuleNotFoundError:  # Direct execution from scripts/.
    from env_utils import is_private_regular_file, parse_env_file


ROOT_DIR = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT_DIR / "compose.yaml"
LOCAL_PROFILE = ROOT_DIR / "profiles" / "deployment.local.env"
CATALOG_PATH = ROOT_DIR / "catalog" / "models.json"
MANIFEST_PATH = (
    ROOT_DIR / "deployments" / "qwen3.5-9b-rtx5070ti" / "manifest.json"
)
COMPOSE_VARIABLE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)")


def canonical_sha256(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def acceptance_configuration(
    model: dict[str, Any],
    mode: str = "quick",
    profile: str = "latency",
) -> dict[str, str]:
    if mode not in {"quick", "standard", "full"}:
        raise ValueError(f"unsupported acceptance mode: {mode}")
    common = {
        "composeSha256": COMPOSE_PATH,
        "contractSha256": ROOT_DIR / "contracts" / "local-qwen-provider-v1.json",
        "catalogSha256": CATALOG_PATH,
        "modelManagerSha256": ROOT_DIR / "scripts" / "model-manager.py",
        "acceptanceSuiteSha256": ROOT_DIR / "scripts" / "acceptance-suite.sh",
        "acceptanceEvidenceWriterSha256": ROOT_DIR / "scripts" / "acceptance-evidence.py",
        "runtimeIdentitySha256": Path(__file__).resolve(),
        "localHttpSha256": ROOT_DIR / "scripts" / "local_http.py",
        "environmentUtilsSha256": ROOT_DIR / "scripts" / "env_utils.py",
        "deploymentLibrarySha256": ROOT_DIR / "scripts" / "lib" / "deployment.sh",
        "unitTestsSha256": ROOT_DIR / "scripts" / "unit-tests.sh",
        "verifyModelsSha256": ROOT_DIR / "scripts" / "verify-models.sh",
        "runtimeControllerSha256": ROOT_DIR / "scripts" / "runtime.sh",
        "runtimeSupervisorSha256": ROOT_DIR / "scripts" / "runtime-supervisor.py",
        "pythonVersionSha256": ROOT_DIR / ".python-version",
        "smokeTestSha256": ROOT_DIR / "scripts" / "smoke-test.sh",
        "reasoningSmokeSha256": ROOT_DIR / "scripts" / "reasoning-smoke.sh",
    }
    standard = {
        "compatibilityCheckSha256": ROOT_DIR / "scripts" / "compatibility-check.py",
        "dashboardSmokeSha256": ROOT_DIR / "scripts" / "dashboard-smoke.py",
        "operationsDashboardSha256": ROOT_DIR / "scripts" / "operations-dashboard.py",
        "operationsReportSha256": ROOT_DIR / "scripts" / "operations-report.py",
        "modelportSmokeSha256": ROOT_DIR / "scripts" / "modelport-smoke.sh",
        "modelportReasoningSmokeSha256": (
            ROOT_DIR / "scripts" / "modelport-reasoning-smoke.py"
        ),
        "modelportTokenCountSmokeSha256": (
            ROOT_DIR / "scripts" / "modelport-token-count-smoke.sh"
        ),
        "modelportContextAdmissionSha256": (
            ROOT_DIR / "scripts" / "modelport-context-admission-smoke.sh"
        ),
        "qualityEvalSha256": ROOT_DIR / "scripts" / "quality-eval.py",
        "qualityCasesSha256": ROOT_DIR / "quality" / "cases.json",
        "toolWorkflowEvalSha256": ROOT_DIR / "scripts" / "tool-workflow-eval.py",
        "toolWorkflowCasesSha256": ROOT_DIR / "quality" / "tool-workflows.json",
        "toolResilienceCasesSha256": (
            ROOT_DIR / "quality" / "tool-resilience-workflows.json"
        ),
    }
    full = {
        "contextAcceptanceSha256": ROOT_DIR / "scripts" / "context-acceptance.py",
        "modelportContextAcceptanceSha256": (
            ROOT_DIR / "scripts" / "modelport-context-acceptance.sh"
        ),
        "decodeBenchmarkSha256": ROOT_DIR / "scripts" / "decode-benchmark.py",
        "concurrencyBenchmarkSha256": (
            ROOT_DIR / "scripts" / "concurrency-benchmark.py"
        ),
    }
    paths = dict(common)
    if mode in {"standard", "full"}:
        paths.update(standard)
    if mode == "full":
        paths.update(full)
    configuration = {key: sha256_file(path) for key, path in paths.items()}
    profile_path = ROOT_DIR / "profiles" / f"{profile}.env"
    configuration.update(
        {
            "runtimeProfile": profile,
            "runtimeProfileSha256": sha256_file(profile_path),
            "deploymentProfileSha256": (
                sha256_file(LOCAL_PROFILE) if LOCAL_PROFILE.is_file() else "absent"
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
            }
            for mount in container.get("Mounts", [])
            if mount.get("Destination") in {"/models", "/cache"}
        ),
        key=lambda item: str(item["destination"]),
    )
    port_bindings = host.get("PortBindings") or {}
    return {
        "image": config.get("Image"),
        "imageId": container.get("Image"),
        "user": config.get("User"),
        "command": config.get("Cmd") or [],
        "environment": sorted(
            value
            for value in (config.get("Env") or [])
            if value.startswith(("HOME=", "CUDA_CACHE_PATH="))
        ),
        "readOnly": host.get("ReadonlyRootfs"),
        "capDrop": sorted(host.get("CapDrop") or []),
        "securityOpt": sorted(host.get("SecurityOpt") or []),
        "pidsLimit": host.get("PidsLimit"),
        "logging": host.get("LogConfig") or {},
        "portBindings": port_bindings,
        "mounts": mounts,
    }


def live_runtime_sha256(container: dict[str, Any]) -> str:
    return canonical_sha256(normalized_live_runtime(container))


def expected_runtime_subset(profile: str = "latency") -> dict[str, Any]:
    service = rendered_compose(profile)["services"]["qwen35"]
    ports = service.get("ports") or []
    volumes = sorted(
        (
            {
                "type": volume.get("type"),
                "source": str(Path(volume.get("source", "")).resolve()),
                "destination": volume.get("target"),
                "rw": not bool(volume.get("read_only")),
            }
            for volume in service.get("volumes", [])
            if volume.get("target") in {"/models", "/cache"}
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
    return {
        "image": service.get("image"),
        "user": service.get("user"),
        "command": service.get("command") or [],
        "environment": sorted(
            f"{key}={value}"
            for key, value in (service.get("environment") or {}).items()
            if key in {"HOME", "CUDA_CACHE_PATH"}
        ),
        "readOnly": service.get("read_only"),
        "capDrop": sorted(service.get("cap_drop") or []),
        "securityOpt": sorted(service.get("security_opt") or []),
        "pidsLimit": service.get("pids_limit"),
        "logging": {
            "Type": logging.get("driver"),
            "Config": {key: str(value) for key, value in (logging.get("options") or {}).items()},
        },
        "portBindings": port_bindings,
        "mounts": volumes,
    }


def runtime_mismatches(container: dict[str, Any], profile: str = "latency") -> list[str]:
    expected = expected_runtime_subset(profile)
    actual = normalized_live_runtime(container)
    checks = {
        "image": actual["image"],
        "user": actual["user"],
        "command": actual["command"],
        "environment": actual["environment"],
        "readOnly": actual["readOnly"],
        "capDrop": actual["capDrop"],
        "securityOpt": actual["securityOpt"],
        "pidsLimit": actual["pidsLimit"],
        "logging": actual["logging"],
        "portBindings": actual["portBindings"],
        "mounts": actual["mounts"],
    }
    return [key for key, value in checks.items() if value != expected[key]]
