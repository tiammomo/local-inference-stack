"""Offline candidate-profile planning; calibration never edits production configuration."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import CatalogError, load_catalog, model_by_id
from .configuration import catalog_runtime_environment, load
from .paths import ProjectPaths
from .result import ConfigError
from .runner import run


def plan(paths: ProjectPaths) -> dict[str, Any]:
    profiles = load(paths)["profiles"]
    try:
        catalog = load_catalog(paths.root / "catalog" / "models.json")
        selected_id = catalog["defaultModel"]
        local_profile = paths.root / "profiles" / "deployment.local.env"
        if local_profile.is_file():
            from scripts.env_utils import is_private_regular_file, parse_env_file

            if not is_private_regular_file(local_profile):
                raise ConfigError(
                    "deployment.local.env is not a private current-user regular file"
                )
            selected_id = parse_env_file(local_profile).get(
                "QWEN_CATALOG_ID", selected_id
            )
        model = model_by_id(catalog, selected_id)
        runtime = model["runtime"]
        catalog_environment = catalog_runtime_environment(model)
    except CatalogError as exc:
        raise ConfigError("cannot derive calibration inputs from the Catalog") from exc

    baseline_batch = int(runtime["batchSize"])
    baseline_ubatch = int(runtime["ubatchSize"])
    return {
        "catalogModel": selected_id,
        "baseline": "latency",
        "fixedModelCapacity": catalog_environment,
        "candidates": [
            {
                "name": "latency-baseline",
                "parallel": int(profiles["latency"]["environment"]["QWEN_PARALLEL"]),
                "batchSize": baseline_batch,
                "ubatchSize": baseline_ubatch,
            },
            {
                "name": "latency-conservative",
                "parallel": int(profiles["latency"]["environment"]["QWEN_PARALLEL"]),
                "batchSize": max(1, baseline_batch // 2),
                "ubatchSize": max(1, baseline_ubatch // 2),
            },
            {
                "name": "throughput-two-slot",
                "parallel": int(
                    profiles["throughput"]["environment"]["QWEN_PARALLEL"]
                ),
                "batchSize": baseline_batch,
                "ubatchSize": baseline_ubatch,
            },
        ],
        "applicationPolicy": "report-only; explicit reviewed profile change required",
    }


def run_benchmarks(paths: ProjectPaths, output: Path) -> dict[str, Any]:
    # Existing benchmark scripts are authoritative for real measurements.  A
    # calibration run is explicitly non-promotable until reviewed thresholds
    # are written to the deployment manifest.
    decode = run(
        ["python3", "scripts/decode-benchmark.py", "--baseline-only", "--json"],
        cwd=paths.root,
        timeout=600,
        check=False,
    )
    concurrency = run(
        [
            "python3",
            "scripts/concurrency-benchmark.py",
            "--baseline-only",
            "--json",
        ],
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
            },
            "concurrency": {
                "exitCode": concurrency.returncode,
                "stdout": concurrency.stdout[-20000:],
                "stderr": concurrency.stderr[-2000:],
            },
        },
        "evidenceEligibility": "baseline-only-not-promotable",
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
    return {
        "output": str(output),
        "benchmarkExitCodes": {
            "decode": decode.returncode,
            "concurrency": concurrency.returncode,
        },
        "measurementsComplete": decode.returncode == 0 and concurrency.returncode == 0,
        "evidenceEligibility": "baseline-only-not-promotable",
        "productionProfileModified": False,
    }
