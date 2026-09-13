"""Build image page: choose a base, pick extras, generate and run the build."""

from __future__ import annotations

import time

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import DISTROS, DISTROS_BY_KEY
from ..core.containerfile import ImageSpec, build_command, generate
from ..core.extras import EXTRAS, ExtraKind, default_selection, implied_by, resolve
from ..core.github import latest_release
from ..core.matrix import MatrixResult, build_matrix
from ..core.paths import data_dir
from ..core import podman as podman_cli
from ..core.runner import stream
from .cards import SectionHeader
from .theme import Palette

KIND_LABEL = {
    ExtraKind.RUSTUP_TARGET: "target",
    ExtraKind.RUSTUP_COMPONENT: "component",
    ExtraKind.CARGO_INSTALL: "cargo install",
    ExtraKind.SDK: "git clone",
}


class BuildWorker(QObject):
    """Loads the package matrix and runs podman build, off the UI thread."""

    matrix_ready = pyqtSignal(object, dict)
    matrix_failed = pyqtSignal(str)
    output = pyqtSignal(str)
    build_finished = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()
        self._cancel = False

    @pyqtSlot()
    def load_matrix(self) -> None:
        try:
            result = build_matrix(data_dir() / "cache")
        except Exception as exc:
            self.matrix_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        # Resolve release tags for the SDK extras in the same pass, so the
        # preview shows the real tag rather than the fallback.
        versions: dict[str, str] = {}
        for extra in EXTRAS:
            if not extra.repo:
                continue
            release = latest_release(
                extra.repo, data_dir() / "cache", fallback=extra.fallback_tag
            )
            if release.tag:
                versions[extra.key] = release.tag
        self.matrix_ready.emit(result, versions)

    @pyqtSlot(list, str)
    def run_build(self, argv: list, cwd: str) -> None:
        self._cancel = False
        code = 1
        self.output.emit(f"$ {' '.join(argv)}")
        for kind, text in stream(argv, cwd=cwd, timeout=7200):
            if self._cancel:
                self.output.emit("cancelled")
                break
            if kind == "line":
                self.output.emit(text)
            else:
                code = int(text) if text.lstrip("-").isdigit() else 1
        self.build_finished.emit(code)

    @pyqtSlot()
    def cancel(self) -> None:
        self._cancel = True


class ExtraRow(QFrame):
    """One extra: a checkbox, the literal command, and why it is there."""

    toggled = pyqtSignal()

    def __init__(self, extra, palette: Palette) -> None:
        super().__init__()
        self.extra = extra
        self.palette_ = palette
        self.setObjectName("Card")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.checkbox = QCheckBox(extra.label)
        self.checkbox.setChecked(extra.default_on)
        self.checkbox.toggled.connect(lambda _c: self.toggled.emit())
        top.addWidget(self.checkbox, 1)

        kind = QLabel(KIND_LABEL.get(extra.kind, ""))
        kind.setObjectName("Badge")
        kind.setStyleSheet(
            f"color: {palette.muted}; border: 1px solid {palette.border}; "
            "border-radius: 8px; padding: 1px 8px; font-size: 8pt;"
        )
        top.addWidget(kind, 0)
        layout.addLayout(top)

        self.command_label = QLabel(self._command_text(extra.fallback_tag))
        command = self.command_label
        command.setStyleSheet(
            f"color: {palette.accent}; font-family: 'Cascadia Mono', Consolas, "
            "monospace; font-size: 9pt;"
        )
        command.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        command.setWordWrap(True)
        layout.addWidget(command)

        self.note = QLabel(extra.note)
        self.note.setObjectName("PageSubtitle")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        self.forced = QLabel("")
        self.forced.setStyleSheet(f"color: {palette.warn}; font-size: 9pt;")
        self.forced.setWordWrap(True)
        self.forced.hide()
        layout.addWidget(self.forced)

    def _command_text(self, version: str) -> str:
        """The command as it will be written, with the tag filled in."""
        if "{version}" not in self.extra.command:
            return self.extra.command
        return self.extra.command.format(version=version or "<unresolved>")

    def set_version(self, version: str) -> None:
        """Show the resolved release tag rather than the placeholder."""
        self.command_label.setText(self._command_text(version))

    def set_forced_by(self, labels: list[str]) -> None:
        """Lock the checkbox on when another selected extra depends on it."""
        if labels:
            self.checkbox.setChecked(True)
            self.checkbox.setEnabled(False)
            self.forced.setText("Required by " + ", ".join(labels))
            self.forced.show()
        else:
            self.checkbox.setEnabled(True)
            self.forced.hide()

    @property
    def key(self) -> str:
        return self.extra.key

    def is_checked(self) -> bool:
        return self.checkbox.isChecked()


class BuildPage(QWidget):
    """Assembles an image definition and runs the build."""

    status_message = pyqtSignal(str)
    request_matrix = pyqtSignal()
    request_build = pyqtSignal(list, str)

    def __init__(self, palette: Palette) -> None:
        super().__init__()
        self.palette_ = palette
        self.matrix: MatrixResult | None = None
        self.sdk_versions: dict[str, str] = {}
        self.rows: list[ExtraRow] = []
        self._building = False
        self._build_started = 0.0

        self._build_ui()
        self._start_worker()
        # Show the rustup and extras part straight away; the distro packages
        # fill in once the matrix arrives.
        self._refresh_preview()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(14)

        self.header = SectionHeader(
            "Build image",
            "Turn the selected distribution and toolchain into a Containerfile, "
            "then build it with podman.",
        )
        root.addWidget(self.header)

        root.addLayout(self._build_options_row())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_extras_panel())
        splitter.addWidget(self._build_preview_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        root.addWidget(splitter, 1)

        root.addLayout(self._build_actions_row())

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.elapsed_label = QLabel("")
        self.elapsed_label.setObjectName("PageSubtitle")
        self.elapsed_label.setMinimumWidth(150)
        self.elapsed_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.elapsed_label, 0)
        root.addLayout(progress_row)

        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(6000)
        self.log.setFixedHeight(150)
        self.log.hide()
        root.addWidget(self.log)

        self.clock = QTimer(self)
        self.clock.setInterval(200)
        self.clock.timeout.connect(self._tick)

    def _build_options_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        row.addWidget(QLabel("Base"))
        self.distro_picker = QComboBox()
        for distro in DISTROS:
            self.distro_picker.addItem(distro.label, distro.key)
        self.distro_picker.setCurrentIndex(1)  # Debian, the recommended base
        self.distro_picker.currentIndexChanged.connect(self._refresh_preview)
        row.addWidget(self.distro_picker)

        row.addSpacing(12)
        row.addWidget(QLabel("Rust"))
        self.toolchain_edit = QLineEdit("stable")
        self.toolchain_edit.setMaximumWidth(120)
        self.toolchain_edit.setToolTip(
            "A rustup toolchain. Pin it to a version such as 1.98.0 so every "
            "team member gets the same compiler."
        )
        self.toolchain_edit.textChanged.connect(self._refresh_preview)
        row.addWidget(self.toolchain_edit)

        row.addSpacing(12)
        row.addWidget(QLabel("Image"))
        self.name_edit = QLineEdit("devenv")
        self.name_edit.setMaximumWidth(150)
        self.name_edit.textChanged.connect(self._refresh_preview)
        row.addWidget(self.name_edit)
        row.addWidget(QLabel(":"))
        self.tag_edit = QLineEdit("latest")
        self.tag_edit.setMaximumWidth(110)
        self.tag_edit.textChanged.connect(self._refresh_preview)
        row.addWidget(self.tag_edit)

        row.addStretch(1)
        return row

    def _build_extras_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)

        title = QLabel("Extras")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        blurb = QLabel(
            "Toolchain pieces that no distribution packages. The rustup and "
            "cargo entries add a rustup install to the image, because the "
            "distribution's rustc cannot provide them. The SDK is a source "
            "checkout and needs no Rust at all."
        )
        blurb.setObjectName("PageSubtitle")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(8)

        for extra in EXTRAS:
            row = ExtraRow(extra, self.palette_)
            row.toggled.connect(self._on_extra_toggled)
            self.rows.append(row)
            holder_layout.addWidget(row)
        holder_layout.addStretch(1)

        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Containerfile")
        title.setObjectName("CardTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("PageSubtitle")
        header.addWidget(self.summary_label)
        layout.addLayout(header)

        self.preview = QPlainTextEdit()
        self.preview.setObjectName("Detail")
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.preview, 1)
        return panel

    def _build_actions_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.log_toggle = QPushButton("Show build log")
        self.log_toggle.setObjectName("Link")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_toggle.toggled.connect(self._on_log_toggle)
        row.addWidget(self.log_toggle)
        row.addStretch(1)

        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self._copy)
        row.addWidget(self.copy_button)

        self.save_button = QPushButton("Save Containerfile")
        self.save_button.clicked.connect(self._save)
        row.addWidget(self.save_button)

        self.build_button = QPushButton("Build image")
        self.build_button.setObjectName("Primary")
        self.build_button.clicked.connect(self._on_build)
        row.addWidget(self.build_button)
        return row

    def _start_worker(self) -> None:
        self.thread = QThread(self)
        self.worker = BuildWorker()
        self.worker.moveToThread(self.thread)
        self.worker.matrix_ready.connect(self._on_matrix)
        self.worker.matrix_failed.connect(self._on_matrix_failed)
        self.worker.output.connect(self._append_log)
        self.worker.build_finished.connect(self._on_build_finished)
        self.request_matrix.connect(self.worker.load_matrix)
        self.request_build.connect(self.worker.run_build)
        self.thread.start()

    def start(self) -> None:
        if self.matrix is None:
            self.status_message.emit("Loading package list...")
            self.request_matrix.emit()

    def shutdown(self) -> None:
        if self.thread.isRunning():
            self.worker.cancel()
            self.thread.quit()
            if not self.thread.wait(4000):
                self.thread.terminate()
                self.thread.wait(1000)

    # -- state -------------------------------------------------------------

    def _on_matrix(self, result: MatrixResult, versions: dict) -> None:
        self.matrix = result
        self.sdk_versions = dict(versions)
        for row in self.rows:
            if row.extra.repo:
                row.set_version(self.sdk_versions.get(row.key, row.extra.fallback_tag))
        self._refresh_preview()
        self.status_message.emit("Ready to build")

    def _on_matrix_failed(self, message: str) -> None:
        self.status_message.emit(f"Could not load the package list: {message}")
        self._refresh_preview()

    def _on_extra_toggled(self) -> None:
        selected = self.selected_extras()
        for row in self.rows:
            row.set_forced_by(implied_by(row.key, selected))
        self._refresh_preview()

    def selected_extras(self) -> set[str]:
        return {row.key for row in self.rows if row.is_checked()}

    def current_spec(self) -> ImageSpec:
        key = self.distro_picker.currentData()
        distro = DISTROS_BY_KEY[key]
        packages: list[str] = []
        if self.matrix is not None:
            for row in self.matrix.rows:
                cell = row.cells.get(key)
                if cell is None:
                    continue
                if cell.meta and cell.package:
                    packages.append(cell.package)
                elif cell.available and cell.package:
                    packages.append(cell.package)
        return ImageSpec(
            distro=distro,
            packages=packages,
            selected_extras=self.selected_extras(),
            rust_toolchain=self.toolchain_edit.text().strip() or "stable",
            image_name=self.name_edit.text().strip() or "devenv",
            image_tag=self.tag_edit.text().strip() or "latest",
            sdk_versions=dict(self.sdk_versions),
        )

    def _refresh_preview(self) -> None:
        spec = self.current_spec()
        self.preview.setPlainText(generate(spec))
        extras = resolve(spec.selected_extras)
        self.summary_label.setText(
            f"{spec.distro.base_image}   {len(spec.packages)} packages   "
            f"{len(extras)} extras"
        )

    # -- actions -----------------------------------------------------------

    def _copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.preview.toPlainText())
            self.status_message.emit("Containerfile copied to the clipboard")

    def _save(self) -> str | None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Containerfile", "Containerfile", "All files (*)"
        )
        if not path:
            return None
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(self.preview.toPlainText())
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return None
        self.status_message.emit(f"Saved to {path}")
        return path

    def _on_build(self) -> None:
        if self._building:
            return
        state = podman_cli.status()
        if not state.ready:
            QMessageBox.warning(
                self,
                "Podman not available",
                "\n\n".join(part for part in (state.detail, state.hint) if part),
            )
            return

        spec = self.current_spec()
        confirm = QMessageBox(self)
        confirm.setWindowTitle("Build image")
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setText(f"Build {spec.reference}?")
        detail = (
            f"Base: {spec.distro.base_image}\n"
            f"Packages: {len(spec.packages)}\n"
            f"Extras: {len(resolve(spec.selected_extras))}\n\n"
            "Choose a directory to write the Containerfile into. The build runs "
            "there."
        )
        if "probe-rs-tools" in spec.selected_extras:
            detail += (
                "\n\nprobe-rs-tools compiles from source and can take several "
                "minutes on its own."
            )
        confirm.setInformativeText(detail)
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if confirm.exec() != QMessageBox.StandardButton.Ok:
            return

        directory = QFileDialog.getExistingDirectory(self, "Build context directory")
        if not directory:
            return
        target = f"{directory}/Containerfile"
        try:
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(self.preview.toPlainText())
        except OSError as exc:
            QMessageBox.warning(self, "Could not write the Containerfile", str(exc))
            return

        self._building = True
        self.build_button.setEnabled(False)
        self.log_toggle.setChecked(True)
        self.log.clear()
        self.progress.show()
        self._build_started = time.monotonic()
        self._tick()
        self.clock.start()
        self.status_message.emit(f"Building {spec.reference}...")
        self.request_build.emit(build_command(spec), directory)

    def _on_build_finished(self, code: int) -> None:
        self.clock.stop()
        self._building = False
        self.build_button.setEnabled(True)
        self.progress.hide()
        elapsed = time.monotonic() - self._build_started
        if code == 0:
            self.elapsed_label.setText(f"built in {elapsed:.0f}s")
            self.status_message.emit(f"Build succeeded in {elapsed:.0f}s")
        else:
            self.elapsed_label.setText(f"failed after {elapsed:.0f}s")
            self.status_message.emit(f"Build failed with exit code {code}")
            QMessageBox.warning(
                self,
                "Build failed",
                f"podman build exited with code {code}. The build log has the "
                "details.",
            )

    def _tick(self) -> None:
        self.elapsed_label.setText(
            f"building   {time.monotonic() - self._build_started:5.0f}s"
        )

    def _append_log(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _on_log_toggle(self, checked: bool) -> None:
        self.log.setVisible(checked)
        self.log_toggle.setText("Hide build log" if checked else "Show build log")
