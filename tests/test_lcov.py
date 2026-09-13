"""lcov 2 with GoogleTest under GCC 14 and later.

The generation tests always run. The wrapper tests run the real Perl script
against a fake gcov, so they need perl and gzip on PATH and skip otherwise.
The whole fix was also verified in built images; see the README.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from devenv_forge.core import lcov
from devenv_forge.core.catalog import DISTROS, DISTROS_BY_KEY
from devenv_forge.core.containerfile import ImageSpec, generate


def _generate(packages, extras=frozenset(), distro="arch") -> list[str]:
    spec = ImageSpec(distro=DISTROS_BY_KEY[distro], packages=list(packages),
                     selected_extras=set(extras))
    return generate(spec).splitlines()


class TestGeneration:
    def test_nothing_without_lcov(self) -> None:
        text = "\n".join(_generate(["gcc", "cmake"]))
        assert lcov.WRAPPER_PATH not in text
        assert "LCOV_HOME" not in text

    @pytest.mark.parametrize("distro", [d.key for d in DISTROS])
    def test_every_distro_gets_the_wrapper(self, distro: str) -> None:
        text = "\n".join(_generate(["lcov"], distro=distro))
        assert f"<<'{lcov.HEREDOC_END}' {lcov.WRAPPER_PATH}" in text

    def test_heredoc_carries_the_script_verbatim(self) -> None:
        lines = _generate(["gcc", "lcov"])
        start = next(i for i, line in enumerate(lines) if line.startswith("COPY --chmod=755 <<"))
        end = lines.index(lcov.HEREDOC_END, start)
        assert "\n".join(lines[start + 1:end]) + "\n" == lcov.WRAPPER_SCRIPT

    def test_delimiter_is_quoted_so_perl_variables_survive(self) -> None:
        """Unquoted, the builder would expand $ENV, $file and the rest."""
        copy = next(line for line in _generate(["lcov"]) if line.startswith("COPY"))
        assert f"<<'{lcov.HEREDOC_END}'" in copy

    def test_script_never_ends_the_heredoc_early(self) -> None:
        assert lcov.HEREDOC_END not in lcov.WRAPPER_SCRIPT.splitlines()

    def test_every_lcov_version_reads_the_setting(self) -> None:
        """2.1 and later look only under $LCOV_HOME/etc; 2.0 reads /etc/lcovrc."""
        lines = _generate(["lcov"])
        assert "ENV LCOV_HOME=/" in lines
        assert any(
            line.startswith("RUN printf")
            and f"geninfo_gcov_tool = {lcov.WRAPPER_PATH}" in line
            and line.endswith(">> /etc/lcovrc")
            for line in lines
        )

    def test_comes_after_the_slow_layers(self) -> None:
        lines = _generate(["gcc", "lcov", "cmake"], extras={"probe-rs-tools", "googletest"})
        section = lines.index(next(line for line in lines if line.startswith("COPY --chmod")))
        rust = next(i for i, line in enumerate(lines) if "cargo install" in line)
        sdk = next(i for i, line in enumerate(lines) if line.startswith("ARG GOOGLETEST_VERSION"))
        workdir = lines.index("WORKDIR /work")
        assert rust < section and sdk < section < workdir

    def test_smoke_test_captures_the_test_shape_last(self) -> None:
        text = "\n".join(_generate(["gcc", "base-devel", "lcov"]))
        smoke = text[text.rindex("RUN "):]
        assert "gcc --coverage -O0 check.c -o check" in smoke
        assert smoke.rstrip().endswith(f"cd / && rm -rf {lcov.CHECK_DIR}")
        # Plain lcov: the check proves the configured wrapper, not a flag.
        capture = next(line for line in smoke.splitlines() if "lcov --quiet --capture" in line)
        assert "--gcov-tool" not in capture and "--ignore-errors" not in capture

    def test_no_check_without_a_compiler(self) -> None:
        text = "\n".join(_generate(["lcov", "base-devel"]))
        assert lcov.WRAPPER_PATH in text
        assert "check.c" not in text

    @pytest.mark.parametrize("distro, essentials", [("debian", "build-essential"), ("alpine", "alpine-sdk")])
    def test_no_check_without_the_build_essentials(self, distro: str, essentials: str) -> None:
        """Measured on Debian: gcc alone cannot link, "cannot find Scrt1.o"."""
        assert "check.c" not in "\n".join(_generate(["gcc", "lcov"], distro=distro))
        assert "check.c" in "\n".join(_generate(["gcc", essentials, "lcov"], distro=distro))

    def test_check_source_survives_single_quoting(self) -> None:
        assert all("'" not in line for line in lcov.CHECK_SOURCE)

    def test_check_source_has_the_colliding_shape(self) -> None:
        """A one-line function and the start of a longer one on the same line."""
        macro, use, *_ = lcov.CHECK_SOURCE
        assert macro.startswith("#define CHECK(name)") and macro.count("(void)") == 2
        assert use.endswith("{")


# ---------------------------------------------------------------------------
# the wrapper itself
# ---------------------------------------------------------------------------

PERL = shutil.which("perl")
GZIP = shutil.which("gzip")

FAKE_GCOV = r"""#!/usr/bin/perl
# Stands in for gcov: writes $FAKE_JSON as fake.gcov.json.gz, exits $FAKE_EXIT.
use strict;
use warnings;
my $code = $ENV{FAKE_EXIT} || 0;
if ($ENV{FAKE_JSON}) {
    open(my $in, '<', $ENV{FAKE_JSON}) or die $!;
    my $text = do { local $/; <$in> };
    open(my $out, '>', 'fake.gcov.json') or die $!;
    print {$out} $text;
    close($out);
    system('gzip', '-f', 'fake.gcov.json') == 0 or die 'gzip failed';
}
if ($ENV{FAKE_CORRUPT}) {
    open(my $out, '>', 'fake.gcov.json.gz') or die $!;
    print {$out} 'not gzip';
    close($out);
}
exit $code;
"""


def _fn(name: str, start: int, end: int | None) -> dict:
    record = {"name": name, "start_line": start, "execution_count": 1}
    if end is not None:
        record["end_line"] = end
    return record


def _document(*functions: dict) -> dict:
    return {"format_version": "2", "files": [{"file": "t.cpp", "functions": list(functions)}]}


def _read(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _write(path: Path, document: dict) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(document, handle)


def _ends(document: dict) -> dict[str, int | None]:
    return {f["name"]: f.get("end_line") for f in document["files"][0]["functions"]}


@pytest.mark.skipif(PERL is None or GZIP is None, reason="needs perl and gzip on PATH")
class TestWrapper:
    @pytest.fixture
    def run(self, tmp_path: Path):
        work = tmp_path.as_posix()
        if " " in work:
            pytest.skip("the fake gcov path must not contain spaces")
        wrapper = tmp_path / "gcov-for-lcov"
        wrapper.write_text(lcov.WRAPPER_SCRIPT, encoding="utf-8", newline="\n")
        fake = tmp_path / "fakegcov"
        fake.write_text(FAKE_GCOV, encoding="utf-8", newline="\n")
        out = tmp_path / "out"
        out.mkdir()

        wrapper.chmod(0o755)

        def run(document: dict | None = None, *, exit_code: int = 0, corrupt: bool = False,
                gcov: str | None = None, leading: tuple[str, ...] = ()):
            env = dict(os.environ)
            env["DEVENV_FORGE_GCOV"] = gcov or f"perl {fake.as_posix()}"
            env["FAKE_EXIT"] = str(exit_code)
            if document is not None:
                source = tmp_path / "document.json"
                source.write_text(json.dumps(document), encoding="utf-8")
                env["FAKE_JSON"] = source.as_posix()
            if corrupt:
                env["FAKE_CORRUPT"] = "1"
            result = subprocess.run(
                [PERL, wrapper.as_posix(), *leading, "t.cpp.gcda"],
                cwd=out, env=env, capture_output=True, text=True, timeout=60,
            )
            return result, out / "fake.gcov.json.gz"

        run.fake = fake.as_posix()
        run.wrapper = wrapper.as_posix()
        return run

    def test_test_shape_gets_the_longest_end_line(self, run) -> None:
        """The reported case: constructor and destructors end on the TEST() line."""
        result, output = run(_document(
            _fn("crc16_ccitt", 8, 18),
            _fn("Crc16_Test_C2", 20, 20),
            _fn("Crc16_Test_D0", 20, 20),
            _fn("Crc16_Test_D2", 20, 20),
            _fn("Crc16_Test_TestBody", 20, 24),
        ))
        assert result.returncode == 0, result.stderr
        assert _ends(_read(output)) == {
            "crc16_ccitt": 18,
            "Crc16_Test_C2": 24, "Crc16_Test_D0": 24, "Crc16_Test_D2": 24,
            "Crc16_Test_TestBody": 24,
        }

    def test_order_does_not_matter(self, run) -> None:
        result, output = run(_document(_fn("body", 43, 49), _fn("dtor", 43, 43)))
        assert result.returncode == 0
        assert _ends(_read(output)) == {"body": 49, "dtor": 49}

    def test_a_genuine_mismatch_is_left_for_lcov_to_report(self, run) -> None:
        """A shorter function spanning several lines is not the macro shape."""
        before = _document(_fn("a", 20, 22), _fn("b", 20, 20), _fn("c", 20, 24))
        result, output = run(before)
        assert result.returncode == 0
        assert _read(output) == before

    def test_agreeing_functions_are_untouched(self, run) -> None:
        """MOCK_METHOD puts two one-line functions on one line; they already agree."""
        before = _document(_fn("write", 35, 35), _fn("gmock_write", 35, 35))
        result, output = run(before)
        assert _read(output) == before

    def test_records_without_end_lines_are_untouched(self, run) -> None:
        before = _document(_fn("old_gcov", 5, None), _fn("other", 5, 9))
        result, output = run(before)
        assert _read(output) == before

    def test_a_failing_gcov_passes_its_exit_code_through(self, run) -> None:
        result, output = run(_document(_fn("x", 1, 1), _fn("y", 1, 3)), exit_code=3)
        assert result.returncode == 3
        # Written by gcov but not processed, because gcov reported failure.
        assert _ends(_read(output)) == {"x": 1, "y": 3}

    def test_files_from_another_run_are_left_alone(self, run, tmp_path: Path) -> None:
        earlier = tmp_path / "out" / "earlier.gcov.json.gz"
        before = _document(_fn("x", 1, 1), _fn("y", 1, 3))
        _write(earlier, before)
        result, _output = run(None)
        assert result.returncode == 0
        assert _read(earlier) == before

    def test_a_gcov_tool_appended_by_lcov_2_0_is_the_gcov_run(self, run) -> None:
        """lcov 2.0 turns --gcov-tool X into "gcov-for-lcov X file.gcda".

        Measured on Fedora 44: without this, gcov was handed X as a data file
        and the capture failed with "GCOV failed".
        """
        document = _document(_fn("ctor", 20, 20), _fn("body", 20, 24))
        result, output = run(document, gcov="definitely-not-a-gcov", leading=("perl", run.fake))
        assert result.returncode == 0, result.stderr
        assert _ends(_read(output)) == {"ctor": 24, "body": 24}

    def test_the_wrapper_named_again_is_not_run_as_gcov(self, run) -> None:
        """--gcov-tool gcov-for-lcov under lcov 2.0 must not recurse."""
        document = _document(_fn("ctor", 20, 20), _fn("body", 20, 24))
        result, output = run(document, gcov="definitely-not-a-gcov",
                             leading=(run.wrapper, "perl", run.fake))
        assert result.returncode == 0, result.stderr
        assert _ends(_read(output)) == {"ctor": 24, "body": 24}

    def test_options_and_data_files_are_never_taken_for_gcov(self, run) -> None:
        result, output = run(_document(_fn("a", 1, 1), _fn("b", 1, 2)), leading=("--json-format",))
        assert result.returncode == 0, result.stderr
        assert _ends(_read(output)) == {"a": 2, "b": 2}

    def test_corrupt_output_is_left_in_place(self, run) -> None:
        result, output = run(None, corrupt=True)
        assert result.returncode == 0
        assert output.read_bytes() == b"not gzip"
