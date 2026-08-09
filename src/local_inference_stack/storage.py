"""Reference-aware local storage inventory and conservative garbage collection."""

from __future__ import annotations

import json
import fcntl
import os
import time
from pathlib import Path
from typing import Any

from .catalog import CatalogError, load_catalog
from .paths import ProjectPaths
from .result import ConfigError
from .transactions import TERMINAL_STATES


PROTECTED_SUFFIXES = {".gguf", ".env", ".json"}


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def inventory(paths: ProjectPaths) -> dict[str, Any]:
    categories: dict[str, Any] = {}
    for name in ("models", "cache", "logs", "backups"):
        root = paths.root / name
        files = [item for item in root.rglob("*") if item.is_file()] if root.exists() else []
        categories[name] = {"files": len(files), "bytes": sum(_size(item) for item in files)}
    protected: list[str] = []
    deployment = paths.root / "profiles" / "deployment.local.env"
    if deployment.exists():
        protected.append(str(deployment.relative_to(paths.root)))
    transaction = paths.transaction_path
    if transaction.exists():
        protected.append(str(transaction.relative_to(paths.root)))
    return {"categories": categories, "protectedReferences": protected}


def gc_candidates(paths: ProjectPaths, *, older_than_days: int = 14) -> list[dict[str, Any]]:
    if older_than_days < 1:
        raise ConfigError("older-than-days must be at least 1")
    cutoff = time.time() - older_than_days * 86400
    candidates: list[dict[str, Any]] = []
    if paths.transaction_path.exists():
        try:
            transaction = json.loads(paths.transaction_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if transaction.get("state") not in TERMINAL_STATES:
            return []
    catalog_path = paths.root / "catalog" / "models.json"
    directory_locks: dict[str, Path] = {}
    if catalog_path.is_file():
        try:
            catalog = load_catalog(catalog_path)
            directory_locks = {
                model["modelDirectory"]: paths.root / "cache" / "locks" / f"download-{model['id']}.lock"
                for model in catalog["models"]
            }
        except CatalogError:
            return []
    # Only interrupted download fragments and known temporary files are eligible.
    patterns = ((paths.root / "models", "*.part"), (paths.root / "cache", "*.tmp"))
    for root, pattern in patterns:
        if not root.exists():
            continue
        for item in root.rglob(pattern):
            try:
                if item.is_symlink():
                    continue
                if root.name == "models":
                    relative = item.relative_to(root)
                    lock = directory_locks.get(relative.parts[0]) if relative.parts else None
                    if lock and _lock_active(lock):
                        continue
                resolved = item.resolve(strict=True)
                resolved.relative_to(root.resolve())
                stat = resolved.stat()
            except (OSError, ValueError):
                continue
            if resolved.is_file() and stat.st_mtime < cutoff:
                candidates.append(
                    {
                        "path": str(resolved.relative_to(paths.root)),
                        "bytes": stat.st_size,
                        "reason": f"unreferenced temporary file older than {older_than_days} days",
                    }
                )
    return sorted(candidates, key=lambda item: item["path"])


def _lock_active(path: Path) -> bool:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        return False
    finally:
        os.close(descriptor)


def delete_candidates(paths: ProjectPaths, candidates: list[dict[str, Any]]) -> list[str]:
    removed: list[str] = []
    allowed_roots = [(paths.root / "models").resolve(), (paths.root / "cache").resolve()]
    for candidate in candidates:
        target = (paths.root / candidate["path"]).resolve(strict=True)
        if not any(target.is_relative_to(root) for root in allowed_roots):
            raise ConfigError(f"refusing to delete outside managed storage: {target}")
        if target.suffix not in {".part", ".tmp"} or target.is_symlink():
            raise ConfigError(f"refusing to delete protected storage object: {target}")
        target.unlink()
        removed.append(candidate["path"])
    return removed
