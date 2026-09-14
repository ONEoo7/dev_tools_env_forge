"""The platform contract the UI talks to.

Each supported OS supplies a :class:`Platform` subclass. The UI only ever sees
:class:`~devenv_forge.core.models.CheckResult` values, so adding an OS means
adding a subclass and nothing else.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..core.models import (
    CheckResult,
    OSFamily,
    OSInfo,
    ProgressSink,
    Remedy,
    RemedyOutcome,
    Status,
)
from ..core.runner import CommandResult, run, stream, which

#: Matches the first version-like token in `podman --version` output, e.g.
#: "podman version 5.8.3" or "podman.exe version 5.8.3-dev".
VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

#: Minimum podman that supports the WSL machine provider we rely on.
MIN_PODMAN = (4, 0)


@dataclass(slots=True)
class PodmanProbe:
    """What a candidate podman executable turned out to be."""

    path: str
    version: str = ""
    version_tuple: tuple[int, ...] = ()
    works: bool = False
    error: str = ""

    @property
    def too_old(self) -> bool:
        return bool(self.version_tuple) and self.version_tuple[:2] < MIN_PODMAN


def probe_podman(executable: str, timeout: float = 20.0) -> PodmanProbe:
    """Run `podman --version` and interpret the result."""
    probe = PodmanProbe(path=executable)
    result: CommandResult = run([executable, "--version"], timeout=timeout)
    if result.launch_error:
        probe.error = result.launch_error
        return probe
    if result.timed_out:
        probe.error = "podman --version timed out"
        return probe
    text = result.output
    if result.returncode != 0:
        probe.error = text or f"exit code {result.returncode}"
        return probe
    match = VERSION_RE.search(text)
    if match:
        probe.version = match.group(0)
        probe.version_tuple = tuple(
            int(g) for g in match.groups(default="0") if g is not None
        )
    probe.works = True
    return probe


#: One ordered preflight step: a stable key, a display title and the probe.
CheckStep = tuple[str, str, Callable[[], CheckResult]]


class Platform(ABC):
    """Per-OS implementation of the environment checks."""

    family: OSFamily = OSFamily.UNKNOWN
    #: Human name of the package manager used to install things.
    installer_name: str = ""

    def __init__(self) -> None:
        self._os_info: OSInfo | None = None
        #: Results are cached so a later step can read an earlier one, e.g. the
        #: machine check needs the podman path the podman check resolved.
        self.results: dict[str, CheckResult] = {}

    # -- OS ----------------------------------------------------------------

    @property
    def os_info(self) -> OSInfo:
        if self._os_info is None:
            self._os_info = self.detect_os()
        return self._os_info

    @abstractmethod
    def detect_os(self) -> OSInfo:
        """Identify the OS, version and whether this tool supports it."""

    # -- checks ------------------------------------------------------------

    @abstractmethod
    def check_podman(self) -> CheckResult:
        """Find the podman CLI, or say how to obtain it."""

    @abstractmethod
    def check_backend(self) -> CheckResult:
        """Check the Linux backend podman needs (WSL2, a VM, or native)."""

    def check_provider(self) -> CheckResult:
        """Check that podman is configured to use the intended backend."""
        return CheckResult(
            key="provider",
            title="Machine provider",
            status=Status.SKIPPED,
            summary="Not applicable on this platform",
        )

    def check_machine(self) -> CheckResult:
        """Report on the podman machine, where the platform uses one."""
        return CheckResult(
            key="machine",
            title="Podman machine",
            status=Status.SKIPPED,
            summary="Not applicable on this platform",
        )

    def check_os(self) -> CheckResult:
        info = self.os_info
        if not info.supported:
            status, summary = Status.UNSUPPORTED, info.notes or "Unsupported OS"
        else:
            status, summary = Status.OK, info.pretty
        detail = info.notes if info.notes and info.supported else ""
        return CheckResult(
            key="os",
            title="Operating system",
            status=status,
            summary=summary,
            detail=detail,
            evidence={
                "family": info.family.value,
                "name": info.name,
                "version": info.version,
                "build": info.build,
                "arch": info.arch,
            },
        )

    def steps(self) -> Sequence[CheckStep]:
        """The preflight sequence, in the order the UI runs and shows it."""
        return (
            ("os", "Operating system", self.check_os),
            ("podman", "Podman CLI", self.check_podman),
            ("backend", self.backend_title, self.check_backend),
            ("provider", "Machine provider", self.check_provider),
            ("machine", "Podman machine", self.check_machine),
            ("vscode", "VS Code Dev Containers", self.check_vscode_extension),
            ("devcontainers", "Dev Containers engine", self.check_devcontainers_engine),
            ("containertools", "Container Tools engine", self.check_container_tools_engine),
        )

    #: Checks that improve the setup but are not needed to build, run or deploy
    #: an image. They are reported, yet never lock the later stages: an
    #: unconfigured editor must not stop someone building an image.
    optional_keys: frozenset[str] = frozenset({"vscode", "devcontainers", "containertools"})

    # -- VS Code -----------------------------------------------------------
    # Shared by every platform: only the file locations differ, and the vscode
    # module already resolves those per OS.

    def check_vscode_extension(self) -> CheckResult:
        from ..core import vscode

        cli = vscode.find_cli()
        if not cli:
            return CheckResult(
                key="vscode",
                title="VS Code Dev Containers",
                status=Status.SKIPPED,
                summary="VS Code not found",
                detail=(
                    "The Dev Containers checks only apply to VS Code. Install it "
                    "and re-run the checks if you want to open these images as "
                    "development containers."
                ),
            )

        listed = run([cli, "--list-extensions", "--show-versions"], timeout=90)
        if not listed.ok:
            return CheckResult(
                key="vscode",
                title="VS Code Dev Containers",
                status=Status.FAILED,
                summary="Could not list VS Code extensions",
                detail=listed.output,
                evidence={"cli": cli},
            )

        version = ""
        for line in listed.lines():
            name, _, found_version = line.partition("@")
            if name.strip().lower() == vscode.EXTENSION_ID:
                version = found_version.strip()
                break

        if version:
            return CheckResult(
                key="vscode",
                title="VS Code Dev Containers",
                status=Status.OK,
                summary=f"Dev Containers {version} installed",
                evidence={"cli": cli, "extension": f"{vscode.EXTENSION_ID}@{version}"},
            )

        return CheckResult(
            key="vscode",
            title="VS Code Dev Containers",
            status=Status.MISSING,
            summary="Dev Containers extension not installed",
            detail=(
                f"VS Code is installed but {vscode.EXTENSION_ID} is not. It opens "
                "a folder inside a container built from these images."
            ),
            remedy=self._install_devcontainers_remedy(cli),
            evidence={"cli": cli},
        )

    def _install_devcontainers_remedy(self, cli: str):
        from ..core import vscode
        from ..core.models import Remedy, RemedyOutcome

        def action(sink) -> RemedyOutcome:
            sink.step(f"Installing {vscode.EXTENSION_ID}")
            result = run(vscode.install_extension_command(cli), timeout=300)
            for line in result.output.splitlines():
                sink.log(line)
            if not result.ok:
                return RemedyOutcome(False, f"Extension install failed: {result.output}")
            return RemedyOutcome(
                True,
                "Dev Containers installed. A VS Code window that was already open "
                "may need a reload to show it.",
            )

        return Remedy(
            label="Install extension",
            description=f"Run code --install-extension {vscode.EXTENSION_ID}.",
            action=action,
            estimated="under a minute",
        )

    def check_devcontainers_engine(self) -> CheckResult:
        from ..core import podman as podman_cli
        from ..core import vscode

        if not vscode.find_cli():
            return CheckResult(
                key="devcontainers",
                title="Dev Containers engine",
                status=Status.SKIPPED,
                summary="VS Code not found",
            )

        state = vscode.read_docker_path()
        evidence: dict[str, object] = {
            "settings": str(state.settings_path),
            vscode.DOCKER_PATH_KEY: state.current or "(unset, defaults to docker)",
        }
        if state.error:
            return CheckResult(
                key="devcontainers",
                title="Dev Containers engine",
                status=Status.FAILED,
                summary="VS Code settings could not be read",
                detail=(
                    f"{state.settings_path} did not parse: {state.error}. It is "
                    "left untouched; fix it in VS Code and re-run the checks."
                ),
                evidence=evidence,
            )

        if state.points_at_podman():
            return CheckResult(
                key="devcontainers",
                title="Dev Containers engine",
                status=Status.OK,
                summary=f"Uses podman ({state.current})",
                evidence=evidence,
            )

        target = podman_cli.executable()
        if not target:
            return CheckResult(
                key="devcontainers",
                title="Dev Containers engine",
                status=Status.SKIPPED,
                summary="Waiting on the podman CLI",
                evidence=evidence,
            )
        # Record the file's real casing rather than PATHEXT's upper-case .EXE.
        try:
            from pathlib import Path

            target = str(Path(target).resolve())
        except OSError:
            pass

        current = state.current or "docker, the default"
        detail = [
            f"Dev Containers currently runs {current}, so it would look for Docker "
            "rather than podman.",
            "",
            f"The fix sets {vscode.DOCKER_PATH_KEY} in your user settings to podman's "
            "full path. User settings, because the extension ignores this setting "
            "in a project's .vscode/settings.json. A full path, because a VS Code "
            "window opened before podman was installed cannot find a bare "
            "'podman' on its PATH.",
        ]
        if state.legacy and "podman" not in state.legacy.lower():
            detail += [
                "",
                f"The old key {vscode.LEGACY_DOCKER_PATH_KEY} is also set to "
                f"{state.legacy}; the new key takes precedence.",
            ]
        return CheckResult(
            key="devcontainers",
            title="Dev Containers engine",
            status=Status.REPAIRABLE,
            summary=f"Uses {current}, not podman",
            detail="\n".join(detail),
            remedy=self._configure_devcontainers_remedy(target),
            evidence=evidence,
        )

    def check_container_tools_engine(self) -> CheckResult:
        """Container Tools, the extension behind VS Code's CONTAINERS view.

        It is separate from Dev Containers and reads its own settings, so the
        Dev Containers fix leaves this view reporting "Is Docker installed?".
        """
        from ..core import podman as podman_cli
        from ..core import vscode

        title = "Container Tools engine"
        cli = vscode.find_cli()
        if not cli:
            return CheckResult(
                key="containertools", title=title, status=Status.SKIPPED,
                summary="VS Code not found",
            )

        installed = vscode.installed_extensions(cli)
        if installed is not None and vscode.CONTAINER_TOOLS_ID not in installed:
            return CheckResult(
                key="containertools", title=title, status=Status.SKIPPED,
                summary="Container Tools extension not installed",
                detail=(
                    f"{vscode.CONTAINER_TOOLS_ID} provides the CONTAINERS and IMAGES "
                    "views. It is not installed, so there is nothing to configure."
                ),
            )

        state = vscode.read_container_tools()
        evidence: dict[str, object] = {
            "settings": str(state.settings_path),
            vscode.CONTAINER_CLIENT_KEY: state.client or "(unset, defaults to Docker)",
            vscode.CONTAINER_COMMAND_KEY: state.command or "(unset, auto-detected)",
        }
        if state.error:
            return CheckResult(
                key="containertools", title=title, status=Status.FAILED,
                summary="VS Code settings could not be read",
                detail=f"{state.settings_path} did not parse: {state.error}.",
                evidence=evidence,
            )

        if state.uses_podman() and not vscode.is_pre_quoted(state.command):
            return CheckResult(
                key="containertools", title=title, status=Status.OK,
                summary="Uses the podman client",
                evidence=evidence,
            )

        target = podman_cli.executable()
        if not target:
            return CheckResult(
                key="containertools", title=title, status=Status.SKIPPED,
                summary="Waiting on the podman CLI",
                evidence=evidence,
            )
        try:
            from pathlib import Path

            target = str(Path(target).resolve())
        except OSError:
            pass

        if state.uses_podman():
            summary = "podman client set, but its command is quoted"
            lead = [
                f"{vscode.CONTAINER_COMMAND_KEY} is {state.command}, wrapped in "
                "quotes. The setting's description asks for that, but the extension "
                "adds quotes itself when it needs them. When it launches podman "
                "directly, the quotes become part of the file name and podman fails "
                "to start.",
            ]
        else:
            summary = "CONTAINERS view looks for Docker, not podman"
            lead = [
                "Container Tools owns VS Code's CONTAINERS, IMAGES and REGISTRIES "
                "views. With no client chosen it uses Docker, which is why those "
                "views say \"Failed to connect. Is Docker installed?\"",
                "",
                "It is a separate extension from Dev Containers and reads its own "
                "settings, so the Dev Containers fix does not reach it.",
            ]

        return CheckResult(
            key="containertools",
            title=title,
            status=Status.REPAIRABLE,
            summary=summary,
            detail="\n".join(
                lead
                + [
                    "",
                    f"The fix selects its podman client and sets "
                    f"{vscode.CONTAINER_COMMAND_KEY} to podman's full path, unquoted. "
                    "A full path, because a VS Code started before podman was "
                    "installed cannot find a bare 'podman'. VS Code has to be "
                    "restarted afterwards; the extension only reads the client "
                    "choice when it starts.",
                ]
            ),
            remedy=self._configure_container_tools_remedy(target),
            evidence=evidence,
        )

    def _configure_container_tools_remedy(self, podman_path: str):
        from ..core import paths, vscode
        from ..core.models import Remedy, RemedyOutcome

        command = vscode.container_tools_command(podman_path)

        def action(sink) -> RemedyOutcome:
            sink.step("Selecting the podman client for Container Tools")
            outcome = vscode.write_settings(
                {
                    vscode.CONTAINER_CLIENT_KEY: vscode.PODMAN_CLIENT_ID,
                    vscode.CONTAINER_COMMAND_KEY: command,
                },
                backup_dir=paths.backup_dir(),
            )
            if outcome.backup:
                sink.log(f"Previous settings backed up to {outcome.backup}")
            sink.log(outcome.message)
            if not outcome.ok:
                return RemedyOutcome(False, outcome.message)
            return RemedyOutcome(
                True,
                "Container Tools now uses podman. Restart VS Code: the extension "
                "only reads the client choice when it starts.",
                needs_restart=True,
            )

        return Remedy(
            label="Use podman",
            description=(
                f"Set {vscode.CONTAINER_CLIENT_KEY} to the podman client and "
                f"{vscode.CONTAINER_COMMAND_KEY} to {command} in your VS Code user "
                "settings. Backed up first; nothing else in the file changes."
            ),
            action=action,
            estimated="instant, then restart VS Code",
        )

    def _configure_devcontainers_remedy(self, podman_path: str):
        from ..core import paths, vscode
        from ..core.models import Remedy, RemedyOutcome

        def action(sink) -> RemedyOutcome:
            sink.step(f"Setting {vscode.DOCKER_PATH_KEY}")
            outcome = vscode.write_docker_path(podman_path, backup_dir=paths.backup_dir())
            if outcome.backup:
                sink.log(f"Previous settings backed up to {outcome.backup}")
            sink.log(outcome.message)
            if not outcome.ok:
                return RemedyOutcome(False, outcome.message)
            return RemedyOutcome(
                True,
                f"{outcome.message}. Reload any open VS Code window to pick it up.",
            )

        return Remedy(
            label="Use podman",
            description=(
                f"Set {vscode.DOCKER_PATH_KEY} to {podman_path} in your VS Code user "
                "settings. The file is backed up first, and only that one key "
                "changes; comments and other settings are left as they are."
            ),
            action=action,
            estimated="instant",
        )

    @property
    def backend_title(self) -> str:
        return "Container backend"

    def run_all(self, on_result: Callable[[CheckResult], None] | None = None) -> list[CheckResult]:
        """Run every step in order, caching results as it goes."""
        collected: list[CheckResult] = []
        for key, title, probe in self.steps():
            try:
                result = probe()
            except Exception as exc:  # a broken probe must not kill the run
                result = CheckResult(
                    key=key,
                    title=title,
                    status=Status.FAILED,
                    summary="Check raised an error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            self.results[result.key] = result
            collected.append(result)
            if on_result is not None:
                on_result(result)
        return collected

    # -- shared helpers ----------------------------------------------------

    def find_podman_on_path(self) -> str | None:
        """Stage 1.1: plain PATH lookup."""
        return which("podman")

    def find_podman_in_known_dirs(self) -> list[str]:
        """Stage 1.2 fallback: look where the installer normally puts it."""
        hits: list[str] = []
        exe = "podman.exe" if self.family is OSFamily.WINDOWS else "podman"
        for directory in self.candidate_dirs():
            if not directory:
                continue
            candidate = os.path.join(directory, exe)
            if os.path.isfile(candidate) and candidate not in hits:
                hits.append(candidate)
        return hits

    def candidate_dirs(self) -> Sequence[str]:
        return ()


#: The name podman gives a machine when none is supplied. Passing it explicitly
#: keeps the create step and the start step talking about the same machine.
DEFAULT_MACHINE_NAME = "podman-machine-default"

#: Output fragments meaning the command failed only because the work was already
#: done, taken from podman's own messages: "VM already exists" and "VM already
#: running or starting". Its exit code does not distinguish those from a real
#: failure, and the machine listing is checked afterwards either way.
ALREADY_EXISTS = ("already exists",)
ALREADY_RUNNING = ("already running",)


def parse_machines(stdout: str) -> list[dict]:
    """Rows from `podman machine list --format json`, or an empty list."""
    import json

    try:
        rows = json.loads(stdout or "[]")
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def default_machine(machines: Sequence[dict]) -> str:
    """The machine podman commands act on when given no name."""
    for row in machines:
        if row.get("Default"):
            return str(row.get("Name") or DEFAULT_MACHINE_NAME)
    if machines:
        return str(machines[0].get("Name") or DEFAULT_MACHINE_NAME)
    return DEFAULT_MACHINE_NAME


def run_machine_command(
    sink: ProgressSink,
    argv: Sequence[str],
    *,
    timeout: float,
    tolerate: Sequence[str] = (),
) -> tuple[bool, str]:
    """Run a podman machine command, logging its output as it arrives.

    Creating a machine downloads a disk image, so this streams rather than
    waiting silently for minutes. Returns ``(ok, output)``; a non-zero exit
    still counts as ok when the output matches one of *tolerate*.
    """
    lines: list[str] = []
    code = "0"
    for kind, text in stream(argv, timeout=timeout):
        if kind == "line":
            lines.append(text)
            sink.log(text)
        else:
            code = text
    output = "\n".join(lines)
    if code == "0":
        return True, output
    haystack = output.lower()
    if any(fragment in haystack for fragment in tolerate):
        return True, output
    return False, output or f"exit code {code}"


def _last_meaningful_line(output: str) -> str:
    """The last non-empty line, which is where podman puts its error."""
    for line in reversed(output.splitlines()):
        if line.strip():
            return line.strip()
    return "no output"


class MachinePlatform(Platform):
    """A platform where podman's Linux side lives in a VM podman manages.

    Windows (WSL2) and macOS (Apple's hypervisor) both build images inside a
    ``podman machine``; a native Linux host has none, which is why this is a
    subclass rather than part of :class:`Platform`.
    """

    #: Anything the OS adds to the explanation of a missing machine.
    machine_note: str = ""

    # -- check -------------------------------------------------------------

    def check_machine(self) -> CheckResult:
        title = "Podman machine"
        podman_result = self.results.get("podman")
        if podman_result is not None and podman_result.status in (
            Status.MISSING,
            Status.FAILED,
        ):
            return CheckResult(
                key="machine",
                title=title,
                status=Status.SKIPPED,
                summary="Waiting on the podman CLI",
            )

        exe = self.podman_executable()
        if not exe:
            return CheckResult(
                key="machine",
                title=title,
                status=Status.SKIPPED,
                summary="podman is not runnable yet",
            )

        result = run([exe, "machine", "list", "--format", "json"], timeout=60)
        evidence: dict[str, object] = {"raw": result.output[:2000]}
        if not result.ok:
            return CheckResult(
                key="machine",
                title=title,
                status=Status.INFO,
                summary="Could not list machines",
                detail=result.output,
                evidence=evidence,
            )

        machines = parse_machines(result.stdout)
        evidence["count"] = len(machines)

        if not machines:
            detail = [
                "Podman is installed but has no Linux machine, and every image is "
                "built and kept inside one. Creating and starting it is the next "
                "step; until then podman has nowhere to build.",
                "",
                "The first run downloads a Linux disk image of a few hundred "
                "megabytes, so it takes a few minutes.",
            ]
            if self.machine_note:
                detail += ["", self.machine_note]
            return CheckResult(
                key="machine",
                title=title,
                status=Status.REPAIRABLE,
                summary="No machine created yet",
                detail="\n".join(detail),
                remedy=self.create_machine_remedy(),
                evidence=evidence,
            )

        names = ", ".join(str(m.get("Name", "?")) for m in machines)
        evidence["names"] = names
        running = [m for m in machines if m.get("Running")]
        if running:
            return CheckResult(
                key="machine",
                title=title,
                status=Status.OK,
                summary=f"{len(running)} of {len(machines)} running ({names})",
                evidence=evidence,
            )

        return CheckResult(
            key="machine",
            title=title,
            status=Status.REPAIRABLE,
            summary=f"Machine present but stopped ({names})",
            detail=(
                "The machine exists but is not running, so podman cannot reach "
                "the images inside it and a build has nowhere to go. Starting it "
                "downloads nothing and keeps everything already in there."
            ),
            remedy=self.start_machine_remedy(default_machine(machines)),
            evidence=evidence,
        )

    # -- remedies ----------------------------------------------------------

    def create_machine_remedy(self) -> Remedy:
        def action(sink: ProgressSink) -> RemedyOutcome:
            exe = self._resolve_podman(sink)
            if not exe:
                return RemedyOutcome(
                    False,
                    "podman could not be found. Install it first, then re-run "
                    "the checks.",
                )
            sink.step(f"Creating {DEFAULT_MACHINE_NAME}; this downloads a Linux image")
            ok, output = run_machine_command(
                sink,
                [exe, "machine", "init", DEFAULT_MACHINE_NAME],
                timeout=3600,
                tolerate=ALREADY_EXISTS,
            )
            if not ok:
                return RemedyOutcome(
                    False,
                    f"podman machine init failed: {_last_meaningful_line(output)}",
                )
            return self._start_machine(sink, exe, DEFAULT_MACHINE_NAME)

        return Remedy(
            label="Create machine",
            description=(
                f"Run podman machine init {DEFAULT_MACHINE_NAME}, then podman "
                "machine start. The first run downloads a Linux disk image of a "
                "few hundred megabytes. The machine belongs to your user account, "
                "so no administrator approval is needed."
            ),
            action=action,
            requires_elevation=False,
            estimated="3-10 minutes on the first run",
        )

    def start_machine_remedy(self, name: str = DEFAULT_MACHINE_NAME) -> Remedy:
        def action(sink: ProgressSink) -> RemedyOutcome:
            exe = self._resolve_podman(sink)
            if not exe:
                return RemedyOutcome(False, "podman could not be found.")
            return self._start_machine(sink, exe, name)

        return Remedy(
            label="Start machine",
            description=(
                f"Run podman machine start {name}. Nothing is downloaded and the "
                "images already inside the machine are untouched."
            ),
            action=action,
            requires_elevation=False,
            estimated="under a minute",
        )

    # -- remedy helpers ----------------------------------------------------

    def _start_machine(self, sink: ProgressSink, exe: str, name: str) -> RemedyOutcome:
        sink.step(f"Starting {name}")
        ok, output = run_machine_command(
            sink,
            [exe, "machine", "start", name],
            timeout=1800,
            tolerate=ALREADY_RUNNING,
        )
        if not ok:
            return RemedyOutcome(
                False, f"podman machine start failed: {_last_meaningful_line(output)}"
            )

        # Trust podman's own listing rather than the exit code: a machine that
        # reports success and then dies would otherwise be called ready.
        sink.step("Verifying the machine is running")
        listed = run([exe, "machine", "list", "--format", "json"], timeout=60)
        machines = parse_machines(listed.stdout) if listed.ok else []
        if any(m.get("Running") for m in machines):
            return RemedyOutcome(True, f"{name} is running. Podman can build images now.")
        return RemedyOutcome(
            False,
            f"{name} was set up, but podman does not report it as running. See "
            "the activity log for podman's own output.",
        )

    def _resolve_podman(self, sink: ProgressSink) -> str:
        """The podman to run a machine command with, with its environment sane.

        ``podman machine init`` writes its connection record under ``%APPDATA%``
        and finds the machine through the user profile, so a process missing
        those variables creates a machine it cannot then reach.
        """
        from ..core import podman as podman_cli

        restored = podman_cli.repair_environment()
        if restored:
            sink.log(f"Restored {', '.join(restored)} for this process")
        # The lookup is cached, and podman may have been installed by an earlier
        # fix in this same batch, after that cache said there was none.
        podman_cli.forget()
        return self.podman_executable()

    def podman_executable(self) -> str:
        """Podman's full path: PATH, the stored PATH, then the install dirs."""
        from ..core import podman as podman_cli

        found = podman_cli.executable()
        if found:
            return found
        candidates = self.find_podman_in_known_dirs()
        return candidates[0] if candidates else ""
