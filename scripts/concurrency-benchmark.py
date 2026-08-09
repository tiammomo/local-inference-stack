#!/usr/bin/env python3
"""Measure aggregate decode throughput without printing prompts or model output."""

from __future__ import annotations

import argparse
import concurrent.futures
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
    load_policy,
    write_private_json,
)

try:
    from scripts.local_http import direct_urlopen
except ModuleNotFoundError:
    from local_http import direct_urlopen


BASE_URL = os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:18080")
CONCURRENCY = int(os.environ.get("BENCHMARK_CONCURRENCY", "2"))
MAX_TOKENS = int(os.environ.get("BENCHMARK_TOKENS", "512"))
MODEL_ID = os.environ.get("QWEN_SERVED_MODEL_ID", "qwen3.5-9b-q5km")


def complete(index: int) -> tuple[int, float | None]:
    request = urllib.request.Request(
        BASE_URL + "/v1/chat/completions",
        data=json.dumps(
            {
                "model": MODEL_ID,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Request {index}: write a detailed technical guide about reliable "
                            "local LLM services. Continue until the token limit."
                        ),
                    }
                ],
                "max_tokens": MAX_TOKENS,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with direct_urlopen(request, timeout=600) as response:
        body = json.load(response)
    if not isinstance(body, dict):
        raise RuntimeError(f"request {index} returned a non-object JSON response")
    usage = body.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    completion_tokens = int(usage.get("completion_tokens", 0))
    timings = body.get("timings")
    if not isinstance(timings, dict):
        timings = {}
    prefill = timings.get("prompt_per_second")
    if (
        isinstance(prefill, bool)
        or not isinstance(prefill, (int, float))
        or not math.isfinite(prefill)
        or prefill <= 0
    ):
        prefill = None
    return completion_tokens, float(prefill) if prefill is not None else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
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
        policy = load_policy(args.manifest)
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
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=CONCURRENCY
        ) as executor:
            results = list(executor.map(complete, range(CONCURRENCY)))
        elapsed = time.monotonic() - started
    finally:
        peak_vram_mib = sampler.stop()

    counts = [tokens for tokens, _prefill in results]
    total_tokens = sum(counts)
    prefill_samples = [prefill for _tokens, prefill in results if prefill is not None]
    metrics = {
        "aggregateTokensPerSecond": total_tokens / elapsed if elapsed > 0 else None,
        "peakVramMiB": peak_vram_mib,
        # Non-streaming wall time cannot distinguish queueing, prefill, and TTFT.
        "ttftMs": None,
        "prefillTokensPerSecond": (
            sum(prefill_samples) / len(prefill_samples) if prefill_samples else None
        ),
    }
    evaluation = evaluate(
        policy,
        metrics,
        required_metrics={"aggregateTokensPerSecond", "peakVramMiB"},
        baseline_only=baseline_only,
    )
    correctness_passed = all(count == MAX_TOKENS for count in counts)
    benchmark_passed = correctness_passed and evaluation["benchmarkPassed"]
    promotion_eligible = correctness_passed and evaluation["promotionEligible"]
    eligibility = evaluation["evidenceEligibility"]
    reasons = list(evaluation.get("reasons", []))
    if not correctness_passed:
        eligibility = "ineligible-incomplete-generation"
        reasons.append(
            "concurrent generations returned token counts "
            + ", ".join(f"{count}/{MAX_TOKENS}" for count in counts)
        )
    return {
        "schemaVersion": 1,
        "benchmark": "concurrency",
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
            "aggregateTokensPerSecond": (
                "sum-completion-tokens/concurrent-non-streaming-wall-seconds"
            ),
            "peakVramMiB": "maximum-nvidia-smi-memory-used-over-benchmark",
            "ttftMs": "not-measured-by-non-streaming-benchmark",
            "prefillTokensPerSecond": "mean-llama.cpp-response-timings",
        },
        "hardGateResults": evaluation["hardGateResults"],
        "warningResults": evaluation["warningResults"],
        "reasons": reasons,
        "workload": {
            "concurrency": CONCURRENCY,
            "requestedTokensPerRequest": MAX_TOKENS,
            "completionTokensPerRequest": counts,
            "totalCompletionTokens": total_tokens,
            "elapsedSeconds": elapsed,
            "allGenerationsCompleted": correctness_passed,
        },
        "privacy": "synthetic workload metadata only; prompts and model output omitted",
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if CONCURRENCY <= 0 or MAX_TOKENS <= 0:
        raise SystemExit("benchmark concurrency and token count must be positive")
    policy, preflight_status = _policy_preflight(args)
    if preflight_status is not None:
        return preflight_status
    assert policy is not None
    try:
        document = _benchmark(policy, baseline_only=args.baseline_only)
        output = args.output or default_evidence_path("concurrency")
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
