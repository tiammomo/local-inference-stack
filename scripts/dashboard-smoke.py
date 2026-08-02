#!/usr/bin/env python3
"""Validate the operations dashboard health and production-status contract."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

try:
    from scripts.local_http import direct_urlopen
except ModuleNotFoundError:
    from local_http import direct_urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:33004"


def get_json(base_url: str, path: str, timeout: float) -> Any:
    with direct_urlopen(f"{base_url}{path}", timeout=timeout) as response:
        return json.load(response)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPERATIONS_DASHBOARD_URL", DEFAULT_BASE_URL),
    )
    parser.add_argument("--timeout", type=float, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        health = get_json(args.base_url, "/api/health", args.timeout)
        if health.get("status") != "ok":
            raise RuntimeError(f"dashboard health is not ok: {health}")
        status = get_json(args.base_url, "/api/status?hours=24", args.timeout)
        backups = status.get("health", {}).get("backups")
        host = status.get("process", {}).get("host")
        if not isinstance(backups, dict) or not isinstance(backups.get("available"), bool):
            raise RuntimeError("dashboard status is missing the backup availability contract")
        if not isinstance(host, dict) or not all(
            key in host for key in ("diskTotalBytes", "diskFreeBytes", "diskFreePercent")
        ):
            raise RuntimeError("dashboard status is missing the host disk contract")
        if not isinstance(status.get("alerts"), list):
            raise RuntimeError("dashboard status alerts must be an array")
    except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        print(
            f"operations dashboard preflight failed at {args.base_url}: {error}",
            file=sys.stderr,
        )
        print(
            "Refresh aggregate snapshots, start the loopback dashboard, then retry standard/full.",
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "backupAvailable": backups["available"],
                "backupAgeHours": backups.get("latestAgeHours"),
                "diskFreePercent": host["diskFreePercent"],
                "alerts": len(status["alerts"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
