#!/usr/bin/env python3
"""Run synthetic ModelPort quality gates without persisting prompts or responses."""

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
DEFAULT_CASES = ROOT_DIR / "quality" / "cases.json"
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
    parser.add_argument("--smoke", action="store_true", help="run only smoke-tagged cases")
    parser.add_argument("--case", action="append", default=[], dest="case_ids")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    if args.trials < 1 or args.trials > 10:
        parser.error("--trials must be between 1 and 10")
    return args


def request_message(base_url: str, token: str, case: dict[str, Any]) -> tuple[dict[str, Any], float]:
    payload: dict[str, Any] = {
        "model": case["model"],
        "max_tokens": case["maxTokens"],
        "temperature": 0,
        "thinking": case["thinking"],
        "messages": [{"role": "user", "content": case["prompt"]}],
    }
    if case.get("tools"):
        payload["tools"] = case["tools"]
    if case.get("toolChoice"):
        payload["tool_choice"] = case["toolChoice"]
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/messages",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
            "x-modelport-traffic-class": "synthetic",
        },
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=600) as response:
        body = json.load(response)
    return body, (time.monotonic() - started) * 1000


def assert_response(body: dict[str, Any], assertion: dict[str, Any]) -> tuple[bool, str]:
    blocks = body.get("content", [])
    text = "".join(
        block.get("text", "") for block in blocks if block.get("type") == "text"
    ).strip()
    kind = assertion["type"]
    if kind == "exact":
        passed = text == assertion["value"]
        return passed, "exact text"
    if kind == "contains-all":
        missing = [value for value in assertion["values"] if value not in text]
        return not missing, "contains all markers" if not missing else f"missing {missing}"
    if kind == "json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return False, "invalid JSON"
        if not isinstance(value, dict):
            return False, "JSON is not an object"
        if any(key not in value for key in assertion.get("required", [])):
            return False, "required JSON key missing"
        if any(value.get(key) != expected for key, expected in assertion.get("equals", {}).items()):
            return False, "JSON value mismatch"
        return True, "valid JSON contract"
    if kind == "tool":
        matches = [
            block
            for block in blocks
            if block.get("type") == "tool_use" and block.get("name") == assertion["name"]
        ]
        if len(matches) != 1:
            return False, "expected exactly one declared tool call"
        tool_input = matches[0].get("input")
        if not isinstance(tool_input, dict):
            return False, "tool input is not an object"
        if any(key not in tool_input for key in assertion.get("required", [])):
            return False, "required tool input missing"
        return True, "valid tool call"
    return False, f"unknown assertion type {kind}"


def main() -> int:
    args = parse_args()
    load_env(DEFAULT_SECRETS)
    token = os.environ.get("MODELPORT_AUTH_TOKEN")
    if not token:
        raise SystemExit("MODELPORT_AUTH_TOKEN is required")
    base_url = os.environ.get("MODELPORT_BASE_URL", "http://127.0.0.1:38082")
    suite = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = suite["cases"]
    if args.smoke:
        cases = [case for case in cases if case.get("smoke")]
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [case for case in cases if case["id"] in wanted]
        missing = wanted - {case["id"] for case in cases}
        if missing:
            raise SystemExit(f"unknown case IDs: {sorted(missing)}")
    if not cases:
        raise SystemExit("no quality cases selected")

    results: list[dict[str, Any]] = []
    for case in cases:
        for trial in range(1, args.trials + 1):
            started = time.monotonic()
            try:
                body, latency_ms = request_message(base_url, token, case)
                passed, reason = assert_response(body, case["assert"])
                usage = body.get("usage", {})
                record = {
                    "caseId": case["id"],
                    "category": case["category"],
                    "trial": trial,
                    "passed": passed,
                    "reason": reason,
                    "latencyMs": round(latency_ms, 2),
                    "inputTokens": usage.get("input_tokens", 0),
                    "outputTokens": usage.get("output_tokens", 0),
                }
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
                record = {
                    "caseId": case["id"],
                    "category": case["category"],
                    "trial": trial,
                    "passed": False,
                    "reason": type(error).__name__,
                    "latencyMs": round((time.monotonic() - started) * 1000, 2),
                    "inputTokens": 0,
                    "outputTokens": 0,
                }
            results.append(record)
            marker = "PASS" if record["passed"] else "FAIL"
            print(f"[{marker}] {case['id']} trial={trial} {record['latencyMs']:.0f}ms {record['reason']}")

    passed = sum(1 for result in results if result["passed"])
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
        "privacy": "synthetic case IDs and aggregate outcomes only; prompts and responses omitted",
    }
    if not args.no_save:
        output_dir = ROOT_DIR / "logs" / "quality"
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = output_dir / f"{stamp}-{'smoke' if args.smoke else 'full'}.json"
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output.chmod(0o600)
        print(f"Quality evidence: {output}")
    print(f"Quality gate: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
