#!/usr/bin/env python3
"""Run a bounded chat generation benchmark without printing model output."""

from __future__ import annotations

import json
import os
import time
import urllib.request


BASE_URL = os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:18080")
MAX_TOKENS = int(os.environ.get("DECODE_BENCHMARK_TOKENS", "512"))
CONTEXT_TOKENS = int(os.environ.get("DECODE_CONTEXT_TOKENS", "0"))
TOPIC = os.environ.get(
    "DECODE_BENCHMARK_TOPIC", "designing reliable local LLM inference services"
)


def post(path: str, payload: dict, timeout: int = 1800) -> dict:
    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def token_count(text: str) -> int:
    return len(post("/tokenize", {"content": text})["tokens"])


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
    prompt = paragraph * max(1, CONTEXT_TOKENS // unit_tokens)
    while token_count(prompt) < CONTEXT_TOKENS:
        prompt += paragraph
    return prompt + "\n\n" + instruction


def main() -> None:
    prompt = build_prompt()
    request_prompt_tokens = token_count(prompt)
    started = time.monotonic()
    body = post(
        "/v1/chat/completions",
        {
            "model": "qwen3.5-9b-q5km",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    elapsed = time.monotonic() - started
    usage = body.get("usage", {})
    completion_tokens = int(usage.get("completion_tokens", 0))

    print(f"request_prompt_tokens={request_prompt_tokens}")
    print(f"server_prompt_tokens={usage.get('prompt_tokens', 0)}")
    print(f"requested_tokens={MAX_TOKENS}")
    print(f"completion_tokens={completion_tokens}")
    print(f"elapsed_seconds={elapsed:.3f}")
    if elapsed > 0:
        print(f"end_to_end_tokens_per_second={completion_tokens / elapsed:.2f}")
    if completion_tokens < MAX_TOKENS:
        raise SystemExit("decode benchmark ended before the requested token count")


if __name__ == "__main__":
    main()
