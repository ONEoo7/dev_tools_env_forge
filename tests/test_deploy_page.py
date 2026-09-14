"""The Deploy page's Volumes section.

A share and a volume look alike in the command and are not alike on disk, so
what the page has to get right is which one someone ends up with, and whether
they are told when the storage they picked is the wrong kind for a build.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QTableWidgetItem  # noqa: E402

from devenv_forge.ui.deploy_page import DeployPage  # noqa: E402
from devenv_forge.ui.theme import DARK  # noqa: E402


@pytest.fixture
def page(qt_app):
    widget = DeployPage(DARK)
    widget.image_picker.addItem("devenv:latest", "devenv:latest")
    widget.image_picker.setCurrentIndex(widget.image_picker.count() - 1)
    widget.name_edit.setText("yocto")
    yield widget
    widget.shutdown()


def _set(page: DeployPage, row: int, column: int, text: str) -> None:
    page.volume_table.setItem(row, column, QTableWidgetItem(text))


class TestAddingAVolume:
    def test_it_starts_in_podman_storage(self, page) -> None:
        """The location is what you opt into, not what you have to clear."""
        page._add_volume()
        assert page.volumes()[0].location == ""
        assert not page.volumes()[0].translated

    def test_the_suggestion_is_named_after_the_container(self, page) -> None:
        page._add_volume()
        volume = page.volumes()[0]
        assert volume.name == "yocto-data"
        assert volume.container == "/work/data"

    def test_a_second_volume_does_not_collide(self, page) -> None:
        page._add_volume()
        page._add_volume()
        names = [v.name for v in page.volumes()]
        assert names == ["yocto-data", "yocto-data2"]
        assert page.current_spec().problems() == []

    def test_removing_a_row_removes_the_volume(self, page) -> None:
        page._add_volume()
        page.volume_table.selectRow(0)
        page._remove_volume()
        assert page.volumes() == []


class TestTheCommandShown:
    def test_the_volume_is_created_before_the_container_runs(self, page) -> None:
        page._add_volume()
        _set(page, 0, 0, "yocto-build")
        _set(page, 0, 1, "/work/build")
        text = page.preview.toPlainText()
        assert text.index("volume create") < text.index("podman run")
        assert "-v yocto-build:/work/build" in text

    def test_a_location_becomes_the_machines_view_of_it(self, page) -> None:
        page._add_volume()
        _set(page, 0, 2, "D:\\yocto")
        assert "device=/mnt/d/yocto" in page.preview.toPlainText()

    def test_no_volumes_leaves_the_command_as_it_was(self, page) -> None:
        assert page.preview.toPlainText().startswith("podman run")


class TestTheWarning:
    def test_silent_for_storage_inside_the_machine(self, page) -> None:
        page._add_volume()
        assert not page.volume_note.isVisibleTo(page)

    def test_shown_for_a_windows_drive(self, page) -> None:
        page._add_volume()
        _set(page, 0, 2, "D:\\yocto")
        assert page.volume_note.isVisibleTo(page)
        assert "9p" in page.volume_note.text()

    def test_it_goes_away_when_the_location_is_cleared(self, page) -> None:
        page._add_volume()
        _set(page, 0, 2, "D:\\yocto")
        _set(page, 0, 2, "")
        assert not page.volume_note.isVisibleTo(page)

    def test_a_warning_does_not_block_starting(self, page, tmp_path) -> None:
        """It is the wrong place for a build, not an invalid one."""
        page._add_volume()
        _set(page, 0, 2, str(tmp_path))
        assert page.current_spec().problems() == []
        assert page.deploy_button.isEnabled()
