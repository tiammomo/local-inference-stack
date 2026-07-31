"""Versioned command results and stable process exit taxonomy."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    SUCCESS = 0
    USAGE = 2
    ADMISSION = 3
    CONFIG = 4
    EXTERNAL = 5
    INTEGRITY = 6
    RECOVERY = 7


@dataclass(frozen=True)
class NextAction:
    command: str
    description: str
    requiresApproval: bool = False


@dataclass
class CommandResult:
    command: str
    status: str
    summary: str
    code: int = int(ExitCode.SUCCESS)
    facts: dict[str, Any] = field(default_factory=dict)
    nextActions: list[NextAction] = field(default_factory=list)
    schemaVersion: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


class StackError(Exception):
    def __init__(self, summary: str, code: ExitCode, *, facts: dict[str, Any] | None = None):
        super().__init__(summary)
        self.summary = summary
        self.code = int(code)
        self.facts = facts or {}


class UsageError(StackError):
    def __init__(self, summary: str, **kwargs: Any):
        super().__init__(summary, ExitCode.USAGE, **kwargs)


class AdmissionError(StackError):
    def __init__(self, summary: str, **kwargs: Any):
        super().__init__(summary, ExitCode.ADMISSION, **kwargs)


class ConfigError(StackError):
    def __init__(self, summary: str, **kwargs: Any):
        super().__init__(summary, ExitCode.CONFIG, **kwargs)


class ExternalError(StackError):
    def __init__(self, summary: str, **kwargs: Any):
        super().__init__(summary, ExitCode.EXTERNAL, **kwargs)


class IntegrityError(StackError):
    def __init__(self, summary: str, **kwargs: Any):
        super().__init__(summary, ExitCode.INTEGRITY, **kwargs)


class RecoveryError(StackError):
    def __init__(self, summary: str, **kwargs: Any):
        super().__init__(summary, ExitCode.RECOVERY, **kwargs)
