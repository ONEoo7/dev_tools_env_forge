# DevEnv Forge

A desktop tool for building Podman development environment images and sharing
them with a team. Python 3.14, uv, PyQt6.

Primary target is Windows 11; macOS and Linux back ends exist behind the same
interface.

## Status

Stages 1 to 4 are implemented: the environment preflight, the base image
comparison, the build image page, and deploying a container. Distribute is
visible in the sidebar but not yet built.

## Running

```bash
uv run devenv-forge
```

Headless preflight, for a terminal or a build agent. Exit code is 0 when nothing
needs attention and 1 otherwise:

```bash
uv run devenv-forge --check -v
```

Headless distro comparison:

```bash
uv run devenv-forge --compare
```

```bash
uv run pytest
```

## What the preflight checks

| Check | Question |
| --- | --- |
| Operating system | Which OS, and is it supported? Windows 11 is build 22000 or higher. |
| Podman CLI | Is the podman engine installed and reachable? |
| WSL2 backend | Is WSL2 present, and does it default to version 2? |
| Machine provider | Will podman use WSL rather than Hyper-V? |
| Podman machine | Does a Linux machine exist, and is it running? Creates and starts one if not. |
| VS Code Dev Containers | Is the Dev Containers extension installed? Optional. |
| Dev Containers engine | Does the extension use podman rather than Docker? Optional. |
| Container Tools engine | Does VS Code's CONTAINERS view use podman? Optional. |

The VS Code checks are optional. They are reported and fixable like the
others, but never lock the later stages: an unconfigured editor must not stop
anyone building or running an image.

### The podman machine

Podman on Windows and macOS is a client: every image is built and kept inside a
Linux machine it manages, and there is none until something creates it. A fresh
`winget install RedHat.Podman` therefore leaves a working `podman --version` and
nothing that can build.

That state used to be reported as a note, which read as "usable" when nothing
was buildable yet. It is a repairable check instead, and its fix runs

```
podman machine init podman-machine-default
podman machine start podman-machine-default
```

Four details decide whether that fix behaves:

- **Both steps, in one fix.** `init` alone leaves a machine that is present and
  stopped, which is the same dead end one step further along.
- **"Already exists" is not a failure.** podman exits non-zero for a machine
  that is already there, and so does `start` for one already running. Those two
  outputs are tolerated; any other non-zero exit fails with podman's own words.
- **Success is podman's listing, not the exit code.** After starting, the
  machine list has to actually report a running machine. A machine that starts
  and then dies would otherwise be announced as ready.
- **The environment is repaired first.** `machine init` writes its connection
  record under `%APPDATA%` and finds the machine through the user profile. A
  process missing those variables creates a machine it cannot then reach, which
  is the "running but unreachable" trap described below.

On Windows the machine is a WSL2 distribution of its own, so it appears in
`wsl --list --verbose` as `podman-machine-default` next to whatever else is
installed there. The check says so, because an empty-looking `wsl -l -v` is the
usual reason people go looking.

A machine that exists but is stopped gets the start step on its own: nothing is
downloaded and the images already inside it are kept.

### Dev Containers with podman

The fix sets `dev.containers.dockerPath` in the VS Code user settings. Three
details decide whether that actually works:

- **It has to be the user settings file.** The extension declares the setting
  with application scope, so it is silently ignored in a project's
  `.vscode/settings.json`.
- **It is podman's full path, not the bare name.** The extension accepts either,
  but a VS Code window that was open before podman was installed kept its old
  PATH and cannot find `podman`.
- **The settings file is edited, not rewritten.** VS Code settings allow comments
  and trailing commas. Only the one key is spliced in, so comments, ordering and
  every other setting survive. The file is backed up first, the result is parsed
  before it replaces the original, and a file that does not parse is left alone.

### Container Tools with podman

VS Code's CONTAINERS, IMAGES and REGISTRIES views do not come from Dev
Containers. They belong to Container Tools, `ms-azuretools.vscode-containers`,
formerly the Docker extension, which reads its own settings. Configuring Dev
Containers therefore leaves those views saying "Failed to connect. Is Docker
installed?". The fix sets two settings and needs a VS Code restart, because the
extension reads the client choice only when it starts:

| Setting | Value |
| --- | --- |
| `containers.containerClient` | `com.microsoft.visualstudio.containers.podman` |
| `containers.containerCommand` | podman's full path, **unquoted** |

**The setting's own description is wrong about quoting.** It says a path
containing whitespace "needs to be quoted appropriately", and podman lives under
`Program Files`. The extension's bundled process library says otherwise. It
returns a path that has a directory unchanged, then adds quotes itself, and only
when it runs the command through a shell:

| Value written | Through a shell | Spawned directly |
| --- | --- | --- |
| Raw path | works, quotes added for you | works |
| Quoted path | works | fails, quotes become part of the file name |

So the raw path is written, and a quoted value already in someone's settings is
flagged for repair rather than accepted.

**The fix refuses to run from inside a packaged Windows app.** VS Code's user
settings live under `AppData\Roaming`, which a packaged (MSIX) app redirects
into its private storage, the same trap that stranded podman's connections. Before
writing, the tool drops a uniquely named probe into `AppData\Roaming` and looks
for it in every package's private storage. If it lands there, nothing is written,
and it says why, rather than reporting success for a change VS Code would never
see.

### The podman lookup cascade

The lookup follows three stages, and stops at the first that succeeds.

1. **PATH.** `podman` resolves and answers `--version`. Nothing to do.
2. **Installed applications.** Nothing on PATH, but an executable exists on
   disk. Sources searched are the uninstall registry across all three views,
   winget's inventory, and the directories installers conventionally use. What
   happens next depends on the registry, not on this process:
   - the directory is already on the stored machine or user PATH, so the install
     is correct and only this process holds a stale environment. Reported as
     ready, with a note that older terminals need reopening.
   - the directory is on neither, so the fix appends it to the user PATH.
3. **Not present.** Install `RedHat.Podman` with winget, which prompts for
   administrator approval.

Stage 2 is evidence-based, never name-based. A candidate counts only once it has
actually answered `podman --version`. This matters because **Podman Desktop is
not the podman engine**: it appears in the installed-programs list under a name
containing "podman" but ships no `podman.exe`. A name match would report the
engine as present and every later stage would then fail.

## Base image comparison

Compares one embedded toolchain across Ubuntu, Debian, Fedora, RHEL, Alpine and
Arch. The package list is derived from a working MSYS2 setup and covers the Arm
bare-metal toolchain, QEMU, OpenOCD, the build system, Python and coverage
tooling. The newest version in each row is highlighted, and selecting a distro
generates the install line with that distro's package names.

**Versions are read live, never stored.** A hardcoded table would be wrong
within weeks. Only the name mapping lives in the source, in
[catalog.py](src/devenv_forge/core/catalog.py).

**Each distro is queried through its own index**, because binary package names
are what you install and only the native indexes know them:

| Distro | Endpoint | Shape |
| --- | --- | --- |
| Debian | `qa.debian.org/madison.php` | one bulk query |
| Ubuntu | `people.canonical.com/~ubuntu-archive/madison.cgi` | one bulk query |
| Alpine | `dl-cdn.alpinelinux.org` | APKINDEX download |
| Arch | `geo.mirror.pkgbuild.com` | `core.db` and `extra.db` download |
| Fedora | `mdapi.fedoraproject.org` | one request per package, in parallel |
| RHEL | `mirror.stream.centos.org` and the EPEL redirector | yum repodata download |

Every endpoint can be redirected with `DEVENV_FORGE_<DISTRO>_URL`, which is how
you would point at a mirror closer to you. Mirror choice dominates everything
else: the same 4 MB EPEL index took 45 seconds from one mirror and 0.9 from
another.

```bash
DEVENV_FORGE_ALPINE_URL=https://mirror.example.org/alpine uv run devenv-forge --compare
```

An aggregator is not enough. It tracks source packages, and on the apt distros
`binutils` and `binutils-arm-none-eabi` come from the same source, so the Arm
rows cannot be told apart. RHEL used an aggregate at first and six of its
twenty values were wrong, including Python reported as 3.14 where the real
default is 3.12. Reading the CentOS Stream repodata directly fixed that and is
also faster.

Names diverge more than versions do, which is half the value of the table. The
Arm compiler is `gcc-arm-none-eabi` on Debian, `arm-none-eabi-gcc-cs` on Fedora
and `arm-none-eabi-gcc` on Arch. Debian and Ubuntu dropped the dedicated Arm GDB
in favour of `gdb-multiarch`.

**One row is not from the MSYS2 list: JSON::XS for LCOV.** lcov 2 reads gcov's
JSON output through a Perl JSON module and prefers JSON::XS. No distribution
makes that module a hard dependency of lcov; Arch lists `perl-json-xs` only as
optional. A plain `lcov` install therefore falls back to the pure-Perl JSON::PP
and warns on every run:

```
lcov: WARNING: using JSON module "JSON::PP" - which is much slower than some alternatives.  Consider installing one of JSON::XS or Cpanel::JSON::XS
```

The row installs it as `libjson-xs-perl` on Debian and Ubuntu, `perl-JSON-XS` on
Fedora and RHEL, and `perl-json-xs` on Alpine and Arch. The Cpanel::JSON::XS fork
is the fallback candidate, and lcov accepts it too. Measured with lcov 2.5 in the
Arch image, capturing the same 24-file GoogleTest project:

| JSON module | Capture | Warning |
| --- | --- | --- |
| JSON::PP | 10.3 s | on every run |
| JSON::XS | 1.7 s | none |

The coverage data was identical either way.

Versions are normalised before comparison, since packaging noise is not
comparable across distros. `1:2.47.3-0+deb13u1` and `2.54.0-r0` and
`16.2.1+r23+gd564253eb6c8` reduce to their upstream versions. A package coming
from a testing repository is coloured differently rather than presented as
shipped.

**A failed lookup is never shown as a missing package.** Arch's website search
API answers HTTP 429 under concurrency, and treating that silently as "no such
package" turned five real packages into apparent absences. Arch is now read from
the repository databases instead, which have no such limit, and any lookup that
does fail is reported as unknown with the reason.

### Speed

The six distros are queried concurrently, so a cold run costs the slowest single
index rather than the sum of all six. Resolved versions are then cached for 12
hours. The cache is keyed on the package list, so editing the catalogue
invalidates it, and a run with failures is never cached.

| Run | Cost |
| --- | --- |
| Cold, everything live | about 6 seconds |
| Warm | instant |

RHEL is now the slowest at roughly 6 seconds, because it downloads about 11 MB
of repodata. Fedora is the only index with no bulk endpoint, so its requests run
concurrently; serially they took about eleven seconds on their own.

The progress bar carries a live elapsed clock and names the index being waited
on, and the finished view reports the total and the slowest source. Both the
graphical and `--compare` views show per-distro timings, so a slow mirror is
visible rather than just felt.

## Build image and Extras

The build page turns a chosen distribution plus a set of extras into a
Containerfile, and runs `podman build` on it.

**Extras are the toolchain pieces no distribution packages.** They come from
rustup and cargo, so they cannot appear in the base image comparison:

| Extra | Command |
| --- | --- |
| Cortex-M4F / M7F target | `rustup target add thumbv7em-none-eabihf` |
| RP2040, Cortex-M0+ | `rustup target add thumbv6m-none-eabi` |
| RP2350 Arm, Cortex-M33 | `rustup target add thumbv8m.main-none-eabihf` |
| RP2350 RISC-V, Hazard3 | `rustup target add riscv32imac-unknown-none-elf` |
| LLVM tools component | `rustup component add llvm-tools-preview` |
| cargo-binutils | `cargo install cargo-binutils --locked` |
| probe-rs tools | `cargo install probe-rs-tools --locked` |
| Raspberry Pi Pico SDK | `git clone` at the latest release tag |
| picotool | built from source at its release tag |
| GoogleTest, with gMock | built from source at its release tag |

Every extra starts selected. Untick the ones a team does not need; the
source builds, probe-rs above all, are what make a full build slow.

probe-rs compiles from source, so it automatically adds the udev and libusb
development headers it links against, under the right name for the chosen
distribution.

cargo-binutils is a thin wrapper over the LLVM tools component and is useless
without it, so selecting it locks that component on and says why.

**Selecting a rustup or cargo extra adds a rustup bootstrap**, because a
distribution's `rustc` package does not provide rustup and `rustup target add`
fails against it. For the same reason the distribution's Rust packages are then
dropped from the image: two toolchains on `PATH` is a trap. With no such extra
selected, the distribution's Rust is kept and no rustup appears.

The Pico SDK and picotool are a different shape and deliberately do not follow
that rule. They are built from source, need no Rust at all, and selecting them
on their own leaves the image Rust-free. `RUST_KINDS` in `extras.py` is what
separates the two families.

**picotool is installed with `cmake --install`, never by copying the binary onto
`PATH`.** SDK 2.x locates picotool through the CMake package config that only
the install target writes, so a binary on `PATH` leaves the SDK unable to find
it. It also needs `PICO_SDK_PATH`, so selecting it pulls the SDK in and orders
the two correctly, and it links against libusb; without those headers it still
builds but silently loses the commands that talk to a board.

**The RP2040 and RP2350 need different targets, and the RP2350 needs two.** The
RP2040 is a Cortex-M0+, so the Cortex-M4F target already in the list will not
run on it. The RP2350 carries both Arm Cortex-M33 cores and Hazard3 RISC-V
cores, selected at boot, and firmware for each is a separate build. The triples
are the ones rp-hal documents rather than ones inferred from the chip names. The
RISC-V entry can be switched off by a team that only ships Arm.

**The SDK tag is resolved from GitHub when the file is generated, then written
in literally.** That way the definition tracks upstream when you regenerate it
and stays reproducible once written, rather than silently drifting on a branch.
The lookup is cached for a day and falls back to a pinned tag, so an offline
machine still produces a file that builds. The clone pulls the five submodules
the SDK needs, and on the apt distributions the Arm C++ runtime is added, since
it is packaged separately there.

The generated file ends with a smoke test that runs the compilers it just
installed, so a broken toolchain fails the build rather than the first person to
use the image. `--locked` on the cargo installs keeps two builds of the same
definition identical, and the Rust toolchain is an `ARG` so it can be pinned to
a version rather than tracking `stable`.

**The rustup installer is downloaded to a file, never piped into a shell.** A
pipe hides a failed download behind the shell's exit status. The usual guard,
`SHELL ["/bin/sh", "-o", "pipefail", "-c"]`, does not help: podman silently
ignores `SHELL` under the OCI image format it builds by default, and Alpine's
busybox shell has no `pipefail` at all, so the protection is missing exactly
where it is needed.

**Arch installs with `pacman -Syu` as a single command.** Refreshing the
database with `-Sy` and then installing separately leaves a partial upgrade,
which on a rolling distribution breaks dynamic linking in ways that only surface
later.

**Arch initialises pacman's keyring first.** The base image ships no local
signing key, so when `-Syu` upgrades `archlinux-keyring`, its install hook fails
with "There is no secret key available to sign with". The build still succeeds,
which makes it easy to miss, but the keyring is left half-updated. Running
`pacman-key --init` and `pacman-key --populate archlinux` beforehand removes the
error; a before-and-after in throwaway containers reproduced it and then showed it
gone.

**Every extra together** was built on Arch as the default selection. It took 411
seconds, most of it compiling probe-rs, and the smoke test ran each tool:
arm-none-eabi-gcc 16.2.0, rustc 1.98.1 with all four targets, cargo-size 0.4.0,
probe-rs 0.32.0, the Pico SDK, picotool 2.3.1 and GoogleTest.

### lcov with GoogleTest

**lcov 2 cannot capture GoogleTest code built with GCC 14 or later, so images
with lcov route its gcov through a wrapper.** Without it the capture stops:

```
lcov: ERROR: (inconsistent) mismatched end line for _ZN39Crc16_MatchesTheStandardCheckValue_Test8TestBodyEv at smoke_test.cpp:20: 20 -> 24
```

`TEST()` expands to a class and its `TestBody` on the line it is written on.
gcov reports the class's implicit constructor and destructors on that line,
ending there, and `TestBody` starting there but ending at its closing brace:

| Function, test on lines 20 to 24 | Start | End |
| --- | --- | --- |
| constructor | 20 | 20 |
| destructor, both variants | 20 | 20 |
| `TestBody()` | 20 | 24 |

Every record is correct. lcov keys functions by start line, so it treats the four
as aliases of one function and rejects the differing end lines. lcov master still
does this as of September 2026; upstream considers it a gcov problem
([#296](https://github.com/linux-test-project/lcov/issues/296),
[#310](https://github.com/linux-test-project/lcov/issues/310)). Its workarounds
each cost something:

| Workaround | Cost |
| --- | --- |
| `--ignore-errors inconsistent` | downgrades all 49 checks in that category, and still prints 24 warnings for the 24-file sample |
| `--erase-functions` | the pattern has to match `TestBody` too, and erasing deletes its coverage |
| `--rc function_coverage=0` | does not help; the capture still fails |

`gcov-for-lcov` runs gcov, then gives a group of functions sharing a start line
the group's highest end line. It does so only when every shorter function ends on
the line it starts, which is the macro shape. That is the value lcov itself keeps
when the error is ignored: for both sample projects the tracefile came out
byte-identical to one captured with `--ignore-errors inconsistent`, without the
warnings. Any other mismatch reaches lcov untouched and still fails, and anything
the wrapper cannot read is left as gcov wrote it.

Wiring it in turned up two things about lcov itself:

- **lcov 2.1 and later never read `/etc/lcovrc` on their own.** They look for
  `~/.lcovrc`, then `$LCOV_HOME/etc/lcovrc` only when `LCOV_HOME` is set; lcov
  2.0 reads `/etc/lcovrc` directly. The image appends `geninfo_gcov_tool` to
  `/etc/lcovrc` and sets `LCOV_HOME=/`, so every version reads it. Every
  uncommented key in the file a distribution ships is an lcov default, and a
  capture and HTML report came out the same with it active.
- **lcov 2.0 appends a command-line `--gcov-tool` to the configured one instead
  of replacing it.** It ran `gcov-for-lcov gcov check.gcda`, and gcov took `gcov`
  for a data file. The wrapper therefore runs a program passed as its first
  argument as the gcov. On lcov 2.1 and later `--gcov-tool` replaces the wrapper
  altogether; to use another gcov and keep the fix, set `DEVENV_FORGE_GCOV`
  instead, for example `DEVENV_FORGE_GCOV=arm-none-eabi-gcov`.

A `~/.lcovrc` or `--config-file` replaces the system file, so it needs the
`geninfo_gcov_tool = /usr/local/bin/gcov-for-lcov` line as well.

The smoke test captures a plain C program of the same shape, a macro line
holding a one-line function and the start of a longer one, with plain lcov. An
image where the fix is not working therefore fails its build. The check needs
the build essentials as well as gcc, because on Debian gcc alone cannot link.

The wrapper adds about 32 ms per `.gcda` file. On the 24-file GoogleTest sample,
capture went from 1.8 to 2.6 seconds, still well under the 10.3 seconds it took
before JSON::XS.

One warning remains on GoogleTest code, once per capture: `unexecuted block on
non-branch line with non-zero hit count`, pointing into libstdc++ headers. It is
left alone on purpose. lcov's suggested `geninfo_unexecuted_blocks=1` sets such
lines to zero, and on the 24-file sample it turned 19 lines that ran into lines
that never did, among them the body of `std::move`, hit 1,944 times.

Each generated image was built with podman, and in each the wrapper matched the
generated script byte for byte. "Bypassed" means `--config-file /dev/null`,
which shows the build-time check really depends on the wrapper:

| Image | lcov | GCC | Image defaults | Wrapper bypassed | `--gcov-tool gcov` |
| --- | --- | --- | --- | --- | --- |
| Arch | 2.5 | 16.2.1 | passes | mismatch error | replaces the wrapper: error |
| Debian 13 | 2.3.1 | 14.2.0 | passes | mismatch error | replaces the wrapper: error |
| Fedora 44 | 2.0 | 16.2.1 | passes | mismatch error | passes, through the wrapper |

Ubuntu at 2.4 and Alpine at 2.3.1 read their configuration the way Debian does,
and RHEL's EPEL build is the same lcov 2.0 as Fedora. In an Arch image with the
GoogleTest extra, plain `lcov --capture` succeeded on both GoogleTest samples,
three tests and 72, with no errors, and `genhtml` rendered the report.

### Verified

The Arch definition was built end to end with podman on WSL2, and the built
image was inspected:

| Result | |
| --- | --- |
| Build time | about 4 minutes |
| Image size | 6.4 GB |
| arm-none-eabi-gcc | 16.2.0 |
| rustc | 1.98.1 |
| Installed targets | `thumbv7em-none-eabihf`, `x86_64-unknown-linux-gnu` |
| cargo-binutils | 0.4.0, `cargo size` and `cargo nm` working |

The image is large mostly because `base-devel`, QEMU and both toolchains are all
present. Dropping QEMU or moving the Rust tooling into a second stage would cut
it substantially.

Adding the Pico SDK on top reused every earlier layer and took 34 seconds. The
result was checked by compiling real firmware inside the image rather than just
looking for files:

| Pico SDK check | Result |
| --- | --- |
| Checked-out tag | 2.3.1 |
| Submodules populated | all five |
| CMake configure, `PICO_BOARD=pico` | succeeded |
| Compiled `smoke.elf` | ELF32, ARM, 10.2 KB text |

Adding picotool took 211 seconds and was checked the same way, by building
firmware that actually exercises it:

| picotool check | Result |
| --- | --- |
| Version | 2.3.1, with USB support compiled in |
| `pico_add_extra_outputs` | produced `blink.uf2`, 13 KB |
| `picotool info` on that UF2 | family `rp2040`, program `blink` |

The UF2 step is the one that matters: it only works if the SDK found the
installed picotool through its CMake package config, which is what the install
target exists to provide.

GoogleTest is built for the host rather than the Arm target, so it declares the
host C++ compiler, CMake and make itself instead of relying on the build
essentials meta-package being present. It is installed with `cmake --install`
so any project in the image finds it through `find_package(GTest)`. From 1.17 it
requires C++17. Adding it on Arch reused every earlier layer and took 19
seconds, and it was checked by building a test project against the installed
copy:

| GoogleTest check | Result |
| --- | --- |
| `find_package(GTest REQUIRED CONFIG)` | found 1.18.0 in `/usr/local/lib/cmake/GTest` |
| gtest assertion, CRC-16 check value | passed |
| gMock, ordered calls on a mocked GPIO | passed |
| `gtest_discover_tests` with `ctest` | 3 of 3 passed |

The Rust targets were checked by compiling `no_std` code for each and reading
the machine type out of the resulting objects, rather than trusting
`rustup target list`:

| Target | Compiles | Object machine |
| --- | --- | --- |
| `thumbv6m-none-eabi` | yes | ARM |
| `thumbv8m.main-none-eabihf` | yes | ARM |
| `riscv32imac-unknown-none-elf` | yes | RISC-V, compressed |
| `thumbv7em-none-eabihf` | yes | ARM |

Two things the real build taught, neither visible from inspection:

- podman warned that it was ignoring the `SHELL` instruction, which is what
  prompted replacing the piped installer.
- A `podman build` on Windows runs inside the WSL virtual machine. Killing the
  Windows client does not stop it; it orphans it. Check with
  `podman machine ssh "ps -eo pid,etimes,args"` before concluding a build has
  hung, because the client sits at near-zero CPU throughout either way.

## Deploy container

Pick a built image, choose which directories are shared with it, and start it.
The command is shown before it runs, and the containers list offers stop,
remove, and a shell.

**Host paths are passed through exactly as picked.** Podman on Windows
translates a native Windows path into the machine's view of it, so rewriting it
here would break it. Drive letters, spaces and the `:ro` suffix all survive,
because each `-v` value is a single argument and never passes through a shell.
Shares are read-write by default and write back to the host; the read-only box
adds `:ro`, which the kernel enforces.

**Detached containers get a terminal.** A dev image whose command is a shell
exits immediately under plain `-d`, because the shell has nothing attached and
returns at once. Keeping it running uses `-dit`, which is why the container
survives long enough to open a shell into it. That shell is handed to the system
terminal, since a window is not a console.

**Podman is located, not assumed to be on PATH.** A process keeps the PATH it
inherited when it started, so an application launched from a terminal that was
open before podman was installed cannot see it, however correct the install is.
The lookup tries PATH, then merges the registry's copy of PATH into this
process's, then the directories the installer uses. When podman still cannot be used, the page says
which reason applies rather than showing an empty list: not installed, machine
stopped, or an error with podman's own message. A stopped machine is the
easiest to confuse, because every image lives inside it and a stopped machine
looks exactly like having no images. That case offers a Start machine button.

**A machine can be running and still unreachable.** Podman keeps its
connections in `%APPDATA%\containers` but keeps the machine under the user
profile, so the two can get separated. When they do, every command fails:

```
podman system connection list    # empty
podman machine init              # Error: VM already exists
podman machine start             # Error: already running
```

Podman's own error message makes it worse by recommending `machine init` and
`machine start`, both of which fail here.

The cause actually seen in practice is running `podman machine init` from a
terminal inside a packaged (MSIX) Windows app, such as an IDE or an AI assistant.
Windows redirects that app's writes under `AppData\Roaming` into its private
storage under `%LOCALAPPDATA%\Packages\<app>\LocalCache\Roaming`. The machine
lands in the real profile, but the connections file lands in the app's private
copy, visible only from inside that app. Everything else, including the
application itself when launched normally, sees a machine with no connection.
Checking `APPDATA` in such a terminal shows nothing wrong, which makes it hard
to spot.

The application detects this: when the machine is running but unreachable, it
looks for a connections file stranded in any package's private storage, names
the app, and gives the command to copy it back. The copy must be run from an
ordinary terminal, because from inside that app it would be redirected straight
back into the private copy:

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\containers" | Out-Null
Copy-Item "$env:LOCALAPPDATA\Packages\<app>\LocalCache\Roaming\containers\podman-connections.json" "$env:APPDATA\containers\podman-connections.json"
```

The application also restores `APPDATA` and `LOCALAPPDATA` at startup from the
Windows known-folder API if a launching shell left them unset, which produces the
same symptoms for a different reason.

Three checks run before the button enables:

- A share cannot target `/`, `/usr`, `/etc` or similar. Mounting a host
  directory there hides the toolchain the image just installed, and the failure
  appears later as a missing compiler.
- Two shares cannot target the same container path, where the second would
  silently shadow the first.
- A container path cannot contain a colon, which `-v` would read as the start of
  its options.

**Containers start where the image says.** There is no working directory field.
The image's own `WORKDIR` decides, which for images built here is `/work`. The
page shows it under the shares, along with whether a share covers it. The first
share you add is proposed at that directory, so a deploy with one share opens
straight into your files. All images are read with a single `podman image
inspect`. An image that sets no working directory starts in `/`, and its first
share is proposed at `/work` instead, since sharing onto `/` would hide the
whole image.

The field was an early version of this page, and it was removed rather than
validated. Docker creates a missing working directory; podman handles one you
supply in ways that are easy to get wrong. Measured on podman 5.8 with a share
at `/work`:

| `-w` working directory | Result |
| --- | --- |
| `/work` | runs |
| `/work/new/dir` | runs, and creates `new/dir` inside the host folder |
| `/work/new/dir`, share read-only | the same: read-only does not prevent it |
| `/usr/local/lib`, present in the image | runs |
| `/projects`, outside every share and not in the image | exit 126, container left behind in Created state |
| not given | runs, in the image's working directory |

The last row is the only one that can never go wrong, because the image created
that directory when it was built.

**A failed start is cleaned up.** `podman run` creates the container before it
starts it, so a start that fails leaves a container behind in Created state,
holding the name, and the next attempt fails with "name already in use". After a
failed start the page removes that container, and only when it never started. The
dialog shows podman's own error instead of an exit code, and explains a name
clash.

### Verified

Deployed through the same code path the button uses:

| Check | Result |
| --- | --- |
| Container state after deploy | running |
| Read-write share | file written inside appeared on the host |
| Read-only share | write refused, "Read-only file system" |
| Toolchain inside | arm-none-eabi-gcc 16.2.0, all four Rust targets |
| Remove | container gone, host files intact |

## Design notes

**One platform interface.** `platforms/base.py` defines the contract; Windows,
macOS and Linux implement it. The UI only ever handles `CheckResult` objects, so
adding an OS means adding a subclass and nothing else.

**Checks are separate from fixes.** A check reports a status and, when it can,
attaches a `Remedy` describing how to repair it. Nothing is changed without the
user pressing a button and confirming.

**Nothing blocks the UI.** Every check and fix runs on a worker thread and
reports back through Qt signals. Installs take minutes and their output streams
into the activity log as it arrives.

### Windows specifics worth knowing

**`wsl.exe` writes UTF-16LE.** Decoding it as UTF-8 yields text with a NUL
between every character. `core/runner.py` sniffs the encoding instead of
assuming one.

**The user PATH is usually `REG_EXPAND_SZ`** and contains unexpanded entries
such as `%LOCALAPPDATA%\bin`. Writing it back expanded, which is what
`SetEnvironmentVariable` does, bakes in literal paths and corrupts those entries.
PATH is read and written through the registry with its value type preserved,
after a timestamped backup, and a write that would shorten PATH is refused.

**Qt parses `#AARRGGBB`, not CSS `#RRGGBBAA`.** Appending an alpha pair to a hex
colour silently produces a different hue. Translucency goes through the `rgba()`
helper in `ui/theme.py`.

**A process keeps the PATH it inherited at launch.** After an installer runs,
the new entry is invisible to every process that was already running, including
this one. Deciding whether PATH needs repairing therefore reads the registry
rather than `os.environ`; otherwise the tool offers to add a user PATH entry
that duplicates the machine entry the installer just wrote. After its own
install, the app reloads PATH from the registry so the follow-up check succeeds
without a restart.

**That reload adds; it never replaces.** The registry's PATH is not a superset
of a running process's: a shell contributes entries of its own before launching
anything, and they were never stored anywhere. Overwriting `os.environ["PATH"]`
with the registry value therefore discards them for the rest of the session,
silently, until some unrelated tool can no longer be found. Both reloads merge
instead, through `merge_path` in `core/runner.py`: existing entries keep their
place and their precedence, and only entries that are genuinely absent are
appended. Two entries count as the same directory when they match after case,
quoting, a trailing separator and variable expansion are normalised away.

**The lookup is cached, so every fix that moves podman retracts it.** Resolving
podman touches PATH, then the registry, then several install directories, so the
answer is cached for the life of the process. Nothing about installing podman
tells that cache it has gone stale: a "not found" recorded before the fix would
be handed to every check that runs afterwards, and the tool would report a
podman it had installed itself as missing until it was restarted. Every remedy
that installs podman or edits PATH calls `Platform.drop_cached_podman()` before
returning, and only on success -- a declined UAC prompt changed nothing.

**`wsl.exe` ships with Windows even when the feature is off**, so its presence
proves nothing. It has to actually answer.

**Locale.** The STATE column of `wsl --list --verbose` is translated; NAME and
VERSION are not, so they are read positionally. The default WSL version comes
from the registry rather than from parsing English output.

## Layout

```
src/devenv_forge/
  core/        models, subprocess handling, paths, catalogue, package sources
  platforms/   base contract, windows, macos, linux, windows primitives
  ui/          theme, worker thread, cards, pages
tests/         pure logic plus a threaded check -> fix -> re-check test
```

Backups and installer logs are written to `%LOCALAPPDATA%\DevEnvForge` on
Windows, `~/Library/Application Support/DevEnvForge` on macOS, and
`~/.local/share/devenv-forge` on Linux.
