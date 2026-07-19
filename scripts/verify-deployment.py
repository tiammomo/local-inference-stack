#!/usr/bin/env python3
"""Verify that the live Qwen deployment matches the versioned manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.request import urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT_DIR / "deployments" / "qwen3.5-9b-rtx5070ti" / "manifest.json"
CONTRACT_PATH = ROOT_DIR / "contracts" / "local-qwen-provider-v1.json"
CONTAINER_NAME = "qwen35-9b-q5km"
LEGACY_PATH = Path("/home/tiammomo/projects/infra/models")


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
    with urlopen(url, timeout=10) as response:
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
    container = command_json("docker", "inspect", CONTAINER_NAME)[0]
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
    interfaces = manifest["interfaces"]
    configuration = manifest["configuration"]
    expected_root = str(ROOT_DIR)
    state = container.get("State", {})
    check("legacy path absent", LEGACY_PATH.exists() or LEGACY_PATH.is_symlink(), False)
    check("container running", state.get("Status"), "running")
    check("container healthy", state.get("Health", {}).get("Status"), "healthy")
    check("runtime health", health.get("status"), "ok")
    check("ModelPort live", modelport_health.get("status"), "ok")
    check("operations dashboard", dashboard_health.get("status"), "ok")
    check("runtime model alias", props.get("model_alias"), manifest["model"]["servedModelId"])
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
    binding = (host_config.get("PortBindings") or {}).get("8080/tcp", [{}])[0]
    check("diagnostic bind address", binding.get("HostIp"), "127.0.0.1")
    check("diagnostic port", binding.get("HostPort"), "18080")
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
        "dashboard baseline SHA256",
        sha256(ROOT_DIR / "dashboard" / "runtime-baseline.json"),
        configuration["dashboardBaselineSha256"],
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
