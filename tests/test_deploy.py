"""Tests for deploying an image as a container with shared directories."""

from __future__ import annotations

import pytest

from devenv_forge.core.deploy import (
    NAME_RE,
    PROTECTED_TARGETS,
    ContainerSpec,
    ImageInfo,
    Mount,
    list_containers,
    list_images,
    remove_command,
    shell_command,
    stop_command,
    suggest_name,
)


@pytest.fixture
def host_dir(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    return str(directory)


class TestMountArgument:
    def test_plain_mount(self, host_dir: str) -> None:
        assert Mount(host_dir, "/work").to_arg() == f"{host_dir}:/work"

    def test_read_only_suffix(self, host_dir: str) -> None:
        assert Mount(host_dir, "/work", read_only=True).to_arg().endswith(":/work:ro")

    def test_windows_drive_letter_is_left_alone(self) -> None:
        """podman translates the host path itself; mangling it here would break it."""
        mount = Mount(r"C:\Users\me\project", "/work")
        assert mount.to_arg() == r"C:\Users\me\project:/work"

    def test_spaces_are_not_quoted(self) -> None:
        """The value is one argv element, so quoting would become part of the path."""
        mount = Mount(r"C:\my project", "/work")
        assert '"' not in mount.to_arg()
        assert "my project" in mount.to_arg()


class TestMountValidation:
    def test_accepts_a_real_directory(self, host_dir: str) -> None:
        assert Mount(host_dir, "/work").problems() == []

    def test_rejects_missing_host_directory(self) -> None:
        problems = Mount("/no/such/place/xyzzy", "/work").problems()
        assert any("does not exist" in p for p in problems)

    def test_rejects_relative_container_path(self, host_dir: str) -> None:
        problems = Mount(host_dir, "work").problems()
        assert any("absolute" in p for p in problems)

    @pytest.mark.parametrize("target", sorted(PROTECTED_TARGETS))
    def test_refuses_to_shadow_system_paths(self, host_dir: str, target: str) -> None:
        """Mounting over /usr would hide the toolchain the image just installed."""
        problems = Mount(host_dir, target).problems()
        assert any("refusing" in p for p in problems)

    def test_trailing_slash_does_not_evade_the_check(self, host_dir: str) -> None:
        assert any("refusing" in p for p in Mount(host_dir, "/usr/").problems())

    def test_rejects_a_colon_in_the_container_path(self, host_dir: str) -> None:
        """-v would read everything after it as options."""
        problems = Mount(host_dir, "/mnt/C:").problems()
        assert any("colon" in p for p in problems)

    def test_a_drive_letter_host_path_is_not_a_colon_problem(self) -> None:
        problems = Mount(r"C:\definitely\missing\xyzzy", "/work").problems()
        assert not any("colon" in p for p in problems)

    def test_empty_fields(self) -> None:
        problems = Mount("", "").problems()
        assert len(problems) == 2


class TestContainerSpec:
    def test_detached_uses_a_terminal(self, host_dir: str) -> None:
        """Detached without a terminal exits immediately; -dit keeps it alive."""
        argv = ContainerSpec(image="img", detached=True).argv()
        assert "-dit" in argv
        assert "-d" not in argv

    def test_foreground_is_interactive(self) -> None:
        assert "-it" in ContainerSpec(image="img", detached=False).argv()

    def test_mounts_become_v_flags(self, host_dir: str) -> None:
        spec = ContainerSpec(
            image="img",
            mounts=[Mount(host_dir, "/work"), Mount(host_dir, "/data", read_only=True)],
        )
        argv = spec.argv()
        assert argv.count("-v") == 2
        assert f"{host_dir}:/data:ro" in argv

    def test_image_is_last(self, host_dir: str) -> None:
        spec = ContainerSpec(image="devenv:latest", mounts=[Mount(host_dir, "/work")])
        assert spec.argv()[-1] == "devenv:latest"

    def test_name(self) -> None:
        argv = ContainerSpec(image="img", name="dev").argv()
        assert argv[argv.index("--name") + 1] == "dev"

    def test_never_sets_a_working_directory(self, host_dir: str) -> None:
        """The image decides where the container starts; -w could point at nothing."""
        spec = ContainerSpec(image="img", name="dev", mounts=[Mount(host_dir, "/work")])
        for argv in (spec.argv(), ContainerSpec(image="img", detached=False).argv()):
            assert "-w" not in argv and "--workdir" not in argv
        assert "-w" not in spec.preview()

    def test_remove_on_exit(self) -> None:
        assert "--rm" in ContainerSpec(image="img", remove_on_exit=True).argv()
        assert "--rm" not in ContainerSpec(image="img").argv()


class TestSpecValidation:
    def test_requires_an_image(self) -> None:
        assert any("no image" in p for p in ContainerSpec(image="").problems())

    def test_rejects_a_name_with_spaces(self) -> None:
        problems = ContainerSpec(image="img", name="my container").problems()
        assert any("name" in p for p in problems)

    def test_accepts_conventional_names(self) -> None:
        for name in ("devenv", "devenv-arch-rp", "dev_env.1"):
            assert ContainerSpec(image="img", name=name).problems() == []

    def test_rejects_two_shares_on_one_target(self, host_dir: str) -> None:
        """The second would silently shadow the first."""
        spec = ContainerSpec(
            image="img", mounts=[Mount(host_dir, "/work"), Mount(host_dir, "/work")]
        )
        assert any("same container path" in p for p in spec.problems())

    def test_clean_spec_has_no_problems(self, host_dir: str) -> None:
        spec = ContainerSpec(
            image="devenv:latest",
            name="devenv",
            mounts=[Mount(host_dir, "/work")],
        )
        assert spec.problems() == []

    def test_working_directory_is_not_accepted(self) -> None:
        with pytest.raises(TypeError):
            ContainerSpec(image="img", workdir="/work")  # type: ignore[call-arg]


class TestPreview:
    def test_preview_lists_each_share(self, host_dir: str) -> None:
        spec = ContainerSpec(
            image="img", mounts=[Mount(host_dir, "/work"), Mount(host_dir, "/data")]
        )
        text = spec.preview()
        assert text.count("-v ") == 2
        assert text.startswith("podman run")

    def test_preview_is_not_for_execution(self, host_dir: str) -> None:
        """It carries continuations for reading; argv is what actually runs."""
        assert "\\\n" in ContainerSpec(
            image="img", mounts=[Mount(host_dir, "/work")]
        ).preview()


class TestHelpers:
    @pytest.mark.parametrize(
        ("image", "expected"),
        [
            ("devenv:latest", "devenv-latest"),
            ("localhost/devenv-arch:rp", "devenv-arch-rp"),
            ("registry.example.com/team/img:2.1", "img-2.1"),
        ],
    )
    def test_suggest_name(self, image: str, expected: str) -> None:
        assert suggest_name(image) == expected

    def test_suggested_names_are_valid(self) -> None:
        for image in ("devenv:latest", "localhost/a_b-c:1.0", "weird///:::"):
            assert NAME_RE.match(suggest_name(image))

    def test_command_builders(self) -> None:
        """argv[0] is the resolved podman path, so assert on the shape."""
        assert stop_command("dev")[1:] == ["stop", "dev"]
        assert remove_command("dev")[1:] == ["rm", "-f", "dev"]
        assert shell_command("dev")[1:] == ["exec", "-it", "dev", "bash"]
        for argv in (stop_command("dev"), remove_command("dev"), shell_command("dev")):
            assert "podman" in argv[0].lower()

    def test_explicit_binary_is_honoured(self) -> None:
        assert stop_command("dev", podman="/opt/podman")[0] == "/opt/podman"

    def test_image_reference(self) -> None:
        assert ImageInfo("localhost/x", "v1", "abc").reference == "localhost/x:v1"
        assert ImageInfo("<none>", "<none>", "abc123").reference == "abc123"


class TestQueriesDegradeGracefully:
    """Podman may be absent; the page must still open."""

    def test_list_images_without_podman(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "")
        assert list_images(podman="definitely-not-podman-xyzzy") == []

    def test_list_containers_without_podman(self) -> None:
        assert list_containers(podman="definitely-not-podman-xyzzy") == []


from devenv_forge.core import deploy  # noqa: E402
from devenv_forge.core.deploy import (  # noqa: E402
    explain_run_failure,
    image_working_dirs,
    is_failed_start,
    share_covering,
    start_note,
    suggest_share_target,
)
from devenv_forge.core.runner import CommandResult  # noqa: E402

FULL_ID_A = "sha256:" + "a1" * 32
FULL_ID_B = "sha256:" + "b2" * 32


def _images() -> list[ImageInfo]:
    return [
        ImageInfo("localhost/devenv-arch", "defaults", FULL_ID_A[7:19]),
        ImageInfo("docker.io/library/archlinux", "latest", FULL_ID_B[7:19]),
    ]


class TestImageWorkingDirs:
    """Where each image starts, read in one podman call."""

    def _fake(self, monkeypatch, stdout: str, returncode: int = 0) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(list(argv))
            return CommandResult(list(argv), returncode, stdout, "")

        monkeypatch.setattr(deploy, "run", fake_run)
        return calls

    def test_one_call_for_all_images(self, monkeypatch) -> None:
        sep = "\x1f"
        calls = self._fake(monkeypatch, f"{FULL_ID_A}{sep}/work\n{FULL_ID_B}{sep}\n")
        found = image_working_dirs(_images(), podman="podman")

        assert len(calls) == 1
        assert calls[0][:3] == ["podman", "image", "inspect"]
        assert found == {
            "localhost/devenv-arch:defaults": "/work",
            # An image that sets nothing starts at the root.
            "docker.io/library/archlinux:latest": "/",
        }

    def test_matches_by_id_not_by_order(self, monkeypatch) -> None:
        sep = "\x1f"
        self._fake(monkeypatch, f"{FULL_ID_B}{sep}/\n{FULL_ID_A}{sep}/work\n")
        assert image_working_dirs(_images(), podman="podman")["localhost/devenv-arch:defaults"] == "/work"

    def test_images_podman_did_report_survive_a_partial_failure(self, monkeypatch) -> None:
        """Measured: a missing image makes the batch exit 125, the rest still print."""
        sep = "\x1f"
        self._fake(monkeypatch, f"{FULL_ID_A}{sep}/work\n", returncode=125)
        assert image_working_dirs(_images(), podman="podman") == {
            "localhost/devenv-arch:defaults": "/work"
        }

    def test_nothing_to_inspect(self, monkeypatch) -> None:
        calls = self._fake(monkeypatch, "")
        assert image_working_dirs([], podman="podman") == {}
        assert calls == []

    def test_without_podman(self) -> None:
        assert image_working_dirs(_images(), podman="definitely-not-podman-xyzzy") == {}


class TestShareCovering:
    def test_exact_target(self, host_dir: str) -> None:
        share = Mount(host_dir, "/work")
        assert share_covering("/work", [share]) is share

    def test_trailing_slashes(self, host_dir: str) -> None:
        share = Mount(host_dir, "/work/")
        assert share_covering("/work/", [share]) is share

    def test_below_a_share(self, host_dir: str) -> None:
        share = Mount(host_dir, "/work")
        assert share_covering("/work/src/app", [share]) is share

    def test_a_prefix_is_not_a_parent(self, host_dir: str) -> None:
        """/workspace is not inside /work."""
        assert share_covering("/workspace", [Mount(host_dir, "/work")]) is None

    def test_deepest_share_wins(self, host_dir: str) -> None:
        outer, inner = Mount(host_dir, "/work"), Mount(host_dir, "/work/src")
        assert share_covering("/work/src/app", [outer, inner]) is inner
        assert share_covering("/work/src/app", [inner, outer]) is inner

    def test_an_unfinished_row_covers_nothing(self, host_dir: str) -> None:
        assert share_covering("/work", [Mount(host_dir, "")]) is None


class TestStartNote:
    def test_shared_start(self) -> None:
        note = start_note("/work", [Mount(r"C:\projects", "/work")])
        assert note == r"Starts in /work, the image's working directory, shared from C:\projects."

    def test_start_below_a_share(self) -> None:
        note = start_note("/work/src", [Mount(r"C:\projects", "/work")])
        assert note.endswith(r"inside the share from C:\projects.")

    def test_unshared_start_says_how_to_fix_it(self) -> None:
        """The reported case, now as advice rather than a failure."""
        note = start_note("/work", [Mount(r"C:\projects", "/projects")])
        assert "None of the shares cover it" in note
        assert "share a folder at /work" in note

    def test_image_without_a_working_directory(self) -> None:
        assert "sets no working directory" in start_note("/", [])

    def test_unknown(self) -> None:
        assert start_note(None, []) == "Starts in the image's own working directory."


class TestSuggestShareTarget:
    def test_first_share_goes_where_the_container_starts(self) -> None:
        assert suggest_share_target("/home/dev/code", [], r"C:\projects") == "/home/dev/code"

    def test_images_built_here_start_in_work(self) -> None:
        assert suggest_share_target("/work", [], r"C:\projects") == "/work"

    def test_root_start_falls_back_to_work(self) -> None:
        """Sharing onto / would hide the whole image."""
        assert suggest_share_target("/", [], r"C:\projects") == "/work"

    def test_unknown_start_falls_back_to_work(self) -> None:
        assert suggest_share_target(None, [], r"C:\projects") == "/work"

    def test_protected_start_is_not_proposed(self) -> None:
        assert suggest_share_target("/usr", [], r"C:\projects") == "/work"

    def test_later_shares_go_under_mnt(self) -> None:
        taken = [Mount(r"C:\projects", "/work")]
        assert suggest_share_target("/work", taken, r"D:\data sets\Raw") == "/mnt/Raw"

    def test_start_not_yet_covered_is_offered_even_after_other_shares(self) -> None:
        taken = [Mount(r"C:\cache", "/mnt/cache")]
        assert suggest_share_target("/work", taken, r"C:\projects") == "/work"

    def test_no_clash_with_an_existing_target(self) -> None:
        taken = [Mount(r"C:\a", "/work"), Mount(r"C:\other\src", "/mnt/src")]
        assert suggest_share_target("/work", taken, r"D:\src") == "/mnt/src-3"

    def test_drive_root_gives_a_usable_path(self, host_dir: str) -> None:
        taken = [Mount(host_dir, "/work")]
        target = suggest_share_target("/work", taken, "C:\\")
        assert target == "/mnt/C"
        assert Mount(host_dir, target).problems() == []

    def test_awkward_folder_names_are_cleaned(self) -> None:
        taken = [Mount(r"C:\a", "/work")]
        assert suggest_share_target("/work", taken, r"C:\My Projects (old)") == "/mnt/My-Projects-old"

    def test_nothing_usable_in_the_name(self) -> None:
        taken = [Mount(r"C:\a", "/work")]
        assert suggest_share_target("/work", taken, "\\\\") == "/mnt/share"


class TestExplainRunFailure:
    def test_name_in_use(self) -> None:
        spec = ContainerSpec(image="img", name="dev")
        output = 'Error: creating container storage: the container name "dev" is already in use'
        assert "already exists" in explain_run_failure(output, spec)

    def test_other_errors_pass_through(self) -> None:
        spec = ContainerSpec(image="img")
        assert explain_run_failure("noise\nError: something odd", spec) == "Error: something odd"

    def test_empty_output(self) -> None:
        assert "without output" in explain_run_failure("", ContainerSpec(image="img"))


class TestFailedStart:
    def test_created_is_a_failed_start(self) -> None:
        assert is_failed_start("Created")

    def test_running_and_exited_are_not(self) -> None:
        assert not is_failed_start("Up 2 minutes")
        assert not is_failed_start("Exited (0) 1 minute ago")
        assert not is_failed_start("")
