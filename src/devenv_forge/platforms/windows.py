"""Windows 11 implementation of the preflight checks.

The podman lookup follows a deliberate cascade:

1. ``podman`` resolves on PATH -- nothing to do.
2. It does not, but a real ``podman.exe`` exists on disk -- the install is fine
   and only PATH is wrong, so repair PATH rather than reinstalling.
3. No executable anywhere -- install it with winget.

Stage 2 is evidence-based, never name-based. "Podman Desktop" appears in the
installed-programs list and is *not* the podman engine; the only thing that
counts as a hit is an executable that answers ``podman --version``.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from ..core import paths
from ..core.models import (
    CheckResult,
    OSFamily,
    OSInfo,
    ProgressSink,
    Remedy,
    RemedyOutcome,
    Status,
)
from ..core.runner import merge_path, run, stream, which
from . import winenv
from .base import MachinePlatform, probe_podman

#: winget package that ships the podman engine and `podman machine`.
WINGET_PODMAN_ID = "RedHat.Podman"
#: The Electron GUI. Explicitly *not* the engine; kept here to explain the
#: distinction to the user when only this is found.
WINGET_PODMAN_DESKTOP_ID = "RedHat.Podman-Desktop"

WSL_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?")
#: Windows 11 starts at build 22000.
WIN11_MIN_BUILD = 22000


class WindowsPlatform(MachinePlatform):
    family = OSFamily.WINDOWS
    installer_name = "winget"
    machine_note = (
        "The machine is a WSL2 distribution of its own, so it appears in "
        "wsl --list --verbose as podman-machine-default next to any "
        "distribution you already use."
    )

    @property
    def backend_title(self) -> str:
        return "WSL2 backend"

    # -- OS ----------------------------------------------------------------

    def detect_os(self) -> OSInfo:
        import platform as _platform

        release = _platform.release()
        version = _platform.version()
        build = version.split(".")[-1] if "." in version else ""
        arch = _platform.machine()

        build_num = 0
        try:
            build_num = int(build)
        except ValueError:
            pass

        # Python reports Windows 11 as release "10"; only the build tells them
        # apart. Read the marketing name from the registry when available.
        product = self._registry_product_name()
        is_11 = build_num >= WIN11_MIN_BUILD
        name = product or ("Windows 11" if is_11 else f"Windows {release}")

        supported = True
        notes = ""
        if build_num and not is_11:
            notes = (
                "Windows 10 detected. Podman with WSL2 generally works, but this "
                "tool is tuned for Windows 11."
            )
        if arch.lower() not in ("amd64", "x86_64", "arm64"):
            supported = False
            notes = f"Unsupported architecture: {arch}"

        return OSInfo(
            family=OSFamily.WINDOWS,
            name=name,
            version=version,
            build=build,
            arch=arch,
            is_windows_11=is_11,
            supported=supported,
            notes=notes,
        )

    @staticmethod
    def _registry_product_name() -> str:
        if sys.platform != "win32":
            return ""
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            ) as key:
                name = str(winreg.QueryValueEx(key, "ProductName")[0])
                display = ""
                try:
                    display = str(winreg.QueryValueEx(key, "DisplayVersion")[0])
                except OSError:
                    pass
                # The registry still says "Windows 10 Pro" on Windows 11.
                try:
                    build = int(winreg.QueryValueEx(key, "CurrentBuildNumber")[0])
                except (OSError, ValueError):
                    build = 0
                if build >= WIN11_MIN_BUILD and "Windows 10" in name:
                    name = name.replace("Windows 10", "Windows 11")
                return f"{name} {display}".strip()
        except OSError:
            return ""

    # -- podman ------------------------------------------------------------

    def candidate_dirs(self) -> Sequence[str]:
        env = os.environ
        program_files = env.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = env.get("LOCALAPPDATA", "")
        program_data = env.get("ProgramData", r"C:\ProgramData")
        home = Path.home()

        dirs = [
            os.path.join(program_files, "RedHat", "Podman"),
            os.path.join(program_files_x86, "RedHat", "Podman"),
            os.path.join(program_files, "Podman"),
            os.path.join(program_data, "chocolatey", "bin"),
            str(home / "scoop" / "shims"),
        ]
        if local:
            dirs.extend(
                [
                    os.path.join(local, "Programs", "RedHat", "Podman"),
                    os.path.join(local, "Microsoft", "WinGet", "Links"),
                ]
            )
        return dirs

    def check_podman(self) -> CheckResult:
        evidence: dict[str, object] = {}

        # --- 1.1 PATH ----------------------------------------------------
        on_path = self.find_podman_on_path()
        evidence["path_lookup"] = on_path or "not found on PATH"
        if on_path:
            probe = probe_podman(on_path)
            evidence["version"] = probe.version
            if probe.works:
                status = Status.REPAIRABLE if probe.too_old else Status.OK
                summary = (
                    f"podman {probe.version} at {on_path}"
                    if probe.version
                    else f"podman found at {on_path}"
                )
                result = CheckResult(
                    key="podman",
                    title="Podman CLI",
                    status=status,
                    summary=summary,
                    detail=f"Resolved through PATH.\n{on_path}",
                    evidence=evidence,
                )
                if probe.too_old:
                    result.summary = f"podman {probe.version} is older than 4.0"
                    result.remedy = self._winget_upgrade_remedy()
                return result
            evidence["probe_error"] = probe.error

        # --- 1.2 installed applications ----------------------------------
        discovered = self._discover_installed_podman(evidence)
        working = [p for p in discovered if p.works]
        if working:
            best = working[0]
            directory = os.path.dirname(best.path)
            evidence["resolved_dir"] = directory

            # A process keeps the PATH it inherited at launch, so an install
            # that happened afterwards is invisible to it. Check the registry
            # for what a newly opened terminal would see; without this the tool
            # would offer to add a user PATH entry that duplicates the machine
            # one the installer already wrote.
            if winenv.on_persistent_path(directory):
                evidence["persistent_path"] = "present"
                return CheckResult(
                    key="podman",
                    title="Podman CLI",
                    status=Status.OK,
                    summary=f"podman {best.version} at {best.path}",
                    detail=(
                        f"{directory} is already on the system PATH.\n\n"
                        "Terminals and editors opened before podman was installed "
                        "still hold the older environment, so they need reopening "
                        "before the command resolves there."
                    ),
                    evidence=evidence,
                )

            evidence["persistent_path"] = "absent"
            return CheckResult(
                key="podman",
                title="Podman CLI",
                status=Status.REPAIRABLE,
                summary=f"Installed ({best.version or 'version unknown'}) but not on PATH",
                detail=(
                    "podman is installed but PATH does not point at it, so the "
                    "command is unavailable in terminals.\n\n"
                    f"Executable: {best.path}\n"
                    f"Directory to add: {directory}"
                ),
                remedy=self._add_to_path_remedy(directory),
                evidence=evidence,
            )

        # --- 1.3 not installed -------------------------------------------
        detail = self._describe_absence(evidence)
        return CheckResult(
            key="podman",
            title="Podman CLI",
            status=Status.MISSING,
            summary="Not installed",
            detail=detail,
            remedy=self._winget_install_remedy(),
            evidence=evidence,
        )

    def _discover_installed_podman(self, evidence: dict[str, object]):
        """Look for a real podman executable outside PATH.

        Sources: the uninstall registry, winget's own inventory, and the
        directories installers conventionally use. Every candidate is executed
        before it counts, which is what keeps Podman Desktop from being mistaken
        for the engine.
        """
        roots: list[str] = []

        registry_hits = winenv.scan_installed_apps("podman")
        evidence["registry_matches"] = [
            f"{app.name} {app.version}".strip() for app in registry_hits
        ]
        for app in registry_hits:
            root = app.guess_root()
            if root:
                roots.append(root)

        winget_ids = self._winget_installed_ids()
        if winget_ids is not None:
            evidence["winget_matches"] = winget_ids

        roots.extend(self.candidate_dirs())
        roots.extend(self._winget_package_dirs())

        found: list[str] = []
        for root in roots:
            found.extend(_find_executable(root, "podman.exe"))

        unique: list[str] = []
        for item in found:
            if item not in unique:
                unique.append(item)
        evidence["executables_found"] = unique

        probes = [probe_podman(exe) for exe in unique]
        for probe in probes:
            if not probe.works and probe.error:
                evidence.setdefault("probe_failures", []).append(  # type: ignore[union-attr]
                    f"{probe.path}: {probe.error}"
                )
        return probes

    def _describe_absence(self, evidence: dict[str, object]) -> str:
        lines = ["No working podman executable was found."]
        matches = evidence.get("registry_matches") or []
        if matches:
            listed = ", ".join(str(m) for m in matches)  # type: ignore[union-attr]
            lines.append("")
            lines.append(f"Installed programs matching 'podman': {listed}")
            if any("desktop" in str(m).casefold() for m in matches):  # type: ignore[union-attr]
                lines.append(
                    "Podman Desktop is the graphical front end, not the engine. "
                    "It does not provide the podman command, so the engine still "
                    "needs to be installed."
                )
        lines.append("")
        lines.append(f"winget will install {WINGET_PODMAN_ID}.")
        return "\n".join(lines)

    def _winget_installed_ids(self) -> list[str] | None:
        """Ask winget what it has installed matching podman."""
        if not which("winget"):
            return None
        result = run(
            ["winget", "list", "--id", "Podman", "--source", "winget"], timeout=90
        )
        if not result.ok:
            return []
        ids: list[str] = []
        for line in result.lines():
            if "podman" in line.casefold():
                ids.append(line)
        return ids

    @staticmethod
    def _winget_package_dirs() -> list[str]:
        """Portable winget installs land under a per-package folder."""
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return []
        base = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if not base.is_dir():
            return []
        try:
            return [str(p) for p in base.iterdir() if p.is_dir() and "podman" in p.name.casefold()]
        except OSError:
            return []

    # -- podman remedies ---------------------------------------------------

    def _add_to_path_remedy(self, directory: str) -> Remedy:
        def action(sink: ProgressSink) -> RemedyOutcome:
            sink.step(f"Adding {directory} to the user PATH")
            result = winenv.add_to_user_path(directory, paths.backup_dir())
            if result.backup:
                sink.log(f"Previous PATH backed up to {result.backup}")
            sink.log(result.message)
            if not result.ok:
                return RemedyOutcome(False, result.message)
            self.drop_cached_podman()
            sink.log(
                "Open a new terminal for the change to be visible there; "
                "already-running shells keep their inherited PATH."
            )
            return RemedyOutcome(True, result.message, needs_restart=True)

        return Remedy(
            label="Add to PATH",
            description=(
                f"Append {directory} to your user PATH. Writes to HKCU only, so "
                "no administrator rights are needed, and the previous value is "
                "backed up first."
            ),
            action=action,
            requires_elevation=False,
            estimated="instant",
        )

    def _winget_install_remedy(self) -> Remedy:
        def action(sink: ProgressSink) -> RemedyOutcome:
            return self._winget_run(sink, "install", WINGET_PODMAN_ID)

        return Remedy(
            label="Install with winget",
            description=(
                f"Run winget install {WINGET_PODMAN_ID}. This is a machine-wide "
                "install, so Windows will ask for administrator approval."
            ),
            action=action,
            requires_elevation=True,
            estimated="2-5 minutes",
        )

    def _winget_upgrade_remedy(self) -> Remedy:
        def action(sink: ProgressSink) -> RemedyOutcome:
            return self._winget_run(sink, "upgrade", WINGET_PODMAN_ID)

        return Remedy(
            label="Upgrade with winget",
            description=f"Run winget upgrade {WINGET_PODMAN_ID}.",
            action=action,
            requires_elevation=True,
            estimated="2-5 minutes",
        )

    def _winget_run(self, sink: ProgressSink, verb: str, package: str) -> RemedyOutcome:
        if not which("winget"):
            return RemedyOutcome(
                False,
                "winget is not available. Install 'App Installer' from the "
                "Microsoft Store, then re-run this check.",
            )

        args = (
            f"{verb} --id {package} --exact --source winget "
            "--accept-package-agreements --accept-source-agreements "
            "--disable-interactivity"
        )
        sink.step(f"Running winget {verb} {package}")
        log_path = paths.log_dir() / f"winget-{verb}.log"

        script = (
            f"winget {args} 2>&1 | Tee-Object -FilePath $log\n"
            "$code = $LASTEXITCODE\n"
            "if ($code -ne 0) { Start-Sleep -Seconds 4 }\n"
            "exit $code\n"
        )
        outcome = winenv.run_elevated_script(script, log_path, on_line=sink.log)

        if outcome.cancelled:
            return RemedyOutcome(False, "Administrator approval was declined.")
        if not outcome.started:
            return RemedyOutcome(False, outcome.error or "Could not start winget.")
        if outcome.returncode not in (0, None):
            return RemedyOutcome(
                False,
                f"winget exited with code {outcome.returncode}. See {log_path}.",
            )

        # PATH set by the installer is not in this process yet; pull it in so a
        # re-check succeeds without restarting the app.
        self._reload_path_from_registry()
        self.drop_cached_podman()
        sink.step("Verifying the installation")
        found = which("podman") or (
            self.find_podman_in_known_dirs()[:1] or [""]
        )[0]
        if not found:
            return RemedyOutcome(
                True,
                "winget reported success but podman is not visible yet. "
                "Restart the application, or your machine, and re-check.",
                needs_restart=True,
            )
        probe = probe_podman(found)
        if probe.works:
            return RemedyOutcome(True, f"Installed podman {probe.version}")
        return RemedyOutcome(
            True,
            "Installed, but podman did not run cleanly yet. A restart may be needed.",
            needs_restart=True,
        )

    @staticmethod
    def _reload_path_from_registry() -> None:
        """Add the stored machine and user PATH entries to this process's own.

        Added, never substituted. This runs straight after an install, when the
        directory the installer wrote is in the registry but not yet in this
        process; replacing PATH with the registry's copy would pick that up and
        simultaneously discard everything the launching shell had added, which
        stays invisible until some unrelated tool fails to launch.
        """
        if sys.platform != "win32":
            return
        stored = [
            os.path.expandvars(raw)
            for raw in (winenv.read_machine_path(), winenv.read_user_path()[0])
            if raw
        ]
        if stored:
            os.environ["PATH"] = merge_path(os.environ.get("PATH", ""), *stored)

    # -- WSL2 --------------------------------------------------------------

    def check_backend(self) -> CheckResult:
        evidence: dict[str, object] = {}
        wsl = shutil.which("wsl") or r"C:\Windows\System32\wsl.exe"
        evidence["wsl_exe"] = wsl

        # wsl.exe ships with Windows even when the feature is off, so its mere
        # presence proves nothing. It has to actually answer.
        version_result = run([wsl, "--version"], timeout=45)
        version_text = version_result.output
        evidence["wsl_version_raw"] = version_text

        if not version_result.ok:
            status_result = run([wsl, "--status"], timeout=45)
            evidence["wsl_status_raw"] = status_result.output
            if not status_result.ok:
                return CheckResult(
                    key="backend",
                    title="WSL2 backend",
                    status=Status.MISSING,
                    summary="WSL2 is not installed",
                    detail=(
                        "Podman on Windows runs its Linux environment inside WSL2. "
                        "WSL is not responding, so it needs to be installed.\n\n"
                        f"{status_result.output or version_text}"
                    ),
                    remedy=self._wsl_install_remedy(),
                    evidence=evidence,
                )
            # Answers --status but not --version: the old inbox WSL.
            return CheckResult(
                key="backend",
                title="WSL2 backend",
                status=Status.REPAIRABLE,
                summary="Legacy WSL detected; update recommended",
                detail=(
                    "This is the in-box WSL that predates the servicing model. "
                    "Podman expects the modern WSL2 release.\n\n"
                    f"{status_result.output}"
                ),
                remedy=self._wsl_update_remedy(),
                evidence=evidence,
            )

        match = WSL_VERSION_RE.search(version_text)
        wsl_version = match.group(0) if match else ""
        evidence["wsl_version"] = wsl_version

        default_version = self._wsl_default_version()
        evidence["default_version"] = default_version
        distros = self._wsl_distros(wsl)
        evidence["distros"] = [f"{n} (v{v})" for n, v in distros]

        detail_lines = [f"WSL version: {wsl_version or 'unknown'}"]
        if default_version:
            detail_lines.append(f"Default version for new distributions: {default_version}")
        if distros:
            detail_lines.append("")
            detail_lines.append("Installed distributions:")
            detail_lines.extend(f"  {name} (WSL{ver})" for name, ver in distros)
        else:
            detail_lines.append("")
            detail_lines.append(
                "No distributions installed. That is fine: podman machine creates "
                "its own."
            )

        if default_version == 1:
            return CheckResult(
                key="backend",
                title="WSL2 backend",
                status=Status.REPAIRABLE,
                summary="WSL is installed but defaults to version 1",
                detail="\n".join(detail_lines),
                remedy=self._wsl_set_default_2_remedy(wsl),
                evidence=evidence,
            )

        major = int(wsl_version.split(".")[0]) if wsl_version else 0
        if major and major < 2:
            return CheckResult(
                key="backend",
                title="WSL2 backend",
                status=Status.REPAIRABLE,
                summary=f"WSL {wsl_version} predates WSL2",
                detail="\n".join(detail_lines),
                remedy=self._wsl_update_remedy(),
                evidence=evidence,
            )

        return CheckResult(
            key="backend",
            title="WSL2 backend",
            status=Status.OK,
            summary=f"WSL {wsl_version} ready" if wsl_version else "WSL2 ready",
            detail="\n".join(detail_lines),
            evidence=evidence,
        )

    @staticmethod
    def _wsl_default_version() -> int:
        """Read the default WSL version from the registry.

        Parsing `wsl --status` would work on English Windows only; the registry
        value is locale-independent.
        """
        if sys.platform != "win32":
            return 0
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Lxss",
            ) as key:
                return int(winreg.QueryValueEx(key, "DefaultVersion")[0])
        except (OSError, ValueError):
            return 0

    @staticmethod
    def _wsl_distros(wsl: str) -> list[tuple[str, int]]:
        result = run([wsl, "--list", "--verbose"], timeout=45)
        if not result.ok:
            return []
        return parse_wsl_distros(result.stdout)

    def _wsl_install_remedy(self) -> Remedy:
        def action(sink: ProgressSink) -> RemedyOutcome:
            sink.step("Installing WSL2")
            log_path = paths.log_dir() / "wsl-install.log"
            # --no-distribution keeps this to the kernel and platform features.
            # podman machine provisions its own distribution later.
            script = (
                "wsl.exe --install --no-distribution 2>&1 | Tee-Object -FilePath $log\n"
                "exit $LASTEXITCODE\n"
            )
            outcome = winenv.run_elevated_script(script, log_path, on_line=sink.log)
            if outcome.cancelled:
                return RemedyOutcome(False, "Administrator approval was declined.")
            if not outcome.started:
                return RemedyOutcome(False, outcome.error or "Could not start WSL setup.")
            if outcome.returncode not in (0, None):
                return RemedyOutcome(
                    False, f"WSL setup exited with code {outcome.returncode}."
                )
            return RemedyOutcome(
                True,
                "WSL2 installed. Windows requires a restart before it works.",
                needs_restart=True,
            )

        return Remedy(
            label="Install WSL2",
            description=(
                "Run wsl --install --no-distribution, which enables the virtual "
                "machine platform and installs the WSL2 kernel. Requires "
                "administrator rights and a reboot."
            ),
            action=action,
            requires_elevation=True,
            estimated="3-10 minutes plus a restart",
        )

    def _wsl_update_remedy(self) -> Remedy:
        def action(sink: ProgressSink) -> RemedyOutcome:
            sink.step("Updating WSL")
            log_path = paths.log_dir() / "wsl-update.log"
            script = (
                "wsl.exe --update 2>&1 | Tee-Object -FilePath $log\n"
                "exit $LASTEXITCODE\n"
            )
            outcome = winenv.run_elevated_script(script, log_path, on_line=sink.log)
            if outcome.cancelled:
                return RemedyOutcome(False, "Administrator approval was declined.")
            if outcome.returncode not in (0, None):
                return RemedyOutcome(
                    False, f"wsl --update exited with code {outcome.returncode}."
                )
            return RemedyOutcome(True, "WSL updated.")

        return Remedy(
            label="Update WSL",
            description="Run wsl --update to fetch the current WSL2 kernel.",
            action=action,
            requires_elevation=True,
            estimated="1-3 minutes",
        )

    def _wsl_set_default_2_remedy(self, wsl: str) -> Remedy:
        def action(sink: ProgressSink) -> RemedyOutcome:
            sink.step("Setting the default WSL version to 2")
            for kind, text in stream([wsl, "--set-default-version", "2"], timeout=60):
                if kind == "line":
                    sink.log(text)
                elif text not in ("0",):
                    return RemedyOutcome(False, f"Command exited with code {text}.")
            return RemedyOutcome(True, "Default WSL version set to 2.")

        return Remedy(
            label="Set default to WSL2",
            description="Run wsl --set-default-version 2 so new distributions use WSL2.",
            action=action,
            requires_elevation=False,
            estimated="instant",
        )

    # -- provider ----------------------------------------------------------

    def check_provider(self) -> CheckResult:
        """Confirm podman will drive WSL rather than Hyper-V.

        On Windows podman can target either. WSL is the default and the one this
        tool supports, so an explicit hyperv setting is a misconfiguration.
        """
        evidence: dict[str, object] = {}
        env_provider = os.environ.get("CONTAINERS_MACHINE_PROVIDER", "").strip()
        evidence["env"] = env_provider or "(unset)"

        conf_path = self._containers_conf_path()
        evidence["containers_conf"] = str(conf_path)
        file_provider = self._read_conf_provider(conf_path)
        evidence["file_provider"] = file_provider or "(unset)"

        effective = env_provider or file_provider or "wsl (default)"
        evidence["effective"] = effective

        if env_provider and env_provider.casefold() != "wsl":
            return CheckResult(
                key="provider",
                title="Machine provider",
                status=Status.REPAIRABLE,
                summary=f"Environment forces provider '{env_provider}'",
                detail=(
                    "CONTAINERS_MACHINE_PROVIDER overrides the configuration file "
                    "and is not set to wsl. Clear or change that variable; this "
                    "tool will not edit your environment variables for you."
                ),
                evidence=evidence,
            )

        if file_provider and file_provider.casefold() != "wsl":
            return CheckResult(
                key="provider",
                title="Machine provider",
                status=Status.REPAIRABLE,
                summary=f"containers.conf selects '{file_provider}'",
                detail=(
                    f"{conf_path} sets the machine provider to {file_provider}. "
                    "Podman would use Hyper-V instead of WSL2."
                ),
                remedy=self._set_provider_remedy(conf_path),
                evidence=evidence,
            )

        return CheckResult(
            key="provider",
            title="Machine provider",
            status=Status.OK,
            summary=f"Provider: {effective}",
            detail=(
                f"Configuration file: {conf_path}\n"
                f"CONTAINERS_MACHINE_PROVIDER: {evidence['env']}\n"
                "Podman will run its machine on WSL2."
            ),
            evidence=evidence,
        )

    @staticmethod
    def _containers_conf_path() -> Path:
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "containers" / "containers.conf"

    @staticmethod
    def _read_conf_provider(conf_path: Path) -> str:
        if not conf_path.is_file():
            return ""
        try:
            import tomllib

            with conf_path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, ValueError):
            return ""
        machine = data.get("machine")
        if isinstance(machine, dict):
            provider = machine.get("provider")
            if isinstance(provider, str):
                return provider.strip()
        return ""

    def _set_provider_remedy(self, conf_path: Path) -> Remedy:
        def action(sink: ProgressSink) -> RemedyOutcome:
            sink.step(f"Setting machine provider to wsl in {conf_path}")
            try:
                conf_path.parent.mkdir(parents=True, exist_ok=True)
                if conf_path.is_file():
                    original = conf_path.read_text(encoding="utf-8")
                    backup = paths.backup_dir()
                    backup.mkdir(parents=True, exist_ok=True)
                    target = backup / "containers.conf.bak"
                    target.write_text(original, encoding="utf-8")
                    sink.log(f"Backed up to {target}")
                    updated = _rewrite_provider(original)
                else:
                    updated = '[machine]\nprovider = "wsl"\n'
                conf_path.write_text(updated, encoding="utf-8")
            except OSError as exc:
                return RemedyOutcome(False, f"Could not write {conf_path}: {exc}")
            return RemedyOutcome(True, "Machine provider set to wsl.")

        return Remedy(
            label="Use WSL provider",
            description=f"Set provider = wsl under [machine] in {conf_path}.",
            action=action,
            requires_elevation=False,
            estimated="instant",
        )


def parse_wsl_distros(stdout: str) -> list[tuple[str, int]]:
    """Parse `wsl --list --verbose` without depending on the display language.

    The STATE column is localised; NAME and VERSION are not. Reading positionally
    from the end of each row keeps this working on a non-English Windows.
    """
    rows: list[tuple[str, int]] = []
    for index, line in enumerate(stdout.splitlines()):
        text = line.strip()
        if not text or index == 0:  # first row is the header
            continue
        tokens = text.lstrip("*").strip().split()
        if len(tokens) < 3:
            continue
        try:
            version = int(tokens[-1])
        except ValueError:
            continue
        name = " ".join(tokens[:-2])
        if name:
            rows.append((name, version))
    return rows


def _rewrite_provider(original: str) -> str:
    """Set provider = "wsl" inside [machine], preserving everything else.

    A minimal textual edit rather than a TOML round-trip: the standard library
    can read TOML but not write it, and rewriting the file wholesale would drop
    the user's comments.
    """
    lines = original.splitlines()
    out: list[str] = []
    in_machine = False
    wrote = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_machine and not wrote:
                out.append('provider = "wsl"')
                wrote = True
            in_machine = stripped == "[machine]"
        if in_machine and re.match(r"^\s*provider\s*=", line):
            if not wrote:
                out.append('provider = "wsl"')
                wrote = True
            continue
        out.append(line)

    if in_machine and not wrote:
        out.append('provider = "wsl"')
        wrote = True
    if not wrote:
        if out and out[-1].strip():
            out.append("")
        out.append("[machine]")
        out.append('provider = "wsl"')
    return "\n".join(out) + "\n"


def _find_executable(root: str, name: str, max_depth: int = 3) -> list[str]:
    """Find *name* under *root*, bounded so a wrong root cannot walk the disk."""
    if not root or not os.path.isdir(root):
        return []
    hits: list[str] = []
    root_path = Path(root)
    direct = root_path / name
    if direct.is_file():
        hits.append(str(direct))
    # Installers commonly use a bin/ subdirectory.
    try:
        for depth in range(1, max_depth + 1):
            pattern = "/".join(["*"] * depth) + "/" + name
            for match in root_path.glob(pattern):
                if match.is_file() and str(match) not in hits:
                    hits.append(str(match))
    except OSError:
        pass
    return hits
