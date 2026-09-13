"""Application bootstrap."""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication, QMessageBox

from .core import paths, podman
from .platforms import create_platform
from .ui.main_window import MainWindow
from .ui.theme import active_palette, build_stylesheet

APP_ID = "DevEnvForge.Podman.DevEnvironments.1"


def _set_windows_app_id() -> None:
    """Give the app its own taskbar identity instead of inheriting Python's."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def run(argv: list[str] | None = None) -> int:
    _set_windows_app_id()
    # Before anything shells out: a terminal without APPDATA hides podman's
    # connections, and every child process would inherit that.
    podman.repair_environment()
    paths.ensure_dirs()

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("DevEnv Forge")
    app.setOrganizationName("DevEnv Forge")

    palette = active_palette()
    app.setStyleSheet(build_stylesheet(palette))

    try:
        platform = create_platform()
    except RuntimeError as exc:
        QMessageBox.critical(None, "Unsupported system", str(exc))
        return 2

    window = MainWindow(platform, palette)
    window.show()
    return app.exec()
