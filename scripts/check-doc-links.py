#!/usr/bin/env python3
"""Fail when a tracked Markdown document links to a missing local file."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote


ROOT_DIR = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)]+)\)")


def markdown_files() -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT_DIR,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [ROOT_DIR / line for line in output.splitlines() if line]


def main() -> int:
    failures: list[str] = []
    for document in markdown_files():
        for line_number, line in enumerate(
            document.read_text(encoding="utf-8").splitlines(), 1
        ):
            for match in LINK.finditer(line):
                raw = match.group("target").strip().strip("<>")
                target = raw.split(maxsplit=1)[0].split("#", 1)[0]
                if not target or target.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (document.parent / unquote(target)).resolve()
                if ROOT_DIR not in resolved.parents and resolved != ROOT_DIR:
                    failures.append(
                        f"{document.relative_to(ROOT_DIR)}:{line_number}: "
                        f"link escapes repository: {raw}"
                    )
                elif not resolved.exists():
                    failures.append(
                        f"{document.relative_to(ROOT_DIR)}:{line_number}: "
                        f"missing link target: {raw}"
                    )
    if failures:
        print("\n".join(failures))
        return 1
    print("Tracked Markdown local links passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
