"""macOS implementation.

Structurally identical to Windows with two substitutions: Homebrew replaces
winget, and the Linux backend is a podman machine on Apple's own hypervisor
rather than WSL2. Untested on this Windows workstation, so the remedies are
written to fail loudly rather than guess.
"""

from __future__ import annotations

import os
import platform as _platform
from collections.abc import Sequence
from pathlib import Path

from ..core.models import (
    CheckResult,
    OSFamily,
    OSInfo,
    ProgressSink,
    Remedy,
    RemedyOutcome,
    Status,
)
from ..core.runner import run, stream, which
from .base import MachinePlatform, probe_podman

#: Apple silicon Homebrew lives under /opt, Intel under /usr/local.
BREW_PREFIXES = ("/opt/homebrew", "/usr/local")


class MacOSPlatform(MachinePlatform):
    family = OSFamily.MACOS
    installer_name = "Homebrew"

    @property
    def backend_title(self) -> str:
        return "Podman machine backend"

    def detect_os(self) -> OSInfo:
        version = _platform.mac_ver()[0] or _platform.release()
        arch = _platform.machine()
        major = 0
        try:
            major = int(version.split(".")[0])
        except (ValueError, IndexError):
            pass
        supported = major == 0 or major >= 13
        notes = "" if supported else f"macOS {version} is older than the supported 13."
        return OSInfo(
            family=OSFamily.MACOS,
            name="macOS",
            version=version,
            arch=arch,
            supported=supported,
            notes=notes,
        )

    def candidate_dirs(self) -> Sequence[str]:
        dirs = [f"{prefix}/bin" for prefix in BREW_PREFIXES]
        dirs.append(str(Path.home() / ".local" / "bin"))
        dirs.append("/usr/bin")
        return dirs

    def check_podman(self) -> CheckResult:
        evidence: dict[str, object] = {}
        on_path = self.find_podman_on_path()
        evidence["path_lookup"] = on_path or "not found on PATH"
        if on_path:
            probe = probe_podman(on_path)
            if probe.works:
                return CheckResult(
                    key="podman",
                    title="Podman CLI",
                    status=Status.OK,
                    summary=f"podman {probe.version} at {on_path}",
                    evidence=evidence,
                )
            evidence["probe_error"] = probe.error

        elsewhere = self.find_podman_in_known_dirs()
        evidence["executables_found"] = elsewhere
        working = [p for p in (probe_podman(exe) for exe in elsewhere) if p.works]
        if working:
            directory = os.path.dirname(working[0].path)
            return CheckResult(
                key="podman",
                title="Podman CLI",
                status=Status.REPAIRABLE,
                summary="Installed but not on PATH",
                detail=(
                    f"Add this to your shell profile:\n\n"
                    f'  export PATH="{directory}:$PATH"'
                ),
                evidence=evidence,
            )

        return CheckResult(
            key="podman",
            title="Podman CLI",
            status=Status.MISSING,
            summary="Not installed",
            detail="Install with Homebrew: brew install podman",
            remedy=self._brew_install_remedy(),
            evidence=evidence,
        )

    def _brew_install_remedy(self) -> Remedy:
        def action(sink: ProgressSink) -> RemedyOutcome:
            brew = which("brew") or next(
                (
                    f"{prefix}/bin/brew"
                    for prefix in BREW_PREFIXES
                    if os.path.isfile(f"{prefix}/bin/brew")
                ),
                "",
            )
            if not brew:
                return RemedyOutcome(
                    False,
                    "Homebrew is not installed. Install it from https://brew.sh "
                    "and re-run this check.",
                )
            sink.step("Running brew install podman")
            code = "1"
            for kind, text in stream([brew, "install", "podman"], timeout=1800):
                if kind == "line":
                    sink.log(text)
                else:
                    code = text
            if code != "0":
                return RemedyOutcome(False, f"brew exited with code {code}.")
            self.drop_cached_podman()
            return RemedyOutcome(True, "podman installed via Homebrew.")

        return Remedy(
            label="Install with Homebrew",
            description="Run brew install podman.",
            action=action,
            estimated="2-5 minutes",
        )

    def check_backend(self) -> CheckResult:
        """On macOS the backend is the podman machine VM itself."""
        exe = which("podman")
        if not exe:
            return CheckResult(
                key="backend",
                title="Podman machine backend",
                status=Status.SKIPPED,
                summary="Waiting on the podman CLI",
            )
        result = run([exe, "machine", "info", "--format", "json"], timeout=60)
        if not result.ok:
            return CheckResult(
                key="backend",
                title="Podman machine backend",
                status=Status.INFO,
                summary="Could not query machine info",
                detail=result.output,
            )
        return CheckResult(
            key="backend",
            title="Podman machine backend",
            status=Status.OK,
            summary="Native virtualisation available",
            detail=(
                "macOS uses Apple's hypervisor for the podman machine; there is "
                "no WSL2 equivalent to install."
            ),
            evidence={"raw": result.output[:2000]},
        )
