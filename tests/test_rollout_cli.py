"""Pure orchestration tests for the typed upgrade and rollback CLI.

Docker, GPU discovery, Git, subprocesses, and the real rollback store stay
behind mocks.  The tests exercise only command parsing and control-plane
orchestration; temporary project roots are used for every case.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_inference_stack import cli  # noqa: E402
from local_inference_stack.acceptance import (  # noqa: E402
    MODELPORT_SOURCE_IDENTITY_POLICY,
    MODELPORT_SOURCE_MATERIAL_PATHS,
    QUALIFICATION_INPUT_POLICY,
    REVIEWED_LOGICAL_MODELS,
)
from local_inference_stack.catalog import load_catalog  # noqa: E402
from local_inference_stack.deployment import (  # noqa: E402
    CatalogDeploymentSpec,
    RolloutActionKind,
    build_rollback_rollout_plan,
    build_upgrade_rollout_plan,
)
from local_inference_stack.paths import ProjectPaths  # noqa: E402
from local_inference_stack.result import (  # noqa: E402
    AdmissionError,
    ExternalError,
    IntegrityError,
    RecoveryError,
    UsageError,
)
from local_inference_stack.rollout import (  # noqa: E402
    ROLLBACK_POINTER_KIND,
    ROLLBACK_POINTER_SCHEMA_VERSION,
    ROLLBACK_SCOPE_POLICY,
    RollbackCASMismatch,
    RollbackPointer,
    RollbackSpecError,
    RollbackStoreError,
)
from local_inference_stack.runner import RunResult  # noqa: E402
from local_inference_stack.transactions import SCHEMA_VERSION  # noqa: E402


TRANSACTION_ID = "0dbdb868-b62f-4471-84b4-0198a0700f09"
CREATED_AT = "2026-08-12T01:02:03Z"
UPDATED_AT = "2026-08-12T02:03:04Z"
ROLLBACK_SHA256 = "a" * 64
OLD_ROLLBACK_SHA256 = "b" * 64
OLDER_ROLLBACK_SHA256 = "c" * 64
FOREIGN_ROLLBACK_SHA256 = "d" * 64


def _specs() -> tuple[CatalogDeploymentSpec, CatalogDeploymentSpec]:
    source_model = load_catalog(ROOT / "catalog" / "models.json")["models"][0]
    target_model = copy.deepcopy(source_model)
    target_model.update(
        {
            "id": "test-rollout-target",
            "displayName": "Test rollout target",
            "servedModelId": "test-rollout-target",
            "modelDirectory": "test-rollout-target",
        }
    )
    target_model["artifacts"][0]["filename"] = "test-rollout-target.gguf"
    return (
        CatalogDeploymentSpec.from_catalog_model(source_model),
        CatalogDeploymentSpec.from_catalog_model(target_model),
    )


def _qualification_input(
    target: CatalogDeploymentSpec,
    *,
    modelport_source_identity_sha256: str,
) -> dict[str, Any]:
    value = {
        "policyId": QUALIFICATION_INPUT_POLICY,
        "targetCatalogSpecSha256": target.sha256,
        "providerContractId": "local-qwen-provider-v1",
        "providerContractSha256": "3" * 64,
        "servedModelId": target.served_model_id,
        "limitsSha256": "4" * 64,
        "acceptanceSha256": "5" * 64,
        "logicalModels": copy.deepcopy(REVIEWED_LOGICAL_MODELS),
        "providerMatrixModel": "qwen3.5-code",
        "toolUseMaxTokens": 2048,
        "directContextTokens": 118000,
        "modelPortContextTokens": 92000,
        "modelPortContextMaxTokens": 32768,
        "decodeTokens": 512,
        "decodeContextTokens": 0,
        "concurrency": 2,
        "concurrencyTokens": 512,
        "modelPortSourceIdentitySha256": modelport_source_identity_sha256,
        "liveModelRegistrySha256": "6" * 64,
        "toolUseLocalProviderReady": True,
    }
    value["sha256"] = cli.sha256_document(value)
    return value


def _pointer(
    generation: int,
    active: str | None,
    previous: str | None,
    *,
    transaction_id: str = TRANSACTION_ID,
) -> RollbackPointer:
    return RollbackPointer.from_document(
        {
            "schemaVersion": ROLLBACK_POINTER_SCHEMA_VERSION,
            "kind": ROLLBACK_POINTER_KIND,
            "scopePolicy": ROLLBACK_SCOPE_POLICY,
            "generation": generation,
            "activeSpecSha256": active,
            "previousSpecSha256": previous,
            "updatedAt": UPDATED_AT,
            "updatedByTransactionId": transaction_id,
        }
    )


class StubRollbackSpec:
    def __init__(
        self,
        catalog_spec: CatalogDeploymentSpec,
        *,
        sha256: str = ROLLBACK_SHA256,
    ) -> None:
        self.sha256 = sha256
        self._catalog_spec = catalog_spec

    def document(self) -> dict[str, Any]:
        return {
            "catalogSpec": self._catalog_spec.document(),
            "selection": {"sha256": "e" * 64},
        }


class RecordingTransaction:
    """Small transaction double that preserves state between CLI calls."""

    def __init__(self, *, transaction_id: str = TRANSACTION_ID) -> None:
        self.state: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "id": transaction_id,
            "operation": "upgrade",
            "state": "planned",
            "rolloutActionOrdinal": 0,
        }
        self.transitions: list[dict[str, Any]] = []
        self.advances: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.authorizations: list[dict[str, Any]] = []
        self.begin_calls: list[dict[str, Any]] = []
        self.runtime_events: list[str] = []
        self.qualification_bindings: list[dict[str, Any]] = []

    def begin_rollout(
        self,
        operation: str,
        target: str,
        capture: Any,
        *,
        approved_catalog_spec: dict[str, Any],
    ) -> dict[str, Any]:
        original, intent = capture(TRANSACTION_ID, CREATED_AT)
        self.state.update(
            {
                "operation": operation,
                "target": target,
                "state": "planned",
                "original": original,
                "rolloutIntent": intent,
                "rolloutActionOrdinal": 0,
            }
        )
        self.begin_calls.append(
            {
                "operation": operation,
                "target": target,
                "approvedCatalogSpec": approved_catalog_spec,
                "original": original,
                "rolloutIntent": intent,
            }
        )
        return copy.deepcopy(self.state)

    def read(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    def transition(self, target_state: str, **kwargs: Any) -> dict[str, Any]:
        record = {"targetState": target_state, **kwargs}
        self.transitions.append(record)
        self.state["state"] = target_state
        return copy.deepcopy(self.state)

    def advance_rollout_action(self, **kwargs: Any) -> dict[str, Any]:
        self.advances.append(dict(kwargs))
        self.state["rolloutActionOrdinal"] = kwargs["expected_action_ordinal"] + 1
        return copy.deepcopy(self.state)

    @contextmanager
    def runtime_boundary(self) -> Any:
        self.runtime_events.append("enter")
        try:
            yield
        finally:
            self.runtime_events.append("exit")

    def pending_rollout_qualification_binding(self, **kwargs: Any) -> dict[str, Any]:
        self.qualification_bindings.append(dict(kwargs))
        return {
            "policyId": "local-inference-stack/rollout-qualification-binding-v1",
            "transactionId": kwargs["transaction_id"],
            "operation": "upgrade",
            "rolloutPlanSha256": "1" * 64,
            "actionOrdinal": kwargs["action_ordinal"],
            "actionKind": "target-full",
            "rollbackSpecSha256": ROLLBACK_SHA256,
            "sourceCatalogSpecSha256": self.state.get(
                "sourceCatalogSpecSha256", "2" * 64
            ),
            "targetCatalogSpecSha256": kwargs["catalog_spec_sha256"],
            "performancePolicySha256": "3" * 64,
            "modelPortSourceIdentitySha256": "4" * 64,
            "qualificationInputSha256": "5" * 64,
        }

    def assert_approved_deployment(self, **kwargs: Any) -> CatalogDeploymentSpec:
        self.approvals.append(dict(kwargs))
        return MagicMock(spec=CatalogDeploymentSpec)

    @contextmanager
    def authorized_runtime_mutation(
        self, transaction_id: str, **kwargs: Any
    ) -> Any:
        self.authorizations.append(
            {"transactionId": transaction_id, **kwargs}
        )
        yield


def _completed(argv: list[str], output: str | None = None) -> RunResult:
    return RunResult(tuple(argv), 0, output or f"ok:{argv[0]}", "")


class RolloutCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.source, self.target = _specs()
        self.rollback_spec = StubRollbackSpec(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _admission(
        source: CatalogDeploymentSpec,
        target: CatalogDeploymentSpec,
    ) -> Any:
        def admit(
            _paths: ProjectPaths, model_id: str, *, mode: str
        ) -> tuple[dict[str, Any], CatalogDeploymentSpec]:
            if mode == "existing-selection" and model_id == source.catalog_id:
                return {
                    "mode": "read-only-existing-selection-admission",
                    "catalogRecoveryEligible": True,
                }, source
            if mode == "replacement" and model_id == target.catalog_id:
                return {"mode": "read-only-replacement-admission"}, target
            raise AssertionError((model_id, mode))

        return admit


class RolloutParserAndDryRunTests(RolloutCliTestCase):
    def test_qualification_reader_rejects_ancestor_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory)
            (outside / "acceptance").mkdir(mode=0o700)
            evidence = outside / "acceptance" / "qualification-test.json"
            evidence.write_text('{"outside":true}\n', encoding="utf-8")
            evidence.chmod(0o600)
            (self.paths.root / "logs").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaisesRegex(IntegrityError, "unsafe"):
                cli._strict_private_qualification_json(
                    self.paths,
                    Path("logs/acceptance/qualification-test.json"),
                    label="qualification evidence",
                )

    def test_modelport_preflight_delegates_to_the_evidence_contract(self) -> None:
        materials = [
            {"path": path, "sha256": str(index) * 64}
            for index, path in enumerate(MODELPORT_SOURCE_MATERIAL_PATHS, start=1)
        ]
        identity = {
            "policyId": MODELPORT_SOURCE_IDENTITY_POLICY,
            "gitCommit": "a" * 40,
            "gitTree": "b" * 40,
            "sourceState": "clean",
            "materials": materials,
            "materialsSha256": cli.sha256_document(materials),
            "liveServiceIdentity": {
                "endpoint": "http://127.0.0.1:38082/livez",
                "service": "model-port",
                "status": "ok",
                "build": {
                    "revision": "a" * 40,
                    "sourceState": "clean",
                    "version": "1.2.3",
                    "configSha256": materials[0]["sha256"],
                },
            },
        }
        qualification_input = {
            "policyId": QUALIFICATION_INPUT_POLICY,
            "targetCatalogSpecSha256": self.target.sha256,
            "providerContractId": "local-qwen-provider-v1",
            "providerContractSha256": "b" * 64,
            "servedModelId": self.target.served_model_id,
            "limitsSha256": "c" * 64,
            "acceptanceSha256": "d" * 64,
            "logicalModels": REVIEWED_LOGICAL_MODELS,
            "providerMatrixModel": "qwen3.5-code",
            "toolUseMaxTokens": 2048,
            "directContextTokens": 118000,
            "modelPortContextTokens": 92000,
            "modelPortContextMaxTokens": 32768,
            "decodeTokens": 512,
            "decodeContextTokens": 0,
            "concurrency": 2,
            "concurrencyTokens": 512,
            "modelPortSourceIdentitySha256": cli.sha256_document(identity),
            "liveModelRegistrySha256": "e" * 64,
            "toolUseLocalProviderReady": True,
        }
        qualification_input["sha256"] = cli.sha256_document(qualification_input)
        completed = RunResult(
            (
                "scripts/acceptance-evidence.py",
                "qualification-preflight",
                "--modelport-project",
                "/srv/modelport",
            ),
            0,
            json.dumps(
                {
                    "schemaVersion": 2,
                    "status": "ready",
                    "modelPortSourceIdentity": identity,
                    "qualificationInput": qualification_input,
                    "prerequisites": {
                        "nodeMajor": 24,
                        "credentials": "available",
                        "providerCompatibility": "passed",
                        "dashboard": "ok",
                    },
                }
            ),
            "",
        )
        with (
            patch.dict(os.environ, {"MODELPORT_PROJECT_DIR": "/srv/modelport"}),
            patch.object(cli, "run", return_value=completed) as runner,
        ):
            path, observed, qualification = cli._modelport_qualification_preflight(
                self.paths, self.target
            )
        self.assertEqual(path, "/srv/modelport")
        self.assertEqual(observed, identity)
        self.assertEqual(qualification, qualification_input)
        runner.assert_called_once_with(
            [
                "scripts/acceptance-evidence.py",
                "qualification-preflight",
                "--modelport-project",
                "/srv/modelport",
                "--catalog-model-id",
                self.target.catalog_id,
                "--catalog-spec-sha256",
                self.target.sha256,
            ],
            cwd=self.paths.root,
            timeout=60,
        )

    def test_upgrade_parser_defaults_to_read_only_and_rollback_rejects_model(self) -> None:
        parsed = cli.parser().parse_args(["upgrade", "--model", self.target.catalog_id])
        self.assertFalse(parsed.yes)
        self.assertEqual(parsed.qualification, "quick")
        full = cli.parser().parse_args(
            [
                "upgrade",
                "--model",
                self.target.catalog_id,
                "--qualification",
                "full",
            ]
        )
        self.assertEqual(full.qualification, "full")
        with self.assertRaises(UsageError):
            cli.parser().parse_args(["rollback", "--model", self.source.catalog_id])

    def test_upgrade_without_yes_creates_no_transaction_or_rollback_store(self) -> None:
        original = {"healthy": True, "profile": "latency"}
        with (
            patch.object(cli, "_selected_catalog_spec", return_value=(self.source, {})),
            patch.object(
                cli,
                "_rollout_admission",
                side_effect=self._admission(self.source, self.target),
            ),
            patch.object(cli, "_original_runtime", return_value=original),
            patch.object(cli, "TransactionStore") as transaction_type,
            patch.object(cli, "RollbackStore") as rollback_store_type,
            patch.object(
                cli, "capture_rollback_spec", return_value=self.rollback_spec
            ) as capture,
        ):
            result = cli._upgrade(
                self.paths,
                Namespace(model=self.target.catalog_id, yes=False),
            )

        self.assertEqual(result.command, "upgrade")
        self.assertTrue(result.facts["dryRun"])
        transaction_type.assert_not_called()
        rollback_store_type.assert_not_called()
        capture.assert_called_once_with(
            self.paths,
            transaction_id=cli.ROLLOUT_PREFLIGHT_TRANSACTION_ID,
            captured_at=cli.ROLLOUT_PREFLIGHT_CAPTURED_AT,
            original=original,
            source_admission={
                "mode": "read-only-existing-selection-admission",
                "catalogRecoveryEligible": True,
            },
        )

    def test_full_upgrade_preflights_modelport_and_performance_before_transaction(
        self,
    ) -> None:
        events: list[str] = []
        performance = SimpleNamespace(
            manifest_relative_path="deployments/reviewed/manifest.json",
            manifest_sha256="b" * 64,
            policy_sha256="c" * 64,
        )

        def runner(argv: list[str], **_kwargs: Any) -> RunResult:
            self.assertEqual(
                argv,
                [
                    "python3",
                    "src/local_inference_stack/performance.py",
                    "--manifest",
                    performance.manifest_relative_path,
                    "--expected-policy-sha256",
                    performance.policy_sha256,
                    "--catalog-id",
                    self.target.catalog_id,
                ],
            )
            events.append("performance")
            raise ExternalError("performance policy is not ready")

        with (
            patch.object(cli, "_selected_catalog_spec", return_value=(self.source, {})),
            patch.object(
                cli,
                "_rollout_admission",
                side_effect=self._admission(self.source, self.target),
            ),
            patch.object(
                cli,
                "_modelport_qualification_preflight",
                side_effect=lambda _paths, _target: (
                    events.append("modelport")
                    or (
                        "/srv/modelport",
                        {"gitCommit": "a" * 40},
                        {
                            "providerMatrixModel": "qwen3.5-code",
                            "toolUseMaxTokens": 2048,
                        },
                    )
                ),
            ),
            patch.object(
                cli,
                "_target_performance_preflight",
                return_value=performance,
            ),
            patch.object(cli, "run", side_effect=runner),
            patch.object(cli, "TransactionStore") as transaction_type,
            patch.object(cli, "RollbackStore") as rollback_store_type,
            patch.object(cli, "capture_rollback_spec") as capture,
            self.assertRaisesRegex(ExternalError, "performance policy"),
        ):
            cli._upgrade(
                self.paths,
                Namespace(
                    model=self.target.catalog_id,
                    qualification="full",
                    yes=True,
                ),
            )
        self.assertEqual(events, ["modelport", "performance"])
        transaction_type.assert_not_called()
        rollback_store_type.assert_not_called()
        capture.assert_not_called()

    def test_full_static_preflight_failure_is_zero_write_and_zero_runtime(self) -> None:
        with (
            patch.object(cli, "_selected_catalog_spec", return_value=(self.source, {})),
            patch.object(
                cli,
                "_rollout_admission",
                side_effect=self._admission(self.source, self.target),
            ),
            patch.object(
                cli,
                "_modelport_qualification_preflight",
                side_effect=AdmissionError("static qualification prerequisite failed"),
            ),
            patch.object(cli, "run") as runner,
            patch.object(cli, "_original_runtime") as original,
            patch.object(cli, "TransactionStore") as transaction_type,
            patch.object(cli, "RollbackStore") as rollback_store_type,
            patch.object(cli, "capture_rollback_spec") as capture,
            self.assertRaisesRegex(AdmissionError, "static qualification"),
        ):
            cli._upgrade(
                self.paths,
                Namespace(
                    model=self.target.catalog_id,
                    qualification="full",
                    yes=True,
                ),
            )
        runner.assert_not_called()
        original.assert_not_called()
        transaction_type.assert_not_called()
        rollback_store_type.assert_not_called()
        capture.assert_not_called()

    def test_upgrade_dry_run_rejects_source_drift_before_any_control_write(self) -> None:
        original = {"healthy": False, "profile": "latency"}
        with (
            patch.object(cli, "_selected_catalog_spec", return_value=(self.source, {})),
            patch.object(
                cli,
                "_rollout_admission",
                side_effect=self._admission(self.source, self.target),
            ),
            patch.object(cli, "_original_runtime", return_value=original),
            patch.object(
                cli,
                "capture_rollback_spec",
                side_effect=cli.RecoveryError("upgrade source is not healthy"),
            ) as capture,
            patch.object(cli, "TransactionStore") as transaction_type,
            patch.object(cli, "RollbackStore") as rollback_store_type,
            self.assertRaisesRegex(cli.RecoveryError, "not healthy"),
        ):
            cli._upgrade(
                self.paths,
                Namespace(model=self.target.catalog_id, yes=False),
            )
        capture.assert_called_once()
        transaction_type.assert_not_called()
        rollback_store_type.assert_not_called()

    def test_upgrade_dry_run_rejects_a_source_that_is_not_a_recovery_anchor(self) -> None:
        admissions = iter(
            (
                (
                    {
                        "mode": "read-only-existing-selection-admission",
                        "catalogRecoveryEligible": False,
                    },
                    self.source,
                ),
                (
                    {"mode": "read-only-replacement-admission"},
                    self.target,
                ),
            )
        )
        with (
            patch.object(cli, "_selected_catalog_spec", return_value=(self.source, {})),
            patch.object(cli, "_rollout_admission", side_effect=admissions),
            patch.object(cli, "TransactionStore") as transaction_type,
            patch.object(cli, "RollbackStore") as rollback_store_type,
            patch.object(cli, "capture_rollback_spec") as capture,
            self.assertRaisesRegex(
                cli.AdmissionError, "eligible immutable rollback anchor"
            ),
        ):
            cli._upgrade(
                self.paths,
                Namespace(model=self.target.catalog_id, yes=False),
            )
        transaction_type.assert_not_called()
        rollback_store_type.assert_not_called()
        capture.assert_not_called()

    def test_rollback_without_yes_reads_but_never_mutates_store(self) -> None:
        pointer = _pointer(1, ROLLBACK_SHA256, None)
        rollback_spec = StubRollbackSpec(self.target)
        store = MagicMock()
        store.read_pointer.return_value = pointer
        store.read_spec.return_value = rollback_spec
        with (
            patch.object(cli, "RollbackStore", return_value=store),
            patch.object(cli, "TransactionStore") as transaction_type,
            patch.object(cli, "verify_rollback_spec", return_value={"verified": True}),
            patch.object(cli, "_selected_catalog_spec", return_value=(self.source, {})),
        ):
            result = cli._rollback(self.paths, Namespace(yes=False))

        self.assertTrue(result.facts["dryRun"])
        transaction_type.assert_not_called()
        store.read_pointer.assert_called_once_with()
        store.read_spec.assert_called_once_with(ROLLBACK_SHA256)
        for mutation in (store.put, store.publish, store.clear, store.restore):
            mutation.assert_not_called()


class UpgradeCaptureTests(RolloutCliTestCase):
    def test_begin_capture_binds_generated_transaction_id_to_anchor_and_intent(self) -> None:
        previous = _pointer(1, OLD_ROLLBACK_SHA256, None)
        transaction = RecordingTransaction()
        store = MagicMock()
        store.read_pointer.return_value = previous
        capture_result = self.rollback_spec
        completed_state = {**transaction.state, "state": "completed"}
        original = {"healthy": True, "profile": "latency"}

        with (
            patch.object(cli, "_selected_catalog_spec", return_value=(self.source, {})),
            patch.object(
                cli,
                "_rollout_admission",
                side_effect=self._admission(self.source, self.target),
            ),
            patch.object(cli, "_original_runtime", return_value=original),
            patch.object(
                cli, "capture_rollback_spec", return_value=capture_result
            ) as capture,
            patch.object(cli, "RollbackStore", return_value=store),
            patch.object(cli, "TransactionStore", return_value=transaction),
            patch.object(
                cli, "_execute_upgrade_rollout", return_value=completed_state
            ) as execute,
        ):
            result = cli._upgrade(
                self.paths,
                Namespace(model=self.target.catalog_id, yes=True),
            )

        capture.assert_called_once_with(
            self.paths,
            transaction_id=TRANSACTION_ID,
            captured_at=CREATED_AT,
            original=original,
            source_admission={
                "mode": "read-only-existing-selection-admission",
                "catalogRecoveryEligible": True,
            },
        )
        store.put.assert_called_once_with(capture_result)
        begin = transaction.begin_calls[0]
        self.assertEqual(begin["operation"], "upgrade")
        self.assertEqual(begin["rolloutIntent"]["rollbackSpecSha256"], ROLLBACK_SHA256)
        self.assertEqual(
            begin["rolloutIntent"]["previousRollbackPointer"], previous.document()
        )
        self.assertEqual(
            begin["rolloutIntent"]["rolloutPlanSha256"],
            build_upgrade_rollout_plan(
                self.source,
                self.target,
                ROLLBACK_SHA256,
                admission_granted=True,
            ).sha256,
        )
        execute.assert_called_once_with(
            self.paths,
            transaction,
            unittest.mock.ANY,
            self.source,
            self.target,
            capture_result,
            previous,
        )
        self.assertFalse(result.facts["dryRun"])


class RolloutExecutionTests(RolloutCliTestCase):
    def test_upgrade_executes_exact_argv_environment_order_and_advances(self) -> None:
        transaction = RecordingTransaction()
        store = MagicMock()
        published = _pointer(1, ROLLBACK_SHA256, None)
        store.publish.return_value = published
        observed: list[tuple[list[str], dict[str, Any]]] = []

        def runner(argv: list[str], **kwargs: Any) -> RunResult:
            observed.append((list(argv), dict(kwargs)))
            return _completed(argv)

        plan = build_upgrade_rollout_plan(
            self.source, self.target, ROLLBACK_SHA256, admission_granted=True
        )
        with (
            patch.object(cli, "RollbackStore", return_value=store),
            patch.object(cli, "verify_rollback_spec") as verify_anchor,
            patch.object(cli, "run", side_effect=runner),
            patch.object(cli, "_utc_now", return_value=UPDATED_AT),
        ):
            state = cli._execute_upgrade_rollout(
                self.paths,
                transaction,
                copy.deepcopy(transaction.state),
                self.source,
                self.target,
                self.rollback_spec,
                None,
            )

        required_artifact = next(
            artifact for artifact in self.target.artifacts if artifact.required
        )
        self.assertEqual(
            [argv for argv, _kwargs in observed],
            [
                ["scripts/acceptance-suite.sh", "quick", "--no-record"],
                [
                    "scripts/model-manager.py",
                    "download",
                    "--model",
                    self.target.catalog_id,
                    "--catalog-spec-sha256",
                    self.target.sha256,
                    "--artifact-sha256",
                    required_artifact.sha256,
                    "--replacement",
                    "--yes",
                ],
                ["scripts/runtime.sh", "stop"],
                [
                    "scripts/model-manager.py",
                    "select",
                    "--model",
                    self.target.catalog_id,
                    "--catalog-spec-sha256",
                    self.target.sha256,
                    "--replacement",
                    "--yes",
                ],
                ["scripts/runtime.sh", "start", "latency"],
                ["scripts/acceptance-suite.sh", "quick", "--no-record"],
            ],
        )
        executed_actions = [
            action
            for action in plan.actions
            if action.kind is not RolloutActionKind.PUBLISH_ROLLBACK
        ]
        for (_argv, kwargs), action in zip(observed, executed_actions, strict=True):
            subject_spec = self.source if action.subject == "source" else self.target
            self.assertEqual(
                kwargs["env"],
                cli._rollout_environment(
                    TRANSACTION_ID,
                    subject_spec,
                    subject=action.subject,
                    ordinal=action.ordinal,
                    kind=action.kind.value,
                ),
            )
            self.assertEqual(kwargs["cwd"], self.paths.root)
        self.assertEqual(
            [item["expected_action_ordinal"] for item in transaction.advances],
            list(range(len(plan.actions))),
        )
        self.assertEqual(
            [item["targetState"] for item in transaction.transitions],
            [
                "production_stopping",
                "candidate_starting",
                "accepting",
                "completed",
            ],
        )
        store.publish.assert_called_once_with(
            self.rollback_spec,
            expected_generation=0,
            expected_previous_sha256=None,
            transaction_id=TRANSACTION_ID,
            updated_at=UPDATED_AT,
        )
        verify_anchor.assert_called_once_with(self.paths, self.rollback_spec)
        self.assertEqual(
            transaction.advances[-1]["result_sha256"],
            cli._action_result(plan.actions[-1], facts=published.document()),
        )
        self.assertEqual(state["state"], "completed")

    def test_full_qualification_is_receipted_inside_runtime_boundary_before_publish(
        self,
    ) -> None:
        transaction = RecordingTransaction()
        store = MagicMock()
        published = _pointer(1, ROLLBACK_SHA256, None)
        receipt = {
            "policyId": "local-inference-stack/rollout-qualification-receipt-v1",
            "evidencePath": (
                f"logs/acceptance/qualification-{TRANSACTION_ID}-6.json"
            ),
            "evidenceSha256": "1" * 64,
            "evidenceSelfSha256": "2" * 64,
            "runManifestPath": (
                f"logs/acceptance/qualification-{TRANSACTION_ID}-6.run.json"
            ),
            "runManifestSha256": "3" * 64,
            "runManifestSelfSha256": "4" * 64,
            "stepResultsSha256": "5" * 64,
            "configurationSha256": "6" * 64,
            "runtimeIdentitySha256": "7" * 64,
            "rolloutBindingSha256": "8" * 64,
        }
        observed: list[tuple[list[str], dict[str, Any]]] = []

        def runner(argv: list[str], **kwargs: Any) -> RunResult:
            observed.append((list(argv), dict(kwargs)))
            if argv == ["scripts/acceptance-suite.sh", "full"]:
                self.assertEqual(transaction.runtime_events, ["enter"])
            return _completed(argv)

        def publish(*args: Any, **kwargs: Any) -> RollbackPointer:
            self.assertEqual(transaction.runtime_events, ["enter", "exit"])
            return published

        store.publish.side_effect = publish
        performance = SimpleNamespace(
            manifest_relative_path="deployments/reviewed/manifest.json",
            manifest_sha256="c" * 64,
            policy_sha256="d" * 64,
        )
        qualification_input = {
            "policyId": QUALIFICATION_INPUT_POLICY,
            "targetCatalogSpecSha256": self.target.sha256,
            "providerContractId": "local-qwen-provider-v1",
            "providerContractSha256": "1" * 64,
            "servedModelId": self.target.served_model_id,
            "limitsSha256": "2" * 64,
            "acceptanceSha256": "3" * 64,
            "logicalModels": REVIEWED_LOGICAL_MODELS,
            "providerMatrixModel": "qwen3.5-code",
            "toolUseMaxTokens": 2048,
            "directContextTokens": 118000,
            "modelPortContextTokens": 92000,
            "modelPortContextMaxTokens": 32768,
            "decodeTokens": 512,
            "decodeContextTokens": 0,
            "concurrency": 2,
            "concurrencyTokens": 512,
            "modelPortSourceIdentitySha256": "e" * 64,
            "liveModelRegistrySha256": "4" * 64,
            "toolUseLocalProviderReady": True,
        }
        qualification_input["sha256"] = cli.sha256_document(qualification_input)
        plan = build_upgrade_rollout_plan(
            self.source,
            self.target,
            ROLLBACK_SHA256,
            admission_granted=True,
            required_acceptance_tier="full",
            performance_policy_sha256="d" * 64,
            modelport_source_identity_sha256="e" * 64,
            qualification_input_sha256=qualification_input["sha256"],
        )
        full_action = next(
            action
            for action in plan.actions
            if action.kind is RolloutActionKind.TARGET_FULL
        )
        with (
            patch.object(cli, "RollbackStore", return_value=store),
            patch.object(cli, "verify_rollback_spec"),
            patch.object(
                cli,
                "_modelport_qualification_preflight",
                return_value=(
                    "/srv/modelport",
                    {"identity": "reviewed"},
                    qualification_input,
                ),
            ),
            patch.object(
                cli, "_target_performance_preflight", return_value=performance
            ),
            patch.object(cli, "run", side_effect=runner),
            patch.object(
                cli, "_qualification_evidence_receipt", return_value=receipt
            ) as read_receipt,
            patch.object(cli, "_utc_now", return_value=UPDATED_AT),
        ):
            state = cli._execute_upgrade_rollout(
                self.paths,
                transaction,
                copy.deepcopy(transaction.state),
                self.source,
                self.target,
                self.rollback_spec,
                None,
                qualification="full",
                modelport_project_dir="/srv/modelport",
                qualification_input=qualification_input,
                performance_policy=performance,
            )

        self.assertEqual(transaction.runtime_events, ["enter", "exit"])
        self.assertEqual(
            [argv for argv, _kwargs in observed][-2:],
            [
                ["scripts/acceptance-suite.sh", "quick", "--no-record"],
                ["scripts/acceptance-suite.sh", "full"],
            ],
        )
        full_kwargs = observed[-1][1]
        self.assertEqual(
            full_kwargs["env"],
            {
                **cli._rollout_environment(
                    TRANSACTION_ID,
                    self.target,
                    subject="target",
                    ordinal=full_action.ordinal,
                    kind="target-full",
                ),
                "MODELPORT_PROJECT_DIR": "/srv/modelport",
                "LOCAL_INFERENCE_PROVIDER_MATRIX_MODEL": "qwen3.5-code",
                "LOCAL_INFERENCE_TOOL_USE_MAX_TOKENS": "2048",
                "LOCAL_INFERENCE_SERVED_MODEL_ID": self.target.served_model_id,
                "LOCAL_INFERENCE_LOGICAL_FAST_MODEL": "qwen3.5-fast",
                "LOCAL_INFERENCE_LOGICAL_CODE_MODEL": "qwen3.5-code",
                "LOCAL_INFERENCE_LOGICAL_DEEP_MODEL": "qwen3.5-deep",
                "LOCAL_INFERENCE_DIRECT_CONTEXT_TOKENS": "118000",
                "LOCAL_INFERENCE_MODELPORT_CONTEXT_TOKENS": "92000",
                "LOCAL_INFERENCE_MODELPORT_CONTEXT_MAX_TOKENS": "32768",
                "LOCAL_INFERENCE_DECODE_TOKENS": "512",
                "LOCAL_INFERENCE_DECODE_CONTEXT_TOKENS": "0",
                "LOCAL_INFERENCE_CONCURRENCY": "2",
                "LOCAL_INFERENCE_CONCURRENCY_TOKENS": "512",
                "LOCAL_INFERENCE_PERFORMANCE_MANIFEST": (
                    performance.manifest_relative_path
                ),
                "LOCAL_INFERENCE_PERFORMANCE_POLICY_SHA256": (
                    performance.policy_sha256
                ),
            },
        )
        full_advance = transaction.advances[full_action.ordinal]
        self.assertEqual(full_advance["qualification_evidence"], receipt)
        self.assertNotIn("result_sha256", full_advance)
        read_receipt.assert_called_once_with(
            self.paths,
            transaction,
            unittest.mock.ANY,
            self.target,
            full_action,
        )
        self.assertEqual(state["state"], "completed")

    def test_rollback_executes_typed_order_and_clear_result(self) -> None:
        transaction = RecordingTransaction()
        transaction.state["operation"] = "rollback"
        pointer = _pointer(1, ROLLBACK_SHA256, None)
        cleared = _pointer(2, None, ROLLBACK_SHA256)
        anchor_spec = StubRollbackSpec(self.target)
        store = MagicMock()
        store.clear.return_value = cleared
        observed: list[tuple[list[str], dict[str, Any]]] = []

        def runner(argv: list[str], **kwargs: Any) -> RunResult:
            observed.append((list(argv), dict(kwargs)))
            return _completed(argv)

        plan = build_rollback_rollout_plan(
            self.source, self.target, ROLLBACK_SHA256, admission_granted=True
        )
        with (
            patch.object(cli, "RollbackStore", return_value=store),
            patch.object(cli, "verify_rollback_spec") as verify,
            patch.object(cli, "write_anchor_selection") as write_selection,
            patch.object(cli, "run", side_effect=runner),
            patch.object(cli, "_utc_now", return_value=UPDATED_AT),
        ):
            state = cli._execute_rollback_rollout(
                self.paths,
                transaction,
                copy.deepcopy(transaction.state),
                self.source,
                self.target,
                anchor_spec,
                pointer,
            )

        self.assertEqual(
            [argv for argv, _kwargs in observed],
            [
                ["scripts/runtime.sh", "stop"],
                ["scripts/runtime.sh", "start", "latency"],
                ["scripts/acceptance-suite.sh", "quick", "--no-record"],
            ],
        )
        executed_actions = [plan.actions[0], plan.actions[2], plan.actions[3]]
        for (_argv, kwargs), action in zip(observed, executed_actions, strict=True):
            subject_spec = self.source if action.subject == "source" else self.target
            expected_environment = cli._rollout_environment(
                TRANSACTION_ID,
                subject_spec,
                subject=action.subject,
                ordinal=action.ordinal,
                kind=action.kind.value,
            )
            if action.kind is RolloutActionKind.START_TARGET:
                expected_environment[cli.RUNTIME_PULL_POLICY_ENV] = (
                    cli.RUNTIME_PULL_POLICY_NEVER
                )
            self.assertEqual(
                kwargs["env"],
                expected_environment,
            )
        self.assertNotIn(cli.RUNTIME_PULL_POLICY_ENV, observed[0][1]["env"])
        self.assertNotIn(cli.RUNTIME_PULL_POLICY_ENV, observed[2][1]["env"])
        self.assertEqual(
            [item["expected_action_ordinal"] for item in transaction.advances],
            list(range(len(plan.actions))),
        )
        self.assertEqual(
            [item["targetState"] for item in transaction.transitions],
            [
                "production_stopping",
                "candidate_starting",
                "accepting",
                "completed",
            ],
        )
        self.assertEqual(
            transaction.authorizations,
            [
                {
                    "transactionId": TRANSACTION_ID,
                    "catalog_spec_sha256": self.target.sha256,
                    "catalog_id": self.target.catalog_id,
                    "rollout_subject": "target",
                    "action_ordinal": 1,
                    "action_kind": "activate-target",
                }
            ],
        )
        verify.assert_called_once_with(self.paths, anchor_spec)
        write_selection.assert_called_once_with(self.paths, anchor_spec)
        store.clear.assert_called_once_with(
            expected_generation=1,
            expected_previous_sha256=ROLLBACK_SHA256,
            transaction_id=TRANSACTION_ID,
            updated_at=UPDATED_AT,
        )
        self.assertEqual(
            transaction.advances[-1]["result_sha256"],
            cli._action_result(plan.actions[-1], facts=cleared.document()),
        )
        self.assertEqual(state["state"], "completed")

    def test_target_quick_failure_marks_recovery_and_never_publishes_or_completes(self) -> None:
        transaction = RecordingTransaction()
        store = MagicMock()
        store.read_pointer.return_value = None
        acceptance_runs = 0

        def runner(argv: list[str], **_kwargs: Any) -> RunResult:
            nonlocal acceptance_runs
            if argv == ["scripts/acceptance-suite.sh", "quick", "--no-record"]:
                acceptance_runs += 1
                if acceptance_runs == 2:
                    raise ExternalError("target quick failed")
            return _completed(argv)

        with (
            patch.object(cli, "_selected_catalog_spec", return_value=(self.source, {})),
            patch.object(
                cli,
                "_rollout_admission",
                side_effect=self._admission(self.source, self.target),
            ),
            patch.object(
                cli,
                "_original_runtime",
                return_value={"healthy": True, "profile": "latency"},
            ),
            patch.object(
                cli, "capture_rollback_spec", return_value=self.rollback_spec
            ),
            patch.object(cli, "RollbackStore", return_value=store),
            patch.object(cli, "TransactionStore", return_value=transaction),
            patch.object(cli, "run", side_effect=runner),
        ):
            with self.assertRaisesRegex(ExternalError, "target quick failed"):
                cli._upgrade(
                    self.paths,
                    Namespace(model=self.target.catalog_id, yes=True),
                )

        self.assertEqual(acceptance_runs, 2)
        store.publish.assert_not_called()
        self.assertEqual(transaction.state["state"], "recovery_required")
        self.assertEqual(
            [item["expected_action_ordinal"] for item in transaction.advances],
            [0, 1, 2, 3, 4],
        )
        self.assertNotIn(
            "completed", [item["targetState"] for item in transaction.transitions]
        )


class RolloutRecoveryTests(RolloutCliTestCase):
    def test_reconcile_reverifies_anchor_before_profile_or_runtime_mutation(self) -> None:
        original = {"healthy": True, "profile": "latency"}
        document = {
            "schemaVersion": SCHEMA_VERSION,
            "id": TRANSACTION_ID,
            "operation": "upgrade",
            "state": "recovery_required",
            "original": original,
            "rolloutIntent": {"rollbackSpecSha256": ROLLBACK_SHA256},
        }
        plan = {
            "required": True,
            "transaction": document,
            "runtimeDisposition": "restoration-required",
            "originalSafeToRestore": True,
        }
        transaction = MagicMock()
        transaction.reconciliation_plan.return_value = plan

        with (
            patch.object(cli, "TransactionStore", return_value=transaction),
            patch.object(cli, "_reconciliation_runtime_plan", return_value=plan),
            patch.object(cli, "recovery_original_is_safe", return_value=True),
            patch.object(
                cli,
                "_verified_rollout_recovery_anchor",
                side_effect=RecoveryError("anchor drift"),
            ),
            patch.object(cli, "_restore_deployment_profile") as restore_profile,
            patch.object(cli, "run") as runner,
            self.assertRaisesRegex(RecoveryError, "anchor drift"),
        ):
            cli._reconcile(self.paths, Namespace(yes=True))

        restore_profile.assert_not_called()
        runner.assert_not_called()
        transaction.transition.assert_not_called()

    def test_restore_pointer_handles_upgrade_publish_and_rollback_tombstone(self) -> None:
        upgrade_previous = _pointer(
            3, OLD_ROLLBACK_SHA256, OLDER_ROLLBACK_SHA256
        )
        upgrade_current = _pointer(
            4, ROLLBACK_SHA256, OLD_ROLLBACK_SHA256
        )
        upgrade_restored = _pointer(
            5, OLD_ROLLBACK_SHA256, ROLLBACK_SHA256
        )
        rollback_previous = _pointer(
            4, ROLLBACK_SHA256, OLD_ROLLBACK_SHA256
        )
        rollback_current = _pointer(5, None, ROLLBACK_SHA256)
        rollback_restored = _pointer(6, ROLLBACK_SHA256, None)

        cases = (
            (
                "upgrade",
                upgrade_previous,
                upgrade_current,
                upgrade_restored,
                ROLLBACK_SHA256,
            ),
            (
                "rollback",
                rollback_previous,
                rollback_current,
                rollback_restored,
                None,
            ),
        )
        for operation, previous, current, restored, expected_current in cases:
            with self.subTest(operation=operation):
                store = MagicMock()
                store.read_pointer.return_value = current
                store.restore.return_value = restored
                document = {
                    "id": TRANSACTION_ID,
                    "operation": operation,
                    "rolloutIntent": {
                        "rollbackSpecSha256": ROLLBACK_SHA256,
                        "previousRollbackPointer": previous.document(),
                    },
                }
                with (
                    patch.object(cli, "RollbackStore", return_value=store),
                    patch.object(cli, "_utc_now", return_value=UPDATED_AT),
                ):
                    result = cli._restore_rollout_pointer(self.paths, document)

                self.assertEqual(result, restored.document())
                store.restore.assert_called_once_with(
                    previous,
                    expected_current_generation=previous.generation + 1,
                    expected_current_sha256=expected_current,
                    transaction_id=TRANSACTION_ID,
                    updated_at=UPDATED_AT,
                )

    def test_restore_pointer_retry_accepts_only_the_exact_durable_successor(self) -> None:
        cases = (
            (
                "upgrade",
                _pointer(3, OLD_ROLLBACK_SHA256, OLDER_ROLLBACK_SHA256),
                _pointer(5, OLD_ROLLBACK_SHA256, ROLLBACK_SHA256),
            ),
            (
                "rollback",
                _pointer(4, ROLLBACK_SHA256, OLD_ROLLBACK_SHA256),
                _pointer(6, ROLLBACK_SHA256, None),
            ),
        )
        for operation, previous, durable_successor in cases:
            with self.subTest(operation=operation):
                store = MagicMock()
                store.read_pointer.return_value = durable_successor
                document = {
                    "id": TRANSACTION_ID,
                    "operation": operation,
                    "rolloutIntent": {
                        "rollbackSpecSha256": ROLLBACK_SHA256,
                        "previousRollbackPointer": previous.document(),
                    },
                }
                with patch.object(cli, "RollbackStore", return_value=store):
                    result = cli._restore_rollout_pointer(self.paths, document)

                self.assertEqual(result, durable_successor.document())
                store.restore.assert_not_called()

    def test_restore_pointer_rejects_a_foreign_current_pointer(self) -> None:
        previous = _pointer(1, OLD_ROLLBACK_SHA256, None)
        foreign = _pointer(2, FOREIGN_ROLLBACK_SHA256, OLD_ROLLBACK_SHA256)
        store = MagicMock()
        store.read_pointer.return_value = foreign
        store.restore.side_effect = RollbackCASMismatch(
            "rollback pointer compare-and-swap precondition failed"
        )
        document = {
            "id": TRANSACTION_ID,
            "operation": "upgrade",
            "rolloutIntent": {
                "rollbackSpecSha256": ROLLBACK_SHA256,
                "previousRollbackPointer": previous.document(),
            },
        }

        with patch.object(cli, "RollbackStore", return_value=store):
            with self.assertRaisesRegex(
                RollbackCASMismatch, "compare-and-swap precondition failed"
            ):
                cli._restore_rollout_pointer(self.paths, document)

        store.restore.assert_called_once_with(
            previous,
            expected_current_generation=2,
            expected_current_sha256=ROLLBACK_SHA256,
            transaction_id=TRANSACTION_ID,
            updated_at=unittest.mock.ANY,
        )

    def test_failed_recovery_quick_never_restores_the_pointer(self) -> None:
        original = {"healthy": True, "profile": "latency"}
        document = {
            "id": TRANSACTION_ID,
            "operation": "upgrade",
            "rolloutIntent": {"rollbackSpecSha256": ROLLBACK_SHA256},
        }
        store = MagicMock()
        store.read_spec.return_value = self.rollback_spec

        with (
            patch.object(cli, "RollbackStore", return_value=store),
            patch.object(cli, "rollback_recovery_original", return_value=original),
            patch.object(cli, "verify_rollback_spec", return_value={"verified": True}),
            patch.object(cli, "_verify_restored_runtime") as verify_runtime,
            patch.object(
                cli, "run", side_effect=ExternalError("recovery quick failed")
            ) as runner,
            patch.object(cli, "_restore_rollout_pointer") as restore_pointer,
        ):
            with self.assertRaisesRegex(ExternalError, "recovery quick failed"):
                cli._verify_rollout_recovery(self.paths, document, original)

        verify_runtime.assert_called_once_with(self.paths, original)
        runner.assert_called_once_with(
            ["scripts/acceptance-suite.sh", "quick", "--no-record"],
            cwd=self.paths.root,
            timeout=7200,
            env={"QWEN_CONTROL_TRANSACTION_ID": TRANSACTION_ID},
        )
        restore_pointer.assert_not_called()


class RolloutResultContractTests(unittest.TestCase):
    def test_public_cli_classifies_rollout_integrity_and_store_failures(self) -> None:
        cases = (
            (RollbackSpecError("rollback subject is invalid"), 6),
            (RollbackStoreError("rollback pointer is unavailable"), 7),
        )
        for error, expected_code in cases:
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with (
                    patch.object(cli.ProjectPaths, "discover", return_value=MagicMock()),
                    patch.object(cli, "dispatch", side_effect=error),
                    redirect_stdout(output),
                ):
                    code = cli.main(["--json", "rollback"])
                payload = json.loads(output.getvalue())
                self.assertEqual(code, expected_code)
                self.assertEqual(payload["code"], expected_code)
                self.assertEqual(payload["status"], "error")
                self.assertNotIn("unexpected control-plane error", payload["summary"])


if __name__ == "__main__":
    unittest.main()
