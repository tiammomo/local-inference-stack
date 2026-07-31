"""Constrained subprocess execution with explicit timeouts and sanitized errors."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .result import ExternalError


SAFE_ENV_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "TERM",
    "TZ",
    "USER",
    "WSL_DISTRO_NAME",
}


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def controlled_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS}
    environment.update({"PYTHONUNBUFFERED": "1", "NO_PROXY": "*", "no_proxy": "*"})
    if extra:
        environment.update(extra)
    return environment


def run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 30,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> RunResult:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=controlled_environment(env),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ExternalError(f"required command is unavailable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExternalError(f"command timed out after {timeout}s: {argv[0]}") from exc
    result = RunResult(tuple(argv), completed.returncode, completed.stdout, completed.stderr)
    if check and completed.returncode:
        detail = completed.stderr.strip().splitlines()[-1:] or completed.stdout.strip().splitlines()[-1:]
        raise ExternalError(
            f"command failed ({completed.returncode}): {argv[0]}",
            facts={"detail": detail[0][:500] if detail else "no diagnostic output"},
        )
    return result
