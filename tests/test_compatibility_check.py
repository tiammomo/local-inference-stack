from __future__ import annotations

import importlib.util
import json
import unittest
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "compatibility_check", ROOT / "scripts" / "compatibility-check.py"
)
assert SPEC and SPEC.loader
compatibility = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compatibility)


class CompatibilityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads(
            (ROOT / "contracts" / "local-qwen-provider-v1.json").read_text(
                encoding="utf-8"
            )
        )
        served = self.contract["runtime"]["servedModelId"]
        provider_id = self.contract["provider"]
        logical = self.contract["application"]["logicalModels"]
        self.config = {
            "providers": {
                provider_id: {
                    "protocol": "openai-compat",
                    "base_url": self.contract["runtime"]["baseUrl"],
                    "default_model": served,
                    "models": [served],
                    "passthrough_unknown_models": False,
                    "reasoning": {
                        "mode": "llama_cpp",
                        "default_enabled": self.contract["capabilities"]["reasoning"]
                        ["providerDefaultEnabled"],
                        "model_enabled": {
                            name: profile["reasoningDefaultEnabled"]
                            for name, profile in logical.items()
                        },
                        "model_budget_tokens": {
                            name: profile["reasoningBudgetTokens"]
                            for name, profile in logical.items()
                        },
                    },
                    "tool_use": {
                        "supported": True,
                        "tool_choice": True,
                        "parallel_tool_calls": True,
                        "streaming_arguments": "best_effort",
                        "response_validation": "strict",
                        "repair_invalid_arguments": True,
                    },
                    "token_counting": {
                        "mode": "anthropic",
                        "context_tokens": 131072,
                        "recommended_reasoning_input_tokens": 94208,
                        "model_recommended_input_tokens": {
                            name: profile["recommendedWorkingSetTokens"]
                            for name, profile in logical.items()
                        },
                        "max_output_tokens": 32768,
                        "model_max_output_tokens": {
                            name: profile["maxOutputTokens"]
                            for name, profile in logical.items()
                        },
                    },
                }
            },
            "aliases": {
                name: f"{provider_id}:{served}" for name in logical
            },
        }

    def test_matching_configuration_passes(self) -> None:
        checks = compatibility.evaluate_contract(self.contract, self.config)
        self.assertTrue(all(item["passed"] for item in checks))

    def test_material_reader_rejects_a_symlinked_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.toml"
            target.write_text("[providers]\n", encoding="utf-8")
            link = root / "config.toml"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "safely read"):
                compatibility.read_material(link, maximum_bytes=1024)

    def test_reasoning_or_tool_drift_fails(self) -> None:
        provider = self.config["providers"][self.contract["provider"]]
        provider["reasoning"]["model_enabled"]["qwen3.5-code"] = False
        provider["tool_use"]["response_validation"] = "best_effort"

        failed = [
            item
            for item in compatibility.evaluate_contract(self.contract, self.config)
            if not item["passed"]
        ]

        self.assertEqual(
            {item["name"] for item in failed},
            {"reasoning enabled qwen3.5-code", "Tool Use response validation"},
        )

    def test_output_limit_drift_fails(self) -> None:
        provider = self.config["providers"][self.contract["provider"]]
        provider["token_counting"]["max_output_tokens"] = 65536
        provider["token_counting"]["model_max_output_tokens"]["qwen3.5-code"] = 32768

        failed = {
            item["name"]
            for item in compatibility.evaluate_contract(self.contract, self.config)
            if not item["passed"]
        }

        self.assertEqual(
            failed,
            {"provider output limit", "output limit qwen3.5-code"},
        )

    def test_governance_source_contract_detects_missing_queue_or_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "src").mkdir()
            (project / "src" / "governance.rs").write_text(
                "DEFAULT_LOCAL_EXECUTING_PER_USER: usize = 1\n",
                encoding="utf-8",
            )
            (project / "src" / "routes.rs").write_text("", encoding="utf-8")

            checks = compatibility.evaluate_governance_source(
                self.contract,
                {
                    "src/governance.rs": (project / "src" / "governance.rs").read_bytes(),
                    "src/routes.rs": (project / "src" / "routes.rs").read_bytes(),
                },
            )

        failed = {item["name"] for item in checks if not item["passed"]}
        self.assertIn("routing request header", failed)
        self.assertIn("strict local wait", failed)
        self.assertNotIn("per-user executing limit", failed)


if __name__ == "__main__":
    unittest.main()
