"""Tests for locating podman and explaining why it is unusable.

The bug these guard against: a process keeps the PATH it inherited at launch, so
an application started before podman was installed cannot see it however correct
the install is. Reporting that as "no images" sends the user looking in the
wrong place entirely.
"""

from __future__ import annotations

import os

import pytest

from devenv_forge.core import podman
from devenv_forge.core.podman import PodmanState
from devenv_forge.core.runner import CommandResult


@pytest.fixture(autouse=True)
def clear_cache():
    podman.forget()
    yield
    podman.forget()


class TestResolution:
    def test_prefers_path(self, monkeypatch) -> None:
        monkeypatch.setattr(podman, "which", lambda name: "/usr/bin/podman")
        assert podman.executable() == "/usr/bin/podman"

    def test_falls_back_to_the_install_directory(self, monkeypatch, tmp_path) -> None:
        """The regression: PATH has no podman but the install is fine."""
        binary = tmp_path / ("podman.exe" if os.name == "nt" else "podman")
        binary.write_text("", encoding="utf-8")

        monkeypatch.setattr(podman, "which", lambda name: None)
        monkeypatch.setattr(podman, "reload_path_from_registry", lambda: False)
        monkeypatch.setattr(podman, "_known_dirs", lambda: [str(tmp_path)])

        assert podman.executable() == str(binary)

    def test_adds_the_directory_to_path(self, monkeypatch, tmp_path) -> None:
        """podman shells out to helpers, so its directory has to be reachable."""
        binary = tmp_path / ("podman.exe" if os.name == "nt" else "podman")
        binary.write_text("", encoding="utf-8")
        monkeypatch.setattr(podman, "which", lambda name: None)
        monkeypatch.setattr(podman, "reload_path_from_registry", lambda: False)
        monkeypatch.setattr(podman, "_known_dirs", lambda: [str(tmp_path)])
        monkeypatch.setenv("PATH", "")

        podman.executable()
        assert str(tmp_path) in os.environ["PATH"]

    def test_retries_after_refreshing_path(self, monkeypatch, tmp_path) -> None:
        """An install that happened after launch appears once PATH is re-read."""
        calls = {"n": 0}

        def fake_which(_name):
            calls["n"] += 1
            return "/found/podman" if calls["n"] > 1 else None

        monkeypatch.setattr(podman, "which", fake_which)
        monkeypatch.setattr(podman, "reload_path_from_registry", lambda: True)
        assert podman.executable() == "/found/podman"

    def test_returns_empty_when_truly_absent(self, monkeypatch) -> None:
        monkeypatch.setattr(podman, "which", lambda name: None)
        monkeypatch.setattr(podman, "reload_path_from_registry", lambda: False)
        monkeypatch.setattr(podman, "_known_dirs", lambda: [])
        assert podman.executable() == ""

    def test_result_is_cached(self, monkeypatch) -> None:
        calls = {"n": 0}

        def fake_which(_name):
            calls["n"] += 1
            return "/usr/bin/podman"

        monkeypatch.setattr(podman, "which", fake_which)
        podman.executable()
        podman.executable()
        assert calls["n"] == 1


def _result(argv, code=0, out="", err=""):
    return CommandResult(list(argv), code, out, err)


class TestStatus:
    def test_not_installed_explains_the_stale_path(self, monkeypatch) -> None:
        monkeypatch.setattr(podman, "executable", lambda: "")
        state = podman.status()
        assert state.state is PodmanState.NOT_INSTALLED
        assert not state.ready
        assert "restart" in state.hint.lower()

    def test_ready(self, monkeypatch) -> None:
        monkeypatch.setattr(podman, "executable", lambda: "/usr/bin/podman")
        monkeypatch.setattr(
            podman, "run", lambda argv, **kw: _result(argv, 0, "podman version 5.8.3")
        )
        state = podman.status()
        assert state.ready
        assert "5.8.3" in state.version

    def test_unknown_machine_state_is_not_guessed(self, monkeypatch) -> None:
        """When even 'machine list' fails, report podman's words, not a guess.

        Treating every "cannot connect" as a stopped machine is what produced a
        Start button for a machine that was already running.
        """
        monkeypatch.setattr(podman, "executable", lambda: "/usr/bin/podman")
        monkeypatch.setattr(podman, "repair_environment", lambda: [])

        def fake_run(argv, **kw):
            if "--version" in argv:
                return _result(argv, 0, "podman version 5.8.3")
            return _result(
                argv, 125, "", "Cannot connect to Podman. Is the podman machine running?"
            )

        monkeypatch.setattr(podman, "run", fake_run)
        state = podman.status()
        assert state.state is PodmanState.ERROR
        assert "Cannot connect" in state.detail
        assert state.state is not PodmanState.MACHINE_STOPPED

    def test_other_failures_are_reported_verbatim(self, monkeypatch) -> None:
        monkeypatch.setattr(podman, "executable", lambda: "/usr/bin/podman")

        def fake_run(argv, **kw):
            if "--version" in argv:
                return _result(argv, 0, "podman version 5.8.3")
            return _result(argv, 1, "", "short name resolution failed")

        monkeypatch.setattr(podman, "run", fake_run)
        state = podman.status()
        assert state.state is PodmanState.ERROR
        assert "short name" in state.detail

    def test_broken_binary(self, monkeypatch) -> None:
        monkeypatch.setattr(podman, "executable", lambda: "/usr/bin/podman")
        monkeypatch.setattr(podman, "run", lambda argv, **kw: _result(argv, 1, "", "boom"))
        assert podman.status().state is PodmanState.ERROR


class TestMachineCommand:
    def test_start_command(self, monkeypatch) -> None:
        monkeypatch.setattr(podman, "executable", lambda: "/usr/bin/podman")
        assert podman.machine_start_command() == [
            "/usr/bin/podman",
            "machine",
            "start",
        ]


def _fake(**responses):
    """Route fake podman calls by subcommand."""

    def fake_run(argv, **kw):
        joined = " ".join(argv)
        for needle, (code, out, err) in responses.items():
            if needle.replace("_", " ") in joined:
                return _result(argv, code, out, err)
        return _result(argv, 0, "")

    return fake_run


CANNOT_CONNECT = (
    "Cannot connect to Podman. Please verify your connection to the Linux system "
    "using `podman system connection list`, or try `podman machine init` and "
    "`podman machine start` to manage a new Linux VM"
)


RUNNING_NO_CONNECTION = {
    "--version": (0, "podman version 5.8.3", ""),
    "images": (125, "", CANNOT_CONNECT),
    "machine list": (0, '[{"Name":"podman-machine-default","Running":true}]', ""),
    "connection list": (0, "[]", ""),
}


class TestConnectionDiagnosis:
    """'Cannot connect' has several causes whose fixes do not overlap."""

    @pytest.fixture(autouse=True)
    def _binary(self, monkeypatch):
        monkeypatch.setattr(podman, "executable", lambda: "/usr/bin/podman")
        monkeypatch.setattr(podman, "repair_environment", lambda: [])
        monkeypatch.setattr(podman, "stranded_connection_files", lambda: [])

    def test_running_machine_without_connection(self, monkeypatch) -> None:
        """Machine up, no connection, and starting it cannot help."""
        monkeypatch.setattr(podman, "run", _fake(**RUNNING_NO_CONNECTION))
        state = podman.status()
        assert state.state is PodmanState.NO_CONNECTION
        assert "already running" in state.detail

    def test_it_does_not_suggest_starting_a_running_machine(self, monkeypatch) -> None:
        monkeypatch.setattr(podman, "run", _fake(**RUNNING_NO_CONNECTION))
        assert "machine start" not in podman.status().hint

    def test_stranded_file_in_a_packaged_app_is_named(self, monkeypatch) -> None:
        """The cause actually found: init run from a terminal inside a packaged app.

        Windows redirected the write into that app's private storage, so the
        machine exists in the real profile while its connections do not.
        """
        source = r"C:\Users\me\AppData\Local\Packages\Some.App_abc\LocalCache\Roaming\containers\podman-connections.json"
        monkeypatch.setattr(
            podman, "stranded_connection_files", lambda: [("Some.App_abc", source)]
        )
        monkeypatch.setattr(podman, "run", _fake(**RUNNING_NO_CONNECTION))
        state = podman.status()
        assert state.state is PodmanState.NO_CONNECTION
        assert "Some.App_abc" in state.detail
        assert "Copy-Item" in state.hint
        assert source in state.hint
        # The copy has to run outside that app, or it is redirected back in.
        assert "ordinary terminal" in state.hint

    def test_does_not_blame_appdata_when_nothing_was_restored(self, monkeypatch) -> None:
        """A missing APPDATA was the first theory, and it was wrong."""
        monkeypatch.setattr(podman, "run", _fake(**RUNNING_NO_CONNECTION))
        state = podman.status()
        assert "APPDATA" not in state.detail
        assert "APPDATA" not in state.hint

    def test_genuinely_stopped_machine(self, monkeypatch) -> None:
        monkeypatch.setattr(
            podman,
            "run",
            _fake(
                **{
                    "--version": (0, "podman version 5.8.3", ""),
                    "images": (125, "", CANNOT_CONNECT),
                    "machine list": (0, '[{"Running":false}]', ""),
                    "connection list": (0, '[{"Name":"podman-machine-default"}]', ""),
                }
            ),
        )
        state = podman.status()
        assert state.state is PodmanState.MACHINE_STOPPED
        assert "machine start" in state.hint

    def test_restored_variables_are_mentioned(self, monkeypatch) -> None:
        monkeypatch.setattr(podman, "repair_environment", lambda: ["APPDATA"])
        monkeypatch.setattr(podman, "run", _fake(**RUNNING_NO_CONNECTION))
        state = podman.status()
        assert "APPDATA" in state.detail
        assert "Refresh" in state.hint


class TestStrandedFiles:
    def test_finds_a_file_in_package_storage(self, monkeypatch, tmp_path) -> None:
        target = tmp_path / "Packages" / "Vendor.App_x1" / "LocalCache" / "Roaming" / "containers"
        target.mkdir(parents=True)
        (target / "podman-connections.json").write_text("{}", encoding="utf-8")
        (tmp_path / "Packages" / "Other.App_y2").mkdir()
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(podman.sys, "platform", "win32")

        found = podman.stranded_connection_files()
        assert [name for name, _path in found] == ["Vendor.App_x1"]

    def test_nothing_without_a_packages_folder(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(podman.sys, "platform", "win32")
        assert podman.stranded_connection_files() == []

    def test_restore_command_targets_the_real_profile(self) -> None:
        source = r"C:\src\podman-connections.json"
        command = podman.restore_command(source)
        assert source in command
        assert "$env:APPDATA" in command
        assert "Copy-Item" in command


@pytest.mark.skipif(os.name != "nt", reason="Windows known folders")
class TestRepairEnvironment:
    def test_restores_a_missing_appdata(self, monkeypatch) -> None:
        """The actual failure: a shell with APPDATA unset."""
        expected = podman._known_folder(podman._FOLDER_IDS["APPDATA"])
        assert expected, "known folder lookup should work on Windows"

        monkeypatch.delenv("APPDATA", raising=False)
        restored = podman.repair_environment()

        assert "APPDATA" in restored
        assert os.environ["APPDATA"] == expected

    def test_does_not_overwrite_an_existing_value(self, monkeypatch) -> None:
        monkeypatch.setenv("APPDATA", r"C:\custom\roaming")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\custom\local")
        assert podman.repair_environment() == []
        assert os.environ["APPDATA"] == r"C:\custom\roaming"

    def test_known_folder_does_not_depend_on_the_variable(self, monkeypatch) -> None:
        monkeypatch.delenv("APPDATA", raising=False)
        assert podman._known_folder(podman._FOLDER_IDS["APPDATA"]).endswith("Roaming")


class TestInstallInvalidatesTheCache:
    """A fix that installs podman has to retract the lookup that missed it.

    The regression: the first preflight run caches "no podman" on behalf of the
    VS Code checks. Fix installs it. The automatic re-check reads the same
    cached miss, so those checks still report "Waiting on the podman CLI" for a
    podman the tool had just installed, until the application was restarted.
    """

    @pytest.fixture
    def installed(self, monkeypatch, tmp_path):
        """A podman that does not exist yet, in a directory the lookup knows.

        Yields a callable that brings it into being, as the installer would.
        """
        binary = tmp_path / ("podman.exe" if os.name == "nt" else "podman")
        monkeypatch.setattr(podman, "which", lambda _name: None)
        monkeypatch.setattr(podman, "reload_path_from_registry", lambda: False)
        monkeypatch.setattr(podman, "_known_dirs", lambda: [str(tmp_path)])

        # What the VS Code checks do on a machine with no podman, and cache.
        assert podman.executable() == ""

        def install() -> str:
            binary.write_text("", encoding="utf-8")
            return str(binary)

        return install

    def test_a_check_after_a_winget_install_sees_podman(
        self, monkeypatch, installed
    ) -> None:
        from devenv_forge.core.models import ProgressSink
        from devenv_forge.platforms import windows as win
        from devenv_forge.platforms.base import PodmanProbe

        path = installed()
        monkeypatch.setattr(
            win, "which", lambda name: path if name == "podman" else "winget.exe"
        )
        monkeypatch.setattr(
            win.winenv,
            "run_elevated_script",
            lambda *a, **kw: win.winenv.ElevatedResult(True, 0),
        )
        monkeypatch.setattr(
            win.WindowsPlatform, "_reload_path_from_registry", staticmethod(lambda: None)
        )
        monkeypatch.setattr(
            win, "probe_podman", lambda p: PodmanProbe(path=p, version="5.8.3", works=True)
        )

        outcome = win.WindowsPlatform()._winget_run(
            ProgressSink(), "install", win.WINGET_PODMAN_ID
        )

        assert outcome.ok
        assert podman.executable() == path

    def test_a_check_after_the_path_repair_sees_podman(
        self, monkeypatch, installed
    ) -> None:
        """Repairing PATH moves podman into view without installing anything."""
        from devenv_forge.core.models import ProgressSink
        from devenv_forge.platforms import windows as win

        path = installed()
        monkeypatch.setattr(
            win.winenv,
            "add_to_user_path",
            lambda directory, backup_dir: win.winenv.PathWriteResult(True, "added"),
        )

        remedy = win.WindowsPlatform()._add_to_path_remedy(os.path.dirname(path))
        outcome = remedy.action(ProgressSink())

        assert outcome.ok
        assert podman.executable() == path

    def test_a_check_after_the_homebrew_install_sees_podman(
        self, monkeypatch, installed
    ) -> None:
        from devenv_forge.core.models import ProgressSink
        from devenv_forge.platforms import macos

        path = installed()
        monkeypatch.setattr(macos, "which", lambda _name: "/opt/homebrew/bin/brew")
        monkeypatch.setattr(macos, "stream", lambda *a, **kw: iter([("exit", "0")]))

        outcome = macos.MacOSPlatform()._brew_install_remedy().action(ProgressSink())

        assert outcome.ok
        assert podman.executable() == path

    def test_a_check_after_the_package_manager_install_sees_podman(
        self, monkeypatch, installed
    ) -> None:
        from devenv_forge.core.models import ProgressSink
        from devenv_forge.platforms import linux

        path = installed()
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
        monkeypatch.setattr(linux, "stream", lambda *a, **kw: iter([("exit", "0")]))

        platform = linux.LinuxPlatform()
        platform._manager = ("apt-get", ["apt-get", "install", "-y", "podman"], "APT")
        outcome = platform._install_remedy().action(ProgressSink())

        assert outcome.ok
        assert podman.executable() == path

    def test_the_dev_containers_check_stops_waiting_after_the_install(
        self, monkeypatch, tmp_path, installed
    ) -> None:
        """The reported symptom, end to end.

        The check asks for podman's path, is told there is none, and skips. The
        install happens. Asked again, it has to get the new path -- before the
        fix it got the cached miss and went on waiting for a podman that was by
        then installed and on PATH.
        """
        from devenv_forge.core import vscode
        from devenv_forge.core.models import ProgressSink, Status
        from devenv_forge.platforms import windows as win
        from devenv_forge.platforms.base import PodmanProbe

        monkeypatch.setattr(vscode, "find_cli", lambda: "code")
        monkeypatch.setattr(
            vscode, "user_settings_path", lambda: tmp_path / "settings.json"
        )
        platform = win.WindowsPlatform()

        waiting = platform.check_devcontainers_engine()
        assert waiting.status is Status.SKIPPED
        assert waiting.summary == "Waiting on the podman CLI"

        path = installed()
        monkeypatch.setattr(
            win, "which", lambda name: path if name == "podman" else "winget.exe"
        )
        monkeypatch.setattr(
            win.winenv,
            "run_elevated_script",
            lambda *a, **kw: win.winenv.ElevatedResult(True, 0),
        )
        monkeypatch.setattr(
            win.WindowsPlatform, "_reload_path_from_registry", staticmethod(lambda: None)
        )
        monkeypatch.setattr(
            win, "probe_podman", lambda p: PodmanProbe(path=p, version="5.8.3", works=True)
        )
        assert platform._winget_run(
            ProgressSink(), "install", win.WINGET_PODMAN_ID
        ).ok

        settled = platform.check_devcontainers_engine()
        assert settled.status is Status.REPAIRABLE
        assert settled.remedy is not None
        assert path in settled.remedy.description

    def test_a_failed_install_is_not_a_reason_to_re_resolve(self, monkeypatch) -> None:
        """Only success retracts it; a declined UAC prompt changed nothing."""
        from devenv_forge.core.models import ProgressSink
        from devenv_forge.platforms import windows as win

        calls = {"n": 0}

        def counting_forget() -> None:
            calls["n"] += 1

        monkeypatch.setattr(podman, "forget", counting_forget)
        monkeypatch.setattr(win, "which", lambda _name: "winget.exe")
        monkeypatch.setattr(
            win.winenv,
            "run_elevated_script",
            lambda *a, **kw: win.winenv.ElevatedResult(False, None, cancelled=True),
        )

        outcome = win.WindowsPlatform()._winget_run(
            ProgressSink(), "install", win.WINGET_PODMAN_ID
        )

        assert not outcome.ok
        assert calls["n"] == 0
