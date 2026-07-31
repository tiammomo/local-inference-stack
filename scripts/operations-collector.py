#!/usr/bin/env python3
"""Short-lived credentialed collector that writes aggregate-only dashboard snapshots."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

try:
    from scripts.env_utils import atomic_write_private_text
except ModuleNotFoundError:
    from env_utils import atomic_write_private_text


ROOT_DIR = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT_DIR / "logs" / "operations"


def load_report_module() -> ModuleType:
    path = ROOT_DIR / "scripts" / "operations-report.py"
    spec = importlib.util.spec_from_file_location("qwen_operations_report_collector", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load operations report module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", nargs="+", type=float, default=[1.0, 6.0, 24.0, 168.0])
    return parser.parse_args()


def report_args(hours: float) -> SimpleNamespace:
    return SimpleNamespace(
        hours=hours,
        modelport_url=os.environ.get("MODELPORT_BASE_URL", "http://127.0.0.1:38082"),
        qwen_url=os.environ.get("QWEN_RUNTIME_URL", "http://127.0.0.1:18080"),
        max_records=int(os.environ.get("OPERATIONS_DASHBOARD_MAX_RECORDS", "5000")),
        unreconciled_baseline=int(os.environ.get("OPERATIONS_UNRECONCILED_BASELINE", "0")),
        include_synthetic=False,
        provider=["local_qwen"],
        resolved_model=[],
        failure_rate_warn=float(os.environ.get("OPERATIONS_FAILURE_RATE_WARN", "0.05")),
        tool_failure_rate_warn=float(os.environ.get("OPERATIONS_TOOL_FAILURE_RATE_WARN", "0.05")),
        p95_latency_ms_warn=int(os.environ.get("OPERATIONS_P95_LATENCY_MS_WARN", "180000")),
        disk_free_percent_warn=float(os.environ.get("OPERATIONS_DISK_FREE_PERCENT_WARN", "10")),
        disk_free_bytes_warn=int(os.environ.get("OPERATIONS_DISK_FREE_BYTES_WARN", str(20 * 1024**3))),
        backup_max_age_hours=float(os.environ.get("OPERATIONS_BACKUP_MAX_AGE_HOURS", "36")),
    )


def main() -> int:
    args = parse_args()
    username = os.environ.get("MODELPORT_ADMIN_USERNAME", "")
    password = os.environ.get("MODELPORT_ADMIN_PASSWORD", "")
    if not username or not password:
        raise SystemExit("collector credentials are not configured")
    module = load_report_module()
    admin = module.AdminClient(
        os.environ.get("MODELPORT_BASE_URL", "http://127.0.0.1:38082"),
        username,
        password,
    )
    REPORT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for hours in args.windows:
        if hours not in {1.0, 6.0, 24.0, 168.0}:
            raise SystemExit(f"unsupported dashboard window: {hours}")
        report = module.build_report(report_args(hours), admin=admin)
        if report.get("privacy", {}).get("mode") != "aggregate-only":
            raise SystemExit("collector refused a non-aggregate report")
        label = str(int(hours))
        output = REPORT_DIR / f"latest-{label}.json"
        atomic_write_private_text(
            output,
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
