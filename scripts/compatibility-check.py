#!/usr/bin/env python3
"""Validate the versioned local-Qwen contract against a ModelPort checkout."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError as error:  # pragma: no cover - depends on host Python
    raise SystemExit("compatibility-check.py requires Python 3.11 or newer") from error


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT_DIR / "contracts" / "local-qwen-provider-v1.json"
DEFAULT_MANIFEST = (
    ROOT_DIR / "deployments" / "qwen3.5-9b-rtx5070ti" / "manifest.json"
)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def git_output(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()


def evaluate_contract(
    contract: dict[str, Any], modelport_config: dict[str, Any]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def check(name: str, actual: Any, expected: Any, passed: bool | None = None) -> None:
        checks.append(
            {
                "name": name,
                "passed": actual == expected if passed is None else passed,
                "actual": actual,
                "expected": expected,
            }
        )

    provider_id = contract["provider"]
    runtime = contract["runtime"]
    application = contract["application"]
    capabilities = contract["capabilities"]
    limits = contract["limits"]
    providers = modelport_config.get("providers", {})
    provider = providers.get(provider_id, {})
    served_model = runtime["servedModelId"]

    check("provider exists", provider_id in providers, True)
    check("provider protocol", provider.get("protocol"), "openai-compat")
    check("runtime base URL", provider.get("base_url"), runtime["baseUrl"])
    check("served model", provider.get("default_model"), served_model)
    configured_models = provider.get("models", [])
    check(
        "served model is allowlisted",
        served_model in configured_models,
        True,
    )
    check("unknown model passthrough", provider.get("passthrough_unknown_models"), False)

    aliases = modelport_config.get("aliases", {})
    expected_target = f"{provider_id}:{served_model}"
    logical_models = application.get("logicalModels", {})
    for alias, profile in logical_models.items():
        check(f"alias {alias}", aliases.get(alias), expected_target)
        reasoning = provider.get("reasoning", {})
        check(
            f"reasoning enabled {alias}",
            reasoning.get("model_enabled", {}).get(alias),
            profile.get("reasoningDefaultEnabled"),
        )
        check(
            f"reasoning budget {alias}",
            reasoning.get("model_budget_tokens", {}).get(alias),
            profile.get("reasoningBudgetTokens"),
        )

    reasoning = provider.get("reasoning", {})
    check("reasoning adapter", reasoning.get("mode"), "llama_cpp")
    check(
        "reasoning provider fallback",
        reasoning.get("default_enabled"),
        capabilities["reasoning"]["providerDefaultEnabled"],
    )

    tool_use = provider.get("tool_use", {})
    tool_contract = capabilities["toolUse"]
    check("Tool Use enabled", tool_use.get("supported"), tool_contract["supported"])
    check(
        "Tool Choice enabled",
        tool_use.get("tool_choice"),
        tool_contract["toolChoice"],
    )
    check(
        "parallel Tool Use",
        tool_use.get("parallel_tool_calls"),
        tool_contract["parallelToolCalls"],
    )
    check(
        "streaming Tool Use arguments",
        tool_use.get("streaming_arguments"),
        tool_contract["streamingArguments"],
    )
    check(
        "Tool Use response validation",
        tool_use.get("response_validation"),
        tool_contract["responseValidation"],
    )
    check(
        "Tool Use repair policy",
        tool_use.get("repair_invalid_arguments"),
        tool_contract["repairInvalidArguments"]["enabled"],
    )

    token_counting = provider.get("token_counting", {})
    check("token counting mode", token_counting.get("mode"), "anthropic")
    check(
        "context limit",
        token_counting.get("context_tokens"),
        limits["contextTokens"],
    )
    check(
        "reasoning input recommendation",
        token_counting.get("recommended_reasoning_input_tokens"),
        limits["recommendedReasoningInputTokens"],
    )
    model_input_limits = token_counting.get("model_recommended_input_tokens", {})
    for alias, profile in logical_models.items():
        check(
            f"recommended input {alias}",
            model_input_limits.get(alias),
            profile.get("recommendedWorkingSetTokens"),
        )
    check(
        "provider output limit",
        token_counting.get("max_output_tokens"),
        limits["maxOutputTokens"],
    )
    model_output_limits = token_counting.get("model_max_output_tokens", {})
    for alias, profile in logical_models.items():
        check(
            f"output limit {alias}",
            model_output_limits.get(alias),
            profile.get("maxOutputTokens"),
        )
    return checks


def evaluate_governance_source(
    contract: dict[str, Any], modelport_project: Path
) -> list[dict[str, Any]]:
    governance = contract["governance"]
    source_paths = [
        modelport_project / "src" / "governance.rs",
        modelport_project / "src" / "routes.rs",
    ]
    source = "\n".join(
        path.read_text(encoding="utf-8") if path.is_file() else ""
        for path in source_paths
    )
    expected_fragments = {
        "routing request header": governance["routingModeHeader"],
        "routing response header": governance["routingModeResponseHeader"],
        "classification header": governance["classificationHeader"],
        "per-user executing limit": "DEFAULT_LOCAL_EXECUTING_PER_USER: usize = 1",
        "per-user queue limit": "DEFAULT_LOCAL_QUEUED_PER_USER: usize = 2",
        "global interactive queue": "DEFAULT_LOCAL_QUEUE_GLOBAL: usize = 16",
        "hybrid overflow": "DEFAULT_OVERFLOW_AFTER: Duration = Duration::from_secs(5)",
        "strict local wait": "DEFAULT_STRICT_WAIT: Duration = Duration::from_secs(60)",
        "batch traffic class": '"batch" => Some(Self::Batch)',
    }
    return [
        {
            "name": name,
            "passed": fragment in source,
            "actual": "present" if fragment in source else "missing",
            "expected": "present",
        }
        for name, fragment in expected_fragments.items()
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="compare ModelPort configuration and release state with the local-Qwen contract"
    )
    parser.add_argument("--modelport-project", type=Path, required=True)
    parser.add_argument(
        "--modelport-config",
        type=Path,
        help=(
            "configuration to validate; relative paths are resolved inside "
            "--modelport-project (default: config.toml)"
        ),
    )
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--release",
        action="store_true",
        help="also require clean repositories and the manifest-pinned ModelPort commit",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    modelport_project = args.modelport_project.resolve()
    config_path = args.modelport_config or Path("config.toml")
    if not config_path.is_absolute():
        config_path = modelport_project / config_path
    config_path = config_path.resolve()
    if not config_path.is_file():
        raise SystemExit(f"ModelPort config not found: {config_path}")

    contract = load_json(args.contract)
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    checks = evaluate_contract(contract, config)
    checks.extend(evaluate_governance_source(contract, modelport_project))

    if args.release:
        manifest = load_json(args.manifest)
        expected_commit = manifest["gateway"]["sourceCommit"]
        actual_commit = git_output(modelport_project, "rev-parse", "HEAD")
        modelport_status = git_output(modelport_project, "status", "--porcelain=v1")
        local_status = git_output(ROOT_DIR, "status", "--porcelain=v1")
        checks.extend(
            [
                {
                    "name": "ModelPort source commit",
                    "passed": actual_commit == expected_commit,
                    "actual": actual_commit,
                    "expected": expected_commit,
                },
                {
                    "name": "ModelPort worktree clean",
                    "passed": not modelport_status,
                    "actual": "clean" if not modelport_status else "dirty",
                    "expected": "clean",
                },
                {
                    "name": "local inference worktree clean",
                    "passed": not local_status,
                    "actual": "clean" if not local_status else "dirty",
                    "expected": "clean",
                },
            ]
        )

    failed = [item for item in checks if not item["passed"]]
    result = {
        "schemaVersion": 1,
        "contractId": contract["contractId"],
        "mode": "release" if args.release else "configuration",
        "status": "passed" if not failed else "failed",
        "summary": {"passed": len(checks) - len(failed), "failed": len(failed)},
        "inputs": {
            "contract": str(args.contract.resolve()),
            "modelportProject": str(modelport_project),
            "modelportConfig": str(config_path),
        },
        "checks": checks,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            marker = "PASS" if item["passed"] else "FAIL"
            print(f"[{marker}] {item['name']}: {item['actual']}")
        print(
            f"\nCompatibility check {result['status']}: "
            f"{result['summary']['passed']} passed, {result['summary']['failed']} failed"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
