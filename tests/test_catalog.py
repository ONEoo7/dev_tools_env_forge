"""Tests for the distro comparison: naming, parsing and resolution.

No network. Every source is fed canned index output taken from the real
services, so the parsers are tested against the shapes they actually meet.
"""

from __future__ import annotations

import pytest

from devenv_forge.core.catalog import (
    CATALOG,
    DISTRO_KEYS,
    DISTROS,
    names_for_distro,
    projects_for_name,
)
from devenv_forge.core.matrix import Cell, Row, version_key
from devenv_forge.core.sources import (
    AlpineSource,
    MadisonSource,
    upstream_version,
)


class TestUpstreamVersion:
    """Packaging noise differs per distro and is not comparable across them."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1:2.47.3-0+deb13u1", "2.47.3"),       # Debian epoch and revision
            ("4.4.1-3", "4.4.1"),                    # plain Debian revision
            ("2.54.0-r0", "2.54.0"),                 # Alpine revision
            ("10.2.1+ds-0ubuntu1", "10.2.1"),        # repacked source
            ("1.85.0+dfsg3", "1.85.0"),              # dfsg repack, no dash
            ("1.93.1ubuntu1", "1.93.1"),             # vendor suffix, no dash
            ("16.2.1+r23+gd564253eb6c8", "16.2.1"),  # Arch VCS suffix
            ("2.44-3+23+b1", "2.44"),                # binNMU
            ("3.14.7", "3.14.7"),                    # already clean
            ("2.5", "2.5"),
        ],
    )
    def test_normalises(self, raw: str, expected: str) -> None:
        assert upstream_version(raw) == expected

    def test_keeps_arm_release_suffix(self) -> None:
        """Regression: a naive 'el' rule truncated 14.2.rel1 to 14.2.r.

        Arm's toolchain versions end in .relN, which overlaps the rpm .elN
        suffix. Stripping it silently produced a wrong, lower version.
        """
        assert upstream_version("15:14.2.rel1-1") == "14.2.rel1"
        assert upstream_version("13.3.rel1") == "13.3.rel1"

    def test_strips_rpm_dist_tag(self) -> None:
        assert upstream_version("2.0.3.el10") == "2.0.3"
        assert upstream_version("1.2.3.fc44") == "1.2.3"

    def test_never_returns_empty(self) -> None:
        assert upstream_version("-") == "-"
        assert upstream_version("weird") == "weird"


class TestVersionKey:
    def test_numeric_ordering_beats_lexical(self) -> None:
        assert version_key("1.10") > version_key("1.9")
        assert version_key("4.4.1") > version_key("4.4")

    def test_release_suffix_sorts(self) -> None:
        assert version_key("14.2.rel1") > version_key("13.3.rel1")

    def test_equal_versions_compare_equal(self) -> None:
        assert version_key("0.12.0") == version_key("0.12.0")


class TestMadisonParsing:
    #: Real output shape from qa.debian.org and the Ubuntu madison CGI.
    SAMPLE = (
        " binutils-arm-none-eabi | 2.44-3+23+b1       | trixie | amd64, arm64\n"
        " binutils-arm-none-eabi | 23                 | trixie | source\n"
        " gcc-arm-none-eabi      | 15:14.2.rel1-1     | trixie | source, amd64\n"
        " gdb-multiarch          | 16.3-1             | trixie | amd64\n"
        " git                    | 1:2.47.3-0+deb13u1 | trixie | source, amd64\n"
        " lcov                   | 2.3.1-1            | resolute/universe | all\n"
    )

    def test_parses_names_and_versions(self) -> None:
        parsed = MadisonSource._parse(self.SAMPLE)
        assert parsed["gcc-arm-none-eabi"].version == "14.2.rel1"
        assert parsed["gdb-multiarch"].version == "16.3"
        assert parsed["git"].version == "2.47.3"

    def test_ignores_the_source_only_row(self) -> None:
        """The source row carries a packaging version, not the real one.

        For binutils-arm-none-eabi that row reads "23", which would otherwise
        be shown as the binutils version.
        """
        parsed = MadisonSource._parse(self.SAMPLE)
        assert parsed["binutils-arm-none-eabi"].version == "2.44"

    def test_records_the_component(self) -> None:
        parsed = MadisonSource._parse(self.SAMPLE)
        assert parsed["lcov"].component == "universe"
        assert parsed["git"].component == ""

    def test_empty_input(self) -> None:
        assert MadisonSource._parse("") == {}

    def test_ignores_malformed_rows(self) -> None:
        parsed = MadisonSource._parse("garbage\n| | |\n name | notaversion | suite | amd64\n")
        assert parsed == {}


class TestAlpineIndex:
    SAMPLE = (
        "C:Q1abc\nP:make\nV:4.4.1-r4\nA:x86_64\n\n"
        "C:Q1def\nP:gcc-arm-none-eabi\nV:16.1.0-r0\nA:x86_64\n\n"
        "C:Q1ghi\nP:lcov\nV:2.3.1-r1\nA:noarch\n"
    )

    def test_parses_index(self) -> None:
        source = AlpineSource("v3.24")
        source._ingest(self.SAMPLE, "main")
        found = source.lookup(["make", "gcc-arm-none-eabi", "lcov"])
        assert found["make"].version == "4.4.1"
        assert found["gcc-arm-none-eabi"].version == "16.1.0"
        assert found["lcov"].version == "2.3.1"

    def test_main_wins_over_community(self) -> None:
        source = AlpineSource("v3.24")
        source._ingest("P:tool\nV:1.0-r0\n", "main")
        source._ingest("P:tool\nV:9.9-r0\n", "community")
        assert source.lookup(["tool"])["tool"].component == "main"

    def test_absent_name(self) -> None:
        source = AlpineSource("v3.24")
        source._ingest(self.SAMPLE, "main")
        assert source.lookup(["nosuchpkg"]) == {}


class TestCatalogIntegrity:
    def test_every_package_covers_every_distro(self) -> None:
        for spec in CATALOG:
            missing = set(DISTRO_KEYS) - set(spec.names)
            assert not missing, f"{spec.key} has no name for {missing}"

    def test_names_are_non_empty(self) -> None:
        for spec in CATALOG:
            for key, names in spec.names.items():
                assert names, f"{spec.key} has an empty name list for {key}"
                assert all(n.strip() for n in names)

    def test_keys_are_unique(self) -> None:
        keys = [spec.key for spec in CATALOG]
        assert len(keys) == len(set(keys))

    def test_distro_keys_are_unique(self) -> None:
        keys = [d.key for d in DISTROS]
        assert len(keys) == len(set(keys))

    def test_names_for_distro_skips_meta_and_dedupes(self) -> None:
        names = names_for_distro("debian")
        assert "build-essential" not in names  # the meta-package has no version
        assert len(names) == len(set(names))
        assert "gcc-arm-none-eabi" in names

    def test_arm_names_differ_per_distro(self) -> None:
        """The whole point of the mapping: the Arm compiler is named differently."""
        spec = next(s for s in CATALOG if s.key == "arm-gcc")
        assert spec.names["debian"][0] == "gcc-arm-none-eabi"
        assert spec.names["arch"][0] == "arm-none-eabi-gcc"
        assert spec.names["fedora"][0] == "arm-none-eabi-gcc-cs"

    def test_projects_lookup_resolves(self) -> None:
        assert "gcc" in projects_for_name("gcc-arm-none-eabi")
        assert projects_for_name("no-such-package") == ()

    def test_only_rhel_is_a_proxy(self) -> None:
        proxies = [d.key for d in DISTROS if d.proxy_note]
        assert proxies == ["rhel"]


class TestRowHighlighting:
    @staticmethod
    def _row(**versions) -> Row:
        cells = {}
        for key in DISTRO_KEYS:
            value = versions.get(key)
            cells[key] = Cell(
                distro=key, version=value or "", available=bool(value)
            )
        return Row(spec=CATALOG[0], cells=cells)

    def test_marks_the_single_newest(self) -> None:
        row = self._row(debian="1.0", arch="2.0", ubuntu="1.5")
        assert row.newest_keys() == {"arch"}

    def test_marks_ties(self) -> None:
        row = self._row(debian="2.0", arch="2.0", ubuntu="1.0")
        assert row.newest_keys() == {"debian", "arch"}

    def test_numeric_not_lexical(self) -> None:
        row = self._row(debian="1.9", arch="1.10")
        assert row.newest_keys() == {"arch"}

    def test_nothing_available(self) -> None:
        assert self._row().newest_keys() == set()

    def test_meta_rows_are_not_ranked(self) -> None:
        cells = {k: Cell(distro=k, available=True, meta=True) for k in DISTRO_KEYS}
        assert Row(spec=CATALOG[0], cells=cells).newest_keys() == set()

    def test_missing_count(self) -> None:
        row = self._row(debian="1.0", arch="2.0")
        assert row.missing_count == len(DISTRO_KEYS) - 2


class TestArchDatabaseParsing:
    """Arch is read from the repository databases, not the website API.

    The API answers HTTP 429 under concurrency, and a silent version of that
    turned five real packages into apparent absences.
    """

    @staticmethod
    def _db(entries: list[tuple[str, str]]) -> bytes:
        import io
        import tarfile

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, version in entries:
                body = f"%NAME%\n{name}\n\n%VERSION%\n{version}\n".encode()
                info = tarfile.TarInfo(f"{name}-{version}/desc")
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
        return buffer.getvalue()

    def test_parses_db_entries(self) -> None:
        import io
        import tarfile

        from devenv_forge.core.sources import ArchSource

        raw = self._db([("gcc", "16.2.1+r23+gd564253eb6c8-1"), ("lcov", "2.5-1")])
        source = ArchSource()
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            source._ingest(archive, "core")
        found = source.lookup(["gcc", "lcov"])
        assert found["gcc"].version == "16.2.1"
        assert found["lcov"].version == "2.5"
        assert found["gcc"].component == "core"

    def test_core_wins_over_extra(self) -> None:
        import io
        import tarfile

        from devenv_forge.core.sources import ArchSource

        source = ArchSource()
        for component, version in (("core", "1.0-1"), ("extra", "9.9-1")):
            with tarfile.open(fileobj=io.BytesIO(self._db([("tool", version)]))) as a:
                source._ingest(a, component)
        assert source.lookup(["tool"])["tool"].component == "core"


class TestFailuresAreNotAbsences:
    def test_failed_lookup_is_not_reported_as_missing(self) -> None:
        """A transport error must never read as "not packaged"."""
        from devenv_forge.core.catalog import DISTROS_BY_KEY
        from devenv_forge.core.matrix import _cell_for

        spec = next(s for s in CATALOG if s.key == "lcov")
        distro = DISTROS_BY_KEY["arch"]

        absent = _cell_for(spec, distro, {}, set())
        assert absent.display == "not packaged"
        assert not absent.error

        failed = _cell_for(spec, distro, {}, {"lcov"})
        assert failed.display == "error"
        assert failed.error

    def test_a_hit_is_unaffected_by_other_failures(self) -> None:
        from devenv_forge.core.catalog import DISTROS_BY_KEY
        from devenv_forge.core.matrix import _cell_for
        from devenv_forge.core.sources import Found

        spec = next(s for s in CATALOG if s.key == "lcov")
        distro = DISTROS_BY_KEY["arch"]
        cell = _cell_for(spec, distro, {"lcov": Found("2.5", "lcov")}, {"other"})
        assert cell.available and cell.version == "2.5"


class TestEndpointOverride:
    def test_environment_variable_wins(self, monkeypatch) -> None:
        from devenv_forge.core import sources

        monkeypatch.setenv("DEVENV_FORGE_ALPINE_URL", "https://mirror.example/alpine/")
        assert sources.endpoint("alpine") == "https://mirror.example/alpine"

    def test_default_is_used_when_unset(self, monkeypatch) -> None:
        from devenv_forge.core import sources

        monkeypatch.delenv("DEVENV_FORGE_FEDORA_URL", raising=False)
        assert sources.endpoint("fedora") == sources.DEFAULT_ENDPOINTS["fedora"]


class TestRepodataParsing:
    """RHEL reads yum repodata directly, which is exact and binary-name aware."""

    @staticmethod
    def _primary(entries: list[tuple[str, str]]) -> bytes:
        body = ['<metadata xmlns="http://linux.duke.edu/metadata/common">']
        for name, version in entries:
            body.append(
                f"<package><name>{name}</name>"
                f'<version epoch="0" ver="{version}" rel="1.el10"/></package>'
            )
        body.append("</metadata>")
        return "".join(body).encode()

    def test_parses_names_and_versions(self) -> None:
        from devenv_forge.core.sources import RepodataSource

        source = RepodataSource({})
        source._ingest(self._primary([("gcc", "14.3.1"), ("lcov", "2.0")]), "BaseOS")
        found = source.lookup(["gcc", "lcov"])
        assert found["gcc"].version == "14.3.1"
        assert found["lcov"].component == "BaseOS"

    def test_first_repository_wins(self) -> None:
        from devenv_forge.core.sources import RepodataSource

        source = RepodataSource({})
        source._ingest(self._primary([("tool", "1.0")]), "BaseOS")
        source._ingest(self._primary([("tool", "9.9")]), "EPEL")
        assert source.lookup(["tool"])["tool"].component == "BaseOS"

    def test_absent_name(self) -> None:
        from devenv_forge.core.sources import RepodataSource

        source = RepodataSource({})
        source._ingest(self._primary([("gcc", "14.3.1")]), "BaseOS")
        assert source.lookup(["nosuchpkg"]) == {}

    def test_gzip_and_zstd_are_both_accepted(self) -> None:
        import gzip

        from compression import zstd

        from devenv_forge.core.sources import RepodataSource

        raw = self._primary([("gcc", "1.0")])
        assert RepodataSource._decompress(gzip.compress(raw)) == raw
        # EPEL ships zstd rather than gzip.
        assert RepodataSource._decompress(zstd.compress(raw)) == raw
        assert RepodataSource._decompress(raw) == raw


class TestMissingVersusFailed:
    """mdapi answers 400, not 404, for a package it does not carry.

    Treating that as a transport failure made every Fedora run look partly
    broken and stopped its results being cached at all.
    """

    def test_missing_codes_return_empty(self, monkeypatch) -> None:
        import urllib.error

        from devenv_forge.core import sources

        def fake_open(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 400, "Bad", {}, None)

        monkeypatch.setattr(sources.urllib.request, "urlopen", fake_open)
        assert sources.http_get("https://x/y", missing_codes=(400, 404)) == ""

    def test_other_codes_raise(self, monkeypatch) -> None:
        import urllib.error

        import pytest as _pytest

        from devenv_forge.core import sources

        def fake_open(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 429, "Slow down", {}, None)

        monkeypatch.setattr(sources.urllib.request, "urlopen", fake_open)
        with _pytest.raises(sources.LookupError_):
            sources.http_get("https://x/y")


class TestLcovJsonModule:
    """lcov warns and captures about six times slower without JSON::XS.

    Measured on lcov 2.5 in the Arch image: 10.3 s with JSON::PP against 1.7 s
    with JSON::XS for the same 24-file GoogleTest project, with identical output.
    """

    #: The first candidate on each distro, as found in its own package index.
    INDEX_NAMES = {
        "ubuntu": "libjson-xs-perl",
        "debian": "libjson-xs-perl",
        "fedora": "perl-JSON-XS",
        "rhel": "perl-JSON-XS",
        "alpine": "perl-json-xs",
        "arch": "perl-json-xs",
    }

    @staticmethod
    def _spec(key: str):
        return next(s for s in CATALOG if s.key == key)

    def test_sits_beside_lcov(self) -> None:
        keys = [spec.key for spec in CATALOG]
        assert keys.index("json-xs") == keys.index("lcov") + 1
        assert self._spec("json-xs").group == self._spec("lcov").group

    def test_prefers_json_xs_then_the_cpanel_fork(self) -> None:
        """lcov's own order: JSON::XS, Cpanel::JSON::XS, then the slow JSON::PP."""
        spec = self._spec("json-xs")
        for distro, first in self.INDEX_NAMES.items():
            names = spec.names_for(distro)
            assert names[0] == first
            assert len(names) == 2 and "cpanel" in names[1].lower()

    def test_both_candidates_are_queried(self) -> None:
        names = names_for_distro("debian")
        assert "libjson-xs-perl" in names and "libcpanel-json-xs-perl" in names

    def test_falls_back_to_the_fork(self) -> None:
        from devenv_forge.core.catalog import DISTROS_BY_KEY
        from devenv_forge.core.matrix import _cell_for
        from devenv_forge.core.sources import Found

        cell = _cell_for(
            self._spec("json-xs"),
            DISTROS_BY_KEY["arch"],
            {"perl-cpanel-json-xs": Found("4.40", "perl-cpanel-json-xs")},
        )
        assert cell.available and cell.package == "perl-cpanel-json-xs"

    def test_is_not_an_msys2_package(self) -> None:
        assert self._spec("json-xs").msys2 == ""

    def test_only_rows_added_on_purpose_lack_an_msys2_source(self) -> None:
        """Provenance stays auditable: a row with no MSYS2 source is one added
        deliberately and listed here, not one that forgot to say where it came
        from. The Clang, analysis and coverage rows came from a later review of
        what a C/C++ quality toolchain needs, not from the MSYS2 list."""
        added_for_the_image = {
            "json-xs",
            "clang", "clang-format", "clang-tidy", "clangd", "llvm", "clang-rt",
            "gcc-asan", "gcc-ubsan", "cppcheck", "valgrind", "pre-commit",
            "ccache", "gcovr",
        }
        assert {spec.key for spec in CATALOG if not spec.msys2} == added_for_the_image

    @pytest.mark.parametrize("distro_key", sorted(INDEX_NAMES))
    def test_resolved_rows_reach_the_containerfile(self, distro_key: str) -> None:
        """The build page installs every available row; this follows the same path."""
        from devenv_forge.core.catalog import DISTROS_BY_KEY
        from devenv_forge.core.containerfile import ImageSpec, generate
        from devenv_forge.core.matrix import _cell_for
        from devenv_forge.core.sources import Found

        distro = DISTROS_BY_KEY[distro_key]
        found = {
            "lcov": Found("2.5", "lcov"),
            self.INDEX_NAMES[distro_key]: Found("4.04", self.INDEX_NAMES[distro_key]),
        }
        packages = [
            cell.package
            for spec in (self._spec("lcov"), self._spec("json-xs"))
            if (cell := _cell_for(spec, distro, found)).available
        ]
        text = generate(ImageSpec(distro=distro, packages=packages))
        install = text[text.index("# Distribution packages"):]
        lines = [line.strip(" \\") for line in install.splitlines()]
        assert lines[lines.index("lcov") + 1] == self.INDEX_NAMES[distro_key]
