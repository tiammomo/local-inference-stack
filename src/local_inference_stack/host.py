"""Small, deterministic host classification shared by planning and evidence."""

from __future__ import annotations

import platform


def environment_kind(
    *, system: str | None = None, kernel_release: str | None = None
) -> str:
    """Classify only the environments represented by the support policy.

    WSL1 is deliberately distinct from WSL2.  An unknown platform never falls
    through to a supported Linux class.
    """

    operating_system = (system or platform.system()).strip().lower()
    if operating_system != "linux":
        return "unsupported"
    release = (kernel_release or platform.release()).strip().lower()
    if "microsoft" not in release:
        return "native-linux"
    if "wsl2" in release or "microsoft-standard" in release:
        return "wsl2"
    return "wsl1"
