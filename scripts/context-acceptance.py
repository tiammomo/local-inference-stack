#!/usr/bin/env python3
"""Build and verify a long prompt against the local llama.cpp server."""

from __future__ import annotations

import json
import os
import time
import urllib.request


CONTEXT_BACKEND = os.environ.get("CONTEXT_BACKEND", "llama")
LLAMA_BASE_URL = os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:18080")
MODELPORT_BASE_URL = os.environ.get("MODELPORT_BASE_URL", "http://127.0.0.1:38082")
TARGET_TOKENS = int(os.environ.get("TARGET_TOKENS", "118000"))
DEFAULT_MAX_TOKENS = "8192" if CONTEXT_BACKEND == "modelport" else "512"
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", DEFAULT_MAX_TOKENS))
ENABLE_THINKING = os.environ.get("ENABLE_THINKING", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
NEEDLE_CODE = os.environ.get("NEEDLE_CODE", "JULY-5070TI-QWEN35-128K")
NEEDLE = f"长上下文验收码是：{NEEDLE_CODE}"
FILLER_PREFIX = os.environ.get("FILLER_PREFIX", "")


def post(
    base_url: str,
    path: str,
    payload: dict,
    headers: dict[str, str] | None = None,
    timeout: int = 1800,
) -> dict:
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def token_count(text: str) -> int:
    return len(post(LLAMA_BASE_URL, "/tokenize", {"content": text})["tokens"])


def complete(prompt: str) -> tuple[str, dict]:
    message = {"role": "user", "content": prompt}
    if CONTEXT_BACKEND == "llama":
        result = post(
            LLAMA_BASE_URL,
            "/v1/chat/completions",
            {
                "model": "qwen3.5-9b-q5km",
                "messages": [message],
                "max_tokens": MAX_TOKENS,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": ENABLE_THINKING},
            },
        )
        answer = result["choices"][0]["message"].get("content", "")
        return answer, result.get("usage", {})

    if CONTEXT_BACKEND == "modelport":
        auth_token = os.environ.get("MODELPORT_AUTH_TOKEN")
        if not auth_token:
            raise SystemExit("MODELPORT_AUTH_TOKEN is required for the modelport backend")
        result = post(
            MODELPORT_BASE_URL,
            "/v1/messages",
            {
                "model": "local_qwen:qwen3.5-9b-q5km",
                "messages": [message],
                "max_tokens": MAX_TOKENS,
                "temperature": 0,
            },
            {
                "x-api-key": auth_token,
                "anthropic-version": "2023-06-01",
            },
        )
        answer = "".join(
            block.get("text", "")
            for block in result.get("content", [])
            if block.get("type") == "text"
        )
        return answer, result.get("usage", {})

    raise SystemExit("CONTEXT_BACKEND must be llama or modelport")


def main() -> None:
    paragraph = (
        "这是用于本地推理服务长上下文验收的填充段落。"
        "模型需要忽略重复内容，并在收到问题后准确找出唯一的验收码。"
        "The quick brown fox jumps over the lazy dog. "
    )
    unit_tokens = token_count(paragraph)
    repeats = max(1, TARGET_TOKENS // unit_tokens)
    prompt = FILLER_PREFIX + paragraph * repeats
    count = token_count(prompt)
    while count < TARGET_TOKENS:
        prompt += paragraph
        count = token_count(prompt)

    prompt = f"{prompt[:len(prompt)//2]}\n{NEEDLE}\n{prompt[len(prompt)//2:]}"
    count = token_count(prompt)
    print(f"prompt_tokens={count}")

    started = time.monotonic()
    answer, usage = complete(prompt + "\n请只回复上文中的长上下文验收码。")
    elapsed = time.monotonic() - started
    print(f"backend={CONTEXT_BACKEND}")
    print(f"elapsed_seconds={elapsed:.2f}")
    print(f"answer={answer}")
    print(f"usage={json.dumps(usage, ensure_ascii=False)}")
    if answer.strip() != NEEDLE_CODE:
        raise SystemExit("long-context acceptance failed: answer was not exactly the needle")


if __name__ == "__main__":
    main()
