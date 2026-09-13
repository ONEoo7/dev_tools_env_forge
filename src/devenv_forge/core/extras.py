"""Extras: toolchain pieces that no distribution packages.

None of these are distro packages, so they cannot appear in the base image
comparison. They are added to the image after the distro packages.

Two families live here. Most come from rustup and cargo, and those need a
rustup bootstrap: a distro's ``rustc`` package does not provide rustup, so
``rustup target add`` fails against it. The other family is an SDK checked out
at a release tag, which needs no Rust at all, so selecting one must not drag a
toolchain into the image. :data:`RUST_KINDS` is what keeps the two apart.
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
)

EXTRAS_BY_KEY = {extra.key: extra for extra in EXTRAS}


def needs_rust(selected: set[str]) -> bool:
    """True when any selected extra requires a rustup toolchain."""
    return any(extra.needs_rust for extra in resolve(selected))


def default_selection() -> set[str]:
    return {extra.key for extra in EXTRAS if extra.default_on}


def resolve(selected: set[str]) -> list[Extra]:
    """Return the chosen extras in install order, with prerequisites pulled in.

    Order follows the catalogue, which places rustup components before the
    cargo installs that depend on them.
    """
    wanted = set(selected)
    for key in list(wanted):
        extra = EXTRAS_BY_KEY.get(key)
        if extra is not None:
            wanted.update(extra.requires)
    return [extra for extra in EXTRAS if extra.key in wanted]


def extra_packages(selected: set[str], distro_key: str) -> list[str]:
    """Distro packages the chosen extras need in order to build."""
    packages: list[str] = []
    for extra in resolve(selected):
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
