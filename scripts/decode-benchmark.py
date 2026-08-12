#!/usr/bin/env python3
"""Run a bounded decode benchmark without printing prompts or model output."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from local_inference_stack.performance import (  # noqa: E402
    DEFAULT_MANIFEST,
    PeakVramSampler,
    PerformancePolicyError,
    default_evidence_path,
    evaluate,
    load_execution_policy,
    write_private_json,
)

try:
    from scripts.local_http import direct_urlopen
except ModuleNotFoundError:
    from local_http import direct_urlopen


BASE_URL = os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:18080")
BOUND_QUALIFICATION = os.environ.get("LOCAL_INFERENCE_BOUND_QUALIFICATION") == "1"
if BOUND_QUALIFICATION:
    MAX_TOKENS = int(os.environ["LOCAL_INFERENCE_DECODE_TOKENS"])
    CONTEXT_TOKENS = int(os.environ["LOCAL_INFERENCE_DECODE_CONTEXT_TOKENS"])
    MODEL_ID = os.environ["LOCAL_INFERENCE_SERVED_MODEL_ID"]
    TOPIC = "designing reliable local LLM inference services"
else:
    MAX_TOKENS = int(os.environ.get("DECODE_BENCHMARK_TOKENS", "512"))
    CONTEXT_TOKENS = int(os.environ.get("DECODE_CONTEXT_TOKENS", "0"))
    MODEL_ID = os.environ.get("QWEN_SERVED_MODEL_ID", "qwen3.5-9b-q5km")
    TOPIC = os.environ.get(
        "DECODE_BENCHMARK_TOPIC", "designing reliable local LLM inference services"
    )


def post(path: str, payload: dict[str, Any], timeout: int = 1800) -> dict[str, Any]:
    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with direct_urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    if not isinstance(body, dict):
        raise RuntimeError(f"{path} returned a non-object JSON response")
    return body


def token_count(text: str) -> int:
    tokens = post("/tokenize", {"content": text}).get("tokens")
    if not isinstance(tokens, list):
        raise RuntimeError("tokenize response did not contain a token list")
    return len(tokens)


def build_prompt() -> str:
    instruction = (
        "Write a detailed technical guide of at least 2000 words about "
        f"{TOPIC}. Do not stop before covering architecture, performance, "
        "reliability, observability, security, and testing."
    )
    if CONTEXT_TOKENS <= 0:
        return instruction

    paragraph = (
        "Background material for a long-context decode benchmark. "
        "Treat this as reference context and write the requested guide afterward. "
        "The quick brown fox jumps over the lazy dog. "
    )
    unit_tokens = token_count(paragraph)
    if unit_tokens <= 0:
        raise RuntimeError("tokenizer returned zero tokens for benchmark material")
    prompt = paragraph * max(1, CONTEXT_TOKENS // unit_tokens)
    while token_count(prompt) < CONTEXT_TOKENS:
        prompt += paragraph
    return prompt + "\n\n" + instruction


def _positive_float(value: Any) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return None
    return float(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(
            os.environ.get(
                "LOCAL_INFERENCE_PERFORMANCE_MANIFEST",
                os.fspath(DEFAULT_MANIFEST),
            )
        ),
    )
    parser.add_argument(
        "--expected-policy-sha256",
        default=os.environ.get("LOCAL_INFERENCE_PERFORMANCE_POLICY_SHA256"),
        help="require the Catalog-mapped reviewed policy with this canonical digest",
    )
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="collect non-promotable measurements while policy thresholds are pending",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="private JSON evidence path (default: logs/performance)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    return parser.parse_args(argv)


def _emit(document: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
        return
    for key in (
        "performancePolicy",
        "evidenceEligibility",
        "promotionEligible",
        "benchmarkPassed",
        "evidencePath",
    ):
        if key in document:
            print(f"{key}={document[key]}")
    metrics = document.get("measurements")
    if isinstance(metrics, dict):
        for key, value in metrics.items():
            print(f"{key}={value}")
    for warning in document.get("warningResults", []):
        if not warning.get("passed"):
            print(
                "warning="
                f"{warning['metric']} {warning['reason']} "
                f"actual={warning['actual']} threshold={warning['threshold']}"
            )
    for reason in document.get("reasons", []):
        print(f"reason={reason}")
    if "error" in document:
        print(f"error={document['error']}")


def _policy_preflight(args: argparse.Namespace) -> tuple[Any | None, int | None]:
    try:
        policy = load_execution_policy(
            root=ROOT_DIR,
            manifest_path=args.manifest,
            expected_policy_sha256=args.expected_policy_sha256,
            catalog_id=os.environ.get("QWEN_CATALOG_ID"),
            require_binding=(
                os.environ.get("LOCAL_INFERENCE_BOUND_QUALIFICATION") == "1"
            ),
        )
    except PerformancePolicyError as exc:
        _emit(
            {
                "schemaVersion": 1,
                "performancePolicy": "invalid-policy",
                "evidenceEligibility": "ineligible-invalid-policy",
                "promotionEligible": False,
                "error": str(exc),
            },
            as_json=args.json,
        )
        return None, 2
    if not policy.enforced and not args.baseline_only:
        _emit(policy.summary(), as_json=args.json)
        return None, 3
    return policy, None


def _benchmark(policy: Any, *, baseline_only: bool) -> dict[str, Any]:
    sampler = PeakVramSampler()
    sampler.start()
    try:
        prompt = build_prompt()
        request_prompt_tokens = token_count(prompt)
        started = time.monotonic()
        body = post(
            "/v1/chat/completions",
            {
                "model": MODEL_ID,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": MAX_TOKENS,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        elapsed = time.monotonic() - started
    finally:
        peak_vram_mib = sampler.stop()

    usage = body.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    completion_tokens = int(usage.get("completion_tokens", 0))
    server_prompt_tokens = int(usage.get("prompt_tokens", 0))
    timings = body.get("timings")
    if not isinstance(timings, dict):
        timings = {}
    decode_tokens_per_second = (
        completion_tokens / elapsed if elapsed > 0 and completion_tokens > 0 else None
    )
    metrics = {
        "decodeTokensPerSecond": decode_tokens_per_second,
        "peakVramMiB": peak_vram_mib,
        # Non-streaming wall time cannot distinguish queueing, prefill, and TTFT.
        "ttftMs": None,
        "prefillTokensPerSecond": _positive_float(timings.get("prompt_per_second")),
    }
    evaluation = evaluate(
        policy,
        metrics,
        required_metrics={"decodeTokensPerSecond", "peakVramMiB"},
        baseline_only=baseline_only,
    )
    correctness_passed = completion_tokens == MAX_TOKENS
    benchmark_passed = correctness_passed and evaluation["benchmarkPassed"]
    promotion_eligible = correctness_passed and evaluation["promotionEligible"]
    eligibility = evaluation["evidenceEligibility"]
    reasons = list(evaluation.get("reasons", []))
    if not correctness_passed:
        eligibility = "ineligible-incomplete-generation"
        reasons.append(
            f"decode generation returned {completion_tokens}/{MAX_TOKENS} requested tokens"
        )
    return {
        "schemaVersion": 1,
        "benchmark": "decode",
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "baselineOnly": baseline_only,
        "performancePolicy": evaluation["performancePolicy"],
        "evidenceEligibility": eligibility,
        "promotionEligible": promotion_eligible,
        "benchmarkPassed": benchmark_passed,
        "hardGatesPassed": evaluation["hardGatesPassed"],
        "measurementsComplete": evaluation["measurementsComplete"],
        "measurements": metrics,
        "measurementMethod": {
            "decodeTokensPerSecond": "completion-tokens/non-streaming-wall-seconds",
            "peakVramMiB": "maximum-nvidia-smi-memory-used-over-benchmark",
            "ttftMs": "not-measured-by-non-streaming-benchmark",
            "prefillTokensPerSecond": "llama.cpp-response-timings",
        },
        "hardGateResults": evaluation["hardGateResults"],
        "warningResults": evaluation["warningResults"],
        "reasons": reasons,
        "workload": {
            "requestedCompletionTokens": MAX_TOKENS,
            "completionTokens": completion_tokens,
            "requestedContextTokens": CONTEXT_TOKENS,
            "requestPromptTokens": request_prompt_tokens,
            "serverPromptTokens": server_prompt_tokens,
            "elapsedSeconds": elapsed,
            "fullGenerationCompleted": correctness_passed,
        },
        "privacy": "synthetic workload metadata only; prompt and model output omitted",
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if MAX_TOKENS <= 0 or CONTEXT_TOKENS < 0:
        raise SystemExit("benchmark token counts must be non-negative and max tokens positive")
    policy, preflight_status = _policy_preflight(args)
    if preflight_status is not None:
        return preflight_status
    assert policy is not None
    try:
        document = _benchmark(policy, baseline_only=args.baseline_only)
        output = args.output or default_evidence_path("decode")
        document["evidencePath"] = str(output)
        write_private_json(output, document)
    except (OSError, RuntimeError, ValueError) as exc:
        _emit(
            {
                "schemaVersion": 1,
                "performancePolicy": policy.status,
                "evidenceEligibility": "ineligible-benchmark-error",
                "promotionEligible": False,
                "benchmarkPassed": False,
                "error": str(exc),
            },
            as_json=args.json,
        )
        return 1
    _emit(document, as_json=args.json)
    return 0 if document["benchmarkPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
