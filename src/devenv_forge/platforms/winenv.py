"""Windows-only helpers: user PATH editing, elevation, installed-app scanning.

The PATH routines here are deliberately defensive. Corrupting a user PATH is the
worst thing this tool could do, so every write is preceded by a backup and
guarded by sanity checks, and the registry value type is preserved.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# One definition of what makes two PATH entries the same directory. It lives in
# core so core.podman can merge PATHs too, without importing this Windows module.
from ..core.runner import normalise_path_entry as normalise_entry

if sys.platform == "win32":  # pragma: no branch
    import winreg
else:  # pragma: no cover - import shim so the module loads anywhere
    winreg = None  # type: ignore[assignment]

ENV_SUBKEY = "Environment"
UNINSTALL_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0
SW_SHOWNORMAL = 1
INFINITE = 0xFFFFFFFF
ERROR_CANCELLED = 1223

#: REG_SZ values top out at 32767 chars; leave headroom rather than corrupt PATH.
MAX_PATH_CHARS = 32000


# ---------------------------------------------------------------------------
# elevation
# ---------------------------------------------------------------------------


def is_elevated() -> bool:
    """True when the current process holds an elevated (administrator) token."""
    if sys.platform != "win32":
        return hasattr(os, "geteuid") and os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - defensive
        return False


class _ShellExecuteInfoW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


@dataclass(slots=True)
class ElevatedResult:
    started: bool
    returncode: int | None
    cancelled: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.started and self.returncode == 0


def run_elevated(
    executable: str,
    parameters: str,
    *,
    timeout_s: float = 3600.0,
    show: int = SW_SHOWNORMAL,
) -> ElevatedResult:
    """Launch a program through UAC and wait for it to finish.

    ShellExecuteExW with the runas verb is the only way to raise a UAC prompt. It
    cannot pipe output back, so callers needing a transcript should have the
    elevated command write to a log file and read that afterwards.

    Declining the UAC prompt is reported as cancelled, not as an error.
    """
    if sys.platform != "win32":
        return ElevatedResult(False, None, error="elevation is Windows-only")

    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = parameters
    info.lpDirectory = None
    info.nShow = show

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.GetLastError()
        if code == ERROR_CANCELLED:
            return ElevatedResult(
                False, None, cancelled=True, error="elevation declined"
            )
        return ElevatedResult(False, None, error=f"ShellExecuteExW failed ({code})")

    handle = info.hProcess
    if not handle:
        return ElevatedResult(True, None, error="no process handle returned")

    try:
        wait_ms = INFINITE if timeout_s <= 0 else int(timeout_s * 1000)
        waited = ctypes.windll.kernel32.WaitForSingleObject(handle, wait_ms)
        if waited != 0:  # WAIT_OBJECT_0
            return ElevatedResult(True, None, error="timed out waiting for process")
        code = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return ElevatedResult(True, None, error="could not read exit code")
        return ElevatedResult(True, int(code.value))
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# user PATH
# ---------------------------------------------------------------------------


def run_elevated_script(
    script: str,
    log_path: Path,
    *,
    on_line: Callable[[str], None] | None = None,
    timeout_s: float = 3600.0,
    show: int = SW_SHOWNORMAL,
) -> ElevatedResult:
    """Run a PowerShell script elevated while streaming its output back.

    An elevated child cannot hand pipes to an unelevated parent, so the script
    tees to a log file and this function tails that file while it waits. The
    console window stays visible: a multi-minute install with no window looks
    like a hang.
    """
    if sys.platform != "win32":
        return ElevatedResult(False, None, error="elevation is Windows-only")

    import threading

    log_path.parent.mkdir(parents=True, exist_ok=True)
    script_path = log_path.with_suffix(".ps1")
    body = (
        "$ErrorActionPreference = 'Continue'\n"
        f"$log = '{log_path}'\n"
        f"{script}\n"
        "exit $LASTEXITCODE\n"
    )
    script_path.write_text(body, encoding="utf-8")
    log_path.write_text("", encoding="utf-8")

    outcome: dict[str, ElevatedResult] = {}

    def worker() -> None:
        outcome["result"] = run_elevated(
            "powershell.exe",
            f'-NoProfile -ExecutionPolicy Bypass -File "{script_path}"',
            timeout_s=timeout_s,
            show=show,
        )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    offset = 0
    while thread.is_alive():
        offset = _drain_log(log_path, offset, on_line)
        thread.join(timeout=0.4)
    _drain_log(log_path, offset, on_line)

    try:
        script_path.unlink()
    except OSError:
        pass

    return outcome.get("result", ElevatedResult(False, None, error="no result"))


def _drain_log(log_path: Path, offset: int, on_line) -> int:
    """Emit any log text written since *offset* and return the new offset."""
    if on_line is None:
        return offset
    try:
        with log_path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
            offset = handle.tell()
    except OSError:
        return offset
    if not chunk:
        return offset
    from ..core.runner import smart_decode

    for line in smart_decode(chunk).splitlines():
        text = line.strip()
        if text:
            on_line(text)
    return offset


@dataclass(slots=True)
class PathWriteResult:
    ok: bool
    message: str
    backup: Path | None = None
    added: str = ""


def read_user_path() -> tuple[str, int]:
    """Return the raw (unexpanded) user PATH and its registry value type.

    QueryValueEx does not expand REG_EXPAND_SZ, so an entry stored as
    %USERPROFILE%\\bin comes back verbatim. That is exactly what must be written
    back; expanding it would bake in a literal path and break the value for any
    other user profile.
    """
    if winreg is None:
        return "", 0
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, ENV_SUBKEY, 0, winreg.KEY_READ
        ) as key:
            value, vtype = winreg.QueryValueEx(key, "Path")
            return str(value), int(vtype)
    except FileNotFoundError:
        return "", winreg.REG_EXPAND_SZ
    except OSError:
        return "", winreg.REG_EXPAND_SZ


def read_machine_path() -> str:
    """Return the raw system-wide PATH from HKLM."""
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            0,
            winreg.KEY_READ,
        ) as key:
            return str(winreg.QueryValueEx(key, "Path")[0])
    except OSError:
        return ""


def on_persistent_path(directory: str) -> bool:
    """True when *directory* is on the stored machine or user PATH.

    Distinct from :func:`path_contains`, which also consults this process's
    inherited environment. A process started before an installer ran keeps the
    old PATH for its whole life, so a directory can be correctly configured and
    still be invisible here. Checking the registry tells us what a newly opened
    terminal would actually see.
    """
    target = normalise_entry(directory)
    if not target:
        return False
    for raw in (read_machine_path(), read_user_path()[0]):
        for entry in raw.split(os.pathsep):
            if entry.strip() and normalise_entry(entry) == target:
                return True
    return False


def path_contains(directory: str, raw_path: str | None = None) -> bool:
    """True when directory is on the persistent user PATH or this process PATH."""
    target = normalise_entry(directory)
    if not target:
        return False
    candidates: list[str] = []
    if raw_path is None:
        raw_path = read_user_path()[0]
    candidates.extend(raw_path.split(os.pathsep))
    candidates.extend(os.environ.get("PATH", "").split(os.pathsep))
    return any(normalise_entry(c) == target for c in candidates if c.strip())


def backup_user_path(backup_dir: Path) -> Path | None:
    """Snapshot the current user PATH to a timestamped file."""
    raw, _ = read_user_path()
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = backup_dir / f"user-path-{stamp}.txt"
        dest.write_text(raw, encoding="utf-8")
        return dest
    except OSError:
        return None


def add_to_user_path(directory: str, backup_dir: Path) -> PathWriteResult:
    """Append directory to the persistent user PATH.

    Writes only to HKCU so no elevation is required, preserves the original
    registry value type, and refuses any write that would shorten PATH.
    """
    if winreg is None:
        return PathWriteResult(False, "PATH editing is Windows-only")

    directory = os.path.normpath(directory.strip().rstrip("\\/"))
    if not directory:
        return PathWriteResult(False, "no directory supplied")
    if not os.path.isdir(directory):
        return PathWriteResult(False, f"not a directory: {directory}")

    raw, vtype = read_user_path()
    if path_contains(directory, raw):
        _refresh_process_path(directory)
        return PathWriteResult(True, "already on PATH")
    # An installer may have put it on the machine PATH. Adding it again to the
    # user PATH would only create a duplicate entry.
    if on_persistent_path(directory):
        _refresh_process_path(directory)
        return PathWriteResult(True, "already on the system PATH")

    backup = backup_user_path(backup_dir)

    existing = raw.rstrip(os.pathsep)
    new_value = f"{existing}{os.pathsep}{directory}" if existing else directory

    # A write must strictly extend the old value; anything else is a bug.
    if len(new_value) <= len(existing):
        return PathWriteResult(
            False, "refusing to write a shorter PATH", backup=backup
        )
    if len(new_value) > MAX_PATH_CHARS:
        return PathWriteResult(
            False,
            "user PATH is near the registry size limit; prune it first",
            backup=backup,
        )

    if vtype not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        vtype = winreg.REG_EXPAND_SZ

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, ENV_SUBKEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "Path", 0, vtype, new_value)
    except OSError as exc:
        return PathWriteResult(False, f"registry write failed: {exc}", backup=backup)

    broadcast_environment_change()
    _refresh_process_path(directory)
    return PathWriteResult(
        True, f"added {directory} to user PATH", backup=backup, added=directory
    )


def _refresh_process_path(directory: str) -> None:
    """Make a new entry visible to this process without restarting it."""
    current = os.environ.get("PATH", "")
    if not path_contains(directory, current):
        os.environ["PATH"] = (
            f"{current}{os.pathsep}{directory}" if current else directory
        )


def broadcast_environment_change() -> bool:
    """Tell Explorer and new shells that the environment changed.

    Processes already running keep their inherited copy regardless; this only
    affects processes started afterwards.
    """
    if sys.platform != "win32":
        return False
    try:
        result = ctypes.c_ulong()
        sent = ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            ctypes.c_wchar_p("Environment"),
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result),
        )
        return bool(sent)
    except Exception:  # pragma: no cover - defensive
        return False


# ---------------------------------------------------------------------------
# installed applications
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InstalledApp:
    name: str
    version: str = ""
    install_location: str = ""
    display_icon: str = ""
    uninstall_string: str = ""
    source: str = "registry"
    extra: dict[str, str] = field(default_factory=dict)

    def guess_root(self) -> str:
        """Best guess at the install directory.

        InstallLocation is often blank, in which case the folder holding
        DisplayIcon is the next best signal.
        """
        if self.install_location and os.path.isdir(self.install_location):
            return self.install_location
        icon = self.display_icon.split(",")[0].strip().strip('"')
        if icon:
            parent = os.path.dirname(icon)
            if os.path.isdir(parent):
                return parent
        return ""


def scan_installed_apps(name_filter: str = "") -> list[InstalledApp]:
    """Enumerate installed programs from all three uninstall registry views.

    Covers HKLM 64-bit, HKLM 32-bit (WOW6432Node) and per-user HKCU, the last of
    which is where user-scope installs land.
    """
    if winreg is None:
        return []

    needle = name_filter.casefold()
    found: list[InstalledApp] = []
    seen: set[tuple[str, str]] = set()

    views = (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, 0),
    )

    for root, flag in views:
        try:
            with winreg.OpenKey(
                root, UNINSTALL_SUBKEY, 0, winreg.KEY_READ | flag
            ) as key:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        child = winreg.EnumKey(key, index)
                    except OSError:
                        continue
                    app = _read_uninstall_entry(key, child)
                    if app is None:
                        continue
                    if needle and needle not in app.name.casefold():
                        continue
                    ident = (app.name.casefold(), app.version)
                    if ident in seen:
                        continue
                    seen.add(ident)
                    found.append(app)
        except OSError:
            continue

    return found


def _read_uninstall_entry(parent, child: str) -> InstalledApp | None:
    try:
        with winreg.OpenKey(parent, child) as key:

            def value(name: str) -> str:
                try:
                    raw = winreg.QueryValueEx(key, name)[0]
                except (FileNotFoundError, OSError):
                    return ""
                return str(raw) if raw is not None else ""

            name = value("DisplayName")
            if not name:
                return None
            # Hotfixes and OS components clutter the list and are never a match.
            if value("SystemComponent") == "1":
                return None
            return InstalledApp(
                name=name,
                version=value("DisplayVersion"),
                install_location=value("InstallLocation").strip().strip('"'),
                display_icon=value("DisplayIcon"),
                uninstall_string=value("UninstallString"),
                extra={
                    "Publisher": value("Publisher"),
                    "RegistryKey": child,
                },
            )
    except OSError:
        return None
