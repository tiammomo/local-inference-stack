#!/usr/bin/env python3
"""Copy only the credentials required by read-only operations collectors."""

from __future__ import annotations

import argparse
import os
import shlex
import tempfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = ROOT_DIR / "profiles" / "operations.secrets.env"
REQUIRED_KEYS = (
    "MODELPORT_ADMIN_USERNAME",
    "MODELPORT_ADMIN_PASSWORD",
    "MODELPORT_AUTH_TOKEN",
)


def dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in REQUIRED_KEYS:
            continue
        if key in values:
            raise ValueError(f"duplicate {key} at line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise ValueError(f"missing required values: {', '.join(missing)}")
    return values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="materialize a least-privilege operations environment file"
    )
    parser.add_argument("--source", type=Path, required=True, help="path to the ModelPort .env file")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.source.is_file():
        raise SystemExit(f"source environment file not found: {args.source}")
    values = dotenv_values(args.source)
    args.target.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{key}={shlex.quote(values[key])}" for key in REQUIRED_KEYS) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{args.target.name}.", dir=args.target.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(args.target)
        args.target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"provisioned operations credentials: {args.target} (mode 0600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
