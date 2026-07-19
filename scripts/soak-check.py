#!/usr/bin/env python3
"""Evaluate whether the single-host deployment has enough continuous production evidence."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT_DIR / "logs" / "operations"
BACKUP_DIR = ROOT_DIR / "backups" / "modelport"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--minimum-hours",
        type=float,
        default=72.0,
        help="required uninterrupted evidence window (default: 72)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args()
    if args.minimum_hours <= 0 or args.minimum_hours > 24 * 90:
        parser.error("--minimum-hours must be in (0, 2160]")
    return args


def run_json(command: list[str]) -> Any:
    result = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=60
    )
    return json.loads(result.stdout)


def parse_docker_time(value: str) -> datetime:
    normalized = re.sub(r"\.(\d{6})\d+(?=Z|[+-])", r".\1", value)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def add_check(
    checks: list[dict[str, Any]], name: str, passed: bool, actual: Any, expected: Any
) -> None:
    checks.append(
        {"name": name, "passed": bool(passed), "actual": actual, "expected": expected}
    )


def report_evidence(minimum_hours: float) -> dict[str, Any]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    target_cutoff_ms = int(now_ms - minimum_hours * 3_600_000)
    cutoff_ms = int(now_ms - max(minimum_hours, 36.0) * 3_600_000)
    timestamps: list[int] = []
    alert_codes: list[str] = []
    for path in REPORT_DIR.glob("*.json"):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if document.get("generatedAtEpochMs", 0) < cutoff_ms:
            continue
        if document.get("scope", {}).get("providers") != ["local_qwen"]:
            continue
        timestamps.append(int(document["generatedAtEpochMs"]))
        alert_codes.extend(
            str(alert.get("code"))
            for alert in document.get("alerts", [])
            if isinstance(alert, dict) and alert.get("code")
        )
    required = max(1, math.ceil(minimum_hours / 24))
    timestamps.sort()
    span_hours = (
        round((timestamps[-1] - timestamps[0]) / 3_600_000, 3)
        if len(timestamps) > 1
        else 0.0
    )
    boundary_points = [
        target_cutoff_ms,
        *(timestamp for timestamp in timestamps if timestamp >= target_cutoff_ms),
        now_ms,
    ]
    maximum_gap_hours = round(
        max(
            (right - left) / 3_600_000
            for left, right in zip(boundary_points, boundary_points[1:])
        ),
        3,
    )
    required_span_hours = max(0.0, minimum_hours - 26.0)
    return {
        "reports": len(timestamps),
        "requiredReports": required,
        "spanHours": span_hours,
        "requiredSpanHours": required_span_hours,
        "maximumGapHours": maximum_gap_hours,
        "alertCodes": sorted(set(alert_codes)),
    }


def latest_backup_age_hours() -> tuple[float | None, bool]:
    try:
        archives = [
            path
            for path in BACKUP_DIR.glob("modelport-*.tar.gz")
            if path.is_file() and not path.is_symlink()
        ]
        if not archives:
            return None, False
        latest = max(archives, key=lambda path: path.stat().st_mtime_ns)
        age = max(0.0, datetime.now(timezone.utc).timestamp() - latest.stat().st_mtime)
        secure = (
            BACKUP_DIR.stat().st_mode & 0o077 == 0
            and latest.stat().st_mode & 0o077 == 0
        )
        return round(age / 3600, 3), secure
    except OSError:
        return None, False


def endpoint_ok(url: str) -> bool:
    try:
        with urlopen(url, timeout=5) as response:
            return 200 <= response.status < 300
    except OSError:
        return False


def evaluate(minimum_hours: float) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    container_names = [
        "qwen35-9b-q5km",
        "modelport-modelport-1",
        "modelport-postgres-1",
    ]
    try:
        containers = run_json(["docker", "inspect", *container_names])
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        containers = []
        add_check(checks, "container inspection", False, str(error), "available")

    now = datetime.now(timezone.utc)
    by_name = {
        str(container.get("Name", "")).lstrip("/"): container for container in containers
    }
    for name in container_names:
        container = by_name.get(name)
        if not container:
            add_check(checks, f"{name} present", False, "missing", "present")
            continue
        state = container.get("State", {})
        health = state.get("Health", {}).get("Status")
        uptime_hours = round(
            (now - parse_docker_time(state.get("StartedAt", ""))).total_seconds() / 3600,
            3,
        )
        add_check(checks, f"{name} running", state.get("Status") == "running", state.get("Status"), "running")
        add_check(checks, f"{name} healthy", health == "healthy", health, "healthy")
        add_check(checks, f"{name} restart count", container.get("RestartCount", 0) == 0, container.get("RestartCount", 0), 0)
        add_check(checks, f"{name} continuous uptime", uptime_hours >= minimum_hours, uptime_hours, f">={minimum_hours}h")

    add_check(checks, "Qwen health endpoint", endpoint_ok("http://127.0.0.1:18080/health"), "reachable", "HTTP 2xx")
    add_check(checks, "ModelPort liveness endpoint", endpoint_ok("http://127.0.0.1:38082/livez"), "reachable", "HTTP 2xx")
    add_check(checks, "operations dashboard endpoint", endpoint_ok("http://127.0.0.1:33004/healthz"), "reachable", "HTTP 2xx")

    report_window = report_evidence(minimum_hours)
    add_check(
        checks,
        "operations report count",
        report_window["reports"] >= report_window["requiredReports"],
        report_window["reports"],
        f">={report_window['requiredReports']}",
    )
    add_check(
        checks,
        "operations report span",
        report_window["spanHours"] >= report_window["requiredSpanHours"],
        report_window["spanHours"],
        f">={report_window['requiredSpanHours']}h",
    )
    add_check(
        checks,
        "operations report maximum gap",
        report_window["maximumGapHours"] <= 36,
        report_window["maximumGapHours"],
        "<=36h",
    )
    add_check(
        checks,
        "operations report alerts",
        not report_window["alertCodes"],
        report_window["alertCodes"],
        [],
    )

    backup_age, secure = latest_backup_age_hours()
    add_check(checks, "latest backup age", backup_age is not None and backup_age <= 36, backup_age, "<=36h")
    add_check(checks, "backup permissions", secure, secure, True)

    deployment = subprocess.run(
        [sys.executable, str(ROOT_DIR / "scripts" / "verify-deployment.py"), "--json"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    deployment_passed = False
    deployment_summary: Any = deployment.stderr.strip() or "invalid output"
    if deployment.returncode == 0:
        try:
            deployment_document = json.loads(deployment.stdout)
            deployment_summary = deployment_document.get("summary")
            deployment_passed = deployment_document.get("status") == "passed"
        except ValueError:
            pass
    add_check(checks, "deployment manifest", deployment_passed, deployment_summary, {"failed": 0})

    failed = sum(not check["passed"] for check in checks)
    return {
        "schemaVersion": 1,
        "status": "passed" if failed == 0 else "collecting_evidence",
        "minimumHours": minimum_hours,
        "summary": {"passed": len(checks) - failed, "failed": failed},
        "checks": checks,
    }


def main() -> int:
    args = parse_args()
    result = evaluate(args.minimum_hours)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for check in result["checks"]:
            label = "PASS" if check["passed"] else "WAIT"
            print(f"[{label}] {check['name']}: {check['actual']} (expected {check['expected']})")
        print(
            f"\nSoak status: {result['status']} — "
            f"{result['summary']['passed']} passed, {result['summary']['failed']} waiting"
        )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
