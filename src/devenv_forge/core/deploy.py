"""Deploy a built image as a running container with host directories shared.

Podman on Windows translates a native Windows path in ``-v`` into the machine's
view of it, so paths are passed through as the user typed them. Drive letters
and spaces both survive, because each ``-v`` value is one argv element and is
never handed to a shell.
"""

from __future__ import annotations

import os
import re
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
        container = self.container.strip()
        if not host:
            issues.append("host directory is empty")
        elif not os.path.isdir(host):
            issues.append(f"host directory does not exist: {host}")
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
        return issues

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
