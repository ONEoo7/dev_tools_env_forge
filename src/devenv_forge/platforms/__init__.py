"""OS detection and the platform back ends."""

from __future__ import annotations

import sys

from ..core.models import OSFamily
from .base import Platform


def current_family() -> OSFamily:
    if sys.platform == "win32":
        return OSFamily.WINDOWS
    if sys.platform == "darwin":
        return OSFamily.MACOS
    if sys.platform.startswith("linux"):
        return OSFamily.LINUX
    return OSFamily.UNKNOWN


def create_platform(family: OSFamily | None = None) -> Platform:
    """Build the back end for this machine.

    Imports are deferred so a Windows-only module never loads on macOS, and so
    the unsupported case can raise a useful message instead of an ImportError.
    """
    family = family or current_family()
    if family is OSFamily.WINDOWS:
        from .windows import WindowsPlatform

        return WindowsPlatform()
    if family is OSFamily.MACOS:
        from .macos import MacOSPlatform

        return MacOSPlatform()
    if family is OSFamily.LINUX:
        from .linux import LinuxPlatform

        return LinuxPlatform()
    raise RuntimeError(f"Unsupported operating system: {sys.platform}")


__all__ = ["Platform", "create_platform", "current_family"]
