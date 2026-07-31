"""Repository-local paths; no home-directory or adjacent-checkout assumptions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @classmethod
    def discover(cls, start: Path | None = None) -> "ProjectPaths":
        candidate = (start or Path(__file__)).resolve()
        if candidate.is_file():
            candidate = candidate.parent
        for directory in (candidate, *candidate.parents):
            if (directory / "AGENTS.md").is_file() and (directory / "catalog/models.json").is_file():
                return cls(directory)
        raise RuntimeError("cannot discover PROJECT_ROOT")

    @property
    def state_dir(self) -> Path:
        return self.root / "cache" / "control-plane"

    @property
    def transaction_path(self) -> Path:
        return self.state_dir / "transaction.json"

    @property
    def config_path(self) -> Path:
        return self.root / "config" / "runtime-profiles.json"

    @property
    def operations_dir(self) -> Path:
        return self.root / "logs" / "operations"
