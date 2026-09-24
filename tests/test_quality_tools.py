"""The C/C++ quality toolchain: Clang, LLVM, the sanitizers and the analysers.

Every name below was checked against each distribution's own package index
before it went into the catalogue, and is written out again here so a change to
the catalogue has to be deliberate. The traps these guard against were found by
building the images, not by reading about them: Ubuntu's versioned LLVM puts no
plain command on PATH, several rows collapse to one package outside apt, and a
RHEL image cannot install anything from EPEL until it is told to.
"""

from __future__ import annotations

import pytest

from devenv_forge.core.catalog import (
    CATALOG_BY_KEY,
    DISTRO_KEYS,
    DISTROS_BY_KEY,
    LLVM_APT,
    groups,
)
from devenv_forge.core.containerfile import ImageSpec, generate, llvm_bin_dir
from devenv_forge.core.extras import EXTRAS_BY_KEY, ExtraKind, available_for, extra_packages

#: The first candidate for each row on (Ubuntu, Debian, Fedora, RHEL, Alpine, Arch).
VERIFIED = {
    "clang": ("clang-22", "clang-22", "clang", "clang", "clang22", "clang"),
    "clang-format": ("clang-format-22", "clang-format-22", "clang-tools-extra",
                     "clang-tools-extra", "clang22-extra-tools", "clang"),
    "clang-tidy": ("clang-tidy-22", "clang-tidy-22", "clang-tools-extra",
                   "clang-tools-extra", "clang22-extra-tools", "clang"),
    "clangd": ("clangd-22", "clangd-22", "clang-tools-extra",
               "clang-tools-extra", "clang22-extra-tools", "clang"),
    "llvm": ("llvm-22", "llvm-22", "llvm", "llvm", "llvm22", "llvm"),
    "clang-rt": ("libclang-rt-22-dev", "libclang-rt-22-dev", "compiler-rt",
                 "compiler-rt", "compiler-rt", "compiler-rt"),
    "gcc-asan": ("libasan8", "libasan8", "libasan", "libasan", "gcc", "gcc"),
    "gcc-ubsan": ("libubsan1", "libubsan1", "libubsan", "libubsan", "gcc", "gcc"),
    "cppcheck": ("cppcheck",) * 6,
    "valgrind": ("valgrind",) * 6,
    "pre-commit": ("pre-commit",) * 6,
    "ccache": ("ccache",) * 6,
    "gcovr": ("gcovr",) * 6,
}


def _names(key: str) -> list[str]:
    """What the matrix would hand the Build page for every new row on *key*."""
    return [CATALOG_BY_KEY[row].names_for(key)[0] for row in VERIFIED]


def spec_for(distro_key: str, packages, selected=frozenset()) -> ImageSpec:
    return ImageSpec(
        distro=DISTROS_BY_KEY[distro_key],
        packages=list(packages),
        selected_extras=set(selected),
    )


class TestTheRows:
    @pytest.mark.parametrize("row", sorted(VERIFIED))
    def test_each_name_is_the_one_found_in_that_index(self, row: str) -> None:
        spec = CATALOG_BY_KEY[row]
        assert tuple(spec.names_for(key)[0] for key in DISTRO_KEYS) == VERIFIED[row]

    def test_the_apt_pin_is_one_number_in_one_place(self) -> None:
        """Plain clang is 21 on Ubuntu and 19 on Debian; the pin keeps them level."""
        assert LLVM_APT == "22"
        assert CATALOG_BY_KEY["clang"].names_for("ubuntu") == (f"clang-{LLVM_APT}",)

    def test_no_row_falls_back_to_an_older_llvm(self) -> None:
        """A fallback to plain clang would be a silent downgrade, not a rename."""
        for row in ("clang", "llvm", "clang-rt"):
            for key in ("ubuntu", "debian"):
                assert len(CATALOG_BY_KEY[row].names_for(key)) == 1

    def test_they_have_groups_of_their_own(self) -> None:
        names = [group for group, _ in groups()]
        assert "Clang and LLVM" in names and "Analysis and sanitizers" in names
        assert names.index("Clang and LLVM") == names.index("Host toolchain") + 1

    def test_coverage_and_caching_join_their_kin(self) -> None:
        assert CATALOG_BY_KEY["gcovr"].group == CATALOG_BY_KEY["lcov"].group
        assert CATALOG_BY_KEY["ccache"].group == CATALOG_BY_KEY["cmake"].group


class TestOneRowOnePackage:
    def test_rows_that_share_a_package_install_it_once(self) -> None:
        """clang-format, clang-tidy and clangd are one package on Fedora."""
        text = generate(spec_for("fedora", _names("fedora")))
        assert text.count("    clang-tools-extra") == 1

    def test_arch_folds_the_clang_tools_into_clang(self) -> None:
        text = generate(spec_for("arch", _names("arch")))
        assert text.count("    clang \\") + text.count("    clang\n") == 1

    def test_order_is_kept_while_duplicates_go(self) -> None:
        text = generate(spec_for("debian", ["git", "cmake", "git"]))
        assert text.index("    git") < text.index("    cmake")
        assert text.count("    git") == 1


class TestVersionedLlvmOnPath:
    """A versioned LLVM puts only part of itself on PATH.

    Ubuntu's clang-22 installs clang-22 and no clang. Alpine's llvm22 links
    clang but not llvm-symbolizer, which surfaced when the Alpine image failed
    its own smoke test on exactly that command.
    """

    @pytest.mark.parametrize("distro_key", ["ubuntu", "debian"])
    def test_the_apt_images_put_llvm_22_first(self, distro_key: str) -> None:
        text = generate(spec_for(distro_key, _names(distro_key)))
        assert "ENV PATH=/usr/lib/llvm-22/bin:$PATH" in text
        # After the packages that install it, before anything that uses it.
        assert text.index("apt-get install") < text.index("ENV PATH=/usr/lib/llvm-22")

    def test_alpine_gets_its_own_layout(self) -> None:
        text = generate(spec_for("alpine", _names("alpine")))
        assert "ENV PATH=/usr/lib/llvm22/bin:$PATH" in text
        assert text.index("apk add") < text.index("ENV PATH=/usr/lib/llvm22")

    @pytest.mark.parametrize("distro_key", ["fedora", "rhel", "arch"])
    def test_plain_names_need_nothing(self, distro_key: str) -> None:
        assert "/usr/lib/llvm" not in generate(spec_for(distro_key, _names(distro_key)))

    def test_no_llvm_no_path_line(self) -> None:
        assert "/usr/lib/llvm" not in generate(spec_for("ubuntu", ["git"]))

    def test_the_directory_comes_from_the_package_name(self) -> None:
        assert llvm_bin_dir(["git", "clang-23"]) == "/usr/lib/llvm-23/bin"
        assert llvm_bin_dir(["llvm22"]) == "/usr/lib/llvm22/bin"
        assert llvm_bin_dir(["clang-format-22", "clang22-extra-tools"]) == ""
        assert llvm_bin_dir(["clang", "llvm"]) == ""

    @pytest.mark.parametrize("distro_key", DISTRO_KEYS)
    def test_the_smoke_test_uses_the_plain_names(self, distro_key: str) -> None:
        """Which is what proves the PATH line did its job."""
        text = generate(spec_for(distro_key, _names(distro_key)))
        smoke = text.rsplit("RUN ", 1)[1]
        assert "clang --version" in smoke
        assert "llvm-symbolizer --version" in smoke

    def test_a_tools_package_alone_is_not_taken_for_the_compiler(self) -> None:
        smoke = generate(spec_for("fedora", ["clang-tools-extra"])).rsplit("RUN ", 1)[1]
        assert "clang --version" not in smoke


class TestRhelGetsEpel:
    """The comparison counts EPEL as RHEL; UBI ships without it."""

    def test_the_install_enables_it_first(self) -> None:
        text = generate(spec_for("rhel", ["cppcheck"]))
        assert "epel-release-latest-10.noarch.rpm" in text
        assert text.index("epel-release") < text.index("    cppcheck")

    @pytest.mark.parametrize("distro_key", [k for k in DISTRO_KEYS if k != "rhel"])
    def test_nobody_else_does(self, distro_key: str) -> None:
        assert "epel" not in generate(spec_for(distro_key, ["git"]))


class TestOptionalPackages:
    """MinGW and Wine are large and serve one workflow, so they are asked for."""

    @pytest.mark.parametrize("key", ["mingw-ucrt", "wine"])
    def test_off_by_default(self, key: str) -> None:
        assert not EXTRAS_BY_KEY[key].default_on
        assert EXTRAS_BY_KEY[key].kind is ExtraKind.PACKAGES

    def test_mingw_only_where_a_ucrt_build_exists(self) -> None:
        """Ubuntu and RHEL package only the older msvcrt build."""
        offered = {k for k in DISTRO_KEYS if "mingw-ucrt" in {e.key for e in available_for(k, "amd64")}}
        assert offered == {"debian", "fedora", "alpine", "arch"}

    def test_wine_everywhere_but_rhel(self) -> None:
        offered = {k for k in DISTRO_KEYS if "wine" in {e.key for e in available_for(k, "amd64")}}
        assert offered == set(DISTRO_KEYS) - {"rhel"}

    @pytest.mark.parametrize("key", ["mingw-ucrt", "wine"])
    def test_x86_64_only(self, key: str) -> None:
        for distro_key in DISTRO_KEYS:
            assert key not in {e.key for e in available_for(distro_key, "arm64")}

    def test_the_package_names_per_distribution(self) -> None:
        assert extra_packages({"mingw-ucrt"}, "debian", "amd64") == [
            "gcc-mingw-w64-ucrt64", "g++-mingw-w64-ucrt64",
        ]
        assert extra_packages({"mingw-ucrt"}, "arch", "amd64") == ["mingw-w64-gcc"]
        assert extra_packages({"wine"}, "ubuntu", "amd64") == ["wine", "wine64"]

    def test_they_add_packages_and_nothing_else(self) -> None:
        """No command, so no layer of their own: just names in the install."""
        text = generate(spec_for("debian", ["git"], {"mingw-ucrt", "wine"}))
        assert "gcc-mingw-w64-ucrt64" in text and "wine64" in text
        assert len([ln for ln in text.splitlines() if ln.startswith("RUN ")]) == 2

    def test_the_mingw_check_knows_both_compiler_names(self) -> None:
        """Debian and Fedora call it ...mingw32ucrt-gcc, Alpine and Arch not."""
        verify = EXTRAS_BY_KEY["mingw-ucrt"].verify
        assert "x86_64-w64-mingw32ucrt-gcc" in verify
        assert "x86_64-w64-mingw32-gcc" in verify

    def test_the_mingw_check_cannot_hide_an_earlier_failure(self) -> None:
        """An ungrouped || in a chain of && lets the check before it fail silently."""
        verify = EXTRAS_BY_KEY["mingw-ucrt"].verify
        assert verify.startswith("(") and verify.endswith(")")


class TestValgrindOnArch:
    """Arch strips the dynamic loader, so Valgrind dies at startup without help.

    Found by running Valgrind in the built image, where a check that only asked
    for its version would have passed: "a function redirection which is
    mandatory for this platform-tool combination cannot be set up".
    """

    def test_an_arch_image_with_valgrind_gets_debuginfod(self) -> None:
        text = generate(spec_for("arch", ["valgrind"]))
        assert "ENV DEBUGINFOD_URLS=https://debuginfod.archlinux.org" in text

    def test_it_follows_the_install(self) -> None:
        text = generate(spec_for("arch", ["valgrind"]))
        assert text.index("pacman -Syu") < text.index("DEBUGINFOD_URLS")

    def test_not_without_valgrind(self) -> None:
        """GDB would use it too, but asks first and copes without."""
        assert "DEBUGINFOD" not in generate(spec_for("arch", ["gdb", "gcc"]))

    @pytest.mark.parametrize("distro_key", [k for k in DISTRO_KEYS if k != "arch"])
    def test_nowhere_else_needs_it(self, distro_key: str) -> None:
        assert "DEBUGINFOD" not in generate(spec_for(distro_key, ["valgrind"]))
