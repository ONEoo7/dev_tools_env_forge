"""The preflight page: run the environment checks and repair what is broken."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.models import CheckResult, RemedyOutcome, Status
from ..core.paths import log_dir
from ..platforms.base import Platform
from .cards import CheckCard, SectionHeader
from .theme import Palette, rgba, status_color
from .worker import PreflightWorker

SUMMARY_TEXT = {
    Status.OK: "Environment is ready.",
    Status.INFO: "Environment is usable. See the notes below.",
    Status.SKIPPED: "Some checks were skipped.",
    Status.REPAIRABLE: "Something needs repairing before you can build images.",
    Status.MISSING: "Required software is missing.",
    Status.UNSUPPORTED: "This system is not supported.",
    Status.FAILED: "A check could not complete.",
}


class PreflightPage(QWidget):
    """Owns the worker thread that runs checks and remedies."""

    #: Emitted with the overall status once a full run completes.
    preflight_finished = pyqtSignal(object)
    status_message = pyqtSignal(str)

    request_run = pyqtSignal()
    request_remedy = pyqtSignal(str)
    request_fix_all = pyqtSignal(list)

    def __init__(self, platform: Platform, palette: Palette) -> None:
        super().__init__()
        self.platform = platform
        self.palette_ = palette
        self.cards: dict[str, CheckCard] = {}
        self._results: dict[str, CheckResult] = {}
        self._busy = False

        self._build_ui()
        self._start_worker()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(16)

        self.header = SectionHeader(
            "Environment preflight",
            "Checking that this machine can build and run Podman images.",
        )
        self.rerun_button = QPushButton("Re-run checks")
        self.rerun_button.clicked.connect(self._on_rerun)
        self.fix_all_button = QPushButton("Fix all")
        self.fix_all_button.setObjectName("Primary")
        self.fix_all_button.setEnabled(False)
        self.fix_all_button.clicked.connect(self._on_fix_all)
        self.header.actions.addWidget(self.rerun_button)
        self.header.actions.addWidget(self.fix_all_button)
        root.addWidget(self.header)

        self.banner = QLabel("Starting checks...")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(
            f"background: {self.palette_.surface_alt}; "
            f"border: 1px solid {self.palette_.border}; "
            "border-radius: 8px; padding: 12px 14px;"
        )
        root.addWidget(self.banner)

        self.progress = QProgressBar()
        self.progress.setRange(0, len(self.platform.steps()))
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        # Only meaningful while work is happening; an idle full bar reads as a
        # result rather than as progress.
        self.progress.hide()
        root.addWidget(self.progress)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.card_layout = QVBoxLayout(container)
        self.card_layout.setContentsMargins(0, 0, 8, 0)
        self.card_layout.setSpacing(10)

        for key, title, _ in self.platform.steps():
            card = CheckCard(key, title, self.palette_)
            card.remedy_requested.connect(self._on_remedy_requested)
            self.cards[key] = card
            self.card_layout.addWidget(card)
        self.card_layout.addStretch(1)

        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        root.addWidget(self._build_log_pane())

    def _build_log_pane(self) -> QWidget:
        wrapper = QFrame()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        row = QHBoxLayout()
        self.log_toggle = QPushButton("Show activity log")
        self.log_toggle.setObjectName("Link")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_toggle.toggled.connect(self._on_log_toggle)
        row.addWidget(self.log_toggle)
        row.addStretch(1)

        self.open_logs_button = QPushButton("Open log folder")
        self.open_logs_button.setObjectName("Link")
        self.open_logs_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_logs_button.clicked.connect(self._open_log_folder)
        row.addWidget(self.open_logs_button)
        layout.addLayout(row)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("Log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        self.log_view.setFixedHeight(150)
        self.log_view.hide()
        layout.addWidget(self.log_view)
        return wrapper

    def _start_worker(self) -> None:
        self.thread = QThread(self)
        self.worker = PreflightWorker(self.platform)
        self.worker.moveToThread(self.thread)

        self.worker.check_started.connect(self._on_check_started)
        self.worker.check_finished.connect(self._on_check_finished)
        self.worker.run_finished.connect(self._on_run_finished)
        self.worker.remedy_started.connect(self._on_remedy_started)
        self.worker.remedy_finished.connect(self._on_remedy_finished)
        self.worker.batch_finished.connect(self._on_batch_finished)
        self.worker.log.connect(self.append_log)
        self.worker.step.connect(self._on_step)
        self.worker.busy_changed.connect(self._set_busy)

        self.request_run.connect(self.worker.run_checks)
        self.request_remedy.connect(self.worker.apply_remedy)
        self.request_fix_all.connect(self.worker.apply_all)

        self.thread.start()

    def start(self) -> None:
        """Kick off the first run once the window is on screen."""
        self._on_rerun()

    def shutdown(self) -> None:
        """Stop the worker thread cleanly."""
        if self.thread.isRunning():
            self.worker.cancel()
            self.thread.quit()
            # A remedy may be mid-install; give it a moment before giving up.
            if not self.thread.wait(4000):
                self.thread.terminate()
                self.thread.wait(1000)

    # -- worker callbacks --------------------------------------------------

    def _on_rerun(self) -> None:
        if self._busy:
            return
        self._results.clear()
        self.progress.setRange(0, len(self.platform.steps()))
        self.progress.setValue(0)
        self.progress.show()
        for card in self.cards.values():
            card.set_running()
        self.banner.setText("Running checks...")
        self.request_run.emit()

    def _on_check_started(self, key: str, _title: str) -> None:
        card = self.cards.get(key)
        if card is not None:
            card.set_running()
        self.status_message.emit(f"Checking {_title}...")

    def _on_check_finished(self, result: CheckResult) -> None:
        self._results[result.key] = result
        card = self.cards.get(result.key)
        if card is not None:
            card.set_result(result)
        self.progress.setValue(self.progress.value() + 1)

    def _on_run_finished(self, results: list) -> None:
        self.progress.hide()
        optional = getattr(self.platform, "optional_keys", frozenset())
        required = [r for r in results if r.key not in optional]
        # Only the checks building actually depends on decide readiness. An
        # optional one needing attention is shown, but must not lock stages.
        overall = Status.worst(r.status for r in required) if required else Status.INFO
        self._paint_banner(overall, results, optional)

        actionable = [r.key for r in results if r.remedy is not None]
        self.fix_all_button.setEnabled(bool(actionable) and not self._busy)
        self.fix_all_button.setText(
            f"Fix all ({len(actionable)})" if actionable else "Fix all"
        )
        self.preflight_finished.emit(overall)
        self.status_message.emit(SUMMARY_TEXT.get(overall, "Checks complete."))

    def _paint_banner(self, overall: Status, results: list, optional=frozenset()) -> None:
        colour = status_color(overall, self.palette_)
        headline = SUMMARY_TEXT.get(overall, "Checks complete.")
        blocking = [r for r in results if r.status.needs_action and r.key not in optional]
        advisory = [r for r in results if r.status.needs_action and r.key in optional]
        if blocking:
            names = ", ".join(r.title for r in blocking)
            headline = f"{headline}  Outstanding: {names}."
        if advisory:
            names = ", ".join(r.title for r in advisory)
            headline = f"{headline}  Optional: {names}."
        self.banner.setText(headline)
        self.banner.setStyleSheet(
            f"background: {rgba(colour, 0.12)}; "
            f"border: 1px solid {rgba(colour, 0.45)}; "
            f"color: {self.palette_.text}; border-radius: 8px; padding: 12px 14px;"
        )

    def _on_remedy_requested(self, key: str) -> None:
        result = self._results.get(key)
        if result is None or result.remedy is None:
            return
        remedy = result.remedy
        note = (
            "\n\nWindows will ask for administrator approval."
            if remedy.requires_elevation
            else ""
        )
        confirm = QMessageBox(self)
        confirm.setWindowTitle(remedy.label)
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setText(f"{remedy.label} - {result.title}")
        confirm.setInformativeText(
            f"{remedy.description}{note}\n\nTypical duration: "
            f"{remedy.estimated or 'unknown'}."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Ok)
        if confirm.exec() != QMessageBox.StandardButton.Ok:
            return

        self.log_toggle.setChecked(True)
        self.request_remedy.emit(key)

    def _on_fix_all(self) -> None:
        keys = [
            key
            for key, _title, _probe in self.platform.steps()
            if (r := self._results.get(key)) is not None and r.remedy is not None
        ]
        if not keys:
            return
        lines = []
        for key in keys:
            result = self._results[key]
            assert result.remedy is not None
            admin = " (administrator)" if result.remedy.requires_elevation else ""
            lines.append(f"  {result.title}: {result.remedy.label}{admin}")

        confirm = QMessageBox(self)
        confirm.setWindowTitle("Fix all")
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setText(f"Apply {len(keys)} fix(es) in order?")
        confirm.setInformativeText(
            "\n".join(lines) + "\n\nThe run stops at the first failure."
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if confirm.exec() != QMessageBox.StandardButton.Ok:
            return

        self.log_toggle.setChecked(True)
        self.request_fix_all.emit(keys)

    def _on_remedy_started(self, key: str) -> None:
        result = self._results.get(key)
        if result is not None:
            self.status_message.emit(f"Working on {result.title}...")
        # An install has no measurable progress, so show the busy animation.
        self.progress.setRange(0, 0)
        self.progress.show()

    def _on_remedy_finished(
        self, key: str, outcome: RemedyOutcome, in_batch: bool
    ) -> None:
        result = self._results.get(key)
        title = result.title if result is not None else key
        self.status_message.emit(f"{title}: {outcome.message}")

        if not outcome.ok:
            QMessageBox.warning(self, title, outcome.message)
            return
        if outcome.needs_restart:
            QMessageBox.information(self, title, outcome.message)
        if not in_batch:
            self._on_rerun()

    def _on_batch_finished(self) -> None:
        self._on_rerun()

    def _on_step(self, message: str) -> None:
        self.status_message.emit(message)
        self.append_log(message)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.rerun_button.setEnabled(not busy)
        self.fix_all_button.setEnabled(
            not busy and any(r.remedy is not None for r in self._results.values())
        )
        if not busy:
            self.progress.hide()
        for card in self.cards.values():
            card.set_busy(busy)

    # -- log ---------------------------------------------------------------

    def append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _on_log_toggle(self, checked: bool) -> None:
        self.log_view.setVisible(checked)
        self.log_toggle.setText("Hide activity log" if checked else "Show activity log")

    def _open_log_folder(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
