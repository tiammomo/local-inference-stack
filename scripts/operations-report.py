#!/usr/bin/env python3
"""Build a privacy-preserving operations report for local Qwen and ModelPort."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
PROMETHEUS_LINE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^}]*\})?\s+(?P<value>[-+0-9.eE]+)$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate health, performance, usage, Tool Use, and error signals "
            "without retaining prompts, tool arguments, identities, IPs, or raw errors."
        )
    )
    parser.add_argument("--hours", type=float, default=24.0, help="report window in hours")
    parser.add_argument(
        "--modelport-url",
        default=os.environ.get("MODELPORT_BASE_URL", "http://127.0.0.1:38082"),
    )
    parser.add_argument(
        "--qwen-url",
        default=os.environ.get("QWEN_RUNTIME_URL", "http://127.0.0.1:18080"),
    )
    parser.add_argument("--max-records", type=int, default=5000)
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help="include only this provider; repeat for multiple providers",
    )
    parser.add_argument(
        "--resolved-model",
        action="append",
        default=[],
        help="include only this resolved model; repeat for multiple models",
    )
    parser.add_argument(
        "--unreconciled-baseline",
        type=int,
        default=int(os.environ.get("OPERATIONS_UNRECONCILED_BASELINE", "0")),
        help="acknowledged all-time unreconciled ledger count",
    )
    parser.add_argument(
        "--include-synthetic",
        action="store_true",
        help="include temporary mock acceptance providers in traffic and alert rates",
    )
    parser.add_argument("--failure-rate-warn", type=float, default=0.05)
    parser.add_argument("--tool-failure-rate-warn", type=float, default=0.05)
    parser.add_argument("--p95-latency-ms-warn", type=int, default=180_000)
    parser.add_argument("--output", type=Path, help="write the report atomically to this path")
    parser.add_argument(
        "--save",
        action="store_true",
        help="save under logs/operations using a UTC timestamp",
    )
    parser.add_argument(
        "--fail-on-alert",
        action="store_true",
        help="exit 1 when the report contains an alert",
    )
    args = parser.parse_args()
    if args.hours <= 0 or args.hours > 24 * 90:
        parser.error("--hours must be in (0, 2160]")
    if args.max_records < 1 or args.max_records > 100_000:
        parser.error("--max-records must be in [1, 100000]")
    if args.unreconciled_baseline < 0:
        parser.error("--unreconciled-baseline must not be negative")
    for name in ("failure_rate_warn", "tool_failure_rate_warn"):
        value = getattr(args, name)
        if value < 0 or value > 1:
            parser.error(f"--{name.replace('_', '-')} must be in [0, 1]")
    if args.save and args.output:
        parser.error("use only one of --save and --output")
    return args


def request_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=10) as response:
            return response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError(f"GET {url} failed: {error}") from error


def request_json(url: str, headers: dict[str, str] | None = None) -> Any:
    return json.loads(request_bytes(url, headers).decode("utf-8"))


class AdminClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        payload = json.dumps({"username": username, "password": password}).encode("utf-8")
        request = Request(
            f"{self.base_url}/admin/auth/login",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"ModelPort administrator login failed: {error}") from error

    def get_json(self, path: str, query: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        try:
            with self.opener.open(url, timeout=15) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"GET {path} failed: {error}") from error


def prometheus_values(raw: str) -> dict[str, list[float]]:
    values: dict[str, list[float]] = defaultdict(list)
    for line in raw.splitlines():
        match = PROMETHEUS_LINE.match(line.strip())
        if match:
            values[match.group("name")].append(float(match.group("value")))
    return dict(values)


def metric_sum(metrics: dict[str, list[float]], name: str) -> float:
    return sum(metrics.get(name, []))


def percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


def rate(successes: int, total: int) -> float | None:
    return round(successes / total, 6) if total else None


def classify_issue(row: dict[str, Any]) -> str:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("errorMessage", "terminalReason", "statusCode")
    ).lower()
    patterns = (
        ("tool_protocol", r"tool|function|input_json|tool_use|tool_result"),
        ("context_or_token_limit", r"context|token.{0,20}limit|too long|maximum.{0,20}token"),
        ("timeout", r"timeout|timed out|idle"),
        ("rate_or_quota_limit", r"rate.?limit|too many|quota|\b429\b"),
        ("authentication", r"unauthori|forbidden|credential|api.?key|\b401\b|\b403\b"),
        ("capacity", r"overload|busy|concurrent|queue|out of memory|\boom\b|cuda"),
        ("upstream_transport", r"upstream|provider|connect|connection|dns|\b502\b|\b503\b"),
    )
    for category, pattern in patterns:
        if re.search(pattern, text):
            return category
    return "other"


def dimension_summary(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        key = str(row.get(field) or "unknown")
        counters[key]["requests"] += 1
        if row.get("status") == "success":
            counters[key]["successes"] += 1
    result = []
    for key, counts in counters.items():
        total = counts["requests"]
        successes = counts["successes"]
        result.append(
            {
                field: key,
                "requests": total,
                "successes": successes,
                "successRate": rate(successes, total),
            }
        )
    return sorted(result, key=lambda item: (-item["requests"], item[field]))


def numeric(row: dict[str, Any], field: str) -> int:
    value = row.get(field)
    return int(value) if isinstance(value, (int, float)) else 0


def is_synthetic_traffic(row: dict[str, Any]) -> bool:
    """Prefer the bounded gateway label and retain legacy mock compatibility."""
    return row.get("trafficClass") == "synthetic" or str(
        row.get("provider") or ""
    ).startswith("local_tool_acceptance_")


def effective_input_tokens(row: dict[str, Any]) -> int:
    return sum(
        numeric(row, field)
        for field in ("inputTokens", "cacheWriteTokens", "cacheReadTokens")
    )


def input_bucket(row: dict[str, Any]) -> str:
    tokens = effective_input_tokens(row)
    for ceiling, label in (
        (8_192, "<8K"),
        (32_768, "8K-32K"),
        (65_536, "32K-64K"),
        (92_000, "64K-92K"),
        (131_072, "92K-128K"),
    ):
        if tokens < ceiling:
            return label
    return ">=128K"


def performance_summary(
    rows: list[dict[str, Any]], field: str, key_function: Any | None = None
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = key_function(row) if key_function else str(row.get(field) or "unknown")
        groups[str(key)].append(row)
    result: list[dict[str, Any]] = []
    for key, group in groups.items():
        successes = sum(row.get("status") == "success" for row in group)
        cancellations = sum(
            row.get("terminalReason") == "downstream_cancelled" for row in group
        )
        service_rows = [
            row for row in group if row.get("terminalReason") != "downstream_cancelled"
        ]
        service_successes = sum(row.get("status") == "success" for row in service_rows)
        latencies = [
            numeric(row, "latencyMs")
            for row in group
            if isinstance(row.get("latencyMs"), (int, float))
        ]
        first_byte = [
            numeric(row, "firstByteLatencyMs")
            for row in group
            if isinstance(row.get("firstByteLatencyMs"), (int, float))
        ]
        cache_read = sum(numeric(row, "cacheReadTokens") for row in group)
        total_input = sum(effective_input_tokens(row) for row in group)
        result.append(
            {
                field: key,
                "requests": len(group),
                "successes": successes,
                "successRate": rate(successes, len(group)),
                "serviceAvailabilityRate": rate(service_successes, len(service_rows)),
                "clientCancellations": cancellations,
                "inputTokens": total_input,
                "outputTokens": sum(numeric(row, "outputTokens") for row in group),
                "cacheReadTokens": cache_read,
                "cacheHitRate": rate(cache_read, total_input),
                "latencyMs": {
                    "p50": percentile(latencies, 0.50),
                    "p95": percentile(latencies, 0.95),
                },
                "firstByteLatencyMs": {
                    "samples": len(first_byte),
                    "p50": percentile(first_byte, 0.50),
                    "p95": percentile(first_byte, 0.95),
                },
            }
        )
    return sorted(result, key=lambda item: (-item["requests"], item[field]))


def tool_use_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tool_rows = [row for row in rows if row.get("toolUseRequested") is True]
    request_successes = sum(row.get("status") == "success" for row in tool_rows)
    outcomes = Counter(
        str(row.get("toolOutcome") or "legacy_unknown") for row in tool_rows
    )
    tool_calls = outcomes["tool_called"] + outcomes["continuation_tool_called"]
    final_answers = outcomes["final_answer"] + outcomes["continuation_completed"]
    continuation_steps = final_answers + outcomes["continuation_tool_called"]
    observed_decisions = sum(
        outcomes[outcome]
        for outcome in (
            "tool_called",
            "continuation_tool_called",
            "final_answer",
            "continuation_completed",
            "answered_without_tool",
        )
    )
    observed_requests = sum(
        count
        for outcome, count in outcomes.items()
        if outcome not in {"unknown_legacy", "legacy_unknown", "completed"}
    )
    observable_successes = observed_decisions + outcomes["completed_unobserved"]
    protocol_errors = outcomes["protocol_error"]
    protocol_passes = observable_successes
    protocol_evaluations = protocol_passes + protocol_errors
    repair_attempts = sum(row.get("toolRepairAttempted") is True for row in tool_rows)
    repair_recoveries = sum(row.get("toolRepairRecovered") is True for row in tool_rows)
    return {
        "requests": len(tool_rows),
        "requestSuccesses": request_successes,
        "requestFailures": len(tool_rows) - request_successes,
        "requestSuccessRate": rate(request_successes, len(tool_rows)),
        "successes": request_successes,
        "failures": len(tool_rows) - request_successes,
        "successRate": rate(request_successes, len(tool_rows)),
        "observedRequests": observed_requests,
        "observedDecisions": observed_decisions,
        "decisionCoverageRate": rate(observed_decisions, observable_successes),
        "modelToolCalls": tool_calls,
        "schemaValidatedCalls": tool_calls,
        "answeredWithoutTool": outcomes["answered_without_tool"],
        "continuationSteps": continuation_steps,
        "continuationCompletions": final_answers,
        "finalAnswers": final_answers,
        "completedUnobserved": outcomes["completed_unobserved"],
        "continuationFinalRate": rate(final_answers, continuation_steps),
        "protocolErrors": protocol_errors,
        "protocolEvaluations": protocol_evaluations,
        "protocolPassRate": rate(protocol_passes, protocol_evaluations),
        "repairAttempts": repair_attempts,
        "repairRecoveries": repair_recoveries,
        "repairRecoveryRate": rate(repair_recoveries, repair_attempts),
        "byOutcome": dict(sorted(outcomes.items())),
        "coverageNote": (
            "request success is transport/protocol availability; continuationFinalRate "
            "describes observed tool-result continuation steps, not end-to-end business success"
        ),
    }


def fetch_logs(
    admin: AdminClient, date_from: int, date_to: int, max_records: int
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    page = 1
    available = 0
    while len(rows) < max_records:
        body = admin.get_json(
            "/admin/logs",
            {
                "page": page,
                "pageSize": min(500, max_records - len(rows)),
                "dateFrom": date_from,
                "dateTo": date_to,
            },
        )
        batch = body.get("logs", [])
        available = int(body.get("total", len(batch)))
        rows.extend(row for row in batch if isinstance(row, dict))
        if not batch or len(rows) >= available:
            break
        page += 1
    return rows[:max_records], available


def qwen_snapshot(base_url: str) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    health = request_json(f"{base_url}/health")
    metrics = prometheus_values(request_bytes(f"{base_url}/metrics").decode("utf-8"))
    slots = request_json(f"{base_url}/slots")
    props = request_json(f"{base_url}/props")
    generation = props.get("default_generation_settings", {})
    params = generation.get("params", {}) if isinstance(generation, dict) else {}
    safe_slots = [
        {
            "id": slot.get("id"),
            "processing": bool(slot.get("is_processing", False)),
            "contextSize": slot.get("n_ctx"),
            "promptTokens": slot.get("n_prompt_tokens"),
            "promptTokensProcessed": slot.get("n_prompt_tokens_processed"),
            "promptTokensCached": slot.get("n_prompt_tokens_cache"),
            "decodedTokens": (
                slot.get("next_token", [{}])[0].get("n_decoded")
                if isinstance(slot.get("next_token"), list) and slot.get("next_token")
                else None
            ),
            "remainingTokens": (
                slot.get("next_token", [{}])[0].get("n_remain")
                if isinstance(slot.get("next_token"), list) and slot.get("next_token")
                else None
            ),
            "speculative": bool(slot.get("speculative", False)),
        }
        for slot in slots
        if isinstance(slot, dict)
    ]
    return {
        "healthy": health.get("status") == "ok",
        "runtime": {
            "modelAlias": props.get("model_alias"),
            "modelType": props.get("model_ftype"),
            "modelFile": Path(str(props.get("model_path") or "")).name or None,
            "buildInfo": props.get("build_info"),
            "contextSize": generation.get("n_ctx") if isinstance(generation, dict) else None,
            "totalSlots": props.get("total_slots"),
            "sleeping": bool(props.get("is_sleeping", False)),
            "modalities": props.get("modalities", {}),
            "toolCapabilities": props.get("chat_template_caps", {}),
            "sampling": {
                "temperature": params.get("temperature"),
                "topK": params.get("top_k"),
                "topP": params.get("top_p"),
                "minP": params.get("min_p"),
                "presencePenalty": params.get("presence_penalty"),
            },
        },
        "slots": safe_slots,
        "metrics": {
            "promptTokensTotal": int(metric_sum(metrics, "llamacpp:prompt_tokens_total")),
            "generatedTokensTotal": int(metric_sum(metrics, "llamacpp:tokens_predicted_total")),
            "promptTokensPerSecond": metric_sum(metrics, "llamacpp:prompt_tokens_seconds"),
            "generatedTokensPerSecond": metric_sum(metrics, "llamacpp:predicted_tokens_seconds"),
            "requestsProcessing": int(metric_sum(metrics, "llamacpp:requests_processing")),
            "requestsDeferred": int(metric_sum(metrics, "llamacpp:requests_deferred")),
        },
    }


def gpu_snapshot() -> dict[str, Any] | None:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits",
    ]
    try:
        line = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=10
        ).stdout.splitlines()[0]
        name, used, total, utilization, temperature, power = [
            value.strip() for value in line.split(",", 5)
        ]
        return {
            "name": name,
            "memoryUsedMiB": int(used),
            "memoryTotalMiB": int(total),
            "utilizationPercent": int(utilization),
            "temperatureC": int(temperature),
            "powerWatts": float(power),
        }
    except (FileNotFoundError, IndexError, ValueError, subprocess.SubprocessError):
        return None


def host_snapshot() -> dict[str, Any] | None:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
        total = values["MemTotal"]
        available = values["MemAvailable"]
        swap_total = values.get("SwapTotal", 0)
        swap_free = values.get("SwapFree", 0)
        load = os.getloadavg()
        return {
            "memoryTotalBytes": total,
            "memoryUsedBytes": max(0, total - available),
            "memoryAvailableBytes": available,
            "swapTotalBytes": swap_total,
            "swapUsedBytes": max(0, swap_total - swap_free),
            "loadAverage": [round(value, 2) for value in load],
        }
    except (OSError, ValueError, KeyError):
        return None


def container_snapshot() -> list[dict[str, Any]]:
    names = [
        os.environ.get("QWEN_CONTAINER_NAME", "qwen35-9b-q5km"),
        "modelport-modelport-1",
        "modelport-dashboard-1",
    ]
    try:
        result = subprocess.run(
            ["docker", "inspect", *names],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        containers = json.loads(result.stdout)
        return [
            {
                "name": container.get("Name", "").lstrip("/"),
                "status": container.get("State", {}).get("Status"),
                "healthy": container.get("State", {}).get("Health", {}).get("Status"),
                "startedAt": container.get("State", {}).get("StartedAt"),
                "restartCount": container.get("RestartCount", 0),
                "imageId": container.get("Image"),
            }
            for container in containers
        ]
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        return []


def selected_ledger_signals(value: Any) -> dict[str, int]:
    signals: dict[str, int] = {}

    def visit(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = f"{path}.{key}" if path else key
                normalized = key.lower()
                if isinstance(child, int) and any(
                    marker in normalized
                    for marker in ("unreconciled", "expiredlease", "activelease", "inflight")
                ):
                    signals[child_path] = child
                else:
                    visit(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{path}[{index}]")

    visit(value)
    return signals


def atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(body)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def build_report(
    args: argparse.Namespace, admin: AdminClient | None = None
) -> dict[str, Any]:
    username = os.environ.get("MODELPORT_ADMIN_USERNAME", "")
    password = os.environ.get("MODELPORT_ADMIN_PASSWORD", "")
    auth_token = os.environ.get("MODELPORT_AUTH_TOKEN", "")
    if not username or not password or not auth_token:
        raise RuntimeError(
            "MODELPORT_ADMIN_USERNAME, MODELPORT_ADMIN_PASSWORD, and "
            "MODELPORT_AUTH_TOKEN must be set"
        )

    now_ms = int(time.time() * 1000)
    from_ms = now_ms - int(args.hours * 3_600_000)
    admin = admin or AdminClient(args.modelport_url, username, password)
    loaded_rows, available = fetch_logs(admin, from_ms, now_ms, args.max_records)
    synthetic_rows = [
        row
        for row in loaded_rows
        if is_synthetic_traffic(row)
    ]
    candidate_rows = (
        loaded_rows
        if args.include_synthetic
        else [row for row in loaded_rows if row not in synthetic_rows]
    )
    included_providers = {
        str(value) for value in (getattr(args, "provider", None) or []) if value
    }
    included_models = {
        str(value)
        for value in (getattr(args, "resolved_model", None) or [])
        if value
    }
    rows = [
        row
        for row in candidate_rows
        if (
            not included_providers
            or str(row.get("provider") or "") in included_providers
        )
        and (
            not included_models
            or str(row.get("resolvedModel") or "") in included_models
        )
    ]
    ready = request_json(
        f"{args.modelport_url.rstrip('/')}/readyz", {"x-api-key": auth_token}
    )
    ledger = admin.get_json("/admin/enterprise/overview")
    ledger_signals = selected_ledger_signals(ledger)
    modelport_metrics = prometheus_values(
        request_bytes(
            f"{args.modelport_url.rstrip('/')}/metrics", {"x-api-key": auth_token}
        ).decode("utf-8")
    )

    successes = sum(row.get("status") == "success" for row in rows)
    failures = len(rows) - successes
    client_cancellations = sum(
        row.get("terminalReason") == "downstream_cancelled" for row in rows
    )
    service_rows = [
        row for row in rows if row.get("terminalReason") != "downstream_cancelled"
    ]
    service_successes = sum(row.get("status") == "success" for row in service_rows)
    service_failures = len(service_rows) - service_successes
    timeouts = sum(row.get("status") == "timeout" for row in rows)
    streams = sum(row.get("stream") == "stream" for row in rows)
    tool = tool_use_summary(rows)
    tool_rows = [row for row in rows if row.get("toolUseRequested") is True]
    tool_successes = tool["requestSuccesses"]
    latencies = [
        int(row["latencyMs"])
        for row in rows
        if isinstance(row.get("latencyMs"), (int, float))
    ]
    first_byte_latencies = [
        int(row["firstByteLatencyMs"])
        for row in rows
        if isinstance(row.get("firstByteLatencyMs"), (int, float))
    ]
    issue_counts = Counter(
        classify_issue(row) for row in rows if row.get("status") != "success"
    )
    terminal_reasons = Counter(str(row.get("terminalReason") or "unknown") for row in rows)

    total_input = sum(int(row.get("inputTokens") or 0) for row in rows)
    total_output = sum(int(row.get("outputTokens") or 0) for row in rows)
    total_cache_read = sum(int(row.get("cacheReadTokens") or 0) for row in rows)
    total_cache_write = sum(int(row.get("cacheWriteTokens") or 0) for row in rows)
    billed_input = total_input + total_cache_read + total_cache_write
    cache_hit_rate = (total_cache_read / billed_input) if billed_input else 0.0
    failure_rate = (service_failures / len(service_rows)) if service_rows else 0.0
    tool_failure_rate = (
        (len(tool_rows) - tool_successes) / len(tool_rows) if tool_rows else 0.0
    )
    p95_latency = percentile(latencies, 0.95)

    qwen = qwen_snapshot(args.qwen_url)
    alerts: list[dict[str, Any]] = []
    if not qwen["healthy"]:
        alerts.append({"code": "qwen_unhealthy", "value": False})
    if failure_rate >= args.failure_rate_warn and rows:
        alerts.append({"code": "failure_rate", "value": round(failure_rate, 6)})
    if tool_failure_rate >= args.tool_failure_rate_warn and tool_rows:
        alerts.append(
            {"code": "tool_failure_rate", "value": round(tool_failure_rate, 6)}
        )
    if p95_latency >= args.p95_latency_ms_warn and latencies:
        alerts.append({"code": "p95_latency_ms", "value": p95_latency})
    if timeouts:
        alerts.append({"code": "timeouts", "value": timeouts})
    if qwen["metrics"]["requestsDeferred"]:
        alerts.append(
            {
                "code": "qwen_deferred_requests",
                "value": qwen["metrics"]["requestsDeferred"],
            }
        )
    unreconciled = sum(
        value for key, value in ledger_signals.items() if "unreconciled" in key.lower()
    )
    if unreconciled > args.unreconciled_baseline:
        alerts.append(
            {
                "code": "ledger_unreconciled_increase",
                "value": {
                    "current": unreconciled,
                    "acknowledgedBaseline": args.unreconciled_baseline,
                    "increase": unreconciled - args.unreconciled_baseline,
                },
            }
        )
    expired_leases = sum(
        value for key, value in ledger_signals.items() if "expiredlease" in key.lower()
    )
    if expired_leases:
        alerts.append({"code": "ledger_expired_leases", "value": expired_leases})
    if available > len(loaded_rows):
        alerts.append(
            {"code": "report_truncated", "value": available - len(loaded_rows)}
        )

    return {
        "schemaVersion": 1,
        "generatedAtEpochMs": now_ms,
        "privacy": {
            "mode": "aggregate-only",
            "excluded": [
                "prompts",
                "responses",
                "tool arguments",
                "raw errors",
                "request IDs",
                "usernames",
                "API key IDs",
                "client IPs",
            ],
        },
        "scope": {
            "mode": "filtered" if included_providers or included_models else "all",
            "providers": sorted(included_providers),
            "resolvedModels": sorted(included_models),
        },
        "window": {
            "hours": args.hours,
            "fromEpochMs": from_ms,
            "toEpochMs": now_ms,
            "recordsLoaded": len(loaded_rows),
            "recordsMatchedScope": len(rows),
            "recordsAnalyzed": len(rows),
            "syntheticRecordsExcluded": 0 if args.include_synthetic else len(synthetic_rows),
            "recordsAvailable": available,
            "truncated": available > len(loaded_rows),
        },
        "health": {
            "modelportReady": bool(ready),
            "qwenHealthy": qwen["healthy"],
            "ledgerSignals": ledger_signals,
            "unreconciledAcknowledgedBaseline": args.unreconciled_baseline,
        },
        "traffic": {
            "requests": len(rows),
            "successes": successes,
            "failures": failures,
            "successRate": rate(successes, len(rows)),
            "serviceRequests": len(service_rows),
            "serviceSuccesses": service_successes,
            "serviceFailures": service_failures,
            "serviceAvailabilityRate": rate(service_successes, len(service_rows)),
            "clientCancellations": client_cancellations,
            "timeouts": timeouts,
            "streamRequests": streams,
            "tokens": {
                "input": total_input,
                "output": total_output,
                "cacheRead": total_cache_read,
                "cacheWrite": total_cache_write,
                "cacheHitRate": round(cache_hit_rate, 6),
            },
            "latencyMs": {
                "average": round(sum(latencies) / len(latencies)) if latencies else 0,
                "p50": percentile(latencies, 0.50),
                "p95": p95_latency,
                "p99": percentile(latencies, 0.99),
                "maximum": max(latencies, default=0),
            },
            "firstByteLatencyMs": {
                "samples": len(first_byte_latencies),
                "average": round(sum(first_byte_latencies) / len(first_byte_latencies))
                if first_byte_latencies
                else 0,
                "p50": percentile(first_byte_latencies, 0.50),
                "p95": percentile(first_byte_latencies, 0.95),
                "p99": percentile(first_byte_latencies, 0.99),
                "maximum": max(first_byte_latencies, default=0),
            },
            "byProvider": dimension_summary(rows, "provider"),
            "byModel": dimension_summary(rows, "resolvedModel"),
            "byLogicalModel": performance_summary(rows, "model"),
            "byInputBucket": performance_summary(rows, "inputBucket", input_bucket),
        },
        "toolUse": tool,
        "issues": {
            "byCategory": dict(sorted(issue_counts.items())),
            "byTerminalReason": dict(sorted(terminal_reasons.items())),
        },
        "process": {
            "modelportUptimeSeconds": int(
                metric_sum(modelport_metrics, "modelport_uptime_seconds")
            ),
            "modelportMessageRequests": int(
                metric_sum(modelport_metrics, "modelport_message_requests_total")
            ),
            "modelportMessageFailures": int(
                metric_sum(modelport_metrics, "modelport_message_failures_total")
            ),
            "qwen": qwen,
            "gpu": gpu_snapshot(),
            "host": host_snapshot(),
            "containers": container_snapshot(),
        },
        "alerts": alerts,
    }


def main() -> int:
    args = parse_args()
    try:
        report = build_report(args)
    except (RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"operations report failed: {error}", file=sys.stderr)
        return 2

    body = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    output = args.output
    if args.save:
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        output = ROOT_DIR / "logs" / "operations" / f"{timestamp}.json"
    if output:
        atomic_write(output, body)
        print(output)
    else:
        print(body)
    return 1 if args.fail_on_alert and report["alerts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
