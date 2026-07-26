"""Tests for catalog validation and deterministic host recommendations."""

from __future__ import annotations

import importlib.util
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "model_manager", ROOT_DIR / "scripts" / "model-manager.py"
)
assert SPEC and SPEC.loader
MODEL_MANAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODEL_MANAGER)


def host(
    vram: float,
    ram: float = 96,
    disk: float = 100,
    *,
    free_vram: float | None = None,
    name: str = "test",
) -> dict:
    available = vram if free_vram is None else free_vram
    return {
        "platform": "linux",
        "architecture": "x86_64",
        "gpus": (
            [
                {
                    "index": 0,
                    "name": name,
                    "vramGiB": vram,
                    "freeVramGiB": available,
                    "driver": "test-driver",
                }
            ]
            if vram
            else []
        ),
        "totalVramGiB": vram,
        "largestGpuVramGiB": vram,
        "totalFreeVramGiB": available,
        "largestFreeVramGiB": available,
        "ramGiB": ram,
        "freeDiskGiB": disk,
        "docker": {"available": True, "version": "test"},
        "dockerCompose": {"available": True, "version": "test"},
        "nvidiaContainerRuntime": True,
    }


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = MODEL_MANAGER.load_catalog()

    def test_catalog_ids_and_artifacts_are_unique(self) -> None:
        ids = [model["id"] for model in self.catalog["models"]]
        self.assertEqual(len(ids), len(set(ids)))
        for model in self.catalog["models"]:
            filenames = [artifact["filename"] for artifact in model["artifacts"]]
            self.assertEqual(len(filenames), len(set(filenames)))
            self.assertEqual(len(model["artifacts"]), 1)
            self.assertEqual(model["license"]["spdx"], "Apache-2.0")
            self.assertNotIn("/resolve/main/", model["artifacts"][0]["url"])
            self.assertIn(
                f"/resolve/{model['artifactRevision']}/",
                model["artifacts"][0]["url"],
            )
            runtime = model["runtime"]
            self.assertLess(
                runtime["recommendedInputTokens"] + runtime["maxOutputTokens"],
                runtime["contextTokens"],
            )

    def test_recommendation_boundaries(self) -> None:
        expected = {
            2: "qwen35-0.8b-q5km",
            4: "qwen35-2b-q5km",
            6: "qwen35-4b-q5km",
            10: "qwen35-9b-q4km",
            14: "qwen35-9b-q5km",
            22: "qwen35-27b-q4km",
            28: "qwen35-35b-a3b-q4km",
        }
        for vram, model_id in expected.items():
            with self.subTest(vram=vram):
                self.assertEqual(
                    MODEL_MANAGER.recommend(self.catalog, host(vram))["id"], model_id
                )

    def test_no_cuda_recommendation_below_catalog_floor(self) -> None:
        self.assertIsNone(MODEL_MANAGER.recommend(self.catalog, host(0)))

    def test_ram_and_disk_are_fail_closed(self) -> None:
        self.assertIsNone(MODEL_MANAGER.recommend(self.catalog, host(16, ram=4)))
        self.assertIsNone(MODEL_MANAGER.recommend(self.catalog, host(16, disk=1)))

    def test_generated_profile_contains_only_selected_catalog_values(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        environment = MODEL_MANAGER.deployment_env(model)
        self.assertIn("QWEN_CATALOG_ID=qwen35-9b-q5km", environment)
        self.assertIn("QWEN_MODEL_FILE=Qwen3.5-9B-Q5_K_M.gguf", environment)
        self.assertIn("QWEN_MODEL_DISPLAY_NAME='Qwen3.5-9B Q5_K_M'", environment)
        self.assertNotIn("MODELPORT_AUTH_TOKEN", environment)

    def test_validation_signature_is_a_hardware_profile_match(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        generic = host(16)
        self.assertFalse(
            MODEL_MANAGER.matches_validated_hardware_profile(model, generic)
        )
        generic["gpus"][0]["name"] = "NVIDIA GeForce RTX 5070 Ti"
        self.assertTrue(
            MODEL_MANAGER.matches_validated_hardware_profile(model, generic)
        )

    def test_multi_gpu_does_not_aggregate_vram_or_enable_automatic_deploy(self) -> None:
        assessed = host(16)
        assessed["gpus"].append(
            {
                "index": 1,
                "name": "test",
                "vramGiB": 16,
                "freeVramGiB": 16,
                "driver": "test-driver",
            }
        )
        assessed["totalVramGiB"] = 32
        assessed["totalFreeVramGiB"] = 32
        args = Namespace(model=None)
        plan = MODEL_MANAGER.plan_for_host(args, self.catalog, assessed)
        self.assertEqual(plan["recommendation"]["id"], "qwen35-9b-q5km")
        self.assertFalse(plan["automaticDeploymentSupported"])
        self.assertFalse(plan["readyToDeploy"])
        self.assertEqual(plan["nextCommands"], [])

    def test_busy_gpu_suppresses_deployment_commands(self) -> None:
        assessed = host(
            16,
            free_vram=3,
            name="NVIDIA GeForce RTX 5070 Ti",
        )
        args = Namespace(model=None)
        plan = MODEL_MANAGER.plan_for_host(args, self.catalog, assessed)
        self.assertTrue(plan["fits"])
        self.assertFalse(plan["resourceAvailableNow"])
        self.assertFalse(plan["readyToDeploy"])
        self.assertEqual(plan["nextCommands"], [])

    def test_plan_distinguishes_profile_match_from_host_acceptance(self) -> None:
        assessed = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        args = Namespace(model=None)
        plan = MODEL_MANAGER.plan_for_host(args, self.catalog, assessed)
        self.assertEqual(
            plan["evidenceStatus"], "validated-hardware-profile-match"
        )
        self.assertEqual(
            plan["hostAcceptanceStatus"], "not-evaluated-by-read-only-plan"
        )
        self.assertEqual(plan["catalogEvidenceStatus"], "validated-profile")
        self.assertTrue(plan["artifactPolicy"]["licenseReviewRequired"])

    def test_upstream_header_parser_normalizes_hugging_face_evidence(self) -> None:
        headers = MODEL_MANAGER.parse_http_headers(
            'HTTP/2 302\nX-Repo-Commit: abc123\nX-Linked-Etag: "def456"\n'
        )
        self.assertEqual(headers["x-repo-commit"], "abc123")
        self.assertEqual(headers["x-linked-etag"], "def456")

    def test_current_configuration_acceptance_is_host_bound(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        assessed = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        artifact = model["artifacts"][0]
        now = datetime(2026, 7, 26, 10, 1, tzinfo=timezone.utc)
        evidence = {
            "schemaVersion": 3,
            "evidenceId": "test",
            "status": "passed",
            "exitCode": 0,
            "failedAtStep": None,
            "terminalStep": "Direct reasoning",
            "durationSeconds": 60,
            "mode": "quick",
            "startedAt": "2026-07-26T09:59:00+00:00",
            "finishedAt": "2026-07-26T10:00:00+00:00",
            "catalogModelId": model["id"],
            "host": {
                "platform": "linux",
                "architecture": "x86_64",
                "ramGiB": 96,
                "gpus": [
                    {
                        "index": 0,
                        "name": "NVIDIA GeForce RTX 5070 Ti",
                        "vramGiB": 16,
                        "driver": "test-driver",
                    }
                ],
                "fingerprintType": MODEL_MANAGER.HOST_FINGERPRINT_TYPE,
                "fingerprint": MODEL_MANAGER.host_fingerprint(),
            },
            "artifact": {
                "filename": artifact["filename"],
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
                "modelRevision": model["modelRevision"],
                "artifactRevision": model["artifactRevision"],
                "integrityVerified": True,
            },
            "runtime": {
                "configuredImage": MODEL_MANAGER.configured_runtime_image(),
                "imageId": "sha256:test",
            },
            "configuration": MODEL_MANAGER.acceptance_configuration(model),
            "freshnessPolicy": {"maxAgeDays": 30, "futureSkewSeconds": 300},
        }
        evidence["selfSha256"] = MODEL_MANAGER.payload_sha256(evidence)
        self.assertTrue(
            MODEL_MANAGER.acceptance_matches_host(
                model, assessed, evidence, now=now
            )
        )
        acceptance = {
            "status": "passed-current-configuration",
            "evidence": "logs/acceptance/test.json",
            "finishedAt": evidence["finishedAt"],
            "mode": "quick",
        }
        plan = MODEL_MANAGER.plan_for_host(
            Namespace(model=None), self.catalog, assessed, acceptance
        )
        self.assertEqual(plan["evidenceStatus"], "validated-on-this-host")
        self.assertEqual(
            plan["hostAcceptanceStatus"], "passed-current-configuration"
        )

        evidence["host"]["gpus"][0]["driver"] = "changed-driver"
        evidence["selfSha256"] = MODEL_MANAGER.payload_sha256(evidence)
        self.assertFalse(
            MODEL_MANAGER.acceptance_matches_host(
                model, assessed, evidence, now=now
            )
        )

    def test_acceptance_expires_and_rejects_future_timestamps(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        assessed = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        artifact = model["artifacts"][0]
        now = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)
        evidence = {
            "schemaVersion": 3,
            "evidenceId": "test",
            "status": "passed",
            "exitCode": 0,
            "failedAtStep": None,
            "terminalStep": "Direct reasoning",
            "durationSeconds": 60,
            "mode": "quick",
            "startedAt": (
                now - timedelta(days=31, minutes=1)
            ).isoformat(),
            "finishedAt": (now - timedelta(days=31)).isoformat(),
            "catalogModelId": model["id"],
            "host": {
                "platform": "linux",
                "architecture": "x86_64",
                "ramGiB": 96,
                "gpus": assessed["gpus"],
                "fingerprintType": MODEL_MANAGER.HOST_FINGERPRINT_TYPE,
                "fingerprint": MODEL_MANAGER.host_fingerprint(),
            },
            "artifact": {
                "filename": artifact["filename"],
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
                "modelRevision": model["modelRevision"],
                "artifactRevision": model["artifactRevision"],
                "integrityVerified": True,
            },
            "runtime": {
                "configuredImage": MODEL_MANAGER.configured_runtime_image(),
                "imageId": "sha256:test",
            },
            "configuration": MODEL_MANAGER.acceptance_configuration(model),
            "freshnessPolicy": {"maxAgeDays": 30, "futureSkewSeconds": 300},
        }
        evidence["selfSha256"] = MODEL_MANAGER.payload_sha256(evidence)
        self.assertFalse(
            MODEL_MANAGER.acceptance_matches_host(
                model, assessed, evidence, now=now
            )
        )

        evidence["startedAt"] = now.isoformat()
        evidence["finishedAt"] = (now + timedelta(minutes=6)).isoformat()
        evidence["durationSeconds"] = 360
        evidence["selfSha256"] = MODEL_MANAGER.payload_sha256(evidence)
        self.assertFalse(
            MODEL_MANAGER.acceptance_matches_host(
                model, assessed, evidence, now=now
            )
        )


if __name__ == "__main__":
    unittest.main()
