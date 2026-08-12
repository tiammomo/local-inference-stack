"""Pure tests for manifest-backed performance gates and baseline eligibility."""

from __future__ import annotations

import json
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from local_inference_stack.performance import (  # noqa: E402
    PerformancePolicyError,
    evaluate,
    load_bound_policy,
    load_execution_policy,
    load_policy,
    resolve_catalog_performance_policy,
)


class PerformancePolicyTests(unittest.TestCase):
    def manifest(self, document: dict) -> Path:
        path = Path(self.temporary.name) / "manifest.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def enforced_manifest(self, **overrides: object) -> Path:
        performance = {
            "policy": "enforced",
            "calibrationRuns": 3,
            "hardGates": {
                "decodeTokensPerSecond": {"minimum": 20.0},
                "aggregateTokensPerSecond": {"minimum": 35.0},
                "peakVramMiB": {"maximum": 15000.0},
            },
            "warningGates": {
                "ttftMs": {"maximum": 250.0},
                "prefillTokensPerSecond": {"minimum": 100.0},
            },
        }
        performance.update(overrides)
        return self.manifest({"schemaVersion": 1, "performance": performance})

    @staticmethod
    def catalog_model() -> dict:
        return json.loads(
            (ROOT_DIR / "catalog" / "models.json").read_text(encoding="utf-8")
        )["models"][0]

    @staticmethod
    def reviewed_manifest(model: dict, *, status: str = "enforced") -> dict:
        artifact = next(
            item
            for item in model["artifacts"]
            if item["role"] == "model" and item["required"] is True
        )
        performance = {
            "schemaVersion": 1,
            "policy": status,
            "calibrationRuns": 3 if status == "enforced" else 0,
            "warningGates": {},
        }
        if status == "enforced":
            performance["hardGates"] = {
                "decodeTokensPerSecond": {"minimum": 20.0},
                "aggregateTokensPerSecond": {"minimum": 35.0},
                "peakVramMiB": {"maximum": 15000.0},
            }
        return {
            "schemaVersion": 2,
            "model": {
                "catalogId": model["id"],
                "servedModelId": model["servedModelId"],
                "weightQuantization": model["quantization"],
                "officialRepository": model["modelRepository"],
                "modelRevision": model["modelRevision"],
                "artifactRepository": model["artifactRepository"],
                "artifactRevision": model["artifactRevision"],
                "artifactFilename": artifact["filename"],
                "artifactBytes": artifact["bytes"],
                "artifactSha256": artifact["sha256"],
                "licenseSpdx": model["license"]["spdx"],
                "licenseReviewRequired": model["license"]["reviewRequired"],
            },
            "performance": performance,
        }

    def write_reviewed_policy(
        self, model: dict, *, status: str = "enforced"
    ) -> tuple[Path, Path]:
        root = Path(self.temporary.name)
        relative = Path("deployments/fixture-target/manifest.json")
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(self.reviewed_manifest(model, status=status)),
            encoding="utf-8",
        )
        model["performancePolicy"] = {
            "schemaVersion": 1,
            "manifestPath": relative.as_posix(),
        }
        return root, relative

    def test_absent_policy_is_pending_and_normal_evidence_is_ineligible(self) -> None:
        policy = load_policy(self.manifest({"schemaVersion": 1}))
        self.assertEqual(policy.status, "pending-baseline")
        result = evaluate(
            policy,
            {"decodeTokensPerSecond": 20.0, "peakVramMiB": 14000.0},
            required_metrics={"decodeTokensPerSecond", "peakVramMiB"},
        )
        self.assertTrue(result["measurementsComplete"])
        self.assertFalse(result["benchmarkPassed"])
        self.assertFalse(result["promotionEligible"])
        self.assertEqual(
            result["evidenceEligibility"], "ineligible-pending-baseline"
        )

    def test_pending_policy_allows_complete_baseline_but_never_promotion(self) -> None:
        policy = load_policy(self.manifest({"schemaVersion": 1}))
        result = evaluate(
            policy,
            {"aggregateTokensPerSecond": 40.0, "peakVramMiB": 14500.0},
            required_metrics={"aggregateTokensPerSecond", "peakVramMiB"},
            baseline_only=True,
        )
        self.assertTrue(result["measurementsComplete"])
        self.assertTrue(result["benchmarkPassed"])
        self.assertFalse(result["promotionEligible"])
        self.assertEqual(
            result["evidenceEligibility"], "baseline-only-not-promotable"
        )

    def test_pending_baseline_requires_all_stable_measurements(self) -> None:
        policy = load_policy(self.manifest({"schemaVersion": 1}))
        result = evaluate(
            policy,
            {"decodeTokensPerSecond": 20.0, "peakVramMiB": None},
            required_metrics={"decodeTokensPerSecond", "peakVramMiB"},
            baseline_only=True,
        )
        self.assertFalse(result["measurementsComplete"])
        self.assertFalse(result["benchmarkPassed"])
        self.assertIn("peakVramMiB", " ".join(result["reasons"]))

    def test_hard_gate_boundaries_are_inclusive(self) -> None:
        policy = load_policy(self.enforced_manifest())
        result = evaluate(
            policy,
            {
                "decodeTokensPerSecond": 20.0,
                "aggregateTokensPerSecond": 35.0,
                "peakVramMiB": 15000.0,
                "ttftMs": 250.0,
                "prefillTokensPerSecond": 100.0,
            },
            required_metrics={
                "decodeTokensPerSecond",
                "aggregateTokensPerSecond",
                "peakVramMiB",
            },
        )
        self.assertTrue(result["hardGatesPassed"])
        self.assertTrue(result["benchmarkPassed"])
        self.assertTrue(result["promotionEligible"])
        self.assertTrue(all(item["passed"] for item in result["hardGateResults"]))

    def test_each_hard_gate_fails_just_outside_its_boundary(self) -> None:
        policy = load_policy(self.enforced_manifest())
        cases = {
            "decodeTokensPerSecond": 19.999,
            "aggregateTokensPerSecond": 34.999,
            "peakVramMiB": 15000.001,
        }
        for metric, value in cases.items():
            with self.subTest(metric=metric):
                metrics = {
                    "decodeTokensPerSecond": 20.0,
                    "aggregateTokensPerSecond": 35.0,
                    "peakVramMiB": 15000.0,
                }
                metrics[metric] = value
                result = evaluate(
                    policy,
                    metrics,
                    required_metrics=set(metrics),
                )
                self.assertFalse(result["hardGatesPassed"])
                self.assertFalse(result["benchmarkPassed"])
                self.assertFalse(result["promotionEligible"])

    def test_noisy_metric_violations_are_warning_only(self) -> None:
        policy = load_policy(self.enforced_manifest())
        result = evaluate(
            policy,
            {
                "decodeTokensPerSecond": 21.0,
                "peakVramMiB": 14900.0,
                "ttftMs": 500.0,
                "prefillTokensPerSecond": 50.0,
            },
            required_metrics={"decodeTokensPerSecond", "peakVramMiB"},
        )
        self.assertTrue(result["benchmarkPassed"])
        self.assertTrue(result["promotionEligible"])
        self.assertEqual(len(result["warningResults"]), 2)
        self.assertTrue(
            all(item["severity"] == "warning" for item in result["warningResults"])
        )
        self.assertTrue(all(not item["passed"] for item in result["warningResults"]))

    def test_baseline_only_never_promotes_even_with_enforced_gates(self) -> None:
        policy = load_policy(self.enforced_manifest())
        result = evaluate(
            policy,
            {"decodeTokensPerSecond": 21.0, "peakVramMiB": 14900.0},
            required_metrics={"decodeTokensPerSecond", "peakVramMiB"},
            baseline_only=True,
        )
        self.assertTrue(result["benchmarkPassed"])
        self.assertFalse(result["promotionEligible"])
        self.assertEqual(
            result["evidenceEligibility"], "baseline-only-not-promotable"
        )

    def test_enforced_policy_requires_calibration_and_all_hard_gates(self) -> None:
        with self.subTest("too few calibration runs"):
            with self.assertRaises(PerformancePolicyError):
                load_policy(self.enforced_manifest(calibrationRuns=2))
        with self.subTest("missing hard gate"):
            with self.assertRaises(PerformancePolicyError):
                load_policy(
                    self.enforced_manifest(
                        hardGates={
                            "decodeTokensPerSecond": {"minimum": 20.0},
                            "peakVramMiB": {"maximum": 15000.0},
                        }
                    )
                )

    def test_catalog_policy_resolution_binds_target_manifest_and_exact_gates(self) -> None:
        model = self.catalog_model()
        root, relative = self.write_reviewed_policy(model)
        resolved = resolve_catalog_performance_policy(root, model)
        self.assertEqual(resolved.manifest_relative_path, relative.as_posix())
        self.assertTrue(resolved.policy.enforced)
        self.assertRegex(resolved.manifest_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(resolved.policy_sha256, r"^[0-9a-f]{64}$")
        self.assertTrue(
            load_bound_policy(
                root=root,
                model=model,
                manifest_path=relative,
                expected_policy_sha256=resolved.policy_sha256,
            ).enforced
        )

        document = json.loads((root / relative).read_text(encoding="utf-8"))
        document["performance"]["hardGates"]["decodeTokensPerSecond"][
            "minimum"
        ] += 1
        (root / relative).write_text(json.dumps(document), encoding="utf-8")
        changed = resolve_catalog_performance_policy(root, model)
        self.assertNotEqual(resolved.policy_sha256, changed.policy_sha256)

    def test_catalog_policy_resolution_fails_closed_before_execution(self) -> None:
        model = self.catalog_model()
        root, relative = self.write_reviewed_policy(model, status="pending-baseline")
        self.assertEqual(
            resolve_catalog_performance_policy(
                root, model, require_enforced=False
            ).policy.status,
            "pending-baseline",
        )
        with self.assertRaisesRegex(PerformancePolicyError, "not enforced"):
            resolve_catalog_performance_policy(root, model)

        enforced = self.reviewed_manifest(model)
        enforced["model"]["servedModelId"] = "wrong-target"
        (root / relative).write_text(json.dumps(enforced), encoding="utf-8")
        with self.assertRaisesRegex(PerformancePolicyError, "does not match"):
            resolve_catalog_performance_policy(root, model)

        missing = copy.deepcopy(model)
        missing.pop("performancePolicy")
        with self.assertRaisesRegex(PerformancePolicyError, "no exact"):
            resolve_catalog_performance_policy(root, missing)

    def test_bound_execution_rejects_wrong_path_digest_and_missing_binding(self) -> None:
        model = self.catalog_model()
        root, relative = self.write_reviewed_policy(model)
        catalog_directory = root / "catalog"
        catalog_directory.mkdir()
        catalog = {"schemaVersion": 1, "defaultModel": model["id"], "models": [model]}
        # The strict Catalog loader has additional top-level policy fields; reuse
        # the checked-in document while replacing only its executable model.
        catalog = json.loads(
            (ROOT_DIR / "catalog" / "models.json").read_text(encoding="utf-8")
        )
        catalog["models"] = [model]
        catalog["defaultModel"] = model["id"]
        (catalog_directory / "models.json").write_text(
            json.dumps(catalog), encoding="utf-8"
        )
        resolved = resolve_catalog_performance_policy(root, model)
        with self.assertRaises(PerformancePolicyError):
            load_bound_policy(
                root=root,
                model=model,
                manifest_path=Path("deployments/other/manifest.json"),
                expected_policy_sha256=resolved.policy_sha256,
            )
        with self.assertRaises(PerformancePolicyError):
            load_bound_policy(
                root=root,
                model=model,
                manifest_path=relative,
                expected_policy_sha256="0" * 64,
            )
        with self.assertRaisesRegex(PerformancePolicyError, "requires"):
            load_execution_policy(
                root=root,
                manifest_path=relative,
                expected_policy_sha256=None,
                catalog_id=model["id"],
                require_binding=True,
            )
        self.assertTrue(
            load_execution_policy(
                root=root,
                manifest_path=relative,
                expected_policy_sha256=resolved.policy_sha256,
                catalog_id=model["id"],
                require_binding=True,
            ).enforced
        )

    def test_catalog_policy_manifest_symlinks_are_rejected(self) -> None:
        model = self.catalog_model()
        root = Path(self.temporary.name)
        outside = root / "outside.json"
        outside.write_text(json.dumps(self.reviewed_manifest(model)), encoding="utf-8")
        path = root / "deployments" / "fixture-target" / "manifest.json"
        path.parent.mkdir(parents=True)
        path.symlink_to(outside)
        model["performancePolicy"] = {
            "schemaVersion": 1,
            "manifestPath": "deployments/fixture-target/manifest.json",
        }
        with self.assertRaisesRegex(PerformancePolicyError, "safely read"):
            resolve_catalog_performance_policy(root, model)


if __name__ == "__main__":
    unittest.main()
