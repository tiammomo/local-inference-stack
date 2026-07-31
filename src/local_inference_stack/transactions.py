"""Durable, atomic transaction state for runtime mutations and recovery."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .paths import ProjectPaths
from .result import RecoveryError


SCHEMA_VERSION = 1
TERMINAL_STATES = {"completed", "failed"}
ALLOWED_TRANSITIONS = {
    "planned": {"production_stopping", "candidate_starting", "deploying", "failed"},
    "deploying": {"accepting", "production_restoring", "completed", "failed"},
    "production_stopping": {"candidate_starting", "production_restoring", "failed"},
    "candidate_starting": {"accepting", "production_restoring", "failed"},
    "accepting": {"production_restoring", "completed", "failed"},
    "production_restoring": {"completed", "failed"},
    "failed": {"production_restoring", "completed"},
    "completed": set(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class TransactionStore:
    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        self.path = paths.transaction_path
        self.lock_path = paths.state_dir / "transaction.lock"

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        try:
            metadata = self.path.lstat()
        except OSError as exc:
            raise RecoveryError(f"cannot inspect transaction state: {exc}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RecoveryError(
                "transaction state is not a private current-user regular file"
            )
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"transaction state is unreadable: {exc}") from exc
        self._validate(document)
        return document

    @staticmethod
    def _validate(document: Any) -> None:
        if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
            raise RecoveryError("transaction state has an unsupported schema")
        if document.get("state") not in ALLOWED_TRANSITIONS:
            raise RecoveryError("transaction state contains an unknown state")
        if not isinstance(document.get("id"), str) or not document["id"]:
            raise RecoveryError("transaction state has no id")
        if not isinstance(document.get("history"), list) or not document["history"]:
            raise RecoveryError("transaction state has no history")

    def begin(self, operation: str, target: str, original: dict[str, Any]) -> dict[str, Any]:
        with self.locked():
            existing = self.read()
            if existing and existing["state"] not in TERMINAL_STATES:
                raise RecoveryError(
                    "an unfinished runtime transaction must be reconciled first",
                    facts={"transactionId": existing["id"], "state": existing["state"]},
                )
            now = utc_now()
            document = {
                "schemaVersion": SCHEMA_VERSION,
                "id": str(uuid.uuid4()),
                "operation": operation,
                "target": target,
                "state": "planned",
                "createdAt": now,
                "updatedAt": now,
                "original": original,
                "history": [{"state": "planned", "at": now}],
                "recovery": {"action": "restore-production", "automatic": True},
            }
            _atomic_json(self.path, document)
            return document

    def transition(self, target_state: str, *, detail: str | None = None) -> dict[str, Any]:
        with self.locked():
            document = self.read()
            if not document:
                raise RecoveryError("there is no transaction to update")
            current = document["state"]
            if target_state not in ALLOWED_TRANSITIONS[current]:
                raise RecoveryError(f"invalid transaction transition: {current} -> {target_state}")
            now = utc_now()
            event: dict[str, Any] = {"state": target_state, "at": now}
            if detail:
                event["detail"] = detail[:500]
            document["state"] = target_state
            document["updatedAt"] = now
            document["history"].append(event)
            _atomic_json(self.path, document)
            return document

    def reconciliation_plan(self) -> dict[str, Any]:
        document = self.read()
        if not document or document["state"] in TERMINAL_STATES:
            return {"required": False, "transaction": document, "actions": []}
        return {
            "required": True,
            "transaction": document,
            "actions": [
                "stop any release-candidate runtime",
                "restore the recorded production profile through the hardened runtime wrapper",
                "verify loopback health and configured runtime identity",
                "mark the transaction completed only after restoration succeeds",
            ],
        }
