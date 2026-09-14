"""Tests for the pure logic: no registry writes, no installs, no subprocesses."""

from __future__ import annotations

import os

import pytest

from devenv_forge.core.models import Status
from devenv_forge.core.runner import smart_decode


class TestSmartDecode:
    """Output encoding is the single most error-prone part of shelling out."""

    def test_utf16le_with_bom(self) -> None:
        raw = "WSL version: 2.6.3.0".encode("utf-16-le")
        assert smart_decode(b"\xff\xfe" + raw) == "WSL version: 2.6.3.0"

    def test_utf16le_without_bom(self) -> None:
        # This is exactly what wsl.exe emits when its output is redirected.
        raw = "  NAME    STATE    VERSION\r\n* Ubuntu-24.04  Stopped  2".encode(
            "utf-16-le"
        )
        decoded = smart_decode(raw)
        assert "Ubuntu-24.04" in decoded
        assert "\x00" not in decoded

    def test_plain_utf8(self) -> None:
        assert smart_decode(b"v1.29.290\r\n") == "v1.29.290\r\n"

    def test_utf8_with_bom(self) -> None:
        assert smart_decode("\ufeffhello".encode("utf-8")) == "hello"

    def test_non_ascii_utf8(self) -> None:
        assert smart_decode("café — naïve".encode("utf-8")) == "café — naïve"

    def test_empty(self) -> None:
        assert smart_decode(b"") == ""

    def test_invalid_bytes_do_not_raise(self) -> None:
        assert smart_decode(b"\x81\x82ok") != ""

    def test_short_output_is_not_mistaken_for_utf16(self) -> None:
        # Too short for the NUL-density heuristic to be trusted.
        assert smart_decode(b"ok\n") == "ok\n"


class TestStatus:
    def test_worst_picks_the_most_severe(self) -> None:
        assert Status.worst([Status.OK, Status.MISSING, Status.INFO]) is Status.MISSING
        assert Status.worst([Status.OK, Status.REPAIRABLE]) is Status.REPAIRABLE
        assert Status.worst([Status.OK, Status.OK]) is Status.OK

    def test_worst_of_nothing(self) -> None:
        assert Status.worst([]) is Status.INFO

    def test_needs_action(self) -> None:
        assert Status.MISSING.needs_action
        assert Status.REPAIRABLE.needs_action
        assert not Status.OK.needs_action
        assert not Status.SKIPPED.needs_action


pytestmark_windows = pytest.mark.skipif(
    os.name != "nt", reason="Windows-specific behaviour"
)


@pytestmark_windows
class TestPathNormalisation:
    def test_case_and_separator_insensitive(self) -> None:
        from devenv_forge.platforms.winenv import normalise_entry

        assert normalise_entry(r"C:\Windows\System32") == normalise_entry(
            "c:/windows/system32/"
        )

    def test_strips_quotes_and_trailing_slash(self) -> None:
        from devenv_forge.platforms.winenv import normalise_entry

        assert normalise_entry('"C:\\Tools\\"') == normalise_entry(r"C:\Tools")

    def test_expands_environment_variables(self) -> None:
        from devenv_forge.platforms.winenv import normalise_entry

        expanded = normalise_entry("%SystemRoot%")
        assert expanded == normalise_entry(os.environ["SystemRoot"])

    def test_empty_entry(self) -> None:
        from devenv_forge.platforms.winenv import normalise_entry

        assert normalise_entry("   ") == ""

    def test_path_contains_matches_unexpanded_entry(self) -> None:
        """A PATH holding %SystemRoot% must still match the literal directory."""
        from devenv_forge.platforms.winenv import path_contains

        raw = r"%SystemRoot%;C:\Tools"
        assert path_contains(os.environ["SystemRoot"], raw)

    def test_path_contains_rejects_absent(self) -> None:
        from devenv_forge.platforms.winenv import path_contains

        assert not path_contains(r"C:\definitely\not\here\xyzzy", r"C:\Tools")


def _entry(name: str) -> str:
    """An absolute PATH entry that contains no os.pathsep on any platform."""
    return os.path.join(os.sep, "tools", name)


class TestMergePath:
    """PATH is merged into, never replaced.

    Replacing it with the registry's copy is how an inherited entry -- one the
    launching shell added and never stored anywhere -- disappears for the rest
    of the session, taking whatever lived there with it.
    """

    def test_an_inherited_entry_survives(self) -> None:
        from devenv_forge.core.runner import merge_path

        inherited = _entry("only-in-this-process")
        merged = merge_path(inherited, _entry("from-the-registry"))
        assert inherited in merged.split(os.pathsep)

    def test_a_new_entry_is_appended(self) -> None:
        from devenv_forge.core.runner import merge_path

        merged = merge_path(_entry("a"), _entry("b"))
        assert merged.split(os.pathsep) == [_entry("a"), _entry("b")]

    def test_current_entries_keep_their_precedence(self) -> None:
        """Appended, not prepended: the process's own lookups must not change."""
        from devenv_forge.core.runner import merge_path

        merged = merge_path(
            os.pathsep.join([_entry("first"), _entry("second")]), _entry("third")
        )
        assert merged.split(os.pathsep) == [
            _entry("first"),
            _entry("second"),
            _entry("third"),
        ]

    def test_nothing_new_leaves_it_untouched(self) -> None:
        from devenv_forge.core.runner import merge_path

        current = os.pathsep.join([_entry("a"), _entry("b")])
        assert merge_path(current, _entry("b"), _entry("a")) == current

    def test_an_addition_is_not_duplicated(self) -> None:
        from devenv_forge.core.runner import merge_path

        merged = merge_path(_entry("a"), _entry("b"), _entry("b"))
        assert merged.split(os.pathsep).count(_entry("b")) == 1

    def test_several_additions_are_taken_in_order(self) -> None:
        from devenv_forge.core.runner import merge_path

        merged = merge_path(_entry("a"), _entry("b"), _entry("c"))
        assert merged.split(os.pathsep) == [_entry("a"), _entry("b"), _entry("c")]

    def test_an_empty_current_gains_no_leading_separator(self) -> None:
        """A leading separator is an empty entry, which means "here"."""
        from devenv_forge.core.runner import merge_path

        assert merge_path("", _entry("a")) == _entry("a")

    def test_a_trailing_separator_does_not_become_an_empty_entry(self) -> None:
        from devenv_forge.core.runner import merge_path

        merged = merge_path(_entry("a") + os.pathsep, _entry("b"))
        assert "" not in merged.split(os.pathsep)

    def test_blank_additions_add_nothing(self) -> None:
        from devenv_forge.core.runner import merge_path

        current = _entry("a")
        assert merge_path(current, "", os.pathsep, "   ") == current


@pytestmark_windows
class TestMergePathOnWindows:
    def test_case_and_trailing_separator_are_not_new_entries(self) -> None:
        from devenv_forge.core.runner import merge_path

        current = r"C:\Program Files\RedHat\Podman"
        assert merge_path(current, "c:/program files/redhat/podman/") == current

    def test_an_unexpanded_variable_is_not_a_new_entry(self) -> None:
        """The registry stores %SystemRoot%; the process holds it expanded."""
        from devenv_forge.core.runner import merge_path

        current = os.environ["SystemRoot"]
        assert merge_path(current, "%SystemRoot%") == current


@pytestmark_windows
class TestRegistryReload:
    """Reading the registry must add to this process's PATH, not supplant it.

    The regression: the whole PATH was replaced with the machine and user
    values, so every directory the launching shell had contributed vanished --
    silently, until some unrelated tool could no longer be found.
    """

    def test_an_inherited_entry_survives_the_reload(self, monkeypatch) -> None:
        from devenv_forge.core import podman

        inherited = r"C:\inherited\from\the\shell"
        monkeypatch.setenv("PATH", inherited)
        podman.reload_path_from_registry()
        assert inherited in os.environ["PATH"].split(os.pathsep)

    def test_the_registry_entries_are_picked_up(self, monkeypatch) -> None:
        from devenv_forge.core import podman

        monkeypatch.setenv("PATH", r"C:\inherited\from\the\shell")
        assert podman.reload_path_from_registry()
        assert "System32" in os.environ["PATH"]

    def test_a_second_reload_changes_nothing(self, monkeypatch) -> None:
        """Nothing new means no write and no retry of the lookup that failed."""
        from devenv_forge.core import podman

        monkeypatch.setenv("PATH", r"C:\inherited\from\the\shell")
        podman.reload_path_from_registry()
        before = os.environ["PATH"]
        assert not podman.reload_path_from_registry()
        assert os.environ["PATH"] == before

    def test_the_platform_reload_keeps_inherited_entries_too(self, monkeypatch) -> None:
        from devenv_forge.platforms.windows import WindowsPlatform

        inherited = r"C:\inherited\from\the\shell"
        monkeypatch.setenv("PATH", inherited)
        WindowsPlatform._reload_path_from_registry()
        entries = os.environ["PATH"].split(os.pathsep)
        assert inherited in entries
        assert any("System32" in e for e in entries)


@pytestmark_windows
class TestPersistentPath:
    """A process keeps the PATH it inherited at launch.

    So a directory an installer added moments ago is invisible to this process
    while being perfectly configured for any new terminal. Confusing the two
    makes the tool offer to add a duplicate entry to the user PATH.
    """

    def test_system32_is_on_the_persistent_path(self) -> None:
        from devenv_forge.platforms.winenv import on_persistent_path

        assert on_persistent_path(os.path.join(os.environ["SystemRoot"], "System32"))

    def test_absent_directory(self) -> None:
        from devenv_forge.platforms.winenv import on_persistent_path

        assert not on_persistent_path(r"C:\definitely\not\here\xyzzy")

    def test_empty(self) -> None:
        from devenv_forge.platforms.winenv import on_persistent_path

        assert not on_persistent_path("")

    def test_ignores_the_process_environment(self, monkeypatch) -> None:
        """Must consult the registry, not os.environ."""
        from devenv_forge.platforms import winenv

        monkeypatch.setenv("PATH", r"C:\injected\only\in\this\process")
        assert not winenv.on_persistent_path(r"C:\injected\only\in\this\process")

    def test_machine_path_is_readable(self) -> None:
        from devenv_forge.platforms.winenv import read_machine_path

        assert "System32" in read_machine_path()


@pytestmark_windows
class TestWslParsing:
    def test_parses_name_and_version(self) -> None:
        from devenv_forge.platforms.windows import parse_wsl_distros

        text = (
            "  NAME            STATE           VERSION\n"
            "* Ubuntu-24.04    Stopped         2\n"
            "  podman-machine-default  Running  2\n"
        )
        assert parse_wsl_distros(text) == [
            ("Ubuntu-24.04", 2),
            ("podman-machine-default", 2),
        ]

    def test_localised_state_column_still_parses(self) -> None:
        """The STATE column is translated; NAME and VERSION are not."""
        from devenv_forge.platforms.windows import parse_wsl_distros

        text = "  NAME    ETAT    VERSION\n* Ubuntu-24.04    Arrete    2\n"
        assert parse_wsl_distros(text) == [("Ubuntu-24.04", 2)]

    def test_detects_version_1(self) -> None:
        from devenv_forge.platforms.windows import parse_wsl_distros

        text = "  NAME    STATE    VERSION\n* Legacy    Stopped    1\n"
        assert parse_wsl_distros(text) == [("Legacy", 1)]

    def test_no_distributions(self) -> None:
        from devenv_forge.platforms.windows import parse_wsl_distros

        assert parse_wsl_distros("") == []
        assert parse_wsl_distros("  NAME    STATE    VERSION\n") == []


@pytestmark_windows
class TestProviderRewrite:
    def test_creates_section_when_absent(self) -> None:
        from devenv_forge.platforms.windows import _rewrite_provider

        result = _rewrite_provider('[engine]\nfoo = "bar"\n')
        assert "[machine]" in result
        assert 'provider = "wsl"' in result
        assert 'foo = "bar"' in result  # unrelated settings survive

    def test_replaces_existing_provider(self) -> None:
        from devenv_forge.platforms.windows import _rewrite_provider

        result = _rewrite_provider('[machine]\nprovider = "hyperv"\ncpus = 4\n')
        assert 'provider = "wsl"' in result
        assert "hyperv" not in result
        assert "cpus = 4" in result

    def test_preserves_comments(self) -> None:
        from devenv_forge.platforms.windows import _rewrite_provider

        original = '# team default\n[machine]\nprovider = "hyperv"\n'
        result = _rewrite_provider(original)
        assert "# team default" in result

    def test_only_touches_the_machine_section(self) -> None:
        from devenv_forge.platforms.windows import _rewrite_provider

        original = '[network]\nprovider = "slirp4netns"\n[machine]\nprovider = "hyperv"\n'
        result = _rewrite_provider(original)
        assert 'provider = "slirp4netns"' in result
        assert "hyperv" not in result

    def test_result_is_valid_toml(self) -> None:
        import tomllib

        from devenv_forge.platforms.windows import _rewrite_provider

        result = _rewrite_provider('[machine]\nprovider = "hyperv"\ncpus = 4\n')
        parsed = tomllib.loads(result)
        assert parsed["machine"]["provider"] == "wsl"
        assert parsed["machine"]["cpus"] == 4

    def test_empty_file(self) -> None:
        import tomllib

        from devenv_forge.platforms.windows import _rewrite_provider

        parsed = tomllib.loads(_rewrite_provider(""))
        assert parsed["machine"]["provider"] == "wsl"


@pytestmark_windows
class TestExecutableSearch:
    def test_finds_in_root(self, tmp_path) -> None:
        from devenv_forge.platforms.windows import _find_executable

        (tmp_path / "podman.exe").write_text("", encoding="utf-8")
        assert _find_executable(str(tmp_path), "podman.exe") == [
            str(tmp_path / "podman.exe")
        ]

    def test_finds_in_bin_subdirectory(self, tmp_path) -> None:
        from devenv_forge.platforms.windows import _find_executable

        nested = tmp_path / "bin"
        nested.mkdir()
        (nested / "podman.exe").write_text("", encoding="utf-8")
        assert _find_executable(str(tmp_path), "podman.exe") == [
            str(nested / "podman.exe")
        ]

    def test_respects_depth_limit(self, tmp_path) -> None:
        """A wrong root must never turn into a full-disk walk."""
        from devenv_forge.platforms.windows import _find_executable

        deep = tmp_path / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "podman.exe").write_text("", encoding="utf-8")
        assert _find_executable(str(tmp_path), "podman.exe", max_depth=2) == []

    def test_missing_root(self) -> None:
        from devenv_forge.platforms.windows import _find_executable

        assert _find_executable(r"C:\no\such\dir", "podman.exe") == []

    def test_empty_root(self) -> None:
        from devenv_forge.platforms.windows import _find_executable

        assert _find_executable("", "podman.exe") == []


@pytestmark_windows
class TestPodmanDesktopIsNotTheEngine:
    def test_desktop_install_alone_does_not_satisfy_the_check(self, tmp_path) -> None:
        """The decisive regression test for this step.

        Podman Desktop is a GUI that appears in the installed-programs list under
        a name containing "podman" but ships no podman.exe. A name-based check
        would call the engine present and every later stage would fail.
        """
        from devenv_forge.platforms.windows import _find_executable

        desktop_root = tmp_path / "Podman Desktop"
        (desktop_root / "resources").mkdir(parents=True)
        (desktop_root / "Podman Desktop.exe").write_text("", encoding="utf-8")
        (desktop_root / "resources" / "app.asar").write_text("", encoding="utf-8")

        assert _find_executable(str(desktop_root), "podman.exe") == []


class TestRgbaHelper:
    def test_builds_css_rgba(self) -> None:
        from devenv_forge.ui.theme import rgba

        assert rgba("#f85149", 0.5) == "rgba(248, 81, 73, 0.500)"

    def test_accepts_missing_hash(self) -> None:
        from devenv_forge.ui.theme import rgba

        assert rgba("3fb950", 1) == "rgba(63, 185, 80, 1.000)"
