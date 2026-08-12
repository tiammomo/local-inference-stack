from __future__ import annotations

import copy
import hashlib
import fcntl
import json
import os
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_inference_stack import (
    attestation,
    bundle,
    calibration,
    cli,
    configuration,
    migration,
    reference,
    storage,
)
from local_inference_stack.deployment import (
    CatalogDeploymentSpec,
    build_deployment_plan,
    build_rollback_rollout_plan,
    build_upgrade_rollout_plan,
)
from local_inference_stack.materials import canonical_sha256
from local_inference_stack.paths import ProjectPaths
from local_inference_stack.result import (
    CommandResult,
    ConfigError,
    ExternalError,
    IntegrityError,
    RecoveryError,
    UsageError,
)
from local_inference_stack.rollout import (
    ROLLBACK_POINTER_SCHEMA_VERSION,
    ROLLBACK_SPEC_SCHEMA_VERSION,
)
from local_inference_stack.runner import RunResult, controlled_environment
from local_inference_stack.transactions import (
    RECOVERY_DEPLOYMENT_KEYS,
    ROLLOUT_INTENT_POLICY_ID,
    TransactionStore,
    recovery_original_is_safe,
)


def write_legacy_selected_profile(root: Path) -> ProjectPaths:
    paths = ProjectPaths(root)
    catalog_dir = root / "catalog"
    catalog_dir.mkdir(parents=True)
    catalog_dir.chmod(0o700)
    (catalog_dir / "models.json").write_text(
        (ROOT / "catalog" / "models.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    model = configuration.load_catalog(catalog_dir / "models.json")["models"][0]
    values = configuration.catalog_deployment_environment(model)
    values.pop("QWEN_CACHE_TYPE_K")
    values.pop("QWEN_CACHE_TYPE_V")
    profile_dir = root / "profiles"
    profile_dir.mkdir(mode=0o700)
    profile = profile_dir / "deployment.local.env"
    profile.write_text(
        "# legacy selected profile\n"
        + "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    profile.chmod(0o600)
    return paths


class ConfigurationTests(unittest.TestCase):
    def test_checked_in_profiles_are_deterministic_derivatives(self) -> None:
        self.assertEqual(configuration.check(ProjectPaths(ROOT)), [])

    def test_systemd_is_the_only_runtime_restart_owner(self) -> None:
        document = json.loads((ROOT / "config/runtime-profiles.json").read_text())
        configured_keys = {
            key
            for profile in document["profiles"].values()
            for key in profile["environment"]
        }
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertNotIn("QWEN_RESTART_POLICY", configured_keys)
        self.assertNotIn("QWEN_RESTART_POLICY", compose)
        self.assertEqual(compose.count('restart: "no"'), 1)

    def test_profile_validation_rejects_candidate_production_port(self) -> None:
        document = json.loads((ROOT / "config/runtime-profiles.json").read_text())
        document["profiles"]["candidate"]["environment"]["QWEN_PUBLISH_PORT"] = "18080"
        with self.assertRaisesRegex(Exception, "production port"):
            configuration.validate(document)

    def test_profiles_cannot_override_catalog_model_capacity(self) -> None:
        document = json.loads((ROOT / "config/runtime-profiles.json").read_text())
        document["profiles"]["latency"]["environment"]["QWEN_CTX_SIZE"] = "4096"
        with self.assertRaisesRegex(Exception, "exact supported key set"):
            configuration.validate(document)

    def test_selected_profile_migration_is_exact_private_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = write_legacy_selected_profile(Path(directory))
            before = configuration.selected_deployment_profile_status(paths)
            self.assertEqual(before["status"], "legacy-compatible-current-defaults")
            self.assertEqual(
                before["missingKeys"], ["QWEN_CACHE_TYPE_K", "QWEN_CACHE_TYPE_V"]
            )
            result = configuration.normalize_selected_deployment_profile(paths)
            profile = paths.root / "profiles" / "deployment.local.env"
            self.assertTrue(result["changed"])
            self.assertEqual(result["after"]["status"], "exact-current-projection")
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
            self.assertFalse(profile.is_symlink())

    def test_selected_profile_migration_rejects_value_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = write_legacy_selected_profile(Path(directory))
            profile = paths.root / "profiles" / "deployment.local.env"
            body = profile.read_text(encoding="utf-8").replace(
                "QWEN_CTX_SIZE=131072", "QWEN_CTX_SIZE=4096"
            )
            profile.write_text(body, encoding="utf-8")
            profile.chmod(0o600)
            status = configuration.selected_deployment_profile_status(paths)
            self.assertEqual(status["status"], "mismatch")
            with self.assertRaisesRegex(ConfigError, "not a compatible migration source"):
                configuration.normalize_selected_deployment_profile(paths)

    def test_selected_profile_migration_rejects_unreviewed_field_omission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = write_legacy_selected_profile(Path(directory))
            profile = paths.root / "profiles" / "deployment.local.env"
            body = "\n".join(
                line
                for line in profile.read_text(encoding="utf-8").splitlines()
                if not line.startswith("QWEN_MODEL_DISPLAY_NAME=")
            )
            profile.write_text(body + "\n", encoding="utf-8")
            profile.chmod(0o600)
            status = configuration.selected_deployment_profile_status(paths)
            self.assertEqual(status["status"], "mismatch")
            self.assertFalse(status["migrationRequired"])
            with self.assertRaisesRegex(ConfigError, "not a compatible migration source"):
                configuration.normalize_selected_deployment_profile(paths)

    def test_dashboard_render_rejects_an_invalid_catalog_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "catalog").mkdir()
            (root / "dashboard").mkdir()
            (root / "dashboard" / "runtime-baseline.json").write_text("{}")
            marker = "catalog-secret-marker"
            (root / "catalog" / "models.json").write_text(
                json.dumps({"schemaVersion": 2, "scope": marker})
            )
            profiles = json.loads(
                (ROOT / "config" / "runtime-profiles.json").read_text()
            )
            with self.assertRaises(ConfigError) as raised:
                configuration.render_dashboard(ProjectPaths(root), profiles)
            self.assertNotIn(marker, str(raised.exception))


class CalibrationTests(unittest.TestCase):
    def test_plan_derives_capacity_from_the_selected_catalog_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "catalog").mkdir()
            (root / "profiles").mkdir()
            (root / "config" / "runtime-profiles.json").write_text(
                (ROOT / "config" / "runtime-profiles.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            catalog = json.loads(
                (ROOT / "catalog" / "models.json").read_text(encoding="utf-8")
            )
            fixture_model = json.loads(json.dumps(catalog["models"][0]))
            fixture_model["id"] = "fixture-alt-model"
            fixture_model["lifecycleRole"] = "candidate"
            fixture_model["runtime"].update(
                {
                    "contextTokens": 65536,
                    "recommendedInputTokens": 32768,
                    "maxOutputTokens": 16384,
                    "batchSize": 1536,
                    "ubatchSize": 768,
                }
            )
            catalog["models"].append(fixture_model)
            (root / "catalog" / "models.json").write_text(
                json.dumps(catalog), encoding="utf-8"
            )
            selected = root / "profiles" / "deployment.local.env"
            selected.write_text("QWEN_CATALOG_ID=fixture-alt-model\n", encoding="utf-8")
            selected.chmod(0o600)

            result = calibration.plan(ProjectPaths(root))

            self.assertEqual(result["catalogModel"], "fixture-alt-model")
            self.assertEqual(result["fixedModelCapacity"]["QWEN_CTX_SIZE"], "65536")
            self.assertEqual(result["candidates"][0]["batchSize"], 1536)
            self.assertEqual(result["candidates"][1]["ubatchSize"], 384)

    def test_plan_rejects_an_invalid_catalog_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "catalog").mkdir()
            (root / "config" / "runtime-profiles.json").write_text(
                (ROOT / "config" / "runtime-profiles.json").read_text()
            )
            marker = "catalog-secret-marker"
            (root / "catalog" / "models.json").write_text(
                json.dumps({"schemaVersion": 2, "scope": marker})
            )
            with self.assertRaises(ConfigError) as raised:
                calibration.plan(ProjectPaths(root))
            self.assertNotIn(marker, str(raised.exception))

    def test_calibration_uses_non_promotable_decode_and_concurrency_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            output = paths.root / "logs" / "calibration" / "report.json"
            completed = RunResult(("benchmark",), 0, '{"benchmarkPassed":true}', "")
            with (
                patch.object(calibration, "plan", return_value={"baseline": "latency"}),
                patch.object(
                    calibration, "run", side_effect=[completed, completed]
                ) as runner,
            ):
                facts = calibration.run_benchmarks(paths, output)
            commands = [call.args[0] for call in runner.call_args_list]
            self.assertEqual(
                commands,
                [
                    [
                        "python3",
                        "scripts/decode-benchmark.py",
                        "--baseline-only",
                        "--json",
                    ],
                    [
                        "python3",
                        "scripts/concurrency-benchmark.py",
                        "--baseline-only",
                        "--json",
                    ],
                ],
            )
            self.assertTrue(facts["measurementsComplete"])
            self.assertEqual(
                facts["evidenceEligibility"], "baseline-only-not-promotable"
            )
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

class CommandContractTests(unittest.TestCase):
    def test_generated_reference_covers_the_argparse_command_tree(self) -> None:
        def leaves(parser: object, prefix: tuple[str, ...] = ()) -> set[str]:
            children = None
            for action in parser._actions:
                if action.__class__.__name__ == "_SubParsersAction":
                    children = action.choices
                    break
            if children is None:
                return {" ".join(prefix)}
            result: set[str] = set()
            for name, child in children.items():
                result.update(leaves(child, (*prefix, name)))
            return result

        self.assertEqual(leaves(cli.parser()), set(reference.command_paths()))
        self.assertEqual(
            set(reference.COMMANDS),
            {path.split()[0] for path in reference.command_paths()},
        )

    def test_reference_check_and_write_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(UsageError, "not allowed"):
            cli.parser().parse_args(
                ["reference", "--check", "--write", "--yes"]
            )

    def test_doctor_uses_recovery_exit_for_an_unfinished_transaction(self) -> None:
        plan = {
            "required": True,
            "classification": "legacy-failed-review-required",
            "automaticEligible": False,
            "transaction": {"schemaVersion": 1, "state": "failed"},
            "actions": ["inspect"],
        }
        with (
            patch.object(cli.configuration, "check", return_value=[]),
            patch.object(cli.TransactionStore, "reconciliation_plan", return_value=plan),
            patch.object(cli.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(cli.platform, "system", return_value="Linux"),
            patch.object(cli.platform, "machine", return_value="x86_64"),
        ):
            result = cli._doctor(ProjectPaths(ROOT))
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.code, 7)
        self.assertEqual(result.facts["reconciliation"], plan)
        self.assertFalse(result.nextActions[0].requiresApproval)

    def test_controlled_subprocesses_preserve_only_the_public_trust_anchor(self) -> None:
        key_path = "/var/lib/local-inference/trust/catalog.pub"
        fingerprint = "a" * 64
        with patch.dict(
            os.environ,
            {
                "LOCAL_INFERENCE_TRUSTED_ATTESTATION_KEY": key_path,
                "LOCAL_INFERENCE_TRUSTED_ATTESTATION_KEY_SHA256": fingerprint,
                "UNRELATED_SECRET": "must-not-cross-the-boundary",
            },
            clear=True,
        ):
            environment = controlled_environment()
        self.assertEqual(
            environment["LOCAL_INFERENCE_TRUSTED_ATTESTATION_KEY"], key_path
        )
        self.assertEqual(
            environment["LOCAL_INFERENCE_TRUSTED_ATTESTATION_KEY_SHA256"],
            fingerprint,
        )
        self.assertNotIn("UNRELATED_SECRET", environment)

    def test_command_result_has_stable_top_level_shape(self) -> None:
        result = CommandResult("doctor", "ok", "healthy").as_dict()
        self.assertEqual(
            set(result),
            {
                "schemaVersion",
                "command",
                "status",
                "code",
                "summary",
                "facts",
                "nextActions",
            },
        )

    def test_plan_adapter_preserves_the_typed_payload(self) -> None:
        payload = {
            "mode": "read-only-plan",
            "recommendation": {"id": "reviewed-model"},
            "evidenceStatus": "estimated-on-this-host",
            "readyToDeploy": False,
            "actionPlan": None,
        }
        with patch.object(
            cli,
            "run",
            return_value=RunResult(("python3",), 0, json.dumps(payload), ""),
        ):
            result = cli._plan_result(
                ProjectPaths(ROOT),
                Namespace(model=None, vram_gib=None, ram_gib=None),
            )
        self.assertEqual(result.facts["plan"], payload)
        self.assertEqual(result.code, 0)

    def test_mutation_without_yes_is_structured_usage_error(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = cli.main(["deploy", "--json"])
        document = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(document["code"], 2)
        self.assertEqual(document["status"], "error")


class MigrationTests(unittest.TestCase):
    def test_migration_registry_matches_the_attestation_implementation(self) -> None:
        self.assertEqual(
            migration.CURRENT["attestation"], attestation.SCHEMA_VERSION
        )
        self.assertEqual(
            migration.READABLE["attestation"], {attestation.SCHEMA_VERSION}
        )
        self.assertEqual(
            migration.CURRENT["rollbackSpec"], ROLLBACK_SPEC_SCHEMA_VERSION
        )
        self.assertEqual(
            migration.CURRENT["rollbackPointer"], ROLLBACK_POINTER_SCHEMA_VERSION
        )

    def test_migration_observes_the_active_rollback_pointer_and_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            paths.config_path.parent.mkdir(parents=True)
            paths.config_path.write_text('{"schemaVersion":2}\n', encoding="utf-8")
            pointer = MagicMock(active_spec_sha256="a" * 64)
            pointer.document.return_value = {
                "schemaVersion": ROLLBACK_POINTER_SCHEMA_VERSION
            }
            spec = MagicMock()
            spec.document.return_value = {
                "schemaVersion": ROLLBACK_SPEC_SCHEMA_VERSION
            }
            store = MagicMock()
            store.read_pointer.return_value = pointer
            store.read_spec.return_value = spec
            with patch.object(migration, "RollbackStore", return_value=store):
                report = migration.check(paths)

            self.assertEqual(
                report["observed"]["rollbackPointer"],
                ROLLBACK_POINTER_SCHEMA_VERSION,
            )
            self.assertEqual(
                report["observed"]["rollbackSpec"],
                ROLLBACK_SPEC_SCHEMA_VERSION,
            )
            self.assertNotIn("rollbackPointer", report["incompatible"])
            self.assertNotIn("rollbackSpec", report["incompatible"])

    def test_readable_legacy_schema_reports_attention_until_explicit_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            paths.config_path.parent.mkdir(parents=True)
            paths.config_path.write_text('{"schemaVersion":2}\n', encoding="utf-8")
            paths.state_dir.mkdir(parents=True)
            paths.transaction_path.write_text(
                '{"schemaVersion":1}\n', encoding="utf-8"
            )
            result = cli.dispatch(paths, Namespace(command="migrate"))
            self.assertEqual(result.status, "attention")
            self.assertEqual(result.code, 4)
            self.assertIn("transaction", result.facts["migrationsRequired"])
            self.assertEqual(result.nextActions[0].command, "./stack reconcile --json")

    def test_current_schemas_report_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            paths.config_path.parent.mkdir(parents=True)
            paths.config_path.write_text('{"schemaVersion":2}\n', encoding="utf-8")
            result = cli.dispatch(paths, Namespace(command="migrate"))
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.code, 0)

    def test_explicit_migration_verifies_artifact_before_profile_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = write_legacy_selected_profile(Path(directory))
            paths.config_path.parent.mkdir(parents=True)
            paths.config_path.write_text('{"schemaVersion":2}\n', encoding="utf-8")
            completed = RunResult(("model-manager",), 0, "verified", "")
            with patch.object(cli, "run", return_value=completed) as runner:
                result = cli.dispatch(
                    paths,
                    Namespace(command="migrate", yes=True, check=False),
                )
            self.assertEqual(result.status, "ok")
            self.assertTrue(
                result.facts["selectedDeploymentProfileMigration"]["changed"]
            )
            self.assertEqual(
                runner.call_args.args[0],
                ["python3", "scripts/model-manager.py", "verify", "--read-only"],
            )
            self.assertEqual(
                configuration.selected_deployment_profile_status(paths)["status"],
                "exact-current-projection",
            )


class TransactionTests(unittest.TestCase):
    @staticmethod
    def safe_original(*, healthy: bool = False) -> dict[str, object]:
        return {
            "healthy": healthy,
            "containerHealthy": healthy,
            "profile": "latency" if healthy else "unknown",
            "containerName": "qwen35-9b-q5km",
            "runtimeIdentity": (
                {"sha256": "a" * 64, "configuration": {"profile": "latency"}}
                if healthy
                else None
            ),
            "deploymentProfile": {"present": False},
            "capturedWithoutSecrets": True,
        }

    @staticmethod
    def rollout_specs() -> tuple[CatalogDeploymentSpec, CatalogDeploymentSpec]:
        model = json.loads(
            (ROOT / "catalog" / "models.json").read_text(encoding="utf-8")
        )["models"][0]
        source = CatalogDeploymentSpec.from_catalog_model(model)
        target_model = copy.deepcopy(model)
        target_model["id"] = "qwen35-9b-next"
        target_model["servedModelId"] = "qwen3.5-9b-next"
        target_model["modelDirectory"] = "qwen3.5-9b-next"
        target = CatalogDeploymentSpec.from_catalog_model(target_model)
        return source, target

    @staticmethod
    def rollout_intent(
        plan: Any, *, previous_pointer: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        document = plan.document()
        return {
            "policyId": ROLLOUT_INTENT_POLICY_ID,
            "rollbackSpecSha256": document["rollbackSpecSha256"],
            "sourceCatalogSpecSha256": document[
                "sourceCatalogSpecSha256"
            ],
            "targetCatalogSpecSha256": document[
                "targetCatalogSpecSha256"
            ],
            "rolloutPlan": document,
            "rolloutPlanSha256": canonical_sha256(document),
            "previousRollbackPointer": previous_pointer,
        }

    def test_interrupted_transaction_is_durable_and_blocks_a_second_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            first = TransactionStore(paths)
            document = first.begin("release", "quick", self.safe_original(healthy=True))
            first.transition("production_stopping", expected_id=document["id"])
            after_restart = TransactionStore(paths)
            plan = after_restart.reconciliation_plan()
            self.assertTrue(plan["required"])
            self.assertEqual(plan["transaction"]["id"], document["id"])
            with self.assertRaises(RecoveryError):
                after_restart.begin("profile", "throughput", {})
            after_restart.transition(
                "recovery_required",
                expected_id=document["id"],
                detail="injected SIGKILL boundary",
            )
            after_restart.transition(
                "production_restoring", expected_id=document["id"]
            )
            completed = after_restart.transition(
                "failed-restored", expected_id=document["id"]
            )
            self.assertFalse(TransactionStore(paths).reconciliation_plan()["required"])
            self.assertEqual(completed["state"], "failed-restored")
            self.assertEqual(paths.transaction_path.stat().st_mode & 0o777, 0o600)

    def test_invalid_transition_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            document = store.begin("release", "quick", {})
            with self.assertRaises(RecoveryError):
                store.transition("completed", expected_id=document["id"])

    def test_begin_uses_the_global_runtime_then_transaction_lock_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            events: list[str] = []

            @contextmanager
            def boundary():
                events.append("runtime-enter")
                try:
                    yield
                finally:
                    events.append("runtime-exit")

            @contextmanager
            def transaction_lock():
                events.append("transaction-enter")
                try:
                    yield
                finally:
                    events.append("transaction-exit")

            with (
                patch.object(store, "runtime_boundary", boundary),
                patch.object(store, "locked", transaction_lock),
            ):
                store.begin("deploy", "reviewed-model", self.safe_original())

            self.assertEqual(
                events,
                [
                    "runtime-enter",
                    "transaction-enter",
                    "transaction-exit",
                    "runtime-exit",
                ],
            )

    def test_begin_rollout_captures_and_publishes_inside_both_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            source, target = self.rollout_specs()
            plan = build_upgrade_rollout_plan(
                source,
                target,
                "a" * 64,
                admission_granted=True,
            )
            intent = self.rollout_intent(plan)
            events: list[str] = []

            @contextmanager
            def runtime_boundary():
                events.append("runtime-enter")
                try:
                    yield
                finally:
                    events.append("runtime-exit")

            @contextmanager
            def transaction_lock():
                events.append("transaction-enter")
                try:
                    yield
                finally:
                    events.append("transaction-exit")

            captured_authority: list[tuple[str, str]] = []

            def capture(
                transaction_id: str, created_at: str
            ) -> tuple[dict[str, Any], dict[str, Any]]:
                events.append("capture")
                captured_authority.append((transaction_id, created_at))
                return self.safe_original(healthy=True), intent

            with (
                patch.object(store, "runtime_boundary", runtime_boundary),
                patch.object(store, "locked", transaction_lock),
            ):
                document = store.begin_rollout(
                    "upgrade",
                    target.catalog_id,
                    capture,
                    approved_catalog_spec=target.approval_document(),
                )

            self.assertEqual(
                events,
                [
                    "runtime-enter",
                    "transaction-enter",
                    "capture",
                    "transaction-exit",
                    "runtime-exit",
                ],
            )
            self.assertEqual(document["rolloutIntent"], intent)
            self.assertEqual(
                captured_authority,
                [(document["id"], document["createdAt"])],
            )
            self.assertEqual(document["rolloutActionOrdinal"], 0)
            self.assertEqual(document["rolloutActionResults"], [])
            self.assertEqual(TransactionStore(store.paths).read(), document)

    def test_begin_rollout_capture_failure_publishes_no_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            _source, target = self.rollout_specs()

            def fail_capture(_transaction_id: str, _created_at: str) -> Any:
                raise RuntimeError("injected capture failure")

            with self.assertRaisesRegex(RuntimeError, "capture failure"):
                store.begin_rollout(
                    "upgrade",
                    target.catalog_id,
                    fail_capture,
                    approved_catalog_spec=target.approval_document(),
                )
            self.assertIsNone(store.read())

    def test_rollout_intent_rejects_wrong_operation_spec_and_shape(self) -> None:
        source, target = self.rollout_specs()
        plan = build_upgrade_rollout_plan(
            source,
            target,
            "a" * 64,
            admission_granted=True,
        )
        baseline = self.rollout_intent(plan)
        mutations = []
        wrong_operation = copy.deepcopy(baseline)
        wrong_operation["rolloutPlan"]["operation"] = "rollback"
        wrong_operation["rolloutPlanSha256"] = canonical_sha256(
            wrong_operation["rolloutPlan"]
        )
        mutations.append(("upgrade", target, wrong_operation))
        wrong_target = copy.deepcopy(baseline)
        wrong_target["targetCatalogSpecSha256"] = source.sha256
        mutations.append(("upgrade", target, wrong_target))
        extra_key = copy.deepcopy(baseline)
        extra_key["unreviewed"] = True
        mutations.append(("upgrade", target, extra_key))
        boolean_ordinal = copy.deepcopy(baseline)
        boolean_ordinal["rolloutPlan"]["actions"][0]["ordinal"] = False
        boolean_ordinal["rolloutPlanSha256"] = canonical_sha256(
            boolean_ordinal["rolloutPlan"]
        )
        mutations.append(("upgrade", target, boolean_ordinal))
        unproved_full = copy.deepcopy(baseline)
        unproved_full["rolloutPlan"]["requiredAcceptanceTier"] = "full"
        unproved_full["rolloutPlanSha256"] = canonical_sha256(
            unproved_full["rolloutPlan"]
        )
        mutations.append(("upgrade", target, unproved_full))

        for operation, approved, intent in mutations:
            with self.subTest(intent=intent):
                with tempfile.TemporaryDirectory() as directory:
                    store = TransactionStore(ProjectPaths(Path(directory)))
                    with self.assertRaisesRegex(RecoveryError, "rollout"):
                        store.begin(
                            operation,
                            target.catalog_id,
                            self.safe_original(healthy=True),
                            approved_catalog_spec=approved.approval_document(),
                            rollout_intent=intent,
                        )
                    self.assertIsNone(store.read())

        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            with self.assertRaisesRegex(RecoveryError, "operation and target"):
                store.begin(
                    "upgrade",
                    target.catalog_id,
                    self.safe_original(healthy=True),
                    approved_catalog_spec=source.approval_document(),
                    rollout_intent=baseline,
                )

    def test_rollout_action_cas_rejects_wrong_ordinal_replay_and_early_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            source, target = self.rollout_specs()
            plan = build_upgrade_rollout_plan(
                source,
                target,
                "a" * 64,
                admission_granted=True,
            )
            document = store.begin_rollout(
                "upgrade",
                target.catalog_id,
                lambda _transaction_id, _created_at: (
                    self.safe_original(healthy=True),
                    self.rollout_intent(plan),
                ),
                approved_catalog_spec=target.approval_document(),
            )
            transaction_id = document["id"]
            store.transition(
                "deploying",
                expected_id=transaction_id,
                expected_state="planned",
                expected_action_ordinal=0,
            )
            with self.assertRaisesRegex(RecoveryError, "ordinal"):
                with store.authorized_runtime_mutation(
                    transaction_id,
                    catalog_spec_sha256=source.sha256,
                    catalog_id=source.catalog_id,
                    rollout_subject="source",
                    action_ordinal=1,
                    action_kind="source-quick",
                ):
                    self.fail("a skipped rollout action crossed the gate")
            with store.authorized_runtime_mutation(
                transaction_id,
                catalog_spec_sha256=source.sha256,
                catalog_id=source.catalog_id,
                rollout_subject="source",
                action_ordinal=0,
                action_kind="source-quick",
            ):
                pass
            with self.assertRaisesRegex(RecoveryError, "pending rollout action"):
                with store.authorized_runtime_mutation(
                    transaction_id,
                    catalog_spec_sha256=source.sha256,
                    catalog_id=source.catalog_id,
                    rollout_subject="source",
                    action_ordinal=0,
                    action_kind="stop-source",
                ):
                    self.fail("a different rollout action kind crossed the gate")
            with self.assertRaisesRegex(RecoveryError, "action kind"):
                store.advance_rollout_action(
                    expected_id=transaction_id,
                    expected_state="deploying",
                    expected_action_ordinal=0,
                    expected_action_kind="stop-source",
                    result_sha256="b" * 64,
                )
            with self.assertRaisesRegex(RecoveryError, "skipped or replayed"):
                store.advance_rollout_action(
                    expected_id=transaction_id,
                    expected_state="deploying",
                    expected_action_ordinal=1,
                    expected_action_kind="fetch-target-artifact",
                    result_sha256="b" * 64,
                )
            store.advance_rollout_action(
                expected_id=transaction_id,
                expected_state="deploying",
                expected_action_ordinal=0,
                expected_action_kind="source-quick",
                result_sha256="b" * 64,
            )
            with self.assertRaisesRegex(RecoveryError, "skipped or replayed"):
                store.advance_rollout_action(
                    expected_id=transaction_id,
                    expected_state="deploying",
                    expected_action_ordinal=0,
                    expected_action_kind="source-quick",
                    result_sha256="c" * 64,
                )
            with self.assertRaisesRegex(RecoveryError, "before all actions"):
                store.transition(
                    "completed",
                    expected_id=transaction_id,
                    expected_state="deploying",
                    expected_action_ordinal=1,
                )

            for action in plan.document()["actions"][1:]:
                subject = action["subject"]
                spec = source if subject == "source" else target
                with store.authorized_runtime_mutation(
                    transaction_id,
                    catalog_spec_sha256=spec.sha256,
                    catalog_id=action["catalogId"],
                    artifact_sha256=action.get("artifactSha256"),
                    rollout_subject=subject,
                    action_ordinal=action["ordinal"],
                    action_kind=action["kind"],
                ):
                    pass
                store.advance_rollout_action(
                    expected_id=transaction_id,
                    expected_state="deploying",
                    expected_action_ordinal=action["ordinal"],
                    expected_action_kind=action["kind"],
                    result_sha256=f"{(action['ordinal'] + 1) % 16:x}" * 64,
                )
            completed = store.transition(
                "completed",
                expected_id=transaction_id,
                expected_state="deploying",
                expected_action_ordinal=len(plan.actions),
            )
            self.assertEqual(completed["state"], "completed")

    def test_rollout_failure_can_enter_recovery_required_at_pending_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            source, target = self.rollout_specs()
            plan = build_upgrade_rollout_plan(
                source,
                target,
                "a" * 64,
                admission_granted=True,
            )
            document = store.begin_rollout(
                "upgrade",
                target.catalog_id,
                lambda _transaction_id, _created_at: (
                    self.safe_original(),
                    self.rollout_intent(plan),
                ),
                approved_catalog_spec=target.approval_document(),
            )
            failed = store.transition(
                "recovery_required",
                expected_id=document["id"],
                expected_state="planned",
                expected_action_ordinal=0,
                detail="injected action failure",
            )
            self.assertEqual(failed["state"], "recovery_required")
            self.assertEqual(failed["rolloutActionOrdinal"], 0)
            for state in ("recovery_required", "production_restoring"):
                with self.subTest(state=state), self.assertRaisesRegex(
                    RecoveryError, "recovering rollout"
                ):
                    with store.authorized_runtime_mutation(
                        document["id"],
                        catalog_spec_sha256=source.sha256,
                        catalog_id=source.catalog_id,
                        rollout_subject="source",
                        action_ordinal=0,
                        action_kind="source-quick",
                    ):
                        self.fail("a stale rollout action crossed the recovery fence")
                with self.assertRaisesRegex(
                    RecoveryError, "recovering rollout"
                ):
                    store.assert_approved_deployment(
                        transaction_id=document["id"],
                        catalog_spec_sha256=source.sha256,
                        catalog_id=source.catalog_id,
                        rollout_subject="source",
                        action_ordinal=0,
                        action_kind="source-quick",
                    )
                if state == "recovery_required":
                    store.transition(
                        "production_restoring",
                        expected_id=document["id"],
                        expected_state="recovery_required",
                        expected_action_ordinal=0,
                        detail="exact source restoration started",
                    )
            restoring = store.read()
            with self.assertRaisesRegex(RecoveryError, "verification detail"):
                store.transition(
                    "failed-restored",
                    expected_id=document["id"],
                    expected_state="production_restoring",
                    expected_action_ordinal=0,
                )
            restored = store.transition(
                "failed-restored",
                expected_id=document["id"],
                expected_state="production_restoring",
                expected_action_ordinal=0,
                detail="exact source identity and health reverified",
            )
            self.assertEqual(restoring["state"], "production_restoring")
            self.assertEqual(restored["state"], "failed-restored")
            self.assertEqual(restored["rolloutActionOrdinal"], 0)
            self.assertEqual(restored["rolloutActionResults"], [])
            self.assertIn("identity", restored["history"][-1]["detail"])

    def test_shell_rollout_gate_rejects_a_missing_catalog_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TransactionStore(ProjectPaths(root))
            source, target = self.rollout_specs()
            plan = build_upgrade_rollout_plan(
                source,
                target,
                "a" * 64,
                admission_granted=True,
            )
            document = store.begin_rollout(
                "upgrade",
                target.catalog_id,
                lambda _transaction_id, _created_at: (
                    self.safe_original(),
                    self.rollout_intent(plan),
                ),
                approved_catalog_spec=target.approval_document(),
            )
            library = ROOT / "scripts" / "lib" / "deployment.sh"
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {library}; acquire_runtime_lock {root}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    **os.environ,
                    "QWEN_CONTROL_TRANSACTION_ID": document["id"],
                    "QWEN_CATALOG_ID": source.catalog_id,
                    "LOCAL_INFERENCE_ROLLOUT_SUBJECT": "source",
                    "LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL": "0",
                    "LOCAL_INFERENCE_ROLLOUT_ACTION_KIND": "source-quick",
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "missing its approved Catalog spec SHA256", result.stderr
            )
            accepted = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {library}; acquire_runtime_lock {root}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    **os.environ,
                    "QWEN_CONTROL_TRANSACTION_ID": document["id"],
                    "QWEN_CATALOG_ID": source.catalog_id,
                    "LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256": source.sha256,
                    "LOCAL_INFERENCE_ROLLOUT_SUBJECT": "source",
                    "LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL": "0",
                    "LOCAL_INFERENCE_ROLLOUT_ACTION_KIND": "source-quick",
                },
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            store.transition(
                "recovery_required",
                expected_id=document["id"],
                expected_state="planned",
                expected_action_ordinal=0,
                detail="injected rollout failure",
            )
            revoked = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {library}; acquire_runtime_lock {root}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    **os.environ,
                    "QWEN_CONTROL_TRANSACTION_ID": document["id"],
                    "QWEN_CATALOG_ID": source.catalog_id,
                    "LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256": source.sha256,
                    "LOCAL_INFERENCE_ROLLOUT_SUBJECT": "source",
                    "LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL": "0",
                    "LOCAL_INFERENCE_ROLLOUT_ACTION_KIND": "source-quick",
                },
            )
            self.assertNotEqual(revoked.returncode, 0)
            self.assertIn("recovering rollout", revoked.stderr)

    def test_rollback_requires_pointer_bound_to_the_immutable_anchor(self) -> None:
        current, anchor = self.rollout_specs()
        rollback_sha256 = "d" * 64
        plan = build_rollback_rollout_plan(
            current,
            anchor,
            rollback_sha256,
            admission_granted=True,
        )
        pointer = {
            "schemaVersion": 1,
            "kind": "local-inference-stack/rollback-pointer",
            "scopePolicy": "same-controller-same-catalog-anchor-v1",
            "generation": 1,
            "activeSpecSha256": rollback_sha256,
            "previousSpecSha256": None,
            "updatedAt": "2026-08-12T00:00:00Z",
            "updatedByTransactionId": "f6c9e79f-dbaa-4a8c-95af-dd6714f48591",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            document = store.begin_rollout(
                "rollback",
                anchor.catalog_id,
                lambda _transaction_id, _created_at: (
                    self.safe_original(healthy=True),
                    self.rollout_intent(plan, previous_pointer=pointer),
                ),
                approved_catalog_spec=anchor.approval_document(),
            )
            with store.authorized_runtime_mutation(
                document["id"],
                catalog_spec_sha256=current.sha256,
                catalog_id=current.catalog_id,
                rollout_subject="source",
                action_ordinal=0,
                action_kind="stop-source",
            ):
                pass

        pointer["activeSpecSha256"] = "e" * 64
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            with self.assertRaisesRegex(RecoveryError, "active rollback pointer"):
                store.begin_rollout(
                    "rollback",
                    anchor.catalog_id,
                    lambda _transaction_id, _created_at: (
                        self.safe_original(healthy=True),
                        self.rollout_intent(plan, previous_pointer=pointer),
                    ),
                    approved_catalog_spec=anchor.approval_document(),
                )

    def test_every_release_failure_boundary_is_recoverable_after_restart(self) -> None:
        paths_to_boundary = (
            (),
            ("production_stopping",),
            ("production_stopping", "candidate_starting"),
            ("production_stopping", "candidate_starting", "accepting"),
            (
                "production_stopping",
                "candidate_starting",
                "accepting",
                "production_restoring",
            ),
        )
        for transitions in paths_to_boundary:
            with self.subTest(boundary=transitions[-1] if transitions else "planned"):
                with tempfile.TemporaryDirectory() as directory:
                    paths = ProjectPaths(Path(directory))
                    store = TransactionStore(paths)
                    document = store.begin(
                        "release", "quick", self.safe_original(healthy=True)
                    )
                    for state in transitions:
                        store.transition(state, expected_id=document["id"])
                    restarted = TransactionStore(paths)
                    self.assertTrue(restarted.reconciliation_plan()["required"])
                    restarted.transition(
                        "recovery_required",
                        expected_id=document["id"],
                        detail="fault injected",
                    )
                    restarted.transition(
                        "production_restoring", expected_id=document["id"]
                    )
                    restarted.transition(
                        "failed-restored", expected_id=document["id"]
                    )
                    self.assertFalse(restarted.reconciliation_plan()["required"])

    def test_legacy_failed_is_read_only_and_requires_explicit_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            paths.state_dir.mkdir(parents=True)
            legacy = {
                "schemaVersion": 1,
                "id": "3f60dedc-e85e-4280-89a9-42bdaf4a71ca",
                "operation": "deploy",
                "target": "reviewed-model",
                "state": "failed",
                "createdAt": "2026-08-01T00:00:00Z",
                "updatedAt": "2026-08-01T00:01:00Z",
                "original": self.safe_original(),
                "history": [
                    {"state": "planned", "at": "2026-08-01T00:00:00Z"},
                    {"state": "failed", "at": "2026-08-01T00:01:00Z"},
                ],
            }
            paths.transaction_path.write_text(json.dumps(legacy), encoding="utf-8")
            paths.transaction_path.chmod(0o600)
            store = TransactionStore(paths)
            plan = store.reconciliation_plan()
            self.assertTrue(plan["required"])
            self.assertFalse(plan["automaticEligible"])
            self.assertEqual(plan["classification"], "legacy-failed-review-required")
            with self.assertRaisesRegex(RecoveryError, "read-only"):
                store.transition(
                    "production_restoring", expected_id=legacy["id"]
                )
            resolved = store.resolve_legacy_failed(
                "superseded-verified",
                expected_id=legacy["id"],
                detail="healthy runtime preserved",
            )
            self.assertEqual(resolved["schemaVersion"], 2)
            self.assertEqual(resolved["state"], "superseded-verified")
            self.assertEqual(
                json.loads(
                    store.archive_path(legacy["id"]).read_text(encoding="utf-8")
                ),
                resolved,
            )

    def test_transaction_begin_refuses_a_held_runtime_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            lock = paths.root / "cache" / "locks" / "runtime.lock"
            lock.parent.mkdir(parents=True)
            descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RecoveryError, "runtime mutation"):
                    TransactionStore(paths).begin(
                        "profile", "throughput", self.safe_original(healthy=True)
                    )
            finally:
                os.close(descriptor)

    def test_runtime_mutation_requires_matching_active_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = TransactionStore(paths)
            document = store.begin(
                "profile", "throughput", self.safe_original(healthy=True)
            )
            with self.assertRaisesRegex(RecoveryError, "active control transaction"):
                with store.authorized_runtime_mutation():
                    self.fail("an unrelated mutation crossed the transaction gate")
            with store.authorized_runtime_mutation(document["id"]):
                self.assertEqual(store.read()["id"], document["id"])
            with self.assertRaisesRegex(RecoveryError, "canonical UUID"):
                with store.authorized_runtime_mutation("not-a-uuid"):
                    self.fail("an invalid transaction id crossed the gate")

    def test_active_deploy_cannot_omit_persisted_catalog_spec_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            model = json.loads(
                (ROOT / "catalog" / "models.json").read_text(encoding="utf-8")
            )["models"][0]
            spec = CatalogDeploymentSpec.from_catalog_model(model)
            store = TransactionStore(paths)
            document = store.begin(
                "deploy",
                spec.catalog_id,
                self.safe_original(healthy=True),
                approved_catalog_spec=spec.approval_document(),
            )
            with self.assertRaisesRegex(RecoveryError, "supplied together"):
                with store.authorized_runtime_mutation(document["id"]):
                    self.fail("an unbound deploy mutation crossed the transaction gate")
            with store.authorized_runtime_mutation(
                document["id"],
                catalog_spec_sha256=spec.sha256,
                catalog_id=spec.catalog_id,
            ):
                self.assertEqual(store.read()["approvedCatalogSpecSha256"], spec.sha256)

    def test_live_transaction_owner_cannot_be_fenced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            document = store.begin("deploy", "model", self.safe_original())
            status = store.initiator_status(document)
            self.assertEqual(status["status"], "alive")
            self.assertFalse(status["fenceEligible"])
            with self.assertRaisesRegex(RecoveryError, "proven dead"):
                store.fence_orphaned(expected_id=document["id"])

    def test_exited_initiator_can_be_fenced_after_runtime_lock_is_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from local_inference_stack.paths import ProjectPaths
from local_inference_stack.transactions import TransactionStore
TransactionStore(ProjectPaths(Path(sys.argv[2]))).begin("deploy", "model", {})
"""
            subprocess.run(
                [sys.executable, "-c", child, str(ROOT / "src"), directory],
                check=True,
                cwd=ROOT,
            )
            store = TransactionStore(ProjectPaths(Path(directory)))
            document = store.read()
            self.assertEqual(store.initiator_status(document)["status"], "dead")
            runtime_lock = Path(directory) / "cache" / "locks" / "runtime.lock"
            descriptor = os.open(runtime_lock, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RecoveryError, "runtime mutation"):
                    store.fence_orphaned(expected_id=document["id"])
            finally:
                os.close(descriptor)
            fenced = store.fence_orphaned(expected_id=document["id"])
            self.assertEqual(fenced["state"], "recovery_required")
            self.assertEqual(
                fenced["history"][-1]["fence"]["policy"],
                "dead-process-and-free-runtime-lock-v1",
            )

    def test_runtime_mutation_gate_refuses_a_held_runtime_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            lock = paths.root / "cache" / "locks" / "runtime.lock"
            lock.parent.mkdir(parents=True)
            descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RecoveryError, "runtime mutation"):
                    with TransactionStore(paths).authorized_runtime_mutation():
                        self.fail("a contending mutation crossed the runtime lock")
            finally:
                os.close(descriptor)

    def test_transition_compare_and_swap_rejects_a_stale_transaction_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            first = store.begin("deploy", "first", self.safe_original())
            store.transition("deploying", expected_id=first["id"])
            store.transition("completed", expected_id=first["id"])
            second = store.begin("deploy", "second", self.safe_original())
            with self.assertRaisesRegex(RecoveryError, "identity changed"):
                store.transition("deploying", expected_id=first["id"])
            self.assertEqual(store.read()["id"], second["id"])
            self.assertEqual(store.read()["state"], "planned")

    def test_terminal_transactions_are_privately_archived_before_slot_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = TransactionStore(paths)
            first = store.begin("profile", "latency", self.safe_original())
            store.transition("deploying", expected_id=first["id"])
            completed = store.transition("completed", expected_id=first["id"])
            archive = store.archive_path(first["id"])

            self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), completed)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(archive.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(store.archive_current_terminal(), archive)

            second = store.begin("profile", "throughput", self.safe_original())
            self.assertEqual(store.read()["id"], second["id"])
            self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), completed)

    def test_terminal_archive_repairs_an_exact_interrupted_publish_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = TransactionStore(paths)
            document = store.begin("profile", "latency", self.safe_original())
            store.transition("deploying", expected_id=document["id"])
            completed = store.transition("completed", expected_id=document["id"])
            archive = store.archive_path(document["id"])
            interrupted = archive.parent / f".{archive.name}.{'b' * 32}.tmp"
            os.link(archive, interrupted)
            self.assertEqual(archive.stat().st_nlink, 2)

            self.assertEqual(store.archive_current_terminal(), archive)

            self.assertFalse(interrupted.exists())
            self.assertEqual(archive.stat().st_nlink, 1)
            self.assertEqual(json.loads(archive.read_text(encoding="utf-8")), completed)

    def test_terminal_archive_symlink_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = TransactionStore(paths)
            document = store.begin("profile", "latency", self.safe_original())
            store.transition("deploying", expected_id=document["id"])
            store.archive_dir.mkdir(mode=0o700)
            target = paths.root / "archive-victim"
            target.write_text("unchanged", encoding="utf-8")
            target.chmod(0o600)
            store.archive_path(document["id"]).symlink_to(target)

            with self.assertRaisesRegex(RecoveryError, "archive"):
                store.transition("completed", expected_id=document["id"])
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(store.read()["state"], "completed")

    def test_terminal_archive_never_overwrites_a_late_competing_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = TransactionStore(paths)
            document = store.begin("profile", "latency", self.safe_original())
            store.transition("deploying", expected_id=document["id"])
            competitor = copy.deepcopy(store.read())
            archive = store.archive_path(document["id"])

            def publish_competitor(*_args: Any, **_kwargs: Any) -> None:
                archive.write_text(
                    json.dumps(competitor, sort_keys=True, separators=(",", ":"))
                    + "\n",
                    encoding="utf-8",
                )
                archive.chmod(0o600)
                raise FileExistsError(archive)

            with (
                patch.object(os, "link", side_effect=publish_competitor),
                self.assertRaisesRegex(RecoveryError, "conflicts"),
            ):
                store.transition("completed", expected_id=document["id"])

            self.assertEqual(
                json.loads(archive.read_text(encoding="utf-8")), competitor
            )
            self.assertEqual(store.read()["state"], "completed")

    def test_terminal_archive_publish_is_bound_to_the_verified_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = TransactionStore(paths)
            document = store.begin("profile", "latency", self.safe_original())
            store.transition("deploying", expected_id=document["id"])
            outside = paths.root / "outside"
            outside.mkdir(mode=0o700)
            moved = paths.state_dir / "transactions-moved"
            real_link = os.link

            def swap_parent_then_link(*args: Any, **kwargs: Any) -> None:
                real_link(*args, **kwargs)
                store.archive_dir.rename(moved)
                store.archive_dir.symlink_to(outside, target_is_directory=True)

            with (
                patch.object(os, "link", side_effect=swap_parent_then_link),
                self.assertRaisesRegex(RecoveryError, "directory changed"),
            ):
                store.transition("completed", expected_id=document["id"])

            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(store.read()["state"], "completed")
            archived = moved / store.archive_path(document["id"]).name
            self.assertTrue(archived.is_file())
            self.assertEqual(archived.stat().st_mode & 0o777, 0o600)

    def test_terminal_archive_rejects_a_hard_link_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = TransactionStore(paths)
            document = store.begin("profile", "latency", self.safe_original())
            store.transition("deploying", expected_id=document["id"])
            store.archive_dir.mkdir(mode=0o700)
            target = paths.root / "archive-victim"
            target.write_text("unchanged", encoding="utf-8")
            target.chmod(0o600)
            os.link(target, store.archive_path(document["id"]))

            with self.assertRaisesRegex(RecoveryError, "archive"):
                store.transition("completed", expected_id=document["id"])
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(target.stat().st_nlink, 2)
            self.assertEqual(store.read()["state"], "completed")

    def test_terminal_slot_survives_archive_publish_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            document = store.begin("profile", "latency", self.safe_original())
            store.transition("deploying", expected_id=document["id"])
            with (
                patch.object(
                    store,
                    "_archive_terminal",
                    side_effect=RecoveryError("injected archive failure"),
                ),
                self.assertRaisesRegex(RecoveryError, "injected archive failure"),
            ):
                store.transition("completed", expected_id=document["id"])
            self.assertEqual(store.read()["state"], "completed")

    def test_legacy_resolution_slot_survives_archive_publish_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            paths.state_dir.mkdir(parents=True)
            legacy = {
                "schemaVersion": 1,
                "id": "3f60dedc-e85e-4280-89a9-42bdaf4a71ca",
                "operation": "deploy",
                "target": "reviewed-model",
                "state": "failed",
                "createdAt": "2026-08-01T00:00:00Z",
                "updatedAt": "2026-08-01T00:01:00Z",
                "original": self.safe_original(),
                "history": [
                    {"state": "planned", "at": "2026-08-01T00:00:00Z"},
                    {"state": "failed", "at": "2026-08-01T00:01:00Z"},
                ],
            }
            paths.transaction_path.write_text(json.dumps(legacy), encoding="utf-8")
            paths.transaction_path.chmod(0o600)
            store = TransactionStore(paths)
            with (
                patch.object(
                    store,
                    "_archive_terminal",
                    side_effect=RecoveryError("injected archive failure"),
                ),
                self.assertRaisesRegex(RecoveryError, "injected archive failure"),
            ):
                store.resolve_legacy_failed(
                    "superseded-verified",
                    expected_id=legacy["id"],
                    detail="healthy runtime preserved",
                )
            current = store.read()
            self.assertEqual(current["schemaVersion"], 2)
            self.assertEqual(current["state"], "superseded-verified")

    def test_healthy_original_requires_complete_runtime_identity(self) -> None:
        original = self.safe_original(healthy=True)
        original["runtimeIdentity"] = None
        self.assertFalse(recovery_original_is_safe(original))
        original = self.safe_original(healthy=True)
        original["containerHealthy"] = False
        self.assertFalse(recovery_original_is_safe(original))

    def test_runtime_lock_symlink_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            locks = paths.root / "cache" / "locks"
            locks.mkdir(parents=True)
            target = paths.root / "victim"
            target.write_text("unchanged", encoding="utf-8")
            target.chmod(0o600)
            (locks / "runtime.lock").symlink_to(target)
            with self.assertRaisesRegex(RecoveryError, "private lock"):
                TransactionStore(paths).begin(
                    "deploy", "reviewed-model", self.safe_original()
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_recovery_profile_is_structured_and_never_restores_raw_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            values = {key: "safe" for key in RECOVERY_DEPLOYMENT_KEYS}
            marker = paths.root / "should-not-run"
            values["QWEN_MODEL_DISPLAY_NAME"] = f"$(touch {marker})"
            canonical = json.dumps(
                values,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            record = {
                "present": True,
                "format": "allowlisted-env-v1",
                "values": values,
                "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                "containsCredentials": False,
            }
            cli._restore_deployment_profile(paths, {"deploymentProfile": record})
            restored = (
                paths.root / "profiles" / "deployment.local.env"
            ).read_text(encoding="utf-8")
            self.assertIn("QWEN_MODEL_DISPLAY_NAME='$(touch ", restored)
            subprocess.run(
                ["bash", "-c", f"source {paths.root / 'profiles/deployment.local.env'}"],
                check=True,
                timeout=10,
            )
            self.assertFalse(marker.exists())
            legacy = {
                "healthy": False,
                "profile": "unknown",
                "containerName": "qwen",
                "runtimeIdentity": None,
                "deploymentProfile": {
                    "present": True,
                    "content": "QWEN_MODEL_FILE=$(touch bad)",
                    "sha256": "0" * 64,
                    "containsCredentials": False,
                },
                "capturedWithoutSecrets": True,
            }
            self.assertFalse(recovery_original_is_safe(legacy))


class TransactionCliTests(unittest.TestCase):
    @staticmethod
    def safe_original(*, healthy: bool = False) -> dict[str, object]:
        return TransactionTests.safe_original(healthy=healthy)

    def test_deploy_command_failure_enters_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            model = json.loads(
                (ROOT / "catalog" / "models.json").read_text(encoding="utf-8")
            )["models"][0]
            spec = CatalogDeploymentSpec.from_catalog_model(model)
            payload = {
                "readyToDeploy": True,
                "recommendation": model,
                "actionPlan": build_deployment_plan(
                    spec,
                    admission_granted=True,
                ).document(),
            }
            with (
                patch.object(cli, "_legacy_plan", return_value=payload),
                patch.object(
                    cli,
                    "load_approved_catalog_spec",
                    return_value=(spec, None),
                ),
                patch.object(
                    cli,
                    "_current_catalog_deployment_admission",
                    return_value={"readyToDeploy": True},
                ),
                patch.object(cli, "_original_runtime", return_value=self.safe_original()),
                patch.object(cli, "run", side_effect=ExternalError("injected start failure")) as runner,
                self.assertRaisesRegex(ExternalError, "injected"),
            ):
                cli._deploy(paths, Namespace(yes=True, model=spec.catalog_id))
            transaction = TransactionStore(paths).read()
            self.assertEqual(transaction["schemaVersion"], 2)
            self.assertEqual(transaction["state"], "recovery_required")
            self.assertEqual(transaction["approvedCatalogSpecSha256"], spec.sha256)
            self.assertEqual(
                transaction["approvedCatalogSpec"]["reviewedModelSha256"],
                spec.reviewed_model_sha256,
            )
            self.assertEqual(
                runner.call_args.kwargs["env"]["QWEN_CONTROL_TRANSACTION_ID"],
                transaction["id"],
            )
            self.assertEqual(
                runner.call_args.kwargs["env"][
                    "LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256"
                ],
                spec.sha256,
            )
            self.assertIn("--artifact-sha256", runner.call_args.args[0])
            self.assertIn(spec.artifacts[0].sha256, runner.call_args.args[0])

    def test_self_consistent_forged_planner_identity_fails_before_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            catalog = json.loads(
                (ROOT / "catalog" / "models.json").read_text(encoding="utf-8")
            )
            (paths.root / "catalog").mkdir(parents=True)
            (paths.root / "catalog" / "models.json").write_text(
                json.dumps(catalog), encoding="utf-8"
            )
            forged = json.loads(json.dumps(catalog["models"][0]))
            forged["purpose"] = "self-consistent forged planner record"
            forged_spec = CatalogDeploymentSpec.from_catalog_model(forged)
            payload = {
                "recommendation": forged,
                "actionPlan": build_deployment_plan(
                    forged_spec,
                    admission_granted=True,
                ).document(),
            }
            with (
                patch.object(cli, "_legacy_plan", return_value=payload),
                patch.object(cli, "_current_catalog_deployment_admission") as admission,
                patch.object(cli, "_original_runtime") as original,
                self.assertRaisesRegex(ConfigError, "current Catalog deployment"),
            ):
                cli._deploy(paths, Namespace(yes=True, model=forged_spec.catalog_id))
            admission.assert_not_called()
            original.assert_not_called()
            self.assertFalse(paths.transaction_path.exists())

    def test_planner_cannot_substitute_a_different_explicit_catalog_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            model = json.loads(
                (ROOT / "catalog" / "models.json").read_text(encoding="utf-8")
            )["models"][0]
            substituted = json.loads(json.dumps(model))
            substituted["id"] = "substituted-model"
            spec = CatalogDeploymentSpec.from_catalog_model(substituted)
            payload = {
                "recommendation": substituted,
                "actionPlan": build_deployment_plan(
                    spec, admission_granted=True
                ).document(),
            }
            with (
                patch.object(cli, "_legacy_plan", return_value=payload),
                patch.object(cli, "_current_catalog_deployment_admission") as admission,
                patch.object(cli, "_original_runtime") as original,
                patch.object(cli, "run") as runner,
                patch.object(TransactionStore, "begin") as begin,
                self.assertRaisesRegex(ConfigError, "explicitly approved --model"),
            ):
                cli._deploy(paths, Namespace(yes=True, model=model["id"]))
            admission.assert_not_called()
            original.assert_not_called()
            runner.assert_not_called()
            begin.assert_not_called()

    def test_profile_command_failure_enters_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            with (
                patch.object(cli, "_original_runtime", return_value=self.safe_original(healthy=True)),
                patch.object(cli, "run", side_effect=ExternalError("injected profile failure")),
                self.assertRaisesRegex(ExternalError, "injected"),
            ):
                cli._profile(paths, Namespace(yes=True, name="throughput"))
            self.assertEqual(TransactionStore(paths).read()["state"], "recovery_required")

    def test_release_rejects_ineligible_catalog_before_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            with (
                patch.dict(os.environ, {"MODELPORT_PROJECT_DIR": "/reviewed/modelport"}),
                patch.object(
                    cli,
                    "_release_catalog_admission",
                    side_effect=cli.AdmissionError("catalog entry is provisional"),
                ),
                patch.object(cli, "_original_runtime") as original,
                self.assertRaisesRegex(cli.AdmissionError, "provisional"),
            ):
                cli._release(paths, Namespace(yes=True, mode="quick"))
            original.assert_not_called()
            self.assertFalse(paths.transaction_path.exists())

    def test_release_catalog_admission_ignores_busy_runtime_but_not_catalog_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.runtime_identity.deployment_values",
            return_value={"QWEN_CATALOG_ID": "provisional-model"},
        ), patch.object(
            cli,
            "_legacy_plan",
            return_value={
                "catalogDeploymentEligible": False,
                "readyToDeploy": False,
                "resourceAvailableNow": False,
                "catalogEvidenceStatus": "provisional-profile",
                "caveats": ["catalog entry is frozen"],
            },
        ):
            with self.assertRaisesRegex(cli.AdmissionError, "not deployment-eligible"):
                cli._release_catalog_admission(ProjectPaths(Path(directory)))

    def test_release_command_failure_enters_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            with (
                patch.dict(os.environ, {"MODELPORT_PROJECT_DIR": "/reviewed/modelport"}),
                patch.object(cli, "_release_catalog_admission", return_value={}),
                patch.object(cli, "_original_runtime", return_value=self.safe_original(healthy=True)),
                patch.object(cli, "run", side_effect=ExternalError("injected candidate failure")) as runner,
                self.assertRaisesRegex(ExternalError, "injected"),
            ):
                cli._release(paths, Namespace(yes=True, mode="quick"))
            transaction = TransactionStore(paths).read()
            self.assertEqual(transaction["state"], "recovery_required")
            self.assertEqual(
                runner.call_args.kwargs["env"]["QWEN_CONTROL_TRANSACTION_ID"],
                transaction["id"],
            )

    def test_reconcile_failure_remains_recovery_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = TransactionStore(paths)
            document = store.begin("deploy", "reviewed-model", self.safe_original())
            store.transition(
                "recovery_required",
                expected_id=document["id"],
                detail="injected deployment failure",
            )
            plan = store.reconciliation_plan()
            plan.update(
                {
                    "runtimeDisposition": "restoration-required",
                    "originalSafeToRestore": True,
                }
            )
            with (
                patch.object(cli, "_reconciliation_runtime_plan", return_value=plan),
                patch.object(cli, "_restore_deployment_profile"),
                patch.object(cli, "run", side_effect=ExternalError("injected recovery failure")),
                self.assertRaisesRegex(ExternalError, "injected"),
            ):
                cli._reconcile(paths, Namespace(yes=True))
            recovered = store.read()
            self.assertEqual(recovered["id"], document["id"])
            self.assertEqual(recovered["state"], "recovery_required")

    def test_healthy_failed_replacement_must_restore_the_v2_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            original = self.safe_original(healthy=True)
            store = TransactionStore(paths)
            document = store.begin("deploy", "replacement-model", original)
            store.transition(
                "recovery_required",
                expected_id=document["id"],
                detail="quick acceptance failed",
            )
            current = self.safe_original(healthy=True)
            current["containerName"] = "replacement-model"
            current["runtimeIdentity"] = {
                "sha256": "b" * 64,
                "configuration": {"profile": "latency"},
            }
            with (
                patch.object(cli, "_original_runtime", return_value=current),
                patch.object(
                    cli,
                    "_verify_restored_runtime",
                    side_effect=RecoveryError("identity differs"),
                ),
            ):
                plan = cli._reconciliation_runtime_plan(
                    paths, store.reconciliation_plan()
                )
            self.assertEqual(plan["runtimeDisposition"], "restoration-required")
            self.assertTrue(plan["automaticEligible"])

    def test_v2_restore_passes_the_failed_container_to_the_hardened_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            original = self.safe_original(healthy=True)
            store = TransactionStore(paths)
            document = store.begin("deploy", "replacement-model", original)
            store.transition(
                "recovery_required",
                expected_id=document["id"],
                detail="quick acceptance failed",
            )
            plan = store.reconciliation_plan()
            plan.update(
                {
                    "runtimeDisposition": "restoration-required",
                    "originalSafeToRestore": True,
                }
            )
            current = self.safe_original(healthy=True)
            current["containerName"] = "replacement-model"
            completed = RunResult(("runtime-reconcile",), 0, "restored", "")
            with (
                patch.object(cli, "_reconciliation_runtime_plan", return_value=plan),
                patch.object(cli, "_original_runtime", return_value=current),
                patch.object(cli, "_restore_deployment_profile"),
                patch.object(cli, "_verify_restored_runtime"),
                patch.object(cli, "run", return_value=completed) as runner,
            ):
                result = cli._reconcile(paths, Namespace(yes=True))
            self.assertEqual(result.facts["transaction"]["state"], "failed-restored")
            self.assertEqual(
                runner.call_args.kwargs["env"]["QWEN_FAILED_CONTAINER_NAME"],
                "replacement-model",
            )

    def test_delayed_failure_from_old_transaction_cannot_poison_a_new_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            first = store.begin("deploy", "first", self.safe_original())
            store.transition("deploying", expected_id=first["id"])
            store.transition("completed", expected_id=first["id"])
            second = store.begin("deploy", "second", self.safe_original())
            cli._mark_recovery_required(store, first["id"], RuntimeError("late"))
            current = store.read()
            self.assertEqual(current["id"], second["id"])
            self.assertEqual(current["state"], "planned")

    def test_reconcile_cannot_fence_a_still_active_v2_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = TransactionStore(paths)
            document = store.begin("deploy", "reviewed-model", self.safe_original())
            plan = store.reconciliation_plan()
            plan.update(
                {
                    "runtimeDisposition": "original-runtime-already-restored",
                    "originalSafeToRestore": True,
                }
            )
            with (
                patch.object(cli, "_reconciliation_runtime_plan", return_value=plan),
                self.assertRaisesRegex(RecoveryError, "proven dead"),
            ):
                cli._reconcile(paths, Namespace(yes=True))
            current = store.read()
            self.assertEqual(current["id"], document["id"])
            self.assertEqual(current["state"], "planned")

    def test_reconcile_fences_a_proven_dead_v2_initiator_before_classification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from local_inference_stack.paths import ProjectPaths
from local_inference_stack.transactions import TransactionStore
TransactionStore(ProjectPaths(Path(sys.argv[2]))).begin("deploy", "model", {})
"""
            subprocess.run(
                [sys.executable, "-c", child, str(ROOT / "src"), directory],
                check=True,
                cwd=ROOT,
            )
            paths = ProjectPaths(Path(directory))

            def classify(_paths, plan):
                self.assertEqual(plan["transaction"]["state"], "recovery_required")
                return {
                    **plan,
                    "runtimeDisposition": "unsafe-original-review-required",
                    "originalSafeToRestore": False,
                }

            with (
                patch.object(
                    cli, "_reconciliation_runtime_plan", side_effect=classify
                ),
                self.assertRaisesRegex(RecoveryError, "cannot mutate runtime"),
            ):
                cli._reconcile(paths, Namespace(yes=True))
            self.assertEqual(
                TransactionStore(paths).read()["state"], "recovery_required"
            )

    def test_legacy_failed_read_only_plan_preserves_healthy_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            paths.state_dir.mkdir(parents=True)
            legacy = {
                "schemaVersion": 1,
                "id": "e93df88a-09df-4fd3-b18b-b7fce8662c38",
                "operation": "deploy",
                "target": "reviewed-model",
                "state": "failed",
                "createdAt": "2026-08-01T00:00:00Z",
                "updatedAt": "2026-08-01T00:01:00Z",
                "original": self.safe_original(),
                "history": [{"state": "failed", "at": "2026-08-01T00:01:00Z"}],
            }
            paths.transaction_path.write_text(json.dumps(legacy), encoding="utf-8")
            paths.transaction_path.chmod(0o600)
            current = self.safe_original(healthy=True)
            with patch.object(cli, "_original_runtime", return_value=current):
                result = cli._reconcile(paths, Namespace(yes=False))
            self.assertEqual(result.code, 7)
            self.assertEqual(
                result.facts["runtimeDisposition"],
                "legacy-failure-healthy-runtime-preserved",
            )
            self.assertIn("without replacing", result.nextActions[0].description)
            self.assertEqual(TransactionStore(paths).read()["schemaVersion"], 1)

            with (
                patch.object(cli, "_original_runtime", return_value=current),
                patch.object(cli, "_verify_current_runtime_canonical") as verify,
            ):
                resolved = cli._reconcile(paths, Namespace(yes=True))
            verify.assert_called_once()
            self.assertEqual(
                resolved.facts["transaction"]["state"], "superseded-verified"
            )

    def test_legacy_resolution_holds_runtime_boundary_through_final_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            paths.state_dir.mkdir(parents=True)
            legacy = {
                "schemaVersion": 1,
                "id": "b42825a3-1ce4-4be7-8d2a-c2a4af2b2653",
                "operation": "deploy",
                "target": "reviewed-model",
                "state": "failed",
                "createdAt": "2026-08-01T00:00:00Z",
                "updatedAt": "2026-08-01T00:01:00Z",
                "original": self.safe_original(),
                "history": [{"state": "failed", "at": "2026-08-01T00:01:00Z"}],
            }
            paths.transaction_path.write_text(json.dumps(legacy), encoding="utf-8")
            paths.transaction_path.chmod(0o600)
            plan = TransactionStore(paths).reconciliation_plan()
            plan.update(
                {
                    "runtimeDisposition": "legacy-failure-healthy-runtime-preserved",
                    "originalSafeToRestore": False,
                }
            )
            events: list[str] = []
            current = self.safe_original(healthy=True)

            @contextmanager
            def boundary(_store):
                events.append("runtime-enter")
                try:
                    yield
                finally:
                    events.append("runtime-exit")

            def observe(_paths):
                self.assertEqual(events, ["runtime-enter"])
                events.append("observe")
                return current

            def verify(_paths, observed):
                self.assertIs(observed, current)
                self.assertEqual(events, ["runtime-enter", "observe"])
                events.append("verify")

            with (
                patch.object(cli, "_reconciliation_runtime_plan", return_value=plan),
                patch.object(cli.TransactionStore, "runtime_boundary", boundary),
                patch.object(cli, "_original_runtime", side_effect=observe),
                patch.object(cli, "_verify_current_runtime_canonical", side_effect=verify),
            ):
                result = cli._reconcile(paths, Namespace(yes=True))

            self.assertEqual(
                events, ["runtime-enter", "observe", "verify", "runtime-exit"]
            )
            self.assertEqual(
                result.facts["transaction"]["state"], "superseded-verified"
            )


class AttestationTests(unittest.TestCase):
    NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)

    def _document(self, *, eligible: bool = True) -> dict:
        checks = {key: True for key in attestation.ELIGIBILITY_CHECKS}
        if not eligible:
            checks["fullMode"] = False
        payload = {
            "schemaVersion": attestation.SCHEMA_VERSION,
            "kind": attestation.KIND,
            "project": {"revision": "a" * 40, "dirty": False},
            "eligibility": {
                "policy": attestation.ELIGIBILITY_POLICY,
                "eligible": eligible,
                "evaluatedAt": "2026-08-09T10:00:00Z",
                "requiredEvidence": {
                    "schemaVersion": 4,
                    "mode": "full",
                    "profile": "latency",
                    "maxAgeDays": 30,
                    "runnerManifestSchemaVersion": 1,
                    "validationInputPolicy": attestation.VALIDATION_INPUT_POLICY,
                },
                "checks": checks,
                "reasonCodes": [] if eligible else ["fullMode"],
            },
            "acceptance": {
                "sourcePath": "logs/acceptance/test-full.json",
                "sourceSha256": "b" * 64,
                "schemaVersion": 4,
                "selfSha256": "c" * 64,
                "configurationSha256": "d" * 64,
                "validationInputSha256": "e" * 64,
                "runManifestSha256": "f" * 64,
                "stepResultsSha256": attestation.sha256_document([]),
                "modelId": "qwen35-9b-q5km",
                "status": "passed",
                "mode": "full" if eligible else "quick",
                "profile": "latency",
                "terminalStep": attestation.FULL_TERMINAL_STEP,
            },
            "hardware": {
                "architecture": "x86_64",
                "environmentKind": "wsl2",
                "platform": "linux",
                "ramGiB": 96,
                "gpus": [],
            },
            "artifact": {"sha256": "2" * 64},
            "runtime": {
                "configuredImage": "image@sha256:" + "3" * 64,
                "imageId": "sha256:" + "4" * 64,
                "containerConfigSha256": "5" * 64,
            },
            "trust": {
                "policy": attestation.TRUST_POLICY,
                "artifactIdentity": "catalog-sha256-plus-current-secure-stat-stamp",
                "runtimeIdentity": "exact-rendered-compose-and-live-container",
                "publisherMetadata": "catalog-metadata-not-cryptographically-verified",
                "signature": {
                    "requiredForPromotion": True,
                    "trustModel": attestation.SIGNATURE_TRUST_MODEL,
                    "allowedTools": list(attestation.ALLOWED_SIGNATURE_TOOLS),
                    "keyFingerprint": {
                        "algorithm": "sha256",
                        "expectedValueRequired": True,
                        "source": "external-trust-anchor",
                    },
                },
            },
            "lifecycle": {
                "validity": {
                    "notBefore": "2026-08-01T00:00:00Z",
                    "expiresAt": "2026-08-31T00:00:00Z",
                },
                "revocation": {"revokedAt": None, "reason": None},
                "supersession": {"supersedes": [], "supersededBy": None},
            },
        }
        subject = {
            "schemaVersion": attestation.SUBJECT_SCHEMA_VERSION,
            "policy": attestation.SUBJECT_POLICY,
            "project": payload["project"],
            "acceptance": payload["acceptance"],
            "validationInput": {
                "policy": attestation.VALIDATION_INPUT_POLICY,
                "catalogSha256": "6" * 64,
                "repositorySha256": "7" * 64,
                "sha256": "e" * 64,
            },
            "hardware": payload["hardware"],
            "artifact": payload["artifact"],
            "runtime": payload["runtime"],
            "run": {
                "manifest": {"sourceSha256": "f" * 64},
                "stepResults": [],
            },
            "lifecycle": payload["lifecycle"],
        }
        payload["evidenceSubject"] = subject
        payload["evidenceSubjectSha256"] = attestation.sha256_document(subject)
        digest = hashlib.sha256(attestation.canonical_bytes(payload)).hexdigest()
        return {
            "schemaVersion": attestation.SCHEMA_VERSION,
            "payload": payload,
            "payloadSha256": digest,
            "signature": None,
            "validationStatus": (
                "draft-eligible-unsigned" if eligible else "draft-ineligible"
            ),
        }

    def _document_with_retained_evidence(
        self, root: Path
    ) -> tuple[dict, Path]:
        source = root / "logs" / "acceptance" / "test-full.json"
        source.parent.mkdir(parents=True)
        finished_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        started_at = finished_at - timedelta(minutes=1)
        evidence = {
            "schemaVersion": 4,
            "gitCommit": "a" * 40,
            "gitState": "clean",
            "catalogModelId": "qwen35-9b-q5km",
            "mode": "full",
            "profile": "latency",
            "status": "passed",
            "exitCode": 0,
            "failedAtStep": None,
            "terminalStep": attestation.FULL_TERMINAL_STEP,
            "startedAt": started_at.isoformat().replace("+00:00", "Z"),
            "finishedAt": finished_at.isoformat().replace("+00:00", "Z"),
            "durationSeconds": 60,
            "configuration": {"acceptanceSuiteSha256": "1" * 64},
            "validationInput": {
                "policy": attestation.VALIDATION_INPUT_POLICY,
                "catalogSha256": "6" * 64,
                "repositorySha256": "7" * 64,
                "sha256": "e" * 64,
            },
            "host": {
                "architecture": "x86_64",
                "environmentKind": "wsl2",
                "platform": "linux",
                "ramGiB": 96,
                "gpus": [],
            },
            "artifact": {"sha256": "2" * 64},
            "runtime": {
                "configuredImage": "image@sha256:" + "3" * 64,
                "imageId": "sha256:" + "4" * 64,
                "containerConfigSha256": "5" * 64,
            },
            "run": {
                "manifest": {"sourceSha256": "f" * 64},
                "stepResults": [],
            },
        }
        evidence["selfSha256"] = attestation._evidence_self_hash(evidence)
        source.write_text(json.dumps(evidence), encoding="utf-8")
        source.chmod(0o600)
        subject = attestation._evidence_subject(
            ProjectPaths(root), source, evidence
        )
        document = self._document()
        for key in (
            "project",
            "acceptance",
            "hardware",
            "artifact",
            "runtime",
            "lifecycle",
        ):
            document["payload"][key] = subject[key]
        document["payload"]["evidenceSubject"] = subject
        document["payload"]["evidenceSubjectSha256"] = (
            attestation.sha256_document(subject)
        )
        document["payloadSha256"] = attestation.sha256_document(
            document["payload"]
        )
        return document, source

    def test_hash_and_clean_tree_are_required(self) -> None:
        document = self._document()
        verified = attestation.verify_document(document, now=self.NOW)
        self.assertTrue(verified["cleanTree"])
        document["payloadSha256"] = "0" * 64
        with self.assertRaises(IntegrityError):
            attestation.verify_document(document, now=self.NOW)

    def test_signature_policy_is_separate_from_self_hash(self) -> None:
        document = self._document()
        with self.assertRaisesRegex(IntegrityError, "signature"):
            attestation.verify_document(
                document, require_signature=True, now=self.NOW
            )

    def test_cli_never_silently_ignores_detached_signature_arguments(self) -> None:
        args = cli.parser().parse_args(
            [
                "attest",
                "verify",
                "draft.json",
                "--tool",
                "minisign",
                "--public-key",
                "trusted.pub",
                "--signature",
                "draft.minisig",
            ]
        )
        with self.assertRaisesRegex(UsageError, "require-signature.*for-promotion"):
            cli.dispatch(ProjectPaths(ROOT), args)

    def test_trusted_key_fingerprint_is_reserved_for_promotion_enforcement(self) -> None:
        args = cli.parser().parse_args(
            [
                "attest",
                "verify",
                "draft.json",
                "--require-signature",
                "--tool",
                "minisign",
                "--public-key",
                "trusted.pub",
                "--signature",
                "draft.minisig",
                "--trusted-key-sha256",
                "a" * 64,
            ]
        )
        with self.assertRaisesRegex(UsageError, "only with --for-promotion"):
            cli.dispatch(ProjectPaths(ROOT), args)

    def test_eligible_flag_cannot_upgrade_quick_or_incomplete_evidence(self) -> None:
        document = self._document()
        document["payload"]["acceptance"]["mode"] = "quick"
        document["payload"]["evidenceSubjectSha256"] = attestation.sha256_document(
            document["payload"]["evidenceSubject"]
        )
        document["payloadSha256"] = hashlib.sha256(
            attestation.canonical_bytes(document["payload"])
        ).hexdigest()
        with self.assertRaisesRegex(IntegrityError, "complete full"):
            attestation.verify_document(document, now=self.NOW)

    def test_signature_metadata_binds_payload_and_trust_model(self) -> None:
        document = self._document()
        document["signature"] = {
            "tool": "minisign",
            "detachedFile": "test.minisig",
            "sha256": "e" * 64,
        }
        document["validationStatus"] = "signed-unverified"
        with self.assertRaisesRegex(IntegrityError, "signature metadata"):
            attestation.verify_document(
                document, require_signature=True, now=self.NOW
            )
        document["signature"] = {
            "schemaVersion": 1,
            "tool": "minisign",
            "detachedFile": "test.minisig",
            "sha256": "e" * 64,
            "payloadSha256": document["payloadSha256"],
            "signedAt": "2026-08-09T11:00:00Z",
            "trustModel": attestation.SIGNATURE_TRUST_MODEL,
        }
        facts = attestation.verify_document(
            document, require_signature=True, now=self.NOW
        )
        self.assertTrue(facts["signature"]["metadataValid"])
        self.assertFalse(facts["signature"]["cryptographicallyVerified"])
        self.assertFalse(facts["promotionEligible"])

    def test_expiry_revocation_and_supersession_block_promotion(self) -> None:
        for field in ("expired", "revoked", "superseded"):
            with self.subTest(field=field):
                document = self._document()
                if field == "expired":
                    document["payload"]["lifecycle"]["validity"] = {
                        "notBefore": "2026-06-01T00:00:00Z",
                        "expiresAt": "2026-07-01T00:00:00Z",
                    }
                elif field == "revoked":
                    document["payload"]["lifecycle"]["revocation"] = {
                        "revokedAt": "2026-08-08T00:00:00Z",
                        "reason": "key compromise",
                    }
                else:
                    document["payload"]["lifecycle"]["supersession"][
                        "supersededBy"
                    ] = "f" * 64
                document["payloadSha256"] = hashlib.sha256(
                    attestation.canonical_bytes(document["payload"])
                ).hexdigest()
                document["payload"][
                    "evidenceSubjectSha256"
                ] = attestation.sha256_document(
                    document["payload"]["evidenceSubject"]
                )
                document["payloadSha256"] = hashlib.sha256(
                    attestation.canonical_bytes(document["payload"])
                ).hexdigest()
                facts = attestation.verify_document(document, now=self.NOW)
                self.assertTrue(facts["lifecycle"][field])
                self.assertFalse(facts["promotionEligible"])

    def test_signing_rejects_ineligible_draft_before_running_a_signer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = self._document(eligible=False)
            path = root / "attestation.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with patch.object(attestation, "run") as signer:
                with self.assertRaisesRegex(IntegrityError, "ineligible"):
                    attestation.sign_file(
                        ProjectPaths(root),
                        path,
                        root / "signature",
                        root / "secret-key",
                        "minisign",
                    )
            signer.assert_not_called()

    def test_signer_rejects_evidence_subject_substitution_before_invocation(self) -> None:
        for substitution in ("modelId", "validationInput"):
            with self.subTest(substitution=substitution), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                document, _source = self._document_with_retained_evidence(root)
                subject = document["payload"]["evidenceSubject"]
                if substitution == "modelId":
                    subject["acceptance"]["modelId"] = "substituted-model"
                else:
                    subject["validationInput"]["sha256"] = "9" * 64
                    subject["acceptance"]["validationInputSha256"] = "9" * 64
                document["payload"]["evidenceSubjectSha256"] = (
                    attestation.sha256_document(subject)
                )
                document["payloadSha256"] = attestation.sha256_document(
                    document["payload"]
                )
                path = root / "attestation.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with (
                    patch.object(
                        attestation,
                        "_current_source_eligibility",
                        return_value=document["payload"]["eligibility"],
                    ),
                    patch.object(attestation, "run") as signer,
                    self.assertRaisesRegex(
                        IntegrityError, "evidence-derived subject differs"
                    ),
                ):
                    attestation.sign_file(
                        ProjectPaths(root),
                        path,
                        root / "signature",
                        root / "secret-key",
                        "minisign",
                    )
                signer.assert_not_called()

    def test_verifier_uses_same_private_snapshots_after_original_paths_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = self._document()
            signature = root / "test.minisig"
            signature.write_bytes(b"original-signature")
            signature.chmod(0o600)
            public_key = root / "trusted.pub"
            public_key.write_bytes(b"original-public-key")
            public_key.chmod(0o600)
            document["signature"] = {
                "schemaVersion": 1,
                "tool": "minisign",
                "detachedFile": signature.name,
                "sha256": hashlib.sha256(signature.read_bytes()).hexdigest(),
                "payloadSha256": document["payloadSha256"],
                "signedAt": "2026-08-09T11:00:00Z",
                "trustModel": attestation.SIGNATURE_TRUST_MODEL,
            }
            document["validationStatus"] = "signed-unverified"
            path = root / "attestation.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            snapshots: list[Path] = []

            def verify_snapshot(argv, **_kwargs):
                key_snapshot = Path(argv[argv.index("-p") + 1])
                signature_snapshot = Path(argv[argv.index("-x") + 1])
                snapshots.extend((key_snapshot, signature_snapshot))
                self.assertNotEqual(key_snapshot, public_key)
                self.assertNotEqual(signature_snapshot, signature)
                self.assertEqual(
                    stat.S_IMODE(key_snapshot.stat().st_mode), 0o600
                )
                self.assertEqual(
                    stat.S_IMODE(signature_snapshot.stat().st_mode), 0o600
                )
                public_key.write_bytes(b"swapped-public-key")
                signature.write_bytes(b"swapped-signature")
                self.assertEqual(key_snapshot.read_bytes(), b"original-public-key")
                self.assertEqual(
                    signature_snapshot.read_bytes(), b"original-signature"
                )
                return RunResult(tuple(argv), 0, "verified", "")

            with (
                patch.object(attestation, "run", side_effect=verify_snapshot),
                patch.object(
                    attestation,
                    "_current_validation_input",
                    return_value=None,
                ),
                patch.object(
                    attestation,
                    "_git_facts",
                    return_value={"revision": "a" * 40, "dirty": False},
                ),
            ):
                facts = attestation.verify_detached(
                    ProjectPaths(root), path, signature, public_key, "minisign"
                )
            self.assertTrue(facts["signature"]["cryptographicallyVerified"])
            self.assertEqual(
                facts["signature"]["verificationKeySha256"],
                hashlib.sha256(b"original-public-key").hexdigest(),
            )
            self.assertTrue(snapshots)
            self.assertTrue(all(not snapshot.exists() for snapshot in snapshots))

    def test_verifier_input_snapshot_rejects_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            linked_target = root / "linked-target"
            linked_target.write_bytes(b"key")
            linked_target.chmod(0o600)
            symlink = root / "symlink"
            symlink.symlink_to(linked_target)

            hard_target = root / "hard-target"
            hard_target.write_bytes(b"signature")
            hard_target.chmod(0o600)
            hardlink = root / "hardlink"
            os.link(hard_target, hardlink)

            wide = root / "wide"
            wide.write_bytes(b"key")
            wide.chmod(0o666)

            for unsafe in (symlink, hardlink, wide, root):
                with self.subTest(path=unsafe.name), self.assertRaises(IntegrityError):
                    attestation._verification_input_snapshot(
                        unsafe, label="verification input"
                    )

    def test_promotion_rejects_cryptographically_valid_substituted_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document, _source = self._document_with_retained_evidence(root)
            subject = document["payload"]["evidenceSubject"]
            subject["acceptance"]["modelId"] = "substituted-model"
            document["payload"]["evidenceSubjectSha256"] = (
                attestation.sha256_document(subject)
            )
            document["payloadSha256"] = attestation.sha256_document(
                document["payload"]
            )
            signature = root / "test.minisig"
            signature.write_bytes(b"detached-signature")
            signature.chmod(0o600)
            public_key = root / "trusted.pub"
            public_key.write_bytes(b"trusted-public-key")
            public_key.chmod(0o600)
            key_sha = hashlib.sha256(public_key.read_bytes()).hexdigest()
            document["signature"] = {
                "schemaVersion": 1,
                "tool": "minisign",
                "detachedFile": signature.name,
                "sha256": hashlib.sha256(signature.read_bytes()).hexdigest(),
                "payloadSha256": document["payloadSha256"],
                "signedAt": "2026-08-09T11:00:00Z",
                "trustModel": attestation.SIGNATURE_TRUST_MODEL,
            }
            document["validationStatus"] = "signed-unverified"
            path = root / "attestation.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            current_input = {
                "policy": attestation.VALIDATION_INPUT_POLICY,
                "expectedSha256": "e" * 64,
                "currentSha256": "e" * 64,
                "matches": True,
                "catalogSha256": "6" * 64,
                "repositorySha256": "7" * 64,
            }
            with (
                patch.object(attestation, "run"),
                patch.object(
                    attestation,
                    "_current_validation_input",
                    return_value=current_input,
                ),
                patch.object(
                    attestation,
                    "_git_facts",
                    return_value={"revision": "a" * 40, "dirty": False},
                ),
                self.assertRaisesRegex(
                    IntegrityError, "canonicalEvidenceSubject"
                ),
            ):
                attestation.verify_detached(
                    ProjectPaths(root),
                    path,
                    signature,
                    public_key,
                    "minisign",
                    trusted_key_sha256=key_sha,
                    require_promotion=True,
                )

    def test_crypto_validity_is_not_promotion_without_trusted_key_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = self._document()
            document["payload"]["lifecycle"]["validity"] = {
                "notBefore": "2020-01-01T00:00:00Z",
                "expiresAt": "2099-01-01T00:00:00Z",
            }
            document["payload"][
                "evidenceSubjectSha256"
            ] = attestation.sha256_document(
                document["payload"]["evidenceSubject"]
            )
            signature = root / "test.minisig"
            signature.write_bytes(b"detached-signature")
            public_key = root / "trusted.pub"
            public_key.write_bytes(b"trusted-public-key")
            document["payloadSha256"] = hashlib.sha256(
                attestation.canonical_bytes(document["payload"])
            ).hexdigest()
            document["signature"] = {
                "schemaVersion": 1,
                "tool": "minisign",
                "detachedFile": signature.name,
                "sha256": hashlib.sha256(signature.read_bytes()).hexdigest(),
                "payloadSha256": document["payloadSha256"],
                "signedAt": "2026-08-09T11:00:00Z",
                "trustModel": attestation.SIGNATURE_TRUST_MODEL,
            }
            document["validationStatus"] = "signed-unverified"
            path = root / "attestation.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            key_sha = hashlib.sha256(public_key.read_bytes()).hexdigest()
            current_input = {
                "policy": attestation.VALIDATION_INPUT_POLICY,
                "expectedSha256": "e" * 64,
                "currentSha256": "e" * 64,
                "matches": True,
                "catalogSha256": "2" * 64,
                "repositorySha256": "3" * 64,
            }
            with (
                patch.object(attestation, "run"),
                patch.object(
                    attestation,
                    "_current_validation_input",
                    return_value=current_input,
                ),
                patch.object(
                    attestation,
                    "_git_facts",
                    return_value={"revision": "a" * 40, "dirty": False},
                ),
                patch.object(
                    attestation,
                    "_evidence_subject_recheck",
                    return_value="passed",
                ),
                patch.object(
                    attestation,
                    "_current_source_eligibility",
                    return_value=document["payload"]["eligibility"],
                ),
            ):
                crypto_only = attestation.verify_detached(
                    ProjectPaths(root), path, signature, public_key, "minisign"
                )
                self.assertTrue(
                    crypto_only["signature"]["cryptographicallyVerified"]
                )
                self.assertFalse(
                    crypto_only["signature"]["trustedKeyFingerprint"]
                )
                self.assertFalse(crypto_only["promotionEligible"])

                with self.assertRaisesRegex(
                    IntegrityError, "cryptographically valid but not valid for promotion"
                ):
                    attestation.verify_detached(
                        ProjectPaths(root),
                        path,
                        signature,
                        public_key,
                        "minisign",
                        trusted_key_sha256="0" * 64,
                        require_promotion=True,
                    )

                promoted = attestation.verify_detached(
                    ProjectPaths(root),
                    path,
                    signature,
                    public_key,
                    "minisign",
                    trusted_key_sha256=key_sha,
                    require_promotion=True,
                )
            self.assertTrue(promoted["promotionEligible"])
            self.assertTrue(promoted["signature"]["trustedKeyFingerprint"])


class BundleTests(unittest.TestCase):
    def _project(self, root: Path) -> ProjectPaths:
        (root / "catalog").mkdir()
        (root / "config").mkdir()
        (root / "models").mkdir(mode=0o700)
        (root / "models" / "tiny").mkdir(mode=0o700)
        data = b"reviewed model bytes"
        artifact = root / "models" / "tiny" / "tiny.gguf"
        artifact.write_bytes(data)
        catalog = json.loads(
            (ROOT / "catalog" / "models.json").read_text(encoding="utf-8")
        )
        model = json.loads(json.dumps(catalog["models"][0]))
        model.update(
            {
                "id": "tiny-model",
                "displayName": "Tiny reviewed test model",
                "purpose": "Bundle boundary test fixture",
                "modelDirectory": "tiny",
                "servedModelId": "tiny-model",
            }
        )
        model["requirements"]["minFreeDiskGiB"] = 1
        model["artifacts"] = [
            {
                "role": "model",
                "filename": "tiny.gguf",
                "required": True,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "url": (
                    f"https://huggingface.co/{model['artifactRepository']}/resolve/"
                    f"{model['artifactRevision']}/tiny.gguf?download=true"
                ),
            }
        ]
        catalog["defaultModel"] = model["id"]
        catalog["models"] = [model]
        (root / "catalog" / "models.json").write_text(json.dumps(catalog))
        (root / "config" / "runtime-profiles.json").write_text("{}")
        (root / "compose.yaml").write_text("services:\n  model:\n    image: example/image@sha256:" + "b" * 64 + "\n")
        return ProjectPaths(root)

    def test_create_rejects_an_invalid_local_catalog_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            marker = "catalog-secret-marker"
            (root / "catalog" / "models.json").write_text(
                json.dumps({"schemaVersion": 2, "scope": marker})
            )
            with self.assertRaises(ConfigError) as raised:
                bundle.create(
                    paths,
                    root / "offline.tar",
                    "tiny-model",
                    include_model=False,
                )
            self.assertNotIn(marker, str(raised.exception))

    def test_bundle_creation_is_explicitly_lts_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            catalog_path = root / "catalog" / "models.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            candidate = copy.deepcopy(catalog["models"][0])
            candidate.update(
                {
                    "id": "tiny-candidate",
                    "displayName": "Tiny candidate",
                    "servedModelId": "tiny-candidate",
                    "modelDirectory": "tiny-candidate",
                    "lifecycleRole": "candidate",
                }
            )
            catalog["models"].append(candidate)
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "only the LTS"):
                bundle.create(
                    paths,
                    root / "candidate.tar",
                    "tiny-candidate",
                    include_model=False,
                )

    def _docker_save_archive(
        self,
        root: Path,
        *,
        declared_layer_diff_id: str | None = None,
    ) -> tuple[Path, dict[str, object]]:
        staging = root / "docker-save"
        staging.mkdir()
        layer_name = "layer/layer.tar"
        layer_path = staging / layer_name
        layer_path.parent.mkdir()
        layer_payload = staging / "payload.txt"
        layer_payload.write_text("immutable runtime layer", encoding="utf-8")
        with tarfile.open(layer_path, "w") as layer:
            layer.add(layer_payload, arcname="payload.txt")
        actual_diff_id = f"sha256:{hashlib.sha256(layer_path.read_bytes()).hexdigest()}"
        diff_id = declared_layer_diff_id or actual_diff_id
        config = {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        }
        config_body = json.dumps(
            config, sort_keys=True, separators=(",", ":")
        ).encode()
        config_digest = f"sha256:{hashlib.sha256(config_body).hexdigest()}"
        config_name = f"{config_digest.removeprefix('sha256:')}.json"
        (staging / config_name).write_bytes(config_body)
        docker_manifest = [
            {
                "Config": config_name,
                "RepoTags": ["example/image:latest"],
                "Layers": [layer_name],
            }
        ]
        (staging / "manifest.json").write_text(
            json.dumps(docker_manifest), encoding="utf-8"
        )
        image_archive = root / "runtime-image.tar"
        with tarfile.open(image_archive, "w") as archive:
            for name in ("manifest.json", config_name, layer_name):
                archive.add(staging / name, arcname=name, recursive=False)
        inspect = {
            "Id": config_digest,
            "RepoDigests": ["example/image@sha256:" + "b" * 64],
            "RootFS": {"Type": "layers", "Layers": [diff_id]},
        }
        return image_archive, inspect

    def _replace_bundled_layer(self, bundle_path: Path, layer_name: str) -> None:
        work = bundle_path.parent / "rewrite"
        outer_staging = work / "outer"
        image_staging = work / "image"
        outer_staging.mkdir(parents=True)
        image_staging.mkdir(parents=True)
        with tarfile.open(bundle_path, "r") as archive:
            for member in archive.getmembers():
                target = outer_staging / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                assert source is not None
                target.write_bytes(source.read())

        image_archive = outer_staging / "images/runtime-image.tar"
        with tarfile.open(image_archive, "r") as archive:
            for member in archive.getmembers():
                target = image_staging / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                assert source is not None
                target.write_bytes(source.read())
        layer = image_staging / layer_name
        changed = bytearray(layer.read_bytes())
        changed[-1] ^= 1
        layer.write_bytes(changed)
        with tarfile.open(image_archive, "w") as archive:
            for item in sorted(image_staging.rglob("*")):
                if item.is_file():
                    archive.add(
                        item,
                        arcname=item.relative_to(image_staging).as_posix(),
                        recursive=False,
                    )

        manifest_path = outer_staging / "bundle-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["images/runtime-image.tar"] = {
            "bytes": image_archive.stat().st_size,
            "sha256": hashlib.sha256(image_archive.read_bytes()).hexdigest(),
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with tarfile.open(bundle_path, "w") as archive:
            for item in sorted(outer_staging.rglob("*")):
                if item.is_file():
                    archive.add(
                        item,
                        arcname=item.relative_to(outer_staging).as_posix(),
                        recursive=False,
                    )

    def _set_bundle_schema(self, bundle_path: Path, schema_version: int) -> None:
        with tempfile.TemporaryDirectory(dir=bundle_path.parent) as directory:
            staging = Path(directory)
            with tarfile.open(bundle_path, "r") as archive:
                for member in archive.getmembers():
                    target = staging / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    assert source is not None
                    target.write_bytes(source.read())
            manifest_path = staging / "bundle-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schemaVersion"] = schema_version
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with tarfile.open(bundle_path, "w") as archive:
                for item in sorted(staging.rglob("*")):
                    if item.is_file():
                        archive.add(
                            item,
                            arcname=item.relative_to(staging).as_posix(),
                            recursive=False,
                        )

    def _rewrite_bundle(
        self,
        bundle_path: Path,
        *,
        manifest_mutator: Callable[[dict[str, Any]], None] | None = None,
        catalog_mutator: Callable[[dict[str, Any]], None] | None = None,
        catalog_text_mutator: Callable[[str], str] | None = None,
        remove_members: tuple[str, ...] = (),
    ) -> None:
        """Rewrite a generated bundle and keep its outer file identities valid."""

        with tempfile.TemporaryDirectory(dir=bundle_path.parent) as directory:
            staging = Path(directory)
            with tarfile.open(bundle_path, "r") as archive:
                for member in archive.getmembers():
                    target = staging / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    assert source is not None
                    target.write_bytes(source.read())

            manifest_path = staging / "bundle-manifest.json"
            catalog_path = staging / "catalog/models.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            if catalog_mutator is not None and catalog_text_mutator is not None:
                raise AssertionError("choose one Catalog mutation form")
            if catalog_mutator is not None:
                catalog_mutator(catalog)
                catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            elif catalog_text_mutator is not None:
                catalog_path.write_text(
                    catalog_text_mutator(
                        catalog_path.read_text(encoding="utf-8")
                    ),
                    encoding="utf-8",
                )
            for name in remove_members:
                (staging / name).unlink()

            manifest["files"] = {
                item.relative_to(staging).as_posix(): {
                    "bytes": item.stat().st_size,
                    "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                }
                for item in sorted(staging.rglob("*"))
                if item.is_file() and item != manifest_path
            }
            if manifest_mutator is not None:
                manifest_mutator(manifest)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with tarfile.open(bundle_path, "w") as archive:
                for item in sorted(staging.rglob("*")):
                    if item.is_file():
                        archive.add(
                            item,
                            arcname=item.relative_to(staging).as_posix(),
                            recursive=False,
                        )

    def test_bundle_round_trip_is_verified_and_import_does_not_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            archive = root / "offline.tar"
            created = bundle.create(paths, archive, "tiny-model", include_model=True)
            self.assertTrue(created["containsModelArtifacts"])
            verified = bundle.verify(archive)
            self.assertTrue(verified["hostAdmissionRequired"])
            (root / "models" / "tiny" / "tiny.gguf").unlink()
            imported = bundle.import_artifacts(paths, archive)
            self.assertFalse(imported["selected"])
            self.assertFalse(imported["runtimeStarted"])

    def test_bundle_binds_top_level_claims_to_its_single_model_catalog(self) -> None:
        def wrong_kind(manifest: dict[str, Any]) -> None:
            manifest["kind"] = "different/offline-bundle"

        def wrong_policy(manifest: dict[str, Any]) -> None:
            manifest["importPolicy"] = {
                "selectModel": True,
                "startRuntime": False,
                "hostAdmissionRequired": True,
            }

        def wrong_model(manifest: dict[str, Any]) -> None:
            manifest["modelId"] = "substitute-model"

        def wrong_default(catalog: dict[str, Any]) -> None:
            catalog["defaultModel"] = "substitute-model"

        def extra_model(catalog: dict[str, Any]) -> None:
            catalog["models"].append(dict(catalog["models"][0]))

        def invalid_scope(catalog: dict[str, Any]) -> None:
            catalog["scope"] = ""

        def legacy_catalog(catalog: dict[str, Any]) -> None:
            catalog["schemaVersion"] = 1

        cases = (
            ("kind", wrong_kind, None, "kind"),
            ("policy", wrong_policy, None, "import policy"),
            ("model", wrong_model, None, "single-model catalog"),
            ("default", None, wrong_default, "single-model catalog"),
            ("catalog-count", None, extra_model, "single-model catalog"),
            ("catalog-schema", None, invalid_scope, "Catalog is invalid"),
            ("catalog-downgrade", None, legacy_catalog, "cannot embed a legacy"),
        )
        for name, manifest_mutator, catalog_mutator, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = self._project(root)
                archive = root / "offline.tar"
                bundle.create(paths, archive, "tiny-model", include_model=True)
                self._rewrite_bundle(
                    archive,
                    manifest_mutator=manifest_mutator,
                    catalog_mutator=catalog_mutator,
                )
                with self.assertRaisesRegex(IntegrityError, message):
                    bundle.verify(archive)

    def test_bundle_catalog_rejects_duplicate_json_keys_after_outer_rehash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            archive = root / "offline.tar"
            bundle.create(paths, archive, "tiny-model", include_model=True)

            def duplicate_default(text: str) -> str:
                marker = '"defaultModel": "tiny-model",'
                return text.replace(marker, marker + "\n  " + marker, 1)

            self._rewrite_bundle(
                archive,
                catalog_text_mutator=duplicate_default,
            )
            with self.assertRaisesRegex(IntegrityError, "Catalog JSON is invalid"):
                bundle.verify(archive)

    def test_bundle_binds_artifact_declaration_path_size_and_sha_to_catalog(self) -> None:
        def declares_none(manifest: dict[str, Any]) -> None:
            manifest["containsModelArtifacts"] = False

        def wrong_path(catalog: dict[str, Any]) -> None:
            catalog["models"][0]["artifacts"][0]["filename"] = "substitute.gguf"

        def wrong_size(catalog: dict[str, Any]) -> None:
            catalog["models"][0]["artifacts"][0]["bytes"] += 1

        def wrong_sha(catalog: dict[str, Any]) -> None:
            catalog["models"][0]["artifacts"][0]["sha256"] = "e" * 64

        cases = (
            ("declares-none", declares_none, None, "declaring none"),
            ("path", None, wrong_path, "absent from its catalog"),
            ("size", None, wrong_size, "does not match its catalog"),
            ("sha", None, wrong_sha, "does not match its catalog"),
        )
        for name, manifest_mutator, catalog_mutator, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = self._project(root)
                archive = root / "offline.tar"
                bundle.create(paths, archive, "tiny-model", include_model=True)
                self._rewrite_bundle(
                    archive,
                    manifest_mutator=manifest_mutator,
                    catalog_mutator=catalog_mutator,
                )
                with self.assertRaisesRegex(IntegrityError, message):
                    bundle.verify(archive)

    def test_bundle_import_rejects_declared_artifacts_with_a_required_member_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            archive = root / "offline.tar"
            bundle.create(paths, archive, "tiny-model", include_model=True)
            self._rewrite_bundle(
                archive,
                remove_members=("artifacts/tiny-model/tiny.gguf",),
            )
            with (
                patch.object(
                    bundle,
                    "verify",
                    return_value={"modelId": "tiny-model"},
                ),
                self.assertRaisesRegex(IntegrityError, "missing required"),
            ):
                bundle.import_artifacts(paths, archive)

    def test_bundle_import_rejects_model_directory_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            paths = self._project(root)
            archive = root / "offline.tar"
            bundle.create(paths, archive, "tiny-model", include_model=True)
            artifact_directory = root / "models" / "tiny"
            (artifact_directory / "tiny.gguf").unlink()
            artifact_directory.rmdir()
            artifact_directory.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                IntegrityError, "unsafe|symbolic link"
            ):
                bundle.import_artifacts(paths, archive)
            self.assertFalse((outside / "tiny.gguf").exists())

    def test_bundle_import_rejects_models_parent_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            paths = self._project(root)
            archive = root / "offline.tar"
            bundle.create(paths, archive, "tiny-model", include_model=True)
            artifact_directory = root / "models" / "tiny"
            (artifact_directory / "tiny.gguf").unlink()
            artifact_directory.rmdir()
            (root / "models").rmdir()
            (root / "models").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                IntegrityError, "unsafe|symbolic link"
            ):
                bundle.import_artifacts(paths, archive)
            self.assertFalse((outside / "tiny.gguf").exists())

    def test_bundle_rejects_non_object_file_identity_as_integrity_error(self) -> None:
        def corrupt_identity(manifest: dict[str, Any]) -> None:
            manifest["files"]["catalog/models.json"] = "not-an-object"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            archive = root / "offline.tar"
            bundle.create(paths, archive, "tiny-model", include_model=True)
            self._rewrite_bundle(archive, manifest_mutator=corrupt_identity)
            with self.assertRaisesRegex(IntegrityError, "identity record is invalid"):
                bundle.verify(archive)

    def test_legacy_artifact_only_bundle_remains_readable(self) -> None:
        def downgrade_catalog(catalog: dict[str, Any]) -> None:
            catalog["schemaVersion"] = 1
            catalog.pop("scope", None)
            catalog.pop("deploymentPolicy", None)
            model = catalog["models"][0]
            model.pop("deploymentEligibility", None)
            model["runtime"].pop("cacheTypeK", None)
            model["runtime"].pop("cacheTypeV", None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            archive = root / "legacy-artifacts.tar"
            bundle.create(paths, archive, "tiny-model", include_model=True)
            self._rewrite_bundle(archive, catalog_mutator=downgrade_catalog)
            self._set_bundle_schema(archive, bundle.LEGACY_SCHEMA_VERSION)
            verified = bundle.verify(archive)
            self.assertTrue(verified["containsModelArtifacts"])

    def test_legacy_image_bundle_is_rejected_with_a_recreation_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            image_archive, inspect = self._docker_save_archive(root)
            completed = bundle.subprocess.CompletedProcess(
                ("docker", "image", "inspect"),
                0,
                json.dumps([inspect]),
                "",
            )
            output = root / "legacy-image.tar"
            with patch.object(bundle.subprocess, "run", return_value=completed):
                bundle.create(
                    paths,
                    output,
                    "tiny-model",
                    include_model=False,
                    image_archive=image_archive,
                )
            self._set_bundle_schema(output, bundle.LEGACY_SCHEMA_VERSION)
            with self.assertRaisesRegex(
                IntegrityError, "legacy schema-v1 runtime image bundle.*recreate"
            ):
                bundle.verify(output)

    def test_bundle_rejects_path_traversal_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "unsafe.tar"
            payload = Path(directory) / "payload"
            payload.write_bytes(b"x")
            with tarfile.open(archive, "w") as handle:
                handle.add(payload, arcname="../escape")
            with self.assertRaises(IntegrityError):
                bundle.verify(archive)

    def test_image_archive_binds_pinned_manifest_config_and_diff_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            image_archive, inspected = self._docker_save_archive(root)
            output = root / "offline.tar"
            completed = bundle.subprocess.CompletedProcess(
                ("docker", "image", "inspect"),
                0,
                json.dumps([inspected]),
                "",
            )
            with patch.object(
                bundle.subprocess, "run", return_value=completed
            ) as docker_inspect:
                created = bundle.create(
                    paths,
                    output,
                    "tiny-model",
                    include_model=False,
                    image_archive=image_archive,
                )
            identity = created["runtimeImageIdentity"]
            self.assertEqual(
                identity["pinnedManifestDigest"], "sha256:" + "b" * 64
            )
            self.assertEqual(identity["localImage"]["configDigest"], inspected["Id"])
            self.assertEqual(
                identity["archive"]["layers"][0]["diffId"],
                inspected["RootFS"]["Layers"][0],
            )
            docker_inspect.assert_called_once_with(
                [
                    "docker",
                    "image",
                    "inspect",
                    "example/image@sha256:" + "b" * 64,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            verified = bundle.verify(output)
            self.assertTrue(verified["containsRuntimeImageArchive"])
            self.assertEqual(verified["runtimeImageIdentity"], identity)

    def test_image_archive_creation_rejects_pinned_config_and_layer_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            image_archive, inspected = self._docker_save_archive(root)
            wrong_pinned_digest = dict(inspected)
            wrong_pinned_digest["RepoDigests"] = [
                "example/image@sha256:" + "e" * 64
            ]
            completed = bundle.subprocess.CompletedProcess(
                ("docker", "image", "inspect"),
                0,
                json.dumps([wrong_pinned_digest]),
                "",
            )
            with (
                patch.object(bundle.subprocess, "run", return_value=completed),
                self.assertRaisesRegex(IntegrityError, "pinned digest"),
            ):
                bundle.create(
                    paths,
                    root / "wrong-pinned.tar",
                    "tiny-model",
                    include_model=False,
                    image_archive=image_archive,
                )

            wrong_config = dict(inspected)
            wrong_config["Id"] = "sha256:" + "c" * 64
            completed = bundle.subprocess.CompletedProcess(
                ("docker", "image", "inspect"),
                0,
                json.dumps([wrong_config]),
                "",
            )
            with (
                patch.object(bundle.subprocess, "run", return_value=completed),
                self.assertRaisesRegex(IntegrityError, "config digest"),
            ):
                bundle.create(
                    paths,
                    root / "wrong-config.tar",
                    "tiny-model",
                    include_model=False,
                    image_archive=image_archive,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            false_diff_id = "sha256:" + "d" * 64
            image_archive, inspected = self._docker_save_archive(
                root, declared_layer_diff_id=false_diff_id
            )
            completed = bundle.subprocess.CompletedProcess(
                ("docker", "image", "inspect"),
                0,
                json.dumps([inspected]),
                "",
            )
            with (
                patch.object(bundle.subprocess, "run", return_value=completed),
                self.assertRaisesRegex(IntegrityError, "layer diff_id mismatch"),
            ):
                bundle.create(
                    paths,
                    root / "wrong-layer.tar",
                    "tiny-model",
                    include_model=False,
                    image_archive=image_archive,
                )

    def test_bundle_verify_recomputes_nested_layer_diff_ids_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            image_archive, inspected = self._docker_save_archive(root)
            output = root / "offline.tar"
            completed = bundle.subprocess.CompletedProcess(
                ("docker", "image", "inspect"),
                0,
                json.dumps([inspected]),
                "",
            )
            with patch.object(bundle.subprocess, "run", return_value=completed):
                created = bundle.create(
                    paths,
                    output,
                    "tiny-model",
                    include_model=False,
                    image_archive=image_archive,
                )
            layer_name = created["runtimeImageIdentity"]["archive"]["layers"][0][
                "path"
            ]
            self._replace_bundled_layer(output, layer_name)
            with self.assertRaisesRegex(IntegrityError, "layer diff_id mismatch"):
                bundle.verify(output)


class StorageTests(unittest.TestCase):
    def test_inventory_pins_active_and_transaction_rollback_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            paths.state_dir.mkdir(parents=True)
            paths.transaction_path.write_text("{}", encoding="utf-8")
            paths.transaction_path.chmod(0o600)
            pointer = MagicMock(active_spec_sha256="a" * 64)
            store = MagicMock()
            store.pointer_path = paths.state_dir / "rollback.json"
            store.read_pointer.return_value = pointer
            store.spec_path.side_effect = lambda digest: (
                paths.state_dir / "rollback-specs" / f"{digest}.json"
            )
            store.read_spec.side_effect = lambda digest: MagicMock(
                document=lambda: {
                    "artifacts": [
                        {"relativePath": f"models/{digest[:4]}/model.gguf"}
                    ],
                    "acceptance": {
                        "evidencePath": f"logs/acceptance/{digest[:4]}.json"
                    },
                }
            )
            transaction = MagicMock()
            transaction.read.return_value = {
                "rolloutIntent": {"rollbackSpecSha256": "b" * 64}
            }
            with (
                patch.object(storage, "RollbackStore", return_value=store),
                patch.object(storage, "TransactionStore", return_value=transaction),
            ):
                report = storage.inventory(paths)

            self.assertEqual(
                report["protectedReferences"],
                sorted(
                    {
                        "cache/control-plane/rollback.json",
                        "cache/control-plane/rollback-specs/" + "a" * 64 + ".json",
                        "cache/control-plane/rollback-specs/" + "b" * 64 + ".json",
                        "cache/control-plane/transaction.json",
                        "logs/acceptance/aaaa.json",
                        "logs/acceptance/bbbb.json",
                        "models/aaaa/model.gguf",
                        "models/bbbb/model.gguf",
                    }
                ),
            )

    def test_gc_is_limited_to_old_partial_and_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            (root / "cache").mkdir()
            partial = root / "models" / "download.gguf.part"
            model = root / "models" / "keep.gguf"
            partial.write_bytes(b"partial")
            model.write_bytes(b"model")
            old = time.time() - 30 * 86400
            os.utime(partial, (old, old))
            candidates = storage.gc_candidates(ProjectPaths(root), older_than_days=14)
            self.assertEqual([item["path"] for item in candidates], ["models/download.gguf.part"])
            storage.delete_candidates(ProjectPaths(root), candidates)
            self.assertFalse(partial.exists())
            self.assertTrue(model.exists())

    def test_gc_suppresses_candidates_during_unfinished_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            partial = root / "models" / "download.gguf.part"
            partial.write_bytes(b"partial")
            old = time.time() - 30 * 86400
            os.utime(partial, (old, old))
            store = TransactionStore(ProjectPaths(root))
            store.begin("deploy", "model", {})
            self.assertEqual(
                storage.gc_candidates(ProjectPaths(root), older_than_days=14), []
            )

    def test_gc_fails_closed_when_the_catalog_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            (root / "catalog").mkdir()
            partial = root / "models" / "download.gguf.part"
            partial.write_bytes(b"partial")
            old = time.time() - 30 * 86400
            os.utime(partial, (old, old))
            (root / "catalog" / "models.json").write_text("{}")
            self.assertEqual(
                storage.gc_candidates(ProjectPaths(root), older_than_days=14), []
            )


if __name__ == "__main__":
    unittest.main()
