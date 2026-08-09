"""Pure tests for manifest-backed performance gates and baseline eligibility."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from local_inference_stack.performance import (  # noqa: E402
    PerformancePolicyError,
    evaluate,
    load_policy,
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


if __name__ == "__main__":
    unittest.main()
