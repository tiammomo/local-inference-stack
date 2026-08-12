"""Tests for atomic, privacy-preserving acceptance evidence."""

from __future__ import annotations

import importlib.util
import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "acceptance_evidence", ROOT_DIR / "scripts" / "acceptance-evidence.py"
)
assert SPEC and SPEC.loader
ACCEPTANCE_EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCEPTANCE_EVIDENCE)
MODEL_MANAGER_SPEC = importlib.util.spec_from_file_location(
    "model_manager_for_evidence", ROOT_DIR / "scripts" / "model-manager.py"
)
assert MODEL_MANAGER_SPEC and MODEL_MANAGER_SPEC.loader
MODEL_MANAGER = importlib.util.module_from_spec(MODEL_MANAGER_SPEC)
MODEL_MANAGER_SPEC.loader.exec_module(MODEL_MANAGER)
from scripts import runtime_identity  # noqa: E402
from local_inference_stack.acceptance import (  # noqa: E402
    EVIDENCE_COMMON_KEYS,
    MODELPORT_SOURCE_IDENTITY_POLICY,
    ROLLOUT_BINDING_POLICY,
    ROLLOUT_FROZEN_INPUTS_POLICY,
    ROLLOUT_RUN_SCHEMA_VERSION,
    RUN_KIND,
    RUN_SCHEMA_VERSION,
    RUNNER_PATH,
    acceptance_evidence_shape_valid,
    rollout_frozen_inputs_valid,
)


class AcceptanceEvidenceTests(unittest.TestCase):
    @staticmethod
    def rollout_binding() -> dict[str, object]:
        return {
            "policyId": ROLLOUT_BINDING_POLICY,
            "transactionId": "12345678-1234-4234-8234-123456789abc",
            "operation": "upgrade",
            "rolloutPlanSha256": "1" * 64,
            "actionOrdinal": 6,
            "actionKind": "target-full",
            "rollbackSpecSha256": "2" * 64,
            "sourceCatalogSpecSha256": "3" * 64,
            "targetCatalogSpecSha256": "4" * 64,
            "performancePolicySha256": "5" * 64,
            "modelPortSourceIdentitySha256": "6" * 64,
            "qualificationInputSha256": "7" * 64,
        }

    @staticmethod
    def frozen_inputs(
        configuration: dict[str, object],
        binding: dict[str, object],
    ) -> dict[str, object]:
        materials = [
            {"path": path, "sha256": str(index) * 64}
            for index, path in enumerate(
                ACCEPTANCE_EVIDENCE.MODELPORT_MATERIAL_PATHS, start=1
            )
        ]
        modelport = {
            "policyId": MODELPORT_SOURCE_IDENTITY_POLICY,
            "gitCommit": "8" * 40,
            "gitTree": "9" * 40,
            "sourceState": "clean",
            "materials": materials,
            "materialsSha256": ACCEPTANCE_EVIDENCE.sha256_document(materials),
            "liveServiceIdentity": {
                "endpoint": "http://127.0.0.1:38082/livez",
                "service": "model-port",
                "status": "ok",
                "build": {
                    "revision": "8" * 40,
                    "sourceState": "clean",
                    "version": "1.2.3",
                    "configSha256": materials[0]["sha256"],
                },
            },
        }
        binding["modelPortSourceIdentitySha256"] = (
            ACCEPTANCE_EVIDENCE.sha256_document(modelport)
        )
        frozen = {
            "policyId": ROLLOUT_FROZEN_INPUTS_POLICY,
            "catalogModelId": "qwen35-9b-q5km",
            "targetCatalogSpecSha256": binding["targetCatalogSpecSha256"],
            "liveRuntimeIdentitySha256": "7" * 64,
            "runtimeIdentity": {
                "containerId": "d" * 64,
                "startedAt": "2026-08-13T00:00:00Z",
                "imageId": "sha256:" + "e" * 64,
                "containerConfigSha256": "7" * 64,
            },
            "acceptanceConfigurationSha256": ACCEPTANCE_EVIDENCE.sha256_document(
                configuration
            ),
            "controllerMaterialIdentity": {
                "materialPolicy": configuration["materialPolicy"],
                "fileSetMaterialPolicy": configuration["fileSetMaterialPolicy"],
                "controlPlanePackageSha256": configuration[
                    "controlPlanePackageSha256"
                ],
                "providerContractSha256": configuration["contractSha256"],
            },
            "modelPortSourceIdentity": modelport,
        }
        frozen["sha256"] = ACCEPTANCE_EVIDENCE.sha256_document(frozen)
        return frozen

    def test_atomic_writer_uses_restrictive_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            payload = {
                "schemaVersion": 2,
                "status": "passed",
                "privacy": "synthetic metadata only",
            }
            ACCEPTANCE_EVIDENCE.write_atomic(target, payload)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_no_replace_writer_refuses_record_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "logs" / "acceptance" / "evidence.json"
            with patch.object(ACCEPTANCE_EVIDENCE, "ROOT_DIR", root):
                ACCEPTANCE_EVIDENCE.write_noreplace(target, {"value": 1})
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    ACCEPTANCE_EVIDENCE.write_noreplace(target, {"value": 2})
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")), {"value": 1}
            )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_no_replace_writer_rejects_logs_parent_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir(mode=0o700)
            (root / "logs").symlink_to(outside, target_is_directory=True)
            target = root / "logs" / "acceptance" / "evidence.json"
            with patch.object(ACCEPTANCE_EVIDENCE, "ROOT_DIR", root), self.assertRaises(
                RuntimeError
            ):
                ACCEPTANCE_EVIDENCE.write_noreplace(target, {"value": 1})
            self.assertFalse((outside / "acceptance" / "evidence.json").exists())

    def test_bound_manifest_update_detects_parent_swap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "logs" / "acceptance"
            parent.mkdir(mode=0o700, parents=True)
            target = parent / "qualification-test.run.json"
            current = {"value": 1}
            current["selfSha256"] = ACCEPTANCE_EVIDENCE._manifest_self_hash(current)
            with patch.object(ACCEPTANCE_EVIDENCE, "ROOT_DIR", root):
                ACCEPTANCE_EVIDENCE.write_noreplace(target, current)
                original_open = ACCEPTANCE_EVIDENCE._open_project_parent
                calls = {"value": 0}

                def swapping_open(path: Path, project_root: Path, **kwargs):
                    calls["value"] += 1
                    if calls["value"] == 2:
                        parent.rename(root / "logs" / "detached")
                        parent.mkdir(mode=0o700)
                    return original_open(path, project_root, **kwargs)

                replacement = {"value": 2}
                replacement["selfSha256"] = (
                    ACCEPTANCE_EVIDENCE._manifest_self_hash(replacement)
                )
                with patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "_open_project_parent",
                    side_effect=swapping_open,
                ), self.assertRaisesRegex(RuntimeError, "directory changed"):
                    ACCEPTANCE_EVIDENCE._replace_bound_manifest(
                        target,
                        replacement,
                        expected_self_sha256=current["selfSha256"],
                    )
            self.assertFalse(target.exists())
            self.assertTrue((root / "logs" / "detached" / target.name).is_file())

    def test_evidence_subprocesses_do_not_inherit_authority(self) -> None:
        observed: dict[str, str] = {}

        def run(*_args, **kwargs):
            observed.update(kwargs["env"])
            return type("Result", (), {"stdout": "ok\n"})()

        authority = {
            key: "secret" for key in ACCEPTANCE_EVIDENCE.CHILD_AUTHORITY_ENV
        }
        with patch.dict(os.environ, {**authority, "SAFE_VALUE": "kept"}, clear=True), patch.object(
            ACCEPTANCE_EVIDENCE.subprocess, "run", side_effect=run
        ):
            self.assertEqual(ACCEPTANCE_EVIDENCE.command_output(["test"]), "ok")
        self.assertEqual(observed, {"NO_PROXY": "*", "no_proxy": "*"})

    def test_partial_rollout_authority_is_never_treated_as_standalone(self) -> None:
        with patch.dict(
            os.environ,
            {"QWEN_CONTROL_TRANSACTION_ID": "12345678-1234-4234-8234-123456789abc"},
            clear=True,
        ), self.assertRaisesRegex(RuntimeError, "incomplete"):
            ACCEPTANCE_EVIDENCE._rollout_authority()

    def test_evidence_and_embedded_run_reject_unknown_fields(self) -> None:
        run = {
            "schemaVersion": RUN_SCHEMA_VERSION,
            "kind": RUN_KIND,
            "runId": "fixture-run",
            "mode": "quick",
            "runner": {
                "path": RUNNER_PATH,
                "sha256": "a" * 64,
                "capabilitySha256": "b" * 64,
            },
            "plan": {"stepNames": [], "sha256": "c" * 64},
            "stepResults": [],
            "failedAtStep": None,
            "terminalStep": None,
            "manifest": {
                "schemaVersion": RUN_SCHEMA_VERSION,
                "sourcePath": "logs/acceptance/test.run.json",
                "sourceSha256": "d" * 64,
                "selfSha256": "e" * 64,
            },
        }
        evidence = {key: None for key in EVIDENCE_COMMON_KEYS}
        evidence.update({"schemaVersion": 4, "run": run})
        self.assertTrue(acceptance_evidence_shape_valid(evidence))
        with_extra = copy.deepcopy(evidence)
        with_extra["unreviewed"] = True
        self.assertFalse(acceptance_evidence_shape_valid(with_extra))
        run_extra = copy.deepcopy(evidence)
        run_extra["run"]["unreviewed"] = True
        self.assertFalse(acceptance_evidence_shape_valid(run_extra))

    def test_target_provider_preflight_binds_contract_target_and_prerequisites(self) -> None:
        catalog = ACCEPTANCE_EVIDENCE.load_catalog(ACCEPTANCE_EVIDENCE.CATALOG_PATH)
        model = ACCEPTANCE_EVIDENCE.model_by_id(catalog, catalog["defaultModel"])
        target = ACCEPTANCE_EVIDENCE.CatalogDeploymentSpec.from_catalog_model(model)
        contract = json.loads(
            ACCEPTANCE_EVIDENCE.PROVIDER_CONTRACT_PATH.read_text(encoding="utf-8")
        )
        configuration = {
            "materialPolicy": "materials-v1",
            "fileSetMaterialPolicy": "files-v1",
            "controlPlanePackageSha256": "a" * 64,
            "contractSha256": "b" * 64,
            "performancePolicySha256": "c" * 64,
        }
        source_identity = self.frozen_inputs(
            configuration, self.rollout_binding()
        )["modelPortSourceIdentity"]
        compatibility = {
            "contractId": contract["contractId"],
            "status": "passed",
            "summary": {"passed": 42, "failed": 0},
            "materialIdentity": {
                "contractSha256": "b" * 64,
                "configSha256": source_identity["materials"][0]["sha256"],
                "governanceSha256": {
                    "src/governance.rs": source_identity["materials"][3]["sha256"],
                    "src/routes.rs": source_identity["materials"][4]["sha256"],
                },
            },
        }
        dashboard = {"status": "ok"}
        def preflight(document: dict[str, object]) -> dict[str, object]:
            with (
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "_strict_json_material",
                    return_value=(document, "b" * 64),
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE.shutil,
                    "which",
                    return_value="/usr/bin/node",
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE, "command_output", return_value="24"
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "read_private_env_values",
                    return_value={"MODELPORT_AUTH_TOKEN": "private-token"},
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "_authenticated_modelport_registry",
                    return_value={"sha256": "d" * 64},
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "command_json",
                    side_effect=[compatibility, dashboard],
                ),
            ):
                return ACCEPTANCE_EVIDENCE._target_provider_qualification_identity(
                    catalog_model_id=model["id"],
                    catalog_spec_sha256=target.sha256,
                    modelport_project=Path("/reviewed/modelport"),
                    modelport_source_identity=source_identity,
                )

        identity = preflight(contract)
        self.assertEqual(identity["targetCatalogSpecSha256"], target.sha256)
        self.assertEqual(identity["providerContractSha256"], "b" * 64)
        self.assertEqual(identity["providerMatrixModel"], "qwen3.5-code")
        self.assertEqual(identity["toolUseMaxTokens"], 2048)
        self.assertEqual(
            identity["modelPortSourceIdentitySha256"],
            ACCEPTANCE_EVIDENCE.sha256_document(source_identity),
        )
        self.assertEqual(
            identity["sha256"],
            ACCEPTANCE_EVIDENCE.sha256_document(
                {key: value for key, value in identity.items() if key != "sha256"}
            ),
        )
        contract_mutations = {
            "Tool Choice": lambda value: value["capabilities"]["toolUse"].update(
                toolChoice=False
            ),
            "parallel Tool Use": lambda value: value["capabilities"][
                "toolUse"
            ].update(parallelToolCalls=False),
            "repair attempts": lambda value: value["capabilities"]["toolUse"][
                "repairInvalidArguments"
            ].update(maximumAttempts=2),
            "routing response header": lambda value: value["governance"].update(
                routingModeResponseHeader="x-substituted-mode"
            ),
            "forced local policy": lambda value: value["governance"].update(
                forcedLocalClassifications=["sensitive"]
            ),
            "local admission": lambda value: value["governance"][
                "localAdmission"
            ].update(executingPerUser=2),
        }
        for label, mutate in contract_mutations.items():
            with self.subTest(contract_field=label):
                changed = copy.deepcopy(contract)
                mutate(changed)
                with self.assertRaisesRegex(RuntimeError, "reviewed full-suite"):
                    preflight(changed)
        compatibility["materialIdentity"]["configSha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "provider contract"):
            preflight(contract)

    def test_qualification_preflight_is_read_only_and_omits_checkout_path(self) -> None:
        checkout = Path("/reviewed/modelport")
        materials = [
            {"path": path, "sha256": str(index) * 64}
            for index, path in enumerate(
                ACCEPTANCE_EVIDENCE.MODELPORT_MATERIAL_PATHS, start=1
            )
        ]
        identity = {
            "policyId": MODELPORT_SOURCE_IDENTITY_POLICY,
            "gitCommit": "a" * 40,
            "gitTree": "b" * 40,
            "sourceState": "clean",
            "materials": materials,
            "materialsSha256": ACCEPTANCE_EVIDENCE.sha256_document(materials),
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
        output = StringIO()
        qualification_input = {
            "policyId": "qualification-input-v1",
            "targetCatalogSpecSha256": "e" * 64,
            "providerContractId": "local-qwen-provider-v1",
            "providerContractSha256": "f" * 64,
            "servedModelId": "fixture-served-model",
            "limitsSha256": "1" * 64,
            "acceptanceSha256": "2" * 64,
            "modelPortSourceIdentitySha256": ACCEPTANCE_EVIDENCE.sha256_document(
                identity
            ),
        }
        qualification_input["sha256"] = ACCEPTANCE_EVIDENCE.sha256_document(
            qualification_input
        )
        with patch.object(
            sys,
            "argv",
            [
                "acceptance-evidence.py",
                "qualification-preflight",
                "--modelport-project",
                str(checkout),
                "--catalog-model-id",
                "fixture-target",
                "--catalog-spec-sha256",
                "e" * 64,
            ],
        ), patch.object(
            ACCEPTANCE_EVIDENCE,
            "_modelport_source_identity",
            return_value=identity,
        ) as capture, patch.object(
            ACCEPTANCE_EVIDENCE,
            "_target_provider_qualification_identity",
            return_value=qualification_input,
        ) as qualify, redirect_stdout(output):
            self.assertEqual(ACCEPTANCE_EVIDENCE.main(), 0)
        capture.assert_called_once_with(checkout)
        qualify.assert_called_once_with(
            catalog_model_id="fixture-target",
            catalog_spec_sha256="e" * 64,
            modelport_project=checkout,
            modelport_source_identity=identity,
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["schemaVersion"], 2)
        self.assertEqual(payload["qualificationInput"], qualification_input)
        self.assertNotIn(str(checkout), output.getvalue())

    def test_rollout_frozen_inputs_are_exact_and_privacy_preserving(self) -> None:
        configuration = {
            "acceptanceSuiteSha256": "a" * 64,
            "materialPolicy": "materials-v1",
            "fileSetMaterialPolicy": "files-v1",
            "controlPlanePackageSha256": "b" * 64,
            "contractSha256": "c" * 64,
            "performancePolicySha256": "5" * 64,
        }
        binding = self.rollout_binding()
        frozen = self.frozen_inputs(configuration, binding)
        self.assertTrue(
            rollout_frozen_inputs_valid(
                frozen,
                rollout_binding=binding,
                configuration=configuration,
            )
        )
        self.assertNotIn("projectPath", json.dumps(frozen))
        changed = copy.deepcopy(frozen)
        changed["modelPortSourceIdentity"]["liveServiceIdentity"]["build"][
            "revision"
        ] = "0" * 40
        changed["sha256"] = ACCEPTANCE_EVIDENCE.sha256_document(
            {key: value for key, value in changed.items() if key != "sha256"}
        )
        self.assertFalse(
            rollout_frozen_inputs_valid(
                changed,
                rollout_binding=binding,
                configuration=configuration,
            )
        )

    def test_modelport_identity_binds_clean_source_materials_and_live_build(self) -> None:
        commit = "a" * 40
        tree = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory).resolve()

            def git_output(command: list[str], timeout: int = 30) -> str | None:
                del timeout
                if command[-2:] == ["rev-parse", "HEAD"]:
                    return commit
                if command[-2:] == ["rev-parse", "HEAD^{tree}"]:
                    return tree
                if command[-2:] == ["rev-parse", "--show-toplevel"]:
                    return str(checkout)
                if command[-2] == "rev-parse" and command[-1].startswith("HEAD:"):
                    body = b"reviewed-modelport-material"
                    return hashlib.sha1(
                        f"blob {len(body)}\0".encode("ascii") + body
                    ).hexdigest()
                if "status" in command:
                    return ""
                self.fail(f"unexpected command: {command}")

            with (
                patch.dict(
                    os.environ, {"MODELPORT_PROJECT_DIR": str(checkout)}, clear=False
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE, "command_output", side_effect=git_output
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "command_json",
                    return_value={
                        "service": "model-port",
                        "status": "ok",
                        "build": {
                            "revision": commit,
                            "sourceState": "clean",
                            "version": "1.2.3",
                            "configSha256": hashlib.sha256(
                                b"reviewed-modelport-material"
                            ).hexdigest(),
                        },
                    },
                ),
                patch.object(ACCEPTANCE_EVIDENCE.os, "access", return_value=True),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "read_file_bytes",
                    return_value=b"reviewed-modelport-material",
                ),
            ):
                identity = ACCEPTANCE_EVIDENCE._modelport_source_identity()
            with (
                patch.dict(
                    os.environ, {"MODELPORT_PROJECT_DIR": str(checkout)}, clear=False
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "command_output",
                    side_effect=lambda command, timeout=30: (
                        " M config.toml"
                        if "status" in command
                        else git_output(command, timeout)
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, "clean Git checkout"),
            ):
                ACCEPTANCE_EVIDENCE._modelport_source_identity()
            with (
                patch.dict(
                    os.environ, {"MODELPORT_PROJECT_DIR": str(checkout)}, clear=False
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE, "command_output", side_effect=git_output
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "command_json",
                    return_value={
                        "service": "model-port",
                        "status": "ok",
                        "build": {
                            "revision": commit,
                            "sourceState": "clean",
                            "version": "1.2.3",
                            "configSha256": "0" * 64,
                        },
                    },
                ),
                patch.object(ACCEPTANCE_EVIDENCE.os, "access", return_value=True),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "read_file_bytes",
                    return_value=b"reviewed-modelport-material",
                ),
                self.assertRaisesRegex(RuntimeError, "live identity"),
            ):
                ACCEPTANCE_EVIDENCE._modelport_source_identity()
        self.assertEqual(identity["gitCommit"], commit)
        self.assertEqual(identity["gitTree"], tree)
        self.assertNotIn(str(checkout), json.dumps(identity))
        self.assertEqual(
            identity["liveServiceIdentity"]["build"]["revision"], commit
        )

    def test_bound_manifest_is_v2_transaction_named_and_no_replace(self) -> None:
        token = "d" * 64
        binding = self.rollout_binding()
        configuration = {
            "acceptanceSuiteSha256": ACCEPTANCE_EVIDENCE.sha256_file(
                ACCEPTANCE_EVIDENCE.SUITE_PATH
            ),
            "materialPolicy": "materials-v1",
            "fileSetMaterialPolicy": "files-v1",
            "controlPlanePackageSha256": "e" * 64,
            "contractSha256": "f" * 64,
            "performancePolicySha256": "5" * 64,
        }
        frozen = self.frozen_inputs(configuration, binding)
        catalog = {"schemaVersion": 1}
        model = {"id": "qwen35-9b-q5km"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = (
                root
                / "logs"
                / "acceptance"
                / "qualification-12345678-1234-4234-8234-123456789abc-6.run.json"
            )
            with (
                patch.dict(
                    os.environ,
                    {ACCEPTANCE_EVIDENCE.RUNNER_TOKEN_ENV: token},
                    clear=False,
                ),
                patch.object(ACCEPTANCE_EVIDENCE, "ROOT_DIR", root),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "reviewed_catalog_model",
                    return_value=(catalog, model),
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "acceptance_configuration",
                    return_value=configuration,
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "_pending_rollout_binding",
                    return_value=binding,
                ),
                patch.object(
                    ACCEPTANCE_EVIDENCE,
                    "_rollout_frozen_inputs",
                    return_value=frozen,
                ),
            ):
                manifest = ACCEPTANCE_EVIDENCE.initialize_manifest(
                    path,
                    mode="full",
                    profile="latency",
                    catalog_model_id="qwen35-9b-q5km",
                    started_at="2026-08-13T00:00:00Z",
                )
                self.assertEqual(
                    manifest["schemaVersion"], ROLLOUT_RUN_SCHEMA_VERSION
                )
                self.assertEqual(manifest["rolloutBinding"], binding)
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    ACCEPTANCE_EVIDENCE.initialize_manifest(
                        path,
                        mode="full",
                        profile="latency",
                        catalog_model_id="qwen35-9b-q5km",
                        started_at="2026-08-13T00:00:00Z",
                    )

    def test_gpu_inventory_parser_does_not_persist_host_identity(self) -> None:
        original = ACCEPTANCE_EVIDENCE.command_output
        try:
            ACCEPTANCE_EVIDENCE.command_output = lambda *_args, **_kwargs: (
                "0, NVIDIA Test GPU, 16384, 999.1"
            )
            self.assertEqual(
                ACCEPTANCE_EVIDENCE.gpu_inventory(),
                [
                    {
                        "index": 0,
                        "name": "NVIDIA Test GPU",
                        "vramGiB": 16.0,
                        "driver": "999.1",
                    }
                ],
            )
        finally:
            ACCEPTANCE_EVIDENCE.command_output = original

    def test_host_fingerprint_is_stable_and_does_not_expose_machine_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            machine_id = "0123456789abcdef0123456789abcdef"
            source = Path(directory) / "machine-id"
            source.write_text(machine_id + "\n", encoding="utf-8")
            fingerprint = ACCEPTANCE_EVIDENCE.host_fingerprint((source,))
            self.assertEqual(len(fingerprint or ""), 64)
            self.assertNotIn(machine_id, fingerprint or "")
            self.assertEqual(
                fingerprint,
                MODEL_MANAGER.host_fingerprint((source,)),
            )

    def test_writer_and_reader_hash_the_same_acceptance_dependencies(self) -> None:
        catalog = MODEL_MANAGER.load_catalog()
        model = MODEL_MANAGER.model_by_id(catalog, "qwen35-9b-q5km")
        configuration = ACCEPTANCE_EVIDENCE.acceptance_configuration(model)
        self.assertEqual(configuration, MODEL_MANAGER.acceptance_configuration(model))
        self.assertEqual(
            configuration["materialPolicy"],
            runtime_identity.ACCEPTANCE_MATERIAL_POLICY_ID,
        )
        self.assertEqual(
            configuration["fileSetMaterialPolicy"],
            runtime_identity.FILE_SET_SHA256_POLICY_ID,
        )

    def test_payload_digest_detects_changes(self) -> None:
        payload = {
            "schemaVersion": 3,
            "status": "passed",
            "durationSeconds": 12,
        }
        payload["selfSha256"] = ACCEPTANCE_EVIDENCE.payload_sha256(payload)
        self.assertEqual(
            payload["selfSha256"],
            MODEL_MANAGER.payload_sha256(payload),
        )
        payload["durationSeconds"] = 13
        self.assertNotEqual(
            payload["selfSha256"],
            MODEL_MANAGER.payload_sha256(payload),
        )

    def test_secure_reader_rejects_wide_permissions_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "evidence.json"
            ACCEPTANCE_EVIDENCE.write_atomic(target, {"schemaVersion": 3})
            with patch.object(MODEL_MANAGER, "ROOT_DIR", root):
                self.assertEqual(
                    MODEL_MANAGER.read_secure_evidence(target),
                    {"schemaVersion": 3},
                )
                target.chmod(0o644)
                self.assertIsNone(MODEL_MANAGER.read_secure_evidence(target))
                target.chmod(0o600)
                linked = root / "linked.json"
                linked.symlink_to(target)
                self.assertIsNone(MODEL_MANAGER.read_secure_evidence(linked))

    def test_catalog_evidence_reader_rejects_ancestor_symlink_escape(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(directory)
            outside = Path(outside_directory)
            (outside / "acceptance").mkdir(mode=0o700)
            evidence = outside / "acceptance" / "evidence.json"
            evidence.write_text('{"outside":true}\n', encoding="utf-8")
            evidence.chmod(0o600)
            (root / "logs").symlink_to(outside, target_is_directory=True)
            with patch.object(MODEL_MANAGER, "ROOT_DIR", root):
                self.assertIsNone(
                    MODEL_MANAGER.read_secure_evidence(
                        root / "logs" / "acceptance" / "evidence.json"
                    )
                )

    def test_validation_input_excludes_only_catalog_promotion_metadata(self) -> None:
        catalog = MODEL_MANAGER.load_catalog()
        model = copy.deepcopy(
            MODEL_MANAGER.model_by_id(catalog, "qwen35-9b-q5km")
        )
        configuration = {
            "acceptanceSuiteSha256": "a" * 64,
            "runtimeProfileSha256": "b" * 64,
            "catalogSha256": "c" * 64,
            "deploymentProfileSha256": "d" * 64,
            "effectiveComposeSha256": "e" * 64,
            "manifestSha256": "f" * 64,
        }
        original = ACCEPTANCE_EVIDENCE.validation_input(
            catalog, model, configuration
        )
        model["status"] = "validated"
        model["lifecycleRole"] = "candidate"
        model["deploymentEligibility"] = {
            "automatic": True,
            "reason": "signed",
        }
        model["validationAttestation"] = {"payloadSha256": "1" * 64}
        promoted = ACCEPTANCE_EVIDENCE.validation_input(
            catalog, model, {**configuration, "catalogSha256": "2" * 64}
        )
        self.assertEqual(original, promoted)
        model["runtime"]["contextTokens"] += 1
        changed = ACCEPTANCE_EVIDENCE.validation_input(
            catalog, model, configuration
        )
        self.assertNotEqual(original["sha256"], changed["sha256"])

    def test_validation_input_binds_the_complete_control_plane_package(self) -> None:
        catalog = MODEL_MANAGER.load_catalog()
        model = MODEL_MANAGER.model_by_id(catalog, "qwen35-9b-q5km")
        attestation_digest = {"value": "a" * 64}

        def fake_file_digest(path: Path) -> str:
            if path.name == "attestation.py":
                return attestation_digest["value"]
            return "b" * 64

        with (
            patch.object(runtime_identity, "sha256_file", side_effect=fake_file_digest),
            patch.object(
                runtime_identity,
                "rendered_compose_sha256",
                return_value="c" * 64,
            ),
        ):
            before_configuration = runtime_identity.acceptance_configuration(
                model, "full", "latency"
            )
            before = ACCEPTANCE_EVIDENCE.validation_input(
                catalog, model, before_configuration
            )
            attestation_digest["value"] = "d" * 64
            after_configuration = runtime_identity.acceptance_configuration(
                model, "full", "latency"
            )
            after = ACCEPTANCE_EVIDENCE.validation_input(
                catalog, model, after_configuration
            )

        self.assertNotEqual(
            before_configuration["controlPlanePackageSha256"],
            after_configuration["controlPlanePackageSha256"],
        )
        self.assertNotEqual(before["sha256"], after["sha256"])

    def test_acceptance_material_inventory_covers_the_control_plane(self) -> None:
        quick = runtime_identity.acceptance_snapshot_spec("quick", "latency")
        standard = runtime_identity.acceptance_snapshot_spec("standard", "latency")
        full = runtime_identity.acceptance_snapshot_spec("full", "latency")
        package_paths = {
            path.relative_to(ROOT_DIR).as_posix()
            for path in (ROOT_DIR / "src" / "local_inference_stack").glob("*.py")
        }

        quick.require_paths(ROOT_DIR, package_paths)
        self.assertTrue(
            set(quick.covered_paths(ROOT_DIR)).issubset(
                standard.covered_paths(ROOT_DIR)
            )
        )
        self.assertTrue(
            set(standard.covered_paths(ROOT_DIR)).issubset(full.covered_paths(ROOT_DIR))
        )
        self.assertEqual(
            quick.policy_id,
            runtime_identity.ACCEPTANCE_MATERIAL_POLICY_ID,
        )
        self.assertNotIn("scripts/unit-tests.sh", full.covered_paths(ROOT_DIR))
        self.assertNotIn("scripts/verify-models.sh", full.covered_paths(ROOT_DIR))

    def test_full_validation_input_binds_performance_threshold_changes(self) -> None:
        catalog = MODEL_MANAGER.load_catalog()
        model = MODEL_MANAGER.model_by_id(catalog, "qwen35-9b-q5km")
        policy = runtime_identity.resolved_performance_policy(model)
        changed_policy = copy.copy(policy)
        object.__setattr__(changed_policy, "policy_sha256", "c" * 64)
        with (
            patch.object(runtime_identity, "sha256_file", return_value="a" * 64),
            patch.object(
                runtime_identity,
                "rendered_compose_sha256",
                return_value="b" * 64,
            ),
            patch.object(
                runtime_identity,
                "resolved_performance_policy",
                side_effect=[policy, changed_policy],
            ),
        ):
            before_configuration = runtime_identity.acceptance_configuration(
                model, "full", "latency"
            )
            before = ACCEPTANCE_EVIDENCE.validation_input(
                catalog, model, before_configuration
            )
            after_configuration = runtime_identity.acceptance_configuration(
                model, "full", "latency"
            )
            after = ACCEPTANCE_EVIDENCE.validation_input(
                catalog, model, after_configuration
            )

        self.assertNotEqual(
            before_configuration["performancePolicySha256"],
            after_configuration["performancePolicySha256"],
        )
        self.assertNotEqual(before["sha256"], after["sha256"])

    def test_runner_manifest_requires_capability_and_exact_completed_plan(self) -> None:
        token = "1" * 64
        configuration = {"acceptanceSuiteSha256": "a" * 64}
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {ACCEPTANCE_EVIDENCE.RUNNER_TOKEN_ENV: token},
            clear=False,
        ), patch.object(
            ACCEPTANCE_EVIDENCE,
            "acceptance_configuration",
            return_value=configuration,
        ), patch.object(
            ACCEPTANCE_EVIDENCE, "ROOT_DIR", Path(directory)
        ):
            (Path(directory) / "logs" / "acceptance").mkdir(parents=True)
            target = Path(directory) / "logs" / "acceptance" / "test.run.json"
            ACCEPTANCE_EVIDENCE.initialize_manifest(
                target,
                mode="quick",
                profile="latency",
                catalog_model_id="qwen35-9b-q5km",
                started_at="2026-08-09T10:00:00Z",
            )
            with self.assertRaisesRegex(RuntimeError, "partial"):
                ACCEPTANCE_EVIDENCE.finalize_manifest(
                    target,
                    finished_at="2026-08-09T10:00:00Z",
                    duration_seconds=0,
                    exit_code=0,
                    failed_at_step="initialization",
                )
            for name in ACCEPTANCE_EVIDENCE.expected_steps("quick"):
                ACCEPTANCE_EVIDENCE.append_step(
                    target,
                    name=name,
                    started_at="2026-08-09T10:00:00Z",
                    finished_at="2026-08-09T10:00:00Z",
                    duration_seconds=0,
                    exit_code=0,
                )
            finalized = ACCEPTANCE_EVIDENCE.finalize_manifest(
                target,
                finished_at="2026-08-09T10:00:00Z",
                duration_seconds=0,
                exit_code=0,
                failed_at_step=ACCEPTANCE_EVIDENCE.expected_steps("quick")[-1],
            )
            self.assertEqual(finalized["status"], "passed")
            self.assertEqual(
                [step["name"] for step in finalized["stepResults"]],
                list(ACCEPTANCE_EVIDENCE.expected_steps("quick")),
            )
            with patch.dict(
                os.environ,
                {ACCEPTANCE_EVIDENCE.RUNNER_TOKEN_ENV: ""},
                clear=False,
            ), self.assertRaisesRegex(RuntimeError, "runner capability"):
                ACCEPTANCE_EVIDENCE._validated_manifest(target)

    def test_writer_has_no_direct_pass_status_interface(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "acceptance-evidence.py",
                "--output",
                "forged.json",
                "--mode",
                "full",
                "--status",
                "passed",
                "--exit-code",
                "0",
                "--failed-at-step",
                "Multi-step and adversarial Tool Use suite",
            ],
        ), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            ACCEPTANCE_EVIDENCE.parse_args()


if __name__ == "__main__":
    unittest.main()
