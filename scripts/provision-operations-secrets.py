#!/usr/bin/env python3
"""Copy only the credentials required by read-only operations collectors."""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path

try:
    from scripts.env_utils import (
        atomic_write_private_text,
        is_private_regular_file,
        parse_env_file,
    )
except ModuleNotFoundError:
    from env_utils import atomic_write_private_text, is_private_regular_file, parse_env_file


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = ROOT_DIR / "profiles" / "operations.secrets.env"
REQUIRED_KEYS = (
    "MODELPORT_ADMIN_USERNAME",
    "MODELPORT_ADMIN_PASSWORD",
    "MODELPORT_AUTH_TOKEN",
)


def dotenv_values(path: Path) -> dict[str, str]:
    parsed = parse_env_file(path)
    values = {key: parsed[key] for key in REQUIRED_KEYS if key in parsed}
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
    if not is_private_regular_file(args.source):
        raise SystemExit(
            "source environment file must be a current-user-owned regular file "
            f"with no group/other permissions: {args.source}"
        )
    values = dotenv_values(args.source)
    body = "\n".join(f"{key}={shlex.quote(values[key])}" for key in REQUIRED_KEYS) + "\n"
    atomic_write_private_text(args.target, body)
    print(f"provisioned operations credentials: {args.target} (mode 0600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
