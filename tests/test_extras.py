"""Tests for the Extras section and Containerfile generation."""

from __future__ import annotations

import pytest

from devenv_forge.core.catalog import DISTRO_KEYS, DISTROS_BY_KEY
from devenv_forge.core.containerfile import (
    ImageSpec,
    build_command,
    distro_rust_packages,
    generate,
)
from devenv_forge.core.extras import (
    EXTRAS,
    EXTRAS_BY_KEY,
    ExtraKind,
    available_for,
    default_selection,
    extra_packages,
    implied_by,
    resolve,
)

BASE_PACKAGES = ["gcc-arm-none-eabi", "build-essential", "git", "python3"]


def spec_for(distro_key: str, selected: set[str], packages=None) -> ImageSpec:
    return ImageSpec(
        distro=DISTROS_BY_KEY[distro_key],
        packages=list(BASE_PACKAGES if packages is None else packages),
        selected_extras=set(selected),
    )


class TestExtrasCatalogue:
    def test_the_four_requested_extras_are_present(self) -> None:
        commands = {extra.command for extra in EXTRAS}
        assert "rustup target add thumbv7em-none-eabihf" in commands
        assert "rustup component add llvm-tools-preview" in commands
        assert any(c.startswith("cargo install cargo-binutils") for c in commands)
        assert any(c.startswith("cargo install probe-rs-tools") for c in commands)

    def test_every_generally_offered_extra_is_on_by_default(self) -> None:
        """The Build image tab starts with everything general selected."""
        general = {extra.key for extra in EXTRAS if not extra.distros}
        assert default_selection() == general
        assert all(extra.default_on for extra in EXTRAS if not extra.distros)

    def test_a_distro_specific_extra_has_to_be_asked_for(self) -> None:
        """Offered on one base image only, so it cannot be a default."""
        specific = [extra for extra in EXTRAS if extra.distros]
        assert specific, "the gating exists for a reason; something should use it"
        assert not any(extra.default_on for extra in specific)

    def test_source_builds_are_included_by_default(self) -> None:
        """Including the ones that compile from source and add build time."""
        for key in ("probe-rs-tools", "pico-sdk", "picotool", "googletest"):
            assert key in default_selection()

    def test_cargo_installs_are_locked(self) -> None:
        """--locked keeps two builds of the same image identical."""
        for extra in EXTRAS:
            if extra.kind is ExtraKind.CARGO_INSTALL:
                assert extra.command.endswith("--locked")

    def test_keys_unique(self) -> None:
        keys = [extra.key for extra in EXTRAS]
        assert len(keys) == len(set(keys))


class TestDependencies:
    def test_cargo_binutils_pulls_in_llvm_tools(self) -> None:
        """cargo-binutils is a wrapper over the llvm-tools component."""
        assert "llvm-tools" in EXTRAS_BY_KEY["cargo-binutils"].requires
        keys = [extra.key for extra in resolve({"cargo-binutils"})]
        assert "llvm-tools" in keys

    def test_component_is_installed_before_the_wrapper(self) -> None:
        keys = [extra.key for extra in resolve({"cargo-binutils"})]
        assert keys.index("llvm-tools") < keys.index("cargo-binutils")

    def test_implied_by_reports_the_dependent(self) -> None:
        assert implied_by("llvm-tools", {"cargo-binutils"}) == ["cargo-binutils"]
        assert implied_by("llvm-tools", {"thumbv7em"}) == []

    def test_resolve_empty(self) -> None:
        assert resolve(set()) == []

    def test_resolve_ignores_unknown_keys(self) -> None:
        assert resolve({"nonsense"}) == []


class TestExtraBuildPackages:
    def test_probe_rs_needs_udev_headers_everywhere(self) -> None:
        for key in DISTRO_KEYS:
            packages = extra_packages({"probe-rs-tools"}, key)
            assert packages, f"no probe-rs build packages for {key}"

    def test_distro_specific_names(self) -> None:
        assert "libudev-dev" in extra_packages({"probe-rs-tools"}, "debian")
        assert "systemd-devel" in extra_packages({"probe-rs-tools"}, "fedora")
        assert "eudev-dev" in extra_packages({"probe-rs-tools"}, "alpine")

    def test_rustup_only_extras_need_no_distro_packages(self) -> None:
        """Targets and components come from rustup, so no headers are added."""
        rust_only = {"thumbv7em", "rp2040", "rp2350-arm", "rp2350-riscv", "llvm-tools"}
        assert extra_packages(rust_only, "debian") == []


class TestGeneratedContainerfile:
    def test_no_rust_section_without_extras(self) -> None:
        text = generate(spec_for("debian", set()))
        assert "rustup" not in text
        assert "FROM --platform=linux/amd64 debian:13-slim" in text

    def test_rustup_bootstrap_added_for_any_extra(self) -> None:
        """A distro rustc cannot satisfy 'rustup target add'."""
        text = generate(spec_for("debian", {"thumbv7em"}))
        assert "sh.rustup.rs" in text
        assert "rustup target add thumbv7em-none-eabihf" in text

    def test_tls_packages_added_for_the_installer(self) -> None:
        text = generate(spec_for("debian", {"thumbv7em"}))
        assert "curl" in text and "ca-certificates" in text

    def test_distro_rust_is_dropped_when_rustup_is_used(self) -> None:
        """Two toolchains on PATH is a trap, so the distro one is removed."""
        text = generate(
            spec_for("debian", {"thumbv7em"}, packages=["rustc", "cargo", "git"])
        )
        install_block = text.split("# Rust toolchain")[0]
        installed = [
            line.strip().rstrip(" \\")
            for line in install_block.splitlines()
            if line.startswith("    ")
        ]
        assert "rustc" not in installed
        assert "cargo" not in installed
        assert "git" in installed
        assert "omitted" in install_block

    def test_distro_rust_kept_when_no_extras(self) -> None:
        text = generate(spec_for("debian", set(), packages=["rustc", "git"]))
        assert "rustc \\" in text

    def test_probe_rs_headers_appear_only_when_selected(self) -> None:
        without = generate(spec_for("debian", default_selection() - {"probe-rs-tools"}))
        with_probe = generate(spec_for("debian", default_selection() | {"probe-rs-tools"}))
        assert "libudev-dev" not in without
        assert "libudev-dev" in with_probe
        assert "cargo install probe-rs-tools --locked" in with_probe

    def test_smoke_test_checks_what_was_installed(self) -> None:
        text = generate(spec_for("debian", default_selection() | {"probe-rs-tools"}))
        assert "arm-none-eabi-gcc --version" in text
        assert "rustc --version" in text
        assert "cargo size --version" in text
        assert "probe-rs --version" in text

    def test_toolchain_is_pinnable(self) -> None:
        spec = spec_for("debian", {"thumbv7em"})
        spec.rust_toolchain = "1.98.0"
        assert "ARG RUST_TOOLCHAIN=1.98.0" in generate(spec)

    @pytest.mark.parametrize("distro_key", DISTRO_KEYS)
    def test_generates_for_every_distro(self, distro_key: str) -> None:
        text = generate(spec_for(distro_key, default_selection() | {"probe-rs-tools"}))
        distro = DISTROS_BY_KEY[distro_key]
        # The platform is part of the FROM: a file without it means something
        # different on a machine of another architecture.
        assert f"FROM --platform=linux/amd64 {distro.base_image}" in text
        assert distro.install_cmd.split()[0] in text

    @pytest.mark.parametrize("distro_key", DISTRO_KEYS)
    def test_line_continuations_are_balanced(self, distro_key: str) -> None:
        """A RUN ending in a stray backslash is a broken build."""
        lines = generate(spec_for(distro_key, default_selection())).splitlines()
        for index, line in enumerate(lines):
            if line.rstrip().endswith("\\"):
                assert index + 1 < len(lines), "file ends on a continuation"
                assert lines[index + 1].strip(), "continuation leads to a blank line"

    def test_cargo_caches_are_removed(self) -> None:
        text = generate(spec_for("debian", default_selection()))
        assert "rm -rf /usr/local/cargo/registry" in text

    def test_ends_with_a_newline(self) -> None:
        assert generate(spec_for("debian", set())).endswith("\n")



#: The package list the Yocto Project's Quick Build guide gives for apt
#: distributions, written out again here so a change to the catalogue has to be
#: a deliberate one rather than a typo nobody notices.
YOCTO_DOCUMENTED = (
    "build-essential chrpath cpio debianutils diffstat file gawk gcc git "
    "iputils-ping libacl1 libcrypt-dev locales python3 python3-git "
    "python3-jinja2 python3-pexpect python3-pip python3-subunit socat texinfo "
    "unzip wget xz-utils zstd"
).split()


class TestYoctoBuildHost:
    """An extra written against one distribution's documented requirements."""

    def test_it_is_offered_on_ubuntu(self) -> None:
        assert "yocto" in {extra.key for extra in available_for("ubuntu")}

    @pytest.mark.parametrize("distro_key", [k for k in DISTRO_KEYS if k != "ubuntu"])
    def test_it_is_offered_nowhere_else(self, distro_key: str) -> None:
        """Its package names are apt's, and Ubuntu's specifically."""
        assert "yocto" not in {extra.key for extra in available_for(distro_key)}

    def test_ubuntu_is_the_26_04_base(self) -> None:
        """The gate is by distro key, and that key is Ubuntu 26.04."""
        assert DISTROS_BY_KEY["ubuntu"].base_image == "ubuntu:26.04"

    def test_it_carries_the_documented_package_list(self) -> None:
        packages = extra_packages({"yocto"}, "ubuntu")
        assert [name for name in packages if name in YOCTO_DOCUMENTED] == YOCTO_DOCUMENTED

    def test_venv_is_the_only_addition(self) -> None:
        """A minimal image has no python3-venv, and the guide's next step needs it."""
        extra = set(extra_packages({"yocto"}, "ubuntu")) - set(YOCTO_DOCUMENTED)
        assert extra == {"python3-venv"}

    def test_a_ticked_but_unoffered_extra_is_not_written_out(self) -> None:
        """The tick survives a change of base image; the package list must not."""
        text = generate(spec_for("debian", {"yocto"}))
        assert "locale-gen" not in text
        assert "texinfo" not in text

    def test_it_needs_no_rust(self) -> None:
        """Selecting a build host must not drag a toolchain into the image."""
        spec = spec_for("ubuntu", {"yocto"})
        assert not spec.needs_rust
        assert "rustup" not in generate(spec)

    def test_it_is_not_an_sdk_clone(self) -> None:
        """Nothing is fetched, so there is no version ARG and no clone."""
        text = generate(spec_for("ubuntu", {"yocto"}))
        assert "git clone" not in text
        assert "YOCTO_VERSION" not in text

    def test_the_locale_is_set_up_and_exported(self) -> None:
        """A Yocto build refuses to start without en_US.UTF-8."""
        text = generate(spec_for("ubuntu", {"yocto"}))
        assert "locale-gen en_US.UTF-8" in text
        assert "ENV LANG=en_US.UTF-8" in text
        assert "ENV LC_ALL=en_US.UTF-8" in text

    def test_the_smoke_test_proves_the_locale_exists(self) -> None:
        """A build-host extra is verified like any other."""
        assert "en_US.utf8" in generate(spec_for("ubuntu", {"yocto"}))

    def test_it_coexists_with_the_rust_extras(self) -> None:
        text = generate(spec_for("ubuntu", {"yocto", "thumbv7em"}))
        assert "rustup target add thumbv7em-none-eabihf" in text
        assert "locale-gen en_US.UTF-8" in text

    def test_the_note_says_what_the_image_deliberately_leaves_out(self) -> None:
        """The build tree, its disk appetite and the root refusal all bite."""
        note = EXTRAS_BY_KEY["yocto"].note
        assert "140 GB" in note
        assert "root" in note



#: The package list on the Android Open Source Project's requirements page,
#: written out again so a change to the catalogue has to be deliberate.
AOSP_DOCUMENTED = (
    "git-core gnupg flex bison build-essential zip curl zlib1g-dev "
    "libc6-dev-i386 x11proto-core-dev libx11-dev lib32z1-dev libgl1-mesa-dev "
    "libxml2-utils xsltproc unzip fontconfig"
).split()


class TestAospBuildHost:
    """A second host written for one distribution, and the first with no command."""

    def test_it_is_offered_on_ubuntu(self) -> None:
        assert "aosp" in {extra.key for extra in available_for("ubuntu")}

    @pytest.mark.parametrize("distro_key", [k for k in DISTRO_KEYS if k != "ubuntu"])
    def test_it_is_offered_nowhere_else(self, distro_key: str) -> None:
        assert "aosp" not in {extra.key for extra in available_for(distro_key)}

    def test_it_carries_the_documented_package_list(self) -> None:
        packages = extra_packages({"aosp"}, "ubuntu")
        assert [name for name in packages if name in AOSP_DOCUMENTED] == AOSP_DOCUMENTED

    def test_the_repo_launcher_comes_with_it(self) -> None:
        """The same page installs repo from apt, so it rides in one layer."""
        assert "repo" in extra_packages({"aosp"}, "ubuntu")

    def test_the_prebuilt_tools_are_left_out(self) -> None:
        """The source tree ships OpenJDK, Make and Python 3; a second set is a trap."""
        packages = set(extra_packages({"aosp"}, "ubuntu"))
        assert not packages & {"openjdk-21-jdk", "openjdk-17-jdk", "default-jdk", "make"}

    def test_the_extra_addition_is_only_repo(self) -> None:
        assert set(extra_packages({"aosp"}, "ubuntu")) - set(AOSP_DOCUMENTED) == {"repo"}

    def test_it_needs_no_rust(self) -> None:
        spec = spec_for("ubuntu", {"aosp"})
        assert not spec.needs_rust
        assert "rustup" not in generate(spec)

    def test_a_requirements_list_needs_no_command(self) -> None:
        """Nothing to configure, so the image gains packages and no layer."""
        text = generate(spec_for("ubuntu", {"aosp"}))
        assert "Android (AOSP) build host" in text
        assert "Its requirements are the packages installed above." in text
        # The only RUN lines are the package install and the smoke test; the
        # host section itself contributes none.
        assert len([ln for ln in text.splitlines() if ln.startswith("RUN ")]) == 2

    def test_the_smoke_test_proves_repo_runs(self) -> None:
        assert "repo --version" in generate(spec_for("ubuntu", {"aosp"}))

    def test_a_ticked_but_unoffered_extra_is_not_written_out(self) -> None:
        assert "xsltproc" not in generate(spec_for("debian", {"aosp"}))

    def test_it_coexists_with_the_other_host(self) -> None:
        """Two build hosts in one image is odd but must not generate nonsense."""
        text = generate(spec_for("ubuntu", {"aosp", "yocto"}))
        assert "locale-gen en_US.UTF-8" in text
        assert "repo" in text
        assert text.count("to its own documented requirements") == 2

    def test_the_note_gives_the_hardware_the_docs_ask_for(self) -> None:
        note = EXTRAS_BY_KEY["aosp"].note
        assert "400 GB" in note and "64 GB" in note

class TestBuildCommand:
    def test_tags_the_image(self) -> None:
        spec = spec_for("debian", set())
        spec.image_name, spec.image_tag = "team-embedded", "2026.09"
        argv = build_command(spec)
        # argv[0] is the resolved podman path, which varies by machine.
        assert "podman" in argv[0].lower()
        assert argv[1] == "build"
        assert "team-embedded:2026.09" in argv

    def test_reference_property(self) -> None:
        spec = spec_for("debian", set())
        assert spec.reference == "devenv:latest"


class TestDistroRustPackages:
    def test_covers_the_obvious_names(self) -> None:
        assert "rustc" in distro_rust_packages("debian")
        assert "cargo" in distro_rust_packages("debian")
        assert "rust" in distro_rust_packages("arch")


class TestArchPacman:
    """Arch is rolling, so a partial upgrade is a real hazard."""

    def test_refresh_and_upgrade_are_one_command(self) -> None:
        text = generate(spec_for("arch", default_selection()))
        assert "pacman -Syu --noconfirm" in text
        # -Sy on its own refreshes the database without upgrading, which leaves
        # newly installed packages linked against libraries that are no longer
        # there.
        assert "pacman -Sy --noconfirm" not in text.replace("-Syu", "")

    def test_cache_is_cleared(self) -> None:
        assert "pacman -Scc --noconfirm" in generate(spec_for("arch", set()))


class TestNoFragileShellFeatures:
    """The generated file must not lean on shell behaviour podman ignores.

    SHELL with -o pipefail is dropped under the OCI image format podman builds
    by default, and busybox sh on Alpine has no pipefail at all. Either way the
    guard is absent exactly when it matters.
    """

    @pytest.mark.parametrize("distro_key", DISTRO_KEYS)
    def test_no_shell_instruction(self, distro_key: str) -> None:
        assert "SHELL [" not in generate(spec_for(distro_key, default_selection()))

    @pytest.mark.parametrize("distro_key", DISTRO_KEYS)
    def test_installer_is_not_piped_into_a_shell(self, distro_key: str) -> None:
        """A pipe hides a failed download behind the shell's exit status."""
        text = generate(spec_for(distro_key, default_selection()))
        assert "| sh" not in text
        assert "-o /tmp/rustup-init.sh" in text
        assert "sh /tmp/rustup-init.sh" in text

    def test_installer_script_is_cleaned_up(self) -> None:
        assert "rm /tmp/rustup-init.sh" in generate(
            spec_for("debian", default_selection())
        )


class TestPicoSdk:
    """An SDK checkout is a different shape from the rustup extras."""

    def test_is_on_by_default(self) -> None:
        assert "pico-sdk" in default_selection()

    def test_does_not_pull_in_rust(self) -> None:
        """The regression that matters: an SDK needs no Rust toolchain."""
        from devenv_forge.core.extras import needs_rust

        assert needs_rust({"pico-sdk"}) is False
        text = generate(spec_for("debian", {"pico-sdk"}))
        assert "rustup" not in text
        assert "sh.rustup.rs" not in text
        assert "RUSTUP_HOME" not in text

    def test_rust_extras_still_pull_in_rust(self) -> None:
        from devenv_forge.core.extras import needs_rust

        assert needs_rust({"pico-sdk", "thumbv7em"}) is True

    def test_distro_rust_is_kept_when_only_an_sdk_is_selected(self) -> None:
        """Nothing replaces the distro rustc unless rustup is actually used."""
        text = generate(spec_for("debian", {"pico-sdk"}, packages=["rustc", "git"]))
        assert "rustc \\" in text
        assert "omitted" not in text

    def test_clone_is_pinned_to_a_tag(self) -> None:
        spec = spec_for("debian", {"pico-sdk"})
        spec.sdk_versions = {"pico-sdk": "2.3.1"}
        text = generate(spec)
        assert "ARG PICO_SDK_VERSION=2.3.1" in text
        assert '--branch "$PICO_SDK_VERSION"' in text

    def test_falls_back_to_a_known_tag(self) -> None:
        """An offline machine must still produce a buildable file."""
        text = generate(spec_for("debian", {"pico-sdk"}))
        assert "ARG PICO_SDK_VERSION=" in text
        assert "ARG PICO_SDK_VERSION=\n" not in text

    def test_submodules_are_fetched(self) -> None:
        """The SDK is unusable without tinyusb, lwip and the rest."""
        text = generate(spec_for("debian", {"pico-sdk"}))
        assert "--recurse-submodules" in text
        assert "--shallow-submodules" in text

    def test_sets_the_sdk_path(self) -> None:
        text = generate(spec_for("debian", {"pico-sdk"}))
        assert "ENV PICO_SDK_PATH=/opt/pico-sdk" in text

    def test_smoke_test_checks_the_checkout(self) -> None:
        text = generate(spec_for("debian", {"pico-sdk"}))
        assert 'test -f "$PICO_SDK_PATH/pico_sdk_init.cmake"' in text

    def test_cpp_runtime_added_on_apt_distros(self) -> None:
        """The SDK builds C++, and apt packages the Arm C++ runtime separately."""
        assert "libstdc++-arm-none-eabi-newlib" in generate(
            spec_for("debian", {"pico-sdk"})
        )

    @pytest.mark.parametrize("distro_key", DISTRO_KEYS)
    def test_generates_everywhere(self, distro_key: str) -> None:
        text = generate(spec_for(distro_key, {"pico-sdk"}))
        assert "pico-sdk.git" in text

    def test_combined_with_rust_extras(self) -> None:
        text = generate(spec_for("debian", default_selection() | {"pico-sdk"}))
        assert "sh.rustup.rs" in text
        assert "pico-sdk.git" in text
        assert "rustup target add thumbv7em-none-eabihf" in text


class TestPicotool:
    """SDK 2.x needs picotool for any flashable output."""

    def test_is_on_by_default(self) -> None:
        assert "picotool" in default_selection()

    def test_requires_the_sdk(self) -> None:
        """It is built against the SDK and needs PICO_SDK_PATH set."""
        assert "pico-sdk" in EXTRAS_BY_KEY["picotool"].requires
        keys = [extra.key for extra in resolve({"picotool"})]
        assert keys == ["pico-sdk", "picotool"]

    def test_sdk_path_is_set_before_picotool_builds(self) -> None:
        text = generate(spec_for("debian", {"picotool"}))
        assert text.index("ENV PICO_SDK_PATH") < text.index("picotool.git")

    def test_installed_not_copied(self) -> None:
        """Copying the binary onto PATH leaves the SDK unable to find it."""
        text = generate(spec_for("debian", {"picotool"}))
        assert "cmake --install" in text
        assert "cp /tmp/picotool" not in text

    def test_libusb_added_for_usb_support(self) -> None:
        """Without libusb picotool builds but loses load, save and reboot."""
        assert "libusb-1.0-0-dev" in generate(spec_for("debian", {"picotool"}))
        assert "libusb1-devel" in generate(spec_for("fedora", {"picotool"}))
        assert "libusb" in generate(spec_for("arch", {"picotool"}))

    def test_pinned_to_a_tag(self) -> None:
        spec = spec_for("debian", {"picotool"})
        spec.sdk_versions = {"picotool": "2.3.1", "pico-sdk": "2.3.1"}
        text = generate(spec)
        assert "ARG PICOTOOL_VERSION=2.3.1" in text
        assert '--branch "$PICOTOOL_VERSION"' in text

    def test_build_tree_is_cleaned_up(self) -> None:
        assert "rm -rf /tmp/picotool" in generate(spec_for("debian", {"picotool"}))

    def test_smoke_test_runs_it(self) -> None:
        assert "picotool version" in generate(spec_for("debian", {"picotool"}))

    def test_still_no_rust(self) -> None:
        from devenv_forge.core.extras import needs_rust

        assert needs_rust({"picotool"}) is False
        assert "rustup" not in generate(spec_for("debian", {"picotool"}))

    @pytest.mark.parametrize("distro_key", DISTRO_KEYS)
    def test_generates_everywhere(self, distro_key: str) -> None:
        text = generate(spec_for(distro_key, {"picotool"}))
        assert "picotool.git" in text
        assert "cmake --install" in text


class TestMultiStepRun:
    def test_steps_are_split_across_lines(self) -> None:
        """A multi-step build reads better with each && on its own line."""
        lines = generate(spec_for("debian", {"picotool"})).splitlines()
        continuation = [ln for ln in lines if ln.startswith(" && cmake")]
        assert len(continuation) >= 2

    @pytest.mark.parametrize("distro_key", DISTRO_KEYS)
    def test_continuations_stay_balanced(self, distro_key: str) -> None:
        lines = generate(spec_for(distro_key, {"picotool"})).splitlines()
        for index, line in enumerate(lines):
            if line.rstrip().endswith("\\"):
                assert index + 1 < len(lines)
                assert lines[index + 1].strip()


class TestPicoRustTargets:
    """RP2040 and RP2350 need different targets, and RP2350 has two cores."""

    TARGETS = {
        "rp2040": "thumbv6m-none-eabi",
        "rp2350-arm": "thumbv8m.main-none-eabihf",
        "rp2350-riscv": "riscv32imac-unknown-none-elf",
    }

    @pytest.mark.parametrize(("key", "triple"), list(TARGETS.items()))
    def test_target_triple(self, key: str, triple: str) -> None:
        """Triples as documented by rp-hal, not guessed from the chip name."""
        assert EXTRAS_BY_KEY[key].command == f"rustup target add {triple}"

    @pytest.mark.parametrize("key", list(TARGETS))
    def test_on_by_default(self, key: str) -> None:
        assert key in default_selection()

    def test_rp2040_is_not_the_cortex_m4_target(self) -> None:
        """The RP2040 is a Cortex-M0+, so thumbv7em would not run on it."""
        assert EXTRAS_BY_KEY["rp2040"].command != EXTRAS_BY_KEY["thumbv7em"].command
        assert "thumbv6m" in EXTRAS_BY_KEY["rp2040"].command

    def test_rp2350_arm_is_hard_float(self) -> None:
        """The M33 on the RP2350 has an FPU, so the hf variant is correct."""
        assert EXTRAS_BY_KEY["rp2350-arm"].command.endswith("eabihf")

    @pytest.mark.parametrize("key", list(TARGETS))
    def test_appears_in_the_containerfile(self, key: str) -> None:
        text = generate(spec_for("debian", {key}))
        assert self.TARGETS[key] in text
        assert "sh.rustup.rs" in text  # a target needs rustup

    def test_all_three_together(self) -> None:
        text = generate(spec_for("debian", default_selection()))
        for triple in self.TARGETS.values():
            assert f"rustup target add {triple}" in text

    def test_targets_precede_cargo_installs(self) -> None:
        """rustup work happens before anything compiles against it."""
        text = generate(spec_for("debian", default_selection()))
        assert text.index("rustup target add thumbv6m-none-eabi") < text.index(
            "cargo install cargo-binutils"
        )

    def test_riscv_can_be_turned_off(self) -> None:
        selected = default_selection() - {"rp2350-riscv"}
        text = generate(spec_for("debian", selected))
        assert "riscv32imac" not in text
        assert "thumbv8m.main-none-eabihf" in text


class TestGoogleTest:
    """Host-side C++ unit testing, built at its latest release tag."""

    def test_is_on_by_default(self) -> None:
        assert "googletest" in default_selection()

    def test_no_rust_needed(self) -> None:
        from devenv_forge.core.extras import needs_rust

        assert needs_rust({"googletest"}) is False
        assert "rustup" not in generate(spec_for("debian", {"googletest"}))

    def test_pinned_to_the_release_tag(self) -> None:
        spec = spec_for("debian", {"googletest"})
        spec.sdk_versions = {"googletest": "v1.18.0"}
        text = generate(spec)
        assert "ARG GOOGLETEST_VERSION=v1.18.0" in text
        assert '--branch "$GOOGLETEST_VERSION"' in text

    def test_fallback_keeps_the_v_prefix(self) -> None:
        """googletest tags are v-prefixed; a bare 1.18.0 is not a valid branch."""
        assert EXTRAS_BY_KEY["googletest"].fallback_tag.startswith("v")

    def test_installed_so_find_package_works(self) -> None:
        text = generate(spec_for("debian", {"googletest"}))
        assert "cmake --install /tmp/googletest/build" in text

    @pytest.mark.parametrize(
        ("distro_key", "compiler"),
        [
            ("debian", "g++"),
            ("ubuntu", "g++"),
            ("fedora", "gcc-c++"),
            ("rhel", "gcc-c++"),
            ("alpine", "g++"),
            ("arch", "gcc"),
        ],
    )
    def test_host_compiler_is_declared(self, distro_key: str, compiler: str) -> None:
        """It builds for the host, so it needs the host C++ compiler."""
        assert compiler in extra_packages({"googletest"}, distro_key)
        assert "cmake" in extra_packages({"googletest"}, distro_key)

    def test_smoke_test_handles_lib_and_lib64(self) -> None:
        """Fedora and RHEL install to lib64, the others to lib."""
        verify = EXTRAS_BY_KEY["googletest"].verify
        assert "find /usr/local" in verify
        assert "/lib/" not in verify and "/lib64/" not in verify

    def test_build_tree_is_removed(self) -> None:
        assert "rm -rf /tmp/googletest" in generate(spec_for("debian", {"googletest"}))

    @pytest.mark.parametrize("distro_key", DISTRO_KEYS)
    def test_generates_everywhere(self, distro_key: str) -> None:
        text = generate(spec_for(distro_key, {"googletest"}))
        assert "googletest.git" in text

    def test_combines_with_the_pico_and_rust_extras(self) -> None:
        text = generate(
            spec_for("debian", default_selection() | {"pico-sdk", "picotool", "googletest"})
        )
        for needle in ("sh.rustup.rs", "pico-sdk.git", "picotool.git", "googletest.git"):
            assert needle in text


class TestArchKeyring:
    """The base image has no local signing key.

    Without initialising it, the archlinux-keyring upgrade hook fails with
    "There is no secret key available to sign with" on every build.
    """

    def test_keyring_is_initialised_before_installing(self) -> None:
        text = generate(spec_for("arch", set()))
        assert "pacman-key --init" in text
        assert "pacman-key --populate archlinux" in text
        assert text.index("pacman-key --init") < text.index("pacman -Syu")

    def test_still_a_single_syu(self) -> None:
        """The keyring step must not reintroduce a separate -Sy."""
        text = generate(spec_for("arch", set()))
        assert "pacman -Sy --noconfirm" not in text.replace("-Syu", "")

    def test_only_arch_gets_it(self) -> None:
        for key in ("debian", "ubuntu", "fedora", "rhel", "alpine"):
            assert "pacman-key" not in generate(spec_for(key, set()))
