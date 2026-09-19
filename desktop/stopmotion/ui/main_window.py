"""The application window.

Owns the device manager, the phone connection, the current project and the
undo stack, and implements the ``TimelineContext`` protocol that the command
classes in ``commands.py`` talk to.  Every timeline mutation in the whole UI
goes through a command pushed onto that stack -- nothing mutates the project
directly, because that is what makes undo restore the selection too.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Slot
from PySide6.QtGui import QAction, QActionGroup, QImage, QKeySequence, QUndoStack
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..camera import CameraCapabilities, CameraSettings
from ..commands import (
    CaptureFrame,
    DeleteFrames,
    DuplicateFrames,
    ReorderFrames,
    SetHidden,
    SetHolds,
)
from ..connection import PhoneConnection
from ..device import DeviceManager
from ..project import Crop, Project
from ..settings import AppSettings
from .controls import ControlsPanel, DisconnectBanner, TransportBar
from .export_dialog import ExportDialog
from .filmstrip import Filmstrip
from .new_project_dialog import NewProjectDialog
from .viewport import REVIEW, Viewport

CAPTURE_TIMEOUT_MS = 20000
RECONNECT_MS = 4000
#: bytes per pixel used for the bandwidth readout when the connection does not
#: expose a real byte counter (see docs/decisions-ui.md)
JPEG_BYTES_PER_PIXEL = 0.15


class MainWindow(QMainWindow):
    """TimelineContext: ``project`` / ``selection()`` / ``set_selection()`` /
    ``timeline_changed()``."""

    def __init__(self, *, fake: bool = False, port: int = 8099, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("stopmotion")
        self.resize(1440, 900)

        self.app_settings = AppSettings()
        self.project: Project | None = None
        self._selection: list[str] = []
        self.dirty = False

        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(200)

        self._fake = bool(fake)
        self._port = int(port)
        self._connected = False
        self._device_serial: str | None = None
        self._connect_failures = 0
        self._pending_capture: str | None = None
        self._last_error = ""

        self.caps: CameraCapabilities | None = None
        self.cam_settings: CameraSettings | None = None

        self.device = DeviceManager(port=self._port, parent=self)
        self.conn = PhoneConnection(port=self._port, parent=self)

        self._build_ui()
        self._build_actions()
        self._wire_transport()
        self._wire_filmstrip()
        self._wire_connection()
        self._restore_view_prefs()

        # perf readout
        self._frame_times: deque[float] = deque(maxlen=90)
        self._frame_bytes = 0.0
        self._last_counter = 0
        self._perf_mark = time.monotonic()
        self._perf_timer = QTimer(self)
        self._perf_timer.timeout.connect(self._update_perf)
        self._perf_timer.start(1000)

        self._capture_timer = QTimer(self)
        self._capture_timer.setSingleShot(True)
        self._capture_timer.timeout.connect(self._on_capture_timeout)

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.timeout.connect(self._try_reconnect)

        self._update_actions()
        self._update_title()
        self._set_status("Not connected")

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.banner = DisconnectBanner(central)
        layout.addWidget(self.banner)

        self.viewport = Viewport(central)
        self.viewport.still_loader = self._load_still
        layout.addWidget(self.viewport, 1)

        self.transport = TransportBar(central)
        layout.addWidget(self.transport)

        self.setCentralWidget(central)

        # right dock: camera controls
        self.controls = ControlsPanel(self)
        scroll = QScrollArea(self)
        scroll.setWidget(self.controls)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(320)
        self.controls_dock = QDockWidget("Camera", self)
        self.controls_dock.setObjectName("controls_dock")
        self.controls_dock.setWidget(scroll)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.controls_dock)

        # bottom dock: filmstrip
        self.filmstrip = Filmstrip(self)
        self.filmstrip_dock = QDockWidget("Timeline", self)
        self.filmstrip_dock.setObjectName("filmstrip_dock")
        self.filmstrip_dock.setWidget(self.filmstrip)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.filmstrip_dock)

        self.status_label = QLabel("")
        self.perf_label = QLabel("")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.perf_label)

    def _build_actions(self) -> None:
        menu_file = self.menuBar().addMenu("&File")

        self.act_new = QAction("&New Project…", self)
        self.act_new.setShortcut(QKeySequence.StandardKey.New)
        self.act_new.triggered.connect(self.new_project)
        menu_file.addAction(self.act_new)

        self.act_open = QAction("&Open Project…", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self.open_project)
        menu_file.addAction(self.act_open)

        self.recent_menu = menu_file.addMenu("Open &Recent")
        self.recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        self._rebuild_recent_menu()

        menu_file.addSeparator()
        self.act_export = QAction("&Export…", self)
        self.act_export.setShortcut(QKeySequence("Ctrl+E"))
        self.act_export.triggered.connect(self.export_movie)
        menu_file.addAction(self.act_export)

        self.act_purge = QAction("&Purge unreferenced frames…", self)
        self.act_purge.triggered.connect(self.purge_unreferenced)
        menu_file.addAction(self.act_purge)

        menu_file.addSeparator()
        self.act_quit = QAction("&Quit", self)
        self.act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.act_quit.triggered.connect(self.close)
        menu_file.addAction(self.act_quit)

        menu_edit = self.menuBar().addMenu("&Edit")
        self.act_undo = self.undo_stack.createUndoAction(self, "&Undo")
        self.act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        menu_edit.addAction(self.act_undo)
        self.act_redo = self.undo_stack.createRedoAction(self, "&Redo")
        self.act_redo.setShortcut(QKeySequence.StandardKey.Redo)
        menu_edit.addAction(self.act_redo)
        menu_edit.addSeparator()

        self.act_capture = QAction("&Capture frame", self)
        self.act_capture.setShortcuts([QKeySequence("Ctrl+Return"), QKeySequence("F5")])
        self.act_capture.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.act_capture.triggered.connect(self.capture_frame)
        menu_edit.addAction(self.act_capture)

        self.act_select_all = QAction("Select &all frames", self)
        self.act_select_all.setShortcut(QKeySequence.StandardKey.SelectAll)
        self.act_select_all.triggered.connect(self.filmstrip.select_all)
        menu_edit.addAction(self.act_select_all)

        self.act_delete = QAction("&Delete frames", self)
        self.act_delete.setShortcut(QKeySequence.StandardKey.Delete)
        self.act_delete.triggered.connect(lambda: self.delete_frames(self.selection()))
        menu_edit.addAction(self.act_delete)

        self.act_hide = QAction("Toggle &hidden", self)
        self.act_hide.setShortcut(QKeySequence("H"))
        self.act_hide.triggered.connect(self._toggle_hidden_selection)
        menu_edit.addAction(self.act_hide)

        self.act_duplicate = QAction("Dup&licate frames", self)
        self.act_duplicate.setShortcut(QKeySequence("Ctrl+D"))
        self.act_duplicate.triggered.connect(lambda: self.duplicate_frames(self.selection()))
        menu_edit.addAction(self.act_duplicate)

        self.act_duration = QAction("Set du&ration…", self)
        self.act_duration.setShortcut(QKeySequence("Ctrl+T"))
        self.act_duration.triggered.connect(
            lambda: self.filmstrip.prompt_duration(self.selection())
        )
        menu_edit.addAction(self.act_duration)

        menu_view = self.menuBar().addMenu("&View")
        self.act_mode = QAction("Toggle &Live / Review", self)
        self.act_mode.setShortcut(QKeySequence(Qt.Key.Key_Space))
        self.act_mode.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.act_mode.triggered.connect(self.viewport.toggle_mode)
        menu_view.addAction(self.act_mode)
        menu_view.addSeparator()

        self.act_onion = QAction("&Onion skin", self)
        self.act_onion.setCheckable(True)
        self.act_onion.setChecked(True)
        self.act_onion.setShortcut(QKeySequence("O"))
        self.act_onion.toggled.connect(self.set_onion_enabled)
        menu_view.addAction(self.act_onion)

        depth_menu = menu_view.addMenu("Onion &depth")
        self._depth_group = QActionGroup(self)
        self._depth_actions: dict[int, QAction] = {}
        for depth in (1, 2, 3):
            action = QAction(f"{depth} frame{'s' if depth > 1 else ''}", self)
            action.setCheckable(True)
            action.setShortcut(QKeySequence(str(depth)))
            action.triggered.connect(lambda _c=False, d=depth: self.set_onion_depth(d))
            self._depth_group.addAction(action)
            depth_menu.addAction(action)
            self._depth_actions[depth] = action

        opacity_menu = menu_view.addMenu("Onion &opacity")
        self._opacity_group = QActionGroup(self)
        self._opacity_actions: dict[int, QAction] = {}
        for pct in (20, 35, 50, 65, 80):
            action = QAction(f"{pct}%", self)
            action.setCheckable(True)
            action.triggered.connect(lambda _c=False, p=pct: self.set_onion_opacity(p / 100.0))
            self._opacity_group.addAction(action)
            opacity_menu.addAction(action)
            self._opacity_actions[pct] = action

        menu_view.addSeparator()
        self.act_crop = QAction("&Crop guides", self)
        self.act_crop.setCheckable(True)
        self.act_crop.setChecked(True)
        self.act_crop.setShortcut(QKeySequence("G"))
        self.act_crop.toggled.connect(self.set_crop_guides)
        menu_view.addAction(self.act_crop)

        self.addAction(self.act_capture)
        self.addAction(self.act_mode)

    def _restore_view_prefs(self) -> None:
        enabled = self.app_settings.onion_enabled()
        depth = self.app_settings.onion_depth()
        opacity = self.app_settings.onion_opacity()
        self.act_onion.setChecked(enabled)
        self.viewport.set_onion_enabled(enabled)
        self.viewport.set_onion_depth(depth)
        self.viewport.set_onion_opacity(opacity)
        self.transport.set_onion(enabled, depth, opacity)
        if depth in self._depth_actions:
            self._depth_actions[depth].setChecked(True)
        crop = self.app_settings.crop_guides()
        self.act_crop.setChecked(crop)
        self.viewport.set_crop_guides(crop)
        loop = self.app_settings.loop_playback()
        self.transport.loop_check.setChecked(loop)
        self.viewport.set_loop(loop)
        self.transport.set_mode(self.viewport.mode())

    # ------------------------------------------------------------- wiring

    def _wire_transport(self) -> None:
        self.transport.mode_toggle_requested.connect(self.viewport.toggle_mode)
        self.transport.play_pause_requested.connect(self.viewport.toggle_play)
        self.transport.step_requested.connect(self.viewport.step)
        self.transport.loop_changed.connect(self._on_loop_changed)
        self.transport.onion_enabled_changed.connect(self.set_onion_enabled)
        self.transport.onion_depth_changed.connect(self.set_onion_depth)
        self.transport.onion_opacity_changed.connect(self.set_onion_opacity)
        self.transport.capture_requested.connect(self.capture_frame)

        self.viewport.mode_changed.connect(self._on_mode_changed)
        self.viewport.playing_changed.connect(self.transport.set_playing)
        self.viewport.playhead_changed.connect(self._on_playhead)
        self.viewport.capture_requested.connect(self.capture_frame)

        self.controls.settings_changed.connect(self._on_settings_changed)
        self.controls.resend_requested.connect(self._push_settings)

        self.banner.reconnect_requested.connect(self._try_reconnect)
        self.banner.launch_requested.connect(self._launch_phone_app)

    def _wire_filmstrip(self) -> None:
        self.filmstrip.selection_changed.connect(self._on_filmstrip_selection)
        self.filmstrip.delete_requested.connect(self.delete_frames)
        self.filmstrip.hide_requested.connect(self.set_hidden)
        self.filmstrip.duplicate_requested.connect(self.duplicate_frames)
        self.filmstrip.duration_requested.connect(self.set_duration_ms)
        self.filmstrip.reorder_requested.connect(self.reorder_frames)
        self.filmstrip.frame_activated.connect(self._on_frame_activated)
        self.undo_stack.indexChanged.connect(lambda _i: self._update_actions())

    def _wire_connection(self) -> None:
        self.conn.connected.connect(self._on_connected)
        self.conn.disconnected.connect(self._on_disconnected)
        self.conn.hello.connect(self._on_hello)
        self.conn.preview_frame.connect(self._on_preview_frame)
        self.conn.capture_result.connect(self._on_capture_result)
        self.conn.config_ack.connect(self._on_config_ack)
        self.conn.error.connect(self._on_error)

        self.device.device_attached.connect(self._on_device_attached)
        self.device.device_detached.connect(self._on_device_detached)
        self.device.status.connect(self._set_status)

    # --------------------------------------------------- TimelineContext

    def selection(self) -> list[str]:
        return list(self._selection)

    def set_selection(self, ids: list[str]) -> None:
        order = {}
        if self.project is not None:
            order = {f.id: i for i, f in enumerate(self.project.frames)}
        cleaned = sorted({i for i in (ids or []) if i in order}, key=lambda i: order[i])
        self._selection = cleaned
        self.filmstrip.set_selection(cleaned)
        self._update_onion()
        self._update_actions()
        if self.viewport.mode() == REVIEW and len(cleaned) == 1:
            self.viewport.seek_to_frame(cleaned[0])

    def timeline_changed(self) -> None:
        self.filmstrip.refresh()
        self.viewport.timeline_changed()
        self._update_onion()
        self._update_actions()
        self._update_title()
        self._save_project()

    def mark_dirty(self, value: bool = True) -> None:
        """Extra hook in case TimelineCommand wants to flag the project."""
        self.dirty = bool(value)

    set_dirty = mark_dirty

    # ---------------------------------------------------------- commands

    def _require_project(self) -> bool:
        if self.project is None:
            QMessageBox.information(
                self, "No project", "Create or open a project first (File ▸ New Project)."
            )
            return False
        return True

    def delete_frames(self, ids: list[str]) -> None:
        ids = [i for i in (ids or []) if i]
        if not ids or self.project is None:
            return
        self.undo_stack.push(DeleteFrames(self, ids))

    def set_hidden(self, ids: list[str], hidden: bool) -> None:
        ids = [i for i in (ids or []) if i]
        if not ids or self.project is None:
            return
        self.undo_stack.push(SetHidden(self, ids, bool(hidden)))

    def duplicate_frames(self, ids: list[str]) -> None:
        ids = [i for i in (ids or []) if i]
        if not ids or self.project is None:
            return
        self.undo_stack.push(DuplicateFrames(self, ids))

    def set_duration_ms(self, ids: list[str], milliseconds: int) -> None:
        if not ids or self.project is None:
            return
        fps = max(1, int(self.project.fps or 12))
        holds = max(1, int(round(float(milliseconds) * fps / 1000.0)))
        self.undo_stack.push(SetHolds(self, list(ids), holds))

    def reorder_frames(self, ids: list[str], insert_at: int) -> None:
        if not ids or self.project is None:
            return
        self.undo_stack.push(ReorderFrames(self, list(ids), int(insert_at)))

    def _toggle_hidden_selection(self) -> None:
        ids = self.selection()
        if not ids or self.project is None:
            return
        frames = {f.id: f for f in self.project.frames}
        any_visible = any(not frames[i].hidden for i in ids if i in frames)
        self.set_hidden(ids, any_visible)

    # ----------------------------------------------------------- project

    def new_project(self) -> None:
        dialog = NewProjectDialog(
            self,
            default_dir=self.app_settings.last_project_dir(),
            default_fps=self.app_settings.default_fps(),
            default_aspect=self.app_settings.default_aspect(),
        )
        if dialog.exec() != NewProjectDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        size = self.caps.sensor_size if self.caps is not None else (4032, 3024)
        crop = None
        if values["aspect"]:
            crop = Crop.for_aspect(values["aspect"], int(size[0]), int(size[1]))
        try:
            project = Project.create(
                values["path"], values["name"], values["fps"], (int(size[0]), int(size[1])), crop
            )
        except Exception as exc:  # pragma: no cover - filesystem errors
            QMessageBox.critical(self, "Could not create project", str(exc))
            return
        self.app_settings.set_last_project_dir(str(values["path"].parent))
        self.app_settings.set_default_fps(values["fps"])
        self.app_settings.set_default_aspect(values["aspect"] or "No crop")
        self.load_project(project)

    def open_project(self) -> None:
        start = self.app_settings.last_project_dir()
        chosen = QFileDialog.getExistingDirectory(self, "Open project folder", start)
        if chosen:
            self.open_project_path(chosen)

    def open_project_path(self, path: str | Path) -> bool:
        try:
            project = Project.open(Path(path))
        except Exception as exc:
            QMessageBox.critical(self, "Could not open project", f"{path}\n\n{exc}")
            self.app_settings.forget_recent_project(str(path))
            return False
        self.app_settings.set_last_project_dir(str(Path(path).parent))
        self.load_project(project)
        return True

    def load_project(self, project: Project) -> None:
        self.project = project
        self._selection = []
        self.undo_stack.clear()
        self.filmstrip.set_project(project)
        self.viewport.set_project(project)
        self.viewport.set_crop(project.crop, tuple(project.capture_size or (0, 0)))
        self.app_settings.add_recent_project(str(project.path))
        self._rebuild_recent_menu()
        self._update_onion()
        self._update_actions()
        self._update_title()

    def _save_project(self) -> None:
        if self.project is None:
            return
        try:
            self.project.save()
            self.dirty = False
        except Exception as exc:  # pragma: no cover - filesystem errors
            self._set_status(f"Could not save project: {exc}")

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        recents = self.app_settings.recent_projects()
        if not recents:
            action = self.recent_menu.addAction("(nothing yet)")
            action.setEnabled(False)
            return
        for path in recents:
            action = self.recent_menu.addAction(path)
            action.triggered.connect(lambda _c=False, p=path: self.open_project_path(p))
        self.recent_menu.addSeparator()
        clear = self.recent_menu.addAction("Clear list")
        clear.triggered.connect(self.app_settings.clear_recent_projects)

    def purge_unreferenced(self) -> None:
        if not self._require_project():
            return
        answer = QMessageBox.question(
            self,
            "Purge unreferenced frames",
            "Delete every JPEG in the project folder that the manifest no longer "
            "references?\n\nThis is irreversible and cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self.project.purge_unreferenced()
        except Exception as exc:
            QMessageBox.critical(self, "Purge failed", str(exc))
            return
        QMessageBox.information(
            self, "Purge complete", f"Removed {len(removed)} unreferenced file(s)."
        )

    def export_movie(self) -> None:
        if not self._require_project():
            return
        if not self.project.visible_frames():
            QMessageBox.information(self, "Nothing to export", "There are no visible frames.")
            return
        dialog = ExportDialog(
            self.project,
            self,
            default_resolution=self.app_settings.export_resolution(),
            default_quality=self.app_settings.export_quality(),
            default_dir=self.app_settings.export_dir(),
        )
        dialog.exec()
        self.app_settings.set_export_resolution(dialog.resolution_combo.currentText())
        self.app_settings.set_export_quality(dialog.quality_combo.currentText())
        try:
            self.app_settings.set_export_dir(str(Path(dialog.path_edit.text()).parent))
        except Exception:
            pass

    # ----------------------------------------------------------- capture

    def capture_frame(self) -> None:
        if self.project is None:
            self._require_project()
            return
        if not self._connected:
            self._set_status("Cannot capture: phone is not connected")
            return
        if self._pending_capture is not None:
            return
        try:
            request_id = self.conn.request_capture()
        except Exception as exc:
            self._set_status(f"Capture failed: {exc}")
            return
        self._pending_capture = str(request_id)
        self.transport.set_capture_state(False, "CAPTURING…")
        self._capture_timer.start(CAPTURE_TIMEOUT_MS)

    def _on_capture_timeout(self) -> None:
        self._pending_capture = None
        self._set_status("Capture timed out — the phone did not answer")
        self._update_actions()

    @Slot(dict, bytes)
    def _on_capture_result(self, header: dict, jpeg: bytes) -> None:
        self._capture_timer.stop()
        self._pending_capture = None
        if self.project is None:
            self._set_status("Capture arrived but there is no open project — discarded")
            self._update_actions()
            return
        width = int(header.get("w") or 0)
        height = int(header.get("h") or 0)
        settings = header.get("settings") or {}
        if not isinstance(settings, dict):
            settings = {}
        before = {f.id for f in self.project.frames}
        self.undo_stack.push(CaptureFrame(self, jpeg, settings, (width, height)))
        new_ids = [f.id for f in self.project.frames if f.id not in before]
        self._reconcile_crop(width, height)
        if new_ids:
            image = QImage()
            if image.loadFromData(jpeg):
                self.viewport.cache_still(new_ids[-1], image)
            self.filmstrip.invalidate_thumbnail(new_ids[-1])
            self.filmstrip.ensure_visible(new_ids[-1])
        self._set_status(f"Captured {new_ids[-1] if new_ids else 'frame'} ({width}×{height})")
        self._update_actions()

    def _reconcile_crop(self, width: int, height: int) -> None:
        """The real capture size is only known once a still arrives."""
        project = self.project
        if project is None or not width or not height:
            return
        if tuple(project.capture_size or ()) == (width, height) and project.crop is not None:
            crop = project.crop
            if crop.x + crop.w <= width and crop.y + crop.h <= height:
                return
        if project.crop is not None:
            project.crop = Crop.for_aspect(project.crop.aspect, width, height)
        project.capture_size = (width, height)
        self.viewport.set_crop(project.crop, (width, height))
        self._save_project()

    # -------------------------------------------------------- connection

    def start(self) -> None:
        """Begin talking to the phone (or, in fake mode, to the simulator)."""
        if self._fake:
            self._set_status(f"Fake phone mode — connecting to 127.0.0.1:{self._port}")
            self.conn.start()
        else:
            self.device.start()
        self._reconnect_timer.start(RECONNECT_MS)

    @Slot(str)
    def _on_device_attached(self, serial: str) -> None:
        self._device_serial = serial
        self._set_status(f"Device {serial} attached — setting up the tunnel")
        try:
            self.device.setup_forward()
        except Exception as exc:
            self._set_status(f"adb forward failed: {exc}")
        if not self._connected:
            self.conn.start()

    @Slot()
    def _on_device_detached(self) -> None:
        self._device_serial = None
        self.conn.stop()
        self._connected = False
        self.banner.show_message("No Android device found — plug the phone in over USB.")
        self.viewport.clear_preview("No device")
        self._update_actions()

    @Slot()
    def _on_connected(self) -> None:
        self._connected = True
        self._connect_failures = 0
        self.banner.setVisible(False)
        self._set_status("Connected — waiting for HELLO")
        self._update_actions()

    @Slot(str)
    def _on_disconnected(self, reason: str) -> None:
        was = self._connected
        self._connected = False
        self._connect_failures += 1
        can_launch = bool(self._device_serial) and self._connect_failures >= 2
        text = f"Phone disconnected: {reason}" if was else f"Not connected: {reason}"
        if can_launch:
            text += " — is the phone app running?"
        self.banner.show_message(text, can_launch=can_launch)
        self.viewport.clear_preview("No preview — phone disconnected")
        self._set_status(text)
        self._update_actions()

    def _try_reconnect(self) -> None:
        if self._connected:
            return
        if not self._fake and self._device_serial is None:
            return
        if not self._fake:
            try:
                self.device.setup_forward()
            except Exception:
                pass
        try:
            self.conn.start()
        except Exception as exc:
            self._set_status(f"Reconnect failed: {exc}")

    def _launch_phone_app(self) -> None:
        self._set_status("Launching the phone app…")
        try:
            ok = self.device.launch_app()
        except Exception as exc:
            self._set_status(f"Could not launch the phone app: {exc}")
            return
        self._set_status(
            "Phone app launched — reconnecting" if ok else "Could not launch the phone app"
        )
        QTimer.singleShot(1500, self._try_reconnect)

    @Slot(dict)
    def _on_hello(self, hello: dict) -> None:
        try:
            caps = CameraCapabilities.from_hello(hello)
        except Exception as exc:
            self._set_status(f"Unusable HELLO from phone: {exc}")
            return
        self.caps = caps
        stored = self.app_settings.camera_settings()
        settings: CameraSettings | None = None
        if stored:
            try:
                settings = CameraSettings.from_json(stored).clamped(caps)
            except Exception:
                settings = None
        if settings is None:
            settings = CameraSettings.defaults(caps)
        self.controls.set_capabilities(caps, settings)
        self.cam_settings = self.controls.settings()
        # the desktop is the source of truth: push our settings, every connect
        self._push_settings()
        self.conn.set_preview(True)
        self._set_status(
            f"Connected — {caps.hardware_level}, sensor "
            f"{caps.sensor_size[0]}×{caps.sensor_size[1]}"
        )
        self._update_actions()

    def _push_settings(self) -> None:
        if self.cam_settings is None:
            return
        try:
            self.conn.send_config(self.cam_settings)
        except Exception as exc:
            self._set_status(f"Could not send camera settings: {exc}")

    @Slot(object)
    def _on_settings_changed(self, settings: CameraSettings) -> None:
        self.cam_settings = settings
        try:
            self.app_settings.set_camera_settings(settings.to_json())
        except Exception:
            pass
        if self._connected:
            self._push_settings()

    @Slot(dict)
    def _on_config_ack(self, applied: dict) -> None:
        self.controls.apply_ack(applied)
        updated = self.controls.settings()
        if updated is not None:
            self.cam_settings = updated

    @Slot(dict)
    def _on_error(self, error: dict) -> None:
        code = error.get("code", "ERROR")
        message = error.get("message", "")
        self._last_error = f"{code}: {message}"
        if error.get("request_id") and error.get("request_id") == self._pending_capture:
            self._capture_timer.stop()
            self._pending_capture = None
        self._set_status(f"Phone error — {self._last_error}")
        self._update_actions()

    @Slot(QImage, int)
    def _on_preview_frame(self, image: QImage, seq: int) -> None:
        self._frame_times.append(time.monotonic())
        if not image.isNull():
            self._frame_bytes += image.width() * image.height() * JPEG_BYTES_PER_PIXEL
        self.viewport.set_preview_image(image)

    # -------------------------------------------------------- view state

    def set_onion_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.viewport.set_onion_enabled(enabled)
        self.app_settings.set_onion_enabled(enabled)
        if self.act_onion.isChecked() != enabled:
            self.act_onion.setChecked(enabled)
        self.transport.set_onion(
            enabled, self.viewport.onion_depth(), self.viewport.onion_opacity()
        )

    def set_onion_depth(self, depth: int) -> None:
        depth = max(1, min(3, int(depth)))
        self.viewport.set_onion_depth(depth)
        self.app_settings.set_onion_depth(depth)
        if depth in self._depth_actions:
            self._depth_actions[depth].setChecked(True)
        self.transport.set_onion(
            self.viewport.onion_enabled(), depth, self.viewport.onion_opacity()
        )
        self._update_onion()

    def set_onion_opacity(self, opacity: float) -> None:
        opacity = min(1.0, max(0.0, float(opacity)))
        self.viewport.set_onion_opacity(opacity)
        self.app_settings.set_onion_opacity(opacity)
        self.transport.set_onion(
            self.viewport.onion_enabled(), self.viewport.onion_depth(), opacity
        )

    def set_crop_guides(self, show: bool) -> None:
        self.viewport.set_crop_guides(bool(show))
        self.app_settings.set_crop_guides(bool(show))

    def _on_loop_changed(self, loop: bool) -> None:
        self.viewport.set_loop(bool(loop))
        self.app_settings.set_loop_playback(bool(loop))

    def _on_mode_changed(self, mode: str) -> None:
        self.transport.set_mode(mode)
        if mode == REVIEW:
            selection = self.selection()
            if len(selection) == 1:
                self.viewport.seek_to_frame(selection[0])
        else:
            self.filmstrip.set_playhead("")
        self._update_actions()

    def _on_playhead(self, frame_id: str) -> None:
        self.filmstrip.set_playhead(frame_id)
        if frame_id:
            self.filmstrip.ensure_visible(frame_id)

    def _on_frame_activated(self, frame_id: str) -> None:
        self.viewport.set_mode(REVIEW)
        self.viewport.seek_to_frame(frame_id)

    def _on_filmstrip_selection(self, ids: list[str]) -> None:
        self._selection = list(ids)
        self._update_onion()
        self._update_actions()
        if self.viewport.mode() == REVIEW and len(ids) == 1:
            self.viewport.seek_to_frame(ids[0])

    # ---------------------------------------------------------- onion skin

    def onion_frame_ids(self) -> list[str]:
        """Nearest first: the previous 1..3 visible stills."""
        if self.project is None:
            return []
        frames = self.project.frames
        if not frames:
            return []
        anchor: int | None = None
        selection = self.selection()
        if len(selection) == 1:
            try:
                anchor = self.project.index_of(selection[0])
            except Exception:
                anchor = None
            if anchor is not None and anchor < 0:
                anchor = None
        if anchor is None:
            visible = [i for i, f in enumerate(frames) if not f.hidden]
            if not visible:
                return []
            anchor = visible[-1]
        prior = [f.id for i, f in enumerate(frames) if i <= anchor and not f.hidden]
        return list(reversed(prior[-3:]))

    def _update_onion(self) -> None:
        self.viewport.set_onion_frames(self.onion_frame_ids())

    def _load_still(self, frame_id: str) -> QImage | None:
        if self.project is None:
            return None
        for frame in self.project.frames:
            if frame.id == frame_id:
                try:
                    path = self.project.frame_path(frame)
                except Exception:
                    return None
                image = QImage(str(path))
                return None if image.isNull() else image
        return None

    # -------------------------------------------------------------- status

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _update_perf(self) -> None:
        now = time.monotonic()
        elapsed = max(1e-6, now - self._perf_mark)
        self._perf_mark = now

        recent = [t for t in self._frame_times if now - t <= 2.0]
        fps = len(recent) / 2.0 if len(recent) > 1 else 0.0

        counter = getattr(self.conn, "bytes_received", None)
        if isinstance(counter, int):
            per_second = max(0, counter - self._last_counter) / elapsed
            self._last_counter = counter
            prefix = ""
        else:
            per_second = self._frame_bytes / elapsed
            prefix = "≈"
        self._frame_bytes = 0.0

        if not self._connected:
            self.perf_label.setText("offline")
            return
        self.perf_label.setText(
            f"{fps:4.1f} fps  ·  {prefix}{per_second / 1_000_000:.2f} MB/s"
        )

    def _update_title(self) -> None:
        if self.project is None:
            self.setWindowTitle("stopmotion — no project")
            return
        total = len(self.project.frames)
        visible = len(self.project.visible_frames())
        try:
            seconds = self.project.duration_ms(True) / 1000.0
            all_seconds = self.project.duration_ms(False) / 1000.0
        except Exception:
            seconds = all_seconds = 0.0
        self.setWindowTitle(
            f"{self.project.name} — {visible}/{total} frames, "
            f"{seconds:.1f} s visible ({all_seconds:.1f} s total) @ {self.project.fps} fps "
            "— stopmotion"
        )
        self.transport.set_info(
            f"{visible} visible / {total} frames · {seconds:.1f} s @ {self.project.fps} fps"
        )

    def _update_actions(self) -> None:
        has_project = self.project is not None
        has_selection = bool(self._selection)
        for action in (self.act_export, self.act_purge, self.act_select_all):
            action.setEnabled(has_project)
        for action in (self.act_delete, self.act_hide, self.act_duplicate, self.act_duration):
            action.setEnabled(has_project and has_selection)
        can_capture = has_project and self._connected and self._pending_capture is None
        self.act_capture.setEnabled(can_capture)
        if self._pending_capture is not None:
            self.transport.set_capture_state(False, "CAPTURING…")
        elif not self._connected:
            self.transport.set_capture_state(False, "DISCONNECTED")
        elif not has_project:
            self.transport.set_capture_state(False, "NO PROJECT")
        else:
            self.transport.set_capture_state(True, "CAPTURE")

    # --------------------------------------------------------------- close

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt)
        self._perf_timer.stop()
        self._reconnect_timer.stop()
        self.app_settings.save_window_state(self.saveGeometry(), self.saveState())
        self._save_project()
        try:
            self.conn.stop()
        except Exception:
            pass
        try:
            self.device.stop()
        except Exception:
            pass
        self.app_settings.sync()
        super().closeEvent(event)
