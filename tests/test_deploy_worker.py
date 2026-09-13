"""The deploy worker's order of operations: run, then clean up a failed start.

podman is replaced by fakes here; the same flow was also run against a real
podman machine, which is where the behaviours asserted below were measured.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from devenv_forge.core.deploy import ContainerSpec, ImageInfo, Mount  # noqa: E402
from devenv_forge.core.podman import PodmanState, PodmanStatus  # noqa: E402
from devenv_forge.ui import deploy_page  # noqa: E402


@pytest.fixture
def share(tmp_path):
    folder = tmp_path / "projects"
    folder.mkdir()
    return str(folder)


@pytest.fixture
def worker(monkeypatch):
    calls: list[list[str]] = []
    state = {"run_code": 0, "run_output": "", "container": ""}

    def fake_stream(self, argv):
        calls.append(list(argv))
        if argv[1] == "run":
            return state["run_code"], state["run_output"]
        return 0, ""

    monkeypatch.setattr(deploy_page.DeployWorker, "_stream", fake_stream)
    monkeypatch.setattr(deploy_page, "container_state", lambda name: state["container"])

    w = deploy_page.DeployWorker()
    results: list[tuple[bool, str]] = []
    w.deploy_finished.connect(lambda ok, message: results.append((ok, message)))
    return w, calls, state, results


def _subcommands(calls):
    return [argv[1] for argv in calls]


def test_runs_straight_away_in_the_image_working_directory(worker, share) -> None:
    """Nothing is probed first, and no -w is passed: the image decides."""
    w, calls, state, results = worker
    w.deploy(ContainerSpec(image="img", name="dev", mounts=[Mount(share, "/work")]), False)

    assert _subcommands(calls) == ["run"]
    assert "-w" not in calls[0]
    assert results[-1] == (True, "dev is running")


def test_failed_start_removes_the_leftover_container(worker, share) -> None:
    """podman run creates before it starts, leaving a Created container behind."""
    w, calls, state, results = worker
    state.update(run_code=125, container="Created",
                 run_output="Error: crun: something went wrong at start")
    w.deploy(ContainerSpec(image="img", name="dev", mounts=[Mount(share, "/work")]), False)

    assert _subcommands(calls) == ["run", "rm"]
    assert calls[-1][-1] == "dev"
    ok, message = results[-1]
    assert not ok and message == "Error: crun: something went wrong at start"


def test_a_running_container_is_never_removed_on_failure(worker, share) -> None:
    """Only a container that never started is cleaned up."""
    w, calls, state, results = worker
    state.update(run_code=125, container="Up 3 minutes", run_output="Error: something")
    w.deploy(ContainerSpec(image="img", name="dev"), False)
    assert "rm" not in _subcommands(calls)


def test_name_in_use_is_explained(worker) -> None:
    w, calls, state, results = worker
    state.update(run_code=125, container="Up 3 minutes",
                 run_output='Error: the container name "dev" is already in use')
    w.deploy(ContainerSpec(image="img", name="dev"), False)
    ok, message = results[-1]
    assert not ok and "already exists" in message


def test_replace_removes_first(worker, share) -> None:
    w, calls, state, results = worker
    w.deploy(ContainerSpec(image="img", name="dev", mounts=[Mount(share, "/work")]), True)
    assert _subcommands(calls) == ["rm", "run"]


class TestRefresh:
    """The list carries each image's working directory for the page to show."""

    def _ready(self, monkeypatch, images, working_dirs):
        status = PodmanStatus(PodmanState.READY, executable="podman")
        monkeypatch.setattr(deploy_page.podman_cli, "status", lambda: status)
        monkeypatch.setattr(deploy_page, "list_images", lambda: images)
        monkeypatch.setattr(deploy_page, "list_containers", lambda: [])
        monkeypatch.setattr(deploy_page, "image_working_dirs", working_dirs)

    def _listed(self):
        w = deploy_page.DeployWorker()
        emitted: list[tuple] = []
        w.listed.connect(lambda *args: emitted.append(args))
        logged: list[str] = []
        w.output.connect(logged.append)
        w.refresh()
        return emitted[-1], logged

    def test_working_directories_are_emitted(self, monkeypatch) -> None:
        images = [ImageInfo("localhost/devenv-arch", "defaults", "a1a1a1a1a1a1")]
        self._ready(monkeypatch, images, lambda imgs: {"localhost/devenv-arch:defaults": "/work"})
        (listed_images, _containers, _state, working_dirs), _logged = self._listed()
        assert listed_images == images
        assert working_dirs == {"localhost/devenv-arch:defaults": "/work"}

    def test_an_inspect_failure_still_lists_the_images(self, monkeypatch) -> None:
        images = [ImageInfo("localhost/devenv-arch", "defaults", "a1a1a1a1a1a1")]

        def broken(_images):
            raise OSError("inspect blew up")

        self._ready(monkeypatch, images, broken)
        (listed_images, _containers, _state, working_dirs), logged = self._listed()
        assert listed_images == images and working_dirs == {}
        assert any("inspect blew up" in line for line in logged)

    def test_no_images_means_no_inspect(self, monkeypatch) -> None:
        def must_not_run(_images):
            raise AssertionError("inspected with no images")

        self._ready(monkeypatch, [], must_not_run)
        (_images, _containers, _state, working_dirs), _logged = self._listed()
        assert working_dirs == {}


class TestPage:
    """The page shows where a container starts and cannot change it."""

    @pytest.fixture
    def page(self, qt_app):
        from PyQt6.QtWidgets import QLineEdit

        from devenv_forge.ui.theme import LIGHT

        page = deploy_page.DeployPage(LIGHT)
        images = [
            ImageInfo("localhost/devenv-arch", "defaults", "a1a1a1a1a1a1"),
            ImageInfo("docker.io/library/archlinux", "latest", "b2b2b2b2b2b2"),
        ]
        page._on_listed(
            images, [], PodmanStatus(PodmanState.READY, executable="podman"),
            {"localhost/devenv-arch:defaults": "/work", "docker.io/library/archlinux:latest": "/"},
        )
        yield page, QLineEdit
        page.shutdown()

    def test_no_working_directory_field(self, page) -> None:
        widget, line_edit = page
        # The container name is the only text field left outside the shares table.
        assert [e for e in widget.findChildren(line_edit) if e is not widget.name_edit] == []
        assert "-w" not in widget.preview.toPlainText()

    def test_first_share_lands_where_the_container_starts(self, page, share, monkeypatch) -> None:
        widget, _ = page
        assert "share a folder at /work" in widget.start_label.text()

        monkeypatch.setattr(
            deploy_page.QFileDialog, "getExistingDirectory", lambda *a, **k: share
        )
        widget._add_share()

        assert widget.mounts()[0].container == "/work"
        assert "shared from" in widget.start_label.text()
        assert widget.deploy_button.isEnabled()

    def test_note_follows_the_selected_image(self, page) -> None:
        widget, _ = page
        widget.image_picker.setCurrentIndex(widget.image_picker.findData("docker.io/library/archlinux:latest"))
        assert "sets no working directory" in widget.start_label.text()
