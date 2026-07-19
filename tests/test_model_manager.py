"""Tests for catalog validation and deterministic host recommendations."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "model_manager", ROOT_DIR / "scripts" / "model-manager.py"
)
assert SPEC and SPEC.loader
MODEL_MANAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODEL_MANAGER)


def host(vram: float, ram: float = 96, disk: float = 100) -> dict:
    return {
        "platform": "linux",
        "architecture": "x86_64",
        "gpus": ([{"index": 0, "name": "test", "vramGiB": vram}] if vram else []),
        "totalVramGiB": vram,
        "largestGpuVramGiB": vram,
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

    def test_validation_signature_is_host_specific(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        generic = host(16)
        self.assertFalse(MODEL_MANAGER.validated_on_host(model, generic))
        generic["gpus"][0]["name"] = "NVIDIA GeForce RTX 5070 Ti"
        self.assertTrue(MODEL_MANAGER.validated_on_host(model, generic))


if __name__ == "__main__":
    unittest.main()
