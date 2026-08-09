"""Tests for the package-native immutable deployment boundary."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_inference_stack.catalog import load_catalog  # noqa: E402
from local_inference_stack.deployment import (  # noqa: E402
    CatalogDeploymentSpec,
    DeploymentSpecError,
    build_deployment_plan,
    parse_deployment_plan,
)


class DeploymentSpecTests(unittest.TestCase):
    @staticmethod
    def model() -> dict:
        catalog = load_catalog(ROOT / "catalog" / "models.json")
        return catalog["models"][0]

    def test_catalog_record_freezes_into_deterministic_neutral_identity(self) -> None:
        model = self.model()
        spec = CatalogDeploymentSpec.from_catalog_model(model)
        reordered = {key: model[key] for key in reversed(tuple(model))}
        second = CatalogDeploymentSpec.from_catalog_model(reordered)
        self.assertEqual(spec.document(), second.document())
        self.assertEqual(spec.sha256, second.sha256)
        self.assertEqual(spec.catalog_id, "qwen35-9b-q5km")
        self.assertNotIn("QWEN", json.dumps(spec.document()))

    def test_identity_commits_to_reviewed_admission_license_and_provenance(self) -> None:
        model = self.model()
        baseline = CatalogDeploymentSpec.from_catalog_model(model)
        model["license"]["metadataVerifiedAt"] = "2026-08-08"
        changed = CatalogDeploymentSpec.from_catalog_model(model)
        self.assertNotEqual(baseline.reviewed_model_sha256, changed.reviewed_model_sha256)
        self.assertNotEqual(baseline.sha256, changed.sha256)

    def test_plan_is_typed_ordered_and_contains_no_executable_text(self) -> None:
        spec = CatalogDeploymentSpec.from_catalog_model(self.model())
        plan = build_deployment_plan(
            spec,
            admission_granted=True,
        ).document()
        self.assertEqual(
            [action["kind"] for action in plan["actions"]],
            ["fetch-artifact", "activate-spec", "start-runtime", "quick-smoke"],
        )
        encoded = json.dumps(plan, sort_keys=True)
        self.assertNotIn("command", encoded.lower())
        self.assertNotIn("./scripts", encoded)

    def test_actions_are_impossible_without_admission(self) -> None:
        spec = CatalogDeploymentSpec.from_catalog_model(self.model())
        with self.assertRaisesRegex(DeploymentSpecError, "explicit admission"):
            build_deployment_plan(
                spec, admission_granted=False
            )

    def test_unsafe_identity_fails_closed(self) -> None:
        model = self.model()
        spec = CatalogDeploymentSpec.from_catalog_model(model)
        model["modelDirectory"] = "../escape"
        with self.assertRaisesRegex(DeploymentSpecError, "model directory"):
            CatalogDeploymentSpec.from_catalog_model(model)

    def test_untrusted_plan_must_bind_identity_order_and_full_lifecycle(self) -> None:
        spec = CatalogDeploymentSpec.from_catalog_model(self.model())
        document = build_deployment_plan(
            spec,
            admission_granted=True,
        ).document()
        parsed = parse_deployment_plan(
            spec, document, require_full_lifecycle=True
        )
        self.assertEqual(parsed.document(), document)
        document["actions"].reverse()
        with self.assertRaisesRegex(DeploymentSpecError, "out of order"):
            parse_deployment_plan(spec, document, require_full_lifecycle=True)


if __name__ == "__main__":
    unittest.main()
