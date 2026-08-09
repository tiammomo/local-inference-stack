"""Durable, atomic transaction state for runtime mutations and recovery."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .deployment import (
    CatalogDeploymentSpec,
    DeploymentSpecError,
    parse_approved_deployment,
)
from .paths import ProjectPaths
from .result import RecoveryError


SCHEMA_VERSION = 2
READABLE_SCHEMA_VERSIONS = {1, SCHEMA_VERSION}
TERMINAL_STATES = {"completed", "failed-restored", "superseded-verified"}
RECOVERY_DEPLOYMENT_KEYS = frozenset(
    {
        "QWEN_CATALOG_ID",
        "QWEN_MODEL_DIR",
        "QWEN_MODEL_FILE",
        "QWEN_MODEL_DISPLAY_NAME",
        "QWEN_QUANTIZATION",
        "QWEN_SERVED_MODEL_ID",
        "QWEN_CONTAINER_NAME",
        "QWEN_RUNTIME_UID",
        "QWEN_RUNTIME_GID",
        "MODELPORT_NETWORK_NAME",
        "QWEN_CTX_SIZE",
        "QWEN_RECOMMENDED_INPUT_TOKENS",
        "QWEN_N_PREDICT",
        "QWEN_CACHE_RAM",
        "QWEN_CACHE_TYPE_K",
        "QWEN_CACHE_TYPE_V",
        "QWEN_BATCH_SIZE",
        "QWEN_UBATCH_SIZE",
    }
)
RECOVERY_REQUIRED_DEPLOYMENT_KEYS = RECOVERY_DEPLOYMENT_KEYS - {
    "QWEN_CACHE_TYPE_K",
    "QWEN_CACHE_TYPE_V",
}
ALLOWED_TRANSITIONS = {
    "planned": {
        "production_stopping",
        "candidate_starting",
        "deploying",
        "recovery_required",
    },
    "deploying": {
        "accepting",
        "production_restoring",
        "completed",
        "recovery_required",
    },
    "production_stopping": {
        "candidate_starting",
        "production_restoring",
        "recovery_required",
    },
    "candidate_starting": {
        "accepting",
        "production_restoring",
        "recovery_required",
    },
    "accepting": {
        "production_restoring",
        "completed",
        "recovery_required",
    },
    "recovery_required": {"production_restoring", "superseded-verified"},
    "production_restoring": {
        "completed",
        "failed-restored",
        "recovery_required",
    },
    "completed": set(),
    "failed-restored": set(),
    "superseded-verified": set(),
}

LEGACY_STATES = {
    "planned",
    "deploying",
    "production_stopping",
    "candidate_starting",
    "accepting",
    "production_restoring",
    "failed",
    "completed",
}
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def _boot_id() -> str:
    try:
        value = BOOT_ID_PATH.read_text(encoding="utf-8").strip()
        return str(uuid.UUID(value))
    except (OSError, ValueError) as exc:
        raise RecoveryError("cannot establish the current Linux boot identity") from exc


def _process_start_time_ticks(pid: int) -> int | None:
    """Read Linux /proc field 22 without being confused by spaces in comm."""
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = value.rfind(")")
        if closing < 0:
            return None
        fields = value[closing + 2 :].split()
        start_time = int(fields[19])
        return start_time if start_time > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def transaction_owner() -> dict[str, Any]:
    pid = os.getpid()
    start_time = _process_start_time_ticks(pid)
    if start_time is None:
        raise RecoveryError("cannot establish the control-plane process identity")
    return {
        "pid": pid,
        "processStartTimeTicks": start_time,
        "bootId": _boot_id(),
        "capturedAt": utc_now(),
    }


def _valid_owner(owner: Any) -> bool:
    if not isinstance(owner, dict) or set(owner) != {
        "pid",
        "processStartTimeTicks",
        "bootId",
        "capturedAt",
    }:
        return False
    if (
        not isinstance(owner.get("pid"), int)
        or isinstance(owner.get("pid"), bool)
        or owner["pid"] <= 0
        or not isinstance(owner.get("processStartTimeTicks"), int)
        or isinstance(owner.get("processStartTimeTicks"), bool)
        or owner["processStartTimeTicks"] <= 0
        or not isinstance(owner.get("capturedAt"), str)
        or not owner["capturedAt"]
    ):
        return False
    try:
        return str(uuid.UUID(str(owner.get("bootId")))) == owner.get("bootId")
    except ValueError:
        return False


def is_terminal(document: dict[str, Any]) -> bool:
    schema = document.get("schemaVersion")
    state = document.get("state")
    if schema == 1:
        # A v1 `failed` record did not prove that recovery completed.  It is
        # deliberately non-terminal under the v2 reader.
        return state == "completed"
    return schema == SCHEMA_VERSION and state in TERMINAL_STATES


def _approved_deployment(document: dict[str, Any]) -> CatalogDeploymentSpec | None:
    spec_value = document.get("approvedCatalogSpec")
    digest_value = document.get("approvedCatalogSpecSha256")
    if spec_value is None and digest_value is None:
        return None
    try:
        spec = parse_approved_deployment(
            {
                "schemaVersion": 1,
                "approvedCatalogSpecSha256": digest_value,
                "catalogSpec": spec_value,
            }
        )
    except DeploymentSpecError as error:
        raise RecoveryError(f"transaction has an invalid approved Catalog spec: {error}") from error
    if document.get("operation") != "deploy" or document.get("target") != spec.catalog_id:
        raise RecoveryError(
            "transaction approved Catalog spec does not match its operation and target"
        )
    return spec


def _require_approved_deployment(
    document: dict[str, Any],
    *,
    catalog_spec_sha256: str,
    catalog_id: str,
    artifact_sha256: str | None = None,
) -> CatalogDeploymentSpec:
    spec = _approved_deployment(document)
    if spec is None:
        raise RecoveryError("deploy transaction has no persisted approved Catalog spec")
    if spec.sha256 != catalog_spec_sha256 or spec.catalog_id != catalog_id:
        raise RecoveryError(
            "runtime mutation does not match the transaction's approved Catalog spec"
        )
    if artifact_sha256 is not None:
        matches = [
            artifact
            for artifact in spec.artifacts
            if artifact.required and artifact.sha256 == artifact_sha256
        ]
        if len(matches) != 1:
            raise RecoveryError(
                "runtime mutation artifact does not match exactly one approved artifact"
            )
    return spec


def recovery_original_is_safe(original: Any) -> bool:
    """Return whether an original-runtime record is safe to restore automatically."""
    if not isinstance(original, dict) or original.get("capturedWithoutSecrets") is not True:
        return False
    healthy = original.get("healthy")
    if not isinstance(healthy, bool):
        return False
    profile = original.get("profile")
    if healthy and profile not in {"latency", "throughput"}:
        return False
    container_name = original.get("containerName")
    if container_name is not None and (
        not isinstance(container_name, str)
        or not container_name
        or len(container_name) > 128
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in container_name)
    ):
        return False
    deployment = original.get("deploymentProfile")
    if not isinstance(deployment, dict) or not isinstance(deployment.get("present"), bool):
        return False
    if deployment["present"]:
        values = deployment.get("values")
        digest = deployment.get("sha256")
        if (
            deployment.get("format") != "allowlisted-env-v1"
            or not isinstance(values, dict)
            or not RECOVERY_REQUIRED_DEPLOYMENT_KEYS.issubset(values)
            or not set(values).issubset(RECOVERY_DEPLOYMENT_KEYS)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items())
            or deployment.get("containsCredentials") is not False
            or not isinstance(digest, str)
            or hashlib.sha256(
                json.dumps(
                    values,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            != digest
        ):
            return False
    if healthy:
        identity = original.get("runtimeIdentity")
        if (
            original.get("containerHealthy") is not True
            or not isinstance(identity, dict)
            or not isinstance(identity.get("configuration"), dict)
            or not isinstance(identity.get("sha256"), str)
            or len(identity["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in identity["sha256"])
            or not container_name
        ):
            return False
    return True


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


def _prepare_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise RecoveryError(
            f"lock directory is not private and current-user-owned: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        path.chmod(0o700)


def open_private_lock(path: Path) -> int:
    """Open a non-symlink private lock file and validate the opened inode."""
    _prepare_private_directory(path.parent)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RecoveryError(f"cannot open the private lock file: {path}: {exc}") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise RecoveryError(
            f"lock file is not a private current-user single-link regular file: {path}"
        )
    return descriptor


class TransactionStore:
    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        self.path = paths.transaction_path
        self.lock_path = paths.state_dir / "transaction.lock"

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        descriptor = open_private_lock(self.lock_path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def runtime_boundary(self) -> Iterator[None]:
        """Exclude a shell runtime mutation while a new transaction is published."""
        path = self.paths.root / "cache" / "locks" / "runtime.lock"
        descriptor = open_private_lock(path)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RecoveryError(
                    "a runtime mutation already holds the local lock; retry after it finishes"
                ) from exc
            yield
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def authorized_runtime_mutation(
        self,
        transaction_id: str | None = None,
        *,
        catalog_spec_sha256: str | None = None,
        catalog_id: str | None = None,
        artifact_sha256: str | None = None,
    ) -> Iterator[None]:
        """Serialize a runtime mutation and reject unrelated active transactions.

        Runtime-facing shell wrappers use the same lock order: runtime first,
        transaction second.  Holding both locks for the complete mutation closes
        the gap between checking transaction state and changing a selection or
        container.
        """
        provided = transaction_id or ""
        if provided:
            try:
                if str(uuid.UUID(provided)) != provided:
                    raise ValueError
            except ValueError as exc:
                raise RecoveryError(
                    "QWEN_CONTROL_TRANSACTION_ID must be a canonical UUID"
                ) from exc

        path = self.paths.root / "cache" / "locks" / "runtime.lock"
        descriptor = open_private_lock(path)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RecoveryError(
                    "another runtime mutation already holds the local lock"
                ) from exc
            with self.locked():
                document = self.read()
                if document and not is_terminal(document):
                    if not provided or document.get("id") != provided:
                        raise RecoveryError(
                            "an active control transaction blocks this runtime mutation; "
                            "use the matching public control-plane command",
                            facts={
                                "transactionId": document.get("id"),
                                "schemaVersion": document.get("schemaVersion"),
                                "state": document.get("state"),
                            },
                        )
                    deployment_binding_required = bool(
                        document.get("schemaVersion") == SCHEMA_VERSION
                        and document.get("operation") == "deploy"
                        and document.get("state")
                        in {"planned", "deploying", "accepting"}
                    )
                    if (
                        deployment_binding_required
                        or catalog_spec_sha256 is not None
                        or catalog_id is not None
                        or artifact_sha256 is not None
                    ):
                        if catalog_spec_sha256 is None or catalog_id is None:
                            raise RecoveryError(
                                "deployment digest and Catalog ID must be supplied together"
                            )
                        _require_approved_deployment(
                            document,
                            catalog_spec_sha256=catalog_spec_sha256,
                            catalog_id=catalog_id,
                            artifact_sha256=artifact_sha256,
                        )
                elif provided:
                    raise RecoveryError(
                        "the authorized control transaction is missing or already terminal"
                    )
                yield
        finally:
            os.close(descriptor)

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
        if (
            not isinstance(document, dict)
            or document.get("schemaVersion") not in READABLE_SCHEMA_VERSIONS
        ):
            raise RecoveryError("transaction state has an unsupported schema")
        states = LEGACY_STATES if document["schemaVersion"] == 1 else set(ALLOWED_TRANSITIONS)
        if document.get("state") not in states:
            raise RecoveryError("transaction state contains an unknown state")
        if not isinstance(document.get("id"), str) or not document["id"]:
            raise RecoveryError("transaction state has no id")
        if not isinstance(document.get("history"), list) or not document["history"]:
            raise RecoveryError("transaction state has no history")
        if (
            document["schemaVersion"] == SCHEMA_VERSION
            and not is_terminal(document)
            and not _valid_owner(document.get("owner"))
        ):
            raise RecoveryError("active transaction state has no verifiable process owner")
        if document["schemaVersion"] == SCHEMA_VERSION:
            _approved_deployment(document)

    @staticmethod
    def initiator_status(document: dict[str, Any]) -> dict[str, Any]:
        """Classify whether a v2 transaction initiator can still be the same process."""
        owner = document.get("owner")
        if document.get("schemaVersion") != SCHEMA_VERSION or not _valid_owner(owner):
            return {
                "status": "unknown",
                "fenceEligible": False,
                "reason": "transaction has no verifiable v2 process owner",
            }
        assert isinstance(owner, dict)
        try:
            current_boot = _boot_id()
        except RecoveryError as exc:
            return {
                "status": "unknown",
                "fenceEligible": False,
                "reason": exc.summary,
            }
        if current_boot != owner["bootId"]:
            return {
                "status": "boot-changed",
                "fenceEligible": True,
                "reason": "transaction belongs to an earlier Linux boot",
            }
        observed_start = _process_start_time_ticks(owner["pid"])
        if observed_start is None:
            return {
                "status": "dead",
                "fenceEligible": True,
                "reason": "transaction initiator PID no longer exists",
            }
        if observed_start != owner["processStartTimeTicks"]:
            return {
                "status": "pid-reused",
                "fenceEligible": True,
                "reason": "transaction initiator PID was reused by another process",
            }
        return {
            "status": "alive",
            "fenceEligible": False,
            "reason": "transaction initiator process is still alive",
        }

    def begin(
        self,
        operation: str,
        target: str,
        original: dict[str, Any],
        *,
        approved_catalog_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runtime-facing wrappers acquire runtime.lock before transaction.lock.
        # Keep the same global order here so transaction publication cannot
        # deadlock with a concurrent select/start/stop operation.
        with self.runtime_boundary():
            with self.locked():
                existing = self.read()
                if existing and not is_terminal(existing):
                    raise RecoveryError(
                        "an unfinished runtime transaction must be reconciled first",
                        facts={
                            "transactionId": existing["id"],
                            "schemaVersion": existing["schemaVersion"],
                            "state": existing["state"],
                        },
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
                    "owner": transaction_owner(),
                    "original": original,
                    "history": [{"state": "planned", "at": now}],
                    "recovery": {
                        "action": "restore-production",
                        "automatic": recovery_original_is_safe(original),
                    },
                }
                if approved_catalog_spec is not None:
                    document["approvedCatalogSpecSha256"] = approved_catalog_spec.get(
                        "approvedCatalogSpecSha256"
                    )
                    document["approvedCatalogSpec"] = approved_catalog_spec.get(
                        "catalogSpec"
                    )
                    _approved_deployment(document)
                _atomic_json(self.path, document)
                return document

    def assert_approved_deployment(
        self,
        *,
        transaction_id: str,
        catalog_spec_sha256: str,
        catalog_id: str,
        artifact_sha256: str | None = None,
        inherited_locks: bool = False,
    ) -> CatalogDeploymentSpec:
        """Bind a child action to the still-active persisted deploy approval."""

        try:
            if str(uuid.UUID(transaction_id)) != transaction_id:
                raise ValueError
        except ValueError as error:
            raise RecoveryError("control transaction id must be a canonical UUID") from error
        if inherited_locks:
            expected = {
                203: self.paths.root / "cache" / "locks" / "runtime.lock",
                204: self.lock_path,
            }
            for descriptor, path in expected.items():
                try:
                    actual = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
                    wanted = path.resolve(strict=True)
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, RuntimeError) as error:
                    raise RecoveryError(
                        "inherited deployment lock descriptors are not valid"
                    ) from error
                if actual != wanted:
                    raise RecoveryError(
                        "inherited deployment lock descriptors target other files"
                    )
        boundary = contextlib.nullcontext() if inherited_locks else self.locked()
        with boundary:
            document = self.read()
            if (
                not document
                or is_terminal(document)
                or document.get("schemaVersion") != SCHEMA_VERSION
                or document.get("id") != transaction_id
            ):
                raise RecoveryError(
                    "approved Catalog deployment transaction is missing, changed, or terminal"
                )
            return _require_approved_deployment(
                document,
                catalog_spec_sha256=catalog_spec_sha256,
                catalog_id=catalog_id,
                artifact_sha256=artifact_sha256,
            )

    def fence_orphaned(self, *, expected_id: str) -> dict[str, Any]:
        """Fence a proven-dead initiator while excluding descendant runtime work."""
        with self.runtime_boundary():
            with self.locked():
                document = self.read()
                if not document or document.get("id") != expected_id:
                    raise RecoveryError("orphaned transaction identity changed")
                if (
                    document.get("schemaVersion") != SCHEMA_VERSION
                    or document.get("state") == "recovery_required"
                    or is_terminal(document)
                ):
                    raise RecoveryError(
                        "only an active v2 transaction can be fenced as orphaned"
                    )
                initiator = self.initiator_status(document)
                if not initiator["fenceEligible"]:
                    raise RecoveryError(
                        "active transaction initiator cannot be proven dead",
                        facts={"initiator": initiator},
                    )
                now = utc_now()
                detail = f"orphaned initiator fenced: {initiator['status']}"
                document["state"] = "recovery_required"
                document["updatedAt"] = now
                document["history"].append(
                    {
                        "state": "recovery_required",
                        "at": now,
                        "detail": detail,
                        "fence": {
                            "policy": "dead-process-and-free-runtime-lock-v1",
                            "initiatorStatus": initiator["status"],
                        },
                    }
                )
                _atomic_json(self.path, document)
                return document

    def transition(
        self,
        target_state: str,
        *,
        expected_id: str,
        detail: str | None = None,
    ) -> dict[str, Any]:
        with self.locked():
            document = self.read()
            if not document:
                raise RecoveryError("there is no transaction to update")
            if document.get("id") != expected_id:
                raise RecoveryError(
                    "transaction identity changed before the requested transition",
                    facts={
                        "expectedTransactionId": expected_id,
                        "actualTransactionId": document.get("id"),
                    },
                )
            if document["schemaVersion"] != SCHEMA_VERSION:
                raise RecoveryError(
                    "legacy transaction state is read-only; use explicit reconciliation"
                )
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

    def resolve_legacy_failed(
        self, target_state: str, *, expected_id: str, detail: str
    ) -> dict[str, Any]:
        """Explicitly resolve a verified v1 failure into a v2 terminal state."""
        if target_state not in {"failed-restored", "superseded-verified"}:
            raise RecoveryError(f"invalid legacy resolution: {target_state}")
        with self.locked():
            document = self.read()
            if (
                not document
                or document.get("id") != expected_id
                or document.get("schemaVersion") != 1
                or document.get("state") != "failed"
            ):
                raise RecoveryError("there is no legacy failed transaction to resolve")
            now = utc_now()
            document["schemaVersion"] = SCHEMA_VERSION
            document["state"] = target_state
            document["updatedAt"] = now
            document["history"].append(
                {
                    "state": target_state,
                    "at": now,
                    "detail": detail[:500],
                    "legacySchemaVersion": 1,
                }
            )
            document["recovery"] = {
                "action": "none",
                "automatic": False,
                "resolution": target_state,
            }
            _atomic_json(self.path, document)
            return document

    def resolve_legacy_active(
        self, target_state: str, *, expected_id: str, detail: str
    ) -> dict[str, Any]:
        """Resolve a reviewed, non-terminal v1 interruption after verification."""
        if target_state not in {"failed-restored", "superseded-verified"}:
            raise RecoveryError(f"invalid legacy resolution: {target_state}")
        with self.locked():
            document = self.read()
            if (
                not document
                or document.get("id") != expected_id
                or document.get("schemaVersion") != 1
                or document.get("state") in {"failed", "completed"}
            ):
                raise RecoveryError("there is no legacy active transaction to resolve")
            now = utc_now()
            legacy_state = document["state"]
            document["schemaVersion"] = SCHEMA_VERSION
            document["state"] = target_state
            document["updatedAt"] = now
            document["history"].append(
                {
                    "state": target_state,
                    "at": now,
                    "detail": detail[:500],
                    "legacySchemaVersion": 1,
                    "legacyState": legacy_state,
                }
            )
            document["recovery"] = {
                "action": "none",
                "automatic": False,
                "resolution": target_state,
            }
            _atomic_json(self.path, document)
            return document

    def reconciliation_plan(self) -> dict[str, Any]:
        document = self.read()
        if not document:
            return {
                "required": False,
                "classification": "no-transaction",
                "automaticEligible": False,
                "transaction": None,
                "actions": [],
            }
        if is_terminal(document):
            return {
                "required": False,
                "classification": "verified-terminal",
                "automaticEligible": False,
                "transaction": document,
                "actions": [],
            }
        if document["schemaVersion"] == 1:
            classification = (
                "legacy-failed-review-required"
                if document["state"] == "failed"
                else "legacy-active-review-required"
            )
            return {
                "required": True,
                "classification": classification,
                "automaticEligible": False,
                "transaction": document,
                "actions": [
                    "inspect the current runtime without mutating it",
                    "preserve any healthy current runtime until it is verified",
                    "use explicit reconciliation to classify and resolve the legacy record",
                ],
            }
        automatic = bool(
            document["state"] == "recovery_required"
            and recovery_original_is_safe(document.get("original"))
        )
        initiator = (
            self.initiator_status(document)
            if document["state"] != "recovery_required"
            else None
        )
        actions = []
        if initiator is not None:
            actions.append(
                "run explicit reconciliation to fence the proven-dead initiator"
                if initiator["fenceEligible"]
                else "wait for the active initiator or inspect it before recovery"
            )
        actions.extend(
            [
                "stop any release-candidate runtime",
                "restore the recorded production profile through the hardened runtime wrapper",
                "verify loopback health and configured runtime identity",
                "mark the transaction completed only after restoration succeeds",
            ]
        )
        return {
            "required": True,
            "classification": (
                "recovery-required"
                if document["state"] == "recovery_required"
                else (
                    "orphaned-active-transaction-review-required"
                    if initiator and initiator["fenceEligible"]
                    else "active-transaction-review-required"
                )
            ),
            "automaticEligible": automatic,
            "initiator": initiator,
            "transaction": document,
            "actions": actions,
        }
