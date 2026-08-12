"""Immutable Catalog deployment identity and typed lifecycle actions.

Catalog dictionaries stop at this boundary.  A caller may freeze one strictly
validated Catalog model into an artifact/runtime-tuning identity, but mutation
actions are available only after an explicit admission decision.  This is not
the complete effective runtime identity: image, Compose, network, ports, and
host policy remain independently verified configuration materials.  Actions
describe intent; they never carry shell commands or executable text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .catalog import CatalogDocument, CatalogError, load_catalog, model_by_id
from .materials import canonical_sha256


SCHEMA_VERSION = 1
ROLLOUT_QUALIFICATION_POLICY_ID = (
    "local-inference-stack/transaction-bound-full-qualification-v1"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")


class DeploymentSpecError(ValueError):
    """A Catalog record cannot become a safe deployment identity."""


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise DeploymentSpecError(f"invalid deployment {field}")
    return value


def _revision(value: Any, field: str) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise DeploymentSpecError(f"invalid deployment {field}")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DeploymentSpecError(f"invalid deployment {field}")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DeploymentSpecError(f"invalid deployment {field}")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    role: str
    required: bool
    filename: str
    bytes: int
    sha256: str
    url: str

    @classmethod
    def from_catalog(cls, value: Any) -> ArtifactSpec:
        if not isinstance(value, dict):
            raise DeploymentSpecError("deployment artifact must be an object")
        filename = value.get("filename")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".gguf")
        ):
            raise DeploymentSpecError("invalid deployment artifact filename")
        role = value.get("role")
        if not isinstance(role, str) or not role:
            raise DeploymentSpecError("invalid deployment artifact role")
        required = value.get("required")
        if not isinstance(required, bool):
            raise DeploymentSpecError("invalid deployment artifact requirement")
        sha256 = value.get("sha256")
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise DeploymentSpecError("invalid deployment artifact SHA256")
        url = value.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise DeploymentSpecError("invalid deployment artifact source")
        return cls(
            role=role,
            required=required,
            filename=filename,
            bytes=_positive_integer(value.get("bytes"), "artifact bytes"),
            sha256=sha256,
            url=url,
        )

    def document(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "required": self.required,
            "filename": self.filename,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "url": self.url,
        }

    @classmethod
    def from_document(cls, value: Any) -> ArtifactSpec:
        if not isinstance(value, dict) or set(value) != {
            "role",
            "required",
            "filename",
            "bytes",
            "sha256",
            "url",
        }:
            raise DeploymentSpecError("deployment artifact has an invalid shape")
        return cls.from_catalog(value)


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    context_tokens: int
    recommended_input_tokens: int
    max_output_tokens: int
    cache_ram_mib: int
    cache_type_k: str
    cache_type_v: str
    batch_size: int
    ubatch_size: int

    @classmethod
    def from_catalog(cls, value: Any) -> RuntimeSpec:
        if not isinstance(value, dict):
            raise DeploymentSpecError("deployment runtime must be an object")
        cache_type_k = value.get("cacheTypeK")
        cache_type_v = value.get("cacheTypeV")
        if cache_type_k != "q8_0" or cache_type_v != "q8_0":
            raise DeploymentSpecError("deployment runtime requires q8_0 KV cache")
        spec = cls(
            context_tokens=_positive_integer(
                value.get("contextTokens"), "context tokens"
            ),
            recommended_input_tokens=_positive_integer(
                value.get("recommendedInputTokens"), "recommended input tokens"
            ),
            max_output_tokens=_positive_integer(
                value.get("maxOutputTokens"), "maximum output tokens"
            ),
            cache_ram_mib=_positive_integer(
                value.get("cacheRamMiB"), "cache RAM"
            ),
            cache_type_k=cache_type_k,
            cache_type_v=cache_type_v,
            batch_size=_positive_integer(value.get("batchSize"), "batch size"),
            ubatch_size=_positive_integer(value.get("ubatchSize"), "ubatch size"),
        )
        if (
            spec.recommended_input_tokens + spec.max_output_tokens
            >= spec.context_tokens
        ):
            raise DeploymentSpecError("deployment token budget has no safety margin")
        return spec

    def document(self) -> dict[str, Any]:
        return {
            "contextTokens": self.context_tokens,
            "recommendedInputTokens": self.recommended_input_tokens,
            "maxOutputTokens": self.max_output_tokens,
            "cacheRamMiB": self.cache_ram_mib,
            "cacheTypeK": self.cache_type_k,
            "cacheTypeV": self.cache_type_v,
            "batchSize": self.batch_size,
            "ubatchSize": self.ubatch_size,
        }

    @classmethod
    def from_document(cls, value: Any) -> RuntimeSpec:
        if not isinstance(value, dict) or set(value) != {
            "contextTokens",
            "recommendedInputTokens",
            "maxOutputTokens",
            "cacheRamMiB",
            "cacheTypeK",
            "cacheTypeV",
            "batchSize",
            "ubatchSize",
        }:
            raise DeploymentSpecError("deployment runtime has an invalid shape")
        return cls.from_catalog(value)


@dataclass(frozen=True, slots=True)
class CatalogDeploymentSpec:
    reviewed_model_sha256: str
    catalog_id: str
    served_model_id: str
    model_directory: str
    quantization: str
    model_revision: str
    artifact_revision: str
    runtime: RuntimeSpec
    artifacts: tuple[ArtifactSpec, ...]

    @classmethod
    def from_catalog_model(cls, model: Any) -> CatalogDeploymentSpec:
        if not isinstance(model, dict):
            raise DeploymentSpecError("deployment model must be an object")
        directory = _identifier(model.get("modelDirectory"), "model directory")
        if Path(directory).name != directory:
            raise DeploymentSpecError("deployment model directory must be one component")
        artifacts_value = model.get("artifacts")
        if not isinstance(artifacts_value, list) or not artifacts_value:
            raise DeploymentSpecError("deployment requires at least one artifact")
        artifacts = tuple(ArtifactSpec.from_catalog(item) for item in artifacts_value)
        primary = [item for item in artifacts if item.role == "model" and item.required]
        if len(primary) != 1:
            raise DeploymentSpecError(
                "deployment requires exactly one required primary model artifact"
            )
        return cls(
            reviewed_model_sha256=canonical_sha256(model),
            catalog_id=_identifier(model.get("id"), "Catalog ID"),
            served_model_id=_identifier(
                model.get("servedModelId"), "served model ID"
            ),
            model_directory=directory,
            quantization=_identifier(model.get("quantization"), "quantization"),
            model_revision=_revision(model.get("modelRevision"), "model revision"),
            artifact_revision=_revision(
                model.get("artifactRevision"), "artifact revision"
            ),
            runtime=RuntimeSpec.from_catalog(model.get("runtime")),
            artifacts=artifacts,
        )

    @classmethod
    def from_document(cls, value: Any) -> CatalogDeploymentSpec:
        """Parse a persisted immutable spec without trusting its digest."""

        if not isinstance(value, dict) or set(value) != {
            "schemaVersion",
            "reviewedModelSha256",
            "catalogId",
            "servedModelId",
            "modelDirectory",
            "quantization",
            "modelRevision",
            "artifactRevision",
            "runtime",
            "artifacts",
        }:
            raise DeploymentSpecError("persisted deployment spec has an invalid shape")
        if value.get("schemaVersion") != SCHEMA_VERSION:
            raise DeploymentSpecError("persisted deployment spec has an invalid schema")
        artifacts_value = value.get("artifacts")
        if not isinstance(artifacts_value, list) or not artifacts_value:
            raise DeploymentSpecError("deployment requires at least one artifact")
        artifacts = tuple(ArtifactSpec.from_document(item) for item in artifacts_value)
        primary = [item for item in artifacts if item.role == "model" and item.required]
        if len(primary) != 1:
            raise DeploymentSpecError(
                "deployment requires exactly one required primary model artifact"
            )
        directory = _identifier(value.get("modelDirectory"), "model directory")
        if Path(directory).name != directory:
            raise DeploymentSpecError("deployment model directory must be one component")
        return cls(
            reviewed_model_sha256=_sha256(
                value.get("reviewedModelSha256"), "reviewed model SHA256"
            ),
            catalog_id=_identifier(value.get("catalogId"), "Catalog ID"),
            served_model_id=_identifier(
                value.get("servedModelId"), "served model ID"
            ),
            model_directory=directory,
            quantization=_identifier(value.get("quantization"), "quantization"),
            model_revision=_revision(value.get("modelRevision"), "model revision"),
            artifact_revision=_revision(
                value.get("artifactRevision"), "artifact revision"
            ),
            runtime=RuntimeSpec.from_document(value.get("runtime")),
            artifacts=artifacts,
        )

    def document(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "reviewedModelSha256": self.reviewed_model_sha256,
            "catalogId": self.catalog_id,
            "servedModelId": self.served_model_id,
            "modelDirectory": self.model_directory,
            "quantization": self.quantization,
            "modelRevision": self.model_revision,
            "artifactRevision": self.artifact_revision,
            "runtime": self.runtime.document(),
            "artifacts": [item.document() for item in self.artifacts],
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.document())

    def approval_document(self) -> dict[str, Any]:
        """Return the Catalog identity persisted by a deploy transaction."""

        return {
            "schemaVersion": 1,
            "approvedCatalogSpecSha256": self.sha256,
            "catalogSpec": self.document(),
        }


def parse_approved_deployment(value: Any) -> CatalogDeploymentSpec:
    """Validate a persisted approval record and return its immutable spec."""

    if not isinstance(value, dict) or set(value) != {
        "schemaVersion",
        "approvedCatalogSpecSha256",
        "catalogSpec",
    }:
        raise DeploymentSpecError("approved Catalog spec has an invalid shape")
    spec = CatalogDeploymentSpec.from_document(value.get("catalogSpec"))
    if (
        value.get("schemaVersion") != 1
        or not isinstance(value.get("approvedCatalogSpecSha256"), str)
        or _SHA256.fullmatch(value["approvedCatalogSpecSha256"]) is None
        or value["approvedCatalogSpecSha256"] != spec.sha256
    ):
        raise DeploymentSpecError("approved Catalog spec digest does not match its spec")
    return spec


def bind_approved_catalog_spec(
    catalog: CatalogDocument,
    catalog_id: str,
    catalog_spec_sha256: str,
    *,
    artifact_sha256: str | None = None,
) -> tuple[CatalogDeploymentSpec, ArtifactSpec | None]:
    """Bind one already validated Catalog snapshot to an approved identity."""

    if not isinstance(catalog_spec_sha256, str) or _SHA256.fullmatch(
        catalog_spec_sha256
    ) is None:
        raise DeploymentSpecError("approved Catalog spec SHA256 is invalid")
    try:
        model = model_by_id(catalog, catalog_id)
    except CatalogError as error:
        raise DeploymentSpecError(f"cannot bind approved Catalog spec: {error}") from error
    spec = CatalogDeploymentSpec.from_catalog_model(model)
    if spec.sha256 != catalog_spec_sha256:
        raise DeploymentSpecError(
            "current Catalog deployment does not match the approved Catalog spec SHA256"
        )
    if artifact_sha256 is None:
        return spec, None
    if not isinstance(artifact_sha256, str) or _SHA256.fullmatch(artifact_sha256) is None:
        raise DeploymentSpecError("approved artifact SHA256 is invalid")
    matches = [
        artifact
        for artifact in spec.artifacts
        if artifact.required and artifact.sha256 == artifact_sha256
    ]
    if len(matches) != 1:
        raise DeploymentSpecError(
            "approved artifact SHA256 does not identify exactly one required artifact"
        )
    return spec, matches[0]


def load_approved_catalog_spec(
    catalog_path: Path,
    catalog_id: str,
    catalog_spec_sha256: str,
    *,
    artifact_sha256: str | None = None,
) -> tuple[CatalogDeploymentSpec, ArtifactSpec | None]:
    """Strictly reload a Catalog and bind one action to its approved snapshot."""

    try:
        catalog = load_catalog(catalog_path)
    except CatalogError as error:
        raise DeploymentSpecError(f"cannot load approved Catalog spec: {error}") from error
    return bind_approved_catalog_spec(
        catalog,
        catalog_id,
        catalog_spec_sha256,
        artifact_sha256=artifact_sha256,
    )


class ActionKind(str, Enum):
    FETCH_ARTIFACT = "fetch-artifact"
    ACTIVATE_SPEC = "activate-spec"
    START_RUNTIME = "start-runtime"
    QUICK_SMOKE = "quick-smoke"


class RolloutActionKind(str, Enum):
    """Typed maintenance-window actions with no executable payload."""

    SOURCE_QUICK = "source-quick"
    FETCH_TARGET_ARTIFACT = "fetch-target-artifact"
    STOP_SOURCE = "stop-source"
    ACTIVATE_TARGET = "activate-target"
    START_TARGET = "start-target"
    TARGET_QUICK = "target-quick"
    TARGET_FULL = "target-full"
    PUBLISH_ROLLBACK = "publish-rollback"
    CLEAR_ROLLBACK = "clear-rollback"


@dataclass(frozen=True, slots=True)
class RolloutAction:
    ordinal: int
    kind: RolloutActionKind
    subject: str
    catalog_id: str
    artifact_sha256: str | None = None

    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "ordinal": self.ordinal,
            "kind": self.kind.value,
            "subject": self.subject,
            "catalogId": self.catalog_id,
        }
        if self.artifact_sha256 is not None:
            document["artifactSha256"] = self.artifact_sha256
        return document


@dataclass(frozen=True, slots=True)
class RolloutQualification:
    """Exact reusable-evidence requirement carried only by rollout plan v2."""

    performance_policy_sha256: str
    modelport_source_identity_sha256: str
    qualification_input_sha256: str
    policy_id: str = ROLLOUT_QUALIFICATION_POLICY_ID
    mode: str = "full"
    profile: str = "latency"
    record_evidence: bool = True
    run_schema_version: int = 2
    evidence_schema_version: int = 5

    def __post_init__(self) -> None:
        _sha256(self.performance_policy_sha256, "performance policy SHA256")
        _sha256(
            self.modelport_source_identity_sha256,
            "ModelPort source identity SHA256",
        )
        _sha256(self.qualification_input_sha256, "qualification input SHA256")
        if (
            self.policy_id != ROLLOUT_QUALIFICATION_POLICY_ID
            or self.mode != "full"
            or self.profile != "latency"
            or self.record_evidence is not True
            or self.run_schema_version != 2
            or self.evidence_schema_version != 5
        ):
            raise DeploymentSpecError("invalid rollout qualification contract")

    def document(self) -> dict[str, Any]:
        return {
            "policyId": self.policy_id,
            "mode": self.mode,
            "profile": self.profile,
            "recordEvidence": self.record_evidence,
            "runSchemaVersion": self.run_schema_version,
            "evidenceSchemaVersion": self.evidence_schema_version,
            "performancePolicySha256": self.performance_policy_sha256,
            "modelPortSourceIdentitySha256": (
                self.modelport_source_identity_sha256
            ),
            "qualificationInputSha256": self.qualification_input_sha256,
        }

    @classmethod
    def from_document(cls, value: Any) -> RolloutQualification:
        if not isinstance(value, dict) or set(value) != {
            "policyId",
            "mode",
            "profile",
            "recordEvidence",
            "runSchemaVersion",
            "evidenceSchemaVersion",
            "performancePolicySha256",
            "modelPortSourceIdentitySha256",
            "qualificationInputSha256",
        }:
            raise DeploymentSpecError("invalid rollout qualification shape")
        qualification = cls(
            performance_policy_sha256=value.get("performancePolicySha256"),
            modelport_source_identity_sha256=value.get(
                "modelPortSourceIdentitySha256"
            ),
            qualification_input_sha256=value.get("qualificationInputSha256"),
            policy_id=value.get("policyId"),
            mode=value.get("mode"),
            profile=value.get("profile"),
            record_evidence=value.get("recordEvidence"),
            run_schema_version=value.get("runSchemaVersion"),
            evidence_schema_version=value.get("evidenceSchemaVersion"),
        )
        if qualification.document() != value:
            raise DeploymentSpecError("invalid rollout qualification contract")
        return qualification


@dataclass(frozen=True, slots=True)
class RolloutPlan:
    """One exact ordered upgrade or rollback intent.

    The plan binds an immutable rollback object and the Catalog identity that
    must be running when the transaction completes.  It deliberately carries
    no command, path, URL, image pull, or arbitrary environment value.
    """

    operation: str
    rollback_spec_sha256: str
    source_catalog_spec_sha256: str
    target_catalog_spec_sha256: str
    actions: tuple[RolloutAction, ...]
    required_acceptance_tier: str = "quick"
    requires_approval: bool = True
    schema_version: int = 1
    qualification: RolloutQualification | None = None

    def document(self) -> dict[str, Any]:
        document = {
            "schemaVersion": self.schema_version,
            "operation": self.operation,
            "rollbackSpecSha256": self.rollback_spec_sha256,
            "sourceCatalogSpecSha256": self.source_catalog_spec_sha256,
            "targetCatalogSpecSha256": self.target_catalog_spec_sha256,
            "requiredAcceptanceTier": self.required_acceptance_tier,
            "requiresApproval": self.requires_approval,
            "actions": [action.document() for action in self.actions],
        }
        if self.qualification is not None:
            document["qualification"] = self.qualification.document()
        return document

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.document())


def build_upgrade_rollout_plan(
    source: CatalogDeploymentSpec,
    target: CatalogDeploymentSpec,
    rollback_spec_sha256: str,
    *,
    admission_granted: bool,
    required_acceptance_tier: str = "quick",
    performance_policy_sha256: str | None = None,
    modelport_source_identity_sha256: str | None = None,
    qualification_input_sha256: str | None = None,
) -> RolloutPlan:
    """Build an admitted single-runtime upgrade sequence.

    Quick keeps the original v1 wire document.  Full qualification is a v2
    plan because it adds both an exact evidence contract and a distinct typed
    action; a stronger label can therefore never be attached to v1 actions.
    """

    if not admission_granted:
        raise DeploymentSpecError("upgrade actions require explicit admission")
    _sha256(rollback_spec_sha256, "rollback spec SHA256")
    if source.catalog_id == target.catalog_id or source.sha256 == target.sha256:
        raise DeploymentSpecError("upgrade source and target must be distinct")
    if required_acceptance_tier not in {"quick", "full"}:
        raise DeploymentSpecError("upgrade acceptance tier must be quick or full")
    qualification: RolloutQualification | None = None
    if required_acceptance_tier == "full":
        if (
            performance_policy_sha256 is None
            or modelport_source_identity_sha256 is None
            or qualification_input_sha256 is None
        ):
            raise DeploymentSpecError(
                "full upgrade requires performance policy and ModelPort source identities"
            )
        qualification = RolloutQualification(
            performance_policy_sha256=performance_policy_sha256,
            modelport_source_identity_sha256=modelport_source_identity_sha256,
            qualification_input_sha256=qualification_input_sha256,
        )
    elif (
        performance_policy_sha256 is not None
        or modelport_source_identity_sha256 is not None
        or qualification_input_sha256 is not None
    ):
        raise DeploymentSpecError(
            "quick upgrade cannot carry full qualification identities"
        )
    actions: list[RolloutAction] = [
        RolloutAction(0, RolloutActionKind.SOURCE_QUICK, "source", source.catalog_id)
    ]
    for artifact in target.artifacts:
        if artifact.required:
            actions.append(
                RolloutAction(
                    len(actions),
                    RolloutActionKind.FETCH_TARGET_ARTIFACT,
                    "target",
                    target.catalog_id,
                    artifact_sha256=artifact.sha256,
                )
            )
    actions.extend(
        (
            RolloutAction(
                len(actions), RolloutActionKind.STOP_SOURCE, "source", source.catalog_id
            ),
            RolloutAction(
                len(actions) + 1,
                RolloutActionKind.ACTIVATE_TARGET,
                "target",
                target.catalog_id,
            ),
            RolloutAction(
                len(actions) + 2,
                RolloutActionKind.START_TARGET,
                "target",
                target.catalog_id,
            ),
            RolloutAction(
                len(actions) + 3,
                RolloutActionKind.TARGET_QUICK,
                "target",
                target.catalog_id,
            ),
        )
    )
    if required_acceptance_tier == "full":
        actions.append(
            RolloutAction(
                len(actions),
                RolloutActionKind.TARGET_FULL,
                "target",
                target.catalog_id,
            )
        )
    actions.append(
        RolloutAction(
            len(actions),
            RolloutActionKind.PUBLISH_ROLLBACK,
            "source",
            source.catalog_id,
        )
    )
    return RolloutPlan(
        "upgrade",
        rollback_spec_sha256,
        source.sha256,
        target.sha256,
        tuple(actions),
        required_acceptance_tier=required_acceptance_tier,
        schema_version=2 if required_acceptance_tier == "full" else 1,
        qualification=qualification,
    )


def build_rollback_rollout_plan(
    current: CatalogDeploymentSpec,
    anchor: CatalogDeploymentSpec,
    rollback_spec_sha256: str,
    *,
    admission_granted: bool,
) -> RolloutPlan:
    """Build the v1 one-shot rollback sequence."""

    if not admission_granted:
        raise DeploymentSpecError("rollback actions require explicit admission")
    _sha256(rollback_spec_sha256, "rollback spec SHA256")
    if current.catalog_id == anchor.catalog_id or current.sha256 == anchor.sha256:
        raise DeploymentSpecError("rollback source and anchor must be distinct")
    actions = (
        RolloutAction(0, RolloutActionKind.STOP_SOURCE, "source", current.catalog_id),
        RolloutAction(1, RolloutActionKind.ACTIVATE_TARGET, "target", anchor.catalog_id),
        RolloutAction(2, RolloutActionKind.START_TARGET, "target", anchor.catalog_id),
        RolloutAction(3, RolloutActionKind.TARGET_QUICK, "target", anchor.catalog_id),
        RolloutAction(4, RolloutActionKind.CLEAR_ROLLBACK, "target", anchor.catalog_id),
    )
    return RolloutPlan(
        "rollback",
        rollback_spec_sha256,
        current.sha256,
        anchor.sha256,
        actions,
    )


def parse_rollout_plan(
    value: Any,
    *,
    rollback_spec_sha256: str,
    target: CatalogDeploymentSpec,
    source: CatalogDeploymentSpec | None = None,
) -> RolloutPlan:
    """Validate an untrusted rollout plan by exact reconstruction."""

    if not isinstance(value, dict):
        raise DeploymentSpecError("rollout plan has an invalid shape")
    schema_version = value.get("schemaVersion")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in {1, 2}
    ):
        raise DeploymentSpecError("rollout plan has an invalid schema")
    expected_keys = {
        "schemaVersion",
        "operation",
        "rollbackSpecSha256",
        "sourceCatalogSpecSha256",
        "targetCatalogSpecSha256",
        "requiredAcceptanceTier",
        "requiresApproval",
        "actions",
    }
    if schema_version == 2:
        expected_keys.add("qualification")
    if set(value) != expected_keys:
        raise DeploymentSpecError("rollout plan has an invalid shape")
    operation = value.get("operation")
    qualification = (
        RolloutQualification.from_document(value.get("qualification"))
        if schema_version == 2
        else None
    )
    if operation == "upgrade":
        if source is None:
            raise DeploymentSpecError("upgrade plan requires its source spec")
        expected = build_upgrade_rollout_plan(
            source,
            target,
            rollback_spec_sha256,
            admission_granted=True,
            required_acceptance_tier=(
                "full"
                if schema_version == 2
                else "quick"
            ),
            performance_policy_sha256=(
                qualification.performance_policy_sha256
                if qualification is not None
                else None
            ),
            modelport_source_identity_sha256=(
                qualification.modelport_source_identity_sha256
                if qualification is not None
                else None
            ),
            qualification_input_sha256=(
                qualification.qualification_input_sha256
                if qualification is not None
                else None
            ),
        )
    elif operation == "rollback":
        if source is None:
            raise DeploymentSpecError("rollback plan requires its current source spec")
        expected = build_rollback_rollout_plan(
            source,
            target,
            rollback_spec_sha256,
            admission_granted=True,
        )
    else:
        raise DeploymentSpecError("rollout operation is not supported")
    if value != expected.document():
        raise DeploymentSpecError("rollout plan does not match its immutable subjects")
    return expected


@dataclass(frozen=True, slots=True)
class DeploymentAction:
    kind: ActionKind
    catalog_id: str
    artifact_sha256: str | None = None

    def document(self) -> dict[str, str]:
        document = {"kind": self.kind.value, "catalogId": self.catalog_id}
        if self.artifact_sha256 is not None:
            document["artifactSha256"] = self.artifact_sha256
        return document


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    catalog_spec_sha256: str
    actions: tuple[DeploymentAction, ...]
    requires_approval: bool = True

    def document(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "catalogSpecSha256": self.catalog_spec_sha256,
            "requiresApproval": self.requires_approval,
            "actions": [action.document() for action in self.actions],
        }


def parse_deployment_plan(
    spec: CatalogDeploymentSpec,
    value: Any,
    *,
    require_full_lifecycle: bool = False,
) -> DeploymentPlan:
    """Validate an untrusted serialized action plan against one frozen spec."""

    if not isinstance(value, dict) or set(value) != {
        "schemaVersion",
        "catalogSpecSha256",
        "requiresApproval",
        "actions",
    }:
        raise DeploymentSpecError("deployment action plan has an invalid shape")
    if (
        value.get("schemaVersion") != 1
        or value.get("catalogSpecSha256") != spec.sha256
        or value.get("requiresApproval") is not True
        or not isinstance(value.get("actions"), list)
    ):
        raise DeploymentSpecError("deployment action plan does not match its spec")
    known_required = {
        artifact.sha256 for artifact in spec.artifacts if artifact.required
    }
    actions: list[DeploymentAction] = []
    seen_singletons: set[ActionKind] = set()
    fetched: set[str] = set()
    ranks = {
        ActionKind.FETCH_ARTIFACT: 0,
        ActionKind.ACTIVATE_SPEC: 1,
        ActionKind.START_RUNTIME: 2,
        ActionKind.QUICK_SMOKE: 3,
    }
    previous_rank = -1
    for item in value["actions"]:
        if not isinstance(item, dict):
            raise DeploymentSpecError("deployment action must be an object")
        try:
            kind = ActionKind(item.get("kind"))
        except (TypeError, ValueError) as error:
            raise DeploymentSpecError("deployment action kind is not supported") from error
        expected_keys = {"kind", "catalogId"}
        if kind is ActionKind.FETCH_ARTIFACT:
            expected_keys.add("artifactSha256")
        if set(item) != expected_keys or item.get("catalogId") != spec.catalog_id:
            raise DeploymentSpecError("deployment action does not match its Catalog ID")
        rank = ranks[kind]
        if rank < previous_rank:
            raise DeploymentSpecError("deployment actions are out of order")
        previous_rank = rank
        artifact_sha256 = item.get("artifactSha256")
        if kind is ActionKind.FETCH_ARTIFACT:
            if artifact_sha256 not in known_required or artifact_sha256 in fetched:
                raise DeploymentSpecError(
                    "deployment fetch action has an unknown or duplicate artifact"
                )
            fetched.add(artifact_sha256)
        else:
            artifact_sha256 = None
            if kind in seen_singletons:
                raise DeploymentSpecError("deployment lifecycle action is duplicated")
            seen_singletons.add(kind)
        actions.append(
            DeploymentAction(kind, spec.catalog_id, artifact_sha256=artifact_sha256)
        )
    if require_full_lifecycle and (
        fetched != known_required
        or [action.kind for action in actions if action.kind is not ActionKind.FETCH_ARTIFACT]
        != [
            ActionKind.ACTIVATE_SPEC,
            ActionKind.START_RUNTIME,
            ActionKind.QUICK_SMOKE,
        ]
    ):
        raise DeploymentSpecError(
            "deployment plan does not contain the complete approved lifecycle"
        )
    return DeploymentPlan(spec.sha256, tuple(actions))


def build_deployment_plan(
    spec: CatalogDeploymentSpec,
    *,
    admission_granted: bool,
) -> DeploymentPlan:
    """Describe a complete new deployment without constructing commands."""

    if not admission_granted:
        raise DeploymentSpecError("deployment actions require explicit admission")
    actions = [
        DeploymentAction(
            ActionKind.FETCH_ARTIFACT,
            spec.catalog_id,
            artifact_sha256=artifact.sha256,
        )
        for artifact in spec.artifacts
        if artifact.required
    ]
    actions.extend(
        (
            DeploymentAction(ActionKind.ACTIVATE_SPEC, spec.catalog_id),
            DeploymentAction(ActionKind.START_RUNTIME, spec.catalog_id),
            DeploymentAction(ActionKind.QUICK_SMOKE, spec.catalog_id),
        )
    )
    return DeploymentPlan(spec.sha256, tuple(actions))
