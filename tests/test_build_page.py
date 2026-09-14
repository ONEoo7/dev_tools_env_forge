"""The Build image page, where the extras on offer follow the chosen base.

An extra written against one distribution's documented requirements is shown
for that base and hidden for the rest. Hidden, not unticked: a selection has to
survive someone looking at what another base would give them.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from devenv_forge.ui.build_page import BuildPage  # noqa: E402
from devenv_forge.ui.theme import DARK  # noqa: E402


@pytest.fixture
def page(qt_app):
    """A real Build page. Construction does no network; start() would."""
    widget = BuildPage(DARK)
    yield widget
    widget.shutdown()


def _row(page: BuildPage, key: str):
    return next(row for row in page.rows if row.key == key)


def _select(page: BuildPage, distro_key: str) -> None:
    page.distro_picker.setCurrentIndex(page.distro_picker.findData(distro_key))


class TestExtrasFollowTheBaseImage:
    def test_the_yocto_host_is_hidden_on_the_default_base(self, page) -> None:
        """The page opens on Debian, which that extra is not written for."""
        assert page.distro_picker.currentData() == "debian"
        assert not _row(page, "yocto").isVisibleTo(page)

    def test_it_appears_on_ubuntu(self, page) -> None:
        _select(page, "ubuntu")
        assert _row(page, "yocto").isVisibleTo(page)

    def test_it_goes_away_again(self, page) -> None:
        _select(page, "ubuntu")
        _select(page, "fedora")
        assert not _row(page, "yocto").isVisibleTo(page)

    def test_the_general_extras_stay_on_every_base(self, page) -> None:
        for distro_key in ("ubuntu", "debian", "fedora", "alpine", "arch"):
            _select(page, distro_key)
            assert _row(page, "pico-sdk").isVisibleTo(page), distro_key

    def test_a_tick_survives_a_look_at_another_base(self, page) -> None:
        _select(page, "ubuntu")
        _row(page, "yocto").checkbox.setChecked(True)

        _select(page, "debian")
        assert "yocto" not in {e.key for e in page.current_spec().extras}

        _select(page, "ubuntu")
        assert _row(page, "yocto").is_checked()
        assert "yocto" in {e.key for e in page.current_spec().extras}

    def test_the_summary_counts_what_will_be_written(self, page) -> None:
        """Not what is ticked: a hidden tick contributes nothing."""
        _select(page, "ubuntu")
        _row(page, "yocto").checkbox.setChecked(True)
        with_yocto = len(page.current_spec().extras)

        _select(page, "debian")
        assert len(page.current_spec().extras) == with_yocto - 1


def _pick(page: BuildPage, distro_key: str, arch_key: str) -> None:
    page.distro_picker.setCurrentIndex(page.distro_picker.findData(distro_key))
    page.arch_picker.setCurrentIndex(page.arch_picker.findData(arch_key))


class TestTheArchitecturePicker:
    def test_it_offers_both_and_starts_native(self, page) -> None:
        """Native is the common case and the only one needing no emulation."""
        from devenv_forge.core.catalog import host_arch

        keys = [page.arch_picker.itemData(i) for i in range(page.arch_picker.count())]
        assert keys == ["amd64", "arm64"]
        assert page.arch_picker.currentData() == host_arch().key

    def test_the_target_reaches_the_file(self, page) -> None:
        _pick(page, "ubuntu", "arm64")
        assert "FROM --platform=linux/arm64 ubuntu:26.04" in page.preview.toPlainText()

    def test_the_summary_says_which(self, page) -> None:
        _pick(page, "ubuntu", "arm64")
        assert "ARM64 (aarch64)" in page.summary_label.text()


class TestWhatTheTargetRulesOut:
    def test_an_x86_only_extra_disappears(self, page) -> None:
        _pick(page, "ubuntu", "amd64")
        assert _row(page, "aosp").isVisibleTo(page)
        _pick(page, "ubuntu", "arm64")
        assert not _row(page, "aosp").isVisibleTo(page)

    def test_a_tick_survives_the_trip(self, page) -> None:
        _pick(page, "ubuntu", "amd64")
        _row(page, "aosp").checkbox.setChecked(True)
        _pick(page, "ubuntu", "arm64")
        assert "repo" not in page.preview.toPlainText()
        _pick(page, "ubuntu", "amd64")
        assert _row(page, "aosp").is_checked()
        assert "repo" in page.preview.toPlainText()

    def test_a_base_without_that_image_is_greyed(self, page) -> None:
        _pick(page, "ubuntu", "arm64")
        model = page.distro_picker.model()
        index = page.distro_picker.findData("arch")
        assert not model.item(index).isEnabled()
        assert model.item(page.distro_picker.findData("debian")).isEnabled()

    def test_choosing_it_anyway_stops_the_build(self, page) -> None:
        _pick(page, "arch", "arm64")
        assert not page.build_button.isEnabled()
        assert "no ARM64" in page.arch_note.text()

    def test_and_the_build_comes_back_when_it_can_run(self, page) -> None:
        _pick(page, "arch", "arm64")
        _pick(page, "arch", "amd64")
        assert page.build_button.isEnabled()


class TestTheEmulationNote:
    def test_silent_when_building_for_this_machine(self, page) -> None:
        from devenv_forge.core.catalog import host_arch

        _pick(page, "ubuntu", host_arch().key)
        assert not page.arch_note.isVisibleTo(page)

    def test_shown_when_building_for_another(self, page) -> None:
        from devenv_forge.core.catalog import host_arch

        other = "arm64" if host_arch().key == "amd64" else "amd64"
        _pick(page, "ubuntu", other)
        assert page.arch_note.isVisibleTo(page)
        assert "emulation" in page.arch_note.text()
        assert "Exec format error" in page.arch_note.text()
