"""Explicit schema compatibility and migration checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import ProjectPaths


CURRENT = {"runtimeProfiles": 1, "transaction": 1, "attestation": 1, "bundle": 1, "commandResult": 1}
READABLE = {name: {version} for name, version in CURRENT.items()}


def check(paths: ProjectPaths) -> dict[str, Any]:
    runtime = json.loads(paths.config_path.read_text(encoding="utf-8"))
    observed = {"runtimeProfiles": runtime.get("schemaVersion")}
    if paths.transaction_path.exists():
        observed["transaction"] = json.loads(paths.transaction_path.read_text(encoding="utf-8")).get("schemaVersion")
    incompatible = {
        name: version
        for name, version in observed.items()
        if version not in READABLE[name]
    }
    migrations = {
        name: {"from": version, "to": CURRENT[name], "automatic": False}
        for name, version in observed.items()
        if version in READABLE[name] and version != CURRENT[name]
    }
    return {
        "current": CURRENT,
        "readable": {name: sorted(versions) for name, versions in READABLE.items()},
        "observed": observed,
        "compatible": not incompatible,
        "incompatible": incompatible,
        "migrationsRequired": migrations,
        "policy": "when schema v2 is introduced, v1 becomes read-only N-1; migrations are never silent",
    }
