#!/usr/bin/env python3
"""Verify ModelPort's Anthropic-to-llama.cpp reasoning controls."""

from __future__ import annotations

import json
import os
import urllib.request

try:
    from scripts.local_http import direct_urlopen
except ModuleNotFoundError:
    from local_http import direct_urlopen


BASE_URL = os.environ.get(
    "MODELPORT_BASE_URL",
    os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:38082"),
)
AUTH_TOKEN = os.environ.get("MODELPORT_AUTH_TOKEN")
BOUND_QUALIFICATION = os.environ.get("LOCAL_INFERENCE_BOUND_QUALIFICATION") == "1"
CODE_MODEL = (
    os.environ["LOCAL_INFERENCE_LOGICAL_CODE_MODEL"]
    if BOUND_QUALIFICATION
    else "qwen3.5-code"
)
FAST_MODEL = (
    os.environ["LOCAL_INFERENCE_LOGICAL_FAST_MODEL"]
    if BOUND_QUALIFICATION
    else "qwen3.5-fast"
)


def complete(model: str, thinking: dict, max_tokens: int) -> tuple[str, dict]:
    if not AUTH_TOKEN:
        raise SystemExit("MODELPORT_AUTH_TOKEN is required")
    request = urllib.request.Request(
        BASE_URL + "/v1/messages",
        data=json.dumps(
            {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "thinking": thinking,
                "messages": [
                    {
                        "role": "user",
                        "content": "计算 19 + 23。请只在最终答案中回复数字。",
                    }
                ],
            },
            ensure_ascii=False,
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": AUTH_TOKEN,
            "anthropic-version": "2023-06-01",
            "x-modelport-traffic-class": "synthetic",
        },
        method="POST",
    )
    with direct_urlopen(request, timeout=600) as response:
        body = json.load(response)
    text = "".join(
        block.get("text", "")
        for block in body.get("content", [])
        if block.get("type") == "text"
    ).strip()
    return text, body.get("usage", {})


def main() -> None:
    enabled_text, enabled_usage = complete(
        CODE_MODEL, {"type": "enabled", "budget_tokens": 128}, 512
    )
    disabled_text, disabled_usage = complete(
        FAST_MODEL, {"type": "disabled"}, 128
    )
    if enabled_text != "42" or disabled_text != "42":
        raise SystemExit(
            f"reasoning control failed: enabled={enabled_text!r}, disabled={disabled_text!r}"
        )
    if "</think>" in enabled_text or "</think>" in disabled_text:
        raise SystemExit("reasoning markup leaked into final content")
    if int(disabled_usage.get("output_tokens", 0)) >= 32:
        raise SystemExit("disabled thinking unexpectedly consumed a reasoning-sized budget")

    print(f"enabled_128_answer={enabled_text}")
    print(f"enabled_128_usage={json.dumps(enabled_usage, ensure_ascii=False)}")
    print(f"disabled_answer={disabled_text}")
    print(f"disabled_usage={json.dumps(disabled_usage, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
