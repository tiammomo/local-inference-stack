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


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError as error:
            raise ValueError(f"invalid shell quoting in {path}:{line_number}") from error
        if not tokens:
            continue
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise ValueError(f"expected one KEY=VALUE assignment in {path}:{line_number}")
        key, value = tokens[0].split("=", 1)
        if not ENV_KEY.fullmatch(key) or key in values:
            raise ValueError(f"invalid or duplicate environment key in {path}:{line_number}")
        values[key] = value
    return values


def load_env_defaults(path: Path, allowed_keys: set[str] | None = None) -> None:
    if not path.exists():
        return
    for key, value in parse_env_file(path).items():
        if allowed_keys is None or key in allowed_keys:
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
