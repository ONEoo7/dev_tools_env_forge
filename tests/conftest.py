"""Test configuration.

Qt needs a display. Selecting the offscreen platform before PyQt6 is imported
lets the UI tests run on a build agent with no desktop session.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qt_app():
    """One QApplication for every widget test.

    It has to stay referenced: an application Python lets go of is destroyed at
    once, and the next widget aborts the whole test process.
    """
    widgets = pytest.importorskip("PyQt6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    yield app
