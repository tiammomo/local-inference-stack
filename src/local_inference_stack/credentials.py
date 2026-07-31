"""Credential-file metadata auditing without reading or exposing secret values."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from .paths import ProjectPaths
from .result import ConfigError
from .runner import run


SECRET_FILES = (
    "profiles/operations.secrets.env",
    "profiles/alerting.local.env",
    "profiles/backup.local.env",
)

CREDENTIAL_KINDS = {
    "operations": ("profiles/operations.secrets.env", "operations.env.cred", "operations.env"),
    "backup": ("profiles/backup.local.env", "backup.env.cred", "backup.env"),
    "alerting": ("profiles/alerting.local.env", "alerting.env.cred", "alerting.env"),
}


def audit(paths: ProjectPaths) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    healthy = True
    for relative in SECRET_FILES:
        path = paths.root / relative
        if not path.exists():
            files.append({"path": relative, "present": False, "status": "optional-missing"})
            continue
        metadata = path.stat()
        mode = stat.S_IMODE(metadata.st_mode)
        secure = mode & 0o077 == 0 and not path.is_symlink()
        healthy = healthy and secure
        files.append(
            {
                "path": relative,
                "present": True,
                "mode": f"{mode:04o}",
                "ownerUid": metadata.st_uid,
                "secure": secure,
                "status": "private" if secure else "permissions-too-broad",
            }
        )
    backend = "systemd-creds-available" if any(
        (Path(directory) / "systemd-creds").exists()
        for directory in os.environ.get("PATH", "").split(os.pathsep)
        if directory
    ) else "env-compatibility-only"
    encrypted = []
    for kind, (_, filename, credential_name) in CREDENTIAL_KINDS.items():
        path = paths.root / "profiles" / "credentials" / filename
        encrypted.append(
            {
                "kind": kind,
                "path": str(path.relative_to(paths.root)),
                "present": path.is_file() and not path.is_symlink(),
                "credentialName": credential_name,
            }
        )
    return {
        "healthy": healthy,
        "backend": backend,
        "files": files,
        "encryptedCredentials": encrypted,
        "valuesRead": False,
    }


def migrate_to_systemd(paths: ProjectPaths, kind: str) -> dict[str, Any]:
    if kind not in CREDENTIAL_KINDS:
        raise ConfigError(f"unsupported credential kind: {kind}")
    source_name, encrypted_name, credential_name = CREDENTIAL_KINDS[kind]
    source = paths.root / source_name
    if not source.is_file() or source.is_symlink():
        raise ConfigError(f"credential source is missing or unsafe: {source_name}")
    metadata = source.stat()
    if stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_uid != os.getuid():
        raise ConfigError(f"credential source must be current-user-owned mode 0600: {source_name}")
    directory = paths.root / "profiles" / "credentials"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    destination = directory / encrypted_name
    if destination.exists():
        raise ConfigError(f"encrypted credential already exists: {destination.relative_to(paths.root)}")
    result = run(
        [
            "systemd-creds",
            "encrypt",
            f"--name={credential_name}",
            str(source),
            str(destination),
        ],
        cwd=paths.root,
        timeout=60,
    )
    destination.chmod(0o600)
    return {
        "kind": kind,
        "sourceRetained": True,
        "encryptedPath": str(destination.relative_to(paths.root)),
        "credentialName": credential_name,
        "next": "rerun scripts/install-user-services.py to render LoadCredentialEncrypted",
        "toolOutputSuppressed": True,
    }
