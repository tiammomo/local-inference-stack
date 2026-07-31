#!/usr/bin/env python3
"""Render relocatable systemd user units for this checkout."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from scripts.env_utils import is_private_regular_file
except ModuleNotFoundError:
    from env_utils import is_private_regular_file


ROOT_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT_DIR / "deploy" / "systemd"
TARGET_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_NAMES = (
    "qwen-model-runtime.service",
    "qwen-model-operations-dashboard.service",
    "qwen-model-operations-report.service",
    "qwen-model-operations-report.timer",
    "qwen-model-backup.service",
    "qwen-model-backup.timer",
    "qwen-model-restore-drill.service",
    "qwen-model-restore-drill.timer",
    "qwen-model-production-alert@.service",
)
RUNTIME_UNITS = ("qwen-model-runtime.service",)
OPERATIONS_UNITS = (
    "qwen-model-operations-report.timer",
    "qwen-model-backup.timer",
    "qwen-model-restore-drill.timer",
    "qwen-model-operations-dashboard.service",
    "qwen-model-operations-report.service",
    "qwen-model-backup.service",
    "qwen-model-restore-drill.service",
)
OPERATIONS_ENABLE_UNITS = (
    "qwen-model-operations-dashboard.service",
    "qwen-model-operations-report.timer",
    "qwen-model-backup.timer",
    "qwen-model-restore-drill.timer",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable", action="store_true", help="enable and start the installed units")
    parser.add_argument("--operations", action="store_true", help="also install dashboard/report units")
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="disable existing operations units and converge to runtime-only service",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="render and verify every unit in a temporary directory without installing",
    )
    args = parser.parse_args()
    if args.check and (args.enable or args.operations):
        parser.error("--check cannot be combined with --enable or --operations")
    if args.runtime_only and (not args.enable or args.operations or args.check):
        parser.error("--runtime-only requires --enable and cannot be combined with --operations or --check")
    return args


def systemd_escape_path(path: Path) -> str:
    """Escape a filesystem path for unquoted systemd unit fields and ExecStart."""
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/._-:@"
    escaped = []
    for value in os.fsencode(path):
        if value == ord("%"):
            escaped.append("%%")
        elif value == ord("$"):
            escaped.append("$$")
        elif value in safe:
            escaped.append(chr(value))
        else:
            escaped.append(f"\\x{value:02x}")
    return "".join(escaped)


def pinned_python(root_dir: Path = ROOT_DIR) -> Path:
    version_file = root_dir / ".python-version"
    if not version_file.is_file():
        return Path(sys.executable).resolve()
    version = version_file.read_text(encoding="utf-8").strip()
    if not version or any(character.isspace() for character in version):
        raise RuntimeError(f"invalid Python pin: {version_file}")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError(f"uv is required by {version_file}")
    result = subprocess.run(
        [uv, "python", "find", version],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    executable = Path(result.stdout.strip()).resolve()
    if not executable.is_file():
        raise RuntimeError(f"uv returned a missing Python executable: {executable}")
    detected = subprocess.run(
        [
            str(executable),
            "-c",
            "import platform; print(platform.python_version())",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if detected != version:
        raise RuntimeError(
            f"uv resolved Python {detected}, but {version_file} pins {version}"
        )
    return executable


def rendered_body(
    name: str,
    root_dir: Path = ROOT_DIR,
    python_executable: Path | None = None,
) -> str:
    template = TEMPLATE_DIR / f"{name}.in"
    escaped_root = systemd_escape_path(root_dir)
    resolved_python = (python_executable or pinned_python(root_dir)).resolve()

    def credential_directive(
        credential_name: str,
        encrypted_name: str,
        fallback_name: str,
        *,
        optional: bool = False,
    ) -> str:
        encrypted = root_dir / "profiles" / "credentials" / encrypted_name
        if encrypted.is_file() and not encrypted.is_symlink():
            return (
                f"LoadCredentialEncrypted={credential_name}:"
                f"{systemd_escape_path(encrypted)}"
            )
        prefix = "-" if optional else ""
        return f"EnvironmentFile={prefix}{escaped_root}/profiles/{fallback_name}"

    replacements = {
        "@PROJECT_ROOT@": escaped_root,
        "@PYTHON_EXECUTABLE@": systemd_escape_path(resolved_python),
        "@PYTHON_BIN_DIR@": systemd_escape_path(resolved_python.parent),
        "@OPERATIONS_CREDENTIAL_DIRECTIVE@": credential_directive(
            "operations.env", "operations.env.cred", "operations.secrets.env"
        ),
        "@BACKUP_CREDENTIAL_DIRECTIVE@": credential_directive(
            "backup.env", "backup.env.cred", "backup.local.env", optional=True
        ),
        "@ALERTING_CREDENTIAL_DIRECTIVE@": credential_directive(
            "alerting.env", "alerting.env.cred", "alerting.local.env", optional=True
        ),
    }
    body = template.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        body = body.replace(marker, value)
    return body


def render(name: str) -> None:
    target = TARGET_DIR / name
    body = rendered_body(name)
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


def verify_units() -> None:
    with tempfile.TemporaryDirectory(prefix="local-inference-systemd-") as directory:
        temporary_dir = Path(directory)
        paths = []
        for name in UNIT_NAMES:
            path = temporary_dir / name
            path.write_text(rendered_body(name), encoding="utf-8")
            paths.append(path)
        environment = dict(os.environ)
        # A trailing colon appends systemd's compiled-in user unit search path.
        environment["SYSTEMD_UNIT_PATH"] = f"{temporary_dir}:"
        subprocess.run(
            ["systemd-analyze", "--user", "verify", *(str(path) for path in paths)],
            check=True,
            env=environment,
        )
    print("systemd user unit templates passed.")


def known_user_units(names: tuple[str, ...]) -> list[str]:
    known = []
    for name in names:
        result = subprocess.run(
            ["systemctl", "--user", "show", name, "--property=LoadState", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.stdout.strip() not in {"", "not-found"}:
            known.append(name)
    return known


def disable_operations_units() -> None:
    known = known_user_units(OPERATIONS_UNITS)
    if not known:
        return
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", *known],
        check=True,
        timeout=60,
    )
    for name in known:
        # A disabled oneshot or timer may be unloaded immediately. reset-failed is
        # therefore best-effort per unit; disable --now above remains strict.
        subprocess.run(
            ["systemctl", "--user", "reset-failed", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )


def main() -> int:
    args = parse_args()
    if args.check:
        verify_units()
        return 0
    if args.enable:
        optional_profiles = (
            ROOT_DIR / "profiles" / "deployment.local.env",
            ROOT_DIR / "profiles" / "alerting.local.env",
        )
        unsafe_optional = [
            str(path)
            for path in optional_profiles
            if path.exists() and not is_private_regular_file(path)
        ]
        if unsafe_optional:
            raise SystemExit(
                "cannot enable services; these optional local profiles are unsafe:\n"
                + "\n".join(f"- {path}" for path in unsafe_optional)
            )
    if args.operations and args.enable:
        required_profiles = (
            (
                ROOT_DIR / "profiles" / "operations.secrets.env",
                ROOT_DIR / "profiles" / "credentials" / "operations.env.cred",
            ),
            (
                ROOT_DIR / "profiles" / "backup.local.env",
                ROOT_DIR / "profiles" / "credentials" / "backup.env.cred",
            ),
        )
        unsafe = [
            " or ".join(str(path) for path in alternatives)
            for alternatives in required_profiles
            if not any(is_private_regular_file(path) for path in alternatives)
        ]
        if unsafe:
            raise SystemExit(
                "cannot enable operations units; these files must be current-user-owned "
                "regular files with mode 0600:\n"
                + "\n".join(f"- {path}" for path in unsafe)
            )
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT_DIR / "backups").mkdir(mode=0o700, parents=True, exist_ok=True)
    (ROOT_DIR / "logs" / "alerts").mkdir(mode=0o700, parents=True, exist_ok=True)
    selected = UNIT_NAMES if args.operations else (UNIT_NAMES[0], UNIT_NAMES[-1])
    for name in selected:
        render(name)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if args.runtime_only:
        disable_operations_units()
    if args.enable:
        units = list(RUNTIME_UNITS)
        if args.operations:
            units.extend(OPERATIONS_ENABLE_UNITS)
        subprocess.run(["systemctl", "--user", "enable", *units], check=True)
        subprocess.run(
            ["systemctl", "--user", "reset-failed", *units],
            check=False,
        )
        subprocess.run(
            ["systemctl", "--user", "restart", "qwen-model-runtime.service"],
            check=True,
            timeout=60,
        )
        if args.operations:
            subprocess.run(
                ["systemctl", "--user", "start", *OPERATIONS_ENABLE_UNITS],
                check=True,
                timeout=60,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
