"""Make lcov 2 capture GoogleTest code built with GCC 14 and later.

GoogleTest's ``TEST()`` expands to a class and its ``TestBody`` on one source
line. GCC reports the class's implicit constructor and destructors on that line,
ending there, and ``TestBody`` starting on the same line but ending at its
closing brace. lcov keys functions by start line, treats everything starting on
one line as aliases of one function, and stops the capture with::

    lcov: ERROR: (inconsistent) mismatched end line for ..._Test8TestBodyEv

Upstream lcov, through master in September 2026, does the same. Its own
suggestions are ``--ignore-errors inconsistent``, which downgrades all 49 checks
in that category and still prints the warnings, 24 of them for a 24-file test
suite, or ``--erase-functions``, which deletes the matched functions' coverage,
test bodies included.

Instead the image routes lcov's gcov through a small wrapper. It reconciles the
end lines of such a group to the highest one, which is exactly what lcov records
when the error is ignored, and only when every shorter function is a single
line. Anything else still reaches lcov unchanged and still fails.
"""

from __future__ import annotations

from collections.abc import Sequence

from .catalog import CATALOG_BY_KEY

WRAPPER_PATH = "/usr/local/bin/gcov-for-lcov"

#: Ends the heredoc that writes the wrapper; must never appear as a script line.
HEREDOC_END = "GCOV_FOR_LCOV"

WRAPPER_SCRIPT = r"""#!/usr/bin/perl
# gcov-for-lcov: run gcov, then give functions that share a start line one end line.
#
# GoogleTest's TEST() expands to a class and its TestBody on one source line.
# GCC 14 and later report the class's implicit constructor and destructors on
# that line, ending there, and TestBody on the same line, ending at the closing
# brace. lcov 2 keys functions by start line, so it stops the capture with
# "(inconsistent) mismatched end line". Were that error ignored, lcov would keep
# the highest end line. This makes the same choice before lcov reads the data,
# and only where every shorter function ends on the line it starts, so a
# genuine inconsistency still fails.
#
# DEVENV_FORGE_GCOV selects another gcov, for example "llvm-cov gcov".
use strict;
use warnings;
use Cwd ();
use Time::HiRes ();

my @gcov = split(' ', $ENV{DEVENV_FORGE_GCOV} || 'gcov');
# lcov 2.0 appends a --gcov-tool from its command line to the configured tool
# instead of replacing it, so the gcov asked for arrives as the first argument.
my $self = Cwd::abs_path($0) // $0;
while (@ARGV && defined(my $program = program($ARGV[0]))) {
    shift(@ARGV);
    next if (Cwd::abs_path($program) // '') eq $self;
    @gcov = ($program);
    last;
}
my @outputs = ('*.gcov.json.gz', '.*.gcov.json.gz');

# Only files this gcov run writes are touched, never another run's.
my %before = map { $_ => signature($_) } map { glob } @outputs;
my $status = system(@gcov, @ARGV);
die("gcov-for-lcov: cannot run $gcov[0]: $!\n") if $status == -1;
exit(128 + ($status & 127)) if $status & 127;
exit($status >> 8) if $status >> 8;

my ($module) = grep { eval "require $_; 1" } qw(JSON::XS Cpanel::JSON::XS JSON::PP);
my $json = $module->new->utf8;

for my $file (map { glob } @outputs) {
    next if defined $before{$file} && $before{$file} eq signature($file);
    # Any failure leaves gcov's own output in place: never worse than no wrapper.
    my $raw = read_gzip($file) // next;
    my $doc = eval { $json->decode($raw) } or next;
    write_gzip($file, $json->encode($doc)) if reconcile($doc);
}
exit 0;

sub reconcile {
    my ($doc) = @_;
    my $changed = 0;
    for my $source (@{ $doc->{files} || [] }) {
        my %by_start;
        for my $fn (@{ $source->{functions} || [] }) {
            push(@{ $by_start{ $fn->{start_line} } }, $fn)
                if defined $fn->{start_line} && defined $fn->{end_line};
        }
        for my $group (values %by_start) {
            my ($end) = sort { $b <=> $a } map { $_->{end_line} } @$group;
            my @shorter = grep { $_->{end_line} != $end } @$group;
            next if !@shorter || grep { $_->{end_line} != $_->{start_line} } @shorter;
            $_->{end_line} = $end for @shorter;
            $changed = 1;
        }
    }
    return $changed;
}

sub signature {
    my @st = Time::HiRes::stat(shift);
    return @st ? "$st[7]:$st[9]" : '';
}

# An executable named by the argument, unless it is an option or gcov data.
sub program {
    my ($name) = @_;
    return undef if $name =~ /^-/ || $name =~ /\.gc(?:da|no)$/;
    for my $dir ($name =~ m{/} ? ('') : split(/:/, $ENV{PATH} || '')) {
        my $path = $dir eq '' ? $name : "$dir/$name";
        return $path if -f $path && -x _;
    }
    return undef;
}

# Through the gzip program, which lcov itself needs to read these files.
sub read_gzip {
    my ($file) = @_;
    open(my $in, '-|', 'gzip', '-dc', '--', $file) or return undef;
    binmode($in);
    my $raw = do { local $/; <$in> };
    return close($in) ? $raw : undef;
}

sub write_gzip {
    my ($file, $text) = @_;
    # Compressed beside the original and renamed over it, so a reader never
    # sees half a file. The name cannot match lcov's *.gcov.json.gz pattern.
    my $tmp = "$file.tmp$$";
    open(my $out, '>:raw', $tmp) or return;
    my $ok = print {$out} $text;
    $ok = close($out) && $ok;
    $ok &&= system('gzip', '-1', '-f', '-q', '--', $tmp) == 0;
    $ok &&= rename("$tmp.gz", $file);
    unlink($tmp, "$tmp.gz") unless $ok;
}
"""

#: A C program with the same shape as TEST(): one macro line holding a
#: single-line function and the start of a longer one. Plain C, so the check
#: needs neither GoogleTest nor a C++ compiler. lcov fails on it without the
#: wrapper.
CHECK_SOURCE = (
    "#define CHECK(name) static int name##_one(void) { return 1; } static int name(void)",
    "CHECK(sample) {",
    "    return sample_one() + 1;",
    "}",
    "int main(void) { return sample() == 2 ? 0 : 1; }",
)

CHECK_DIR = "/tmp/lcov-check"


def installs_lcov(packages: Sequence[str], distro_key: str) -> bool:
    return _has(packages, distro_key, "lcov")


def can_check(packages: Sequence[str], distro_key: str) -> bool:
    """The build-time check compiles and links C.

    That takes the build essentials as well as gcc: on Debian and Alpine, gcc
    alone has no C library start files, and linking fails on Scrt1.o.
    """
    return installs_lcov(packages, distro_key) and all(
        _has(packages, distro_key, key) for key in ("gcc", "base-devel")
    )


def _has(packages: Sequence[str], distro_key: str, key: str) -> bool:
    spec = CATALOG_BY_KEY.get(key)
    return spec is not None and any(name in packages for name in spec.names_for(distro_key))


def section() -> list[str]:
    """Containerfile lines installing the wrapper and pointing lcov at it."""
    script = WRAPPER_SCRIPT.rstrip("\n").splitlines()
    if HEREDOC_END in script:
        raise ValueError(f"the wrapper script contains the heredoc terminator {HEREDOC_END}")
    return [
        "# lcov and GoogleTest. TEST() puts a generated constructor, destructors",
        "# and TestBody on one line, and GCC 14 and later give them different end",
        "# lines. lcov 2 keys functions by start line and stops the capture with",
        '# "(inconsistent) mismatched end line". This gcov wrapper gives such a',
        "# group lcov's own merged end line; any other mismatch still fails.",
        f"COPY --chmod=755 <<'{HEREDOC_END}' {WRAPPER_PATH}",
        *script,
        HEREDOC_END,
        "# lcov 2.1 and later read a system lcovrc only from $LCOV_HOME/etc; lcov",
        "# 2.0 reads /etc/lcovrc directly. This makes every version read that file.",
        "ENV LCOV_HOME=/",
        f"RUN printf '\\n%s\\n' 'geninfo_gcov_tool = {WRAPPER_PATH}' >> /etc/lcovrc",
        "",
    ]


def check_steps() -> list[str]:
    """Smoke-test steps: capture the TEST()-shaped program with plain lcov."""
    source = " ".join(f"'{line}'" for line in CHECK_SOURCE)
    return [
        f"mkdir -p {CHECK_DIR}",
        f"cd {CHECK_DIR}",
        f"printf '%s\\n' {source} > check.c",
        "gcc --coverage -O0 check.c -o check",
        "./check",
        "lcov --quiet --capture --directory . --output-file check.info",
        f"cd / && rm -rf {CHECK_DIR}",
    ]
