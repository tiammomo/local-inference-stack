"""Explicit schema compatibility and migration checks."""

from __future__ import annotations

import json
from typing import Any

from . import configuration
from .paths import ProjectPaths
from .rollout import RollbackStore, RollbackStoreError


CURRENT = {
    "runtimeProfiles": 2,
    "transaction": 2,
    "rollbackSpec": 1,
    "rollbackPointer": 1,
    "attestation": 2,
    "bundle": 2,
    "commandResult": 1,
}
READABLE = {name: {version} for name, version in CURRENT.items()}
READABLE["runtimeProfiles"] = {1, 2}
READABLE["transaction"] = {1, 2}
READABLE["bundle"] = {1, 2}


def check(paths: ProjectPaths) -> dict[str, Any]:
    runtime = json.loads(paths.config_path.read_text(encoding="utf-8"))
    observed: dict[str, Any] = {"runtimeProfiles": runtime.get("schemaVersion")}
    transaction: dict[str, Any] | None = None
    if paths.transaction_path.exists():
        transaction = json.loads(
            paths.transaction_path.read_text(encoding="utf-8")
        )
        observed["transaction"] = transaction.get("schemaVersion")
    rollback_store = RollbackStore(paths)
    intent = (transaction or {}).get("rolloutIntent")
    rollback_spec_sha256 = (
        intent.get("rollbackSpecSha256")
        if isinstance(intent, dict)
        and isinstance(intent.get("rollbackSpecSha256"), str)
        else None
    )
    pointer_expected = (
        rollback_store.pointer_path.exists()
        or rollback_store.pointer_path.is_symlink()
    )
    if pointer_expected or rollback_spec_sha256 is not None:
        try:
            pointer = rollback_store.read_pointer()
            if pointer is not None:
                observed["rollbackPointer"] = pointer.document().get(
                    "schemaVersion"
                )
                if rollback_spec_sha256 is None:
                    rollback_spec_sha256 = pointer.active_spec_sha256
            if rollback_spec_sha256 is not None:
                observed["rollbackSpec"] = rollback_store.read_spec(
                    rollback_spec_sha256
                ).document().get("schemaVersion")
        except RollbackStoreError:
            # A corrupt or unsupported active rollback object is incompatible,
            # not silently absent. Keep the report bounded and avoid echoing
            # private paths or document contents.
            if pointer_expected and "rollbackPointer" not in observed:
                observed["rollbackPointer"] = "invalid"
            if rollback_spec_sha256 is not None:
                observed["rollbackSpec"] = "invalid"
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
