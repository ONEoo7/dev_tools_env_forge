"""Extras: toolchain pieces that no distribution packages.

None of these are distro packages, so they cannot appear in the base image
comparison. They are added to the image after the distro packages.

Three families live here. Most come from rustup and cargo, and those need a
rustup bootstrap: a distro's ``rustc`` package does not provide rustup, so
``rustup target add`` fails against it. The second is an SDK checked out at a
release tag, which needs no Rust at all, so selecting one must not drag a
toolchain into the image. :data:`RUST_KINDS` is what keeps those apart.

The third is a build host set up to an upstream project's own documented
requirements. Those requirements name a distribution's packages, so such an
extra is offered on the distributions it was written for and nowhere else --
:attr:`Extra.distros`.

The fourth is plain distribution packages that the comparison catalogue
deliberately leaves out, because only some images want them. Every catalogue
row goes into every image; these wait to be asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ExtraKind(str, Enum):
    RUSTUP_TARGET = "rustup target"
    RUSTUP_COMPONENT = "rustup component"
    CARGO_INSTALL = "cargo install"
    #: Source checked out at a release tag, not installed from a package
    #: manager and not part of the Rust toolchain.
    SDK = "sdk"
    #: A distribution configured to somebody else's documented build-host
    #: requirements: their package list and the settings they insist on.
    #: Nothing is fetched, so there is no version to pin.
    BUILD_HOST = "build host"
    #: Distribution packages kept out of the image unless asked for, because
    #: they are large or serve one workflow. No command, no configuration.
    PACKAGES = "packages"


#: The kinds that need rustup present. An SDK checkout does not, so adding one
#: must not drag a Rust toolchain into the image.
RUST_KINDS = frozenset(
    {ExtraKind.RUSTUP_TARGET, ExtraKind.RUSTUP_COMPONENT, ExtraKind.CARGO_INSTALL}
)


@dataclass(frozen=True, slots=True)
class Extra:
    key: str
    label: str
    kind: ExtraKind
    #: The literal command written into the image.
    command: str
    #: Off by default when it is situational rather than generally wanted.
    default_on: bool = True
    note: str = ""
    #: Keys of extras that must be installed first.
    requires: tuple[str, ...] = ()
    #: Distro packages this extra needs in order to build, keyed by distro.
    build_packages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Distro keys this extra is offered on; empty means all of them. A
    #: requirement list written for one distribution does not travel: the
    #: package names are that distribution's, so offering it elsewhere would
    #: only produce an image that fails to build.
    distros: tuple[str, ...] = ()
    #: Architecture keys this extra can be built for; empty means all of them.
    #: Some requirements are architecture-specific in a way no package rename
    #: fixes -- a 32-bit x86 multilib has no ARM64 equivalent at all.
    arches: tuple[str, ...] = ()
    #: "owner/name" on GitHub, for extras pinned to a release tag.
    repo: str = ""
    #: Tag used when the GitHub lookup cannot run, so the file still builds.
    fallback_tag: str = ""
    #: Environment variables the extra needs, written as ENV lines.
    env: tuple[tuple[str, str], ...] = ()
    #: Shell test proving the extra landed, folded into the final smoke test.
    verify: str = ""

    @property
    def needs_rust(self) -> bool:
        return self.kind in RUST_KINDS

    def applies_to(self, distro_key: str, arch_key: str = "") -> bool:
        if self.distros and distro_key not in self.distros:
            return False
        return not (arch_key and self.arches and arch_key not in self.arches)

    @property
    def run_line(self) -> str:
        return self.command


#: Packages needed to build probe-rs-tools from source. It links against udev
#: for device enumeration and needs pkg-config to find it.
_PROBE_RS_PACKAGES = {
    "ubuntu": ("libudev-dev", "pkg-config", "libusb-1.0-0-dev"),
    "debian": ("libudev-dev", "pkg-config", "libusb-1.0-0-dev"),
    "fedora": ("systemd-devel", "pkgconf-pkg-config", "libusb1-devel"),
    "rhel": ("systemd-devel", "pkgconf-pkg-config", "libusb1-devel"),
    "alpine": ("eudev-dev", "pkgconf", "libusb-dev"),
    "arch": ("systemd-libs", "pkgconf", "libusb"),
}


#: picotool links against libusb for the commands that talk to a board over
#: USB. Without it picotool still builds, but load, save and reboot are gone.
_PICOTOOL_PACKAGES = {
    "ubuntu": ("libusb-1.0-0-dev", "pkg-config"),
    "debian": ("libusb-1.0-0-dev", "pkg-config"),
    "fedora": ("libusb1-devel", "pkgconf-pkg-config"),
    "rhel": ("libusb1-devel", "pkgconf-pkg-config"),
    "alpine": ("libusb-dev", "pkgconf"),
    "arch": ("libusb", "pkgconf"),
}


#: googletest is built for the host, not the Arm target, so it needs the host
#: C++ compiler. Declared here rather than assumed, because the build-essentials
#: meta-package is not guaranteed to be in every image.
_GOOGLETEST_PACKAGES = {
    "ubuntu": ("g++", "cmake", "make"),
    "debian": ("g++", "cmake", "make"),
    "fedora": ("gcc-c++", "cmake", "make"),
    "rhel": ("gcc-c++", "cmake", "make"),
    "alpine": ("g++", "cmake", "make"),
    "arch": ("gcc", "cmake", "make"),
}


#: The SDK builds C++ as well as C, and on the apt distros the Arm C++ runtime
#: is packaged separately from the C one.
_PICO_SDK_PACKAGES = {
    "ubuntu": ("libstdc++-arm-none-eabi-newlib",),
    "debian": ("libstdc++-arm-none-eabi-newlib",),
    "fedora": (),
    "rhel": (),
    "alpine": (),
    "arch": (),
}


#: The Yocto Project's own build-host package list for apt distributions, from
#: its Quick Build guide at
#: https://docs.yoctoproject.org/brief-yoctoprojectqs/index.html
#: Copied in the order the guide writes it, so the two can be compared at a
#: glance. python3-venv is the single addition: the guide's next step builds a
#: virtual environment, and a minimal image does not ship venv alongside
#: python3 the way a desktop install does.
_YOCTO_PACKAGES = {
    "ubuntu": (
        "build-essential",
        "chrpath",
        "cpio",
        "debianutils",
        "diffstat",
        "file",
        "gawk",
        "gcc",
        "git",
        "iputils-ping",
        "libacl1",
        "libcrypt-dev",
        "locales",
        "python3",
        "python3-git",
        "python3-jinja2",
        "python3-pexpect",
        "python3-pip",
        "python3-subunit",
        "socat",
        "texinfo",
        "unzip",
        "wget",
        "xz-utils",
        "zstd",
        "python3-venv",
    ),
}


#: The Android Open Source Project's own build-host package list, from its
#: requirements page at https://source.android.com/docs/setup/start/requirements
#: Copied in the order that page writes it, with repo last: the same page
#: installs the repo launcher from apt, and one list keeps it in one layer.
#: OpenJDK, Make and Python 3 are deliberately absent -- the source tree ships
#: prebuilt copies of all three, and a second set on PATH is the same trap as
#: two Rust toolchains.
_AOSP_PACKAGES = {
    "ubuntu": (
        "git-core",
        "gnupg",
        "flex",
        "bison",
        "build-essential",
        "zip",
        "curl",
        "zlib1g-dev",
        "libc6-dev-i386",
        "x11proto-core-dev",
        "libx11-dev",
        "lib32z1-dev",
        "libgl1-mesa-dev",
        "libxml2-utils",
        "xsltproc",
        "unzip",
        "fontconfig",
        "repo",
    ),
}


#: MinGW-w64 built against the Universal C Runtime, per distribution. Ubuntu
#: 26.04 and RHEL 10 package only the older msvcrt build, so they have none.
#: Alpine's and Arch's names say nothing about the runtime; compiling a program
#: with each and reading its imports showed api-ms-win-crt, which is UCRT.
_MINGW_UCRT_PACKAGES = {
    "debian": ("gcc-mingw-w64-ucrt64", "g++-mingw-w64-ucrt64"),
    "fedora": ("ucrt64-gcc", "ucrt64-gcc-c++"),
    "alpine": ("mingw-w64-gcc",),
    "arch": ("mingw-w64-gcc",),
}


#: Wine, to run what the cross-compiler produces. The apt distributions split
#: the 64-bit loader into its own package; RHEL does not package Wine at all.
_WINE_PACKAGES = {
    "ubuntu": ("wine", "wine64"),
    "debian": ("wine", "wine64"),
    "fedora": ("wine",),
    "alpine": ("wine",),
    "arch": ("wine",),
}


EXTRAS: tuple[Extra, ...] = (
    Extra(
        key="thumbv7em",
        label="Cortex-M4F / M7F target",
        kind=ExtraKind.RUSTUP_TARGET,
        command="rustup target add thumbv7em-none-eabihf",
        default_on=True,
        note=(
            "Bare-metal target for Cortex-M4F and M7F, the hard-float variant. "
            "Adds the precompiled core and alloc libraries for that triple."
        ),
    ),
    Extra(
        key="rp2040",
        label="RP2040 target (Cortex-M0+)",
        kind=ExtraKind.RUSTUP_TARGET,
        command="rustup target add thumbv6m-none-eabi",
        default_on=True,
        note=(
            "The Rust target for the RP2040, as used by rp2040-hal. The Pico "
            "SDK builds C for the same chip; this is the Rust side of it."
        ),
    ),
    Extra(
        key="rp2350-arm",
        label="RP2350 target, Arm (Cortex-M33)",
        kind=ExtraKind.RUSTUP_TARGET,
        command="rustup target add thumbv8m.main-none-eabihf",
        default_on=True,
        note=(
            "The RP2350 running its Arm cores, which is the usual choice and "
            "what rp235x-hal defaults to. Hard-float, matching the M33 FPU."
        ),
    ),
    Extra(
        key="rp2350-riscv",
        label="RP2350 target, RISC-V (Hazard3)",
        kind=ExtraKind.RUSTUP_TARGET,
        command="rustup target add riscv32imac-unknown-none-elf",
        default_on=True,
        note=(
            "The same RP2350 silicon running its two Hazard3 RISC-V cores "
            "instead of the Arm ones. A chip-level choice made at boot, so "
            "firmware for it is a separate build. Turn this off if your team "
            "only ships Arm."
        ),
    ),
    Extra(
        key="llvm-tools",
        label="LLVM tools component",
        kind=ExtraKind.RUSTUP_COMPONENT,
        command="rustup component add llvm-tools-preview",
        default_on=True,
        note=(
            "Ships llvm-objdump, llvm-nm and llvm-size for the Rust toolchain. "
            "cargo-binutils is a thin wrapper over these and is useless without "
            "them, so it is installed first."
        ),
    ),
    Extra(
        key="cargo-binutils",
        label="cargo-binutils",
        kind=ExtraKind.CARGO_INSTALL,
        command="cargo install cargo-binutils --locked",
        default_on=True,
        requires=("llvm-tools",),
        note=(
            "Gives cargo size, cargo nm and cargo objdump for inspecting "
            "firmware images. --locked keeps the build reproducible."
        ),
    ),
    Extra(
        key="probe-rs-tools",
        label="probe-rs tools",
        kind=ExtraKind.CARGO_INSTALL,
        command="cargo install probe-rs-tools --locked",
        default_on=True,
        note=(
            "On-chip debugging and flashing, an alternative to OpenOCD, for use "
            "with debug probe hardware attached. Compiling it adds several "
            "minutes to the build and needs udev and libusb development "
            "headers, which are added automatically."
        ),
        build_packages=_PROBE_RS_PACKAGES,
    ),
    Extra(
        key="pico-sdk",
        label="Raspberry Pi Pico SDK",
        kind=ExtraKind.SDK,
        # {version} is replaced with the release tag when the file is generated.
        command=(
            "git clone --branch {version} --depth 1 "
            "--recurse-submodules --shallow-submodules "
            "https://github.com/raspberrypi/pico-sdk.git /opt/pico-sdk"
        ),
        default_on=True,
        repo="raspberrypi/pico-sdk",
        fallback_tag="2.3.1",
        env=(("PICO_SDK_PATH", "/opt/pico-sdk"),),
        verify='test -f "$PICO_SDK_PATH/pico_sdk_init.cmake"',
        note=(
            "Cloned at the latest GitHub release tag, which is resolved when "
            "the file is generated and then written in literally so the image "
            "stays reproducible. Pulls the five submodules the SDK needs: "
            "tinyusb, cyw43-driver, lwip, mbedtls and btstack. For UF2 output "
            "SDK 2.x also needs picotool, which is the next entry."
        ),
        build_packages=_PICO_SDK_PACKAGES,
    ),
    Extra(
        key="picotool",
        label="picotool",
        kind=ExtraKind.SDK,
        # Installed with cmake --install, never by copying the binary onto
        # PATH: the SDK locates picotool through the CMake package config that
        # only the install target writes.
        command=(
            "git clone --branch {version} --depth 1 "
            "https://github.com/raspberrypi/picotool.git /tmp/picotool"
            " && cmake -S /tmp/picotool -B /tmp/picotool/build "
            "-DCMAKE_BUILD_TYPE=Release"
            ' && cmake --build /tmp/picotool/build -j"$(nproc)"'
            " && cmake --install /tmp/picotool/build"
            " && rm -rf /tmp/picotool"
        ),
        default_on=True,
        repo="raspberrypi/picotool",
        fallback_tag="2.3.1",
        requires=("pico-sdk",),
        verify="picotool version",
        note=(
            "Converts ELF to UF2, which SDK 2.x needs for any flashable output, "
            "and hashes and signs binaries. Built from source at its release "
            "tag and installed with cmake --install, because the SDK finds it "
            "through the CMake package config rather than PATH. Requires the "
            "Pico SDK, which it is built against."
        ),
        build_packages=_PICOTOOL_PACKAGES,
    ),
    Extra(
        key="googletest",
        label="GoogleTest",
        kind=ExtraKind.SDK,
        # Installed rather than left for each project to fetch, so every
        # project in the image finds the same version through find_package.
        command=(
            "git clone --branch {version} --depth 1 "
            "https://github.com/google/googletest.git /tmp/googletest"
            " && cmake -S /tmp/googletest -B /tmp/googletest/build "
            "-DCMAKE_BUILD_TYPE=Release"
            ' && cmake --build /tmp/googletest/build -j"$(nproc)"'
            " && cmake --install /tmp/googletest/build"
            " && rm -rf /tmp/googletest"
        ),
        default_on=True,
        repo="google/googletest",
        fallback_tag="v1.18.0",
        # lib or lib64 depending on the distro, so search rather than assume.
        # No pipe: the result is tested directly rather than through grep.
        verify='test -n "$(find /usr/local -name GTestConfig.cmake)"',
        note=(
            "Host-side unit testing for C and C++, with gMock included. Built "
            "from its latest release tag and installed with cmake --install, "
            "so a project picks it up with find_package(GTest). Runs on this "
            "machine's architecture, not the Arm target, which pairs with LCOV "
            "for coverage. Requires C++17 from 1.17 onward."
        ),
        build_packages=_GOOGLETEST_PACKAGES,
    ),
    Extra(
        key="yocto",
        label="Yocto Project build host",
        kind=ExtraKind.BUILD_HOST,
        # locale-gen reads the package list installed above; update-locale
        # writes /etc/default/locale for anything that reads it at login.
        command="locale-gen en_US.UTF-8 && update-locale LANG=en_US.UTF-8",
        # Situational and large: nobody wants a Yocto host inside an image
        # built for firmware work, so this one is asked for rather than assumed.
        default_on=False,
        distros=("ubuntu",),
        env=(("LANG", "en_US.UTF-8"), ("LC_ALL", "en_US.UTF-8")),
        verify='test -n "$(locale -a | grep -i en_US.utf8)"',
        note=(
            "The Yocto Project's documented build-host requirements: the "
            "package list from its Quick Build guide, and the en_US.UTF-8 "
            "locale a build refuses to start without. Ubuntu only, because "
            "that list names apt packages. The build tree deliberately stays "
            "out of the image: the guide puts it in the working directory, "
            "which here is /work, and a real build wants 140 GB of free disk "
            "and 32 GB of RAM. Inside the container the guide continues as "
            "written -- python3 -m venv ./bitbake-setup-venv, pip install "
            "bitbake-setup, bitbake-setup init -- and the container has to run "
            "as an ordinary user, because bitbake refuses to run as root."
        ),
        build_packages=_YOCTO_PACKAGES,
    ),
    Extra(
        key="aosp",
        label="Android (AOSP) build host",
        kind=ExtraKind.BUILD_HOST,
        # Nothing to configure: the requirements are a package list, and the
        # repo launcher is one of the packages on it.
        command="",
        # A large, situational set of packages, several of them 32-bit.
        default_on=False,
        distros=("ubuntu",),
        # "A 64-bit x86 system", says the same page, and it is not a formality:
        # libc6-dev-i386 is a 32-bit x86 multilib with no ARM64 counterpart.
        arches=("amd64",),
        verify="repo --version",
        note=(
            "The Android Open Source Project's documented build-host packages, "
            "and the repo launcher that fetches the source. Ubuntu only, "
            "because that list names apt packages. OpenJDK, Make and Python 3 "
            "are left out on purpose: the source tree ships prebuilt copies and "
            "a second set on PATH is the trap. The checkout stays out of the "
            "image, as AOSP asks for 400 GB of disk -- 250 to check out, 150 to "
            "build -- and 64 GB of RAM; give the container a volume for it "
            "rather than a share, because a source tree of this size on a "
            "Windows-backed path is hundreds of times slower."
        ),
        build_packages=_AOSP_PACKAGES,
    ),
    Extra(
        key="mingw-ucrt",
        label="MinGW-w64 cross-compiler (UCRT)",
        kind=ExtraKind.PACKAGES,
        command="",
        default_on=False,
        distros=("debian", "fedora", "alpine", "arch"),
        # Checked on x86-64 only; the ARM64 packages have not been looked at.
        arches=("amd64",),
        # Debian and Fedora name the UCRT compiler x86_64-w64-mingw32ucrt-gcc,
        # Alpine and Arch plain x86_64-w64-mingw32-gcc. Grouped, because an
        # ungrouped || inside the smoke test's && chain would let an earlier
        # failure fall through to it and pass.
        verify="(x86_64-w64-mingw32ucrt-gcc --version || x86_64-w64-mingw32-gcc --version)",
        note=(
            "Builds Windows executables against the Universal C Runtime, the "
            "same runtime as MSYS2's UCRT64. Not offered on Ubuntu 26.04 or "
            "RHEL, whose only MinGW is the older msvcrt build. Alpine's and "
            "Arch's packages carry no runtime in their names, so both were "
            "checked by compiling with them: each links api-ms-win-crt, which "
            "is UCRT."
        ),
        build_packages=_MINGW_UCRT_PACKAGES,
    ),
    Extra(
        key="wine",
        label="Wine",
        kind=ExtraKind.PACKAGES,
        command="",
        default_on=False,
        distros=("ubuntu", "debian", "fedora", "alpine", "arch"),
        # Wine runs a program on the processor it was built for; it does not
        # translate x86-64 on an ARM64 machine.
        arches=("amd64",),
        verify="wine --version",
        note=(
            "Runs the Windows executables a cross-compiler produces, so a test "
            "suite can execute them on Linux. RHEL does not package it, and it "
            "is x86-64 only here: Wine does not translate an x86-64 program on "
            "ARM64."
        ),
        build_packages=_WINE_PACKAGES,
    ),
)

EXTRAS_BY_KEY = {extra.key: extra for extra in EXTRAS}


def needs_rust(
    selected: set[str], distro_key: str | None = None, arch_key: str = ""
) -> bool:
    """True when any selected extra requires a rustup toolchain."""
    return any(extra.needs_rust for extra in resolve(selected, distro_key, arch_key))


def default_selection() -> set[str]:
    return {extra.key for extra in EXTRAS if extra.default_on}


def available_for(distro_key: str, arch_key: str = "") -> tuple[Extra, ...]:
    """The extras offered on *distro_key*, in catalogue order."""
    return tuple(extra for extra in EXTRAS if extra.applies_to(distro_key, arch_key))


def resolve(
    selected: set[str], distro_key: str | None = None, arch_key: str = ""
) -> list[Extra]:
    """Return the chosen extras in install order, with prerequisites pulled in.

    Order follows the catalogue, which places rustup components before the
    cargo installs that depend on them.

    Given a *distro_key*, and an *arch_key* where it matters, extras not
    offered there are dropped. A selection outlives a change of base image or
    architecture -- the Yocto host stays ticked while someone looks at what
    Debian would give them -- and without this the ticked-but-hidden extra
    would be written into a file that cannot build it.
    """
    wanted = set(selected)
    for key in list(wanted):
        extra = EXTRAS_BY_KEY.get(key)
        if extra is not None:
            wanted.update(extra.requires)
    return [
        extra
        for extra in EXTRAS
        if extra.key in wanted
        and (distro_key is None or extra.applies_to(distro_key, arch_key))
    ]


def extra_packages(
    selected: set[str], distro_key: str, arch_key: str = ""
) -> list[str]:
    """Distro packages the chosen extras need in order to build."""
    packages: list[str] = []
    for extra in resolve(selected, distro_key, arch_key):
        for name in extra.build_packages.get(distro_key, ()):
            if name not in packages:
                packages.append(name)
    return packages


def implied_by(key: str, selected: set[str]) -> list[str]:
    """Labels of selected extras that require *key*, so it cannot be turned off."""
    labels = []
    for extra in EXTRAS:
        if extra.key in selected and key in extra.requires:
            labels.append(extra.label)
    return labels
