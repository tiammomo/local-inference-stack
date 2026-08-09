#!/usr/bin/env python3
"""Smoke-test documented public CLI routes without executing state changes."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from local_inference_stack.reference import command_paths  # noqa: E402


STACK_PATTERN = re.compile(
    r"(?:^|\s)\./stack\s+([a-z][a-z0-9-]*)(?:\s+([a-z][a-z0-9-]*))?"
)
NESTED_COMMANDS = {
    path.split()[0] for path in command_paths() if len(path.split()) == 2
}
SAFE_JSON_COMMANDS = (
    ("plan", "--vram-gib", "16", "--ram-gib", "64", "--json"),
    ("config", "check", "--json"),
    ("config", "render", "--json"),
    ("storage", "report", "--json"),
    ("migrate", "--check", "--json"),
    ("reference", "--check", "--json"),
)
SAFE_RESULT_CODES = {
    ("migrate", "--check", "--json"): {0, 4},
}


def markdown_files() -> list[Path]:
    files = list(ROOT_DIR.glob("*.md"))
    files.extend((ROOT_DIR / "docs").rglob("*.md"))
    files.extend((ROOT_DIR / "deployments").rglob("*.md"))
    return sorted(set(files))


def documented_routes() -> list[tuple[Path, int, str]]:
    routes: list[tuple[Path, int, str]] = []
    for path in markdown_files():
        in_shell_block = False
        for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = raw.strip()
            if stripped.startswith("```"):
                language = stripped[3:].strip().lower()
                if in_shell_block:
                    in_shell_block = False
                else:
                    in_shell_block = language in {"bash", "sh", "shell", "console"}
                continue
            if not in_shell_block or stripped.startswith("#"):
                continue
            match = STACK_PATTERN.search(stripped.removeprefix("$ "))
            if not match:
                continue
            root, child = match.groups()
            route = f"{root} {child}" if root in NESTED_COMMANDS and child else root
            routes.append((path, line_number, route))
    return routes


def run(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT_DIR / "stack"), *command],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )


def result_is_acceptable(
    command: tuple[str, ...], process_code: int, document: dict[str, object]
) -> bool:
    allowed = SAFE_RESULT_CODES.get(command, {0})
    result_code = document.get("code")
    return (
        document.get("schemaVersion") == 1
        and isinstance(result_code, int)
        and process_code == result_code
        and result_code in allowed
        and (
            result_code == 0
            or (command[0] == "migrate" and document.get("status") == "attention")
        )
    )


def main() -> int:
    known = set(command_paths())
    issues: list[str] = []
    for path, line_number, route in documented_routes():
        if route not in known:
            issues.append(f"{path.relative_to(ROOT_DIR)}:{line_number}: unknown ./stack route {route!r}")

    for route in command_paths():
        result = run((*route.split(), "--help"))
        if result.returncode != 0:
            issues.append(f"help smoke failed for './stack {route}': {result.stderr.strip()}")

    for command in SAFE_JSON_COMMANDS:
        result = run(command)
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError:
            issues.append(f"safe command did not return JSON: ./stack {' '.join(command)}")
            continue
        if not result_is_acceptable(command, result.returncode, document):
            issues.append(
                f"safe command failed: ./stack {' '.join(command)} "
                f"(process={result.returncode}, result={document.get('code')})"
            )

    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1
    print(
        f"Documentation command checks passed: {len(known)} routes, "
        f"{len(SAFE_JSON_COMMANDS)} read-only executions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
