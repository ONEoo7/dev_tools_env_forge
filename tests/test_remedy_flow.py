"""End-to-end test of the check -> fix -> re-check loop across the worker thread.

Runs the real PreflightPage and its real worker thread against a fake platform,
so the signal ordering between busy state, remedy completion and the automatic
re-check is genuinely exercised.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from devenv_forge.core.models import (  # noqa: E402
    CheckResult,
    OSFamily,
    OSInfo,
    ProgressSink,
    Remedy,
    RemedyOutcome,
    Status,
)
from devenv_forge.platforms.base import Platform  # noqa: E402
from devenv_forge.ui.preflight_page import PreflightPage  # noqa: E402
from devenv_forge.ui.theme import LIGHT  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class FakePlatform(Platform):
    """A platform whose single check starts broken and is healed by its remedy."""

    family = OSFamily.LINUX
    installer_name = "fake"

    def __init__(self) -> None:
        super().__init__()
        self.fixed = False
        self.remedy_calls = 0
        self.check_calls = 0

    def detect_os(self) -> OSInfo:
        return OSInfo(family=OSFamily.LINUX, name="Fake OS", version="1.0")

    def check_podman(self) -> CheckResult:
        self.check_calls += 1
        if self.fixed:
            return CheckResult("podman", "Podman CLI", Status.OK, "Installed")
        return CheckResult(
            key="podman",
            title="Podman CLI",
            status=Status.MISSING,
            summary="Not installed",
            remedy=Remedy(
                label="Install",
                description="Pretend to install",
                action=self._fix,
            ),
        )

    def check_backend(self) -> CheckResult:
        return CheckResult("backend", "Backend", Status.OK, "Fine")

    def _fix(self, sink: ProgressSink) -> RemedyOutcome:
        self.remedy_calls += 1
        sink.step("installing")
        sink.log("pretend install output")
        self.fixed = True
        return RemedyOutcome(True, "Installed")

    def steps(self):
        return (
            ("podman", "Podman CLI", self.check_podman),
            ("backend", "Backend", self.check_backend),
        )


def _pump(app, predicate, timeout: float = 15.0) -> bool:
    """Spin the event loop until *predicate* holds or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_fix_triggers_an_automatic_recheck(qt_app):
    """The regression guard.

    The worker used to announce the outcome while still flagged busy, so the
    page refused the follow-up run and a fixed check kept showing as broken.
    """
    platform = FakePlatform()
    page = PreflightPage(platform, LIGHT)
    try:
        page.start()
        assert _pump(qt_app, lambda: "podman" in page._results)
        assert page._results["podman"].status is Status.MISSING
        # isVisible() is false for any child of a window that was never shown,
        # so ask whether the widget was hidden deliberately.
        assert not page.cards["podman"].fix_button.isHidden()

        # Skip the confirmation dialog; exercise the worker path itself.
        page.request_remedy.emit("podman")

        assert _pump(qt_app, lambda: platform.remedy_calls == 1)
        # _results is emptied while the re-run is in flight, so tolerate the gap.
        def healed() -> bool:
            result = page._results.get("podman")
            return result is not None and result.status is Status.OK

        assert _pump(qt_app, healed), "the check did not re-run after the fix"

        assert page.cards["podman"].badge.text() == "Ready"
        assert not page._busy
        assert "pretend install output" in page.log_view.toPlainText()
    finally:
        page.shutdown()
        page.deleteLater()


def test_batch_rechecks_once_at_the_end(qt_app):
    platform = FakePlatform()
    page = PreflightPage(platform, LIGHT)
    try:
        page.start()
        assert _pump(qt_app, lambda: "podman" in page._results)

        runs_before = platform.check_calls
        page.request_fix_all.emit(["podman"])

        assert _pump(qt_app, lambda: platform.remedy_calls == 1)
        def healed() -> bool:
            result = page._results.get("podman")
            return result is not None and result.status is Status.OK

        assert _pump(qt_app, healed)
        # One extra pass over the checks, not one per fix applied.
        assert platform.check_calls == runs_before + 1
    finally:
        page.shutdown()
        page.deleteLater()


def test_overall_status_unlocks_nothing_while_broken(qt_app):
    platform = FakePlatform()
    page = PreflightPage(platform, LIGHT)
    seen: list[Status] = []
    page.preflight_finished.connect(seen.append)
    try:
        page.start()
        assert _pump(qt_app, lambda: bool(seen))
        assert seen[0] is Status.MISSING
    finally:
        page.shutdown()
        page.deleteLater()


class PlatformWithOptionalRepair(FakePlatform):
    """Everything building needs is fine; only an optional editor check is not."""

    optional_keys = frozenset({"editor"})

    def __init__(self) -> None:
        super().__init__()
        self.fixed = True  # the required podman check passes

    def check_editor(self) -> CheckResult:
        return CheckResult(
            key="editor",
            title="Editor integration",
            status=Status.REPAIRABLE,
            summary="Not configured",
            remedy=Remedy(
                label="Configure",
                description="Pretend to configure",
                action=lambda sink: RemedyOutcome(True, "Configured"),
            ),
        )

    def steps(self):
        return (
            ("podman", "Podman CLI", self.check_podman),
            ("backend", "Backend", self.check_backend),
            ("editor", "Editor integration", self.check_editor),
        )


def test_optional_check_does_not_lock_the_later_stages(qt_app):
    """The regression: an unconfigured VS Code locked Build and Deploy.

    Readiness to build must come only from the checks building depends on.
    """
    page = PreflightPage(PlatformWithOptionalRepair(), LIGHT)
    seen: list[Status] = []
    page.preflight_finished.connect(seen.append)
    try:
        page.start()
        assert _pump(qt_app, lambda: bool(seen))
        assert seen[0] is Status.OK
        assert "Optional: Editor integration" in page.banner.text()
        assert "Outstanding" not in page.banner.text()
        # Optional does not mean ignored: its fix is still offered, and counted.
        assert not page.cards["editor"].fix_button.isHidden()
        assert page.fix_all_button.text() == "Fix all (1)"
    finally:
        page.shutdown()
        page.deleteLater()
