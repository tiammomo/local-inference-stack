#!/usr/bin/env python3
"""Render relocatable systemd user units for this checkout."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT_DIR / "deploy" / "systemd"
TARGET_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_NAMES = (
    "qwen-model-runtime.service",
    "qwen-model-operations-dashboard.service",
    "qwen-model-operations-report.service",
    "qwen-model-operations-report.timer",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable", action="store_true", help="enable and start the installed units")
    parser.add_argument("--operations", action="store_true", help="also install dashboard/report units")
    return parser.parse_args()


def render(name: str) -> None:
    template = TEMPLATE_DIR / f"{name}.in"
    target = TARGET_DIR / name
    body = template.read_text(encoding="utf-8").replace("@PROJECT_ROOT@", str(ROOT_DIR))
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{name}.", dir=TARGET_DIR, text=True)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"installed {target}")


def main() -> int:
    args = parse_args()
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    selected = UNIT_NAMES if args.operations else UNIT_NAMES[:1]
    for name in selected:
        render(name)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if args.enable:
        units = ["qwen-model-runtime.service"]
        if args.operations:
            units.extend(
                ["qwen-model-operations-dashboard.service", "qwen-model-operations-report.timer"]
            )
        subprocess.run(["systemctl", "--user", "enable", "--now", *units], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
