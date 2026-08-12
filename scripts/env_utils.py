#!/usr/bin/env python3
"""Small helpers for controlled local environment files and private evidence."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path
from typing import Any


ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
MAX_PRIVATE_ENV_BYTES = 64 * 1024


def _open_private_env(path: Path) -> tuple[int, os.stat_result]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    directory: int | None = None
    descriptor: int | None = None
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory = os.open(absolute.anchor, directory_flags)
        for component in absolute.parts[1:-1]:
            child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > MAX_PRIVATE_ENV_BYTES
        ):
            raise ValueError("private environment input is not a bounded owner-only file")
        result = descriptor, metadata
        descriptor = None
        return result
    except ValueError:
        raise
    except OSError as error:
        raise ValueError("cannot safely read private environment input") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _parse_env_text(body: str, label: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(body.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError as error:
            raise ValueError(f"invalid shell quoting in {label}:{line_number}") from error
        if not tokens:
            continue
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise ValueError(
                f"expected one KEY=VALUE assignment in {label}:{line_number}"
            )
        key, value = tokens[0].split("=", 1)
        if not ENV_KEY.fullmatch(key) or key in values:
            raise ValueError(
                f"invalid or duplicate environment key in {label}:{line_number}"
            )
        values[key] = value
    return values


def parse_env_file(path: Path) -> dict[str, str]:
    return _parse_env_text(path.read_text(encoding="utf-8"), str(path))


def read_private_env_values(
    path: Path, *, allowed_keys: set[str] | frozenset[str]
) -> dict[str, str]:
    """Read only allowlisted values from one stable private environment file.

    This is the credential boundary used by acceptance workloads.  It never
    executes shell text and it binds parsing to the same regular-file inode
    that was checked for ownership, links, permissions, and size.
    """

    descriptor: int | None = None
    try:
        descriptor, before = _open_private_env(path)
        chunks: list[bytes] = []
        remaining = MAX_PRIVATE_ENV_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(16 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_nlink,
        )
        if len(body) > MAX_PRIVATE_ENV_BYTES or identity(before) != identity(after):
            raise ValueError("private environment input changed while it was read")
        current_descriptor, current = _open_private_env(path)
        try:
            if identity(current) != identity(before):
                raise ValueError("private environment path changed while it was read")
        finally:
            os.close(current_descriptor)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("private environment input is not UTF-8") from error
        values = _parse_env_text(text, str(path))
        return {key: values[key] for key in allowed_keys if key in values}
    except OSError as error:
        raise ValueError("cannot safely read private environment input") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def load_env_defaults(path: Path, allowed_keys: set[str] | None = None) -> None:
    if not path.exists():
        return
    for key, value in parse_env_file(path).items():
        if allowed_keys is None or key in allowed_keys:
            os.environ.setdefault(key, value)


def load_private_env_defaults(
    path: Path, *, allowed_keys: set[str] | frozenset[str]
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    for key, value in read_private_env_values(path, allowed_keys=allowed_keys).items():
        os.environ.setdefault(key, value)


def is_private_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    )


def atomic_write_private_text(path: Path, body: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise RuntimeError(f"unsafe output directory: {path.parent}")
    parent = path.parent.stat()
    if parent.st_uid != os.getuid():
        raise RuntimeError(f"output directory has a different owner: {path.parent}")
    if stat.S_IMODE(parent.st_mode) & 0o077:
        path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            if not body.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_private_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
