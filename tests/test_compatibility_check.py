from __future__ import annotations

import importlib.util
import json
import unittest
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
                        "parallel_tool_calls": True,
                        "response_validation": "strict",
                        "repair_invalid_arguments": True,
                    },
                    "token_counting": {
                        "mode": "anthropic",
                        "context_tokens": 131072,
                        "recommended_reasoning_input_tokens": 94208,
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


if __name__ == "__main__":
    unittest.main()
