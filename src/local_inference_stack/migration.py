"""Explicit schema compatibility and migration checks."""

from __future__ import annotations

import json
from typing import Any

from . import configuration
from .paths import ProjectPaths


CURRENT = {"runtimeProfiles": 2, "transaction": 2, "attestation": 2, "bundle": 2, "commandResult": 1}
READABLE = {name: {version} for name, version in CURRENT.items()}
READABLE["runtimeProfiles"] = {1, 2}
READABLE["transaction"] = {1, 2}
READABLE["bundle"] = {1, 2}


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
    selected_profile = configuration.selected_deployment_profile_status(paths)
    if selected_profile.get("migrationRequired") is True:
        migrations["selectedDeploymentProfile"] = {
            "from": selected_profile["status"],
            "to": "exact-current-projection",
            "automatic": False,
        }
    return {
        "current": CURRENT,
        "readable": {name: sorted(versions) for name, versions in READABLE.items()},
        "observed": observed,
        "compatible": not incompatible,
        "incompatible": incompatible,
        "migrationsRequired": migrations,
        "selectedDeploymentProfile": selected_profile,
        "policy": (
            "runtimeProfiles and transaction v1 are read-only; bundle v1 is "
            "readable only when no legacy unbound image archive is present; "
            "attestation v1 is rejected; a compatible private selected profile "
            "is normalized only by explicit --yes after artifact verification; "
            "migrations are never silent"
        ),
    }
