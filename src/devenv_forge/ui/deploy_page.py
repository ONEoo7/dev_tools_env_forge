"""Deploy page: run a built image as a container with host directories shared."""

from __future__ import annotations

import sys

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.deploy import (
    ContainerInfo,
    ContainerSpec,
    ImageInfo,
    Mount,
    Volume,
    container_state,
    explain_run_failure,
    image_working_dirs,
    is_failed_start,
    list_containers,
    list_images,
    remove_command,
    shell_command,
    start_note,
    stop_command,
    suggest_name,
    suggest_share_target,
    suggest_volume,
)
from ..core import podman as podman_cli
from ..core.podman import PodmanState
from ..core.runner import run, stream
from .cards import SectionHeader
from .theme import Palette

MOUNT_COLUMNS = ("Host directory", "Container path", "Read-only", "")
VOLUME_COLUMNS = ("Name", "Container path", "Location (empty: inside the machine)")


class DeployWorker(QObject):
    """Talks to podman off the UI thread."""

    #: images, containers, podman status, {image reference: working directory}
    listed = pyqtSignal(list, list, object, dict)
    output = pyqtSignal(str)
    #: label, exit code, podman's own error text
    finished = pyqtSignal(str, int, str)
    #: succeeded, message to show
    deploy_finished = pyqtSignal(bool, str)

    @pyqtSlot()
    def refresh(self) -> None:
        # Ask why podman is unusable before listing, so an empty list is never
        # reported as "no images" when the real cause is something else.
        state = podman_cli.status()
        images: list = []
        containers: list = []
        if state.ready:
            try:
                images = list_images()
                containers = list_containers()
            except Exception as exc:
                self.output.emit(f"could not query podman: {exc}")
        working_dirs: dict = {}
        if images:
            try:
                working_dirs = image_working_dirs(images)
            except Exception as exc:
                self.output.emit(f"could not read image working directories: {exc}")
        self.listed.emit(images, containers, state, working_dirs)

    def _stream(self, argv: list) -> tuple[int, str]:
        self.output.emit(f"$ {' '.join(argv)}")
        code = 1
        lines: list[str] = []
        for kind, text in stream(argv, timeout=1800):
            if kind == "line":
                lines.append(text)
                self.output.emit(text)
            else:
                code = int(text) if text.lstrip("-").isdigit() else 1
        return code, "\n".join(lines)

    @pyqtSlot(list, str)
    def execute(self, argv: list, label: str) -> None:
        code, output = self._stream(argv)
        errors = [ln for ln in output.splitlines() if ln.strip().lower().startswith("error")]
        self.finished.emit(label, code, errors[-1].strip() if errors else "")

    @pyqtSlot(object, bool)
    def deploy(self, spec: ContainerSpec, replace: bool) -> None:
        """Create the volumes, run the container, clean up a failed start."""
        if replace and spec.name:
            self._stream(remove_command(spec.name))

        if not self._create_volumes(spec):
            return

        code, output = self._stream(spec.argv())
        if code == 0:
            self.deploy_finished.emit(True, f"{spec.name or spec.image} is running")
            return

        # podman run creates the container before starting it, so a failed
        # start leaves it behind in Created state, holding the name and making
        # the next attempt fail with "name already in use".
        if spec.name and is_failed_start(container_state(spec.name)):
            self._stream(remove_command(spec.name))
            self.output.emit(f"removed the container {spec.name} left by the failed start")
        self.deploy_finished.emit(False, explain_run_failure(output, spec))

    def _create_volumes(self, spec: ContainerSpec) -> bool:
        """Create the volumes the container asks for. False stops the deploy.

        A volume that is already there is reused rather than treated as a
        failure, but its location is whatever it was created with: podman will
        not repoint an existing volume, so saying so beats letting someone
        believe the location typed here took effect.
        """
        for volume in spec.volumes:
            if not volume.name.strip():
                continue
            code, output = self._stream(volume.create_argv())
            if code == 0:
                continue
            if "already exists" in output.lower():
                self.output.emit(
                    f"volume {volume.name} already exists and is reused; its "
                    "location is the one it was created with"
                )
                continue
            self.deploy_finished.emit(
                False, f"could not create the volume {volume.name}: {output.strip()}"
            )
            return False
        return True


class DeployPage(QWidget):
    """Choose an image, share directories, and start a container."""

    status_message = pyqtSignal(str)
    request_refresh = pyqtSignal()
    request_execute = pyqtSignal(list, str)
    request_deploy = pyqtSignal(object, bool)

    def __init__(self, palette: Palette) -> None:
        super().__init__()
        self.palette_ = palette
        self.images: list[ImageInfo] = []
        self.podman_state = None
        self.containers: list[ContainerInfo] = []
        #: image reference -> the directory its containers start in
        self.working_dirs: dict[str, str] = {}
        self._busy = False

        self._build_ui()
        self._start_worker()
        self._refresh_preview()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(12)

        self.header = SectionHeader(
            "Deploy container",
            "Run a built image with directories shared between this machine and "
            "the container.",
        )
        self.start_machine_button = QPushButton("Start machine")
        self.start_machine_button.clicked.connect(self._start_machine)
        self.start_machine_button.hide()
        self.header.actions.addWidget(self.start_machine_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(lambda: self.start(force=True))
        self.header.actions.addWidget(self.refresh_button)
        root.addWidget(self.header)

        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(
            f"background: {self.palette_.surface_alt}; "
            f"border: 1px solid {self.palette_.warn}; color: {self.palette_.text}; "
            "border-radius: 8px; padding: 10px 12px;"
        )
        self.banner.hide()
        root.addWidget(self.banner)

        root.addLayout(self._image_row())
        root.addWidget(self._shares_section(), 1)
        root.addWidget(self._volumes_section(), 1)
        root.addLayout(self._options_row())

        preview_label = QLabel("Command")
        preview_label.setObjectName("CardTitle")
        root.addWidget(preview_label)
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("Detail")
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(110)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        root.addWidget(self.preview)

        self.problem_label = QLabel("")
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet(f"color: {self.palette_.danger};")
        self.problem_label.hide()
        root.addWidget(self.problem_label)

        root.addWidget(self._running_section())
        root.addLayout(self._actions_row())

        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        self.log.setFixedHeight(110)
        self.log.hide()
        root.addWidget(self.log)

    def _image_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Image"))
        self.image_picker = QComboBox()
        self.image_picker.setMinimumWidth(320)
        self.image_picker.currentIndexChanged.connect(self._on_image_changed)
        row.addWidget(self.image_picker, 1)

        row.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit()
        self.name_edit.setMaximumWidth(220)
        self.name_edit.textChanged.connect(self._refresh_preview)
        row.addWidget(self.name_edit)
        return row

    def _shares_section(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Shared directories")
        title.setObjectName("CardTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.add_share_button = QPushButton("Add share")
        self.add_share_button.clicked.connect(self._add_share)
        header.addWidget(self.add_share_button)
        self.remove_share_button = QPushButton("Remove")
        self.remove_share_button.clicked.connect(self._remove_share)
        header.addWidget(self.remove_share_button)
        layout.addLayout(header)

        hint = QLabel(
            "The host path is written as you pick it; podman translates it for "
            "the virtual machine. Edits inside the container appear on this "
            "machine and the other way round."
        )
        hint.setObjectName("PageSubtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, len(MOUNT_COLUMNS))
        self.table.setHorizontalHeaderLabels(MOUNT_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(lambda _i: self._refresh_preview())
        layout.addWidget(self.table, 1)

        # Shown, never edited. The container starts in the image's own working
        # directory; a free text field here let people point it at nothing.
        self.start_label = QLabel("")
        self.start_label.setObjectName("PageSubtitle")
        self.start_label.setWordWrap(True)
        layout.addWidget(self.start_label)
        return panel

    def _volumes_section(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Volumes")
        title.setObjectName("CardTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.add_volume_button = QPushButton("Add volume")
        self.add_volume_button.clicked.connect(self._add_volume)
        header.addWidget(self.add_volume_button)
        self.locate_volume_button = QPushButton("Set location...")
        self.locate_volume_button.clicked.connect(self._locate_volume)
        header.addWidget(self.locate_volume_button)
        self.remove_volume_button = QPushButton("Remove")
        self.remove_volume_button.clicked.connect(self._remove_volume)
        header.addWidget(self.remove_volume_button)
        layout.addLayout(header)

        hint = QLabel(
            "Storage podman owns, created before the container starts. Leave "
            "the location empty and it lives inside the virtual machine on a "
            "Linux filesystem, which is what a build tree wants. Give it a "
            "location to pin it somewhere: a folder on this PC through Set "
            "location, or a path the machine itself has. Removing the container "
            "leaves a volume alone."
        )
        hint.setObjectName("PageSubtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.volume_table = QTableWidget(0, len(VOLUME_COLUMNS))
        self.volume_table.setHorizontalHeaderLabels(VOLUME_COLUMNS)
        self.volume_table.verticalHeader().setVisible(False)
        self.volume_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        head = self.volume_table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.volume_table.itemChanged.connect(lambda _i: self._refresh_preview())
        layout.addWidget(self.volume_table, 1)

        # Only ever a warning: a volume on a Windows drive works, it is just the
        # wrong place for a build, and saying so beats refusing to start.
        self.volume_note = QLabel("")
        self.volume_note.setWordWrap(True)
        self.volume_note.setStyleSheet(f"color: {self.palette_.warn};")
        self.volume_note.hide()
        layout.addWidget(self.volume_note)
        return panel

    # -- volumes -----------------------------------------------------------

    def _add_volume(self) -> None:
        """A volume in podman's own storage, which is the one to want."""
        name, target = suggest_volume(
            str(self.name_edit.text() or self.image_picker.currentData() or "devenv"),
            [v.name for v in self.volumes()],
        )
        row = self.volume_table.rowCount()
        self.volume_table.insertRow(row)
        self.volume_table.setItem(row, 0, QTableWidgetItem(name))
        self.volume_table.setItem(row, 1, QTableWidgetItem(target))
        self.volume_table.setItem(row, 2, QTableWidgetItem(""))
        self._refresh_preview()

    def _locate_volume(self) -> None:
        """Pin the selected volume to a folder on this machine."""
        row = self._selected_volume_row()
        if row is None:
            QMessageBox.information(
                self, "Set location", "Select a volume row first."
            )
            return
        directory = QFileDialog.getExistingDirectory(self, "Folder to hold the volume")
        if not directory:
            return
        native = directory.replace("/", "\\") if sys.platform == "win32" else directory
        self.volume_table.setItem(row, 2, QTableWidgetItem(native))
        self._refresh_preview()

    def _selected_volume_row(self) -> int | None:
        rows = {i.row() for i in self.volume_table.selectedItems()}
        return min(rows) if rows else None

    def _remove_volume(self) -> None:
        rows = sorted({i.row() for i in self.volume_table.selectedItems()}, reverse=True)
        for row in rows:
            self.volume_table.removeRow(row)
        self._refresh_preview()

    def volumes(self) -> list[Volume]:
        result: list[Volume] = []
        for row in range(self.volume_table.rowCount()):
            cells = [self.volume_table.item(row, column) for column in range(3)]
            result.append(
                Volume(
                    name=cells[0].text().strip() if cells[0] else "",
                    container=cells[1].text().strip() if cells[1] else "",
                    location=cells[2].text().strip() if cells[2] else "",
                )
            )
        return result

    def _options_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        self.detached_check = QCheckBox("Keep running in the background")
        self.detached_check.setChecked(True)
        self.detached_check.setToolTip(
            "Starts the container detached with a terminal attached, so it stays "
            "up and you can open a shell into it. Without a terminal the shell "
            "would exit immediately."
        )
        self.detached_check.toggled.connect(self._refresh_preview)
        row.addWidget(self.detached_check)

        self.rm_check = QCheckBox("Remove when it stops")
        self.rm_check.toggled.connect(self._refresh_preview)
        row.addWidget(self.rm_check)
        row.addStretch(1)
        return row

    def _running_section(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        title = QLabel("Containers")
        title.setObjectName("CardTitle")
        layout.addWidget(title)

        self.container_table = QTableWidget(0, 3)
        self.container_table.setHorizontalHeaderLabels(("Name", "Image", "Status"))
        self.container_table.verticalHeader().setVisible(False)
        self.container_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.container_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.container_table.setFixedHeight(120)
        head = self.container_table.horizontalHeader()
        for column in range(3):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.container_table)
        return panel

    def _actions_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.log_toggle = QPushButton("Show log")
        self.log_toggle.setObjectName("Link")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_toggle.toggled.connect(self._on_log_toggle)
        row.addWidget(self.log_toggle)
        row.addStretch(1)

        self.shell_button = QPushButton("Open shell")
        self.shell_button.clicked.connect(self._open_shell)
        row.addWidget(self.shell_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._stop_selected)
        row.addWidget(self.stop_button)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_selected)
        row.addWidget(self.remove_button)
        self.deploy_button = QPushButton("Deploy")
        self.deploy_button.setObjectName("Primary")
        self.deploy_button.clicked.connect(self._deploy)
        row.addWidget(self.deploy_button)
        return row

    def _start_worker(self) -> None:
        self.thread = QThread(self)
        self.worker = DeployWorker()
        self.worker.moveToThread(self.thread)
        self.worker.listed.connect(self._on_listed)
        self.worker.output.connect(self._append_log)
        self.worker.finished.connect(self._on_finished)
        self.worker.deploy_finished.connect(self._on_deploy_finished)
        self.request_deploy.connect(self.worker.deploy)
        self.request_refresh.connect(self.worker.refresh)
        self.request_execute.connect(self.worker.execute)
        self.thread.start()

    def start(self, force: bool = False) -> None:
        if force or not self.images:
            self.status_message.emit("Querying podman...")
            self.request_refresh.emit()

    def shutdown(self) -> None:
        if self.thread.isRunning():
            self.thread.quit()
            if not self.thread.wait(4000):
                self.thread.terminate()
                self.thread.wait(1000)

    # -- state -------------------------------------------------------------

    def _on_listed(self, images: list, containers: list, state, working_dirs: dict) -> None:
        self.images = images
        self.containers = containers
        self.podman_state = state
        self.working_dirs = working_dirs
        self._show_podman_state(state)

        previous = self.image_picker.currentData()
        self.image_picker.blockSignals(True)
        self.image_picker.clear()
        for image in images:
            label = f"{image.reference}   {image.size}"
            self.image_picker.addItem(label, image.reference)
        if previous:
            index = self.image_picker.findData(previous)
            if index >= 0:
                self.image_picker.setCurrentIndex(index)
        self.image_picker.blockSignals(False)

        self.container_table.setRowCount(len(containers))
        for row, container in enumerate(containers):
            for column, value in enumerate(
                (container.name, container.image, container.status)
            ):
                item = QTableWidgetItem(value)
                if column == 2:
                    item.setForeground(
                        Qt.GlobalColor.green if container.running else Qt.GlobalColor.gray
                    )
                self.container_table.setItem(row, column, item)

        if state.ready:
            if images:
                self.status_message.emit(
                    f"{len(images)} image(s), {len(containers)} container(s)"
                )
            else:
                self.status_message.emit(
                    "Podman is running but has no images yet. Build one on the "
                    "Build image page."
                )
        if not self.name_edit.text().strip():
            self._on_image_changed()
        self._refresh_preview()

    def _show_podman_state(self, state) -> None:
        """Explain an unusable podman rather than showing an empty list."""
        self.start_machine_button.setVisible(state.state is PodmanState.MACHINE_STOPPED)
        if state.ready:
            self.banner.hide()
            return
        message = state.detail or "Podman is not usable."
        if state.hint:
            message = f"{message}  {state.hint}"
        self.banner.setText(message)
        self.banner.show()
        self.status_message.emit(message.splitlines()[0][:120])

    def _start_machine(self) -> None:
        self.log_toggle.setChecked(True)
        self._run(podman_cli.machine_start_command(), "machine start")

    def _on_image_changed(self) -> None:
        reference = self.image_picker.currentData()
        if reference:
            self.name_edit.setText(suggest_name(str(reference)))
        self._refresh_preview()

    # -- shares ------------------------------------------------------------

    def _add_share(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Directory to share")
        if not directory:
            return
        # Qt hands back forward slashes; show the native form the user expects.
        native = directory.replace("/", "\\") if sys.platform == "win32" else directory
        # The first share lands where the container starts, so it opens among
        # the shared files.
        image = str(self.image_picker.currentData() or "")
        suggested = suggest_share_target(self.working_dirs.get(image), self.mounts(), native)
        self._append_row(native, suggested, read_only=False)
        self._refresh_preview()

    def _append_row(self, host: str, container: str, read_only: bool) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(host))
        self.table.setItem(row, 1, QTableWidgetItem(container))
        check = QCheckBox()
        check.setChecked(read_only)
        check.toggled.connect(self._refresh_preview)
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(check)
        self.table.setCellWidget(row, 2, holder)

    def _remove_share(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
        self._refresh_preview()

    def mounts(self) -> list[Mount]:
        result: list[Mount] = []
        for row in range(self.table.rowCount()):
            host_item = self.table.item(row, 0)
            container_item = self.table.item(row, 1)
            holder = self.table.cellWidget(row, 2)
            check = holder.findChild(QCheckBox) if holder is not None else None
            result.append(
                Mount(
                    host=host_item.text().strip() if host_item else "",
                    container=container_item.text().strip() if container_item else "",
                    read_only=bool(check.isChecked()) if check is not None else False,
                )
            )
        return result

    def current_spec(self) -> ContainerSpec:
        return ContainerSpec(
            image=str(self.image_picker.currentData() or ""),
            name=self.name_edit.text().strip(),
            mounts=self.mounts(),
            volumes=self.volumes(),
            detached=self.detached_check.isChecked(),
            remove_on_exit=self.rm_check.isChecked(),
        )

    def _refresh_preview(self) -> None:
        spec = self.current_spec()
        setup = spec.setup_preview()
        # The volume commands run first, so they read first.
        parts = [part for part in (setup, spec.preview()) if part]
        self.preview.setPlainText("\n\n".join(parts))
        problems = spec.problems()
        if problems:
            self.problem_label.setText("  •  ".join(problems))
            self.problem_label.show()
        else:
            self.problem_label.hide()
        warnings = spec.warnings()
        self.volume_note.setText("  ".join(warnings))
        self.volume_note.setVisible(bool(warnings))
        self.start_label.setText(start_note(self.working_dirs.get(spec.image), spec.mounts))
        self.deploy_button.setEnabled(not problems and not self._busy)

    # -- actions -----------------------------------------------------------

    def _selected_container(self) -> str:
        rows = {i.row() for i in self.container_table.selectedItems()}
        if not rows:
            return ""
        item = self.container_table.item(next(iter(rows)), 0)
        return item.text() if item else ""

    def _deploy(self) -> None:
        state = podman_cli.status()
        if not state.ready:
            QMessageBox.warning(
                self,
                "Podman not available",
                "\n\n".join(part for part in (state.detail, state.hint) if part),
            )
            self._show_podman_state(state)
            return
        spec = self.current_spec()
        if spec.problems():
            return
        replace = False
        existing = next((c for c in self.containers if c.name == spec.name), None)
        if spec.name and existing is not None:
            if is_failed_start(existing.status):
                prompt = (
                    f"A container called {spec.name} exists but never started, "
                    "most likely left by an earlier deploy that failed. Replace it?"
                )
            else:
                prompt = f"A container called {spec.name} already exists. Replace it?"
            answer = QMessageBox.question(self, "Name already used", prompt)
            if answer != QMessageBox.StandardButton.Yes:
                return
            replace = True
        self.log_toggle.setChecked(True)
        self._busy = True
        self.deploy_button.setEnabled(False)
        self.status_message.emit(f"Deploying {spec.name or spec.image}...")
        self.request_deploy.emit(spec, replace)

    def _on_deploy_finished(self, ok: bool, message: str) -> None:
        self._busy = False
        self.status_message.emit(message.splitlines()[0] if message else "")
        if not ok:
            QMessageBox.warning(self, "Deploy failed", message)
        self._refresh_preview()
        self.request_refresh.emit()

    def _run(self, argv: list, label: str) -> None:
        self._busy = True
        self.deploy_button.setEnabled(False)
        self.status_message.emit(f"Running podman {label}...")
        self.request_execute.emit(argv, label)

    def _on_finished(self, label: str, code: int, error: str) -> None:
        self._busy = False
        if code == 0:
            self.status_message.emit(f"{label} succeeded")
        else:
            self.status_message.emit(f"{label} failed with exit code {code}")
            QMessageBox.warning(
                self, f"{label} failed",
                error or f"podman exited with code {code}. The log has the details.",
            )
        self._refresh_preview()
        self.request_refresh.emit()

    def _stop_selected(self) -> None:
        name = self._selected_container()
        if name:
            self.log_toggle.setChecked(True)
            self._run(stop_command(name), "stop")

    def _remove_selected(self) -> None:
        name = self._selected_container()
        if not name:
            return
        answer = QMessageBox.question(
            self, "Remove container",
            f"Remove {name}? Anything written inside it that is not on a shared "
            "directory is lost.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.log_toggle.setChecked(True)
            self._run(remove_command(name), "remove")

    def _open_shell(self) -> None:
        """Open a real terminal attached to the container.

        An interactive shell needs a console, which this window is not, so it is
        handed to the system terminal instead.
        """
        name = self._selected_container()
        if not name:
            QMessageBox.information(
                self, "Open shell", "Select a running container first."
            )
            return
        argv = shell_command(name)
        if sys.platform == "win32":
            run(["cmd", "/c", "start", "", *argv], timeout=15)
        elif sys.platform == "darwin":
            run(["open", "-a", "Terminal", "--args", *argv], timeout=15)
        else:
            run(["x-terminal-emulator", "-e", *argv], timeout=15)
        self.status_message.emit(f"Opened a shell in {name}")

    def _append_log(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _on_log_toggle(self, checked: bool) -> None:
        self.log.setVisible(checked)
        self.log_toggle.setText("Hide log" if checked else "Show log")
