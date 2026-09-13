"""Data model shared by the platform probes and the UI.

Everything the preflight pipeline produces is a :class:`CheckResult`. The UI
never inspects platform-specific types, which is what keeps the Windows, macOS
and Linux back ends interchangeable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class OSFamily(str, Enum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    UNKNOWN = "unknown"


class Status(str, Enum):
    """Outcome of a single check.

    The ordering matters: :meth:`worst` uses it to roll several results up into
    one headline status.
    """

    OK = "ok"
    INFO = "info"
    SKIPPED = "skipped"
    REPAIRABLE = "repairable"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"

    @property
    def rank(self) -> int:
        return _STATUS_RANK[self]

    @property
    def needs_action(self) -> bool:
        return self in (Status.REPAIRABLE, Status.MISSING)

    @classmethod
    def worst(cls, statuses) -> "Status":
        items = list(statuses)
        if not items:
            return cls.INFO
        return max(items, key=lambda s: s.rank)


_STATUS_RANK: dict[Status, int] = {
    Status.OK: 0,
    Status.INFO: 1,
    Status.SKIPPED: 2,
    Status.REPAIRABLE: 3,
    Status.MISSING: 4,
    Status.UNSUPPORTED: 5,
    Status.FAILED: 6,
}


@dataclass(slots=True)
class Remedy:
    """An action that can move a check from a bad status towards OK."""

    label: str
    description: str
    action: Callable[["ProgressSink"], "RemedyOutcome"]
    requires_elevation: bool = False
    estimated: str = ""


@dataclass(slots=True)
class RemedyOutcome:
    ok: bool
    message: str
    #: True when the fix cannot take effect until the app (or the machine) is
    #: restarted -- a PATH edit that other processes have not picked up yet, for
    #: example.
    needs_restart: bool = False


@dataclass(slots=True)
class CheckResult:
    key: str
    title: str
    status: Status
    summary: str
    detail: str = ""
    remedy: Remedy | None = None
    #: Raw findings kept for the log pane and for later checks to reuse.
    evidence: dict[str, object] = field(default_factory=dict)

    def with_status(self, status: Status, summary: str) -> "CheckResult":
        self.status = status
        self.summary = summary
        return self


@dataclass(slots=True)
class OSInfo:
    family: OSFamily
    name: str
    version: str
    build: str = ""
    arch: str = ""
    #: Windows 11 is build 22000+. False on Windows 10 and on non-Windows.
    is_windows_11: bool = False
    supported: bool = True
    notes: str = ""

    @property
    def pretty(self) -> str:
        bits = [self.name]
        if self.version:
            bits.append(self.version)
        if self.build:
            bits.append(f"build {self.build}")
        if self.arch:
            bits.append(self.arch)
        return " ".join(bits)


class ProgressSink:
    """Callback bundle handed to long-running remedies.

    The UI passes a thread-safe implementation that marshals back to the Qt main
    thread; tests and the CLI pass the no-op default.
    """

    def log(self, line: str) -> None:  # pragma: no cover - trivial
        pass

    def step(self, message: str) -> None:  # pragma: no cover - trivial
        pass

    @property
    def cancelled(self) -> bool:  # pragma: no cover - trivial
        return False
