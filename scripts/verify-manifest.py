#!/usr/bin/env python3
"""Verify the deployment manifest's repository-local configuration digests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT_DIR / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from local_inference_stack.materials import (  # noqa: E402
    FILE_SET_SHA256_POLICY_ID,
    MaterialSet,
    SnapshotSpec,
)


MANIFEST_PATH = (
    ROOT_DIR / "deployments" / "qwen3.5-9b-rtx5070ti" / "manifest.json"
)
CONFIGURATION_FILES = {
    "agentContractSha256": "AGENTS.md",
    "gitIgnorePolicySha256": ".gitignore",
    "projectLicenseSha256": "LICENSE",
    "projectReadmeSha256": "README.md",
    "deploymentGuideSha256": "deployments/qwen3.5-9b-rtx5070ti/README.md",
    "securityPolicySha256": "SECURITY.md",
    "ciWorkflowSha256": ".github/workflows/ci.yml",
    "pythonVersionSha256": ".python-version",
    "nodeVersionSha256": ".nvmrc",
    "composeSha256": "compose.yaml",
    "latencyProfileSha256": "profiles/latency.env",
    "throughputProfileSha256": "profiles/throughput.env",
    "candidateProfileSha256": "profiles/candidate.env",
    "backupProfileExampleSha256": "profiles/backup.local.env.example",
    "alertingProfileExampleSha256": "profiles/alerting.local.env.example",
    "operationsProfileSha256": "profiles/operations.env",
    "providerContractSha256": "contracts/local-qwen-provider-v1.json",
    "qualitySuiteSha256": "quality/cases.json",
    "acceptanceSuiteSha256": "scripts/acceptance-suite.sh",
    "smokeTestSha256": "scripts/smoke-test.sh",
    "reasoningSmokeSha256": "scripts/reasoning-smoke.sh",
    "modelportSmokeSha256": "scripts/modelport-smoke.sh",
    "modelportReasoningSmokeSha256": "scripts/modelport-reasoning-smoke.py",
    "modelportReasoningSmokeWrapperSha256": "scripts/modelport-reasoning-smoke.sh",
    "modelportTokenCountSmokeSha256": "scripts/modelport-token-count-smoke.sh",
    "modelportContextAdmissionSha256": "scripts/modelport-context-admission-smoke.sh",
    "modelportContextAcceptanceSha256": "scripts/modelport-context-acceptance.sh",
    "contextAcceptanceSha256": "scripts/context-acceptance.py",
    "qualityEvaluatorSha256": "scripts/quality-eval.py",
    "performancePolicyEvaluatorSha256": "src/local_inference_stack/performance.py",
    "decodeBenchmarkSha256": "scripts/decode-benchmark.py",
    "concurrencyBenchmarkSha256": "scripts/concurrency-benchmark.py",
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
    "docCommandCheckSha256": "scripts/check-doc-commands.py",
    "deploymentLibrarySha256": "scripts/lib/deployment.sh",
    "integratedDeploymentVerifierSha256": "scripts/verify-integrated-deployment.py",
    "manifestVerifierSha256": "scripts/verify-manifest.py",
    "acceptanceEvidenceWriterSha256": "scripts/acceptance-evidence.py",
    "environmentUtilsSha256": "scripts/env_utils.py",
    "localHttpSha256": "scripts/local_http.py",
    "runtimeIdentitySha256": "scripts/runtime_identity.py",
    "secretsProvisionerSha256": "scripts/provision-operations-secrets.py",
    "environmentGuideSha256": "docs/ENVIRONMENT.md",
    "apiGuideSha256": "docs/API.md",
    "gettingStartedGuideSha256": "docs/GETTING_STARTED.md",
    "hardwareGuideSha256": "docs/HARDWARE_GUIDE.md",
    "modelArtifactsGuideSha256": "models/README.md",
    "modelportGuideSha256": "docs/MODELPORT.md",
    "architectureGuideSha256": "docs/ARCHITECTURE.md",
    "operationsGuideSha256": "docs/OPERATIONS.md",
    "acceptanceGuideSha256": "docs/ACCEPTANCE.md",
    "roadmapGuideSha256": "docs/ROADMAP.md",
    "upgradingGuideSha256": "docs/UPGRADING.md",
    "docsIndexSha256": "docs/README.md",
    "contributingGuideSha256": "CONTRIBUTING.md",
    "learningGuideSha256": "docs/LEARNING_GUIDE.md",
    "controlPlaneReferenceSha256": "docs/REFERENCE.md",
    "runtimeProfilesSha256": "config/runtime-profiles.json",
    "runtimeProfilesSchemaSha256": "config/schemas/runtime-profiles.schema.json",
    "operationsSnapshotContractSha256": "contracts/operations-snapshot-v1.schema.json",
    "modelportOperationsContractSha256": "contracts/modelport-operations-v1.json",
    "stackLauncherSha256": "stack",
}
REPOSITORY_MATERIAL_POLICY_ID = (
    "local-inference-stack/repository-configuration-materials-v1"
)
REPOSITORY_MATERIAL_SETS = (
    MaterialSet(
        key="architectureDecisionsSha256",
        policy_id=FILE_SET_SHA256_POLICY_ID,
        includes=("docs/decisions/*.md",),
    ),
    MaterialSet(
        key="systemdTemplatesSha256",
        policy_id=FILE_SET_SHA256_POLICY_ID,
        includes=("deploy/systemd/*.in",),
    ),
    MaterialSet(
        key="controlPlanePackageSha256",
        policy_id=FILE_SET_SHA256_POLICY_ID,
        includes=("src/local_inference_stack/*.py",),
    ),
    MaterialSet(
        key="unitTestPackageSha256",
        policy_id=FILE_SET_SHA256_POLICY_ID,
        includes=("tests/test_*.py",),
    ),
)
REPOSITORY_SNAPSHOT_SPEC = SnapshotSpec.from_mapping(
    policy_id=REPOSITORY_MATERIAL_POLICY_ID,
    files=CONFIGURATION_FILES,
    material_sets=REPOSITORY_MATERIAL_SETS,
)


def expected_configuration() -> dict[str, str]:
    values = REPOSITORY_SNAPSHOT_SPEC.snapshot(
        ROOT_DIR,
        expected_policy_id=REPOSITORY_MATERIAL_POLICY_ID,
    )
    values.update(
        {
            "repositoryMaterialPolicy": REPOSITORY_MATERIAL_POLICY_ID,
            "fileSetMaterialPolicy": FILE_SET_SHA256_POLICY_ID,
        }
    )
    return dict(sorted(values.items()))


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


def verify_document(manifest: object) -> list[dict[str, str]]:
    if not isinstance(manifest, dict):
        return [
            {
                "key": "manifest",
                "actual": type(manifest).__name__,
                "expected": "object",
            }
        ]
    issues: list[dict[str, str]] = []
    if manifest.get("schemaVersion") != 2:
        issues.append(
            {
                "key": "schemaVersion",
                "actual": str(manifest.get("schemaVersion", "<missing>")),
                "expected": "2",
            }
        )
    validation = manifest.get("validation")
    if (
        not isinstance(validation, dict)
        or validation.get("status") != "provisional-legacy"
        or validation.get("automaticDeploymentEligible") is not False
        or validation.get("historicalEvidenceOnly") is not True
    ):
        issues.append(
            {
                "key": "validation",
                "actual": "invalid-or-deployment-eligible",
                "expected": "provisional-legacy/non-deployable/historical-only",
            }
        )
    performance = manifest.get("performance")
    if (
        not isinstance(performance, dict)
        or performance.get("schemaVersion") != 1
        or performance.get("policy") != "pending-baseline"
        or performance.get("calibrationRuns") != 0
    ):
        issues.append(
            {
                "key": "performance",
                "actual": "invalid-or-unreviewed-policy",
                "expected": "schema-v1 pending-baseline with zero calibration runs",
            }
        )
    gateway = manifest.get("gateway")
    reviewed_identities = (
        gateway.get("reviewedContainerIdentities")
        if isinstance(gateway, dict)
        else None
    )
    if (
        not isinstance(reviewed_identities, dict)
        or reviewed_identities.get("schemaVersion") != 1
        or reviewed_identities.get("status") != "review-required"
        or not isinstance(reviewed_identities.get("reason"), str)
        or not reviewed_identities.get("reason")
        or reviewed_identities.get("containers") != {}
    ):
        issues.append(
            {
                "key": "gateway.reviewedContainerIdentities",
                "actual": "invalid-or-unreviewed-identity-claimed-current",
                "expected": "schema-v1 review-required with no reviewed containers",
            }
        )
    historical = manifest.get("validatedConfiguration")
    if not isinstance(historical, dict) or not historical:
        issues.append(
            {
                "key": "validatedConfiguration",
                "actual": "missing-or-empty",
                "expected": "preserved historical validation hashes",
            }
        )
    current = manifest.get("repositoryConfiguration")
    if not isinstance(current, dict):
        issues.append(
            {
                "key": "repositoryConfiguration",
                "actual": type(current).__name__,
                "expected": "object",
            }
        )
    else:
        issues.extend(verify(current))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    issues = verify_document(manifest)
    result = {
        "schemaVersion": 2,
        "status": "passed" if not issues else "failed",
        "manifest": str(MANIFEST_PATH.relative_to(ROOT_DIR)),
        "validationStatus": (manifest.get("validation") or {}).get("status"),
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
