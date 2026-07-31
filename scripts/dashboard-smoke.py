#!/usr/bin/env python3
"""Validate the operations dashboard health and production-status contract."""

from __future__ import annotations

import json
from typing import Any

try:
    from scripts.local_http import direct_urlopen
except ModuleNotFoundError:
    from local_http import direct_urlopen


BASE_URL = "http://127.0.0.1:33004"


def get_json(path: str) -> Any:
    with direct_urlopen(f"{BASE_URL}{path}", timeout=30) as response:
        return json.load(response)


def main() -> int:
    health = get_json("/api/health")
    if health.get("status") != "ok":
        raise RuntimeError(f"dashboard health is not ok: {health}")
    status = get_json("/api/status?hours=24")
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
