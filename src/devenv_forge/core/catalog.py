"""The distro and package catalogue behind the comparison matrix.

The package list mirrors a working MSYS2 embedded-development setup. Each entry
records the binary package name on every distro, because the names diverge a
lot and the name is half the answer: Ninja is ``ninja-build`` on Debian and
Fedora, the Arm compiler is ``gcc-arm-none-eabi`` on Debian, but
``arm-none-eabi-gcc-cs`` on Fedora and ``arm-none-eabi-gcc`` on Arch.

Versions are resolved at runtime, so nothing here goes stale. Only names live
in this file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum


class Cadence(str, Enum):
    """How a distro moves, which matters as much as the version numbers."""

    LTS = "lts"
    STABLE = "stable"
    FAST = "fast"
    ROLLING = "rolling"


@dataclass(frozen=True, slots=True)
class Arch:
    """An architecture an image can be built for.

    Not the machine doing the building. Podman will build for either, but a
    foreign one runs every command in the image under emulation, which is why
    the choice is worth making deliberately rather than inheriting.
    """

    key: str
    label: str
    #: What podman calls it, for ``--platform`` and the ``FROM`` prefix.
    platform: str
    #: What ``uname -m`` reports inside a container of this architecture.
    uname: str
    note: str = ""


ARCHES: tuple[Arch, ...] = (
    Arch(
        key="amd64",
        label="x86-64 (amd64)",
        platform="linux/amd64",
        uname="x86_64",
        note="Intel and AMD machines, and nearly every CI runner.",
    ),
    Arch(
        key="arm64",
        label="ARM64 (aarch64)",
        platform="linux/arm64",
        uname="aarch64",
        note="Raspberry Pi 5, Apple silicon Macs, AWS Graviton.",
    ),
)

ARCHES_BY_KEY: dict[str, Arch] = {arch.key: arch for arch in ARCHES}
ARCH_KEYS: tuple[str, ...] = tuple(arch.key for arch in ARCHES)


def host_arch() -> Arch:
    """The architecture this machine builds without emulation.

    Windows reports AMD64 or ARM64, Linux and macOS x86_64 or aarch64/arm64;
    anything unrecognised is taken as x86-64, which is what it will be.
    """
    import platform as _platform

    if _platform.machine().lower() in ("arm64", "aarch64"):
        return ARCHES_BY_KEY["arm64"]
    return ARCHES_BY_KEY["amd64"]


@dataclass(frozen=True, slots=True)
class Distro:
    key: str
    label: str
    base_image: str
    package_manager: str
    install_cmd: str
    cadence: Cadence
    support: str
    #: Architectures this base image is published for. Building for one it does
    #: not have fails at the pull, not at the end of a long build.
    arches: tuple[str, ...] = ARCH_KEYS
    #: Run before installing, where the package manager needs a refresh.
    update_cmd: str = ""
    #: Run after installing, to keep the image layer small.
    clean_cmd: str = ""
    #: Fetching the rustup installer needs these.
    tls_packages: tuple[str, ...] = ("curl", "ca-certificates")
    #: Set when versions come from a stand-in rather than the product itself.
    proxy_note: str = ""
    caveat: str = ""

    def supports(self, arch_key: str) -> bool:
        return arch_key in self.arches

    def install_run(self, packages: Sequence[str], indent: str = "    ") -> str:
        """Build the RUN body that installs *packages* on this distro.

        One package per line so a diff shows exactly which package changed,
        with the refresh and cleanup steps folded into the same layer.
        """
        if not packages:
            return ""
        cont = " \\\n"
        listed = cont.join(f"{indent}{name}" for name in packages)
        body = ""
        if self.update_cmd:
            body += f"{self.update_cmd}{cont} && "
        body += f"{self.install_cmd}{cont}{listed}"
        if self.clean_cmd:
            body += f"{cont} && {self.clean_cmd}"
        return body


DISTROS: tuple[Distro, ...] = (
    Distro(
        key="ubuntu",
        label="Ubuntu 26.04",
        base_image="ubuntu:26.04",
        package_manager="apt",
        install_cmd="apt-get install -y --no-install-recommends",
        cadence=Cadence.LTS,
        support="5 years, to 2031",
        update_cmd="apt-get update",
        clean_cmd="rm -rf /var/lib/apt/lists/*",
    ),
    Distro(
        key="debian",
        label="Debian 13",
        base_image="debian:13-slim",
        package_manager="apt",
        install_cmd="apt-get install -y --no-install-recommends",
        cadence=Cadence.STABLE,
        support="approx. 5 years with LTS, to 2030",
        update_cmd="apt-get update",
        clean_cmd="rm -rf /var/lib/apt/lists/*",
    ),
    Distro(
        key="fedora",
        label="Fedora 44",
        base_image="fedora:44",
        package_manager="dnf",
        install_cmd="dnf install -y",
        cadence=Cadence.FAST,
        support="13 months",
        clean_cmd="dnf clean all",
    ),
    Distro(
        key="rhel",
        label="RHEL 10",
        base_image="redhat/ubi10",
        package_manager="dnf",
        install_cmd="dnf install -y",
        cadence=Cadence.STABLE,
        support="10 years, to 2035",
        clean_cmd="dnf clean all",
        proxy_note=(
            "Red Hat publishes no open package index, so these are read from "
            "the CentOS Stream 10 repodata, which RHEL 10 is built from, plus "
            "EPEL 10. Package names and versions are exact; RHEL itself may "
            "lag Stream slightly."
        ),
    ),
    Distro(
        key="alpine",
        label="Alpine 3.24",
        base_image="alpine:3.24",
        package_manager="apk",
        install_cmd="apk add --no-cache",
        cadence=Cadence.STABLE,
        support="2 years",
        caveat=(
            "musl libc, not glibc. Prebuilt vendor toolchains and most "
            "commercial SDKs are built against glibc and will not run here."
        ),
    ),
    Distro(
        key="arch",
        label="Arch Linux",
        base_image="archlinux:latest",
        package_manager="pacman",
        # -Syu in one command, never -Sy followed by a separate -S. Refreshing
        # the database without upgrading leaves a partial upgrade, which on a
        # rolling distro breaks dynamic linking in ways that surface later.
        install_cmd="pacman -Syu --noconfirm",
        cadence=Cadence.ROLLING,
        support="rolling, no version to pin",
        # The base image ships no local signing key, so when -Syu upgrades
        # archlinux-keyring its install hook fails with "There is no secret key
        # available to sign with". The build carries on, but the keyring is left
        # half-updated. Initialising it first makes the hook succeed.
        update_cmd="pacman-key --init && pacman-key --populate archlinux",
        clean_cmd="pacman -Scc --noconfirm",
        # Arch itself targets x86-64; the ARM ports are separate projects with
        # their own images, so the official one has no arm64 manifest at all.
        arches=("amd64",),
        caveat=(
            "Rolling. Newest of everything today, different tomorrow, which "
            "works against a reproducible team image. x86-64 only: the "
            "official image publishes no ARM64 build."
        ),
    ),
)

DISTROS_BY_KEY = {d.key: d for d in DISTROS}
DISTRO_KEYS = tuple(d.key for d in DISTROS)


@dataclass(frozen=True, slots=True)
class PackageSpec:
    """One tool and its binary package name on each distro.

    ``names`` maps a distro key to candidate binary package names, tried in
    order so a distro that renamed a package still resolves.
    """

    key: str
    label: str
    #: The MSYS2 package this came from, so the mapping stays auditable. Empty
    #: for a package added for the image rather than taken from that list.
    msys2: str
    group: str
    names: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Repology project ids, used only for the RHEL fallback.
    projects: tuple[str, ...] = ()
    meta: bool = False
    note: str = ""

    def names_for(self, distro_key: str) -> tuple[str, ...]:
        return self.names.get(distro_key, ())


GROUP_ARM = "Arm bare-metal toolchain"
GROUP_EMU = "Emulation and debug"
GROUP_HOST = "Host toolchain"
GROUP_BUILD = "Build system"
GROUP_LANG = "Languages and runtimes"
GROUP_DOCS = "Docs and coverage"


def _n(ubuntu, debian, fedora, rhel, alpine, arch) -> dict[str, tuple[str, ...]]:
    """Build a name map, accepting a string or a tuple of candidates each."""
    def as_tuple(value):
        return (value,) if isinstance(value, str) else tuple(value)

    return {
        "ubuntu": as_tuple(ubuntu),
        "debian": as_tuple(debian),
        "fedora": as_tuple(fedora),
        "rhel": as_tuple(rhel),
        "alpine": as_tuple(alpine),
        "arch": as_tuple(arch),
    }


def _all(name: str) -> dict[str, tuple[str, ...]]:
    """Same package name everywhere."""
    return {key: (name,) for key in DISTRO_KEYS}


CATALOG: tuple[PackageSpec, ...] = (
    # -- Arm bare-metal toolchain -------------------------------------------
    PackageSpec(
        key="arm-gcc",
        label="arm-none-eabi-gcc",
        msys2="mingw-w64-ucrt-x86_64-arm-none-eabi-gcc",
        group=GROUP_ARM,
        names=_n(
            "gcc-arm-none-eabi", "gcc-arm-none-eabi",
            ("arm-none-eabi-gcc-cs", "arm-none-eabi-gcc"),
            ("arm-none-eabi-gcc-cs", "arm-none-eabi-gcc"),
            "gcc-arm-none-eabi", "arm-none-eabi-gcc",
        ),
        projects=("gcc",),
    ),
    PackageSpec(
        key="arm-binutils",
        label="arm-none-eabi-binutils",
        msys2="mingw-w64-ucrt-x86_64-arm-none-eabi-binutils",
        group=GROUP_ARM,
        names=_n(
            "binutils-arm-none-eabi", "binutils-arm-none-eabi",
            ("arm-none-eabi-binutils-cs", "arm-none-eabi-binutils"),
            ("arm-none-eabi-binutils-cs", "arm-none-eabi-binutils"),
            "binutils-arm-none-eabi", "arm-none-eabi-binutils",
        ),
        projects=("binutils",),
    ),
    PackageSpec(
        key="arm-gdb",
        label="arm-none-eabi-gdb",
        msys2="mingw-w64-ucrt-x86_64-arm-none-eabi-gdb",
        group=GROUP_ARM,
        names=_n(
            ("gdb-arm-none-eabi", "gdb-multiarch"),
            ("gdb-arm-none-eabi", "gdb-multiarch"),
            "arm-none-eabi-gdb", "arm-none-eabi-gdb",
            ("gdb-arm-none-eabi", "gdb-multiarch"), "arm-none-eabi-gdb",
        ),
        projects=("gdb",),
        note="Debian, Ubuntu and Alpine dropped the dedicated build; gdb-multiarch covers Arm targets.",
    ),
    PackageSpec(
        key="arm-newlib",
        label="arm-none-eabi-newlib",
        msys2="mingw-w64-ucrt-x86_64-arm-none-eabi-newlib",
        group=GROUP_ARM,
        names=_n(
            "libnewlib-arm-none-eabi", "libnewlib-arm-none-eabi",
            "arm-none-eabi-newlib", "arm-none-eabi-newlib",
            "newlib-arm-none-eabi", "arm-none-eabi-newlib",
        ),
        projects=("arm-none-eabi-newlib", "newlib"),
    ),
    # -- emulation and debug -------------------------------------------------
    PackageSpec(
        key="qemu",
        label="QEMU (system, Arm)",
        msys2="mingw-w64-ucrt-x86_64-qemu",
        group=GROUP_EMU,
        names=_n(
            "qemu-system-arm", "qemu-system-arm", "qemu-system-arm",
            ("qemu-kvm", "qemu-system-arm"), "qemu-system-arm", "qemu-system-arm",
        ),
        projects=("qemu",),
    ),
    PackageSpec(
        key="gdb", label="GDB (host)", msys2="mingw-w64-ucrt-x86_64-gdb",
        group=GROUP_EMU, names=_all("gdb"), projects=("gdb",),
    ),
    PackageSpec(
        key="openocd", label="OpenOCD", msys2="mingw-w64-ucrt-x86_64-openocd",
        group=GROUP_EMU, names=_all("openocd"), projects=("openocd",),
    ),
    # -- host toolchain ------------------------------------------------------
    PackageSpec(
        key="gcc", label="GCC (host)", msys2="mingw-w64-ucrt-x86_64-toolchain",
        group=GROUP_HOST, names=_all("gcc"), projects=("gcc",),
    ),
    PackageSpec(
        key="binutils", label="binutils (host)", msys2="mingw-w64-ucrt-x86_64-toolchain",
        group=GROUP_HOST, names=_all("binutils"), projects=("binutils",),
    ),
    PackageSpec(
        key="base-devel",
        label="Build essentials",
        msys2="base-devel",
        group=GROUP_HOST,
        meta=True,
        names=_n(
            "build-essential", "build-essential",
            "@development-tools", '@"Development Tools"',
            "alpine-sdk", "base-devel",
        ),
        note="A meta-package pulling in the compiler and build tools. It has no version of its own.",
    ),
    # -- build system --------------------------------------------------------
    PackageSpec(
        key="cmake", label="CMake", msys2="mingw-w64-ucrt-x86_64-cmake",
        group=GROUP_BUILD, names=_all("cmake"), projects=("cmake",),
    ),
    PackageSpec(
        key="ninja", label="Ninja", msys2="mingw-w64-ucrt-x86_64-ninja",
        group=GROUP_BUILD,
        names=_n("ninja-build", "ninja-build", "ninja-build", "ninja-build",
                 ("ninja-build", "ninja", "samurai"), "ninja"),
        projects=("ninja",),
    ),
    PackageSpec(
        key="make", label="GNU Make", msys2="make",
        group=GROUP_BUILD, names=_all("make"), projects=("make",),
    ),
    PackageSpec(
        key="git", label="Git", msys2="git",
        group=GROUP_BUILD, names=_all("git"), projects=("git",),
    ),
    # -- languages -----------------------------------------------------------
    PackageSpec(
        key="python", label="Python", msys2="mingw-w64-ucrt-x86_64-python",
        group=GROUP_LANG,
        names=_n("python3", "python3", "python3", "python3", "python3", "python"),
        projects=("python",),
        note="The default interpreter the distro installs, not the newest it packages.",
    ),
    PackageSpec(
        key="pip", label="pip", msys2="mingw-w64-ucrt-x86_64-python-pip",
        group=GROUP_LANG,
        names=_n("python3-pip", "python3-pip", "python3-pip", "python3-pip",
                 "py3-pip", "python-pip"),
        projects=("pip",),
    ),
    PackageSpec(
        key="uv", label="uv", msys2="mingw-w64-ucrt-x86_64-uv",
        group=GROUP_LANG, names=_all("uv"), projects=("uv",),
        note="Moves fast. Where a distro has not packaged it, install the upstream binary.",
    ),
    PackageSpec(
        key="rust", label="Rust", msys2="mingw-w64-ucrt-x86_64-rust",
        group=GROUP_LANG,
        names=_n("rustc", "rustc", "rust", "rust", "rust", "rust"),
        projects=("rust",),
        note="For a pinned toolchain, rustup usually beats distro packages.",
    ),
    # -- docs and coverage ---------------------------------------------------
    PackageSpec(
        key="doxygen", label="Doxygen", msys2="mingw-w64-ucrt-x86_64-doxygen",
        group=GROUP_DOCS, names=_all("doxygen"), projects=("doxygen",),
    ),
    PackageSpec(
        key="graphviz", label="Graphviz", msys2="mingw-w64-ucrt-x86_64-graphviz",
        group=GROUP_DOCS, names=_all("graphviz"), projects=("graphviz",),
    ),
    PackageSpec(
        key="lcov", label="LCOV", msys2="mingw-w64-ucrt-x86_64-lcov",
        group=GROUP_DOCS, names=_all("lcov"), projects=("lcov",),
    ),
    PackageSpec(
        key="json-xs",
        label="JSON::XS for LCOV",
        # Not from the MSYS2 list: added for the image, where lcov runs on Linux.
        msys2="",
        group=GROUP_DOCS,
        # In lcov's own order of preference: JSON::XS, then its Cpanel fork.
        names=_n(
            ("libjson-xs-perl", "libcpanel-json-xs-perl"),
            ("libjson-xs-perl", "libcpanel-json-xs-perl"),
            ("perl-JSON-XS", "perl-Cpanel-JSON-XS"),
            ("perl-JSON-XS", "perl-Cpanel-JSON-XS"),
            ("perl-json-xs", "perl-cpanel-json-xs"),
            ("perl-json-xs", "perl-cpanel-json-xs"),
        ),
        projects=("perl:json-xs", "perl:cpanel-json-xs"),
        note=(
            "lcov parses gcov's JSON with it. Without it lcov 2 falls back to "
            "JSON::PP, warns on every run, and captures several times slower. "
            "No distro makes it a hard dependency of lcov."
        ),
    ),
)

CATALOG_BY_KEY = {p.key: p for p in CATALOG}

ALL_PROJECTS: tuple[str, ...] = tuple(
    dict.fromkeys(project for spec in CATALOG for project in spec.projects)
)


def projects_for_name(name: str) -> tuple[str, ...]:
    """Repology projects that might contain a given binary package name."""
    for spec in CATALOG:
        for names in spec.names.values():
            if name in names:
                return spec.projects
    return ()


def names_for_distro(distro_key: str) -> list[str]:
    """Every candidate package name to query for one distro."""
    collected: list[str] = []
    for spec in CATALOG:
        if spec.meta:
            continue
        for name in spec.names_for(distro_key):
            if name not in collected:
                collected.append(name)
    return collected


def groups() -> list[tuple[str, list[PackageSpec]]]:
    ordered: dict[str, list[PackageSpec]] = {}
    for spec in CATALOG:
        ordered.setdefault(spec.group, []).append(spec)
    return list(ordered.items())
