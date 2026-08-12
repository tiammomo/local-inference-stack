"""Tests for the package-native immutable deployment boundary."""

from __future__ import annotations

import json
import copy
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
    build_rollback_rollout_plan,
    build_upgrade_rollout_plan,
    parse_deployment_plan,
    parse_rollout_plan,
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

    def test_upgrade_plan_binds_both_subjects_without_executable_text(self) -> None:
        source_model = self.model()
        source_model["lifecycleRole"] = "rollback"
        source_model["deploymentEligibility"] = {
            "automatic": False,
            "reason": "explicit-rollback-only",
        }
        source = CatalogDeploymentSpec.from_catalog_model(source_model)
        target_model = self.model()
        target_model["id"] = "qwen35-9b-next"
        target_model["servedModelId"] = "qwen3.5-9b-next"
        target_model["modelDirectory"] = "qwen3.5-9b-next"
        target = CatalogDeploymentSpec.from_catalog_model(target_model)
        rollback_sha256 = "a" * 64

        plan = build_upgrade_rollout_plan(
            source,
            target,
            rollback_sha256,
            admission_granted=True,
        )
        parsed = parse_rollout_plan(
            plan.document(),
            rollback_spec_sha256=rollback_sha256,
            source=source,
            target=target,
        )
        self.assertEqual(parsed.document(), plan.document())
        self.assertEqual(
            [item["ordinal"] for item in plan.document()["actions"]],
            list(range(len(plan.actions))),
        )
        encoded = json.dumps(plan.document(), sort_keys=True).lower()
        for forbidden in ("command", "argv", "shell", "url", "environment"):
            self.assertNotIn(forbidden, encoded)

        tampered = copy.deepcopy(plan.document())
        tampered["actions"][0]["catalogId"] = target.catalog_id
        with self.assertRaisesRegex(DeploymentSpecError, "immutable subjects"):
            parse_rollout_plan(
                tampered,
                rollback_spec_sha256=rollback_sha256,
                source=source,
                target=target,
            )

    def test_rollback_plan_is_one_shot_and_exact(self) -> None:
        anchor = CatalogDeploymentSpec.from_catalog_model(self.model())
        current_model = self.model()
        current_model["id"] = "qwen35-9b-next"
        current_model["servedModelId"] = "qwen3.5-9b-next"
        current_model["modelDirectory"] = "qwen3.5-9b-next"
        current = CatalogDeploymentSpec.from_catalog_model(current_model)
        plan = build_rollback_rollout_plan(
            current,
            anchor,
            "b" * 64,
            admission_granted=True,
        )
        self.assertEqual(
            [item["kind"] for item in plan.document()["actions"]],
            [
                "stop-source",
                "activate-target",
                "start-target",
                "target-quick",
                "clear-rollback",
            ],
        )
        self.assertEqual(
            parse_rollout_plan(
                plan.document(),
                rollback_spec_sha256="b" * 64,
                source=current,
                target=anchor,
            ).sha256,
            plan.sha256,
        )

    def test_full_upgrade_is_an_exact_v2_qualification_plan(self) -> None:
        source_model = self.model()
        source = CatalogDeploymentSpec.from_catalog_model(source_model)
        target_model = self.model()
        target_model["id"] = "qwen35-9b-full"
        target_model["servedModelId"] = "qwen3.5-9b-full"
        target_model["modelDirectory"] = "qwen3.5-9b-full"
        target = CatalogDeploymentSpec.from_catalog_model(target_model)
        plan = build_upgrade_rollout_plan(
            source,
            target,
            "c" * 64,
            admission_granted=True,
            required_acceptance_tier="full",
            performance_policy_sha256="d" * 64,
            modelport_source_identity_sha256="e" * 64,
            qualification_input_sha256="f" * 64,
        )
        document = plan.document()

        self.assertEqual(document["schemaVersion"], 2)
        self.assertEqual(document["requiredAcceptanceTier"], "full")
        self.assertEqual(
            document["qualification"],
            {
                "policyId": (
                    "local-inference-stack/transaction-bound-full-qualification-v1"
                ),
                "mode": "full",
                "profile": "latency",
                "recordEvidence": True,
                "runSchemaVersion": 2,
                "evidenceSchemaVersion": 5,
                "performancePolicySha256": "d" * 64,
                "modelPortSourceIdentitySha256": "e" * 64,
                "qualificationInputSha256": "f" * 64,
            },
        )
        kinds = [action["kind"] for action in document["actions"]]
        self.assertEqual(kinds[-3:], ["target-quick", "target-full", "publish-rollback"])
        self.assertEqual(
            parse_rollout_plan(
                document,
                rollback_spec_sha256="c" * 64,
                source=source,
                target=target,
            ).document(),
            document,
        )

        for mutation in (
            lambda value: value.update(schemaVersion=True),
            lambda value: value["qualification"].update(evidenceSchemaVersion=4),
            lambda value: value["actions"].__setitem__(
                -2, value["actions"][-3]
            ),
            lambda value: value.update(unreviewed=True),
        ):
            tampered = copy.deepcopy(document)
            mutation(tampered)
            with self.assertRaises(DeploymentSpecError):
                parse_rollout_plan(
                    tampered,
                    rollback_spec_sha256="c" * 64,
                    source=source,
                    target=target,
                )

        for missing in (
            {},
            {"performance_policy_sha256": "d" * 64},
            {"modelport_source_identity_sha256": "e" * 64},
            {
                "performance_policy_sha256": "d" * 64,
                "modelport_source_identity_sha256": "e" * 64,
            },
        ):
            with self.assertRaisesRegex(DeploymentSpecError, "requires"):
                build_upgrade_rollout_plan(
                    source,
                    target,
                    "c" * 64,
                    admission_granted=True,
                    required_acceptance_tier="full",
                    **missing,
                )

        with self.assertRaisesRegex(DeploymentSpecError, "quick upgrade"):
            build_upgrade_rollout_plan(
                source,
                target,
                "c" * 64,
                admission_granted=True,
                performance_policy_sha256="d" * 64,
                modelport_source_identity_sha256="e" * 64,
                qualification_input_sha256="f" * 64,
            )

    def test_quick_upgrade_and_rollback_keep_the_v1_shape(self) -> None:
        source = CatalogDeploymentSpec.from_catalog_model(self.model())
        target_model = self.model()
        target_model["id"] = "qwen35-9b-next"
        target_model["servedModelId"] = "qwen3.5-9b-next"
        target_model["modelDirectory"] = "qwen3.5-9b-next"
        target = CatalogDeploymentSpec.from_catalog_model(target_model)
        for plan in (
            build_upgrade_rollout_plan(
                source, target, "d" * 64, admission_granted=True
            ),
            build_rollback_rollout_plan(
                target, source, "d" * 64, admission_granted=True
            ),
        ):
            document = plan.document()
            self.assertEqual(document["schemaVersion"], 1)
            self.assertEqual(document["requiredAcceptanceTier"], "quick")
            self.assertNotIn("qualification", document)
            self.assertNotIn(
                "target-full", [action["kind"] for action in document["actions"]]
            )


if __name__ == "__main__":
    unittest.main()
