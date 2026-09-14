"""Deploy a built image as a running container with host directories shared.

Podman on Windows translates a native Windows path in ``-v`` into the machine's
view of it, so paths are passed through as the user typed them. Drive letters
and spaces both survive, because each ``-v`` value is one argv element and is
never handed to a shell.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

from . import podman as podman_cli
from .runner import run

#: Container paths that would break the image if something were mounted over
#: them. Sharing a host directory onto /usr hides the toolchain that was just
#: installed there.
PROTECTED_TARGETS = frozenset(
    {"/", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/var", "/proc", "/sys", "/dev"}
)

#: Podman's own rule for container names.
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _binary(podman: str | None) -> str:
    """The podman to invoke: the caller's choice, else the resolved one.

    Resolving here rather than trusting PATH matters: an application started
    before podman was installed inherits a PATH without it.
    """
    return podman or podman_cli.executable() or "podman"


@dataclass(slots=True)
class Mount:
    host: str
    container: str
    read_only: bool = False

    def to_arg(self) -> str:
        """The value for ``-v``. Never quoted: it is passed as one argv item."""
        value = f"{self.host}:{self.container}"
        return f"{value}:ro" if self.read_only else value

    def problems(self) -> list[str]:
        issues: list[str] = []
        host = self.host.strip()
        if not host:
            issues.append("host directory is empty")
        elif not os.path.isdir(host):
            issues.append(f"host directory does not exist: {host}")
        issues.extend(container_path_problems(self.container))
        return issues


#: Where WSL exposes this machine's drives inside the virtual machine. It is
#: configurable in /etc/wsl.conf, but /mnt is the default and what podman's own
#: machine uses.
WSL_MOUNT_ROOT = "/mnt"

#: A Windows path with a drive letter, which the machine cannot see under that
#: name. C:\work becomes /mnt/c/work inside it.
_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")

#: A path already under the drive translation, however it was typed.
_TRANSLATED_RE = re.compile(rf"^{WSL_MOUNT_ROOT}/[a-z](/|$)")


def machine_path(location: str) -> str:
    """*location* as the podman machine sees it, or "" when there is none.

    A volume's device is resolved inside the machine rather than on this
    desktop, so a Windows path has to be translated: ``D:\\yocto`` is
    ``/mnt/d/yocto`` in there. A path that is already POSIX is one the machine
    can see under that name, and is passed through.
    """
    text = location.strip().strip('"')
    if not text:
        return ""
    match = _DRIVE_RE.match(text)
    if not match:
        return text.replace("\\", "/")
    drive, rest = match.group(1).lower(), match.group(2).replace("\\", "/")
    translated = f"{WSL_MOUNT_ROOT}/{drive}/{rest}".rstrip("/")
    return translated or f"{WSL_MOUNT_ROOT}/{drive}"


def container_path_problems(path: str) -> list[str]:
    """What is wrong with a mount target inside the container, if anything."""
    issues: list[str] = []
    container = path.strip()
    if not container:
        issues.append("container path is empty")
    elif not container.startswith("/"):
        issues.append(f"container path must be absolute: {container}")
    elif ":" in container:
        # -v splits on colons after the host path; the rest would be taken
        # as options such as ro.
        issues.append(f"container path cannot contain a colon: {container}")
    elif container.rstrip("/") in PROTECTED_TARGETS or container == "/":
        issues.append(
            f"refusing to mount over {container}, which would hide the "
            "image's own contents"
        )
    return issues


@dataclass(slots=True)
class Volume:
    """A named volume podman owns, and where on disk its data sits.

    Not a share under another name. A share hands the container a directory of
    this desktop's; a volume is storage podman manages, and on Windows that
    difference is the whole point. A share, and equally a volume pinned to a
    Windows folder, reaches the container over 9p: measured on this setup,
    creating 2000 small files took 11.3 s there against 28 ms inside the
    machine, the filesystem is case-insensitive, and a chmod is not kept. A
    volume left in podman's own storage is ext4 inside the machine, which is
    what a build tree needs and why this is not just another share.

    An empty *location* is that default storage. Anything else pins the volume
    to a directory: a folder on this desktop, or a path inside the machine.
    """

    name: str
    container: str
    #: Where the data sits. Empty means podman's own volume storage.
    location: str = ""

    @property
    def device(self) -> str:
        """The location as the machine sees it, or "" for podman's storage."""
        return machine_path(self.location)

    @property
    def translated(self) -> bool:
        """True when the data is reached through the drive translation."""
        device = self.device
        return bool(device) and bool(_TRANSLATED_RE.match(device))

    def to_arg(self) -> str:
        """The value for ``-v``: the volume's name, not a path."""
        return f"{self.name}:{self.container}"

    def create_argv(self, podman: str | None = None) -> list[str]:
        """The ``podman volume create`` that must run before the container."""
        argv = [_binary(podman), "volume", "create"]
        if self.device:
            # The local driver's bind options are how a volume is pinned to a
            # directory instead of podman's own storage. type=none with o=bind
            # binds an existing directory rather than mounting a filesystem.
            argv += [
                "--driver", "local",
                "--opt", "type=none",
                "--opt", "o=bind",
                "--opt", f"device={self.device}",
            ]
        argv.append(self.name)
        return argv

    def problems(self) -> list[str]:
        issues: list[str] = []
        name = self.name.strip()
        if not name:
            issues.append("volume name is empty")
        elif not NAME_RE.match(name):
            issues.append(
                f"volume name may only contain letters, digits, dot, dash and "
                f"underscore: {name}"
            )
        issues.extend(container_path_problems(self.container))

        location = self.location.strip()
        if location.startswith("\\\\") or location.startswith("//"):
            issues.append(
                f"a network path cannot back a volume: {location}. The machine "
                "sees local drives only."
            )
        elif location and not (_DRIVE_RE.match(location) or location.startswith("/")):
            issues.append(f"volume location must be an absolute path: {location}")
        elif location and _local_directory(location) is False:
            issues.append(f"volume location does not exist: {location}")
        return issues

    def warnings(self) -> list[str]:
        """Not wrong, but worth saying before someone waits on a slow build.

        The numbers are measured on this kind of setup, not estimated: the same
        script in both places, 2000 small files each.
        """
        if not self.translated:
            return []
        return [
            f"{self.name} sits on a Windows drive ({self.location}), which the "
            "container reaches over 9p rather than as a Linux filesystem. Fine "
            "for sources and finished artefacts, and wrong for a build tree: "
            "creating 2000 small files there took 11.3 s against 28 ms inside "
            "the machine, the filesystem is case-insensitive, and chmod does "
            "not stick. Clear the location to keep this volume in the machine."
        ]


def _local_directory(location: str) -> bool | None:
    """Whether *location* is a directory, or None when this OS cannot tell.

    A path inside the virtual machine is not visible from a Windows desktop, so
    it is left to podman to complain about rather than guessed at here.
    """
    if _DRIVE_RE.match(location):
        return os.path.isdir(location)
    if sys.platform != "win32" and location.startswith("/"):
        return os.path.isdir(location)
    return None


@dataclass(slots=True)
class ImageInfo:
    repository: str
    tag: str
    image_id: str
    size: str = ""
    created: str = ""

    @property
    def reference(self) -> str:
        if self.repository and self.tag and self.tag != "<none>":
            return f"{self.repository}:{self.tag}"
        return self.image_id


@dataclass(slots=True)
class ContainerInfo:
    name: str
    image: str
    status: str
    container_id: str = ""

    @property
    def running(self) -> bool:
        return self.status.lower().startswith("up")


@dataclass(slots=True)
class ContainerSpec:
    """What to run. There is deliberately no working directory.

    A container starts in its image's own working directory. Letting the user
    set one with ``-w`` was a trap: podman refuses a directory that does not
    exist, unlike Docker, and quietly creates one inside the host folder when it
    sits under a share, even a read-only share.
    """

    image: str
    name: str = ""
    mounts: list[Mount] = field(default_factory=list)
    volumes: list[Volume] = field(default_factory=list)
    #: Detached keeps the container alive in the background to exec into.
    detached: bool = True
    remove_on_exit: bool = False

    def problems(self) -> list[str]:
        issues: list[str] = []
        if not self.image.strip():
            issues.append("no image selected")
        if self.name and not NAME_RE.match(self.name):
            issues.append(
                "container name may only contain letters, digits, dot, dash "
                "and underscore"
            )
        seen: set[str] = set()
        for mount in self.mounts:
            issues.extend(mount.problems())
            target = mount.container.strip().rstrip("/")
            if target and target in seen:
                issues.append(f"two shares point at the same container path: {target}")
            seen.add(target)

        names: set[str] = set()
        for volume in self.volumes:
            issues.extend(volume.problems())
            # One target, one source. A share and a volume on the same path is
            # the same collision as two shares, and podman would silently let
            # the later -v win.
            target = volume.container.strip().rstrip("/")
            if target and target in seen:
                issues.append(f"two mounts point at the same container path: {target}")
            seen.add(target)
            name = volume.name.strip()
            if name and name in names:
                issues.append(f"two volumes have the same name: {name}")
            names.add(name)
        return issues

    def warnings(self) -> list[str]:
        """Things worth saying that are not reasons to refuse to start."""
        notes: list[str] = []
        for volume in self.volumes:
            notes.extend(volume.warnings())
        return notes

    def setup_argv(self, podman: str | None = None) -> list[list[str]]:
        """What has to run before the container: creating its volumes.

        Podman creates a named volume on first use, but only in its own storage.
        A volume pinned to a directory has to exist before the container asks
        for it, or it is silently created in the wrong place.
        """
        return [v.create_argv(podman) for v in self.volumes if v.name.strip()]

    def setup_preview(self, podman: str | None = None) -> str:
        """The volume commands, for reading alongside :meth:`preview`."""
        # argv[0] is the resolved podman path; the bare name reads better and
        # this text is never executed.
        return "\n".join(
            " ".join(["podman", *argv[1:]]) for argv in self.setup_argv(podman)
        )

    def argv(self, podman: str | None = None) -> list[str]:
        argv = [_binary(podman), "run"]
        if self.detached:
            # -d alone exits at once: the image's shell has no terminal and
            # returns immediately. -dit gives it one so the container stays up.
            argv.append("-dit")
        else:
            argv.append("-it")
        if self.remove_on_exit:
            argv.append("--rm")
        if self.name:
            argv += ["--name", self.name]
        for mount in self.mounts:
            argv += ["-v", mount.to_arg()]
        for volume in self.volumes:
            argv += ["-v", volume.to_arg()]
        argv.append(self.image)
        return argv

    def preview(self, podman: str | None = None) -> str:
        """The command, wrapped for reading. Not for execution."""
        argv = self.argv(podman)
        # Show the bare name; the full path is an implementation detail.
        lines = ["podman " + argv[1]]
        index = 2
        while index < len(argv) - 1:
            token = argv[index]
            if token in ("-v", "--name") and index + 1 < len(argv):
                lines.append(f"    {token} {argv[index + 1]}")
                index += 2
            else:
                lines.append(f"    {token}")
                index += 1
        lines.append(f"    {argv[-1]}")
        return " \\\n".join(lines)


# ---------------------------------------------------------------------------
# querying podman
# ---------------------------------------------------------------------------

_SEP = "\x1f"


def list_images(podman: str | None = None) -> list[ImageInfo]:
    """Tagged images, newest first. Intermediate layers are excluded."""
    template = _SEP.join(
        ["{{.Repository}}", "{{.Tag}}", "{{.ID}}", "{{.Size}}", "{{.CreatedSince}}"]
    )
    result = run([_binary(podman), "images", "--format", template], timeout=60)
    if not result.ok:
        return []
    images: list[ImageInfo] = []
    for line in result.lines():
        parts = line.split(_SEP)
        if len(parts) < 3:
            continue
        repository, tag = parts[0], parts[1]
        if tag == "<none>" or repository == "<none>":
            continue
        images.append(
            ImageInfo(
                repository=repository,
                tag=tag,
                image_id=parts[2],
                size=parts[3] if len(parts) > 3 else "",
                created=parts[4] if len(parts) > 4 else "",
            )
        )
    return images


def list_containers(podman: str | None = None, *, only_running: bool = False) -> list[ContainerInfo]:
    argv = [_binary(podman), "ps", "--format",
            _SEP.join(["{{.Names}}", "{{.Image}}", "{{.Status}}", "{{.ID}}"])]
    if not only_running:
        argv.insert(2, "-a")
    result = run(argv, timeout=60)
    if not result.ok:
        return []
    containers: list[ContainerInfo] = []
    for line in result.lines():
        parts = line.split(_SEP)
        if len(parts) < 3:
            continue
        containers.append(
            ContainerInfo(
                name=parts[0],
                image=parts[1],
                status=parts[2],
                container_id=parts[3] if len(parts) > 3 else "",
            )
        )
    return containers


def name_exists(name: str, podman: str | None = None) -> bool:
    return any(c.name == name for c in list_containers(podman))


def stop_command(name: str, podman: str | None = None) -> list[str]:
    return [_binary(podman), "stop", name]


def remove_command(name: str, podman: str | None = None) -> list[str]:
    return [_binary(podman), "rm", "-f", name]


def shell_command(name: str, shell: str = "bash", podman: str | None = None) -> list[str]:
    """Interactive shell inside a running container, for a real terminal."""
    return [_binary(podman), "exec", "-it", name, shell]


def suggest_volume(base: str, taken=()) -> tuple[str, str]:
    """A volume name and where to mount it, unique among *taken* names.

    Named after the container rather than after what it holds, so a machine
    running several projects does not collect a pile of volumes called "data".
    """
    stem = suggest_name(base) or "devenv"
    existing = set(taken)
    suffix, index = "data", 2
    while f"{stem}-{suffix}" in existing:
        suffix, index = f"data{index}", index + 1
    return f"{stem}-{suffix}", f"/work/{suffix}"


def suggest_name(image: str) -> str:
    """A container name derived from the image reference."""
    base = image.split("/")[-1].replace(":", "-")
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "-", base).strip("-._")
    return cleaned or "devenv"


# ---------------------------------------------------------------------------
# starting directory
# ---------------------------------------------------------------------------
# A container starts in its image's working directory, which images built by
# this tool set to /work. It is shown, never edited: podman 5.8 refuses a
# user-supplied working directory that does not exist, and silently creates one
# inside the host folder when it sits under a share, read-only or not.


def _posix_parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.strip().split("/") if part)


def _host_leaf(path: str) -> str:
    """The last folder name, made safe for a container path.

    A drive root such as ``C:\\`` would otherwise leave a colon, which ``-v``
    reads as the start of its options.
    """
    leaf = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", leaf).strip("-.")


def image_working_dirs(images: list[ImageInfo], podman: str | None = None) -> dict[str, str]:
    """Each image's working directory, keyed by reference, in one podman call.

    An image that sets none starts in "/". If inspection fails for some images,
    the ones podman did report are still returned.
    """
    if not images:
        return {}
    result = run(
        [_binary(podman), "image", "inspect", "--format", "{{.Id}}" + _SEP + "{{.Config.WorkingDir}}"]
        + [image.reference for image in images],
        timeout=60,
    )
    by_full_id: dict[str, str] = {}
    for line in result.stdout.splitlines():
        # Not line.strip(): Python counts the \x1f separator as whitespace, so
        # an image with no working directory would lose it and be skipped.
        full_id, sep, working_dir = line.partition(_SEP)
        if sep:
            by_full_id[full_id.strip().removeprefix("sha256:")] = working_dir.strip(" \t\r") or "/"
    found: dict[str, str] = {}
    for image in images:
        short = image.image_id.removeprefix("sha256:")
        for full_id, working_dir in by_full_id.items():
            if short and full_id.startswith(short):
                found[image.reference] = working_dir
                break
    return found


def share_covering(directory: str, mounts: list[Mount]) -> Mount | None:
    """The share that ``directory`` sits in, or None."""
    target = _posix_parts(directory)
    best: Mount | None = None
    for mount in mounts:
        parts = _posix_parts(mount.container)
        if parts and target[: len(parts)] == parts:
            # The deepest matching share is the one actually mounted there.
            if best is None or len(parts) > len(_posix_parts(best.container)):
                best = mount
    return best


def start_note(working_dir: str | None, mounts: list[Mount]) -> str:
    """One line saying where the container will start, and whether files are there."""
    if not working_dir:
        return "Starts in the image's own working directory."
    if working_dir == "/":
        return "Starts in /, because the image sets no working directory."
    share = share_covering(working_dir, mounts)
    if share is not None:
        where = "shared from" if _posix_parts(share.container) == _posix_parts(working_dir) else "inside the share from"
        return f"Starts in {working_dir}, the image's working directory, {where} {share.host}."
    return (
        f"Starts in {working_dir}, the image's working directory. None of the shares "
        f"cover it; share a folder at {working_dir} to start among your files."
    )


def suggest_share_target(working_dir: str | None, mounts: list[Mount], host: str) -> str:
    """Container path to propose for a newly added share.

    The first share goes where the container starts, so a deploy with one share
    opens straight into the user's files.
    """
    taken = {m.container.strip().rstrip("/") for m in mounts}
    if (
        working_dir
        and working_dir.startswith("/")
        and working_dir.rstrip("/") not in PROTECTED_TARGETS
        and working_dir != "/"
        and share_covering(working_dir, mounts) is None
        and working_dir.rstrip("/") not in taken
    ):
        return working_dir
    if not mounts:
        return "/work"
    candidate = f"/mnt/{_host_leaf(host) or 'share'}"
    return candidate if candidate not in taken else f"{candidate}-{len(mounts) + 1}"


def container_state(name: str, podman: str | None = None) -> str:
    """podman's status line for the container called ``name``, or ""."""
    for container in list_containers(podman):
        if container.name == name:
            return container.status
    return ""


def is_failed_start(status: str) -> bool:
    """Created but never started: what a podman run that fails to start leaves."""
    return status.lower().startswith("created")


def explain_run_failure(output: str, spec: ContainerSpec) -> str:
    """Turn podman's output into a message a person can act on."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if line.lower().startswith("error")]
    raw = errors[-1] if errors else (lines[-1] if lines else "podman failed without output")

    if "name" in raw and "already in use" in raw:
        return (
            f"A container called {spec.name} already exists. Remove it from the "
            f"Containers list or pick another name.\n\npodman said: {raw}"
        )
    return raw
