"""Subprocess helpers that behave in a GUI process on Windows.

Two things bite here and both are handled centrally:

* ``wsl.exe`` writes **UTF-16LE** to stdout, so a naive ``text=True`` decode
  yields text with a NUL between every character. :func:`smart_decode` sniffs
  the encoding instead of guessing.
* A GUI process spawning a console tool pops a black window on every call.
  ``CREATE_NO_WINDOW`` suppresses it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

#: Keeps console windows from flashing when the GUI shells out.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def smart_decode(data: bytes) -> str:
    """Decode process output without knowing the encoding up front.

    Order: explicit BOM, then a NUL-density test for BOM-less UTF-16LE (what
    ``wsl.exe`` produces), then UTF-8, then the OS default with replacement so
    this never raises.
    """
    if not data:
        return ""
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16-le", errors="replace").lstrip("﻿")
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16-be", errors="replace").lstrip("﻿")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")

    # BOM-less UTF-16LE: ASCII text becomes 'a\x00b\x00', so odd-indexed bytes
    # are almost all NUL. Require a decent sample to avoid false positives on
    # short binary-ish output.
    sample = data[:4096]
    if len(sample) >= 8:
        odd = sample[1::2]
        if odd and (odd.count(0) / len(odd)) > 0.7:
            return data.decode("utf-16-le", errors="replace").lstrip("﻿")

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(_fallback_encoding(), errors="replace")


def _fallback_encoding() -> str:
    if IS_WINDOWS:
        return "mbcs"
    return sys.getfilesystemencoding() or "utf-8"


@dataclass(slots=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    launch_error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.launch_error

    @property
    def output(self) -> str:
        """stdout plus stderr, for tools that report to either."""
        return "\n".join(p for p in (self.stdout.strip(), self.stderr.strip()) if p)

    def lines(self) -> list[str]:
        return [ln.strip() for ln in self.stdout.splitlines() if ln.strip()]


def run(
    argv: Sequence[str],
    *,
    timeout: float = 60.0,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run a command and capture its output. Never raises on failure."""
    argv = [str(a) for a in argv]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
            creationflags=_NO_WINDOW,
            shell=False,
        )
    except FileNotFoundError as exc:
        return CommandResult(argv, 127, "", "", launch_error=str(exc))
    except PermissionError as exc:
        return CommandResult(argv, 126, "", "", launch_error=str(exc))
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            argv,
            -1,
            smart_decode(exc.stdout or b""),
            smart_decode(exc.stderr or b""),
            timed_out=True,
        )
    except OSError as exc:  # pragma: no cover - defensive
        return CommandResult(argv, 1, "", "", launch_error=str(exc))

    return CommandResult(
        argv,
        proc.returncode,
        smart_decode(proc.stdout),
        smart_decode(proc.stderr),
    )


def stream(
    argv: Sequence[str],
    *,
    timeout: float = 1800.0,
    cwd: str | None = None,
) -> Iterable[tuple[str, str]]:
    """Yield ``(kind, text)`` as a long command runs.

    ``kind`` is ``"line"`` for output and ``"exit"`` for the final status, whose
    text is the numeric return code. Installers take minutes, so the UI needs
    output as it arrives rather than at the end.
    """
    argv = [str(a) for a in argv]
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            creationflags=_NO_WINDOW,
            shell=False,
        )
    except OSError as exc:
        yield ("line", f"failed to launch: {exc}")
        yield ("exit", "127")
        return

    assert proc.stdout is not None
    buffer = b""
    try:
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            # Installers use \r to redraw progress; treat it as a line break so
            # the log advances instead of stalling on one enormous line.
            if chunk in (b"\n", b"\r"):
                text = smart_decode(buffer).strip()
                buffer = b""
                if text:
                    yield ("line", text)
            else:
                buffer += chunk
        tail = smart_decode(buffer).strip()
        if tail:
            yield ("line", tail)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        yield ("line", "timed out; process killed")
        yield ("exit", "-1")
        return
    finally:
        if proc.stdout:
            proc.stdout.close()

    yield ("exit", str(proc.returncode))


def normalise_path_entry(entry: str) -> str:
    """Canonical form used to compare two PATH entries.

    Case, surrounding quotes and a trailing separator all vary between the
    registry's copy of PATH and the one a process inherited, and none of them
    make two entries different directories.
    """
    expanded = os.path.expandvars(entry.strip().strip('"'))
    if not expanded:
        return ""
    return os.path.normcase(os.path.normpath(expanded)).rstrip("\\/")


def merge_path(current: str, *additions: str) -> str:
    """Append whatever *additions* hold that *current* does not already have.

    Each addition is a PATH-shaped string of one or more entries. Overwriting
    PATH with a stored copy is the obvious way to pick up a new install and the
    wrong one: a process inherits entries from the shell that launched it which
    were never written to the registry, and replacing the value throws those
    away for the rest of the session -- silently, until something fails to
    launch. So *current* is kept verbatim, keeping its entries' precedence, and
    only genuinely new entries are appended. Returns *current* unchanged when
    there are none.
    """
    seen = {
        key
        for key in (normalise_path_entry(e) for e in current.split(os.pathsep))
        if key
    }
    fresh: list[str] = []
    for addition in additions:
        for entry in addition.split(os.pathsep):
            key = normalise_path_entry(entry)
            if not key or key in seen:
                continue
            seen.add(key)
            fresh.append(entry.strip())
    if not fresh:
        return current
    tail = os.pathsep.join(fresh)
    trimmed = current.rstrip(os.pathsep)
    return f"{trimmed}{os.pathsep}{tail}" if trimmed else tail


def which(name: str, extra_paths: Sequence[str] | None = None) -> str | None:
    """Locate an executable, optionally searching beyond the current PATH."""
    found = shutil.which(name)
    if found:
        return found
    if extra_paths:
        search = os.pathsep.join(p for p in extra_paths if p)
        return shutil.which(name, path=search)
    return None
