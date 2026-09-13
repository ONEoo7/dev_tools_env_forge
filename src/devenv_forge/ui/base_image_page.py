"""Base image page: compare the same toolchain across distros."""

from __future__ import annotations

import time

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QBrush, QColor, QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import CATALOG, DISTROS, DISTROS_BY_KEY, Cadence, groups
from ..core.matrix import MatrixResult, Row, build_matrix
from ..core.paths import data_dir
from .cards import SectionHeader
from .theme import Palette

def _tint(hex_color: str, alpha: int) -> QColor:
    """A translucent QColor from a hex string.

    Qt stylesheets take rgba() strings but QColor does not, so table item
    brushes need the components set directly.
    """
    colour = QColor(hex_color)
    colour.setAlpha(alpha)
    return colour


def _origin(spec) -> str:
    """Where a row came from: the MSYS2 package, or added for the image."""
    return f"MSYS2: {spec.msys2}" if spec.msys2 else "Added for the image; not in the MSYS2 list"


CADENCE_LABEL = {
    Cadence.LTS: "LTS",
    Cadence.STABLE: "stable",
    Cadence.FAST: "fast",
    Cadence.ROLLING: "rolling",
}


class MatrixWorker(QObject):
    """Queries the distro package indexes off the UI thread."""

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._cancel = False

    @pyqtSlot(bool)
    def run(self, refresh: bool) -> None:
        self._cancel = False
        try:
            result = build_matrix(
                data_dir() / "cache",
                refresh=refresh,
                on_progress=lambda i, n, label: self.progress.emit(i, n, label),
                should_cancel=lambda: self._cancel,
            )
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result)

    @pyqtSlot()
    def cancel(self) -> None:
        self._cancel = True


class BaseImagePage(QWidget):
    """The comparison matrix plus a per-distro install preview."""

    status_message = pyqtSignal(str)
    request_build = pyqtSignal(bool)

    def __init__(self, palette: Palette) -> None:
        super().__init__()
        self.palette_ = palette
        self.result: MatrixResult | None = None
        self._row_lookup: dict[int, Row] = {}

        self._build_ui()
        self._start_worker()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(14)

        self.header = SectionHeader(
            "Base image",
            "The same toolchain across six distributions, read live from each "
            "distribution's own package index.",
        )
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(lambda: self._start(refresh=True))
        self.header.actions.addWidget(self.refresh_button)
        root.addWidget(self.header)

        # A cold run hits six remote indexes, so the bar carries a live clock
        # beside it. Drawing the text inside the bar clips it at the chunk
        # boundary and cannot contrast with both the filled and empty halves.
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress_row = QWidget()
        progress_layout = QHBoxLayout(self.progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(10)
        progress_layout.addWidget(self.progress, 1)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setObjectName("PageSubtitle")
        self.elapsed_label.setMinimumWidth(230)
        self.elapsed_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        progress_layout.addWidget(self.elapsed_label, 0)
        self.progress_row.hide()
        root.addWidget(self.progress_row)

        self._started_at = 0.0
        self._current_step = ""
        self.clock = QTimer(self)
        self.clock.setInterval(100)
        self.clock.timeout.connect(self._tick)

        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        root.addWidget(self.table, 1)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setObjectName("PageSubtitle")
        root.addWidget(self.note)

        picker_row = QHBoxLayout()
        picker_row.setSpacing(8)
        picker_row.addWidget(QLabel("Install line for"))
        self.distro_picker = QComboBox()
        for distro in DISTROS:
            self.distro_picker.addItem(distro.label, distro.key)
        self.distro_picker.currentIndexChanged.connect(self._update_install_preview)
        picker_row.addWidget(self.distro_picker)
        picker_row.addStretch(1)
        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self._copy_install)
        picker_row.addWidget(self.copy_button)
        root.addLayout(picker_row)

        self.install_view = QPlainTextEdit()
        self.install_view.setObjectName("Log")
        self.install_view.setReadOnly(True)
        self.install_view.setFixedHeight(96)
        root.addWidget(self.install_view)

    def _start_worker(self) -> None:
        self.thread = QThread(self)
        self.worker = MatrixWorker()
        self.worker.moveToThread(self.thread)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.request_build.connect(self.worker.run)
        self.thread.start()

    def start(self) -> None:
        if self.result is None:
            self._start(refresh=False)

    def _start(self, refresh: bool) -> None:
        self.refresh_button.setEnabled(False)
        self.progress.setRange(0, len(DISTROS))
        self.progress.setValue(0)
        self.progress_row.show()
        self._started_at = time.monotonic()
        self._current_step = "Starting"
        self._tick()
        self.clock.start()
        self.status_message.emit("Querying distribution package indexes...")
        self.request_build.emit(refresh)

    def _tick(self) -> None:
        elapsed = time.monotonic() - self._started_at
        done, total = self.progress.value(), self.progress.maximum()
        self.elapsed_label.setText(
            f"{self._current_step}   {done}/{total}   {elapsed:4.1f}s"
        )

    def shutdown(self) -> None:
        if self.thread.isRunning():
            self.worker.cancel()
            self.thread.quit()
            if not self.thread.wait(4000):
                self.thread.terminate()
                self.thread.wait(1000)

    # -- worker callbacks --------------------------------------------------

    def _on_progress(self, index: int, total: int, label: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(index)
        self._current_step = f"{label} done"
        self._tick()
        self.status_message.emit(f"{label} resolved ({index} of {total})")

    def _on_failed(self, message: str) -> None:
        self.clock.stop()
        self.progress_row.hide()
        self.refresh_button.setEnabled(True)
        self.note.setText(f"Could not build the comparison: {message}")
        self.status_message.emit("Comparison failed")

    def _on_finished(self, result: MatrixResult) -> None:
        self.clock.stop()
        self.result = result
        self.progress_row.hide()
        self.refresh_button.setEnabled(True)
        self._populate(result)
        self._update_install_preview()

        cached = all(seconds == 0 for seconds in result.timings.values())
        if cached:
            timing = f"from cache in {result.elapsed:.2f}s"
        else:
            timing = f"in {result.elapsed:.1f}s"
            slowest = result.slowest()
            if slowest is not None:
                key, seconds = slowest
                label = DISTROS_BY_KEY[key].label if key in DISTROS_BY_KEY else key
                timing += f", slowest {label} at {seconds:.1f}s"
        self.header.set_subtitle(
            "The same toolchain across six distributions, read live from each "
            f"distribution's own package index. Resolved {timing}."
        )
        summary = ", ".join(
            f"{d.label} {result.coverage(d.key)[0]}/{result.coverage(d.key)[1]}"
            for d in DISTROS
        )
        self.status_message.emit(f"Resolved {timing}. Packages available: {summary}")
        self.note.setText(
            "Some indexes had problems: " + "; ".join(result.errors)
            if result.errors
            else ""
        )

    # -- table -------------------------------------------------------------

    def _populate(self, result: MatrixResult) -> None:
        by_key = {row.spec.key: row for row in result.rows}
        grouped = groups()
        total_rows = sum(len(specs) + 1 for _name, specs in grouped) + 1

        self.table.clear()
        self._row_lookup.clear()
        self.table.setColumnCount(1 + len(DISTROS))
        self.table.setRowCount(total_rows)
        self.table.setHorizontalHeaderLabels(
            ["Package"] + [f"{d.label}\n{CADENCE_LABEL[d.cadence]}" for d in DISTROS]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(1, 1 + len(DISTROS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
            item = self.table.horizontalHeaderItem(column)
            if item is not None:
                distro = DISTROS[column - 1]
                item.setToolTip(
                    f"{distro.base_image}\n{distro.package_manager}\n"
                    f"Support: {distro.support}"
                    + (f"\n\n{distro.caveat}" if distro.caveat else "")
                    + (f"\n\n{distro.proxy_note}" if distro.proxy_note else "")
                )

        index = 0
        for group_name, specs in grouped:
            self._write_group_header(index, group_name)
            index += 1
            for spec in specs:
                row = by_key.get(spec.key)
                if row is not None:
                    self._write_row(index, row)
                index += 1

        self._write_coverage_row(index, result)
        self.table.resizeRowsToContents()

    def _write_group_header(self, index: int, name: str) -> None:
        item = QTableWidgetItem(name)
        font = QFont()
        font.setBold(True)
        item.setFont(font)
        item.setBackground(QBrush(QColor(self.palette_.surface_alt)))
        item.setForeground(QBrush(QColor(self.palette_.muted)))
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(index, 0, item)
        self.table.setSpan(index, 0, 1, 1 + len(DISTROS))

    def _write_row(self, index: int, row: Row) -> None:
        self._row_lookup[index] = row

        label = QTableWidgetItem(row.spec.label)
        label.setToolTip("\n\n".join(part for part in (_origin(row.spec), row.spec.note) if part))
        self.table.setItem(index, 0, label)

        newest = row.newest_keys()
        for column, distro in enumerate(DISTROS, start=1):
            cell = row.cells[distro.key]
            item = QTableWidgetItem(cell.display)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            tooltip = [f"{distro.label}"]
            if cell.meta:
                tooltip.append(f"Install: {cell.package}")
            elif cell.available:
                tooltip.append(f"Package: {cell.package}")
                if cell.component:
                    tooltip.append(f"Repository: {cell.component}")
            else:
                tooltip.append("No package in this distribution's index.")
            if cell.approximate:
                tooltip.append("Approximate; see the column tooltip.")

            if cell.meta:
                item.setForeground(QBrush(QColor(self.palette_.muted)))
            elif not cell.available:
                item.setForeground(QBrush(QColor(self.palette_.danger)))
            elif distro.key in newest:
                item.setBackground(QBrush(_tint(self.palette_.ok, 40)))
                item.setForeground(QBrush(QColor(self.palette_.ok)))
                font = QFont()
                font.setBold(True)
                item.setFont(font)
                tooltip.append("Newest across the compared distributions.")

            # A package from a testing repository is not what a plain install
            # gives you, so say so rather than presenting it as shipped.
            if "testing" in (cell.component or ""):
                item.setForeground(QBrush(QColor(self.palette_.warn)))
                tooltip.append("From a testing repository, not enabled by default.")

            item.setToolTip("\n".join(tooltip))
            self.table.setItem(index, column, item)

    def _write_coverage_row(self, index: int, result: MatrixResult) -> None:
        label = QTableWidgetItem("Available")
        font = QFont()
        font.setBold(True)
        label.setFont(font)
        self.table.setItem(index, 0, label)
        for column, distro in enumerate(DISTROS, start=1):
            have, total = result.coverage(distro.key)
            item = QTableWidgetItem(f"{have}/{total}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setFont(font)
            item.setForeground(
                QBrush(QColor(self.palette_.ok if have == total else self.palette_.muted))
            )
            self.table.setItem(index, column, item)

    # -- interaction -------------------------------------------------------

    def _on_row_selected(self) -> None:
        rows = {i.row() for i in self.table.selectedItems()}
        if not rows:
            return
        row = self._row_lookup.get(next(iter(rows)))
        if row is None:
            return
        parts = [f"{row.spec.label}  ({_origin(row.spec)})"]
        if row.spec.note:
            parts.append(row.spec.note)
        names = []
        for distro in DISTROS:
            cell = row.cells[distro.key]
            if cell.available:
                names.append(f"{distro.label}: {cell.package}")
        if names:
            parts.append(" | ".join(names))
        self.note.setText("   ".join(parts))

    def _update_install_preview(self) -> None:
        if self.result is None:
            return
        key = self.distro_picker.currentData()
        distro = DISTROS_BY_KEY.get(key)
        if distro is None:
            return

        available: list[str] = []
        missing: list[str] = []
        for row in self.result.rows:
            cell = row.cells.get(key)
            if cell is None:
                continue
            if cell.meta:
                if cell.package:
                    available.append(cell.package)
            elif cell.available:
                available.append(cell.package)
            else:
                missing.append(row.spec.label)

        lines = [f"{distro.install_cmd} \\", "    " + " \\\n    ".join(available)]
        text = "\n".join(lines)
        if missing:
            text += "\n\n# Not packaged, install from upstream: " + ", ".join(missing)
        if distro.caveat:
            text += f"\n# {distro.caveat}"
        self.install_view.setPlainText(text)

    def _copy_install(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.install_view.toPlainText())
            self.status_message.emit("Install line copied to the clipboard")
