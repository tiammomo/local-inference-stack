"""Canonical, policy-bound snapshots of repository materials.

The public digests produced here intentionally retain the original wire format:
files are raw SHA256 values and file sets are the SHA256 of a canonical JSON
``[{"path": ..., "sha256": ...}]`` list.  Policy identifiers are checked by
callers before a snapshot is produced; they are not silently mixed into a legacy
digest and therefore cannot invalidate existing validation records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping


CANONICAL_JSON_POLICY_ID = "local-inference-stack/canonical-json-v1"
FILE_SHA256_POLICY_ID = "local-inference-stack/file-sha256-v1"
FILE_SET_SHA256_POLICY_ID = (
    "local-inference-stack/canonical-path-file-set-sha256-v1"
)
_POLICY_ID = re.compile(r"[a-z0-9][a-z0-9._/-]*-v[1-9][0-9]*")
_MATERIAL_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]*")


class MaterialError(RuntimeError):
    """Base error for an unsafe or incomplete material snapshot."""


class MaterialPolicyDrift(MaterialError):
    """Raised when a caller and a snapshot declaration disagree on policy."""


class MaterialCoverageError(MaterialError):
    """Raised when a material declaration is empty, ambiguous, or escapes root."""


def canonical_bytes(value: Any) -> bytes:
    """Encode one JSON value using the repository's stable canonical form."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return SHA256 over :func:`canonical_bytes`."""

    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def cleanup_interrupted_noreplace_link_at(
    directory_descriptor: int,
    name: str,
) -> bool:
    """Repair only the exact hard-link residue of a no-replace publication.

    The portable ``link(temp, final); unlink(temp)`` sequence has a crash
    window where the immutable final object has two links.  Callers must hold
    the store's writer lock and pass an already verified private directory fd.
    An arbitrary hard link remains rejected: cleanup requires exactly one
    internal temp name with the expected nonce shape and the same inode.
    """

    prefix = f".{name}."
    suffix = ".tmp"
    try:
        final = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(final.st_mode)
        or final.st_uid != os.getuid()
        or stat.S_IMODE(final.st_mode) != 0o600
        or final.st_nlink != 2
    ):
        return False
    candidates: list[str] = []
    for entry in os.listdir(directory_descriptor):
        if not (
            entry.startswith(prefix)
            and entry.endswith(suffix)
            and len(entry) == len(prefix) + 32 + len(suffix)
        ):
            continue
        nonce = entry[len(prefix) : -len(suffix)]
        if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
            continue
        try:
            temporary = os.stat(
                entry,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if (
            stat.S_ISREG(temporary.st_mode)
            and temporary.st_uid == os.getuid()
            and stat.S_IMODE(temporary.st_mode) == 0o600
            and temporary.st_nlink == 2
            and (temporary.st_dev, temporary.st_ino)
            == (final.st_dev, final.st_ino)
        ):
            candidates.append(entry)
    if len(candidates) != 1:
        return False
    os.unlink(candidates[0], dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)
    repaired = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if (
        repaired.st_nlink != 1
        or (repaired.st_dev, repaired.st_ino) != (final.st_dev, final.st_ino)
    ):
        raise OSError("interrupted no-replace publication did not stabilize")
    return True


def _file_set_sha256(entries: list[dict[str, str]]) -> str:
    """Preserve the original file-set v1 JSON wire encoding.

    File-set v1 predates :data:`CANONICAL_JSON_POLICY_ID` and used the
    ``json.dumps`` default ``ensure_ascii=True``.  Unicode paths therefore need
    this dedicated encoder; silently routing them through ``canonical_bytes``
    would redefine an existing digest policy.
    """

    body = json.dumps(
        entries,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise MaterialError("safe material reads require O_NOFOLLOW")
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | nofollow


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _open_material(path: Path) -> tuple[int, os.stat_result]:
    """Open a regular file without following any path-component symlink."""

    absolute = _absolute_path(path)
    descriptor: int | None = None
    parent_descriptor: int | None = None
    try:
        parent_descriptor = os.open(absolute.anchor, _directory_flags())
        for component in absolute.parts[1:-1]:
            child = os.open(component, _directory_flags(), dir_fd=parent_descriptor)
            os.close(parent_descriptor)
            parent_descriptor = child
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(absolute.name, flags, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or mode & 0o022
            or mode & 0o7000
        ):
            raise MaterialError(
                "material must be a current-user regular file with one link, "
                f"no special bits, and no group/other write permission: {path}"
            )
        result = descriptor, metadata
        descriptor = None
        return result
    except MaterialError:
        raise
    except OSError as error:
        raise MaterialError(f"cannot safely open material: {path}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
    )


def sha256_file(path: Path, *, maximum_bytes: int | None = None) -> str:
    """Hash one stable, safely opened regular file."""

    if maximum_bytes is not None and maximum_bytes < 0:
        raise ValueError("maximum_bytes cannot be negative")
    descriptor, before = _open_material(path)
    digest = hashlib.sha256()
    total = 0
    try:
        if maximum_bytes is not None and before.st_size > maximum_bytes:
            raise MaterialError(f"material exceeds the size limit: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if maximum_bytes is not None and total > maximum_bytes:
                raise MaterialError(f"material exceeds the size limit: {path}")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if total != before.st_size or _stat_identity(after) != _stat_identity(before):
            raise MaterialError(f"material changed while it was hashed: {path}")
        current_descriptor, current = _open_material(path)
        try:
            if _stat_identity(current) != _stat_identity(before):
                raise MaterialError(
                    f"material path changed while it was hashed: {path}"
                )
        finally:
            os.close(current_descriptor)
        return digest.hexdigest()
    except MaterialError:
        raise
    except OSError as error:
        raise MaterialError(f"cannot read material: {path}: {error}") from error
    finally:
        os.close(descriptor)


def read_file_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    """Read one bounded material from a stable, safely opened descriptor."""

    if maximum_bytes < 0:
        raise ValueError("maximum_bytes cannot be negative")
    descriptor, before = _open_material(path)
    chunks: list[bytes] = []
    total = 0
    try:
        if before.st_size > maximum_bytes:
            raise MaterialError(f"material exceeds the size limit: {path}")
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise MaterialError(f"material exceeds the size limit: {path}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if total != before.st_size or _stat_identity(after) != _stat_identity(before):
            raise MaterialError(f"material changed while it was read: {path}")
        current_descriptor, current = _open_material(path)
        try:
            if _stat_identity(current) != _stat_identity(before):
                raise MaterialError(f"material path changed while it was read: {path}")
        finally:
            os.close(current_descriptor)
        return b"".join(chunks)
    except MaterialError:
        raise
    except OSError as error:
        raise MaterialError(f"cannot read material: {path}: {error}") from error
    finally:
        os.close(descriptor)


def read_private_json_file(
    path: Path,
    *,
    root: Path,
    maximum_bytes: int,
) -> tuple[dict[str, Any], str]:
    """Read and hash one private JSON object beneath an exact filesystem root.

    Every path component is opened with ``O_NOFOLLOW`` through
    :func:`_open_material`.  Parsing and hashing consume the same stable byte
    string, and a second component-wise open proves that the named path still
    resolves to the inspected inode after the read.
    """

    if maximum_bytes < 0:
        raise ValueError("maximum_bytes cannot be negative")
    absolute_root = _absolute_path(root)
    absolute_path = _absolute_path(path if path.is_absolute() else root / path)
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as error:
        raise MaterialCoverageError(
            f"private JSON is outside the trusted root: {path}"
        ) from error
    if not relative.parts:
        raise MaterialCoverageError("the trusted root cannot be a private JSON file")

    descriptor, before = _open_material(absolute_path)
    chunks: list[bytes] = []
    total = 0
    try:
        mode = stat.S_IMODE(before.st_mode)
        if mode & 0o077 or mode & 0o7000:
            raise MaterialError(
                f"private JSON must be owner-only with no special bits: {path}"
            )
        if before.st_size > maximum_bytes:
            raise MaterialError(f"private JSON exceeds the size limit: {path}")
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum_bytes + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise MaterialError(f"private JSON exceeds the size limit: {path}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if total != before.st_size or _stat_identity(after) != _stat_identity(before):
            raise MaterialError(f"private JSON changed while it was read: {path}")
        current_descriptor, current = _open_material(absolute_path)
        try:
            if _stat_identity(current) != _stat_identity(before):
                raise MaterialError(
                    f"private JSON path changed while it was read: {path}"
                )
        finally:
            os.close(current_descriptor)
    except MaterialError:
        raise
    except OSError as error:
        raise MaterialError(f"cannot read private JSON: {path}: {error}") from error
    finally:
        os.close(descriptor)

    body = b"".join(chunks)

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("duplicate JSON key")
            document[key] = value
        return document

    try:
        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON number: {value}")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise MaterialError(f"private JSON is not strict JSON: {path}") from error
    if not isinstance(document, dict):
        raise MaterialError(f"private JSON must contain an object: {path}")
    return document, hashlib.sha256(body).hexdigest()


def _relative_path(path: Path, root: Path) -> str:
    absolute_root = _absolute_path(root)
    absolute_path = _absolute_path(path if path.is_absolute() else root / path)
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as error:
        raise MaterialCoverageError(
            f"material is outside the snapshot root: {path}"
        ) from error
    if not relative.parts:
        raise MaterialCoverageError("the snapshot root cannot be a material")
    return relative.as_posix()


def sha256_file_set(
    paths: Iterable[Path],
    *,
    root: Path,
    file_hasher: Callable[[Path], str] | None = None,
) -> str:
    """Hash a deterministic canonical path/hash list.

    Enumeration order and host filesystem metadata never affect this digest.
    Duplicate paths are rejected instead of being silently counted twice.
    """

    hasher = file_hasher or sha256_file
    normalized: dict[str, Path] = {}
    for path in paths:
        relative = _relative_path(path, root)
        if relative in normalized:
            raise MaterialCoverageError(
                f"material file set declares a duplicate path: {relative}"
            )
        normalized[relative] = path if path.is_absolute() else root / path
    entries = [
        {"path": relative, "sha256": hasher(normalized[relative])}
        for relative in sorted(normalized)
    ]
    return _file_set_sha256(entries)


def _validate_policy_id(policy_id: str) -> None:
    if not isinstance(policy_id, str) or _POLICY_ID.fullmatch(policy_id) is None:
        raise ValueError(f"invalid material policy ID: {policy_id!r}")


def _validate_key(key: str) -> None:
    if not isinstance(key, str) or _MATERIAL_KEY.fullmatch(key) is None:
        raise ValueError(f"invalid material key: {key!r}")


def _validate_relative_pattern(pattern: str) -> str:
    if not isinstance(pattern, str) or not pattern or "\\" in pattern:
        raise ValueError(f"invalid material include pattern: {pattern!r}")
    parsed = PurePosixPath(pattern)
    normalized = parsed.as_posix()
    if (
        parsed.is_absolute()
        or normalized != pattern
        or any(part in {"", ".", "..", "**"} for part in parsed.parts)
    ):
        raise ValueError(f"material include must be a bounded relative pattern: {pattern}")
    return normalized


@dataclass(frozen=True, slots=True)
class MaterialSet:
    """One named aggregate of files selected by bounded project-root globs."""

    key: str
    policy_id: str
    includes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_key(self.key)
        _validate_policy_id(self.policy_id)
        normalized = tuple(_validate_relative_pattern(item) for item in self.includes)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError(f"material set {self.key} needs unique include patterns")
        object.__setattr__(self, "includes", normalized)

    def paths(self, root: Path) -> tuple[Path, ...]:
        selected: dict[str, Path] = {}
        for pattern in self.includes:
            matches = tuple(root.glob(pattern))
            if not matches:
                raise MaterialCoverageError(
                    f"material set {self.key} include matched no files: {pattern}"
                )
            for path in matches:
                relative = _relative_path(path, root)
                if relative in selected:
                    raise MaterialCoverageError(
                        f"material set {self.key} matched a path twice: {relative}"
                    )
                selected[relative] = path
        return tuple(selected[relative] for relative in sorted(selected))

    def digest(
        self,
        root: Path,
        *,
        expected_policy_id: str,
        file_hasher: Callable[[Path], str] | None = None,
    ) -> str:
        if self.policy_id != expected_policy_id:
            raise MaterialPolicyDrift(
                f"material set {self.key} policy changed from "
                f"{expected_policy_id} to {self.policy_id}"
            )
        return sha256_file_set(
            self.paths(root), root=root, file_hasher=file_hasher
        )


@dataclass(frozen=True, slots=True)
class SnapshotSpec:
    """A deterministic declaration of individual and aggregate materials."""

    policy_id: str
    files: tuple[tuple[str, str], ...]
    material_sets: tuple[MaterialSet, ...] = ()

    def __post_init__(self) -> None:
        _validate_policy_id(self.policy_id)
        normalized_files: list[tuple[str, str]] = []
        keys: set[str] = set()
        paths: set[str] = set()
        for key, relative in self.files:
            _validate_key(key)
            normalized_relative = _validate_relative_pattern(relative)
            if any(character in normalized_relative for character in "*?["):
                raise ValueError(
                    f"individual material cannot contain a glob: {relative}"
                )
            if key in keys:
                raise ValueError(f"duplicate material key: {key}")
            if normalized_relative in paths:
                raise ValueError(f"duplicate individual material: {normalized_relative}")
            keys.add(key)
            paths.add(normalized_relative)
            normalized_files.append((key, normalized_relative))
        for material_set in self.material_sets:
            if material_set.key in keys:
                raise ValueError(f"duplicate material key: {material_set.key}")
            keys.add(material_set.key)
        object.__setattr__(self, "files", tuple(sorted(normalized_files)))
        object.__setattr__(
            self,
            "material_sets",
            tuple(sorted(self.material_sets, key=lambda item: item.key)),
        )

    @classmethod
    def from_mapping(
        cls,
        *,
        policy_id: str,
        files: Mapping[str, str],
        material_sets: Iterable[MaterialSet] = (),
    ) -> SnapshotSpec:
        return cls(
            policy_id=policy_id,
            files=tuple(files.items()),
            material_sets=tuple(material_sets),
        )

    def inventory(self, root: Path) -> dict[str, Any]:
        """Return a deterministic, unhashed description for audits and tests."""

        return {
            "policyId": self.policy_id,
            "files": [
                {"key": key, "path": relative} for key, relative in self.files
            ],
            "fileSets": [
                {
                    "key": material_set.key,
                    "policyId": material_set.policy_id,
                    "includes": list(material_set.includes),
                    "paths": [
                        _relative_path(path, root)
                        for path in material_set.paths(root)
                    ],
                }
                for material_set in self.material_sets
            ],
        }

    def covered_paths(self, root: Path) -> frozenset[str]:
        covered = {relative for _key, relative in self.files}
        for material_set in self.material_sets:
            covered.update(
                _relative_path(path, root) for path in material_set.paths(root)
            )
        return frozenset(covered)

    def require_paths(self, root: Path, required: Iterable[str]) -> None:
        normalized = {_validate_relative_pattern(path) for path in required}
        missing = sorted(normalized - self.covered_paths(root))
        if missing:
            raise MaterialCoverageError(
                "snapshot does not cover required materials: " + ", ".join(missing)
            )

    def snapshot(
        self,
        root: Path,
        *,
        expected_policy_id: str,
        file_hasher: Callable[[Path], str] | None = None,
    ) -> dict[str, str]:
        if self.policy_id != expected_policy_id:
            raise MaterialPolicyDrift(
                f"snapshot policy changed from {expected_policy_id} to {self.policy_id}"
            )
        hasher = file_hasher or sha256_file
        values = {
            key: hasher(root / relative) for key, relative in self.files
        }
        for material_set in self.material_sets:
            values[material_set.key] = material_set.digest(
                root,
                expected_policy_id=FILE_SET_SHA256_POLICY_ID,
                file_hasher=hasher,
            )
        return dict(sorted(values.items()))
