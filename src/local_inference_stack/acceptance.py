"""Pure acceptance-run and catalog validation-input policies.

The acceptance runner, evidence writer, model planner, and attestation verifier all
use this module so that a terminal step name or a mutable catalog promotion field
cannot stand in for a completed run.
"""

from __future__ import annotations

import re
from typing import Any

from .materials import canonical_sha256


RUN_SCHEMA_VERSION = 1
RUN_KIND = "local-inference-stack/acceptance-run"
RUNNER_PATH = "scripts/acceptance-suite.sh"
VALIDATION_INPUT_SCHEMA_VERSION = 1
VALIDATION_INPUT_POLICY = "local-inference-stack/catalog-validation-input-v1"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

QUICK_STEPS = (
    "Local unit tests",
    "Artifact integrity",
    "Runtime status",
    "Canonical runtime profile",
    "Direct generation",
    "Direct reasoning",
)
STANDARD_STEPS = (
    "Operations dashboard preflight",
    *QUICK_STEPS,
    "Cross-repository provider contract",
    "ModelPort Messages",
    "Exact token counting",
    "ModelPort context admission",
    "ModelPort reasoning mapping",
    "ModelPort provider matrix",
    "ModelPort Tool Use",
    "Closed-loop Tool Use smoke",
    "Tool resilience smoke",
    "Synthetic quality smoke",
)
FULL_STEPS = (
    "Performance policy preflight",
    *STANDARD_STEPS,
    "Full artifact rehash",
    "118K direct context",
    "92K ModelPort reasoning context",
    "Decode benchmark",
    "Concurrency benchmark",
    "Repeated synthetic quality suite",
    "Forty-case closed-loop Tool Use suite",
    "Multi-step and adversarial Tool Use suite",
)
STEP_PLANS = {
    "quick": QUICK_STEPS,
    "standard": STANDARD_STEPS,
    "full": FULL_STEPS,
}

# These fields are the result of promotion, rather than inputs to validation.
# Omitting them prevents adding a signed attestation to the catalog from
# invalidating the acceptance run that authorized that promotion.
PROMOTION_FIELDS = frozenset(
    {
        "status",
        "lifecycleRole",
        "deploymentEligibility",
        "validation",
        "validatedHardware",
        "validationAttestation",
    }
)
HOST_LOCAL_CONFIGURATION_FIELDS = frozenset(
    {
        "catalogSha256",
        "deploymentProfileSha256",
        "effectiveComposeSha256",
        "manifestSha256",
    }
)


def sha256_document(value: Any) -> str:
    return canonical_sha256(value)


def expected_steps(mode: str) -> tuple[str, ...]:
    try:
        return STEP_PLANS[mode]
    except KeyError as exc:
        raise ValueError(f"unsupported acceptance mode: {mode}") from exc


def step_plan_sha256(mode: str) -> str:
    return sha256_document(list(expected_steps(mode)))


def catalog_validation_input(
    catalog: dict[str, Any], model: dict[str, Any]
) -> dict[str, Any]:
    """Return the stable catalog subset that was actually validated.

    Promotion-only fields are intentionally excluded. Artifact identity, source
    revisions, license metadata, capacity, and runtime settings remain bound.
    """

    normalized_model = {
        key: value for key, value in model.items() if key not in PROMOTION_FIELDS
    }
    return {
        "schemaVersion": VALIDATION_INPUT_SCHEMA_VERSION,
        "policy": VALIDATION_INPUT_POLICY,
        "catalogSchemaVersion": catalog.get("schemaVersion"),
        "artifactPolicy": catalog.get("artifactPolicy"),
        "deploymentPolicy": catalog.get("deploymentPolicy"),
        "model": normalized_model,
    }


def catalog_validation_input_sha256(
    catalog: dict[str, Any], model: dict[str, Any]
) -> str:
    return sha256_document(catalog_validation_input(catalog, model))


def repository_validation_input(
    configuration: dict[str, Any],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in configuration.items()
        if key not in HOST_LOCAL_CONFIGURATION_FIELDS
    }


def validation_input(
    catalog: dict[str, Any],
    model: dict[str, Any],
    configuration: dict[str, Any],
) -> dict[str, Any]:
    catalog_digest = catalog_validation_input_sha256(catalog, model)
    repository_digest = sha256_document(repository_validation_input(configuration))
    combined = {
        "catalogSha256": catalog_digest,
        "repositorySha256": repository_digest,
    }
    return {
        "policy": VALIDATION_INPUT_POLICY,
        **combined,
        "sha256": sha256_document(combined),
    }


def run_record_valid(
    run: Any,
    *,
    mode: str,
    overall_status: str,
    overall_exit_code: int,
    configuration: dict[str, Any] | None = None,
) -> bool:
    """Validate the runner-owned, ordered per-step result embedded in evidence."""

    if not isinstance(run, dict) or run.get("schemaVersion") != RUN_SCHEMA_VERSION:
        return False
    runner = run.get("runner")
    plan = run.get("plan")
    steps = run.get("stepResults")
    manifest = run.get("manifest")
    if (
        run.get("kind") != RUN_KIND
        or run.get("mode") != mode
        or not isinstance(run.get("runId"), str)
        or not run["runId"]
        or not isinstance(runner, dict)
        or runner.get("path") != RUNNER_PATH
        or not SHA256_PATTERN.fullmatch(str(runner.get("sha256", "")))
        or not SHA256_PATTERN.fullmatch(
            str(runner.get("capabilitySha256", ""))
        )
        or not isinstance(plan, dict)
        or plan.get("stepNames") != list(expected_steps(mode))
        or plan.get("sha256") != step_plan_sha256(mode)
        or not isinstance(steps, list)
        or not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != RUN_SCHEMA_VERSION
        or not SHA256_PATTERN.fullmatch(str(manifest.get("selfSha256", "")))
        or not SHA256_PATTERN.fullmatch(str(manifest.get("sourceSha256", "")))
    ):
        return False
    if configuration is not None and (
        runner.get("sha256") != configuration.get("acceptanceSuiteSha256")
    ):
        return False
    names: list[str] = []
    for ordinal, step in enumerate(steps, start=1):
        if (
            not isinstance(step, dict)
            or set(step)
            != {
                "ordinal",
                "name",
                "startedAt",
                "finishedAt",
                "durationSeconds",
                "exitCode",
                "status",
            }
            or step.get("ordinal") != ordinal
            or not isinstance(step.get("name"), str)
            or not isinstance(step.get("startedAt"), str)
            or not isinstance(step.get("finishedAt"), str)
            or not isinstance(step.get("durationSeconds"), int)
            or isinstance(step.get("durationSeconds"), bool)
            or step["durationSeconds"] < 0
            or not isinstance(step.get("exitCode"), int)
            or isinstance(step.get("exitCode"), bool)
            or step.get("status")
            != ("passed" if step.get("exitCode") == 0 else "failed")
        ):
            return False
        names.append(step["name"])
    planned = list(expected_steps(mode))
    if names != planned[: len(names)]:
        return False
    if overall_status == "passed":
        return (
            overall_exit_code == 0
            and names == planned
            and all(step["exitCode"] == 0 for step in steps)
            and run.get("terminalStep") == planned[-1]
            and run.get("failedAtStep") is None
        )
    if overall_status != "failed" or overall_exit_code == 0:
        return False
    return bool(
        run.get("failedAtStep")
        and (
            any(step["exitCode"] != 0 for step in steps)
            or len(steps) < len(planned)
        )
    )


def run_manifest_matches_evidence(
    manifest: Any,
    run: Any,
    evidence: dict[str, Any],
) -> bool:
    """Bind evidence fields to the finalized private runner manifest."""

    if not isinstance(manifest, dict) or not isinstance(run, dict):
        return False
    manifest_hash = sha256_document(
        {key: value for key, value in manifest.items() if key != "selfSha256"}
    )
    manifest_identity = run.get("manifest")
    return bool(
        manifest.get("schemaVersion") == RUN_SCHEMA_VERSION
        and manifest.get("kind") == RUN_KIND
        and manifest.get("finalized") is True
        and manifest.get("selfSha256") == manifest_hash
        and isinstance(manifest_identity, dict)
        and manifest_identity.get("selfSha256") == manifest_hash
        and manifest.get("runId") == run.get("runId")
        and manifest.get("mode") == evidence.get("mode") == run.get("mode")
        and manifest.get("profile") == evidence.get("profile")
        and manifest.get("catalogModelId") == evidence.get("catalogModelId")
        and manifest.get("runner") == run.get("runner")
        and manifest.get("plan") == run.get("plan")
        and manifest.get("stepResults") == run.get("stepResults")
        and manifest.get("failedAtStep")
        == evidence.get("failedAtStep")
        == run.get("failedAtStep")
        and manifest.get("terminalStep")
        == evidence.get("terminalStep")
        == run.get("terminalStep")
        and manifest.get("startedAt") == evidence.get("startedAt")
        and manifest.get("finishedAt") == evidence.get("finishedAt")
        and manifest.get("durationSeconds") == evidence.get("durationSeconds")
        and manifest.get("status") == evidence.get("status")
        and manifest.get("exitCode") == evidence.get("exitCode")
        and manifest.get("validationInput") == evidence.get("validationInput")
    )
