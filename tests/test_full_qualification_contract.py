"""Pure contract tests for transaction-bound full qualification.

The tests in this module mock every process boundary.  They exercise the
controller's fail-closed treatment of preflight output and the final recheck
immediately before the source runtime can enter its stopping state.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_inference_stack import cli  # noqa: E402
from local_inference_stack.catalog import load_catalog  # noqa: E402
from local_inference_stack.deployment import CatalogDeploymentSpec  # noqa: E402
from local_inference_stack.paths import ProjectPaths  # noqa: E402
from local_inference_stack.result import AdmissionError, IntegrityError  # noqa: E402
from local_inference_stack.runner import RunResult  # noqa: E402
from local_inference_stack.transactions import _qualification_binding  # noqa: E402


ACCEPTANCE_EVIDENCE_SPEC = importlib.util.spec_from_file_location(
    "full_qualification_acceptance_evidence",
    ROOT / "scripts" / "acceptance-evidence.py",
)
assert ACCEPTANCE_EVIDENCE_SPEC and ACCEPTANCE_EVIDENCE_SPEC.loader
ACCEPTANCE_EVIDENCE = importlib.util.module_from_spec(ACCEPTANCE_EVIDENCE_SPEC)
ACCEPTANCE_EVIDENCE_SPEC.loader.exec_module(ACCEPTANCE_EVIDENCE)


TRANSACTION_ID = "f18ab4a1-dd5f-4db0-8466-7ca2c9293d65"
ROLLBACK_SHA256 = "a" * 64


def _specs() -> tuple[CatalogDeploymentSpec, CatalogDeploymentSpec]:
    source_model = load_catalog(ROOT / "catalog" / "models.json")["models"][0]
    target_model = copy.deepcopy(source_model)
    target_model.update(
        {
            "id": "qualification-contract-target",
            "displayName": "Qualification contract target",
            "modelDirectory": "qualification-contract-target",
        }
    )
    target_model["artifacts"][0]["filename"] = "qualification-contract-target.gguf"
    return (
        CatalogDeploymentSpec.from_catalog_model(source_model),
        CatalogDeploymentSpec.from_catalog_model(target_model),
    )


def _modelport_identity() -> dict[str, Any]:
    materials = [
        {"path": path, "sha256": str(index) * 64}
        for index, path in enumerate(
            ACCEPTANCE_EVIDENCE.MODELPORT_MATERIAL_PATHS, start=1
        )
    ]
    identity = {
        "policyId": "local-inference-stack/modelport-source-identity-v2",
        "gitCommit": "b" * 40,
        "gitTree": "c" * 40,
        "sourceState": "clean",
        "materials": materials,
        "materialsSha256": cli.sha256_document(materials),
        "liveServiceIdentity": {
            "endpoint": "http://127.0.0.1:38082/livez",
            "service": "model-port",
            "status": "ok",
            "build": {
                "revision": "b" * 40,
                "sourceState": "clean",
                "version": "1.2.3",
                "configSha256": materials[0]["sha256"],
            },
        },
    }
    return identity


def _qualification_input(
    target: CatalogDeploymentSpec,
    identity: dict[str, Any],
) -> dict[str, Any]:
    contract_path = ROOT / "contracts" / "local-qwen-provider-v1.json"
    contract_body = contract_path.read_bytes()
    contract = json.loads(contract_body)
    value: dict[str, Any] = {
        "policyId": "local-inference-stack/full-qualification-input-v1",
        "targetCatalogSpecSha256": target.sha256,
        "providerContractId": "local-qwen-provider-v1",
        "providerContractSha256": hashlib.sha256(contract_body).hexdigest(),
        "servedModelId": target.served_model_id,
        "limitsSha256": cli.sha256_document(contract["limits"]),
        "acceptanceSha256": cli.sha256_document(contract["acceptance"]),
        "logicalModels": contract["application"]["logicalModels"],
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
        "liveModelRegistrySha256": "1" * 64,
        "toolUseLocalProviderReady": True,
    }
    value["sha256"] = cli.sha256_document(value)
    return value


def _preflight_result(
    target: CatalogDeploymentSpec,
    identity: dict[str, Any],
    qualification_input: dict[str, Any],
) -> RunResult:
    return RunResult(
        ("scripts/acceptance-evidence.py", "qualification-preflight"),
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


def _performance_policy() -> SimpleNamespace:
    return SimpleNamespace(
        manifest_relative_path=(
            "deployments/qwen3.5-9b-rtx5070ti/manifest.json"
        ),
        manifest_sha256="2" * 64,
        policy_sha256="3" * 64,
    )


class _RollbackSpec:
    sha256 = ROLLBACK_SHA256


class _StopObserved(RuntimeError):
    pass


class _RecordingTransaction:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.state: dict[str, Any] = {
            "id": TRANSACTION_ID,
            "operation": "upgrade",
            "state": "planned",
            "rolloutActionOrdinal": 0,
        }

    def begin_rollout(
        self,
        operation: str,
        target: str,
        capture: Any,
        *,
        approved_catalog_spec: dict[str, Any],
    ) -> dict[str, Any]:
        del approved_catalog_spec
        original, intent = capture(TRANSACTION_ID, "2026-08-13T01:02:03Z")
        self.state.update(
            {
                "operation": operation,
                "target": target,
                "original": original,
                "rolloutIntent": intent,
            }
        )
        return copy.deepcopy(self.state)

    def transition(self, target_state: str, **_kwargs: Any) -> dict[str, Any]:
        self.events.append(f"transition:{target_state}")
        self.state["state"] = target_state
        return copy.deepcopy(self.state)

    def advance_rollout_action(self, **kwargs: Any) -> dict[str, Any]:
        self.state["rolloutActionOrdinal"] = kwargs["expected_action_ordinal"] + 1
        return copy.deepcopy(self.state)

    @contextmanager
    def authorized_runtime_mutation(self, *_args: Any, **_kwargs: Any):
        yield

    @contextmanager
    def runtime_boundary(self):
        yield

    def pending_rollout_qualification_binding(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "policyId": "local-inference-stack/rollout-qualification-binding-v1",
            "transactionId": TRANSACTION_ID,
            "operation": "upgrade",
            "rolloutPlanSha256": "4" * 64,
            "actionOrdinal": kwargs["action_ordinal"],
            "actionKind": "target-full",
            "rollbackSpecSha256": ROLLBACK_SHA256,
            "sourceCatalogSpecSha256": "5" * 64,
            "targetCatalogSpecSha256": kwargs["catalog_spec_sha256"],
            "performancePolicySha256": "3" * 64,
            "modelPortSourceIdentitySha256": "6" * 64,
            "qualificationInputSha256": "7" * 64,
        }


class StrictPreflightOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        _source, self.target = _specs()
        self.identity = _modelport_identity()
        self.qualification_input = _qualification_input(
            self.target, self.identity
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _read(
        self,
        qualification_input: dict[str, Any],
        *,
        identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_identity = identity or self.identity
        result = _preflight_result(
            self.target, source_identity, qualification_input
        )
        with (
            patch.dict(
                os.environ,
                {"MODELPORT_PROJECT_DIR": "/srv/modelport"},
                clear=True,
            ),
            patch.object(cli, "run", return_value=result),
        ):
            _path, _identity, observed = cli._modelport_qualification_preflight(
                self.paths, self.target
            )
        return observed

    def test_cli_rejects_legacy_or_live_config_substituted_source_identity(
        self,
    ) -> None:
        mutations = {
            "legacy policy": lambda value: value.update(
                policyId="local-inference-stack/modelport-source-identity-v1"
            ),
            "live config": lambda value: value["liveServiceIdentity"]["build"].update(
                configSha256="0" * 64
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                identity = copy.deepcopy(self.identity)
                mutate(identity)
                qualification_input = copy.deepcopy(self.qualification_input)
                qualification_input["modelPortSourceIdentitySha256"] = (
                    cli.sha256_document(identity)
                )
                qualification_input["sha256"] = cli.sha256_document(
                    {
                        key: item
                        for key, item in qualification_input.items()
                        if key != "sha256"
                    }
                )
                with self.assertRaises(AdmissionError):
                    self._read(qualification_input, identity=identity)

    def test_strict_v1_preflight_output_is_accepted_and_secret_free(self) -> None:
        observed = self._read(self.qualification_input)
        self.assertEqual(observed, self.qualification_input)
        encoded = json.dumps(observed, sort_keys=True)
        self.assertNotIn("MODELPORT_AUTH_TOKEN", encoded)
        self.assertNotIn("private-token", encoded)

    def test_rehashed_alias_context_or_benchmark_substitution_is_rejected(self) -> None:
        mutations = {
            "provider alias": lambda value: value.update(
                providerMatrixModel="qwen3.5-fast"
            ),
            "served model": lambda value: value.update(
                servedModelId="substituted-model"
            ),
            "logical model": lambda value: value["logicalModels"][
                "qwen3.5-code"
            ].update(maxOutputTokens=1),
            "extra logical alias": lambda value: value["logicalModels"].update(
                {"qwen3.5-cloud": copy.deepcopy(value["logicalModels"]["qwen3.5-code"])}
            ),
            "direct context": lambda value: value.update(directContextTokens=1),
            "ModelPort context": lambda value: value.update(
                modelPortContextTokens=1
            ),
            "ModelPort context output": lambda value: value.update(
                modelPortContextMaxTokens=1
            ),
            "decode tokens": lambda value: value.update(decodeTokens=1),
            "decode context": lambda value: value.update(decodeContextTokens=1),
            "concurrency": lambda value: value.update(concurrency=1),
            "concurrency tokens": lambda value: value.update(
                concurrencyTokens=1
            ),
            "local provider": lambda value: value.update(
                toolUseLocalProviderReady=False
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(self.qualification_input)
                mutate(changed)
                changed["sha256"] = cli.sha256_document(
                    {key: item for key, item in changed.items() if key != "sha256"}
                )
                with self.assertRaises(AdmissionError):
                    self._read(changed)

    def test_rehashed_secret_or_unknown_field_cannot_cross_preflight_boundary(
        self,
    ) -> None:
        changed = copy.deepcopy(self.qualification_input)
        changed["MODELPORT_AUTH_TOKEN"] = "private-token"
        changed["sha256"] = cli.sha256_document(
            {key: item for key, item in changed.items() if key != "sha256"}
        )
        with self.assertRaises(AdmissionError):
            self._read(changed)

    def test_authenticated_registry_uses_token_without_returning_it(self) -> None:
        token = "preflight-token-must-not-escape"
        logical_models = self.qualification_input["logicalModels"]
        observed_request: dict[str, Any] = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

        def urlopen(request: Any, **kwargs: Any) -> Response:
            observed_request["url"] = request.full_url
            observed_request["api_key"] = {
                key.lower(): value for key, value in request.header_items()
            }.get("x-api-key")
            observed_request["kwargs"] = kwargs
            response = Response()
            response.read = lambda *_args: json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": name,
                            "object": "model",
                            "owned_by": "local_qwen",
                        }
                        for name in logical_models
                    ],
                }
            ).encode("utf-8")
            return response

        with patch.object(
            ACCEPTANCE_EVIDENCE, "direct_urlopen", side_effect=urlopen
        ):
            registry = ACCEPTANCE_EVIDENCE._authenticated_modelport_registry(
                token,
                logical_models,
                "local_qwen",
            )

        self.assertEqual(
            observed_request["url"], "http://127.0.0.1:38082/v1/models"
        )
        self.assertEqual(observed_request["api_key"], token)
        self.assertLessEqual(observed_request["kwargs"]["timeout"], 10)
        self.assertNotIn(token, json.dumps(registry, sort_keys=True))
        self.assertNotIn("servedModelId", registry)
        self.assertNotIn(self.target.served_model_id, json.dumps(registry))
        self.assertRegex(registry["sha256"], r"^[0-9a-f]{64}$")

        with self.assertRaises(TypeError):
            ACCEPTANCE_EVIDENCE._authenticated_modelport_registry(
                token,
                logical_models,
                "local_qwen",
                "caller-injected-served-model",
            )

    def test_authenticated_registry_rejects_substituted_logical_models(self) -> None:
        logical_models = self.qualification_input["logicalModels"]

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

            def read(self, *_args: Any) -> bytes:
                return json.dumps(
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": "qwen3.5-fast",
                                "object": "model",
                                "owned_by": "foreign-provider",
                            }
                        ],
                    }
                ).encode("utf-8")

        with (
            patch.object(
                ACCEPTANCE_EVIDENCE,
                "direct_urlopen",
                return_value=Response(),
            ),
            self.assertRaisesRegex(RuntimeError, "model registry"),
        ):
            ACCEPTANCE_EVIDENCE._authenticated_modelport_registry(
                "private-token",
                logical_models,
                "local_qwen",
            )


class FullPlanDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.source, self.target = _specs()
        self.identity = _modelport_identity()
        self.qualification_input = _qualification_input(
            self.target, self.identity
        )
        self.performance = _performance_policy()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_upgrade_plan_and_pending_binding_carry_preflight_input_digest(
        self,
    ) -> None:
        events: list[str] = []
        transaction = _RecordingTransaction(events)
        rollback_store = MagicMock()
        rollback_store.read_pointer.return_value = None

        def admit(
            _paths: ProjectPaths, model_id: str, *, mode: str
        ) -> tuple[dict[str, Any], CatalogDeploymentSpec]:
            if mode == "existing-selection" and model_id == self.source.catalog_id:
                return (
                    {
                        "mode": "read-only-existing-selection-admission",
                        "catalogRecoveryEligible": True,
                    },
                    self.source,
                )
            if mode == "replacement" and model_id == self.target.catalog_id:
                return {"mode": "read-only-replacement-admission"}, self.target
            raise AssertionError((model_id, mode))

        def execute(
            _paths: ProjectPaths,
            observed_transaction: _RecordingTransaction,
            state: dict[str, Any],
            *_args: Any,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            intent = observed_transaction.state["rolloutIntent"]
            plan = intent["rolloutPlan"]
            self.assertEqual(
                plan["qualification"]["qualificationInputSha256"],
                self.qualification_input["sha256"],
            )
            action = next(
                item for item in plan["actions"] if item["kind"] == "target-full"
            )
            binding = _qualification_binding(
                observed_transaction.state, intent, action
            )
            self.assertEqual(
                binding["qualificationInputSha256"],
                self.qualification_input["sha256"],
            )
            return {
                **state,
                "state": "completed",
                "rolloutActionResults": [],
            }

        with (
            patch.object(
                cli,
                "_selected_catalog_spec",
                return_value=(self.source, {}),
            ),
            patch.object(cli, "_rollout_admission", side_effect=admit),
            patch.object(
                cli,
                "_modelport_qualification_preflight",
                return_value=(
                    "/srv/modelport",
                    self.identity,
                    copy.deepcopy(self.qualification_input),
                ),
            ) as preflight,
            patch.object(
                cli,
                "_target_performance_preflight",
                return_value=self.performance,
            ),
            patch.object(
                cli,
                "run",
                return_value=RunResult(("performance",), 0, "ok", ""),
            ),
            patch.object(
                cli,
                "_original_runtime",
                return_value={"healthy": True, "profile": "latency"},
            ),
            patch.object(
                cli,
                "capture_rollback_spec",
                return_value=_RollbackSpec(),
            ),
            patch.object(cli, "RollbackStore", return_value=rollback_store),
            patch.object(cli, "TransactionStore", return_value=transaction),
            patch.object(cli, "_execute_upgrade_rollout", side_effect=execute),
        ):
            result = cli._upgrade(
                self.paths,
                SimpleNamespace(
                    model=self.target.catalog_id,
                    qualification="full",
                    yes=True,
                ),
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(preflight.call_count, 2)


class StopSourceRecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.source, self.target = _specs()
        self.identity = _modelport_identity()
        self.qualification_input = _qualification_input(
            self.target, self.identity
        )
        self.performance = _performance_policy()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_full_inputs_are_rechecked_immediately_before_stopping_transition(
        self,
    ) -> None:
        events: list[str] = []
        transaction = _RecordingTransaction(events)

        def runner(argv: list[str], **_kwargs: Any) -> RunResult:
            if argv == ["scripts/runtime.sh", "stop"]:
                events.append("runtime:stop")
                raise _StopObserved("stop boundary reached")
            events.append("runtime:" + " ".join(argv))
            return RunResult(tuple(argv), 0, "ok", "")

        with (
            patch.object(cli, "RollbackStore", return_value=MagicMock()),
            patch.object(
                cli,
                "_modelport_qualification_preflight",
                side_effect=lambda _paths, _target: (
                    events.append("recheck:modelport")
                    or (
                        "/srv/modelport",
                        self.identity,
                        copy.deepcopy(self.qualification_input),
                    )
                ),
            ),
            patch.object(
                cli,
                "_target_performance_preflight",
                side_effect=lambda _paths, _target: (
                    events.append("recheck:performance") or self.performance
                ),
            ),
            patch.object(cli, "run", side_effect=runner),
            self.assertRaises(_StopObserved),
        ):
            cli._execute_upgrade_rollout(
                self.paths,
                transaction,
                copy.deepcopy(transaction.state),
                self.source,
                self.target,
                _RollbackSpec(),
                None,
                qualification="full",
                modelport_project_dir="/srv/modelport",
                qualification_input=self.qualification_input,
                performance_policy=self.performance,
            )

        stop_transition = events.index("transition:production_stopping")
        artifact_fetch = max(
            index
            for index, event in enumerate(events)
            if event.startswith("runtime:scripts/model-manager.py download")
        )
        modelport_recheck = events.index("recheck:modelport")
        performance_recheck = events.index("recheck:performance")
        self.assertLess(artifact_fetch, modelport_recheck)
        self.assertLess(modelport_recheck, performance_recheck)
        self.assertLess(performance_recheck, stop_transition)
        self.assertEqual(events[stop_transition + 1], "runtime:stop")

    def test_changed_last_moment_input_never_enters_stopping_state(self) -> None:
        events: list[str] = []
        transaction = _RecordingTransaction(events)
        changed = copy.deepcopy(self.qualification_input)
        changed["providerMatrixModel"] = "qwen3.5-fast"
        changed["sha256"] = cli.sha256_document(
            {key: item for key, item in changed.items() if key != "sha256"}
        )

        with (
            patch.object(cli, "RollbackStore", return_value=MagicMock()),
            patch.object(
                cli,
                "_modelport_qualification_preflight",
                return_value=("/srv/modelport", self.identity, changed),
            ),
            patch.object(
                cli,
                "_target_performance_preflight",
                return_value=self.performance,
            ),
            patch.object(
                cli,
                "run",
                side_effect=lambda argv, **_kwargs: RunResult(
                    tuple(argv), 0, "ok", ""
                ),
            ) as runner,
            self.assertRaisesRegex(IntegrityError, "qualification inputs changed"),
        ):
            cli._execute_upgrade_rollout(
                self.paths,
                transaction,
                copy.deepcopy(transaction.state),
                self.source,
                self.target,
                _RollbackSpec(),
                None,
                qualification="full",
                modelport_project_dir="/srv/modelport",
                qualification_input=self.qualification_input,
                performance_policy=self.performance,
            )

        self.assertNotIn("transition:production_stopping", events)
        self.assertNotIn(
            ["scripts/runtime.sh", "stop"],
            [list(call.args[0]) for call in runner.call_args_list],
        )


class BoundChildEnvironmentTests(unittest.TestCase):
    def test_bound_workload_replaces_ambient_alias_context_and_benchmark_values(
        self,
    ) -> None:
        script = ROOT / "scripts" / "acceptance-suite.sh"
        ambient = {
            "ANTHROPIC_MODEL": "attacker-alias",
            "QWEN_SERVED_MODEL_ID": "attacker-served-model",
            "TARGET_TOKENS": "1",
            "MAX_TOKENS": "1",
            "DECODE_BENCHMARK_TOKENS": "1",
            "DECODE_CONTEXT_TOKENS": "1",
            "BENCHMARK_CONCURRENCY": "1",
            "BENCHMARK_TOKENS": "1",
            "MODELPORT_AUTH_TOKEN": "ambient-secret",
            "MODELPORT_ENV_FILE": "/attacker/credentials.env",
        }
        bound = {
            "LOCAL_INFERENCE_PROVIDER_MATRIX_MODEL": "qwen3.5-code",
            "LOCAL_INFERENCE_SERVED_MODEL_ID": "reviewed-served-model",
            "LOCAL_INFERENCE_DIRECT_CONTEXT_TOKENS": "118000",
            "LOCAL_INFERENCE_MODELPORT_CONTEXT_TOKENS": "92000",
            "LOCAL_INFERENCE_MODELPORT_CONTEXT_MAX_TOKENS": "32768",
            "LOCAL_INFERENCE_DECODE_TOKENS": "512",
            "LOCAL_INFERENCE_DECODE_CONTEXT_TOKENS": "0",
            "LOCAL_INFERENCE_CONCURRENCY": "2",
            "LOCAL_INFERENCE_CONCURRENCY_TOKENS": "512",
        }
        keys = [*ambient, *bound]
        program = f"""
set -euo pipefail
source {script} help >/dev/null
BOUND_QUALIFICATION=true
BOUND_DIRECT_URL=http://127.0.0.1:18080
BOUND_MODELPORT_URL=http://127.0.0.1:38082
BOUND_PERFORMANCE_MANIFEST=deployments/reviewed/manifest.json
BOUND_PERFORMANCE_POLICY_SHA256={'a' * 64}
BOUND_MODELPORT_ENV_FILE=/private/modelport-credential.env
{chr(10).join(f'export {key}={value}' for key, value in {**ambient, **bound}.items())}
run_acceptance_child python3 - <<'PY'
import json
import os
print(json.dumps({{key: os.environ.get(key) for key in {keys!r}}}, sort_keys=True))
PY
"""
        result = subprocess.run(
            ["bash", "-c", program],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        child = json.loads(result.stdout.strip().splitlines()[-1])

        self.assertIsNone(child["ANTHROPIC_MODEL"])
        self.assertIsNone(child["MODELPORT_AUTH_TOKEN"])
        self.assertIsNone(child["MODELPORT_ENV_FILE"])
        for key in (
            "QWEN_SERVED_MODEL_ID",
            "TARGET_TOKENS",
            "MAX_TOKENS",
            "DECODE_BENCHMARK_TOKENS",
            "DECODE_CONTEXT_TOKENS",
            "BENCHMARK_CONCURRENCY",
            "BENCHMARK_TOKENS",
        ):
            self.assertIsNone(child[key], key)
        for key, value in bound.items():
            self.assertEqual(child[key], value, key)

    def test_only_explicit_modelport_child_receives_credential_file_capability(
        self,
    ) -> None:
        script = ROOT / "scripts" / "acceptance-suite.sh"
        credential_path = "/private/modelport-credential.env"
        program = f"""
set -euo pipefail
source {script} help >/dev/null
BOUND_QUALIFICATION=true
BOUND_DIRECT_URL=http://127.0.0.1:18080
BOUND_MODELPORT_URL=http://127.0.0.1:38082
BOUND_PERFORMANCE_MANIFEST=deployments/reviewed/manifest.json
BOUND_PERFORMANCE_POLICY_SHA256={'a' * 64}
BOUND_MODELPORT_ENV_FILE={credential_path}
export MODELPORT_AUTH_TOKEN=ambient-secret
run_modelport_acceptance_child python3 - <<'PY'
import json
import os
print(json.dumps({{
    "MODELPORT_ENV_FILE": os.environ.get("MODELPORT_ENV_FILE"),
    "MODELPORT_AUTH_TOKEN": os.environ.get("MODELPORT_AUTH_TOKEN"),
}}))
PY
"""
        result = subprocess.run(
            ["bash", "-c", program],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        child = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(child["MODELPORT_ENV_FILE"], credential_path)
        self.assertIsNone(child["MODELPORT_AUTH_TOKEN"])


class FrozenWorkloadConsumerTests(unittest.TestCase):
    @staticmethod
    def frozen_environment() -> dict[str, str]:
        return {
            "LOCAL_INFERENCE_BOUND_QUALIFICATION": "1",
            "LOCAL_INFERENCE_SERVED_MODEL_ID": "reviewed-served-model",
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
        }

    def test_context_decode_and_concurrency_import_only_frozen_values(self) -> None:
        environment = {
            **self.frozen_environment(),
            "CONTEXT_BACKEND": "llama",
            "QWEN_SERVED_MODEL_ID": "ambient-model",
            "TARGET_TOKENS": "1",
            "MAX_TOKENS": "1",
            "DECODE_BENCHMARK_TOKENS": "1",
            "DECODE_CONTEXT_TOKENS": "1",
            "BENCHMARK_CONCURRENCY": "1",
            "BENCHMARK_TOKENS": "1",
        }
        with patch.dict(os.environ, environment, clear=True):
            direct = runpy.run_path(
                str(ROOT / "scripts" / "context-acceptance.py"),
                run_name="bound_direct_context_fixture",
            )
            decode = runpy.run_path(
                str(ROOT / "scripts" / "decode-benchmark.py"),
                run_name="bound_decode_fixture",
            )
            concurrency = runpy.run_path(
                str(ROOT / "scripts" / "concurrency-benchmark.py"),
                run_name="bound_concurrency_fixture",
            )
        self.assertEqual(direct["SERVED_MODEL_ID"], "reviewed-served-model")
        self.assertEqual(direct["TARGET_TOKENS"], 118000)
        self.assertEqual(direct["MAX_TOKENS"], 512)
        self.assertEqual(decode["MODEL_ID"], "reviewed-served-model")
        self.assertEqual(decode["MAX_TOKENS"], 512)
        self.assertEqual(decode["CONTEXT_TOKENS"], 0)
        self.assertEqual(concurrency["MODEL_ID"], "reviewed-served-model")
        self.assertEqual(concurrency["CONCURRENCY"], 2)
        self.assertEqual(concurrency["MAX_TOKENS"], 512)

        environment["CONTEXT_BACKEND"] = "modelport"
        with patch.dict(os.environ, environment, clear=True):
            modelport = runpy.run_path(
                str(ROOT / "scripts" / "context-acceptance.py"),
                run_name="bound_modelport_context_fixture",
            )
        self.assertEqual(modelport["SERVED_MODEL_ID"], "reviewed-served-model")
        self.assertEqual(modelport["TARGET_TOKENS"], 92000)
        self.assertEqual(modelport["MAX_TOKENS"], 32768)

    def test_modelport_wrappers_load_only_explicit_file_and_frozen_workload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            fake_bin = temporary / "bin"
            fake_bin.mkdir()
            credential = temporary / "modelport.env"
            credential.write_text(
                "MODELPORT_AUTH_TOKEN=fixture-only\n", encoding="utf-8"
            )
            credential.chmod(0o600)
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-" ]]; then
  [[ "${3:-}" == "${MODELPORT_ENV_FILE:-}" ]]
  printf '%s' 'loaded-private-token'
  exit 0
fi
token_present=false
[[ -n "${MODELPORT_AUTH_TOKEN:-}" ]] && token_present=true
printf 'MODELPORT_ENV_FILE=%s\n' "${MODELPORT_ENV_FILE:-}"
printf 'MODELPORT_AUTH_TOKEN_PRESENT=%s\n' "$token_present"
printf 'MODELPORT_BASE_URL=%s\n' "${MODELPORT_BASE_URL:-}"
printf 'ANTHROPIC_BASE_URL=%s\n' "${ANTHROPIC_BASE_URL:-}"
printf 'CONTEXT_BACKEND=%s\n' "${CONTEXT_BACKEND:-}"
printf 'TARGET_TOKENS=%s\n' "${TARGET_TOKENS:-}"
printf 'MAX_TOKENS=%s\n' "${MAX_TOKENS:-}"
printf 'LOGICAL_FAST=%s\n' "${LOCAL_INFERENCE_LOGICAL_FAST_MODEL:-}"
printf 'LOGICAL_CODE=%s\n' "${LOCAL_INFERENCE_LOGICAL_CODE_MODEL:-}"
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o700)
            environment = {
                **self.frozen_environment(),
                "MODELPORT_ENV_FILE": str(credential),
                "MODELPORT_CONTEXT_URL": "http://attacker.invalid",
                "MODELPORT_CONTEXT_TARGET_TOKENS": "1",
                "MODELPORT_CONTEXT_MAX_TOKENS": "1",
                "ANTHROPIC_BASE_URL": "http://attacker.invalid",
                "PATH": f"{fake_bin}:/usr/bin:/bin",
            }

            observed: dict[str, dict[str, str]] = {}
            for script_name in (
                "modelport-context-acceptance.sh",
                "modelport-reasoning-smoke.sh",
            ):
                result = subprocess.run(
                    ["bash", str(ROOT / "scripts" / script_name)],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                observed[script_name] = dict(
                    line.split("=", 1)
                    for line in result.stdout.splitlines()
                    if "=" in line
                )

        context = observed["modelport-context-acceptance.sh"]
        self.assertEqual(context["MODELPORT_ENV_FILE"], str(credential))
        self.assertEqual(context["MODELPORT_AUTH_TOKEN_PRESENT"], "true")
        self.assertEqual(context["MODELPORT_BASE_URL"], "http://127.0.0.1:38082")
        self.assertEqual(context["CONTEXT_BACKEND"], "modelport")
        self.assertEqual(context["TARGET_TOKENS"], "92000")
        self.assertEqual(context["MAX_TOKENS"], "32768")
        reasoning = observed["modelport-reasoning-smoke.sh"]
        self.assertEqual(reasoning["MODELPORT_ENV_FILE"], str(credential))
        self.assertEqual(reasoning["MODELPORT_AUTH_TOKEN_PRESENT"], "true")
        self.assertEqual(reasoning["MODELPORT_BASE_URL"], "http://127.0.0.1:38082")
        self.assertEqual(reasoning["ANTHROPIC_BASE_URL"], "")
        self.assertEqual(reasoning["LOGICAL_FAST"], "qwen3.5-fast")
        self.assertEqual(reasoning["LOGICAL_CODE"], "qwen3.5-code")


if __name__ == "__main__":
    unittest.main()
