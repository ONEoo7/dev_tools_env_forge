"""Tests for pointing VS Code's Dev Containers extension at podman.

The settings editor is tested hardest. Corrupting someone's VS Code settings, or
silently dropping their comments, would be far worse than not configuring
podman at all.
"""

from __future__ import annotations

import json
import os

import pytest

from devenv_forge.core import vscode
from devenv_forge.core.models import Status

KEY = vscode.DOCKER_PATH_KEY


class TestStripJsonc:
    def test_line_comments(self) -> None:
        text = '{\n  // editor font\n  "editor.fontSize": 14\n}'
        assert json.loads(vscode.strip_jsonc(text)) == {"editor.fontSize": 14}

    def test_block_comments(self) -> None:
        text = '{ /* one */ "a": 1, /* two\n spanning */ "b": 2 }'
        assert json.loads(vscode.strip_jsonc(text)) == {"a": 1, "b": 2}

    def test_slashes_inside_strings_survive(self) -> None:
        """A naive stripper cuts URLs in half at the '//'."""
        text = '{ "http.proxy": "http://proxy.example.com:8080" }'
        assert json.loads(vscode.strip_jsonc(text))["http.proxy"] == (
            "http://proxy.example.com:8080"
        )

    def test_comment_markers_inside_strings_survive(self) -> None:
        text = '{ "pattern": "/* not a comment */", "x": "// nor this" }'
        parsed = json.loads(vscode.strip_jsonc(text))
        assert parsed == {"pattern": "/* not a comment */", "x": "// nor this"}

    def test_escaped_quotes(self) -> None:
        text = '{ "a": "say \\"hi\\" // still string", "b": 1 }'
        assert json.loads(vscode.strip_jsonc(text))["b"] == 1

    def test_trailing_commas(self) -> None:
        text = '{ "a": [1, 2, ], "b": { "c": 3, }, }'
        assert json.loads(vscode.strip_jsonc(text)) == {"a": [1, 2], "b": {"c": 3}}

    def test_comma_inside_string_before_brace_is_kept(self) -> None:
        text = '{ "a": ",}" }'
        assert json.loads(vscode.strip_jsonc(text)) == {"a": ",}"}


class TestLoadJsonc:
    def test_empty_file_is_empty_object(self) -> None:
        assert vscode.load_jsonc("") == {}
        assert vscode.load_jsonc("   \n") == {}

    def test_rejects_non_object(self) -> None:
        with pytest.raises(ValueError):
            vscode.load_jsonc("[1, 2]")


class TestSetTopLevel:
    def test_empty_file(self) -> None:
        result = vscode.set_top_level("", KEY, "podman")
        assert vscode.load_jsonc(result) == {KEY: "podman"}

    def test_empty_object(self) -> None:
        result = vscode.set_top_level("{}", KEY, "podman")
        assert vscode.load_jsonc(result) == {KEY: "podman"}

    def test_inserts_into_existing_settings(self) -> None:
        text = '{\n    "editor.fontSize": 14,\n    "files.autoSave": "on"\n}\n'
        result = vscode.set_top_level(text, KEY, "podman")
        assert vscode.load_jsonc(result) == {
            "editor.fontSize": 14,
            "files.autoSave": "on",
            KEY: "podman",
        }

    def test_replaces_an_existing_value_in_place(self) -> None:
        text = '{\n    "a": 1,\n    "dev.containers.dockerPath": "docker",\n    "b": 2\n}\n'
        result = vscode.set_top_level(text, KEY, "podman")
        assert vscode.load_jsonc(result) == {"a": 1, KEY: "podman", "b": 2}
        assert result.count(KEY) == 1

    def test_preserves_comments(self) -> None:
        """The reason for splicing text instead of re-serialising the file."""
        text = (
            "{\n"
            "    // keep this note\n"
            '    "editor.fontSize": 14, /* and this */\n'
            '    "http.proxy": "http://proxy:8080"\n'
            "}\n"
        )
        result = vscode.set_top_level(text, KEY, "podman")
        assert "// keep this note" in result
        assert "/* and this */" in result
        assert vscode.load_jsonc(result)["http.proxy"] == "http://proxy:8080"

    def test_preserves_trailing_commas_and_still_parses(self) -> None:
        text = '{\n    "a": 1,\n}\n'
        result = vscode.set_top_level(text, KEY, "podman")
        assert vscode.load_jsonc(result) == {"a": 1, KEY: "podman"}

    def test_matches_only_the_top_level_key(self) -> None:
        """A nested object with the same key name must not be edited."""
        text = '{\n    "[python]": { "dev.containers.dockerPath": "nested" }\n}\n'
        result = vscode.set_top_level(text, KEY, "podman")
        parsed = vscode.load_jsonc(result)
        assert parsed[KEY] == "podman"
        assert parsed["[python]"][KEY] == "nested"

    def test_replaces_a_non_string_value(self) -> None:
        text = '{ "dev.containers.dockerPath": null, "z": [1, {"q": 2}] }'
        result = vscode.set_top_level(text, KEY, "podman")
        assert vscode.load_jsonc(result) == {KEY: "podman", "z": [1, {"q": 2}]}

    def test_windows_path_is_escaped(self) -> None:
        path = r"C:\Program Files\RedHat\Podman\podman.exe"
        result = vscode.set_top_level("{}", KEY, path)
        assert vscode.load_jsonc(result)[KEY] == path
        assert "\\\\Program Files" in result

    def test_matches_the_file_indentation(self) -> None:
        text = '{\n\t"a": 1\n}\n'
        result = vscode.set_top_level(text, KEY, "podman")
        assert f'\t"{KEY}"' in result

    def test_leading_comment_before_the_object(self) -> None:
        text = '// header\n{\n    "a": 1\n}\n'
        result = vscode.set_top_level(text, KEY, "podman")
        assert result.startswith("// header")
        assert vscode.load_jsonc(result) == {KEY: "podman", "a": 1}


class TestWriteDockerPath:
    def test_writes_and_backs_up(self, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text('{\n    "editor.fontSize": 14\n}\n', encoding="utf-8")
        outcome = vscode.write_docker_path(
            "podman", settings, tmp_path / "backups", check_redirection=False
        )
        assert outcome.ok
        assert outcome.backup is not None and outcome.backup.is_file()
        assert json.loads(settings.read_text(encoding="utf-8")) == {
            "editor.fontSize": 14,
            KEY: "podman",
        }

    def test_creates_a_missing_settings_file(self, tmp_path) -> None:
        settings = tmp_path / "Code" / "User" / "settings.json"
        outcome = vscode.write_docker_path("podman", settings, None, check_redirection=False)
        assert outcome.ok
        assert json.loads(settings.read_text(encoding="utf-8")) == {KEY: "podman"}

    def test_is_idempotent(self, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        for _ in range(3):
            vscode.write_docker_path("podman", settings, None, check_redirection=False)
        assert settings.read_text(encoding="utf-8").count(KEY) == 1

    def test_refuses_an_unparseable_file(self, tmp_path) -> None:
        """Guessing at a broken file's structure could make it worse."""
        settings = tmp_path / "settings.json"
        broken = '{ "a": 1 "b": 2 }'
        settings.write_text(broken, encoding="utf-8")
        outcome = vscode.write_docker_path("podman", settings, None, check_redirection=False)
        assert not outcome.ok
        assert settings.read_text(encoding="utf-8") == broken

    def test_handles_a_byte_order_mark(self, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_bytes(b'\xef\xbb\xbf{ "a": 1 }')
        outcome = vscode.write_docker_path("podman", settings, None, check_redirection=False)
        assert outcome.ok
        assert vscode.load_jsonc(settings.read_text(encoding="utf-8-sig"))[KEY] == "podman"

    def test_refuses_under_package_redirection(self, tmp_path, monkeypatch) -> None:
        """Writing from inside a packaged app lands where VS Code never looks."""
        settings = tmp_path / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(vscode, "redirected_into_package", lambda: "Some.App_abc")
        outcome = vscode.write_docker_path("podman", settings, None)
        assert not outcome.ok
        assert "Some.App_abc" in outcome.message
        assert settings.read_text(encoding="utf-8") == "{}"


@pytest.mark.skipif(os.name != "nt", reason="package redirection is Windows-only")
class TestRedirectionProbe:
    def test_no_false_positive_without_a_package_copy(self, tmp_path, monkeypatch) -> None:
        roaming = tmp_path / "Roaming"
        local = tmp_path / "Local"
        (local / "Packages" / "Unrelated.App_1").mkdir(parents=True)
        roaming.mkdir()
        monkeypatch.setenv("APPDATA", str(roaming))
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        assert vscode.redirected_into_package() == ""

    def test_probe_is_cleaned_up(self, tmp_path, monkeypatch) -> None:
        roaming = tmp_path / "Roaming"
        roaming.mkdir()
        (tmp_path / "Local").mkdir()
        monkeypatch.setenv("APPDATA", str(roaming))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        vscode.redirected_into_package()
        assert list(roaming.iterdir()) == []


class TestReadDockerPath:
    def test_unset(self, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text('{ "a": 1 }', encoding="utf-8")
        state = vscode.read_docker_path(settings)
        assert state.current is None
        assert not state.points_at_podman()

    def test_recognises_a_full_windows_path(self, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        value = r"C:\Program Files\RedHat\Podman\podman.exe"
        settings.write_text(json.dumps({KEY: value}), encoding="utf-8")
        assert vscode.read_docker_path(settings).points_at_podman()

    def test_docker_is_not_podman(self, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({KEY: "docker"}), encoding="utf-8")
        assert not vscode.read_docker_path(settings).points_at_podman()

    def test_legacy_key_is_reported(self, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps({vscode.LEGACY_DOCKER_PATH_KEY: "docker"}), encoding="utf-8"
        )
        assert vscode.read_docker_path(settings).legacy == "docker"

    def test_reports_a_parse_error(self, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("{ not json", encoding="utf-8")
        assert vscode.read_docker_path(settings).error


class _FakePlatform:
    """Borrow the shared checks without constructing a real platform."""

    from devenv_forge.platforms.base import Platform

    check_vscode_extension = Platform.check_vscode_extension
    check_devcontainers_engine = Platform.check_devcontainers_engine
    _install_devcontainers_remedy = Platform._install_devcontainers_remedy
    _configure_devcontainers_remedy = Platform._configure_devcontainers_remedy


def _result(argv, code=0, out=""):
    from devenv_forge.core.runner import CommandResult

    return CommandResult(list(argv), code, out, "")


class TestPreflightChecks:
    def test_skipped_without_vscode(self, monkeypatch) -> None:
        monkeypatch.setattr(vscode, "find_cli", lambda: "")
        assert _FakePlatform().check_vscode_extension().status is Status.SKIPPED
        assert _FakePlatform().check_devcontainers_engine().status is Status.SKIPPED

    def test_extension_installed(self, monkeypatch) -> None:
        from devenv_forge.platforms import base

        monkeypatch.setattr(vscode, "find_cli", lambda: "code")
        monkeypatch.setattr(
            base,
            "run",
            lambda argv, **kw: _result(argv, 0, "ms-vscode-remote.remote-containers@0.469.0\n"),
        )
        result = _FakePlatform().check_vscode_extension()
        assert result.status is Status.OK
        assert "0.469.0" in result.summary

    def test_extension_missing_offers_install(self, monkeypatch) -> None:
        from devenv_forge.platforms import base

        monkeypatch.setattr(vscode, "find_cli", lambda: "code")
        monkeypatch.setattr(
            base, "run", lambda argv, **kw: _result(argv, 0, "ms-python.python@2026.1.0\n")
        )
        result = _FakePlatform().check_vscode_extension()
        assert result.status is Status.MISSING
        assert result.remedy is not None

    def test_engine_already_podman(self, monkeypatch, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({KEY: "podman"}), encoding="utf-8")
        monkeypatch.setattr(vscode, "find_cli", lambda: "code")
        monkeypatch.setattr(vscode, "user_settings_path", lambda: settings)
        assert _FakePlatform().check_devcontainers_engine().status is Status.OK

    def test_engine_on_docker_offers_the_fix(self, monkeypatch, tmp_path) -> None:
        from devenv_forge.core import podman

        settings = tmp_path / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(vscode, "find_cli", lambda: "code")
        monkeypatch.setattr(vscode, "user_settings_path", lambda: settings)
        monkeypatch.setattr(podman, "executable", lambda: str(tmp_path / "podman.exe"))
        result = _FakePlatform().check_devcontainers_engine()
        assert result.status is Status.REPAIRABLE
        assert result.remedy is not None
        assert "user settings" in result.detail

    def test_broken_settings_are_not_repaired(self, monkeypatch, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("{ broken", encoding="utf-8")
        monkeypatch.setattr(vscode, "find_cli", lambda: "code")
        monkeypatch.setattr(vscode, "user_settings_path", lambda: settings)
        result = _FakePlatform().check_devcontainers_engine()
        assert result.status is Status.FAILED
        assert result.remedy is None


class TestContainerTools:
    """Container Tools owns the CONTAINERS view and reads its own settings."""

    PATH = r"C:\Program Files\RedHat\Podman\podman.exe"

    def test_command_is_the_raw_path(self) -> None:
        """Not quoted, despite the setting's description.

        The extension's process library adds quotes itself when it runs through
        a shell. When it spawns podman directly, pre-quoted input is read as a
        file name containing quote characters and fails to start.
        """
        assert vscode.container_tools_command(self.PATH) == self.PATH

    def test_existing_quotes_are_stripped(self) -> None:
        assert vscode.container_tools_command(f'"{self.PATH}"') == self.PATH

    def test_is_pre_quoted(self) -> None:
        assert vscode.is_pre_quoted(f'"{self.PATH}"')
        assert not vscode.is_pre_quoted(self.PATH)
        assert not vscode.is_pre_quoted(None)

    def test_write_settings_sets_both_keys_together(self, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text('{\n    // mine\n    "editor.fontSize": 14\n}\n', encoding="utf-8")
        outcome = vscode.write_settings(
            {
                vscode.CONTAINER_CLIENT_KEY: vscode.PODMAN_CLIENT_ID,
                vscode.CONTAINER_COMMAND_KEY: self.PATH,
            },
            settings,
            None,
            check_redirection=False,
        )
        assert outcome.ok
        text = settings.read_text(encoding="utf-8")
        parsed = vscode.load_jsonc(text)
        assert parsed[vscode.CONTAINER_CLIENT_KEY] == vscode.PODMAN_CLIENT_ID
        assert parsed[vscode.CONTAINER_COMMAND_KEY] == self.PATH
        assert "// mine" in text
        assert parsed["editor.fontSize"] == 14

    def test_reads_state(self, tmp_path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps({vscode.CONTAINER_CLIENT_KEY: vscode.PODMAN_CLIENT_ID}),
            encoding="utf-8",
        )
        state = vscode.read_container_tools(settings)
        assert state.uses_podman()
        assert state.command is None


class TestContainerToolsCheck:
    PATH = r"C:\Program Files\RedHat\Podman\podman.exe"

    @pytest.fixture
    def platform(self):
        from devenv_forge.platforms.base import Platform

        class Probe:
            check_container_tools_engine = Platform.check_container_tools_engine
            _configure_container_tools_remedy = Platform._configure_container_tools_remedy

        return Probe()

    @pytest.fixture
    def installed(self, monkeypatch, tmp_path):
        from devenv_forge.core import podman

        settings = tmp_path / "settings.json"
        monkeypatch.setattr(vscode, "find_cli", lambda: "code")
        monkeypatch.setattr(
            vscode, "installed_extensions", lambda cli: {vscode.CONTAINER_TOOLS_ID}
        )
        monkeypatch.setattr(vscode, "user_settings_path", lambda: settings)
        monkeypatch.setattr(podman, "executable", lambda: self.PATH)
        return settings

    def test_skipped_without_vscode(self, platform, monkeypatch) -> None:
        monkeypatch.setattr(vscode, "find_cli", lambda: "")
        assert platform.check_container_tools_engine().status is Status.SKIPPED

    def test_skipped_when_the_extension_is_absent(self, platform, monkeypatch) -> None:
        """Nothing to configure, and installing it was not asked for."""
        monkeypatch.setattr(vscode, "find_cli", lambda: "code")
        monkeypatch.setattr(vscode, "installed_extensions", lambda cli: {"ms-python.python"})
        result = platform.check_container_tools_engine()
        assert result.status is Status.SKIPPED
        assert result.remedy is None

    def test_default_docker_client_needs_repair(self, platform, installed) -> None:
        """The state in the screenshot: 'Failed to connect. Is Docker installed?'."""
        installed.write_text("{}", encoding="utf-8")
        result = platform.check_container_tools_engine()
        assert result.status is Status.REPAIRABLE
        assert "Docker" in result.summary
        assert "restart" in result.detail.lower()

    def test_podman_client_with_raw_path_is_ok(self, platform, installed) -> None:
        installed.write_text(
            json.dumps(
                {
                    vscode.CONTAINER_CLIENT_KEY: vscode.PODMAN_CLIENT_ID,
                    vscode.CONTAINER_COMMAND_KEY: self.PATH,
                }
            ),
            encoding="utf-8",
        )
        assert platform.check_container_tools_engine().status is Status.OK

    def test_quoted_command_is_flagged(self, platform, installed) -> None:
        """What someone following the setting's description ends up with."""
        installed.write_text(
            json.dumps(
                {
                    vscode.CONTAINER_CLIENT_KEY: vscode.PODMAN_CLIENT_ID,
                    vscode.CONTAINER_COMMAND_KEY: f'"{self.PATH}"',
                }
            ),
            encoding="utf-8",
        )
        result = platform.check_container_tools_engine()
        assert result.status is Status.REPAIRABLE
        assert "quoted" in result.summary

    def test_remedy_writes_the_unquoted_path(self, platform, installed, monkeypatch) -> None:
        from devenv_forge.core import paths
        from devenv_forge.core.models import ProgressSink

        installed.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(vscode, "redirected_into_package", lambda: "")
        monkeypatch.setattr(paths, "backup_dir", lambda: installed.parent / "backups")

        result = platform.check_container_tools_engine()
        outcome = result.remedy.action(ProgressSink())
        assert outcome.ok
        assert outcome.needs_restart
        parsed = json.loads(installed.read_text(encoding="utf-8"))
        assert parsed[vscode.CONTAINER_CLIENT_KEY] == vscode.PODMAN_CLIENT_ID
        assert parsed[vscode.CONTAINER_COMMAND_KEY] == self.PATH
        assert not parsed[vscode.CONTAINER_COMMAND_KEY].startswith('"')

    def test_is_optional(self) -> None:
        from devenv_forge.platforms.base import Platform

        assert "containertools" in Platform.optional_keys
