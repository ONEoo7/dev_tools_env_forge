"""The per-check card widget."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.models import CheckResult, Status
from .theme import STATUS_LABEL, Palette, rgba, status_color


class StatusDot(QLabel):
    """A small coloured disc conveying status at a glance."""

    def __init__(self, palette: Palette) -> None:
        super().__init__()
        self._palette = palette
        self.setFixedSize(12, 12)
        self.set_status(None)

    def set_status(self, status: Status | None) -> None:
        colour = (
            self._palette.neutral if status is None else status_color(status, self._palette)
        )
        self.setStyleSheet(f"background: {colour}; border-radius: 6px;")


class CheckCard(QFrame):
    """One preflight check: status, summary, expandable detail, fix button."""

    remedy_requested = pyqtSignal(str)

    def __init__(self, key: str, title: str, palette: Palette) -> None:
        super().__init__()
        self.key = key
        self._palette = palette
        self._result: CheckResult | None = None

        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)

        self.dot = StatusDot(palette)
        top.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignTop)

        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        self.summary_label = QLabel("Waiting")
        self.summary_label.setObjectName("CardSummary")
        self.summary_label.setWordWrap(True)
        text_column.addWidget(self.title_label)
        text_column.addWidget(self.summary_label)
        top.addLayout(text_column, 1)

        self.badge = QLabel("")
        self.badge.setObjectName("Badge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.hide()
        top.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)

        self.fix_button = QPushButton("Fix")
        self.fix_button.setObjectName("Primary")
        self.fix_button.hide()
        self.fix_button.clicked.connect(lambda: self.remedy_requested.emit(self.key))
        top.addWidget(self.fix_button, 0, Qt.AlignmentFlag.AlignTop)

        outer.addLayout(top)

        self.detail_toggle = QPushButton("Show details")
        self.detail_toggle.setObjectName("Link")
        self.detail_toggle.setCheckable(True)
        self.detail_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.detail_toggle.toggled.connect(self._on_toggle)
        self.detail_toggle.hide()

        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(24, 0, 0, 0)
        toggle_row.addWidget(self.detail_toggle)
        toggle_row.addStretch(1)
        outer.addLayout(toggle_row)

        self.detail = QTextEdit()
        self.detail.setObjectName("Detail")
        self.detail.setReadOnly(True)
        self.detail.hide()
        outer.addWidget(self.detail)

    # -- state -------------------------------------------------------------

    def set_running(self) -> None:
        self.dot.set_status(None)
        self.summary_label.setText("Checking...")
        self.badge.hide()
        self.fix_button.hide()
        self.detail_toggle.hide()
        self.detail.hide()

    def set_result(self, result: CheckResult) -> None:
        self._result = result
        self.title_label.setText(result.title)
        self.summary_label.setText(result.summary)
        self.dot.set_status(result.status)

        colour = status_color(result.status, self._palette)
        self.badge.setText(STATUS_LABEL.get(result.status, result.status.value))
        self.badge.setStyleSheet(
            f"background: {rgba(colour, 0.14)}; color: {colour}; "
            f"border: 1px solid {rgba(colour, 0.45)}; border-radius: 9px; "
            "padding: 2px 10px; font-size: 8pt; font-weight: 600;"
        )
        self.badge.show()

        if result.remedy is not None:
            label = result.remedy.label
            if result.remedy.requires_elevation:
                label += "  (admin)"
            self.fix_button.setText(label)
            self.fix_button.setToolTip(
                f"{result.remedy.description}\n\nTypical duration: "
                f"{result.remedy.estimated or 'unknown'}"
            )
            self.fix_button.show()
            self.fix_button.setEnabled(True)
        else:
            self.fix_button.hide()

        body = self._compose_detail(result)
        if body:
            self.detail.setPlainText(body)
            self.detail_toggle.show()
            # Anything needing action opens expanded; healthy checks stay quiet.
            if result.status.needs_action or result.status is Status.FAILED:
                self.detail_toggle.setChecked(True)
        else:
            self.detail_toggle.hide()
            self.detail.hide()

    @staticmethod
    def _compose_detail(result: CheckResult) -> str:
        parts: list[str] = []
        if result.detail:
            parts.append(result.detail)
        if result.evidence:
            parts.append("")
            parts.append("Evidence")
            for key, value in result.evidence.items():
                if isinstance(value, (list, tuple)):
                    rendered = ", ".join(str(v) for v in value) or "(none)"
                else:
                    rendered = str(value)
                parts.append(f"  {key}: {rendered}")
        return "\n".join(parts).strip()

    def set_busy(self, busy: bool) -> None:
        if self._result is not None and self._result.remedy is not None:
            self.fix_button.setEnabled(not busy)

    def _on_toggle(self, checked: bool) -> None:
        self.detail.setVisible(checked)
        self.detail_toggle.setText("Hide details" if checked else "Show details")
        if checked:
            # Grow to fit the content instead of scrolling a tiny viewport.
            document_height = int(self.detail.document().size().height())
            self.detail.setFixedHeight(max(90, min(320, document_height + 24)))


class SectionHeader(QWidget):
    """Page title, subtitle and a right-aligned slot for actions."""

    def __init__(self, title: str, subtitle: str) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        column = QVBoxLayout()
        column.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("PageTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("PageSubtitle")
        self.subtitle_label.setWordWrap(True)
        column.addWidget(self.title_label)
        column.addWidget(self.subtitle_label)
        layout.addLayout(column, 1)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        layout.addLayout(self.actions, 0)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
