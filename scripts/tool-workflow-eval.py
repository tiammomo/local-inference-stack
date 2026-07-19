#!/usr/bin/env python3
"""Evaluate closed-loop Tool Use without persisting prompts, outputs, or arguments."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT_DIR / "quality" / "tool-workflows.json"
DEFAULT_SECRETS = ROOT_DIR / "profiles" / "operations.secrets.env"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value.strip().strip("'\""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--case", action="append", default=[], dest="case_ids")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    if args.trials < 1 or args.trials > 5:
        parser.error("--trials must be between 1 and 5")
    return args


def interpolate(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        if value.startswith("{") and value.endswith("}"):
            key = value[1:-1]
            if key in variables:
                return variables[key]
        return value.format_map(variables)
    if isinstance(value, list):
        return [interpolate(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: interpolate(item, variables) for key, item in value.items()}
    return value


def expand_cases(suite: dict[str, Any]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for family in suite["families"]:
        for variant in family["variants"]:
            case = {
                key: interpolate(value, variant)
                for key, value in family.items()
                if key != "variants"
            }
            case["id"] = f"{family['id']}-{variant['id']}"
            case["smoke"] = bool(variant.get("smoke"))
            expanded.append(case)
    return expanded


def request_message(
    base_url: str,
    token: str,
    model: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], float]:
    payload = {
        "model": model,
        "max_tokens": 1024,
        "temperature": 0,
        "thinking": {"type": "enabled", "budget_tokens": 256},
        "tools": tools,
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        "messages": messages,
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/messages",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response), (time.monotonic() - started) * 1000


def text_from(body: dict[str, Any]) -> str:
    return "".join(
        block.get("text", "")
        for block in body.get("content", [])
        if block.get("type") == "text"
    ).strip()


def assert_contains(text: str, values: list[str]) -> bool:
    folded = text.casefold()
    return all(str(value).casefold() in folded for value in values)


def execute_mock_tool(case: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    """Return the deterministic synthetic result only after exact dispatch validation."""
    if call.get("name") != case.get("expectedTool"):
        raise ValueError("unexpected tool selection")
    if call.get("input") != case.get("expectedInput"):
        raise ValueError("tool arguments do not match the scenario contract")
    result = case.get("toolResult")
    if not isinstance(result, dict):
        raise ValueError("synthetic tool returned a non-object result")
    return result


def evaluate_case(
    base_url: str,
    token: str,
    model: str,
    tools: list[dict[str, Any]],
    case: dict[str, Any],
) -> dict[str, Any]:
    first, first_ms = request_message(
        base_url, token, model, tools, [{"role": "user", "content": case["promptTemplate"]}]
    )
    calls = [block for block in first.get("content", []) if block.get("type") == "tool_use"]
    expected_tool = case.get("expectedTool")
    usage = first.get("usage", {})
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    if expected_tool is None:
        passed = not calls and assert_contains(text_from(first), case["finalContains"])
        return {
            "passed": passed,
            "stage": "direct_answer" if passed else "unexpected_tool_or_answer",
            "rounds": 1,
            "latencyMs": round(first_ms, 2),
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
        }
    if len(calls) != 1:
        return {
            "passed": False,
            "stage": "tool_call_count",
            "rounds": 1,
            "latencyMs": round(first_ms, 2),
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
        }
    call = calls[0]
    if (
        first.get("stop_reason") != "tool_use"
        or not isinstance(call.get("id"), str)
        or not call["id"]
    ):
        return {
            "passed": False,
            "stage": "tool_terminal_contract",
            "rounds": 1,
            "latencyMs": round(first_ms, 2),
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
        }
    try:
        tool_result = execute_mock_tool(case, call)
    except ValueError:
        return {
            "passed": False,
            "stage": "tool_selection_or_arguments",
            "rounds": 1,
            "latencyMs": round(first_ms, 2),
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
        }
    second, second_ms = request_message(
        base_url,
        token,
        model,
        tools,
        [
            {"role": "user", "content": case["promptTemplate"]},
            {"role": "assistant", "content": first["content"]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                ],
            },
        ],
    )
    second_calls = [
        block for block in second.get("content", []) if block.get("type") == "tool_use"
    ]
    usage = second.get("usage", {})
    input_tokens += int(usage.get("input_tokens") or 0)
    output_tokens += int(usage.get("output_tokens") or 0)
    passed = (
        not second_calls
        and second.get("stop_reason") not in {"tool_use", "max_tokens"}
        and assert_contains(text_from(second), case["finalContains"])
    )
    return {
        "passed": passed,
        "stage": "completed" if passed else "final_answer",
        "rounds": 2,
        "latencyMs": round(first_ms + second_ms, 2),
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
    }


def main() -> int:
    args = parse_args()
    load_env(DEFAULT_SECRETS)
    token = os.environ.get("MODELPORT_AUTH_TOKEN")
    if not token:
        raise SystemExit("MODELPORT_AUTH_TOKEN is required")
    base_url = os.environ.get("MODELPORT_BASE_URL", "http://127.0.0.1:38082")
    model = os.environ.get("TOOL_WORKFLOW_MODEL", "qwen3.5-code")
    suite = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = expand_cases(suite)
    if len(cases) < 40:
        raise SystemExit(f"expected at least 40 expanded cases, found {len(cases)}")
    if args.smoke:
        cases = [case for case in cases if case["smoke"]]
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [case for case in cases if case["id"] in wanted]
        missing = wanted - {case["id"] for case in cases}
        if missing:
            raise SystemExit(f"unknown case IDs: {sorted(missing)}")

    results: list[dict[str, Any]] = []
    for case in cases:
        for trial in range(1, args.trials + 1):
            started = time.monotonic()
            try:
                outcome = evaluate_case(base_url, token, model, suite["tools"], case)
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
                outcome = {
                    "passed": False,
                    "stage": type(error).__name__,
                    "rounds": 0,
                    "latencyMs": round((time.monotonic() - started) * 1000, 2),
                    "inputTokens": 0,
                    "outputTokens": 0,
                }
            record = {
                "caseId": case["id"],
                "category": case["category"],
                "trial": trial,
                **outcome,
            }
            results.append(record)
            marker = "PASS" if record["passed"] else "FAIL"
            print(
                f"[{marker}] {case['id']} trial={trial} rounds={record['rounds']} "
                f"{record['latencyMs']:.0f}ms stage={record['stage']}"
            )

    passed = sum(result["passed"] for result in results)
    latencies = [float(result["latencyMs"]) for result in results]
    evidence = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(UTC).isoformat(),
        "suite": str(args.cases.relative_to(ROOT_DIR)),
        "mode": "smoke" if args.smoke else "full",
        "trials": args.trials,
        "summary": {
            "passed": passed,
            "failed": len(results) - passed,
            "passRate": round(passed / len(results), 6),
            "medianLatencyMs": round(statistics.median(latencies), 2),
        },
        "results": results,
        "privacy": "synthetic IDs and aggregate outcomes only; prompts, model output, tool names, arguments, and results omitted",
    }
    if not args.no_save:
        output_dir = ROOT_DIR / "logs" / "quality"
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = output_dir / f"{stamp}-tool-workflow-{'smoke' if args.smoke else 'full'}.json"
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output.chmod(0o600)
        print(f"Tool workflow evidence: {output}")
    print(f"Tool workflow gate: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
