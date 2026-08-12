"""Durable, atomic transaction state for runtime mutations and recovery."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .deployment import (
    CatalogDeploymentSpec,
    DeploymentSpecError,
    RolloutQualification,
    parse_approved_deployment,
)
from .materials import (
    canonical_bytes,
    canonical_sha256,
    cleanup_interrupted_noreplace_link_at,
)
from .paths import ProjectPaths
from .result import RecoveryError


SCHEMA_VERSION = 2
READABLE_SCHEMA_VERSIONS = {1, SCHEMA_VERSION}
TERMINAL_STATES = {"completed", "failed-restored", "superseded-verified"}
RECOVERY_DEPLOYMENT_KEYS = frozenset(
    {
        "QWEN_CATALOG_ID",
        "QWEN_MODEL_DIR",
        "QWEN_MODEL_FILE",
        "QWEN_MODEL_DISPLAY_NAME",
        "QWEN_QUANTIZATION",
        "QWEN_SERVED_MODEL_ID",
        "QWEN_CONTAINER_NAME",
        "QWEN_RUNTIME_UID",
        "QWEN_RUNTIME_GID",
        "MODELPORT_NETWORK_NAME",
        "QWEN_CTX_SIZE",
        "QWEN_RECOMMENDED_INPUT_TOKENS",
        "QWEN_N_PREDICT",
        "QWEN_CACHE_RAM",
        "QWEN_CACHE_TYPE_K",
        "QWEN_CACHE_TYPE_V",
        "QWEN_BATCH_SIZE",
        "QWEN_UBATCH_SIZE",
    }
)
RECOVERY_REQUIRED_DEPLOYMENT_KEYS = RECOVERY_DEPLOYMENT_KEYS - {
    "QWEN_CACHE_TYPE_K",
    "QWEN_CACHE_TYPE_V",
}
ALLOWED_TRANSITIONS = {
    "planned": {
        "production_stopping",
        "candidate_starting",
        "deploying",
        "recovery_required",
    },
    "deploying": {
        "accepting",
        "production_restoring",
        "completed",
        "recovery_required",
    },
    "production_stopping": {
        "candidate_starting",
        "production_restoring",
        "recovery_required",
    },
    "candidate_starting": {
        "accepting",
        "production_restoring",
        "recovery_required",
    },
    "accepting": {
        "production_restoring",
        "completed",
        "recovery_required",
    },
    "recovery_required": {"production_restoring", "superseded-verified"},
    "production_restoring": {
        "completed",
        "failed-restored",
        "recovery_required",
    },
    "completed": set(),
    "failed-restored": set(),
    "superseded-verified": set(),
}

LEGACY_STATES = {
    "planned",
    "deploying",
    "production_stopping",
    "candidate_starting",
    "accepting",
    "production_restoring",
    "failed",
    "completed",
}
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
MAX_TRANSACTION_BYTES = 4 * 1024 * 1024
ROLLOUT_OPERATIONS = frozenset({"upgrade", "rollback"})
CATALOG_BOUND_OPERATIONS = frozenset({"deploy", *ROLLOUT_OPERATIONS})
ROLLOUT_INTENT_POLICY_ID = (
    "local-inference-stack/transaction-rollout-intent-v1"
)
ROLLOUT_INTENT_POLICY_ID_V2 = (
    "local-inference-stack/transaction-rollout-intent-v2"
)
ROLLOUT_INTENT_KEYS = frozenset(
    {
        "policyId",
        "rollbackSpecSha256",
        "sourceCatalogSpecSha256",
        "targetCatalogSpecSha256",
        "rolloutPlan",
        "rolloutPlanSha256",
        "previousRollbackPointer",
    }
)
ROLLOUT_PLAN_KEYS = frozenset(
    {
        "schemaVersion",
        "operation",
        "rollbackSpecSha256",
        "sourceCatalogSpecSha256",
        "targetCatalogSpecSha256",
        "requiredAcceptanceTier",
        "requiresApproval",
        "actions",
    }
)
ROLLOUT_PLAN_V2_KEYS = ROLLOUT_PLAN_KEYS | {"qualification"}
ROLLOUT_ACTION_KEYS = frozenset(
    {"ordinal", "kind", "subject", "catalogId"}
)
ROLLOUT_ACTION_RESULT_KEYS = frozenset(
    {
        "ordinal",
        "kind",
        "subject",
        "catalogId",
        "resultSha256",
        "completedAt",
    }
)
QUALIFICATION_RECEIPT_POLICY_ID = (
    "local-inference-stack/rollout-qualification-receipt-v1"
)
QUALIFICATION_RESULT_POLICY_ID = (
    "local-inference-stack/rollout-qualification-result-v1"
)
ROLLOUT_QUALIFICATION_BINDING_POLICY_ID = (
    "local-inference-stack/rollout-qualification-binding-v1"
)
QUALIFICATION_EVIDENCE_RECEIPT_KEYS = frozenset(
    {
        "policyId",
        "evidencePath",
        "evidenceSha256",
        "evidenceSelfSha256",
        "runManifestPath",
        "runManifestSha256",
        "runManifestSelfSha256",
        "stepResultsSha256",
        "configurationSha256",
        "runtimeIdentitySha256",
        "rolloutBindingSha256",
    }
)
ROLLOUT_ACTION_SUBJECTS = {
    "source-quick": "source",
    "fetch-target-artifact": "target",
    "stop-source": "source",
    "activate-target": "target",
    "start-target": "target",
    "target-quick": "target",
    "target-full": "target",
    "publish-rollback": "source",
    "clear-rollback": "target",
}
ROLLOUT_RECOVERY_STATES = frozenset({"recovery_required", "production_restoring"})
_SHA256_CHARACTERS = frozenset("0123456789abcdef")


def _boot_id() -> str:
    try:
        value = BOOT_ID_PATH.read_text(encoding="utf-8").strip()
        return str(uuid.UUID(value))
    except (OSError, ValueError) as exc:
        raise RecoveryError("cannot establish the current Linux boot identity") from exc


def _process_start_time_ticks(pid: int) -> int | None:
    """Read Linux /proc field 22 without being confused by spaces in comm."""
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = value.rfind(")")
        if closing < 0:
            return None
        fields = value[closing + 2 :].split()
        start_time = int(fields[19])
        return start_time if start_time > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def transaction_owner() -> dict[str, Any]:
    pid = os.getpid()
    start_time = _process_start_time_ticks(pid)
    if start_time is None:
        raise RecoveryError("cannot establish the control-plane process identity")
    return {
        "pid": pid,
        "processStartTimeTicks": start_time,
        "bootId": _boot_id(),
        "capturedAt": utc_now(),
    }


def _valid_owner(owner: Any) -> bool:
    if not isinstance(owner, dict) or set(owner) != {
        "pid",
        "processStartTimeTicks",
        "bootId",
        "capturedAt",
    }:
        return False
    if (
        not isinstance(owner.get("pid"), int)
        or isinstance(owner.get("pid"), bool)
        or owner["pid"] <= 0
        or not isinstance(owner.get("processStartTimeTicks"), int)
        or isinstance(owner.get("processStartTimeTicks"), bool)
        or owner["processStartTimeTicks"] <= 0
        or not isinstance(owner.get("capturedAt"), str)
        or not owner["capturedAt"]
    ):
        return False
    try:
        return str(uuid.UUID(str(owner.get("bootId")))) == owner.get("bootId")
    except ValueError:
        return False


def is_terminal(document: dict[str, Any]) -> bool:
    schema = document.get("schemaVersion")
    state = document.get("state")
    if schema == 1:
        # A v1 `failed` record did not prove that recovery completed.  It is
        # deliberately non-terminal under the v2 reader.
        return state == "completed"
    return schema == SCHEMA_VERSION and state in TERMINAL_STATES


def _is_sha256(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and set(value).issubset(_SHA256_CHARACTERS)
    )


def _canonical_clone(value: Any, *, label: str) -> Any:
    try:
        return json.loads(canonical_bytes(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RecoveryError(f"{label} is not canonical JSON data") from error


def _qualification_material_path(value: Any, *, manifest: bool) -> str:
    label = (
        "qualification run manifest path"
        if manifest
        else "qualification evidence path"
    )
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or not value.isascii()
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/"
            for character in value
        )
    ):
        raise RecoveryError(f"{label} is invalid")
    path = Path(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or path.parts[:2] != ("logs", "acceptance")
        or any(part in {"", ".", ".."} for part in path.parts)
        or not value.endswith(".json")
        or value.endswith(".run.json") is not manifest
    ):
        raise RecoveryError(f"{label} is invalid")
    return value


def validate_qualification_evidence_receipt(value: Any) -> dict[str, Any]:
    """Return a strict canonical clone of one full-qualification receipt."""

    receipt = _canonical_clone(value, label="qualification evidence receipt")
    if (
        not isinstance(receipt, dict)
        or set(receipt) != QUALIFICATION_EVIDENCE_RECEIPT_KEYS
        or receipt.get("policyId") != QUALIFICATION_RECEIPT_POLICY_ID
    ):
        raise RecoveryError(
            "qualification evidence receipt has an invalid shape or policy"
        )
    receipt["evidencePath"] = _qualification_material_path(
        receipt.get("evidencePath"), manifest=False
    )
    receipt["runManifestPath"] = _qualification_material_path(
        receipt.get("runManifestPath"), manifest=True
    )
    for key in QUALIFICATION_EVIDENCE_RECEIPT_KEYS - {
        "policyId",
        "evidencePath",
        "runManifestPath",
    }:
        if not _is_sha256(receipt.get(key)):
            raise RecoveryError(f"qualification evidence receipt {key} is invalid")
    return receipt


def _qualification_result_sha256(
    action: dict[str, Any], receipt: dict[str, Any]
) -> str:
    return canonical_sha256(
        {
            "policyId": QUALIFICATION_RESULT_POLICY_ID,
            "action": {
                "ordinal": action["ordinal"],
                "kind": action["kind"],
                "subject": action["subject"],
                "catalogId": action["catalogId"],
            },
            "qualificationEvidence": receipt,
        }
    )


def _validated_rollout_qualification(value: Any) -> RolloutQualification:
    try:
        return RolloutQualification.from_document(value)
    except DeploymentSpecError as error:
        raise RecoveryError(
            "rollout plan v2 requires its exact full qualification contract"
        ) from error


def _qualification_binding(
    document: dict[str, Any],
    intent: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any]:
    qualification = _validated_rollout_qualification(
        intent["rolloutPlan"].get("qualification")
    )
    return {
        "policyId": ROLLOUT_QUALIFICATION_BINDING_POLICY_ID,
        "transactionId": document["id"],
        "operation": "upgrade",
        "rolloutPlanSha256": intent["rolloutPlanSha256"],
        "actionOrdinal": action["ordinal"],
        "actionKind": "target-full",
        "rollbackSpecSha256": intent["rollbackSpecSha256"],
        "sourceCatalogSpecSha256": intent["sourceCatalogSpecSha256"],
        "targetCatalogSpecSha256": intent["targetCatalogSpecSha256"],
        "performancePolicySha256": qualification.performance_policy_sha256,
        "modelPortSourceIdentitySha256": (
            qualification.modelport_source_identity_sha256
        ),
        "qualificationInputSha256": qualification.qualification_input_sha256,
    }


def _validate_rollout_plan(
    value: Any,
    *,
    operation: str,
    target: str,
    rollback_spec_sha256: str,
    source_catalog_spec_sha256: str,
    target_catalog_spec_sha256: str,
    approved_spec: CatalogDeploymentSpec,
) -> dict[str, Any]:
    plan = _canonical_clone(value, label="rollout plan")
    if not isinstance(plan, dict):
        raise RecoveryError("rollout plan has an invalid shape")
    plan_schema_version = plan.get("schemaVersion")
    if not isinstance(plan_schema_version, int) or isinstance(
        plan_schema_version, bool
    ):
        raise RecoveryError("rollout plan has an invalid schema")
    expected_plan_keys = (
        ROLLOUT_PLAN_KEYS
        if plan_schema_version == 1
        else ROLLOUT_PLAN_V2_KEYS
        if plan_schema_version == 2
        else frozenset()
    )
    if not expected_plan_keys or set(plan) != expected_plan_keys:
        raise RecoveryError("rollout plan has an invalid shape")
    if (
        plan.get("operation") != operation
        or plan.get("rollbackSpecSha256") != rollback_spec_sha256
        or plan.get("sourceCatalogSpecSha256")
        != source_catalog_spec_sha256
        or plan.get("targetCatalogSpecSha256")
        != target_catalog_spec_sha256
        or plan.get("requiresApproval") is not True
    ):
        raise RecoveryError("rollout plan does not match its persisted intent")
    if plan_schema_version == 1:
        if plan.get("requiredAcceptanceTier") != "quick":
            raise RecoveryError(
                "rollout plan v1 can prove only quick acceptance"
            )
    else:
        if operation != "upgrade" or plan.get("requiredAcceptanceTier") != "full":
            raise RecoveryError(
                "rollout plan v2 requires its exact full qualification contract"
            )
        _validated_rollout_qualification(plan.get("qualification"))
    if (
        target != approved_spec.catalog_id
        or target_catalog_spec_sha256 != approved_spec.sha256
        or source_catalog_spec_sha256 == target_catalog_spec_sha256
    ):
        raise RecoveryError(
            "rollout source, target, and approved Catalog spec are inconsistent"
        )

    actions = plan.get("actions")
    if not isinstance(actions, list) or not actions or len(actions) > 128:
        raise RecoveryError("rollout plan must contain a bounded action sequence")
    source_ids: set[str] = set()
    target_artifacts: list[str] = []
    for ordinal, action in enumerate(actions):
        if not isinstance(action, dict):
            raise RecoveryError("rollout action must be an object")
        kind = action.get("kind")
        expected_keys = (
            ROLLOUT_ACTION_KEYS | {"artifactSha256"}
            if kind == "fetch-target-artifact"
            else ROLLOUT_ACTION_KEYS
        )
        expected_subject = ROLLOUT_ACTION_SUBJECTS.get(kind)
        catalog_id = action.get("catalogId")
        if (
            set(action) != expected_keys
            or not isinstance(action.get("ordinal"), int)
            or isinstance(action.get("ordinal"), bool)
            or action.get("ordinal") != ordinal
            or expected_subject is None
            or action.get("subject") != expected_subject
            or not isinstance(catalog_id, str)
            or not catalog_id
            or len(catalog_id) > 128
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in catalog_id
            )
        ):
            raise RecoveryError("rollout action identity or order is invalid")
        if expected_subject == "target":
            if catalog_id != approved_spec.catalog_id:
                raise RecoveryError(
                    "rollout target action does not match the approved Catalog spec"
                )
        else:
            source_ids.add(catalog_id)
        if kind == "fetch-target-artifact":
            artifact_sha256 = action.get("artifactSha256")
            if not _is_sha256(artifact_sha256):
                raise RecoveryError("rollout artifact SHA256 is invalid")
            target_artifacts.append(artifact_sha256)
    if len(source_ids) != 1 or approved_spec.catalog_id in source_ids:
        raise RecoveryError("rollout source Catalog identity is ambiguous")

    required_artifacts = [
        artifact.sha256 for artifact in approved_spec.artifacts if artifact.required
    ]
    if operation == "upgrade":
        expected_kinds = (
            ["source-quick"]
            + ["fetch-target-artifact"] * len(required_artifacts)
            + [
                "stop-source",
                "activate-target",
                "start-target",
                "target-quick",
                *(["target-full"] if plan_schema_version == 2 else []),
                "publish-rollback",
            ]
        )
        if target_artifacts != required_artifacts:
            raise RecoveryError(
                "upgrade plan does not bind every required target artifact exactly once"
            )
    elif operation == "rollback":
        expected_kinds = [
            "stop-source",
            "activate-target",
            "start-target",
            "target-quick",
            "clear-rollback",
        ]
        if target_artifacts:
            raise RecoveryError("rollback plan cannot authorize artifact acquisition")
    else:
        raise RecoveryError("rollout operation is unsupported")
    if [action["kind"] for action in actions] != expected_kinds:
        raise RecoveryError("rollout action sequence is not the approved lifecycle")
    return plan


def _validate_rollout_intent(
    value: Any,
    *,
    operation: str,
    target: str,
    approved_spec: CatalogDeploymentSpec,
) -> dict[str, Any]:
    intent = _canonical_clone(value, label="rollout intent")
    if not isinstance(intent, dict) or set(intent) != ROLLOUT_INTENT_KEYS:
        raise RecoveryError("rollout intent has an invalid shape")
    rollback_spec_sha256 = intent.get("rollbackSpecSha256")
    source_catalog_spec_sha256 = intent.get("sourceCatalogSpecSha256")
    target_catalog_spec_sha256 = intent.get("targetCatalogSpecSha256")
    if (
        not _is_sha256(rollback_spec_sha256)
        or not _is_sha256(source_catalog_spec_sha256)
        or not _is_sha256(target_catalog_spec_sha256)
        or not _is_sha256(intent.get("rolloutPlanSha256"))
    ):
        raise RecoveryError("rollout intent has an invalid policy or digest")
    plan = _validate_rollout_plan(
        intent.get("rolloutPlan"),
        operation=operation,
        target=target,
        rollback_spec_sha256=rollback_spec_sha256,
        source_catalog_spec_sha256=source_catalog_spec_sha256,
        target_catalog_spec_sha256=target_catalog_spec_sha256,
        approved_spec=approved_spec,
    )
    expected_policy_id = (
        ROLLOUT_INTENT_POLICY_ID
        if plan["schemaVersion"] == 1
        else ROLLOUT_INTENT_POLICY_ID_V2
    )
    if intent.get("policyId") != expected_policy_id:
        raise RecoveryError(
            "rollout intent policy does not match its plan schema"
        )
    if canonical_sha256(plan) != intent["rolloutPlanSha256"]:
        raise RecoveryError("rollout plan digest does not match its document")

    previous_pointer = intent.get("previousRollbackPointer")
    if previous_pointer is not None:
        from .rollout import RollbackPointer, RollbackSpecError

        try:
            parsed_pointer = RollbackPointer.from_document(previous_pointer)
        except RollbackSpecError as error:
            raise RecoveryError(
                f"previous rollback pointer is invalid: {error}"
            ) from error
        if (
            operation == "rollback"
            and parsed_pointer.active_spec_sha256 != rollback_spec_sha256
        ):
            raise RecoveryError(
                "rollback intent does not match the active rollback pointer"
            )
    elif operation == "rollback":
        raise RecoveryError("rollback intent requires an active rollback pointer")
    intent["rolloutPlan"] = plan
    return intent


def _rollout_intent(
    document: dict[str, Any], approved_spec: CatalogDeploymentSpec | None = None
) -> dict[str, Any] | None:
    value = document.get("rolloutIntent")
    operation = document.get("operation")
    if value is None:
        if operation in ROLLOUT_OPERATIONS:
            raise RecoveryError("rollout transaction has no persisted rollout intent")
        if "rolloutActionOrdinal" in document or "rolloutActionResults" in document:
            raise RecoveryError("non-rollout transaction has rollout progress fields")
        return None
    if operation not in ROLLOUT_OPERATIONS:
        raise RecoveryError("only upgrade or rollback may persist a rollout intent")
    if approved_spec is None:
        approved_spec = _approved_deployment(document)
    if approved_spec is None:
        raise RecoveryError("rollout transaction has no approved target Catalog spec")
    return _validate_rollout_intent(
        value,
        operation=operation,
        target=document.get("target"),
        approved_spec=approved_spec,
    )


def _validate_rollout_progress(
    document: dict[str, Any], intent: dict[str, Any] | None
) -> None:
    if intent is None:
        return
    actions = intent["rolloutPlan"]["actions"]
    ordinal = document.get("rolloutActionOrdinal")
    results = document.get("rolloutActionResults")
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or ordinal < 0
        or ordinal > len(actions)
        or not isinstance(results, list)
        or len(results) != ordinal
    ):
        raise RecoveryError("rollout action progress is invalid")
    for expected_ordinal, result in enumerate(results):
        action = actions[expected_ordinal]
        qualification_action = action["kind"] == "target-full"
        expected_result_keys = ROLLOUT_ACTION_RESULT_KEYS | (
            {"qualificationEvidence"} if qualification_action else set()
        )
        if (
            not isinstance(result, dict)
            or set(result) != expected_result_keys
            or not isinstance(result.get("ordinal"), int)
            or isinstance(result.get("ordinal"), bool)
            or result.get("ordinal") != expected_ordinal
            or result.get("kind") != action["kind"]
            or result.get("subject") != action["subject"]
            or result.get("catalogId") != action["catalogId"]
            or not _is_sha256(result.get("resultSha256"))
            or not isinstance(result.get("completedAt"), str)
            or not result["completedAt"]
        ):
            raise RecoveryError("rollout action result journal is invalid")
        if qualification_action:
            receipt = validate_qualification_evidence_receipt(
                result.get("qualificationEvidence")
            )
            if result["resultSha256"] != _qualification_result_sha256(
                action, receipt
            ):
                raise RecoveryError(
                    "rollout qualification result does not match its evidence receipt"
                )
            if receipt["rolloutBindingSha256"] != canonical_sha256(
                _qualification_binding(document, intent, action)
            ):
                raise RecoveryError(
                    "rollout qualification receipt does not match its transaction binding"
                )
    if (
        document.get("state") in {"completed", "superseded-verified"}
        and ordinal != len(actions)
    ):
        raise RecoveryError(
            "rollout transaction became successful-terminal before all actions completed"
        )


def verify_completed_upgrade_qualification(
    document: Any,
    *,
    evidence_path: str,
    evidence_self_sha256: str,
    rollout_binding_sha256: str,
) -> dict[str, Any]:
    """Return the exact receipt only for a completed transaction-bound full run."""

    TransactionStore._validate(document)
    if (
        not isinstance(document, dict)
        or document.get("schemaVersion") != SCHEMA_VERSION
        or document.get("operation") != "upgrade"
        or document.get("state") != "completed"
    ):
        raise RecoveryError(
            "qualification evidence requires a completed v2 upgrade transaction"
        )
    approved_spec = _approved_deployment(document)
    intent = _rollout_intent(document, approved_spec)
    if intent is None:
        raise RecoveryError("completed upgrade has no rollout intent")
    _validate_rollout_progress(document, intent)
    plan = intent["rolloutPlan"]
    if (
        plan.get("schemaVersion") != 2
        or plan.get("requiredAcceptanceTier") != "full"
    ):
        raise RecoveryError(
            "completed upgrade has no exact full qualification contract"
        )
    _validated_rollout_qualification(plan.get("qualification"))
    actions = plan["actions"]
    qualification_ordinals = [
        action["ordinal"] for action in actions if action["kind"] == "target-full"
    ]
    if len(qualification_ordinals) != 1:
        raise RecoveryError(
            "completed upgrade does not contain exactly one target-full action"
        )
    receipt = document["rolloutActionResults"][qualification_ordinals[0]].get(
        "qualificationEvidence"
    )
    receipt = validate_qualification_evidence_receipt(receipt)
    expected_path = _qualification_material_path(evidence_path, manifest=False)
    if not _is_sha256(evidence_self_sha256) or not _is_sha256(
        rollout_binding_sha256
    ):
        raise RecoveryError("qualification evidence lookup digest is invalid")
    if (
        receipt["evidencePath"] != expected_path
        or receipt["evidenceSelfSha256"] != evidence_self_sha256
        or receipt["rolloutBindingSha256"] != rollout_binding_sha256
    ):
        raise RecoveryError(
            "qualification evidence does not match the completed transaction receipt"
        )
    return receipt


def _approved_deployment(document: dict[str, Any]) -> CatalogDeploymentSpec | None:
    spec_value = document.get("approvedCatalogSpec")
    digest_value = document.get("approvedCatalogSpecSha256")
    if spec_value is None and digest_value is None:
        return None
    try:
        spec = parse_approved_deployment(
            {
                "schemaVersion": 1,
                "approvedCatalogSpecSha256": digest_value,
                "catalogSpec": spec_value,
            }
        )
    except DeploymentSpecError as error:
        raise RecoveryError(f"transaction has an invalid approved Catalog spec: {error}") from error
    if (
        document.get("operation") not in CATALOG_BOUND_OPERATIONS
        or document.get("target") != spec.catalog_id
    ):
        raise RecoveryError(
            "transaction approved Catalog spec does not match its operation and target"
        )
    return spec


def _require_approved_deployment(
    document: dict[str, Any],
    *,
    catalog_spec_sha256: str,
    catalog_id: str,
    artifact_sha256: str | None = None,
) -> CatalogDeploymentSpec:
    spec = _approved_deployment(document)
    if spec is None:
        raise RecoveryError(
            "Catalog-bound transaction has no persisted approved Catalog spec"
        )
    if spec.sha256 != catalog_spec_sha256 or spec.catalog_id != catalog_id:
        raise RecoveryError(
            "runtime mutation does not match the transaction's approved Catalog spec"
        )
    if artifact_sha256 is not None:
        matches = [
            artifact
            for artifact in spec.artifacts
            if artifact.required and artifact.sha256 == artifact_sha256
        ]
        if len(matches) != 1:
            raise RecoveryError(
                "runtime mutation artifact does not match exactly one approved artifact"
            )
    return spec


def _rollout_binding_from_environment(
    rollout_subject: str | None,
    action_ordinal: int | None,
    action_kind: str | None,
) -> tuple[str | None, int | None, str | None]:
    if rollout_subject is None:
        rollout_subject = os.environ.get("LOCAL_INFERENCE_ROLLOUT_SUBJECT")
    if action_ordinal is None:
        encoded_ordinal = os.environ.get("LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL")
        if encoded_ordinal is not None:
            if (
                not encoded_ordinal
                or not encoded_ordinal.isascii()
                or not encoded_ordinal.isdecimal()
                or (len(encoded_ordinal) > 1 and encoded_ordinal.startswith("0"))
            ):
                raise RecoveryError(
                    "rollout action ordinal must be a decimal integer"
                )
            action_ordinal = int(encoded_ordinal)
    if action_kind is None:
        action_kind = os.environ.get("LOCAL_INFERENCE_ROLLOUT_ACTION_KIND")
    return rollout_subject, action_ordinal, action_kind


def _require_rollout_action(
    document: dict[str, Any],
    *,
    catalog_spec_sha256: str,
    catalog_id: str,
    rollout_subject: str | None,
    action_ordinal: int | None,
    action_kind: str | None,
    artifact_sha256: str | None = None,
) -> CatalogDeploymentSpec:
    if document.get("state") in ROLLOUT_RECOVERY_STATES:
        raise RecoveryError(
            "a recovering rollout has no pending action authority"
        )
    approved_spec = _approved_deployment(document)
    intent = _rollout_intent(document, approved_spec)
    if approved_spec is None or intent is None:
        raise RecoveryError("rollout transaction authority is incomplete")
    rollout_subject, action_ordinal, action_kind = _rollout_binding_from_environment(
        rollout_subject, action_ordinal, action_kind
    )
    if rollout_subject not in {"source", "target"}:
        raise RecoveryError("rollout subject must be supplied as source or target")
    if (
        not isinstance(action_ordinal, int)
        or isinstance(action_ordinal, bool)
        or action_ordinal < 0
    ):
        raise RecoveryError("rollout action ordinal must be supplied")
    current_ordinal = document.get("rolloutActionOrdinal")
    actions = intent["rolloutPlan"]["actions"]
    if action_ordinal != current_ordinal or action_ordinal >= len(actions):
        raise RecoveryError(
            "rollout action ordinal does not identify the next pending action"
        )
    action = actions[action_ordinal]
    if (
        action["kind"] != action_kind
        or action["subject"] != rollout_subject
        or action["catalogId"] != catalog_id
        or intent[f"{rollout_subject}CatalogSpecSha256"]
        != catalog_spec_sha256
    ):
        raise RecoveryError(
            "runtime mutation does not match the pending rollout action subject"
        )
    expected_artifact = action.get("artifactSha256")
    if expected_artifact != artifact_sha256:
        raise RecoveryError(
            "runtime mutation artifact does not match the pending rollout action"
        )
    if rollout_subject == "target":
        return _require_approved_deployment(
            document,
            catalog_spec_sha256=catalog_spec_sha256,
            catalog_id=catalog_id,
            artifact_sha256=artifact_sha256,
        )
    return approved_spec


def recovery_original_is_safe(original: Any) -> bool:
    """Return whether an original-runtime record is safe to restore automatically."""
    if not isinstance(original, dict) or original.get("capturedWithoutSecrets") is not True:
        return False
    healthy = original.get("healthy")
    if not isinstance(healthy, bool):
        return False
    profile = original.get("profile")
    if healthy and profile not in {"latency", "throughput"}:
        return False
    container_name = original.get("containerName")
    if container_name is not None and (
        not isinstance(container_name, str)
        or not container_name
        or len(container_name) > 128
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in container_name)
    ):
        return False
    deployment = original.get("deploymentProfile")
    if not isinstance(deployment, dict) or not isinstance(deployment.get("present"), bool):
        return False
    if deployment["present"]:
        values = deployment.get("values")
        digest = deployment.get("sha256")
        if (
            deployment.get("format") != "allowlisted-env-v1"
            or not isinstance(values, dict)
            or not RECOVERY_REQUIRED_DEPLOYMENT_KEYS.issubset(values)
            or not set(values).issubset(RECOVERY_DEPLOYMENT_KEYS)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items())
            or deployment.get("containsCredentials") is not False
            or not isinstance(digest, str)
            or hashlib.sha256(
                json.dumps(
                    values,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            != digest
        ):
            return False
    if healthy:
        identity = original.get("runtimeIdentity")
        if (
            original.get("containerHealthy") is not True
            or not isinstance(identity, dict)
            or not isinstance(identity.get("configuration"), dict)
            or not isinstance(identity.get("sha256"), str)
            or len(identity["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in identity["sha256"])
            or not container_name
        ):
            return False
    return True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_private_json_at(
    directory_descriptor: int, name: str, *, label: str
) -> dict[str, Any]:
    """Read a stable private JSON name relative to an already verified directory."""

    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise RecoveryError(f"cannot open {label}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        named = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > MAX_TRANSACTION_BYTES
            or (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise RecoveryError(
                f"{label} is not a bounded private current-user regular file"
            )
        chunks: list[bytes] = []
        remaining = MAX_TRANSACTION_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        final = os.fstat(descriptor)
        named_final = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_nlink,
        )
        if (
            len(body) > MAX_TRANSACTION_BYTES
            or identity(final) != identity(metadata)
            or (named_final.st_dev, named_final.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise RecoveryError(f"{label} changed while it was read")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"{label} is unreadable: {exc}") from exc
        if not isinstance(document, dict):
            raise RecoveryError(f"{label} must contain a JSON object")
        return document
    finally:
        os.close(descriptor)


def _atomic_json_noreplace(
    path: Path, document: dict[str, Any]
) -> dict[str, Any]:
    """Publish JSON atomically without ever replacing an existing name."""

    body = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(body) > MAX_TRANSACTION_BYTES:
        raise RecoveryError("transaction archive exceeds the bounded size policy")
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_descriptor = os.open(path.parent, flags)
    except OSError as exc:
        raise RecoveryError(f"cannot open the transaction archive directory: {exc}") from exc
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        metadata = os.fstat(directory_descriptor)
        named = path.parent.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise RecoveryError(
                "transaction archive directory is not private and current-user-owned"
            )
        cleanup_interrupted_noreplace_link_at(directory_descriptor, path.name)
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RecoveryError("transaction archive write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            pass
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_name = ""
        os.fsync(directory_descriptor)
        archived = _read_private_json_at(
            directory_descriptor,
            path.name,
            label="transaction archive",
        )
        try:
            named_final = path.parent.lstat()
        except OSError as exc:
            raise RecoveryError(
                f"transaction archive directory changed during publish: {exc}"
            ) from exc
        if (named_final.st_dev, named_final.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise RecoveryError("transaction archive directory changed during publish")
        return archived
    except OSError as exc:
        raise RecoveryError(f"cannot publish the transaction archive safely: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        os.close(directory_descriptor)


def _read_private_json(path: Path, *, label: str) -> dict[str, Any]:
    """Read one bounded private JSON file through a stable no-follow descriptor."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RecoveryError(f"cannot open {label}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        try:
            named = path.lstat()
        except OSError as exc:
            raise RecoveryError(f"cannot inspect {label}: {exc}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > MAX_TRANSACTION_BYTES
            or (named.st_dev, named.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise RecoveryError(
                f"{label} is not a bounded private current-user regular file"
            )
        chunks: list[bytes] = []
        remaining = MAX_TRANSACTION_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        final = os.fstat(descriptor)
        try:
            named_final = path.lstat()
        except OSError as exc:
            raise RecoveryError(f"{label} changed while it was read: {exc}") from exc
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_nlink,
        )
        if (
            len(body) > MAX_TRANSACTION_BYTES
            or identity(final) != identity(metadata)
            or (named_final.st_dev, named_final.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise RecoveryError(f"{label} changed while it was read")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"{label} is unreadable: {exc}") from exc
        if not isinstance(document, dict):
            raise RecoveryError(f"{label} must contain a JSON object")
        return document
    finally:
        os.close(descriptor)


def _prepare_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise RecoveryError(
            f"lock directory is not private and current-user-owned: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        path.chmod(0o700)


def open_private_lock(path: Path) -> int:
    """Open a non-symlink private lock file and validate the opened inode."""
    _prepare_private_directory(path.parent)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RecoveryError(f"cannot open the private lock file: {path}: {exc}") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise RecoveryError(
            f"lock file is not a private current-user single-link regular file: {path}"
        )
    return descriptor


class TransactionStore:
    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        self.path = paths.transaction_path
        self.lock_path = paths.state_dir / "transaction.lock"
        self.archive_dir = paths.state_dir / "transactions"

    def archive_path(self, transaction_id: str) -> Path:
        """Return a traversal-safe content address for one transaction ID."""

        if not isinstance(transaction_id, str) or not transaction_id:
            raise RecoveryError("transaction archive requires a non-empty id")
        key = hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()
        return self.archive_dir / f"{key}.json"

    def _read_archive(self, transaction_id: str) -> dict[str, Any] | None:
        """Read one archive beneath a stable private no-follow directory."""

        archive = self.archive_path(transaction_id)
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            directory_descriptor = os.open(self.archive_dir, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RecoveryError(
                f"cannot open the transaction archive directory: {error}"
            ) from error
        try:
            metadata = os.fstat(directory_descriptor)
            try:
                named = self.archive_dir.lstat()
            except OSError as error:
                raise RecoveryError(
                    f"cannot inspect the transaction archive directory: {error}"
                ) from error
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or (named.st_dev, named.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise RecoveryError(
                    "transaction archive directory is not private and current-user-owned"
                )
            try:
                os.stat(
                    archive.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            document = _read_private_json_at(
                directory_descriptor,
                archive.name,
                label="transaction archive",
            )
            try:
                named_final = self.archive_dir.lstat()
            except OSError as error:
                raise RecoveryError(
                    f"transaction archive directory changed while it was read: {error}"
                ) from error
            if (named_final.st_dev, named_final.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise RecoveryError(
                    "transaction archive directory changed while it was read"
                )
            self._validate(document)
            if not is_terminal(document):
                raise RecoveryError(
                    "transaction archive does not contain a terminal transaction"
                )
            if document.get("id") != transaction_id:
                raise RecoveryError(
                    "transaction archive content does not match the requested id"
                )
            return document
        finally:
            os.close(directory_descriptor)

    def _archive_terminal(self, document: dict[str, Any]) -> Path:
        """Persist one exact terminal document before its single-slot pointer moves."""

        self._validate(document)
        if not is_terminal(document):
            raise RecoveryError("only a verified terminal transaction can be archived")
        path = self.archive_path(document["id"])
        _prepare_private_directory(self.archive_dir)
        archived = _atomic_json_noreplace(path, document)
        self._validate(archived)
        if archived != document:
            raise RecoveryError(
                "transaction archive conflicts with the terminal transaction"
            )
        return path

    def archive_current_terminal(self) -> Path:
        """Idempotently archive the current terminal slot under the store lock."""

        with self.locked():
            document = self.read()
            if not document or not is_terminal(document):
                raise RecoveryError("there is no verified terminal transaction to archive")
            return self._archive_terminal(document)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        descriptor = open_private_lock(self.lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def runtime_boundary(self) -> Iterator[None]:
        """Exclude a shell runtime mutation while a new transaction is published."""
        path = self.paths.root / "cache" / "locks" / "runtime.lock"
        descriptor = open_private_lock(path)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RecoveryError(
                    "a runtime mutation already holds the local lock; retry after it finishes"
                ) from exc
            yield
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def authorized_runtime_mutation(
        self,
        transaction_id: str | None = None,
        *,
        catalog_spec_sha256: str | None = None,
        catalog_id: str | None = None,
        artifact_sha256: str | None = None,
        rollout_subject: str | None = None,
        action_ordinal: int | None = None,
        action_kind: str | None = None,
    ) -> Iterator[None]:
        """Serialize a runtime mutation and reject unrelated active transactions.

        Runtime-facing shell wrappers use the same lock order: runtime first,
        transaction second.  Holding both locks for the complete mutation closes
        the gap between checking transaction state and changing a selection or
        container.
        """
        provided = transaction_id or ""
        if provided:
            try:
                if str(uuid.UUID(provided)) != provided:
                    raise ValueError
            except ValueError as exc:
                raise RecoveryError(
                    "QWEN_CONTROL_TRANSACTION_ID must be a canonical UUID"
                ) from exc

        path = self.paths.root / "cache" / "locks" / "runtime.lock"
        descriptor = open_private_lock(path)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RecoveryError(
                    "another runtime mutation already holds the local lock"
                ) from exc
            with self.locked():
                document = self.read()
                if document and not is_terminal(document):
                    if not provided or document.get("id") != provided:
                        raise RecoveryError(
                            "an active control transaction blocks this runtime mutation; "
                            "use the matching public control-plane command",
                            facts={
                                "transactionId": document.get("id"),
                                "schemaVersion": document.get("schemaVersion"),
                                "state": document.get("state"),
                            },
                        )
                    operation = document.get("operation")
                    catalog_binding_required = bool(
                        document.get("schemaVersion") == SCHEMA_VERSION
                        and operation in CATALOG_BOUND_OPERATIONS
                        and document.get("state") not in ROLLOUT_RECOVERY_STATES
                    )
                    if (
                        catalog_binding_required
                        or catalog_spec_sha256 is not None
                        or catalog_id is not None
                        or artifact_sha256 is not None
                        or rollout_subject is not None
                        or action_ordinal is not None
                        or action_kind is not None
                    ):
                        if catalog_spec_sha256 is None or catalog_id is None:
                            raise RecoveryError(
                                "deployment digest and Catalog ID must be supplied together"
                            )
                        if operation in ROLLOUT_OPERATIONS:
                            _require_rollout_action(
                                document,
                                catalog_spec_sha256=catalog_spec_sha256,
                                catalog_id=catalog_id,
                                artifact_sha256=artifact_sha256,
                                rollout_subject=rollout_subject,
                                action_ordinal=action_ordinal,
                                action_kind=action_kind,
                            )
                        else:
                            if (
                                rollout_subject is not None
                                or action_ordinal is not None
                                or action_kind is not None
                            ):
                                raise RecoveryError(
                                    "non-rollout mutation cannot carry rollout authority"
                                )
                            _require_approved_deployment(
                                document,
                                catalog_spec_sha256=catalog_spec_sha256,
                                catalog_id=catalog_id,
                                artifact_sha256=artifact_sha256,
                            )
                elif provided:
                    raise RecoveryError(
                        "the authorized control transaction is missing or already terminal"
                    )
                yield
        finally:
            os.close(descriptor)

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        document = _read_private_json(self.path, label="transaction state")
        self._validate(document)
        return document

    def read_by_id(self, transaction_id: str) -> dict[str, Any] | None:
        """Safely read an exact current or archived transaction by identity."""

        if not isinstance(transaction_id, str) or not transaction_id:
            raise RecoveryError("transaction lookup requires a non-empty id")
        with self.locked():
            current = self.read()
            if current is not None and current.get("id") == transaction_id:
                return current
            return self._read_archive(transaction_id)

    def completed_upgrade_qualification(
        self,
        transaction_id: str,
        *,
        evidence_path: str,
        evidence_self_sha256: str,
        rollout_binding_sha256: str,
    ) -> dict[str, Any]:
        """Resolve an evidence identity through its exact completed transaction."""

        document = self.read_by_id(transaction_id)
        if document is None:
            raise RecoveryError("qualification transaction does not exist")
        return verify_completed_upgrade_qualification(
            document,
            evidence_path=evidence_path,
            evidence_self_sha256=evidence_self_sha256,
            rollout_binding_sha256=rollout_binding_sha256,
        )

    @staticmethod
    def _validate(document: Any) -> None:
        if (
            not isinstance(document, dict)
            or document.get("schemaVersion") not in READABLE_SCHEMA_VERSIONS
        ):
            raise RecoveryError("transaction state has an unsupported schema")
        states = LEGACY_STATES if document["schemaVersion"] == 1 else set(ALLOWED_TRANSITIONS)
        if document.get("state") not in states:
            raise RecoveryError("transaction state contains an unknown state")
        if not isinstance(document.get("id"), str) or not document["id"]:
            raise RecoveryError("transaction state has no id")
        if not isinstance(document.get("history"), list) or not document["history"]:
            raise RecoveryError("transaction state has no history")
        if (
            document["schemaVersion"] == SCHEMA_VERSION
            and not is_terminal(document)
            and not _valid_owner(document.get("owner"))
        ):
            raise RecoveryError("active transaction state has no verifiable process owner")
        if document["schemaVersion"] == SCHEMA_VERSION:
            approved_spec = _approved_deployment(document)
            intent = _rollout_intent(document, approved_spec)
            _validate_rollout_progress(document, intent)

    @staticmethod
    def initiator_status(document: dict[str, Any]) -> dict[str, Any]:
        """Classify whether a v2 transaction initiator can still be the same process."""
        owner = document.get("owner")
        if document.get("schemaVersion") != SCHEMA_VERSION or not _valid_owner(owner):
            return {
                "status": "unknown",
                "fenceEligible": False,
                "reason": "transaction has no verifiable v2 process owner",
            }
        assert isinstance(owner, dict)
        try:
            current_boot = _boot_id()
        except RecoveryError as exc:
            return {
                "status": "unknown",
                "fenceEligible": False,
                "reason": exc.summary,
            }
        if current_boot != owner["bootId"]:
            return {
                "status": "boot-changed",
                "fenceEligible": True,
                "reason": "transaction belongs to an earlier Linux boot",
            }
        observed_start = _process_start_time_ticks(owner["pid"])
        if observed_start is None:
            return {
                "status": "dead",
                "fenceEligible": True,
                "reason": "transaction initiator PID no longer exists",
            }
        if observed_start != owner["processStartTimeTicks"]:
            return {
                "status": "pid-reused",
                "fenceEligible": True,
                "reason": "transaction initiator PID was reused by another process",
            }
        return {
            "status": "alive",
            "fenceEligible": False,
            "reason": "transaction initiator process is still alive",
        }

    def begin(
        self,
        operation: str,
        target: str,
        original: dict[str, Any],
        *,
        approved_catalog_spec: dict[str, Any] | None = None,
        rollout_intent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runtime-facing wrappers acquire runtime.lock before transaction.lock.
        # Keep the same global order here so transaction publication cannot
        # deadlock with a concurrent select/start/stop operation.
        with self.runtime_boundary():
            with self.locked():
                self._prepare_begin_locked()
                return self._publish_begin_locked(
                    operation,
                    target,
                    original,
                    approved_catalog_spec=approved_catalog_spec,
                    rollout_intent=rollout_intent,
                )

    def begin_rollout(
        self,
        operation: str,
        target: str,
        capture: Callable[
            [str, str], tuple[dict[str, Any], dict[str, Any]]
        ],
        *,
        approved_catalog_spec: dict[str, Any],
    ) -> dict[str, Any]:
        """Capture and publish rollout authority inside one global lock boundary.

        The callback may inspect the source runtime and publish its immutable
        rollback object.  It runs only after an earlier active transaction has
        been excluded, while both runtime and transaction locks remain held.
        """

        if operation not in ROLLOUT_OPERATIONS:
            raise RecoveryError("begin_rollout only accepts upgrade or rollback")
        if not callable(capture):
            raise RecoveryError("rollout capture must be callable")
        try:
            approved_target = parse_approved_deployment(approved_catalog_spec)
        except DeploymentSpecError as error:
            raise RecoveryError(
                f"rollout target has an invalid approved Catalog spec: {error}"
            ) from error
        if approved_target.catalog_id != target:
            raise RecoveryError(
                "rollout target does not match its approved Catalog spec"
            )
        frozen_approval = approved_target.approval_document()
        with self.runtime_boundary():
            with self.locked():
                self._prepare_begin_locked()
                transaction_id = str(uuid.uuid4())
                created_at = utc_now()
                captured = capture(transaction_id, created_at)
                if not isinstance(captured, tuple) or len(captured) != 2:
                    raise RecoveryError(
                        "rollout capture must return (original, rollout_intent)"
                    )
                original, rollout_intent = captured
                if not isinstance(original, dict) or not isinstance(
                    rollout_intent, dict
                ):
                    raise RecoveryError(
                        "rollout capture returned an invalid transaction document"
                    )
                return self._publish_begin_locked(
                    operation,
                    target,
                    original,
                    approved_catalog_spec=frozen_approval,
                    rollout_intent=rollout_intent,
                    transaction_id=transaction_id,
                    created_at=created_at,
                )

    def _prepare_begin_locked(self) -> None:
        existing = self.read()
        if existing and not is_terminal(existing):
            raise RecoveryError(
                "an unfinished runtime transaction must be reconciled first",
                facts={
                    "transactionId": existing["id"],
                    "schemaVersion": existing["schemaVersion"],
                    "state": existing["state"],
                },
            )
        if existing:
            self._archive_terminal(existing)

    def _publish_begin_locked(
        self,
        operation: str,
        target: str,
        original: dict[str, Any],
        *,
        approved_catalog_spec: dict[str, Any] | None,
        rollout_intent: dict[str, Any] | None,
        transaction_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        now = created_at or utc_now()
        identifier = transaction_id or str(uuid.uuid4())
        document = {
            "schemaVersion": SCHEMA_VERSION,
            "id": identifier,
            "operation": operation,
            "target": target,
            "state": "planned",
            "createdAt": now,
            "updatedAt": now,
            "owner": transaction_owner(),
            "original": original,
            "history": [{"state": "planned", "at": now}],
            "recovery": {
                "action": "restore-production",
                "automatic": recovery_original_is_safe(original),
            },
        }
        if approved_catalog_spec is not None:
            try:
                approved_spec = parse_approved_deployment(approved_catalog_spec)
            except DeploymentSpecError as error:
                raise RecoveryError(
                    f"transaction has an invalid approved Catalog spec: {error}"
                ) from error
            document["approvedCatalogSpecSha256"] = approved_spec.sha256
            document["approvedCatalogSpec"] = approved_spec.document()
        if rollout_intent is not None:
            document["rolloutIntent"] = rollout_intent
            document["rolloutActionOrdinal"] = 0
            document["rolloutActionResults"] = []
        approved_spec = _approved_deployment(document)
        intent = _rollout_intent(document, approved_spec)
        if intent is not None:
            # Persist the validated deep clone, never the caller-owned object
            # that could be mutated after validation but before serialization.
            document["rolloutIntent"] = intent
        _validate_rollout_progress(document, intent)
        _atomic_json(self.path, document)
        return document

    def assert_approved_deployment(
        self,
        *,
        transaction_id: str,
        catalog_spec_sha256: str,
        catalog_id: str,
        artifact_sha256: str | None = None,
        rollout_subject: str | None = None,
        action_ordinal: int | None = None,
        action_kind: str | None = None,
        inherited_locks: bool = False,
    ) -> CatalogDeploymentSpec:
        """Bind a child action to its still-active persisted Catalog authority."""

        try:
            if str(uuid.UUID(transaction_id)) != transaction_id:
                raise ValueError
        except ValueError as error:
            raise RecoveryError("control transaction id must be a canonical UUID") from error
        if inherited_locks:
            expected = {
                203: self.paths.root / "cache" / "locks" / "runtime.lock",
                204: self.lock_path,
            }
            for descriptor, path in expected.items():
                try:
                    actual = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
                    wanted = path.resolve(strict=True)
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, RuntimeError) as error:
                    raise RecoveryError(
                        "inherited deployment lock descriptors are not valid"
                    ) from error
                if actual != wanted:
                    raise RecoveryError(
                        "inherited deployment lock descriptors target other files"
                    )
        boundary = contextlib.nullcontext() if inherited_locks else self.locked()
        with boundary:
            document = self.read()
            if (
                not document
                or is_terminal(document)
                or document.get("schemaVersion") != SCHEMA_VERSION
                or document.get("id") != transaction_id
            ):
                raise RecoveryError(
                    "approved Catalog deployment transaction is missing, changed, or terminal"
                )
            if document.get("operation") in ROLLOUT_OPERATIONS:
                return _require_rollout_action(
                    document,
                    catalog_spec_sha256=catalog_spec_sha256,
                    catalog_id=catalog_id,
                    artifact_sha256=artifact_sha256,
                    rollout_subject=rollout_subject,
                    action_ordinal=action_ordinal,
                    action_kind=action_kind,
                )
            if (
                rollout_subject is not None
                or action_ordinal is not None
                or action_kind is not None
            ):
                raise RecoveryError(
                    "non-rollout deployment cannot carry rollout authority"
                )
            return _require_approved_deployment(
                document,
                catalog_spec_sha256=catalog_spec_sha256,
                catalog_id=catalog_id,
                artifact_sha256=artifact_sha256,
            )

    def pending_rollout_qualification_binding(
        self,
        *,
        transaction_id: str,
        catalog_spec_sha256: str,
        catalog_id: str,
        rollout_subject: str | None = None,
        action_ordinal: int | None = None,
        action_kind: str | None = None,
    ) -> dict[str, Any]:
        """Bind evidence creation to the one active pending ``target-full`` action."""

        try:
            if str(uuid.UUID(transaction_id)) != transaction_id:
                raise ValueError
        except ValueError as error:
            raise RecoveryError(
                "control transaction id must be a canonical UUID"
            ) from error
        rollout_subject, action_ordinal, action_kind = (
            _rollout_binding_from_environment(
                rollout_subject, action_ordinal, action_kind
            )
        )
        if rollout_subject != "target" or action_kind != "target-full":
            raise RecoveryError(
                "full qualification requires target-full rollout authority"
            )
        with self.locked():
            document = self.read()
            if (
                not document
                or is_terminal(document)
                or document.get("schemaVersion") != SCHEMA_VERSION
                or document.get("id") != transaction_id
                or document.get("operation") != "upgrade"
                or document.get("state") != "accepting"
            ):
                raise RecoveryError(
                    "full qualification transaction is missing, changed, or terminal"
                )
            _require_rollout_action(
                document,
                catalog_spec_sha256=catalog_spec_sha256,
                catalog_id=catalog_id,
                rollout_subject=rollout_subject,
                action_ordinal=action_ordinal,
                action_kind=action_kind,
            )
            intent = _rollout_intent(document, _approved_deployment(document))
            assert intent is not None
            plan = intent["rolloutPlan"]
            if (
                plan.get("schemaVersion") != 2
                or plan.get("requiredAcceptanceTier") != "full"
            ):
                raise RecoveryError(
                    "pending rollout action has no exact full qualification contract"
                )
            _validated_rollout_qualification(plan.get("qualification"))
            assert isinstance(action_ordinal, int)
            return _qualification_binding(
                document, intent, plan["actions"][action_ordinal]
            )

    def fence_orphaned(self, *, expected_id: str) -> dict[str, Any]:
        """Fence a proven-dead initiator while excluding descendant runtime work."""
        with self.runtime_boundary():
            with self.locked():
                document = self.read()
                if not document or document.get("id") != expected_id:
                    raise RecoveryError("orphaned transaction identity changed")
                if (
                    document.get("schemaVersion") != SCHEMA_VERSION
                    or document.get("state") == "recovery_required"
                    or is_terminal(document)
                ):
                    raise RecoveryError(
                        "only an active v2 transaction can be fenced as orphaned"
                    )
                initiator = self.initiator_status(document)
                if not initiator["fenceEligible"]:
                    raise RecoveryError(
                        "active transaction initiator cannot be proven dead",
                        facts={"initiator": initiator},
                    )
                now = utc_now()
                detail = f"orphaned initiator fenced: {initiator['status']}"
                document["state"] = "recovery_required"
                document["updatedAt"] = now
                document["history"].append(
                    {
                        "state": "recovery_required",
                        "at": now,
                        "detail": detail,
                        "fence": {
                            "policy": "dead-process-and-free-runtime-lock-v1",
                            "initiatorStatus": initiator["status"],
                        },
                    }
                )
                _atomic_json(self.path, document)
                return document

    def transition(
        self,
        target_state: str,
        *,
        expected_id: str,
        expected_state: str | None = None,
        expected_action_ordinal: int | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        with self.locked():
            document = self.read()
            if not document:
                raise RecoveryError("there is no transaction to update")
            if document.get("id") != expected_id:
                raise RecoveryError(
                    "transaction identity changed before the requested transition",
                    facts={
                        "expectedTransactionId": expected_id,
                        "actualTransactionId": document.get("id"),
                    },
                )
            if document["schemaVersion"] != SCHEMA_VERSION:
                raise RecoveryError(
                    "legacy transaction state is read-only; use explicit reconciliation"
                )
            current = document["state"]
            if expected_state is not None and current != expected_state:
                raise RecoveryError(
                    "transaction state changed before the requested transition"
                )
            intent = _rollout_intent(document, _approved_deployment(document))
            if expected_action_ordinal is not None:
                if intent is None:
                    raise RecoveryError(
                        "non-rollout transaction has no action ordinal to compare"
                    )
                if (
                    not isinstance(expected_action_ordinal, int)
                    or isinstance(expected_action_ordinal, bool)
                    or expected_action_ordinal < 0
                    or document.get("rolloutActionOrdinal")
                    != expected_action_ordinal
                ):
                    raise RecoveryError(
                        "rollout action ordinal changed before the requested transition"
                    )
            if (
                intent is not None
                and target_state in {"completed", "superseded-verified"}
                and document.get("rolloutActionOrdinal")
                != len(intent["rolloutPlan"]["actions"])
            ):
                raise RecoveryError(
                    "rollout transaction cannot become successful-terminal "
                    "before all actions complete"
                )
            if (
                intent is not None
                and target_state == "failed-restored"
                and (not isinstance(detail, str) or not detail.strip())
            ):
                raise RecoveryError(
                    "failed-restored rollout transition requires recovery verification detail"
                )
            if target_state not in ALLOWED_TRANSITIONS[current]:
                raise RecoveryError(f"invalid transaction transition: {current} -> {target_state}")
            now = utc_now()
            event: dict[str, Any] = {"state": target_state, "at": now}
            if detail:
                event["detail"] = detail[:500]
            document["state"] = target_state
            document["updatedAt"] = now
            document["history"].append(event)
            _atomic_json(self.path, document)
            if is_terminal(document):
                self._archive_terminal(document)
            return document

    def advance_rollout_action(
        self,
        *,
        expected_id: str,
        expected_state: str,
        expected_action_ordinal: int,
        expected_action_kind: str,
        result_sha256: str | None = None,
        qualification_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """CAS one exact typed action result into the durable rollout journal."""
        with self.locked():
            document = self.read()
            if not document or document.get("id") != expected_id:
                raise RecoveryError(
                    "transaction identity changed before rollout action advancement"
                )
            if document.get("schemaVersion") != SCHEMA_VERSION or is_terminal(document):
                raise RecoveryError("only an active v2 rollout can advance an action")
            if document.get("state") != expected_state:
                raise RecoveryError(
                    "transaction state changed before rollout action advancement"
                )
            if document["state"] in ROLLOUT_RECOVERY_STATES:
                raise RecoveryError(
                    "a recovering transaction cannot advance its rollout action plan"
                )
            approved_spec = _approved_deployment(document)
            intent = _rollout_intent(document, approved_spec)
            if intent is None:
                raise RecoveryError("transaction has no rollout action plan")
            current_ordinal = document.get("rolloutActionOrdinal")
            actions = intent["rolloutPlan"]["actions"]
            if (
                not isinstance(expected_action_ordinal, int)
                or isinstance(expected_action_ordinal, bool)
                or expected_action_ordinal != current_ordinal
                or expected_action_ordinal >= len(actions)
            ):
                raise RecoveryError(
                    "rollout action ordinal cannot be skipped or replayed"
                )
            action = actions[expected_action_ordinal]
            if action["kind"] != expected_action_kind:
                raise RecoveryError(
                    "rollout action kind does not match the next pending action"
                )
            result: dict[str, Any]
            if action["kind"] == "target-full":
                if document.get("state") != "accepting":
                    raise RecoveryError(
                        "target-full can complete only in the accepting state"
                    )
                if result_sha256 is not None:
                    raise RecoveryError(
                        "target-full result SHA256 is derived from its evidence receipt"
                    )
                receipt = validate_qualification_evidence_receipt(
                    qualification_evidence
                )
                binding = _qualification_binding(document, intent, action)
                if receipt["rolloutBindingSha256"] != canonical_sha256(binding):
                    raise RecoveryError(
                        "qualification evidence receipt does not match the pending rollout binding"
                    )
                normalized_result_sha256 = _qualification_result_sha256(
                    action, receipt
                )
                result = {
                    "ordinal": expected_action_ordinal,
                    "kind": action["kind"],
                    "subject": action["subject"],
                    "catalogId": action["catalogId"],
                    "resultSha256": normalized_result_sha256,
                    "qualificationEvidence": receipt,
                }
            else:
                if qualification_evidence is not None:
                    raise RecoveryError(
                        "only target-full may persist qualification evidence"
                    )
                if not _is_sha256(result_sha256):
                    raise RecoveryError("rollout action result SHA256 is invalid")
                result = {
                    "ordinal": expected_action_ordinal,
                    "kind": action["kind"],
                    "subject": action["subject"],
                    "catalogId": action["catalogId"],
                    "resultSha256": result_sha256,
                }
            completed_at = utc_now()
            result["completedAt"] = completed_at
            document["rolloutActionResults"].append(result)
            document["rolloutActionOrdinal"] = expected_action_ordinal + 1
            document["updatedAt"] = completed_at
            _validate_rollout_progress(document, intent)
            _atomic_json(self.path, document)
            return document

    def resolve_legacy_failed(
        self, target_state: str, *, expected_id: str, detail: str
    ) -> dict[str, Any]:
        """Explicitly resolve a verified v1 failure into a v2 terminal state."""
        if target_state not in {"failed-restored", "superseded-verified"}:
            raise RecoveryError(f"invalid legacy resolution: {target_state}")
        with self.locked():
            document = self.read()
            if (
                not document
                or document.get("id") != expected_id
                or document.get("schemaVersion") != 1
                or document.get("state") != "failed"
            ):
                raise RecoveryError("there is no legacy failed transaction to resolve")
            now = utc_now()
            document["schemaVersion"] = SCHEMA_VERSION
            document["state"] = target_state
            document["updatedAt"] = now
            document["history"].append(
                {
                    "state": target_state,
                    "at": now,
                    "detail": detail[:500],
                    "legacySchemaVersion": 1,
                }
            )
            document["recovery"] = {
                "action": "none",
                "automatic": False,
                "resolution": target_state,
            }
            _atomic_json(self.path, document)
            self._archive_terminal(document)
            return document

    def resolve_legacy_active(
        self, target_state: str, *, expected_id: str, detail: str
    ) -> dict[str, Any]:
        """Resolve a reviewed, non-terminal v1 interruption after verification."""
        if target_state not in {"failed-restored", "superseded-verified"}:
            raise RecoveryError(f"invalid legacy resolution: {target_state}")
        with self.locked():
            document = self.read()
            if (
                not document
                or document.get("id") != expected_id
                or document.get("schemaVersion") != 1
                or document.get("state") in {"failed", "completed"}
            ):
                raise RecoveryError("there is no legacy active transaction to resolve")
            now = utc_now()
            legacy_state = document["state"]
            document["schemaVersion"] = SCHEMA_VERSION
            document["state"] = target_state
            document["updatedAt"] = now
            document["history"].append(
                {
                    "state": target_state,
                    "at": now,
                    "detail": detail[:500],
                    "legacySchemaVersion": 1,
                    "legacyState": legacy_state,
                }
            )
            document["recovery"] = {
                "action": "none",
                "automatic": False,
                "resolution": target_state,
            }
            _atomic_json(self.path, document)
            self._archive_terminal(document)
            return document

    def reconciliation_plan(self) -> dict[str, Any]:
        document = self.read()
        if not document:
            return {
                "required": False,
                "classification": "no-transaction",
                "automaticEligible": False,
                "transaction": None,
                "actions": [],
            }
        if is_terminal(document):
            return {
                "required": False,
                "classification": "verified-terminal",
                "automaticEligible": False,
                "transaction": document,
                "actions": [],
            }
        if document["schemaVersion"] == 1:
            classification = (
                "legacy-failed-review-required"
                if document["state"] == "failed"
                else "legacy-active-review-required"
            )
            return {
                "required": True,
                "classification": classification,
                "automaticEligible": False,
                "transaction": document,
                "actions": [
                    "inspect the current runtime without mutating it",
                    "preserve any healthy current runtime until it is verified",
                    "use explicit reconciliation to classify and resolve the legacy record",
                ],
            }
        automatic = bool(
            document["state"] == "recovery_required"
            and recovery_original_is_safe(document.get("original"))
        )
        initiator = (
            self.initiator_status(document)
            if document["state"] != "recovery_required"
            else None
        )
        actions = []
        if initiator is not None:
            actions.append(
                "run explicit reconciliation to fence the proven-dead initiator"
                if initiator["fenceEligible"]
                else "wait for the active initiator or inspect it before recovery"
            )
        actions.extend(
            [
                "stop any release-candidate runtime",
                "restore the recorded production profile through the hardened runtime wrapper",
                "verify loopback health and configured runtime identity",
                "mark the transaction completed only after restoration succeeds",
            ]
        )
        return {
            "required": True,
            "classification": (
                "recovery-required"
                if document["state"] == "recovery_required"
                else (
                    "orphaned-active-transaction-review-required"
                    if initiator and initiator["fenceEligible"]
                    else "active-transaction-review-required"
                )
            ),
            "automaticEligible": automatic,
            "initiator": initiator,
            "transaction": document,
            "actions": actions,
        }
