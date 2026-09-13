"""DevEnv Forge - build and share Podman development environment images."""

from __future__ import annotations

import sys

__version__ = "0.1.0"


def main() -> int:
    """Entry point. Launches the GUI, or runs a headless check with --check."""
    argv = sys.argv[1:]

    # A shell without APPDATA hides podman's connections from every child
    # process, so restore it before any mode shells out.
    from .core.podman import repair_environment

    repair_environment()

    if "--help" in argv or "-h" in argv:
        print(_USAGE)
        return 0
    if "--version" in argv:
        print(f"devenv-forge {__version__}")
        return 0
    if "--check" in argv:
        return _headless_check(verbose="--verbose" in argv or "-v" in argv)
    if "--compare" in argv:
        return _headless_compare(refresh="--refresh" in argv)

    from .app import run

    return run()


_USAGE = """devenv-forge - Podman development environment images

Usage:
  devenv-forge              Launch the graphical application
  devenv-forge --check      Run the environment preflight and print the result
  devenv-forge --check -v   As above, including the collected evidence
  devenv-forge --compare    Print the distro package comparison matrix
  devenv-forge --compare --refresh   As above, bypassing the cache
  devenv-forge --version
"""


def _headless_compare(refresh: bool = False) -> int:
    """Print the distro comparison as a table."""
    from .core.catalog import DISTROS, groups
    from .core.matrix import build_matrix
    from .core.paths import data_dir, ensure_dirs

    ensure_dirs()
    result = build_matrix(data_dir() / "cache", refresh=refresh)
    by_key = {row.spec.key: row for row in result.rows}

    name_width = max(len(s.label) for _g, specs in groups() for s in specs) + 2
    cell_width = 17
    header = "package".ljust(name_width) + "".join(
        d.label.rjust(cell_width) for d in DISTROS
    )
    print(header)
    print("-" * len(header))

    for group_name, specs in groups():
        print(f"\n{group_name}")
        for spec in specs:
            row = by_key.get(spec.key)
            if row is None:
                continue
            newest = row.newest_keys()
            line = f"  {spec.label}".ljust(name_width)
            for distro in DISTROS:
                cell = row.cells[distro.key]
                text = "meta" if cell.meta else cell.display
                if distro.key in newest:
                    text += " *"
                line += text.rjust(cell_width)
            print(line)

    print()
    for distro in DISTROS:
        have, total = result.coverage(distro.key)
        seconds = result.timings.get(distro.key, 0.0)
        timing = "cached" if seconds == 0 else f"{seconds:5.2f}s"
        print(
            f"  {distro.label:<16} {have}/{total} packages   "
            f"{timing:>7}   {distro.base_image}"
        )
    print(f"\n  resolved in {result.elapsed:.2f}s")
    print("  * newest among the compared distributions")
    for distro in DISTROS:
        if distro.proxy_note:
            print(f"\n  {distro.label}: {distro.proxy_note}")
    for error in result.errors:
        print(f"\n  warning: {error}")
    return 0


def _headless_check(verbose: bool = False) -> int:
    """Run the preflight without Qt.

    Useful on a build agent, over SSH, or when the GUI itself is the thing that
    will not start. Exit code 0 means nothing needs attention.
    """
    from .core.models import Status
    from .core.paths import ensure_dirs
    from .platforms import create_platform

    ensure_dirs()
    try:
        platform = create_platform()
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 2

    print(f"Platform : {platform.os_info.pretty}")
    print(f"Installer: {platform.installer_name or 'n/a'}")
    print()

    results = platform.run_all()
    for result in results:
        print(f"[{result.status.value.upper():<11}] {result.title}: {result.summary}")
        if result.detail:
            for line in result.detail.splitlines():
                print(f"{'':>14}{line}")
        if result.remedy is not None:
            admin = " [administrator]" if result.remedy.requires_elevation else ""
            print(f"{'':>14}fix: {result.remedy.label}{admin}")
        if verbose and result.evidence:
            for key, value in result.evidence.items():
                print(f"{'':>14}. {key}: {value}")
        print()

    overall = Status.worst(r.status for r in results)
    print(f"Overall: {overall.value}")
    return 1 if overall.needs_action or overall is Status.FAILED else 0
