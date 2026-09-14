"""Tests for deploying an image as a container with shared directories."""

from __future__ import annotations

import pytest

from devenv_forge.core.deploy import (
    NAME_RE,
    PROTECTED_TARGETS,
    ContainerSpec,
    ImageInfo,
    Mount,
    Volume,
    list_containers,
    list_images,
    machine_path,
    remove_command,
    shell_command,
    stop_command,
    suggest_name,
    suggest_volume,
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



class TestMachinePath:
    """A volume's device is resolved inside the machine, where C: means nothing."""

    def test_a_drive_letter_becomes_a_mount_point(self) -> None:
        assert machine_path("D:\\yocto\\build") == "/mnt/d/yocto/build"

    def test_forward_slashes_are_the_same_path(self) -> None:
        assert machine_path("D:/yocto") == "/mnt/d/yocto"

    def test_a_bare_drive(self) -> None:
        assert machine_path("D:\\") == "/mnt/d"

    def test_the_drive_letter_is_lowercased(self) -> None:
        assert machine_path("C:\\Work") == "/mnt/c/Work"

    def test_a_posix_path_is_the_machines_own(self) -> None:
        """Not everything is a Windows path; the machine has its own disk."""
        assert machine_path("/var/lib/yocto") == "/var/lib/yocto"

    def test_quotes_and_spaces_survive(self) -> None:
        assert machine_path('"D:\\my builds"') == "/mnt/d/my builds"

    def test_nothing(self) -> None:
        assert machine_path("   ") == ""


class TestVolume:
    """Storage podman owns, as opposed to a directory of this desktop's."""

    def test_it_mounts_by_name_not_by_path(self) -> None:
        assert Volume("yocto-build", "/work/build").to_arg() == "yocto-build:/work/build"

    def test_podman_storage_needs_no_options(self) -> None:
        assert Volume("v", "/work").create_argv("podman") == [
            "podman", "volume", "create", "v",
        ]

    def test_a_location_pins_it_with_the_local_driver(self) -> None:
        argv = Volume("v", "/work", "D:\\yocto").create_argv("podman")
        assert argv[:5] == ["podman", "volume", "create", "--driver", "local"]
        assert "type=none" in argv and "o=bind" in argv
        assert "device=/mnt/d/yocto" in argv
        assert argv[-1] == "v"

    def test_the_windows_path_never_reaches_podman(self) -> None:
        """It would be resolved inside the machine, which has no D: drive."""
        argv = Volume("v", "/work", "D:\\yocto").create_argv("podman")
        assert not any("D:" in token for token in argv)

    def test_a_machine_path_is_used_as_typed(self) -> None:
        argv = Volume("v", "/work", "/var/lib/yocto").create_argv("podman")
        assert "device=/var/lib/yocto" in argv


class TestVolumeWarnings:
    """Windows-backed storage works; it is the wrong place for a build tree."""

    def test_a_windows_drive_is_flagged(self) -> None:
        assert Volume("v", "/work", "D:\\yocto").warnings()

    def test_the_flag_carries_the_measurement(self) -> None:
        """Numbers beat adjectives when someone is about to wait on a build."""
        note = Volume("v", "/work", "D:\\yocto").warnings()[0]
        assert "9p" in note and "11.3 s" in note and "28 ms" in note

    def test_a_typed_mount_point_is_flagged_too(self) -> None:
        """However it was typed, /mnt/d is the same translated path."""
        assert Volume("v", "/work", "/mnt/d/yocto").warnings()

    def test_podman_storage_is_not_flagged(self) -> None:
        assert not Volume("v", "/work").warnings()

    def test_a_path_inside_the_machine_is_not_flagged(self) -> None:
        assert not Volume("v", "/work", "/var/lib/yocto").warnings()

    def test_mnt_wsl_is_not_a_drive(self) -> None:
        """/mnt is where drives appear, but not everything under it is one."""
        assert not Volume("v", "/work", "/mnt/wsl/yocto").warnings()


class TestVolumeValidation:
    def test_a_clean_volume_has_no_problems(self, host_dir: str) -> None:
        assert Volume("yocto-build", "/work/build", host_dir).problems() == []

    def test_podman_storage_needs_no_location(self) -> None:
        assert Volume("yocto-build", "/work/build").problems() == []

    def test_rejects_an_empty_name(self) -> None:
        assert any("name is empty" in p for p in Volume("", "/work").problems())

    def test_rejects_a_name_podman_would_refuse(self) -> None:
        assert any("volume name" in p for p in Volume("has space", "/work").problems())

    def test_container_path_rules_are_the_shares_rules(self) -> None:
        assert Volume("v", "relative").problems()
        assert Volume("v", "/usr").problems()
        assert Volume("v", "/work:extra").problems()

    def test_rejects_a_network_path(self) -> None:
        """The machine sees local drives; a UNC path is not one of them."""
        assert any("network path" in p for p in Volume("v", "/work", "\\\\nas\\share").problems())

    def test_rejects_a_relative_location(self) -> None:
        assert any("absolute" in p for p in Volume("v", "/work", "builds").problems())

    def test_reports_a_location_that_is_not_there(self, tmp_path) -> None:
        missing = str(tmp_path / "not-created")
        assert any("does not exist" in p for p in Volume("v", "/work", missing).problems())

    def test_a_machine_path_is_left_to_podman(self) -> None:
        """This desktop cannot see inside the machine, so it does not guess."""
        assert Volume("v", "/work", "/var/lib/yocto").problems() == []


class TestSpecWithVolumes:
    def test_volumes_become_v_flags(self, host_dir: str) -> None:
        spec = ContainerSpec(
            image="img",
            mounts=[Mount(host_dir, "/work/layers")],
            volumes=[Volume("build", "/work/build")],
        )
        argv = spec.argv("podman")
        assert argv.count("-v") == 2
        assert "build:/work/build" in argv
        assert argv[-1] == "img"

    def test_setup_runs_before_the_container(self, host_dir: str) -> None:
        spec = ContainerSpec(image="img", volumes=[Volume("build", "/work/build")])
        assert spec.setup_argv("podman") == [["podman", "volume", "create", "build"]]

    def test_no_volumes_means_no_setup(self, host_dir: str) -> None:
        assert ContainerSpec(image="img", mounts=[Mount(host_dir, "/work")]).setup_argv() == []

    def test_a_share_and_a_volume_cannot_share_a_target(self, host_dir: str) -> None:
        """podman would take the last -v and say nothing."""
        spec = ContainerSpec(
            image="img",
            mounts=[Mount(host_dir, "/work/build")],
            volumes=[Volume("build", "/work/build")],
        )
        assert any("same container path" in p for p in spec.problems())

    def test_two_volumes_cannot_have_one_name(self) -> None:
        spec = ContainerSpec(
            image="img",
            volumes=[Volume("build", "/work/a"), Volume("build", "/work/b")],
        )
        assert any("same name" in p for p in spec.problems())

    def test_warnings_are_not_problems(self) -> None:
        """A Windows-backed volume still starts; it is only worth a word."""
        spec = ContainerSpec(image="img", volumes=[Volume("build", "/work/build", "D:\\yocto")])
        assert spec.warnings()
        assert not any("9p" in p for p in spec.problems())

    def test_the_setup_preview_reads_as_commands(self) -> None:
        spec = ContainerSpec(image="img", volumes=[Volume("build", "/work/build", "D:\\yocto")])
        text = spec.setup_preview("podman")
        assert text.startswith("podman volume create")
        assert "device=/mnt/d/yocto" in text


class TestSuggestVolume:
    def test_named_after_the_container(self) -> None:
        assert suggest_volume("yocto") == ("yocto-data", "/work/data")

    def test_an_image_reference_is_cleaned_up(self) -> None:
        name, target = suggest_volume("localhost/devenv:latest")
        assert name == "devenv-latest-data"
        assert target == "/work/data"

    def test_it_avoids_a_name_already_taken(self) -> None:
        assert suggest_volume("yocto", ["yocto-data"]) == ("yocto-data2", "/work/data2")

    def test_the_suggestions_are_valid(self) -> None:
        name, target = suggest_volume("localhost/devenv:latest")
        assert Volume(name, target).problems() == []

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
