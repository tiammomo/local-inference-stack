#!/usr/bin/env python3
"""Measure aggregate decode throughput for concurrent local Qwen requests."""

from __future__ import annotations

import concurrent.futures
import json
import os
import time
import urllib.request


BASE_URL = os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:18080")
CONCURRENCY = int(os.environ.get("BENCHMARK_CONCURRENCY", "2"))
MAX_TOKENS = int(os.environ.get("BENCHMARK_TOKENS", "512"))


def complete(index: int) -> int:
    request = urllib.request.Request(
        BASE_URL + "/v1/chat/completions",
        data=json.dumps(
            {
                "model": "qwen3.5-9b-q5km",
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
    with urllib.request.urlopen(request, timeout=600) as response:
        body = json.load(response)
    completion_tokens = int(body.get("usage", {}).get("completion_tokens", 0))
    if completion_tokens != MAX_TOKENS:
        raise RuntimeError(
            f"request {index} returned {completion_tokens}/{MAX_TOKENS} tokens"
        )
    return completion_tokens


def main() -> None:
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        counts = list(executor.map(complete, range(CONCURRENCY)))
    elapsed = time.monotonic() - started
    total_tokens = sum(counts)

    print(f"concurrency={CONCURRENCY}")
    print(f"tokens_per_request={MAX_TOKENS}")
    print(f"total_completion_tokens={total_tokens}")
    print(f"wall_seconds={elapsed:.3f}")
    print(f"aggregate_tokens_per_second={total_tokens / elapsed:.2f}")


if __name__ == "__main__":
    main()
