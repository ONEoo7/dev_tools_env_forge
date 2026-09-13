"""VS Code and its Dev Containers extension, pointed at podman.

Three details decide whether this works, and each has already caused trouble
elsewhere in this tool:

* ``dev.containers.dockerPath`` is an *application*-scoped setting. It is only
  honoured in the user settings file; placed in a project's
  ``.vscode/settings.json`` it is silently ignored.
* The extension accepts an executable name or a full path. A bare ``podman``
  fails in a VS Code that was already open when podman was installed, because
  that process kept its old PATH, so the resolved full path is written.
* On Windows the user settings live under ``AppData\\Roaming``. A process running
  inside a packaged (MSIX) app has those writes redirected into the app's
  private storage, where VS Code never looks. Writing is refused when that is
  detected, rather than reporting success for a change that did not happen.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .runner import run, which

EXTENSION_ID = "ms-vscode-remote.remote-containers"
DOCKER_PATH_KEY = "dev.containers.dockerPath"
#: The pre-rename key. Still read by older extension versions, so a stale value
#: here is worth reporting.
LEGACY_DOCKER_PATH_KEY = "remote.containers.dockerPath"


# ---------------------------------------------------------------------------
# locating VS Code
# ---------------------------------------------------------------------------


def _candidate_clis() -> list[str]:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        return [
            os.path.join(local, "Programs", "Microsoft VS Code", "bin", "code.cmd"),
            os.path.join(program_files, "Microsoft VS Code", "bin", "code.cmd"),
        ]
    if sys.platform == "darwin":
        return [
            "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
            "/usr/local/bin/code",
            "/opt/homebrew/bin/code",
        ]
    return ["/usr/bin/code", "/usr/share/code/bin/code", "/snap/bin/code"]


def find_cli() -> str:
    """Path to the ``code`` command, or an empty string.

    Checks PATH first, then the install locations, so a VS Code installed after
    this process started is still found.
    """
    found = which("code")
    if found:
        return found
    for candidate in _candidate_clis():
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


def user_settings_path() -> Path:
    """The user settings file, where application-scoped settings must live."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Code" / "User" / "settings.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User" / "settings.json"
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "Code" / "User" / "settings.json"


def installed_extensions(cli: str, timeout: float = 90.0) -> set[str] | None:
    """Lower-cased extension ids, or None when the CLI could not be run."""
    result = run([cli, "--list-extensions"], timeout=timeout)
    if not result.ok:
        return None
    return {line.strip().lower() for line in result.stdout.splitlines() if line.strip()}


def install_extension_command(cli: str) -> list[str]:
    return [cli, "--install-extension", EXTENSION_ID]


# ---------------------------------------------------------------------------
# JSON with comments
# ---------------------------------------------------------------------------


def strip_jsonc(text: str) -> str:
    """Remove comments and trailing commas so the stdlib can parse the text.

    String-aware on purpose: settings routinely contain URLs, and a naive
    comment stripper cuts ``"https://example.com"`` in half at the ``//``.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    return _drop_trailing_commas("".join(out))


def _drop_trailing_commas(text: str) -> str:
    out: list[str] = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_jsonc(text: str) -> dict:
    """Parse a settings file. Empty text is an empty object."""
    stripped = strip_jsonc(text).strip()
    if not stripped:
        return {}
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("settings file is not a JSON object")
    return value


def _skip_insignificant(text: str, i: int) -> int:
    """Advance past whitespace and comments."""
    n = len(text)
    while i < n:
        if text[i] in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            break
    return i


def _string_end(text: str, i: int) -> int:
    """Index just past the string starting at ``text[i] == '"'``."""
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    raise ValueError("unterminated string")


def _value_end(text: str, i: int) -> int:
    """Index just past the JSON value starting at ``i``."""
    if text[i] == '"':
        return _string_end(text, i)
    if text[i] in "{[":
        depth = 0
        while i < len(text):
            i = _skip_insignificant(text, i)
            ch = text[i]
            if ch == '"':
                i = _string_end(text, i)
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise ValueError("unterminated container")
    j = i
    while j < len(text) and text[j] not in ",}] \t\r\n/":
        j += 1
    return j


def _top_level_member(text: str, key: str) -> tuple[int, int] | None:
    """Span of the value for ``key`` in the outermost object, or None."""
    i = _skip_insignificant(text, 0)
    if i >= len(text) or text[i] != "{":
        raise ValueError("settings file does not start with an object")
    i += 1
    while True:
        i = _skip_insignificant(text, i)
        if i >= len(text) or text[i] == "}":
            return None
        if text[i] == ",":
            i += 1
            continue
        if text[i] != '"':
            raise ValueError(f"unexpected character at {i}")
        key_end = _string_end(text, i)
        name = json.loads(text[i:key_end])
        i = _skip_insignificant(text, key_end)
        if i >= len(text) or text[i] != ":":
            raise ValueError("expected ':' after key")
        i = _skip_insignificant(text, i + 1)
        value_end = _value_end(text, i)
        if name == key:
            return i, value_end
        i = value_end


def _indent_unit(text: str) -> str:
    """The indentation the file already uses for its top-level keys."""
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if stripped.startswith('"'):
            return line[: len(line) - len(stripped)] or "    "
    return "    "


def set_top_level(text: str, key: str, value: object) -> str:
    """Set one top-level setting, leaving every other byte of the file alone.

    Rewriting the whole file through ``json.dumps`` would discard the user's
    comments and ordering, so the value is spliced in textually instead.
    """
    literal = json.dumps(value)
    if not text.strip():
        return "{\n" + f'    {json.dumps(key)}: {literal}\n' + "}\n"

    span = _top_level_member(text, key)
    if span is not None:
        start, end = span
        return text[:start] + literal + text[end:]

    opening = text.index("{", _skip_insignificant(text, 0))
    after = _skip_insignificant(text, opening + 1)
    indent = _indent_unit(text)
    entry = f"\n{indent}{json.dumps(key)}: {literal}"
    if text[after] == "}":
        # Empty object: no neighbour needs a separating comma.
        return text[: opening + 1] + entry + "\n" + text[after:]
    return text[: opening + 1] + entry + "," + text[opening + 1 :]


# ---------------------------------------------------------------------------
# redirection
# ---------------------------------------------------------------------------


def redirected_into_package() -> str:
    """Name of the packaged app capturing this process's AppData writes, or "".

    Writes a uniquely named probe to AppData\\Roaming and looks for it in each
    package's private storage. If it appears there, anything written for VS Code
    would land where VS Code cannot see it.
    """
    if sys.platform != "win32":
        return ""
    appdata = os.environ.get("APPDATA")
    local = os.environ.get("LOCALAPPDATA")
    if not appdata or not local:
        return ""
    name = f".devenv-forge-probe-{uuid.uuid4().hex}"
    probe = os.path.join(appdata, name)
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("probe")
    except OSError:
        return ""
    try:
        packages = os.path.join(local, "Packages")
        try:
            entries = os.listdir(packages)
        except OSError:
            entries = []
        for package in entries:
            if os.path.isfile(os.path.join(packages, package, "LocalCache", "Roaming", name)):
                return package
        return ""
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# reading and writing the setting
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DockerPathState:
    settings_path: Path
    current: str | None
    legacy: str | None
    error: str = ""

    def points_at_podman(self) -> bool:
        return bool(self.current) and "podman" in os.path.basename(str(self.current)).lower()


def read_docker_path(settings_path: Path | None = None) -> DockerPathState:
    path = settings_path or user_settings_path()
    if not path.is_file():
        return DockerPathState(path, None, None)
    try:
        data = load_jsonc(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return DockerPathState(path, None, None, error=str(exc))
    current = data.get(DOCKER_PATH_KEY)
    legacy = data.get(LEGACY_DOCKER_PATH_KEY)
    return DockerPathState(
        path,
        current if isinstance(current, str) else None,
        legacy if isinstance(legacy, str) else None,
    )


@dataclass(slots=True)
class WriteOutcome:
    ok: bool
    message: str
    backup: Path | None = None


def write_docker_path(
    value: str,
    settings_path: Path | None = None,
    backup_dir: Path | None = None,
    *,
    check_redirection: bool = True,
) -> WriteOutcome:
    """Point Dev Containers at ``value``, safely."""
    return write_settings(
        {DOCKER_PATH_KEY: value},
        settings_path,
        backup_dir,
        check_redirection=check_redirection,
    )


def write_settings(
    values: dict[str, object],
    settings_path: Path | None = None,
    backup_dir: Path | None = None,
    *,
    check_redirection: bool = True,
) -> WriteOutcome:
    """Set several top-level user settings in one safe write.

    Refuses under package redirection, backs the file up first, and parses the
    result before replacing the original, so a mistake cannot leave VS Code with
    a settings file it refuses to load. All keys land together or none do.
    """
    path = settings_path or user_settings_path()

    if check_redirection:
        package = redirected_into_package()
        if package:
            return WriteOutcome(
                False,
                f"Not written: this process runs inside the packaged app {package}, "
                "so Windows would redirect the change into that app's private "
                "storage and VS Code would never see it. Run DevEnv Forge from an "
                "ordinary terminal or the Start menu and apply the fix again.",
            )

    original = ""
    if path.is_file():
        try:
            original = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            return WriteOutcome(False, f"Could not read {path}: {exc}")
        try:
            load_jsonc(original)
        except ValueError as exc:
            return WriteOutcome(
                False,
                f"{path} is not valid JSON ({exc}); leaving it untouched rather "
                "than guessing at its structure.",
            )

    try:
        updated = original
        for key, value in values.items():
            updated = set_top_level(updated, key, value)
        parsed = load_jsonc(updated)
        for key, value in values.items():
            if parsed.get(key) != value:
                raise ValueError(f"the edited file does not contain the new {key}")
    except ValueError as exc:
        return WriteOutcome(False, f"Could not edit {path} safely: {exc}")

    backup: Path | None = None
    if original and backup_dir is not None:
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = backup_dir / f"vscode-settings-{stamp}.json"
            shutil.copy2(path, backup)
        except OSError as exc:
            return WriteOutcome(False, f"Could not back up {path}: {exc}")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.devenv-forge.tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        return WriteOutcome(False, f"Could not write {path}: {exc}", backup)

    summary = ", ".join(f"{key} to {value}" for key, value in values.items())
    return WriteOutcome(True, f"Set {summary}", backup)


# ---------------------------------------------------------------------------
# Container Tools
# ---------------------------------------------------------------------------
# A separate extension from Dev Containers, formerly the Docker extension. It
# owns the CONTAINERS, IMAGES and REGISTRIES views, and has its own settings,
# so configuring Dev Containers leaves those views still looking for Docker.

CONTAINER_TOOLS_ID = "ms-azuretools.vscode-containers"
CONTAINER_CLIENT_KEY = "containers.containerClient"
CONTAINER_COMMAND_KEY = "containers.containerCommand"
PODMAN_CLIENT_ID = "com.microsoft.visualstudio.containers.podman"


def container_tools_command(executable: str) -> str:
    """The value for containers.containerCommand: the raw, unquoted path.

    The setting's own description says a path containing whitespace "needs to
    be quoted appropriately". The code that runs it says otherwise. Its process
    library returns any path with a directory unchanged, then adds quotes itself,
    and only when it launches through a shell. When it spawns the executable
    directly, a value that already carries quotes is taken as a file name with
    quote characters in it and fails to start. The raw path works on both routes.
    """
    return executable.strip().strip('"')


def is_pre_quoted(command: str | None) -> bool:
    """True for a quoted command, which breaks Container Tools' direct spawns."""
    return bool(command) and command.strip().startswith('"')


@dataclass(slots=True)
class ContainerToolsState:
    settings_path: Path
    client: str | None
    command: str | None
    error: str = ""

    def uses_podman(self) -> bool:
        return self.client == PODMAN_CLIENT_ID


def read_container_tools(settings_path: Path | None = None) -> ContainerToolsState:
    path = settings_path or user_settings_path()
    if not path.is_file():
        return ContainerToolsState(path, None, None)
    try:
        data = load_jsonc(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return ContainerToolsState(path, None, None, error=str(exc))
    client = data.get(CONTAINER_CLIENT_KEY)
    command = data.get(CONTAINER_COMMAND_KEY)
    return ContainerToolsState(
        path,
        client if isinstance(client, str) else None,
        command if isinstance(command, str) else None,
    )
