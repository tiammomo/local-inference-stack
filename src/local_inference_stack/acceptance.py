"""Pure acceptance-run and catalog validation-input policies.

The acceptance runner, evidence writer, model planner, and attestation verifier all
use this module so that a terminal step name or a mutable catalog promotion field
cannot stand in for a completed run.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from .materials import canonical_sha256


RUN_SCHEMA_VERSION = 1
ROLLOUT_RUN_SCHEMA_VERSION = 2
RUN_KIND = "local-inference-stack/acceptance-run"
RUNNER_PATH = "scripts/acceptance-suite.sh"
ROLLOUT_EVIDENCE_SCHEMA_VERSION = 5
ROLLOUT_BINDING_POLICY = (
    "local-inference-stack/rollout-qualification-binding-v1"
)
ROLLOUT_FROZEN_INPUTS_POLICY = (
    "local-inference-stack/rollout-qualification-frozen-inputs-v1"
)
MODELPORT_SOURCE_IDENTITY_POLICY = (
    "local-inference-stack/modelport-source-identity-v2"
)
MODELPORT_SOURCE_MATERIAL_PATHS = (
    "config.toml",
    "scripts/lib.sh",
    "scripts/provider-matrix.sh",
    "src/governance.rs",
    "src/routes.rs",
)
QUALIFICATION_INPUT_POLICY = (
    "local-inference-stack/full-qualification-input-v1"
)
QUALIFICATION_INPUT_KEYS = frozenset(
    {
        "policyId",
        "targetCatalogSpecSha256",
        "providerContractId",
        "providerContractSha256",
        "servedModelId",
        "limitsSha256",
        "acceptanceSha256",
        "logicalModels",
        "providerMatrixModel",
        "toolUseMaxTokens",
        "directContextTokens",
        "modelPortContextTokens",
        "modelPortContextMaxTokens",
        "decodeTokens",
        "decodeContextTokens",
        "concurrency",
        "concurrencyTokens",
        "modelPortSourceIdentitySha256",
        "liveModelRegistrySha256",
        "toolUseLocalProviderReady",
        "sha256",
    }
)
REVIEWED_LOGICAL_MODELS = {
    "qwen3.5-fast": {
        "reasoningDefaultEnabled": False,
        "reasoningBudgetTokens": 512,
        "recommendedWorkingSetTokens": 24576,
        "maxOutputTokens": 4096,
    },
    "qwen3.5-code": {
        "reasoningDefaultEnabled": True,
        "reasoningBudgetTokens": 4096,
        "recommendedWorkingSetTokens": 57344,
        "maxOutputTokens": 16384,
    },
    "qwen3.5-deep": {
        "reasoningDefaultEnabled": True,
        "reasoningBudgetTokens": 16384,
        "recommendedWorkingSetTokens": 94208,
        "maxOutputTokens": 32768,
    },
}
REVIEWED_MODELPORT_TOOL_USE = {
    "supported": True,
    "toolChoice": True,
    "streamingArguments": "best_effort",
    "responseValidation": "strict",
    "repairInvalidArguments": {
        "enabled": True,
        "maximumAttempts": 1,
        "nonStreamOnly": True,
    },
    "parallelToolCalls": True,
}
REVIEWED_MODELPORT_GOVERNANCE = {
    "routingModeHeader": "x-modelport-hybrid-mode",
    "routingModeResponseHeader": "x-modelport-execution-mode",
    "routingModes": ["local_strict", "local_first", "balanced", "cloud_first"],
    "defaultMode": "local_strict",
    "classificationHeader": "x-modelport-data-classification",
    "classifications": ["unknown", "sensitive", "internal", "public"],
    "forcedLocalClassifications": ["unknown", "sensitive"],
    "localAdmission": {
        "executingPerUser": 1,
        "queuedPerUser": 2,
        "globalInteractiveQueue": 16,
        "overflowAfterSeconds": 5,
        "strictWaitSeconds": 60,
        "strictTimeoutStatus": 429,
        "strictTimeoutRetryAfter": True,
        "batchQueue": "independent-low-priority",
    },
}
VALIDATION_INPUT_SCHEMA_VERSION = 1
VALIDATION_INPUT_POLICY = "local-inference-stack/catalog-validation-input-v1"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
GIT_OBJECT_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
ROLLOUT_BINDING_KEYS = frozenset(
    {
        "policyId",
        "transactionId",
        "operation",
        "rolloutPlanSha256",
        "actionOrdinal",
        "actionKind",
        "rollbackSpecSha256",
        "sourceCatalogSpecSha256",
        "targetCatalogSpecSha256",
        "performancePolicySha256",
        "modelPortSourceIdentitySha256",
        "qualificationInputSha256",
    }
)
ROLLOUT_FROZEN_INPUT_KEYS = frozenset(
    {
        "policyId",
        "catalogModelId",
        "targetCatalogSpecSha256",
        "liveRuntimeIdentitySha256",
        "runtimeIdentity",
        "acceptanceConfigurationSha256",
        "controllerMaterialIdentity",
        "modelPortSourceIdentity",
        "sha256",
    }
)
EVIDENCE_COMMON_KEYS = frozenset(
    {
        "schemaVersion",
        "evidenceId",
        "mode",
        "profile",
        "status",
        "exitCode",
        "failedAtStep",
        "terminalStep",
        "startedAt",
        "finishedAt",
        "durationSeconds",
        "gitCommit",
        "gitState",
        "catalogModelId",
        "validationInput",
        "run",
        "host",
        "artifact",
        "runtime",
        "configuration",
        "freshnessPolicy",
        "privacy",
        "selfSha256",
    }
)
RUN_RECORD_COMMON_KEYS = frozenset(
    {
        "schemaVersion",
        "kind",
        "runId",
        "mode",
        "runner",
        "plan",
        "stepResults",
        "failedAtStep",
        "terminalStep",
        "manifest",
    }
)

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


def rollout_binding_valid(value: Any) -> bool:
    """Validate the transaction-derived identity of one full qualification."""

    if not isinstance(value, dict) or set(value) != ROLLOUT_BINDING_KEYS:
        return False
    try:
        transaction_id = str(uuid.UUID(str(value.get("transactionId"))))
    except ValueError:
        return False
    return bool(
        value.get("policyId") == ROLLOUT_BINDING_POLICY
        and transaction_id == value.get("transactionId")
        and value.get("operation") == "upgrade"
        and isinstance(value.get("actionOrdinal"), int)
        and not isinstance(value.get("actionOrdinal"), bool)
        and value["actionOrdinal"] >= 0
        and value.get("actionKind") == "target-full"
        and all(
            SHA256_PATTERN.fullmatch(str(value.get(key, "")))
            for key in (
                "rolloutPlanSha256",
                "rollbackSpecSha256",
                "sourceCatalogSpecSha256",
                "targetCatalogSpecSha256",
                "performancePolicySha256",
                "modelPortSourceIdentitySha256",
                "qualificationInputSha256",
            )
        )
        and value.get("sourceCatalogSpecSha256")
        != value.get("targetCatalogSpecSha256")
    )


def qualification_input_valid(value: Any) -> bool:
    """Validate the complete, secret-free workload frozen by a full rollout.

    These constants are part of the v1 wire policy.  Changing a logical alias,
    context workload, or benchmark size therefore requires an explicit policy
    version rather than an ambient environment override.
    """

    return bool(
        isinstance(value, dict)
        and set(value) == QUALIFICATION_INPUT_KEYS
        and value.get("policyId") == QUALIFICATION_INPUT_POLICY
        and value.get("providerContractId") == "local-qwen-provider-v1"
        and value.get("logicalModels") == REVIEWED_LOGICAL_MODELS
        and value.get("providerMatrixModel") == "qwen3.5-code"
        and value.get("toolUseMaxTokens") == 2048
        and value.get("directContextTokens") == 118000
        and value.get("modelPortContextTokens") == 92000
        and value.get("modelPortContextMaxTokens") == 32768
        and value.get("decodeTokens") == 512
        and value.get("decodeContextTokens") == 0
        and value.get("concurrency") == 2
        and value.get("concurrencyTokens") == 512
        and value.get("toolUseLocalProviderReady") is True
        and isinstance(value.get("servedModelId"), str)
        and bool(value["servedModelId"])
        and all(
            SHA256_PATTERN.fullmatch(str(value.get(key, "")))
            for key in (
                "targetCatalogSpecSha256",
                "providerContractSha256",
                "limitsSha256",
                "acceptanceSha256",
                "modelPortSourceIdentitySha256",
                "liveModelRegistrySha256",
                "sha256",
            )
        )
        and value.get("sha256")
        == sha256_document(
            {key: item for key, item in value.items() if key != "sha256"}
        )
    )


def modelport_source_identity_valid(value: Any) -> bool:
    """Validate a clean checkout and its exact live, non-secret config binding."""

    if not isinstance(value, dict):
        return False
    materials = value.get("materials")
    live = value.get("liveServiceIdentity")
    build = live.get("build") if isinstance(live, dict) else None
    if (
        set(value)
        != {
            "policyId",
            "gitCommit",
            "gitTree",
            "sourceState",
            "materials",
            "materialsSha256",
            "liveServiceIdentity",
        }
        or value.get("policyId") != MODELPORT_SOURCE_IDENTITY_POLICY
        or GIT_OBJECT_PATTERN.fullmatch(str(value.get("gitCommit", ""))) is None
        or GIT_OBJECT_PATTERN.fullmatch(str(value.get("gitTree", ""))) is None
        or value.get("sourceState") != "clean"
        or not isinstance(materials, list)
        or [item.get("path") if isinstance(item, dict) else None for item in materials]
        != list(MODELPORT_SOURCE_MATERIAL_PATHS)
        or any(
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not SHA256_PATTERN.fullmatch(str(item.get("sha256", "")))
            for item in materials
        )
        or value.get("materialsSha256") != sha256_document(materials)
        or not isinstance(live, dict)
        or set(live) != {"endpoint", "service", "status", "build"}
        or live.get("endpoint") != "http://127.0.0.1:38082/livez"
        or live.get("service") != "model-port"
        or live.get("status") != "ok"
        or not isinstance(build, dict)
        or set(build) != {"revision", "sourceState", "version", "configSha256"}
        or build.get("revision") != value.get("gitCommit")
        or build.get("sourceState") != "clean"
        or not isinstance(build.get("version"), str)
        or not build["version"]
        or len(build["version"]) > 128
        or not SHA256_PATTERN.fullmatch(str(build.get("configSha256", "")))
    ):
        return False
    material_digests = {item["path"]: item["sha256"] for item in materials}
    return build["configSha256"] == material_digests["config.toml"]


def rollout_frozen_inputs_valid(
    value: Any,
    *,
    rollout_binding: Any,
    configuration: dict[str, Any] | None = None,
) -> bool:
    """Validate immutable runtime, controller, and ModelPort qualification inputs."""

    if (
        not isinstance(value, dict)
        or set(value) != ROLLOUT_FROZEN_INPUT_KEYS
        or not rollout_binding_valid(rollout_binding)
    ):
        return False
    controller = value.get("controllerMaterialIdentity")
    modelport = value.get("modelPortSourceIdentity")
    runtime = value.get("runtimeIdentity")
    if (
        value.get("policyId") != ROLLOUT_FROZEN_INPUTS_POLICY
        or not isinstance(value.get("catalogModelId"), str)
        or not value["catalogModelId"]
        or value.get("targetCatalogSpecSha256")
        != rollout_binding.get("targetCatalogSpecSha256")
        or any(
            not SHA256_PATTERN.fullmatch(str(value.get(key, "")))
            for key in (
                "liveRuntimeIdentitySha256",
                "acceptanceConfigurationSha256",
                "sha256",
            )
        )
        or not isinstance(runtime, dict)
        or set(runtime)
        != {
            "containerId",
            "startedAt",
            "imageId",
            "containerConfigSha256",
        }
        or re.fullmatch(r"[0-9a-f]{64}", str(runtime.get("containerId", "")))
        is None
        or not isinstance(runtime.get("startedAt"), str)
        or not runtime["startedAt"]
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(runtime.get("imageId", ""))
        )
        is None
        or not SHA256_PATTERN.fullmatch(
            str(runtime.get("containerConfigSha256", ""))
        )
        or value.get("liveRuntimeIdentitySha256")
        != runtime.get("containerConfigSha256")
        or not isinstance(controller, dict)
        or set(controller)
        != {
            "materialPolicy",
            "fileSetMaterialPolicy",
            "controlPlanePackageSha256",
            "providerContractSha256",
        }
        or not isinstance(controller.get("materialPolicy"), str)
        or not controller["materialPolicy"]
        or not isinstance(controller.get("fileSetMaterialPolicy"), str)
        or not controller["fileSetMaterialPolicy"]
        or any(
            not SHA256_PATTERN.fullmatch(str(controller.get(key, "")))
            for key in ("controlPlanePackageSha256", "providerContractSha256")
        )
        or not modelport_source_identity_valid(modelport)
        or sha256_document(modelport)
        != rollout_binding.get("modelPortSourceIdentitySha256")
        or value.get("sha256")
        != sha256_document({key: item for key, item in value.items() if key != "sha256"})
    ):
        return False
    if configuration is not None:
        if (
            value.get("acceptanceConfigurationSha256")
            != sha256_document(configuration)
            or configuration.get("performancePolicySha256")
            != rollout_binding.get("performancePolicySha256")
            or controller
            != {
                "materialPolicy": configuration.get("materialPolicy"),
                "fileSetMaterialPolicy": configuration.get("fileSetMaterialPolicy"),
                "controlPlanePackageSha256": configuration.get(
                    "controlPlanePackageSha256"
                ),
                "providerContractSha256": configuration.get("contractSha256"),
            }
        ):
            return False
    return True


def acceptance_evidence_shape_valid(value: Any) -> bool:
    """Reject ambiguous or forward-extended evidence at every trust boundary."""

    if not isinstance(value, dict):
        return False
    schema_version = value.get("schemaVersion")
    expected = EVIDENCE_COMMON_KEYS | (
        {"rolloutBinding", "frozenInputs"}
        if schema_version == ROLLOUT_EVIDENCE_SCHEMA_VERSION
        else set()
    )
    if schema_version not in {4, ROLLOUT_EVIDENCE_SCHEMA_VERSION} or set(value) != expected:
        return False
    run = value.get("run")
    run_schema_version = (
        ROLLOUT_RUN_SCHEMA_VERSION
        if schema_version == ROLLOUT_EVIDENCE_SCHEMA_VERSION
        else RUN_SCHEMA_VERSION
    )
    run_expected = RUN_RECORD_COMMON_KEYS | (
        {"profile", "catalogModelId", "rolloutBinding", "frozenInputs"}
        if run_schema_version == ROLLOUT_RUN_SCHEMA_VERSION
        else set()
    )
    return bool(
        isinstance(run, dict)
        and run.get("schemaVersion") == run_schema_version
        and set(run) == run_expected
        and isinstance(run.get("runner"), dict)
        and set(run["runner"]) == {"path", "sha256", "capabilitySha256"}
        and isinstance(run.get("plan"), dict)
        and set(run["plan"]) == {"stepNames", "sha256"}
        and isinstance(run.get("manifest"), dict)
        and set(run["manifest"])
        == {"schemaVersion", "sourcePath", "sourceSha256", "selfSha256"}
    )


def run_record_valid(
    run: Any,
    *,
    mode: str,
    overall_status: str,
    overall_exit_code: int,
    configuration: dict[str, Any] | None = None,
) -> bool:
    """Validate the runner-owned, ordered per-step result embedded in evidence."""

    if not isinstance(run, dict) or run.get("schemaVersion") not in {
        RUN_SCHEMA_VERSION,
        ROLLOUT_RUN_SCHEMA_VERSION,
    }:
        return False
    schema_version = run["schemaVersion"]
    expected_keys = RUN_RECORD_COMMON_KEYS | (
        {"profile", "catalogModelId", "rolloutBinding", "frozenInputs"}
        if schema_version == ROLLOUT_RUN_SCHEMA_VERSION
        else set()
    )
    if set(run) != expected_keys:
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
        or manifest.get("schemaVersion") != schema_version
        or not SHA256_PATTERN.fullmatch(str(manifest.get("selfSha256", "")))
        or not SHA256_PATTERN.fullmatch(str(manifest.get("sourceSha256", "")))
    ):
        return False
    if schema_version == ROLLOUT_RUN_SCHEMA_VERSION:
        if (
            mode != "full"
            or run.get("profile") != "latency"
            or run.get("catalogModelId") is None
            or run.get("rolloutBinding") is None
            or run.get("frozenInputs") is None
            or not rollout_frozen_inputs_valid(
                run.get("frozenInputs"),
                rollout_binding=run.get("rolloutBinding"),
                configuration=configuration,
            )
            or run["frozenInputs"].get("catalogModelId")
            != run.get("catalogModelId")
        ):
            return False
    elif any(
        key in run
        for key in ("profile", "catalogModelId", "rolloutBinding", "frozenInputs")
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
    schema_version = manifest.get("schemaVersion")
    if schema_version not in {RUN_SCHEMA_VERSION, ROLLOUT_RUN_SCHEMA_VERSION}:
        return False
    manifest_hash = sha256_document(
        {key: value for key, value in manifest.items() if key != "selfSha256"}
    )
    manifest_identity = run.get("manifest")
    return bool(
        manifest.get("schemaVersion") == run.get("schemaVersion")
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
        and (
            schema_version == RUN_SCHEMA_VERSION
            and evidence.get("schemaVersion") == 4
            and "rolloutBinding" not in manifest
            and "frozenInputs" not in manifest
            and "rolloutBinding" not in run
            and "frozenInputs" not in run
            and "rolloutBinding" not in evidence
            and "frozenInputs" not in evidence
            or schema_version == ROLLOUT_RUN_SCHEMA_VERSION
            and evidence.get("schemaVersion") == ROLLOUT_EVIDENCE_SCHEMA_VERSION
            and manifest.get("profile") == run.get("profile")
            and manifest.get("catalogModelId") == run.get("catalogModelId")
            and manifest.get("rolloutBinding")
            == run.get("rolloutBinding")
            == evidence.get("rolloutBinding")
            and manifest.get("frozenInputs")
            == run.get("frozenInputs")
            == evidence.get("frozenInputs")
            and rollout_frozen_inputs_valid(
                manifest.get("frozenInputs"),
                rollout_binding=manifest.get("rolloutBinding"),
                configuration=evidence.get("configuration"),
            )
        )
    )
