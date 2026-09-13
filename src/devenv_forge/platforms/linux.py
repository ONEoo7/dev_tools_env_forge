"""Linux implementation.

The simplest of the three: containers run natively, so there is no machine and
no virtualisation layer to install. The interesting part is picking the right
package manager and checking that rootless podman is actually usable.
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
from .base import Platform, probe_podman

#: Package manager, install argv template, human label.
PACKAGE_MANAGERS = (
    ("apt-get", ["apt-get", "install", "-y", "podman"], "APT"),
    ("dnf", ["dnf", "install", "-y", "podman"], "DNF"),
    ("zypper", ["zypper", "--non-interactive", "install", "podman"], "Zypper"),
    ("pacman", ["pacman", "-S", "--noconfirm", "podman"], "Pacman"),
    ("apk", ["apk", "add", "podman"], "APK"),
)


class LinuxPlatform(Platform):
    family = OSFamily.LINUX

    def __init__(self) -> None:
        super().__init__()
        self._manager = self._detect_package_manager()
        self.installer_name = self._manager[2] if self._manager else ""

    @property
    def backend_title(self) -> str:
        return "Native container support"

    @staticmethod
    def _detect_package_manager():
        for binary, argv, label in PACKAGE_MANAGERS:
            if which(binary):
                return (binary, argv, label)
        return None

    def detect_os(self) -> OSInfo:
        name, version = _read_os_release()
        return OSInfo(
            family=OSFamily.LINUX,
            name=name or "Linux",
            version=version or _platform.release(),
            arch=_platform.machine(),
            supported=True,
            notes=(
                f"Package manager: {self.installer_name}"
                if self.installer_name
                else "No supported package manager detected."
            ),
        )

    def candidate_dirs(self) -> Sequence[str]:
        return (
            "/usr/bin",
            "/usr/local/bin",
            "/bin",
            str(Path.home() / ".local" / "bin"),
        )

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
                detail=f'Add to your shell profile:\n\n  export PATH="{directory}:$PATH"',
                evidence=evidence,
            )

        return CheckResult(
            key="podman",
            title="Podman CLI",
            status=Status.MISSING,
            summary="Not installed",
            detail=(
                f"Install with {self.installer_name}."
                if self.installer_name
                else "No supported package manager was found."
            ),
            remedy=self._install_remedy() if self._manager else None,
            evidence=evidence,
        )

    def _install_remedy(self) -> Remedy:
        assert self._manager is not None
        _, argv, label = self._manager

        def action(sink: ProgressSink) -> RemedyOutcome:
            command = argv if os.geteuid() == 0 else ["sudo", "-n", *argv]
            sink.step(f"Installing podman with {label}")
            code = "1"
            for kind, text in stream(command, timeout=1800):
                if kind == "line":
                    sink.log(text)
                else:
                    code = text
            if code != "0":
                return RemedyOutcome(
                    False,
                    f"{label} exited with code {code}. Run this in a terminal "
                    f"where sudo can prompt: sudo {' '.join(argv)}",
                )
            return RemedyOutcome(True, f"podman installed with {label}.")

        return Remedy(
            label=f"Install with {label}",
            description=f"Run {' '.join(argv)} as root.",
            action=action,
            requires_elevation=True,
            estimated="1-3 minutes",
        )

    def check_backend(self) -> CheckResult:
        """Verify rootless containers work, which is the real prerequisite."""
        detail: list[str] = ["Containers run natively; no virtual machine needed."]
        status = Status.OK
        summary = "Native container support"

        subuid = Path("/etc/subuid")
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if subuid.is_file() and user:
            try:
                mapped = any(
                    line.split(":")[0] == user
                    for line in subuid.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            except OSError:
                mapped = True
            detail.append("")
            if mapped:
                detail.append(f"Rootless UID range is configured for {user}.")
            else:
                status = Status.REPAIRABLE
                summary = "Rootless UID mapping is missing"
                detail.append(
                    f"{user} has no entry in /etc/subuid, so rootless containers "
                    f"will fail. Fix with:\n\n"
                    f"  sudo usermod --add-subuids 100000-165535 "
                    f"--add-subgids 100000-165535 {user}"
                )

        cgroup = Path("/sys/fs/cgroup/cgroup.controllers")
        detail.append("")
        detail.append(
            "cgroup v2 detected." if cgroup.exists() else "cgroup v2 not detected."
        )

        return CheckResult(
            key="backend",
            title="Native container support",
            status=status,
            summary=summary,
            detail="\n".join(detail),
        )

    def check_provider(self) -> CheckResult:
        return CheckResult(
            key="provider",
            title="Machine provider",
            status=Status.SKIPPED,
            summary="Not used on Linux; containers run directly on the host",
        )

    def check_machine(self) -> CheckResult:
        return CheckResult(
            key="machine",
            title="Podman machine",
            status=Status.SKIPPED,
            summary="Not needed on Linux",
        )


def _read_os_release() -> tuple[str, str]:
    """Read the distribution name and version from /etc/os-release."""
    path = Path("/etc/os-release")
    if not path.is_file():
        return "", ""
    fields: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip().strip('"')
    except OSError:
        return "", ""
    return fields.get("NAME", ""), fields.get("VERSION_ID", "")
