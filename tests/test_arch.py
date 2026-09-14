"""Building for an architecture other than the one doing the building.

The machine that builds and the machine that runs the image need not be the
same, which is the point of the selector: an image for a Raspberry Pi 5 or an
Apple silicon Mac is built on whatever is to hand. What has to hold is that the
file says which target it means, that an impossible combination is refused
before the build rather than during it, and that a failure nobody can read gets
explained.
"""

from __future__ import annotations

import pytest

from devenv_forge.core.catalog import (
    ARCH_KEYS,
    ARCHES,
    ARCHES_BY_KEY,
    DISTRO_KEYS,
    DISTROS,
    DISTROS_BY_KEY,
    host_arch,
)
from devenv_forge.core.containerfile import (
    ImageSpec,
    build_command,
    explain_build_failure,
    generate,
    summarise,
)
from devenv_forge.core.extras import available_for, extra_packages


def spec_for(distro_key: str, arch_key: str, selected=frozenset()) -> ImageSpec:
    return ImageSpec(
        distro=DISTROS_BY_KEY[distro_key],
        arch=ARCHES_BY_KEY[arch_key],
        packages=["git"],
        selected_extras=set(selected),
    )


class TestArchCatalogue:
    def test_both_architectures_are_offered(self) -> None:
        assert ARCH_KEYS == ("amd64", "arm64")

    def test_the_platform_strings_are_what_podman_takes(self) -> None:
        assert [a.platform for a in ARCHES] == ["linux/amd64", "linux/arm64"]

    def test_each_knows_what_uname_says_inside_it(self) -> None:
        """For checking a built image is what it claims to be."""
        assert ARCHES_BY_KEY["amd64"].uname == "x86_64"
        assert ARCHES_BY_KEY["arm64"].uname == "aarch64"

    def test_the_host_is_one_of_them(self) -> None:
        assert host_arch() in ARCHES

    def test_every_base_declares_what_it_publishes(self) -> None:
        for distro in DISTROS:
            assert distro.arches, distro.key
            assert set(distro.arches) <= set(ARCH_KEYS), distro.key

    def test_arch_linux_is_x86_64_only(self) -> None:
        """Its own project targets x86-64; the ARM ports are separate images."""
        assert DISTROS_BY_KEY["arch"].arches == ("amd64",)
        assert not DISTROS_BY_KEY["arch"].supports("arm64")

    @pytest.mark.parametrize("distro_key", [k for k in DISTRO_KEYS if k != "arch"])
    def test_every_other_base_publishes_both(self, distro_key: str) -> None:
        assert DISTROS_BY_KEY[distro_key].supports("arm64")
        assert DISTROS_BY_KEY[distro_key].supports("amd64")


class TestTheFileSaysItsTarget:
    @pytest.mark.parametrize("arch_key", ARCH_KEYS)
    def test_the_platform_is_written_into_the_from(self, arch_key: str) -> None:
        """Left out, the same file means something else on another machine."""
        arch = ARCHES_BY_KEY[arch_key]
        text = generate(spec_for("ubuntu", arch_key))
        assert f"FROM --platform={arch.platform} ubuntu:26.04" in text

    @pytest.mark.parametrize("arch_key", ARCH_KEYS)
    def test_the_header_names_the_target(self, arch_key: str) -> None:
        assert f"# Target: {ARCHES_BY_KEY[arch_key].label}" in generate(
            spec_for("ubuntu", arch_key)
        )

    @pytest.mark.parametrize("arch_key", ARCH_KEYS)
    def test_the_command_and_the_file_agree(self, arch_key: str) -> None:
        """Disagreeing would build one thing and describe another."""
        spec = spec_for("ubuntu", arch_key)
        argv = build_command(spec)
        assert argv[argv.index("--platform") + 1] == spec.arch.platform
        assert f"--platform={spec.arch.platform}" in generate(spec)

    def test_the_summary_names_the_architecture(self) -> None:
        assert "ARM64 (aarch64)" in summarise(spec_for("ubuntu", "arm64"))


class TestImpossibleCombinations:
    def test_a_base_without_that_image_is_refused(self) -> None:
        problems = spec_for("arch", "arm64").problems()
        assert problems and "no ARM64 (aarch64) image" in problems[0]

    def test_the_same_base_is_fine_on_its_own_architecture(self) -> None:
        assert spec_for("arch", "amd64").problems() == []

    @pytest.mark.parametrize("distro_key", [k for k in DISTRO_KEYS if k != "arch"])
    def test_everything_else_builds_for_either(self, distro_key: str) -> None:
        assert spec_for(distro_key, "arm64").problems() == []
        assert spec_for(distro_key, "amd64").problems() == []


class TestExtrasByArchitecture:
    def test_a_host_requiring_x86_is_not_offered_elsewhere(self) -> None:
        """AOSP asks for a 64-bit x86 system, and means it."""
        assert "aosp" in {e.key for e in available_for("ubuntu", "amd64")}
        assert "aosp" not in {e.key for e in available_for("ubuntu", "arm64")}

    def test_a_host_that_travels_is_offered_on_both(self) -> None:
        for arch_key in ARCH_KEYS:
            assert "yocto" in {e.key for e in available_for("ubuntu", arch_key)}

    def test_the_rust_extras_are_architecture_independent(self) -> None:
        """A cross-compilation target does not care what hosts the compiler."""
        for arch_key in ARCH_KEYS:
            keys = {e.key for e in available_for("debian", arch_key)}
            assert {"thumbv7em", "rp2040", "cargo-binutils"} <= keys

    def test_a_ticked_but_unbuildable_extra_stays_out_of_the_file(self) -> None:
        text = generate(spec_for("ubuntu", "arm64", {"aosp"}))
        assert "repo" not in text
        assert "libc6-dev-i386" not in text

    def test_its_packages_are_not_pulled_in_either(self) -> None:
        assert extra_packages({"aosp"}, "ubuntu", "arm64") == []
        assert extra_packages({"aosp"}, "ubuntu", "amd64")


class TestExplainBuildFailure:
    """The kernel's own words for a missing emulator explain nothing."""

    def test_exec_format_error_is_named_for_what_it_is(self) -> None:
        message = explain_build_failure(
            1, "STEP 2/7: RUN apt-get update\nError: exec container process: Exec format error"
        )
        assert "another architecture" in message
        assert "binfmt" in message or "emulation" in message
        assert "--privileged" in message

    def test_the_fix_is_a_command_to_copy(self) -> None:
        message = explain_build_failure(1, "Exec format error")
        assert "podman run --rm --privileged" in message

    def test_any_other_failure_keeps_podmans_exit_code(self) -> None:
        message = explain_build_failure(125, "Error: short-name resolution")
        assert "125" in message
        assert "architecture" not in message

    def test_a_clean_log_is_not_read_as_emulation(self) -> None:
        assert "architecture" not in explain_build_failure(2, "")
