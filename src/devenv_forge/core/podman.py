"""Locating podman and reporting why it is unusable.

A process keeps the PATH it inherited when it started. Podman is installed into
a directory that goes on the machine PATH, so an application launched from a
terminal that was open before the install cannot see it, however correct the
install is. Resolving through PATH alone therefore reports a missing podman on a
machine where podman works.

This module resolves the executable properly and, when it cannot, says which of
the several possible reasons applies instead of collapsing them into "nothing
found".
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

from .runner import run, which


class PodmanState(str, Enum):
    READY = "ready"
    NOT_INSTALLED = "not_installed"
    MACHINE_STOPPED = "machine_stopped"
    #: The machine is running, but the client has no connection to reach it.
    #: Starting the machine cannot help, and podman's own advice to run
    #: "machine init" and "machine start" fails with "already exists" and
    #: "already running".
    NO_CONNECTION = "no_connection"
    ERROR = "error"


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

#: Known folder ids, which Windows resolves without consulting environment
#: variables. That is the point: they still answer when APPDATA is missing.
_FOLDER_IDS = {
    "APPDATA": "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D",  # FOLDERID_RoamingAppData
    "LOCALAPPDATA": "F1B32785-6FBA-4FCF-9D55-7B8E7F157091",  # FOLDERID_LocalAppData
}


def _known_folder(folder_id: str) -> str:
    """Resolve a Windows known folder straight from the shell, or return ""."""
    if sys.platform != "win32":
        return ""
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    parsed = uuid.UUID(folder_id)
    guid = GUID()
    guid.Data1, guid.Data2, guid.Data3 = parsed.fields[0], parsed.fields[1], parsed.fields[2]
    for index, byte in enumerate(parsed.bytes[8:]):
        guid.Data4[index] = byte

    path = ctypes.c_wchar_p()
    try:
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(path)
        )
        return path.value or "" if result == 0 else ""
    except OSError:
        return ""
    finally:
        if path:
            ctypes.windll.ole32.CoTaskMemFree(path)


def repair_environment() -> list[str]:
    """Restore Windows profile variables that podman depends on.

    podman keeps its connections in %APPDATA%\\containers but finds the machine
    through the user profile. A process started without APPDATA therefore sees
    a machine that exists and is running, yet has no connection to reach it,
    and every command fails with a misleading "cannot connect". Returns the
    names of the variables it restored.
    """
    if sys.platform != "win32":
        return []
    restored: list[str] = []
    for name, folder_id in _FOLDER_IDS.items():
        if os.environ.get(name):
            continue
        value = _known_folder(folder_id)
        if value:
            os.environ[name] = value
            restored.append(name)
    return restored


@dataclass(slots=True)
class PodmanStatus:
    state: PodmanState
    executable: str = ""
    version: str = ""
    detail: str = ""
    hint: str = ""

    @property
    def ready(self) -> bool:
        return self.state is PodmanState.READY


#: Where the Windows installer puts podman, checked when PATH does not have it.
def _known_dirs() -> list[str]:
    if sys.platform == "win32":
        env = os.environ
        program_files = env.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = env.get("LOCALAPPDATA", "")
        dirs = [
            os.path.join(program_files, "RedHat", "Podman"),
            os.path.join(program_files_x86, "RedHat", "Podman"),
        ]
        if local:
            dirs.append(os.path.join(local, "Programs", "RedHat", "Podman"))
            dirs.append(os.path.join(local, "Microsoft", "WinGet", "Links"))
        return dirs
    return ["/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"]


def reload_path_from_registry() -> bool:
    """Rebuild this process's PATH from the stored machine and user values.

    Picks up anything installed since the process started. Windows only; a no-op
    elsewhere, where the same staleness exists but has no registry to consult.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:  # pragma: no cover - defensive
        return False

    parts: list[str] = []
    for root, subkey in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:
                parts.append(os.path.expandvars(str(winreg.QueryValueEx(key, "Path")[0])))
        except OSError:
            continue
    if not parts:
        return False
    merged = os.pathsep.join(p for p in parts if p)
    if merged and merged != os.environ.get("PATH"):
        os.environ["PATH"] = merged
        return True
    return False


@lru_cache(maxsize=1)
def executable() -> str:
    """Full path to podman, or an empty string.

    Tries PATH, then the stored PATH, then the directories the installer uses.
    """
    found = which("podman")
    if found:
        return found
    if reload_path_from_registry():
        found = which("podman")
        if found:
            return found
    name = "podman.exe" if sys.platform == "win32" else "podman"
    for directory in _known_dirs():
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            # Put it on PATH so podman's own helper lookups work too.
            os.environ["PATH"] = f"{os.environ.get('PATH', '')}{os.pathsep}{directory}"
            return candidate
    return ""


def forget() -> None:
    """Drop the cached lookup, for use after an install."""
    executable.cache_clear()


def _machine_running(binary: str, timeout: float) -> bool | None:
    """True or False from podman machine list, or None when it cannot be told."""
    import json

    result = run([binary, "machine", "list", "--format", "json"], timeout=timeout)
    if not result.ok:
        return None
    try:
        machines = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    if not isinstance(machines, list):
        return None
    return any(bool(m.get("Running")) for m in machines if isinstance(m, dict))


def _connection_count(binary: str, timeout: float) -> int | None:
    import json

    result = run([binary, "system", "connection", "list", "--format", "json"], timeout=timeout)
    if not result.ok:
        return None
    try:
        connections = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    return len(connections) if isinstance(connections, list) else None


def stranded_connection_files() -> list[tuple[str, str]]:
    """Connection files trapped in a packaged app's private storage.

    A packaged (MSIX) Windows app redirects writes under AppData\\Roaming into
    its own private folder. Running ``podman machine init`` from a terminal
    inside such an app, an IDE or an AI assistant for instance, creates the
    machine in the real profile but writes the connections file into that
    private folder. Every process outside the app then sees a running machine
    with no connection. Returns ``(package name, file path)`` pairs.
    """
    if sys.platform != "win32":
        return []
    local = os.environ.get("LOCALAPPDATA") or _known_folder(_FOLDER_IDS["LOCALAPPDATA"])
    if not local:
        return []
    packages = os.path.join(local, "Packages")
    found: list[tuple[str, str]] = []
    try:
        entries = os.listdir(packages)
    except OSError:
        return []
    for name in entries:
        candidate = os.path.join(
            packages, name, "LocalCache", "Roaming", "containers", "podman-connections.json"
        )
        if os.path.isfile(candidate):
            found.append((name, candidate))
    return found


def real_connections_file() -> str:
    appdata = os.environ.get("APPDATA") or _known_folder(_FOLDER_IDS["APPDATA"])
    return os.path.join(appdata, "containers", "podman-connections.json") if appdata else ""


def restore_command(source: str) -> str:
    """PowerShell that copies a stranded connections file to the real profile.

    Meant to be run by the user from an ordinary terminal. Run from inside the
    packaged app, the copy would be redirected straight back into its storage.
    """
    return (
        "New-Item -ItemType Directory -Force \"$env:APPDATA\\containers\" | Out-Null; "
        f"Copy-Item '{source}' \"$env:APPDATA\\containers\\podman-connections.json\""
    )


_CONNECTION_MARKERS = (
    "cannot connect",
    "unable to connect",
    "connection refused",
    "dead network",
    "podman.sock",
    "no such host",
)


def status(*, timeout: float = 45.0) -> PodmanStatus:
    """Work out whether podman can actually be used, and why not if it cannot."""
    restored = repair_environment()
    binary = executable()
    if not binary:
        return PodmanStatus(
            state=PodmanState.NOT_INSTALLED,
            detail="The podman command could not be found.",
            hint=(
                "Run the preflight checks on the first page to install it. If it "
                "is already installed, this application was started before the "
                "install and needs restarting."
            ),
        )

    version = run([binary, "--version"], timeout=timeout)
    if not version.ok:
        return PodmanStatus(
            state=PodmanState.ERROR,
            executable=binary,
            detail=version.output or "podman --version failed",
        )

    # The client runs on the host but every image lives in the machine, so a
    # stopped machine looks exactly like an empty image list.
    probe = run([binary, "images", "--format", "{{.ID}}"], timeout=timeout)
    if probe.ok:
        return PodmanStatus(
            state=PodmanState.READY,
            executable=binary,
            version=version.output.strip(),
        )

    text = probe.output.lower()
    if any(marker in text for marker in _CONNECTION_MARKERS):
        # "Cannot connect" has two very different causes, and the fix for one
        # does nothing for the other, so ask podman which applies.
        running = _machine_running(binary, timeout)
        connections = _connection_count(binary, timeout)

        if running and not connections:
            cause = (
                "The podman machine is running, but podman has no connection to "
                "reach it, so every command fails to connect. Starting the "
                "machine will not help; it is already running."
            )
            stranded = stranded_connection_files()
            if stranded:
                package, source = stranded[0]
                cause += (
                    f" The connection file exists, but inside the private storage "
                    f"of the packaged app {package}. That happens when "
                    "'podman machine init' is run from a terminal inside that app: "
                    "Windows redirects the write, so only that app can see it."
                )
                hint = (
                    "Copy it to your real profile from an ordinary terminal, not "
                    f"one inside that app:  {restore_command(source)}"
                )
            elif restored:
                cause += (
                    f" This process had no {', '.join(restored)} set; it has been "
                    "restored, so refreshing may already fix it."
                )
                hint = "Click Refresh."
            else:
                hint = (
                    "The connection records are missing. Recreate them with "
                    "'podman machine rm' followed by 'podman machine init', which "
                    "also discards the machine's images."
                )
            return PodmanStatus(
                state=PodmanState.NO_CONNECTION,
                executable=binary,
                version=version.output.strip(),
                detail=cause,
                hint=hint,
            )

        if running is False:
            return PodmanStatus(
                state=PodmanState.MACHINE_STOPPED,
                executable=binary,
                version=version.output.strip(),
                detail="The podman machine is not running, so no images are reachable.",
                hint="Start it with: podman machine start",
            )

    return PodmanStatus(
        state=PodmanState.ERROR,
        executable=binary,
        version=version.output.strip(),
        detail=probe.output,
    )


def machine_start_command() -> list[str]:
    return [executable() or "podman", "machine", "start"]
