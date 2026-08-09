"""Tests for atomic, privacy-preserving acceptance evidence."""

from __future__ import annotations

import importlib.util
import copy
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
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


class AcceptanceEvidenceTests(unittest.TestCase):
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
        performance = {
            "schemaVersion": 1,
            "policy": "enforced",
            "calibrationRuns": 3,
            "hardGates": {
                "decodeTokensPerSecond": {"minimum": 20.0},
                "aggregateTokensPerSecond": {"minimum": 35.0},
                "peakVramMiB": {"maximum": 15000.0},
            },
            "warningGates": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"

            def write_manifest() -> None:
                manifest.write_text(
                    json.dumps(
                        {
                            "model": {"catalogId": model["id"]},
                            "performance": performance,
                        }
                    ),
                    encoding="utf-8",
                )

            write_manifest()
            with (
                patch.object(runtime_identity, "MANIFEST_PATH", manifest),
                patch.object(
                    runtime_identity, "sha256_file", return_value="a" * 64
                ),
                patch.object(
                    runtime_identity,
                    "rendered_compose_sha256",
                    return_value="b" * 64,
                ),
            ):
                before_configuration = runtime_identity.acceptance_configuration(
                    model, "full", "latency"
                )
                before = ACCEPTANCE_EVIDENCE.validation_input(
                    catalog, model, before_configuration
                )
                performance["hardGates"]["decodeTokensPerSecond"]["minimum"] = 21.0
                write_manifest()
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
