"""Main window: a sidebar of workflow stages over a stacked page area."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..core.models import OSFamily, Status
from ..platforms.base import Platform
from .base_image_page import BaseImagePage
from .build_page import BuildPage
from .deploy_page import DeployPage
from .preflight_page import PreflightPage
from .theme import Palette

#: Stages after preflight. Present but disabled so the shape of the tool is
#: visible; each is filled in as it is built.
UPCOMING_STAGES = (
    ("share", "Distribute", "Push to a registry or export a portable archive."),
)


class PlaceholderPage(QWidget):
    """Stands in for a stage that has not been built yet."""

    def __init__(self, title: str, description: str, palette: Palette) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)

        heading = QLabel(title)
        heading.setObjectName("PageTitle")
        subtitle = QLabel(description)
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)

        note = QLabel("This stage is not implemented yet.")
        note.setStyleSheet(
            f"background: {palette.surface_alt}; border: 1px solid {palette.border}; "
            f"color: {palette.muted}; border-radius: 8px; padding: 14px;"
        )
        note.setWordWrap(True)

        layout.addWidget(heading)
        layout.addWidget(subtitle)
        layout.addSpacing(8)
        layout.addWidget(note)
        layout.addStretch(1)


class MainWindow(QMainWindow):
    def __init__(self, platform: Platform, palette: Palette) -> None:
        super().__init__()
        self.platform = platform
        self.palette_ = palette

        self.setWindowTitle("DevEnv Forge")
        self.resize(1020, 760)
        self.setMinimumSize(820, 600)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.stack = QStackedWidget()
        layout.addWidget(self._build_sidebar(), 0)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.preflight = PreflightPage(platform, palette)
        self.preflight.status_message.connect(self._set_status)
        self.preflight.preflight_finished.connect(self._on_preflight_finished)
        self.stack.addWidget(self.preflight)

        self.base_image = BaseImagePage(palette)
        self.base_image.status_message.connect(self._set_status)
        self.stack.addWidget(self.base_image)

        self.build = BuildPage(palette)
        self.build.status_message.connect(self._set_status)
        self.stack.addWidget(self.build)

        self.deploy = DeployPage(palette)
        self.deploy.status_message.connect(self._set_status)
        self.stack.addWidget(self.deploy)

        for _key, title, description in UPCOMING_STAGES:
            self.stack.addWidget(PlaceholderPage(title, description, palette))

        status = QStatusBar()
        self.setStatusBar(status)
        self._set_status("Ready")

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(232)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(0)

        brand = QLabel("DevEnv Forge")
        brand.setObjectName("Brand")
        subtitle = QLabel(_platform_caption(self.platform))
        subtitle.setObjectName("BrandSub")
        subtitle.setWordWrap(True)
        layout.addWidget(brand)
        layout.addWidget(subtitle)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)

        entries = [
            ("preflight", "1. Preflight"),
            ("base", "2. Base image"),
            ("build", "3. Build image"),
            ("deploy", "4. Deploy container"),
        ] + [
            (key, f"{index + 5}. {title}")
            for index, (key, title, _desc) in enumerate(UPCOMING_STAGES)
        ]
        self.nav_buttons: list[QPushButton] = []
        for index, (_key, label) in enumerate(entries):
            button = QPushButton(label)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if index == 0:
                button.setChecked(True)
            else:
                # Later stages stay locked until the environment is usable.
                button.setEnabled(False)
            button.clicked.connect(lambda _checked, i=index: self._show_page(i))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            layout.addWidget(button)

        layout.addStretch(1)
        return sidebar

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # Run the first pass only once, after the window is actually visible.
        if not getattr(self, "_started", False):
            self._started = True
            self.preflight.start()

    def _show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        # Both of these hit the network, so they wait until first opened.
        if index == 1:
            self.base_image.start()
        elif index == 2:
            self.build.start()
        elif index == 3:
            self.deploy.start()

    def _on_preflight_finished(self, overall: Status) -> None:
        ready = overall in (Status.OK, Status.INFO, Status.SKIPPED)
        for button in self.nav_buttons[1:]:
            button.setEnabled(ready)
            button.setToolTip(
                "" if ready else "Finish the preflight checks to unlock this stage."
            )

    def _set_status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        self.preflight.shutdown()
        self.base_image.shutdown()
        self.build.shutdown()
        self.deploy.shutdown()
        super().closeEvent(event)


def _platform_caption(platform: Platform) -> str:
    info = platform.os_info
    if info.family is OSFamily.WINDOWS:
        backend = "WSL2 backend"
    elif info.family is OSFamily.MACOS:
        backend = "Podman machine"
    elif info.family is OSFamily.LINUX:
        backend = "Native containers"
    else:
        backend = "Unknown backend"
    return f"{info.name}\n{backend}"
