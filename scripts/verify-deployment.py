#!/usr/bin/env python3
"""Verify that the live Qwen deployment matches the versioned manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

try:
    from scripts.local_http import direct_urlopen
except ModuleNotFoundError:
    from local_http import direct_urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT_DIR / "deployments" / "qwen3.5-9b-rtx5070ti" / "manifest.json"
CONTRACT_PATH = ROOT_DIR / "contracts" / "local-qwen-provider-v1.json"
CATALOG_PATH = ROOT_DIR / "catalog" / "models.json"
CONTAINER_NAME = "qwen35-9b-q5km"
MODELPORT_CONTAINER_NAME = "modelport-modelport-1"
MODELPORT_POSTGRES_CONTAINER_NAME = "modelport-postgres-1"
MODELPORT_DASHBOARD_CONTAINER_NAME = "modelport-dashboard-1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_json(*command: str) -> Any:
    output = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=30
    ).stdout
    return json.loads(output)


def get_json(url: str) -> Any:
    with direct_urlopen(url, timeout=10) as response:
        return json.load(response)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="compare the running deployment with its pinned manifest"
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_model = next(
        model
        for model in catalog["models"]
        if model["id"] == manifest["model"]["catalogId"]
    )
    catalog_artifact = next(
        artifact
        for artifact in catalog_model["artifacts"]
        if artifact["role"] == "model"
    )
    container = command_json("docker", "inspect", CONTAINER_NAME)[0]
    modelport_container = command_json(
        "docker", "inspect", MODELPORT_CONTAINER_NAME
    )[0]
    modelport_postgres_container = command_json(
        "docker", "inspect", MODELPORT_POSTGRES_CONTAINER_NAME
    )[0]
    modelport_dashboard_container = command_json(
        "docker", "inspect", MODELPORT_DASHBOARD_CONTAINER_NAME
    )[0]
    props = get_json("http://127.0.0.1:18080/props")
    slots = get_json("http://127.0.0.1:18080/slots")
    health = get_json("http://127.0.0.1:18080/health")
    modelport_health = get_json("http://127.0.0.1:38082/livez")
    dashboard_health = get_json("http://127.0.0.1:33004/api/health")
    checks: list[dict[str, Any]] = []

    def check(name: str, actual: Any, expected: Any, passed: bool | None = None) -> None:
        checks.append(
            {
                "name": name,
                "passed": actual == expected if passed is None else passed,
                "actual": actual,
                "expected": expected,
            }
        )

    runtime = manifest["runtime"]
    model = manifest["model"]
    gateway = manifest["gateway"]
    interfaces = manifest["interfaces"]
    configuration = manifest["configuration"]
    expected_root = str(ROOT_DIR)
    state = container.get("State", {})
    check("container running", state.get("Status"), "running")
    check("container healthy", state.get("Health", {}).get("Status"), "healthy")
    check("runtime health", health.get("status"), "ok")
    check("ModelPort live", modelport_health.get("status"), "ok")
    check("operations dashboard", dashboard_health.get("status"), "ok")
    check("runtime model alias", props.get("model_alias"), manifest["model"]["servedModelId"])
    check("catalog model ID", catalog_model["id"], model["catalogId"])
    check(
        "official model repository",
        catalog_model["modelRepository"],
        model["officialRepository"],
    )
    check("official model revision", catalog_model["modelRevision"], model["modelRevision"])
    check(
        "artifact repository",
        catalog_model["artifactRepository"],
        model["artifactRepository"],
    )
    check(
        "artifact revision",
        catalog_model["artifactRevision"],
        model["artifactRevision"],
    )
    check("artifact filename", catalog_artifact["filename"], model["artifactFilename"])
    check("artifact byte size", catalog_artifact["bytes"], model["artifactBytes"])
    check("artifact SHA256", catalog_artifact["sha256"], model["artifactSha256"])
    check("model license SPDX", catalog_model["license"]["spdx"], model["licenseSpdx"])
    check(
        "model license review required",
        catalog_model["license"]["reviewRequired"],
        model["licenseReviewRequired"],
    )
    check("runtime build", props.get("build_info"), runtime["engineBuild"])
    check("slot count", len(slots), runtime["slots"])
    check(
        "context per slot",
        [slot.get("n_ctx") for slot in slots],
        [runtime["contextTokens"]] * runtime["slots"],
    )
    check("container image", container.get("Config", {}).get("Image"), runtime["containerImage"])
    check("container name", container.get("Name", "").lstrip("/"), interfaces["containerName"])
    check("unprivileged runtime user", container.get("Config", {}).get("User"), "1000:1000")
    host_config = container.get("HostConfig", {})
    check("read-only root filesystem", host_config.get("ReadonlyRootfs"), True)
    check(
        "no-new-privileges",
        any(
            option.startswith("no-new-privileges")
            for option in (host_config.get("SecurityOpt") or [])
        ),
        True,
    )
    check("all capabilities dropped", host_config.get("CapDrop") or [], ["ALL"])
    runtime_log = host_config.get("LogConfig", {}) or {}
    check("runtime log driver", runtime_log.get("Type"), runtime["logging"]["driver"])
    check(
        "runtime log max size",
        (runtime_log.get("Config") or {}).get("max-size"),
        runtime["logging"]["maxSize"],
    )
    check(
        "runtime log max files",
        (runtime_log.get("Config") or {}).get("max-file"),
        str(runtime["logging"]["maxFiles"]),
    )
    binding = (host_config.get("PortBindings") or {}).get("8080/tcp", [{}])[0]
    check("diagnostic bind address", binding.get("HostIp"), "127.0.0.1")
    check("diagnostic port", binding.get("HostPort"), "18080")

    modelport_state = modelport_container.get("State", {})
    check("ModelPort container running", modelport_state.get("Status"), "running")
    check(
        "ModelPort container healthy",
        modelport_state.get("Health", {}).get("Status"),
        "healthy",
    )
    check(
        "ModelPort container name",
        modelport_container.get("Name", "").lstrip("/"),
        gateway["containerName"],
    )
    check(
        "ModelPort image ID",
        modelport_container.get("Image"),
        gateway["containerImageId"],
    )
    check(
        "ModelPort configured image",
        modelport_container.get("Config", {}).get("Image"),
        gateway["containerImage"],
    )
    modelport_labels = modelport_container.get("Config", {}).get("Labels", {}) or {}
    check(
        "ModelPort source revision label",
        modelport_labels.get("org.opencontainers.image.revision"),
        gateway["sourceCommit"],
    )
    check(
        "ModelPort source state label",
        modelport_labels.get("io.modelport.source-state"),
        gateway["sourceState"],
    )
    check(
        "ModelPort unprivileged user",
        modelport_container.get("Config", {}).get("User"),
        "modelport",
    )
    modelport_host = modelport_container.get("HostConfig", {})
    check(
        "ModelPort read-only root filesystem",
        modelport_host.get("ReadonlyRootfs"),
        True,
    )
    check(
        "ModelPort privileged mode disabled",
        modelport_host.get("Privileged"),
        False,
    )
    check(
        "ModelPort no-new-privileges",
        any(
            option.startswith("no-new-privileges")
            for option in (modelport_host.get("SecurityOpt") or [])
        ),
        True,
    )
    check(
        "ModelPort capabilities dropped",
        modelport_host.get("CapDrop") or [],
        ["ALL"],
    )
    for label, inspected in (
        ("ModelPort", modelport_container),
        ("ModelPort PostgreSQL", modelport_postgres_container),
        ("ModelPort Dashboard", modelport_dashboard_container),
    ):
        log_config = inspected.get("HostConfig", {}).get("LogConfig", {}) or {}
        check(
            f"{label} log driver",
            log_config.get("Type"),
            gateway["logging"]["driver"],
        )
        check(
            f"{label} log max size",
            (log_config.get("Config") or {}).get("max-size"),
            gateway["logging"]["maxSize"],
        )
        check(
            f"{label} log max files",
            (log_config.get("Config") or {}).get("max-file"),
            str(gateway["logging"]["maxFiles"]),
        )
    modelport_binding = (modelport_host.get("PortBindings") or {}).get(
        "38082/tcp", [{}]
    )[0]
    check("ModelPort bind address", modelport_binding.get("HostIp"), "127.0.0.1")
    check("ModelPort port", modelport_binding.get("HostPort"), "38082")

    for unit in manifest["operations"]["enabledUnits"]:
        enabled = subprocess.run(
            ["systemctl", "--user", "is-enabled", unit],
            capture_output=True,
            text=True,
            timeout=10,
        )
        active = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=10,
        )
        check(f"{unit} enabled", enabled.stdout.strip(), "enabled")
        check(f"{unit} active", active.stdout.strip(), "active")

    backup_dir = ROOT_DIR / manifest["operations"]["backupDirectory"]
    backup_archives = [
        path
        for path in backup_dir.glob("modelport-*.tar.gz")
        if path.is_file() and not path.is_symlink()
    ]
    check("ModelPort backup available", bool(backup_archives), True)
    if backup_archives:
        latest_backup = max(backup_archives, key=lambda path: path.stat().st_mtime_ns)
        backup_age_hours = round((time.time() - latest_backup.stat().st_mtime) / 3600, 3)
        backup_permissions = (
            backup_dir.stat().st_mode & 0o077 == 0
            and latest_backup.stat().st_mode & 0o077 == 0
        )
        check(
            "ModelPort backup freshness",
            backup_age_hours,
            f"<={manifest['operations']['backupMaxAgeHours']}h",
            backup_age_hours <= manifest["operations"]["backupMaxAgeHours"],
        )
        check("ModelPort backup permissions", backup_permissions, True)
    command = container.get("Config", {}).get("Cmd", []) or []
    check("KV snapshot path enabled", "--slot-save-path" in command, True)
    if "--slot-save-path" in command:
        slot_path_index = command.index("--slot-save-path") + 1
        check("KV snapshot path", command[slot_path_index], "/cache/slots")

    mounts = {
        mount.get("Destination"): mount.get("Source")
        for mount in container.get("Mounts", [])
    }
    check("model mount", mounts.get("/models"), f"{expected_root}/models/qwen3.5-9b")
    check("cache mount", mounts.get("/cache"), f"{expected_root}/cache")
    labels = container.get("Config", {}).get("Labels", {}) or {}
    check("compose working directory", labels.get("com.docker.compose.project.working_dir"), expected_root)
    check("compose file", labels.get("com.docker.compose.project.config_files"), f"{expected_root}/compose.yaml")

    check("compose SHA256", sha256(ROOT_DIR / "compose.yaml"), configuration["composeSha256"])
    check(
        "latency profile SHA256",
        sha256(ROOT_DIR / "profiles" / "latency.env"),
        configuration["latencyProfileSha256"],
    )
    check(
        "candidate profile SHA256",
        sha256(ROOT_DIR / "profiles" / "candidate.env"),
        configuration["candidateProfileSha256"],
    )
    check("provider contract SHA256", sha256(CONTRACT_PATH), configuration["providerContractSha256"])
    check(
        "quality suite SHA256",
        sha256(ROOT_DIR / "quality" / "cases.json"),
        configuration["qualitySuiteSha256"],
    )
    check(
        "acceptance suite SHA256",
        sha256(ROOT_DIR / "scripts" / "acceptance-suite.sh"),
        configuration["acceptanceSuiteSha256"],
    )
    check(
        "model catalog SHA256",
        sha256(ROOT_DIR / "catalog" / "models.json"),
        configuration["modelCatalogSha256"],
    )
    check(
        "model manager SHA256",
        sha256(ROOT_DIR / "scripts" / "model-manager.py"),
        configuration["modelManagerSha256"],
    )
    check(
        "compatibility checker SHA256",
        sha256(ROOT_DIR / "scripts" / "compatibility-check.py"),
        configuration["compatibilityCheckSha256"],
    )
    check(
        "Tool workflow suite SHA256",
        sha256(ROOT_DIR / "quality" / "tool-workflows.json"),
        configuration["toolWorkflowSuiteSha256"],
    )
    check(
        "Tool workflow harness SHA256",
        sha256(ROOT_DIR / "scripts" / "tool-workflow-eval.py"),
        configuration["toolWorkflowHarnessSha256"],
    )
    check(
        "Tool resilience suite SHA256",
        sha256(ROOT_DIR / "quality" / "tool-resilience-workflows.json"),
        configuration["toolResilienceSuiteSha256"],
    )
    check(
        "dashboard baseline SHA256",
        sha256(ROOT_DIR / "dashboard" / "runtime-baseline.json"),
        configuration["dashboardBaselineSha256"],
    )
    check(
        "dashboard application SHA256",
        sha256(ROOT_DIR / "dashboard" / "app.js"),
        configuration["dashboardApplicationSha256"],
    )
    check(
        "dashboard document SHA256",
        sha256(ROOT_DIR / "dashboard" / "index.html"),
        configuration["dashboardDocumentSha256"],
    )
    check(
        "dashboard server SHA256",
        sha256(ROOT_DIR / "scripts" / "operations-dashboard.py"),
        configuration["dashboardServerSha256"],
    )
    check(
        "dashboard smoke SHA256",
        sha256(ROOT_DIR / "scripts" / "dashboard-smoke.py"),
        configuration["dashboardSmokeSha256"],
    )
    check(
        "operations report SHA256",
        sha256(ROOT_DIR / "scripts" / "operations-report.py"),
        configuration["operationsReportSha256"],
    )
    check(
        "backup orchestrator SHA256",
        sha256(ROOT_DIR / "scripts" / "modelport-backup.sh"),
        configuration["backupOrchestratorSha256"],
    )
    check(
        "soak gate SHA256",
        sha256(ROOT_DIR / "scripts" / "soak-check.py"),
        configuration["soakCheckSha256"],
    )
    check(
        "systemd installer SHA256",
        sha256(ROOT_DIR / "scripts" / "install-user-services.py"),
        configuration["systemdInstallerSha256"],
    )
    check(
        "runtime controller SHA256",
        sha256(ROOT_DIR / "scripts" / "runtime.sh"),
        configuration["runtimeControllerSha256"],
    )
    check(
        "runtime supervisor SHA256",
        sha256(ROOT_DIR / "scripts" / "runtime-supervisor.py"),
        configuration["runtimeSupervisorSha256"],
    )
    check(
        "Python version pin SHA256",
        sha256(ROOT_DIR / ".python-version"),
        configuration["pythonVersionSha256"],
    )
    check(
        "candidate runtime SHA256",
        sha256(ROOT_DIR / "scripts" / "candidate-runtime.sh"),
        configuration["candidateRuntimeSha256"],
    )
    check(
        "release candidate SHA256",
        sha256(ROOT_DIR / "scripts" / "release-candidate.sh"),
        configuration["releaseCandidateSha256"],
    )
    check(
        "release check SHA256",
        sha256(ROOT_DIR / "scripts" / "release-check.sh"),
        configuration["releaseCheckSha256"],
    )
    check(
        "deployment library SHA256",
        sha256(ROOT_DIR / "scripts" / "lib" / "deployment.sh"),
        configuration["deploymentLibrarySha256"],
    )
    check(
        "deployment verifier SHA256",
        sha256(ROOT_DIR / "scripts" / "verify-deployment.py"),
        configuration["deploymentVerifierSha256"],
    )
    check(
        "acceptance evidence writer SHA256",
        sha256(ROOT_DIR / "scripts" / "acceptance-evidence.py"),
        configuration["acceptanceEvidenceWriterSha256"],
    )
    check("contract provider", contract.get("provider"), interfaces["modelportProvider"])
    check(
        "contract served model",
        contract.get("runtime", {}).get("servedModelId"),
        manifest["model"]["servedModelId"],
    )
    check(
        "contract context limit",
        contract.get("limits", {}).get("contextTokens"),
        runtime["contextTokens"],
    )
    check(
        "contract reasoning input limit",
        contract.get("limits", {}).get("recommendedReasoningInputTokens"),
        runtime["recommendedReasoningInputTokens"],
    )

    integrity = subprocess.run(
        [str(ROOT_DIR / "scripts" / "verify-models.sh"), "--active", "--cached"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    check(
        "active model integrity",
        integrity.stdout.strip() or integrity.stderr.strip(),
        "pinned SHA256",
        integrity.returncode == 0,
    )

    failed = [item for item in checks if not item["passed"]]
    result = {
        "schemaVersion": 1,
        "deploymentId": manifest["deploymentId"],
        "status": "passed" if not failed else "failed",
        "checks": checks,
        "summary": {"passed": len(checks) - len(failed), "failed": len(failed)},
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            marker = "PASS" if item["passed"] else "FAIL"
            print(f"[{marker}] {item['name']}: {item['actual']}")
        print(
            f"\nDeployment verification {result['status']}: "
            f"{result['summary']['passed']} passed, {result['summary']['failed']} failed"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
