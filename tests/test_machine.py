"""Tests for the podman machine check and the create/start remedies.

The bug these guard against: podman installed with no Linux machine was
reported as a note and left there, so the environment read as usable while
every build had nowhere to run.
"""

from __future__ import annotations

import pytest

from devenv_forge.core.models import OSFamily, OSInfo, ProgressSink, Status
from devenv_forge.core.runner import CommandResult
from devenv_forge.platforms import base
from devenv_forge.platforms.base import (
    DEFAULT_MACHINE_NAME,
    MachinePlatform,
    default_machine,
    parse_machines,
)

NO_MACHINES = "[]"
ONE_STOPPED = '[{"Name":"podman-machine-default","Running":false,"Default":true}]'
ONE_RUNNING = '[{"Name":"podman-machine-default","Running":true,"Default":true}]'


class RecordingSink(ProgressSink):
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.steps: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(line)

    def step(self, message: str) -> None:
        self.steps.append(message)


class FakeMachinePlatform(MachinePlatform):
    """A machine platform with podman pinned to a known path."""

    family = OSFamily.WINDOWS

    def detect_os(self) -> OSInfo:
        return OSInfo(family=OSFamily.WINDOWS, name="Fake Windows", version="11")

    def check_podman(self):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def check_backend(self):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def podman_executable(self) -> str:
        return "/usr/bin/podman"


@pytest.fixture
def platform() -> FakeMachinePlatform:
    return FakeMachinePlatform()


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


def _listing(stdout: str, *, code: int = 0):
    """A fake `run` answering `podman machine list` with *stdout*."""

    def fake_run(argv, **kw):
        assert "machine" in argv and "list" in argv
        return CommandResult(list(argv), code, stdout, "")

    return fake_run


def _streamer(script: dict[str, tuple[list[str], str]]):
    """A fake `stream` keyed by the podman machine verb it is asked to run."""
    calls: list[list[str]] = []

    def fake_stream(argv, **kw):
        calls.append(list(argv))
        verb = argv[2] if len(argv) > 2 else ""
        lines, code = script.get(verb, ([], "0"))
        for line in lines:
            yield ("line", line)
        yield ("exit", code)

    fake_stream.calls = calls  # type: ignore[attr-defined]
    return fake_stream


class TestParsing:
    def test_reads_the_listing(self) -> None:
        assert parse_machines(ONE_RUNNING)[0]["Name"] == "podman-machine-default"

    def test_survives_junk(self) -> None:
        assert parse_machines("not json") == []
        assert parse_machines("") == []
        assert parse_machines('{"Name":"x"}') == []

    def test_drops_non_objects(self) -> None:
        assert parse_machines('[{"Name":"a"}, "b", null]') == [{"Name": "a"}]

    def test_default_machine_is_the_flagged_one(self) -> None:
        rows = [{"Name": "other"}, {"Name": "chosen", "Default": True}]
        assert default_machine(rows) == "chosen"

    def test_default_machine_falls_back_to_the_first(self) -> None:
        assert default_machine([{"Name": "only"}]) == "only"

    def test_default_machine_without_any(self) -> None:
        assert default_machine([]) == DEFAULT_MACHINE_NAME


class TestCheck:
    """A missing machine is a blocker with a fix, not a note to read."""

    def test_no_machine_is_repairable(self, platform, monkeypatch) -> None:
        monkeypatch.setattr(base, "run", _listing(NO_MACHINES))
        result = platform.check_machine()
        assert result.status is Status.REPAIRABLE
        assert result.remedy is not None
        assert result.remedy.label == "Create machine"
        assert not result.remedy.requires_elevation

    def test_the_platform_note_reaches_the_detail(self, platform, monkeypatch) -> None:
        monkeypatch.setattr(base, "run", _listing(NO_MACHINES))
        monkeypatch.setattr(FakeMachinePlatform, "machine_note", "appears in wsl -l -v")
        assert "appears in wsl -l -v" in platform.check_machine().detail

    def test_stopped_machine_offers_a_start(self, platform, monkeypatch) -> None:
        monkeypatch.setattr(base, "run", _listing(ONE_STOPPED))
        result = platform.check_machine()
        assert result.status is Status.REPAIRABLE
        assert result.remedy is not None
        assert result.remedy.label == "Start machine"

    def test_running_machine_is_ok_and_has_no_fix(self, platform, monkeypatch) -> None:
        monkeypatch.setattr(base, "run", _listing(ONE_RUNNING))
        result = platform.check_machine()
        assert result.status is Status.OK
        assert result.remedy is None

    def test_an_unreadable_listing_is_not_guessed_at(self, platform, monkeypatch) -> None:
        """Without podman's answer, offering to create one could destroy work."""
        monkeypatch.setattr(base, "run", _listing("", code=125))
        result = platform.check_machine()
        assert result.status is Status.INFO
        assert result.remedy is None

    def test_waits_for_a_missing_podman(self, platform, monkeypatch) -> None:
        from devenv_forge.core.models import CheckResult

        platform.results["podman"] = CheckResult(
            "podman", "Podman CLI", Status.MISSING, "Not installed"
        )
        result = platform.check_machine()
        assert result.status is Status.SKIPPED
        assert result.remedy is None

    def test_skips_when_podman_cannot_be_run(self, monkeypatch) -> None:
        class NoPodman(FakeMachinePlatform):
            def podman_executable(self) -> str:
                return ""

        result = NoPodman().check_machine()
        assert result.status is Status.SKIPPED


class TestCreateRemedy:
    @pytest.fixture(autouse=True)
    def _podman_env(self, monkeypatch) -> None:
        from devenv_forge.core import podman as podman_cli

        monkeypatch.setattr(podman_cli, "repair_environment", lambda: [])

    def test_creates_then_starts(self, platform, monkeypatch, sink) -> None:
        streamer = _streamer({})
        monkeypatch.setattr(base, "stream", streamer)
        monkeypatch.setattr(base, "run", _listing(ONE_RUNNING))

        outcome = platform.create_machine_remedy().action(sink)

        assert outcome.ok
        verbs = [argv[2] for argv in streamer.calls]
        assert verbs == ["init", "start"]
        assert all(DEFAULT_MACHINE_NAME in argv for argv in streamer.calls)

    def test_an_existing_machine_is_started_not_treated_as_failure(
        self, platform, monkeypatch, sink
    ) -> None:
        """podman exits non-zero for a machine that is already there."""
        streamer = _streamer({"init": (["Error: VM already exists"], "125")})
        monkeypatch.setattr(base, "stream", streamer)
        monkeypatch.setattr(base, "run", _listing(ONE_RUNNING))

        outcome = platform.create_machine_remedy().action(sink)

        assert outcome.ok
        assert [argv[2] for argv in streamer.calls] == ["init", "start"]

    def test_a_failed_init_stops_before_starting(
        self, platform, monkeypatch, sink
    ) -> None:
        streamer = _streamer({"init": (["Error: no WSL distributions"], "125")})
        monkeypatch.setattr(base, "stream", streamer)
        monkeypatch.setattr(base, "run", _listing(NO_MACHINES))

        outcome = platform.create_machine_remedy().action(sink)

        assert not outcome.ok
        assert "no WSL distributions" in outcome.message
        assert [argv[2] for argv in streamer.calls] == ["init"]

    def test_podman_output_reaches_the_log(self, platform, monkeypatch, sink) -> None:
        streamer = _streamer({"init": (["Downloading VM image", "Image resized"], "0")})
        monkeypatch.setattr(base, "stream", streamer)
        monkeypatch.setattr(base, "run", _listing(ONE_RUNNING))

        platform.create_machine_remedy().action(sink)

        assert "Downloading VM image" in sink.lines

    def test_success_is_podmans_listing_not_the_exit_code(
        self, platform, monkeypatch, sink
    ) -> None:
        """A machine that starts and then dies must not be reported as ready."""
        monkeypatch.setattr(base, "stream", _streamer({}))
        monkeypatch.setattr(base, "run", _listing(ONE_STOPPED))

        outcome = platform.create_machine_remedy().action(sink)

        assert not outcome.ok
        assert "not report it as running" in outcome.message

    def test_a_missing_podman_fails_cleanly(self, monkeypatch, sink) -> None:
        class NoPodman(FakeMachinePlatform):
            def podman_executable(self) -> str:
                return ""

        outcome = NoPodman().create_machine_remedy().action(sink)
        assert not outcome.ok
        assert "podman could not be found" in outcome.message


class TestStartRemedy:
    @pytest.fixture(autouse=True)
    def _podman_env(self, monkeypatch) -> None:
        from devenv_forge.core import podman as podman_cli

        monkeypatch.setattr(podman_cli, "repair_environment", lambda: [])

    def test_starts_the_named_machine(self, platform, monkeypatch, sink) -> None:
        streamer = _streamer({})
        monkeypatch.setattr(base, "stream", streamer)
        monkeypatch.setattr(base, "run", _listing(ONE_RUNNING))

        outcome = platform.start_machine_remedy("other-machine").action(sink)

        assert outcome.ok
        assert streamer.calls == [
            ["/usr/bin/podman", "machine", "start", "other-machine"]
        ]

    def test_an_already_running_machine_is_success(
        self, platform, monkeypatch, sink
    ) -> None:
        """podman fails a redundant start; the user's goal is met either way."""
        monkeypatch.setattr(
            base, "stream", _streamer({"start": (["Error: already running"], "125")})
        )
        monkeypatch.setattr(base, "run", _listing(ONE_RUNNING))

        assert platform.start_machine_remedy().action(sink).ok

    def test_a_real_failure_carries_podmans_words(
        self, platform, monkeypatch, sink
    ) -> None:
        monkeypatch.setattr(
            base,
            "stream",
            _streamer({"start": (["Error: WSL kernel is out of date"], "125")}),
        )
        monkeypatch.setattr(base, "run", _listing(ONE_STOPPED))

        outcome = platform.start_machine_remedy().action(sink)

        assert not outcome.ok
        assert "WSL kernel is out of date" in outcome.message


class TestWiring:
    """Both machine platforms answer with the shared implementation."""

    def test_windows_uses_it(self) -> None:
        from devenv_forge.platforms.windows import WindowsPlatform

        assert issubclass(WindowsPlatform, MachinePlatform)
        assert "wsl --list --verbose" in WindowsPlatform.machine_note

    def test_macos_uses_it(self) -> None:
        from devenv_forge.platforms.macos import MacOSPlatform

        assert issubclass(MacOSPlatform, MachinePlatform)

    def test_linux_has_no_machine(self) -> None:
        from devenv_forge.platforms.linux import LinuxPlatform

        assert not issubclass(LinuxPlatform, MachinePlatform)
        assert LinuxPlatform().check_machine().status is Status.SKIPPED

    def test_the_machine_check_can_block_the_later_stages(self) -> None:
        """It is not optional: nothing can be built without a machine."""
        assert "machine" not in MachinePlatform.optional_keys
