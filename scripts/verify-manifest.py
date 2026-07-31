#!/usr/bin/env python3
"""Verify the deployment manifest's repository-local configuration digests."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable


ROOT_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    ROOT_DIR / "deployments" / "qwen3.5-9b-rtx5070ti" / "manifest.json"
)
CONFIGURATION_FILES = {
    "pythonVersionSha256": ".python-version",
    "composeSha256": "compose.yaml",
    "latencyProfileSha256": "profiles/latency.env",
    "candidateProfileSha256": "profiles/candidate.env",
    "providerContractSha256": "contracts/local-qwen-provider-v1.json",
    "qualitySuiteSha256": "quality/cases.json",
    "acceptanceSuiteSha256": "scripts/acceptance-suite.sh",
    "compatibilityCheckSha256": "scripts/compatibility-check.py",
    "modelCatalogSha256": "catalog/models.json",
    "modelManagerSha256": "scripts/model-manager.py",
    "toolWorkflowSuiteSha256": "quality/tool-workflows.json",
    "toolWorkflowHarnessSha256": "scripts/tool-workflow-eval.py",
    "toolResilienceSuiteSha256": "quality/tool-resilience-workflows.json",
    "dashboardBaselineSha256": "dashboard/runtime-baseline.json",
    "dashboardApplicationSha256": "dashboard/app.js",
    "dashboardDocumentSha256": "dashboard/index.html",
    "dashboardStylesSha256": "dashboard/styles.css",
    "dashboardServerSha256": "scripts/operations-dashboard.py",
    "dashboardSmokeSha256": "scripts/dashboard-smoke.py",
    "operationsReportSha256": "scripts/operations-report.py",
    "operationsCollectorSha256": "scripts/operations-collector.py",
    "operationsWrapperSha256": "scripts/operations-report.sh",
    "productionAlertSha256": "scripts/production-alert.sh",
    "backupOrchestratorSha256": "scripts/modelport-backup.sh",
    "soakCheckSha256": "scripts/soak-check.py",
    "systemdInstallerSha256": "scripts/install-user-services.py",
    "runtimeControllerSha256": "scripts/runtime.sh",
    "runtimeReconcileSha256": "scripts/runtime-reconcile.sh",
    "runtimeSupervisorSha256": "scripts/runtime-supervisor.py",
    "candidateRuntimeSha256": "scripts/candidate-runtime.sh",
    "releaseCandidateSha256": "scripts/release-candidate.sh",
    "releaseCheckSha256": "scripts/release-check.sh",
    "gitleaksConfigSha256": ".gitleaks.toml",
    "docLinkCheckSha256": "scripts/check-doc-links.py",
    "deploymentLibrarySha256": "scripts/lib/deployment.sh",
    "deploymentVerifierSha256": "scripts/verify-deployment.py",
    "manifestVerifierSha256": "scripts/verify-manifest.py",
    "acceptanceEvidenceWriterSha256": "scripts/acceptance-evidence.py",
    "environmentUtilsSha256": "scripts/env_utils.py",
    "localHttpSha256": "scripts/local_http.py",
    "runtimeIdentitySha256": "scripts/runtime_identity.py",
    "secretsProvisionerSha256": "scripts/provision-operations-secrets.py",
    "learningGuideSha256": "docs/LEARNING_GUIDE.md",
    "controlPlaneReferenceSha256": "docs/REFERENCE.md",
    "runtimeProfilesSha256": "config/runtime-profiles.json",
    "runtimeProfilesSchemaSha256": "config/schemas/runtime-profiles.schema.json",
    "operationsSnapshotContractSha256": "contracts/operations-snapshot-v1.schema.json",
    "modelportOperationsContractSha256": "contracts/modelport-operations-v1.json",
    "stackLauncherSha256": "stack",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_set(paths: Iterable[Path]) -> str:
    entries = [
        {
            "path": path.relative_to(ROOT_DIR).as_posix(),
            "sha256": sha256_file(path),
        }
        for path in sorted(paths)
    ]
    body = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def expected_configuration() -> dict[str, str]:
    values = {
        key: sha256_file(ROOT_DIR / relative)
        for key, relative in CONFIGURATION_FILES.items()
    }
    values["systemdTemplatesSha256"] = sha256_file_set(
        (ROOT_DIR / "deploy" / "systemd").glob("*.in")
    )
    values["controlPlanePackageSha256"] = sha256_file_set(
        (ROOT_DIR / "src" / "local_inference_stack").glob("*.py")
    )
    return values


def verify(configuration: dict[str, str]) -> list[dict[str, str]]:
    expected = expected_configuration()
    issues: list[dict[str, str]] = []
    for key in sorted(set(expected) | set(configuration)):
        actual = configuration.get(key)
        wanted = expected.get(key)
        if actual != wanted:
            issues.append(
                {
                    "key": key,
                    "actual": actual or "<missing>",
                    "expected": wanted or "<unexpected-key>",
                }
            )
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    issues = verify(manifest.get("configuration", {}))
    result = {
        "schemaVersion": 1,
        "status": "passed" if not issues else "failed",
        "manifest": str(MANIFEST_PATH.relative_to(ROOT_DIR)),
        "issues": issues,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif issues:
        for issue in issues:
            print(
                f"[FAIL] {issue['key']}: {issue['actual']} "
                f"(expected {issue['expected']})"
            )
    else:
        print("Deployment manifest file digests passed.")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
