"""Offline candidate-profile planning; calibration never edits production configuration."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .configuration import load
from .paths import ProjectPaths
from .runner import run


def plan(paths: ProjectPaths) -> dict[str, Any]:
    profiles = load(paths)["profiles"]
    latency = profiles["latency"]["environment"]
    return {
        "baseline": "latency",
        "fixed": {
            "QWEN_CTX_SIZE": latency["QWEN_CTX_SIZE"],
            "QWEN_N_PREDICT": latency["QWEN_N_PREDICT"],
        },
        "candidates": [
            {"name": "latency-baseline", "parallel": 1, "batchSize": 2048, "ubatchSize": 1024},
            {"name": "latency-conservative", "parallel": 1, "batchSize": 1024, "ubatchSize": 512},
            {"name": "throughput-two-slot", "parallel": 2, "batchSize": 2048, "ubatchSize": 1024},
        ],
        "applicationPolicy": "report-only; explicit reviewed profile change required",
    }


def run_benchmarks(paths: ProjectPaths, output: Path) -> dict[str, Any]:
    # Existing benchmark scripts are authoritative for real measurements.
    decode = run(
        ["python3", "scripts/decode-benchmark.py"],
        cwd=paths.root,
        timeout=600,
        check=False,
    )
    document = {
        "schemaVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "plan": plan(paths),
        "measurements": {
            "decode": {
                "exitCode": decode.returncode,
                "stdout": decode.stdout[-20000:],
                "stderr": decode.stderr[-2000:],
            }
        },
        "productionProfileModified": False,
    }
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"output": str(output), "benchmarkExitCode": decode.returncode, "productionProfileModified": False}
