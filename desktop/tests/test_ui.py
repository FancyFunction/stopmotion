"""Headless smoke tests for the UI stream.

Run with::

    QT_QPA_PLATFORM=offscreen desktop/.venv/bin/python -m pytest desktop/tests/test_ui.py -q

pytest-qt is deliberately not used (it is not installed and must not be added):
a plain session-scoped QApplication fixture is enough for everything here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_DESKTOP = Path(__file__).resolve().parents[1]
if str(_DESKTOP) not in sys.path:
    sys.path.insert(0, str(_DESKTOP))

from PySide6.QtCore import QBuffer, QIODevice, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from stopmotion.camera import CameraCapabilities, CameraSettings  # noqa: E402
from stopmotion.project import Crop, Project  # noqa: E402
from stopmotion.ui.filmstrip import Filmstrip  # noqa: E402
from stopmotion.ui.main_window import MainWindow  # noqa: E402
from stopmotion.ui.new_project_dialog import NewProjectDialog, slugify  # noqa: E402
from stopmotion.ui.viewport import LIVE, REVIEW, Viewport  # noqa: E402

NOMOD = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
CTRL = Qt.KeyboardModifier.ControlModifier

HELLO = {
    "hardware_level": "LEVEL_3",
    "sensor": {"width": 4032, "height": 3024},
    "preview_sizes": [[1920, 1080], [1280, 720], [640, 360]],
    "exposure_ns": {"min": 100000, "max": 200000000},
    "iso": {"min": 50, "max": 3200},
    "focus_diopters": {"min": 0.0, "max": 10.0},
    "focus_calibration": "UNCALIBRATED",
    "awb_modes": ["AUTO", "DAYLIGHT", "CLOUDY_DAYLIGHT", "INCANDESCENT"],
}


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def qapp(tmp_path_factory):
    """One QApplication for the whole session, with throwaway QSettings."""
    settings_dir = tmp_path_factory.mktemp("qsettings")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(settings_dir)
    )
    app = QApplication.instance() or QApplication([sys.argv[0] if sys.argv else "test"])
    yield app
    app.processEvents()


def jpeg_bytes(width: int = 160, height: int = 90, color: str = "#336699") -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "JPG", 85)
    return bytes(buffer.data())


def make_project(path: Path, frames: int = 5, fps: int = 12) -> Project:
    project = Project.create(
        path, "test-film", fps, (4032, 3024), Crop.for_aspect("16:9", 4032, 3024)
    )
    palette = ["#802020", "#208020", "#202080", "#808020", "#802080", "#208080"]
    for index in range(frames):
        project.add_frame(
            jpeg_bytes(color=palette[index % len(palette)]),
            {"iso": 100, "exposure_ns": 8000000},
            (4032, 3024),
        )
    return project


@pytest.fixture
def window(qapp, tmp_path):
    win = MainWindow(fake=True, port=18099)  # never started: no sockets in tests
    yield win
    win.conn.stop()
    win.close()
    win.deleteLater()
    qapp.processEvents()


@pytest.fixture
def loaded(window, tmp_path):
    project = make_project(tmp_path / "proj", frames=5)
    window.load_project(project)
    return window, project


def ids_of(project: Project) -> list[str]:
    return [f.id for f in project.frames]


# --------------------------------------------------------------------------- #
# construction
# --------------------------------------------------------------------------- #

def test_window_constructs(window):
    assert window.undo_stack.undoLimit() == 200
    assert window.project is None
    assert window.viewport.mode() == LIVE
    # capture must be impossible with no project and no connection
    assert not window.act_capture.isEnabled()
    assert not window.transport.capture_button.isEnabled()


def test_window_implements_timeline_context(window):
    from stopmotion.commands import TimelineContext

    assert isinstance(window, TimelineContext)
    assert window.selection() == []
    window.set_selection(["nope"])  # unknown ids are dropped, not crashed on
    assert window.selection() == []


def test_project_loads_into_filmstrip(loaded):
    window, project = loaded
    assert window.filmstrip.count() == 5
    assert window.filmstrip.project() is project
    assert "test-film" in window.windowTitle()
    assert "5/5 frames" in window.windowTitle()
    # a thumbnail actually renders from disk
    assert window.filmstrip._thumbnail(project.frames[0]) is not None


# --------------------------------------------------------------------------- #
# selection semantics
# --------------------------------------------------------------------------- #

def test_plain_click_selects_only_that_frame(loaded):
    window, project = loaded
    strip: Filmstrip = window.filmstrip
    ids = ids_of(project)

    strip.handle_click(2, NOMOD)
    assert strip.selection() == [ids[2]]
    strip.handle_click(4, NOMOD)
    assert strip.selection() == [ids[4]]
    assert window.selection() == [ids[4]]


def test_shift_click_extends_a_range(loaded):
    window, project = loaded
    strip = window.filmstrip
    ids = ids_of(project)

    strip.handle_click(1, NOMOD)
    strip.handle_click(3, SHIFT)
    assert strip.selection() == ids[1:4]

    # backwards from the same anchor
    strip.handle_click(0, SHIFT)
    assert strip.selection() == ids[0:2]


def test_ctrl_click_toggles_individual_frames(loaded):
    window, project = loaded
    strip = window.filmstrip
    ids = ids_of(project)

    strip.handle_click(0, NOMOD)
    strip.handle_click(2, CTRL)
    strip.handle_click(4, CTRL)
    assert strip.selection() == [ids[0], ids[2], ids[4]]

    strip.handle_click(2, CTRL)  # toggles back out
    assert strip.selection() == [ids[0], ids[4]]

    strip.handle_click(3, NOMOD)  # plain click collapses again
    assert strip.selection() == [ids[3]]


def test_selection_round_trips_through_the_context(loaded):
    window, project = loaded
    ids = ids_of(project)
    window.set_selection([ids[3], ids[1]])
    # normalised into project order
    assert window.selection() == [ids[1], ids[3]]
    assert window.filmstrip.selection() == [ids[1], ids[3]]


# --------------------------------------------------------------------------- #
# commands and undo
# --------------------------------------------------------------------------- #

def test_delete_goes_through_the_undo_stack_and_restores_selection(loaded):
    window, project = loaded
    ids = ids_of(project)
    window.filmstrip.handle_click(1, NOMOD)
    window.filmstrip.handle_click(2, SHIFT)
    assert window.selection() == [ids[1], ids[2]]

    window.delete_frames(window.selection())
    assert window.undo_stack.count() == 1
    assert ids_of(project) == [ids[0], ids[3], ids[4]]
    assert window.filmstrip.count() == 3

    window.undo_stack.undo()
    assert ids_of(project) == ids
    assert window.selection() == [ids[1], ids[2]]  # selection came back too
    assert window.filmstrip.selection() == [ids[1], ids[2]]

    window.undo_stack.redo()
    assert ids_of(project) == [ids[0], ids[3], ids[4]]


def test_hide_duplicate_and_duration_are_undoable(loaded):
    window, project = loaded
    ids = ids_of(project)

    window.set_selection([ids[0]])
    window.set_hidden([ids[0]], True)
    assert project.frames[0].hidden is True
    assert len(project.visible_frames()) == 4

    window.set_duration_ms([ids[1]], 250)  # 250 ms at 12 fps -> 3 holds
    assert project.frame_by_id(ids[1]).holds == 3

    window.duplicate_frames([ids[2]])
    assert len(project.frames) == 6

    for _ in range(3):
        window.undo_stack.undo()
    assert len(project.frames) == 5
    assert project.frames[0].hidden is False
    assert project.frame_by_id(ids[1]).holds == 1


def test_reorder_goes_through_a_command(loaded):
    window, project = loaded
    ids = ids_of(project)
    window.reorder_frames([ids[4]], 0)
    assert ids_of(project)[0] == ids[4]
    window.undo_stack.undo()
    assert ids_of(project) == ids


def test_capture_result_appends_a_frame(loaded):
    window, project = loaded
    before = len(project.frames)
    window._pending_capture = "c0001"
    window._on_capture_result(
        {"request_id": "c0001", "w": 4032, "h": 3024, "settings": {"iso": 100}},
        jpeg_bytes(320, 240, "#123456"),
    )
    assert len(project.frames) == before + 1
    assert window.undo_stack.count() == 1
    new_id = project.frames[-1].id
    assert project.frame_path(project.frames[-1]).exists()
    # the still was cached for the onion skin on arrival
    assert new_id in window.viewport.cached_still_ids()
    window.undo_stack.undo()
    assert len(project.frames) == before


# --------------------------------------------------------------------------- #
# viewport: modes, onion skin, crop
# --------------------------------------------------------------------------- #

def test_mode_toggle(loaded):
    window, _project = loaded
    assert window.viewport.mode() == LIVE
    window.viewport.toggle_mode()
    assert window.viewport.mode() == REVIEW
    assert "REVIEW" in window.transport.mode_button.text()
    window.act_mode.trigger()
    assert window.viewport.mode() == LIVE
    assert "LIVE" in window.transport.mode_button.text()


def test_onion_follows_the_selection(loaded):
    window, project = loaded
    ids = ids_of(project)
    window.set_onion_depth(3)

    # nothing selected -> last visible frame first
    window.set_selection([])
    assert window.onion_frame_ids() == [ids[4], ids[3], ids[2]]

    # exactly one selected -> that frame first
    window.set_selection([ids[2]])
    assert window.onion_frame_ids() == [ids[2], ids[1], ids[0]]

    # multi selection -> back to the last visible frame
    window.set_selection([ids[0], ids[1]])
    assert window.onion_frame_ids() == [ids[4], ids[3], ids[2]]

    # hidden frames are skipped
    window.set_hidden([ids[4], ids[3]], True)
    window.set_selection([])
    assert window.onion_frame_ids() == [ids[2], ids[1], ids[0]]


def test_onion_depth_limits_what_is_painted(loaded):
    window, project = loaded
    ids = ids_of(project)
    window.set_selection([])
    window.set_onion_depth(1)
    assert window.viewport.onion_depth() == 1
    assert window.viewport.onion_frames()[:1] == [ids[4]]
    window.set_onion_opacity(0.8)
    assert window.viewport.onion_opacity() == pytest.approx(0.8)
    assert window.transport.opacity_slider.value() == 80


def test_review_playback_honours_holds_and_skips_hidden(loaded):
    window, project = loaded
    ids = ids_of(project)
    window.set_hidden([ids[1]], True)
    window.set_duration_ms([ids[0]], 250)  # 3 holds at 12 fps

    view = window.viewport
    view.set_mode(REVIEW)
    view._rebuild_ticks()
    assert view._ticks == [ids[0]] * 3 + [ids[2], ids[3], ids[4]]

    view.seek_to_frame(ids[0])
    assert view.current_frame_id() == ids[0]
    view.step(1)
    assert view.current_frame_id() == ids[2]  # hidden frame skipped
    view.step(-1)
    assert view.current_frame_id() == ids[0]


def test_review_renders_a_still(loaded):
    window, project = loaded
    window.viewport.set_mode(REVIEW)
    window.viewport.seek_to_frame(project.frames[0].id)
    window.viewport.resize(640, 360)
    assert window.viewport._review_image() is not None
    window.viewport.grab()  # exercise paintEvent


def test_live_paint_with_preview_and_onion(loaded):
    window, project = loaded
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(QColor("#101010"))
    window._on_preview_frame(image, 1)
    window.viewport.resize(640, 360)
    window.viewport.grab()  # must not raise with onion + crop overlay active
    assert window.viewport.image_rect().width() > 0
    crop = window.viewport.crop_rect_on(window.viewport.image_rect())
    assert crop.width() > 0 and crop.height() > 0
    assert crop.height() <= window.viewport.image_rect().height()


def test_crop_guides_toggle(loaded):
    window, _ = loaded
    window.act_crop.setChecked(False)
    assert window.viewport.crop_guides() is False
    window.act_crop.setChecked(True)
    assert window.viewport.crop_guides() is True


# --------------------------------------------------------------------------- #
# camera controls built from HELLO
# --------------------------------------------------------------------------- #

def test_controls_are_built_from_the_capabilities(window):
    window._on_hello(HELLO)
    caps = window.caps
    assert isinstance(caps, CameraCapabilities)
    panel = window.controls

    assert panel.iso_slider.minimum() == int(caps.iso.lo)
    assert panel.iso_slider.maximum() == int(caps.iso.hi)
    assert panel.awb_combo.count() == len(caps.awb_modes)
    assert panel.preview_size_combo.count() == len(caps.preview_sizes)
    # UNCALIBRATED -> the focus slider must not claim metres or diopters
    assert "relative" in panel._focus_value.text().lower()
    # LOCK ALL is on by default
    assert panel.lock_all.isChecked()
    assert panel.settings().ae_lock and panel.settings().af_lock and panel.settings().awb_lock


def test_lock_all_drives_the_individual_locks(window):
    window._on_hello(HELLO)
    panel = window.controls
    panel.lock_all.setChecked(False)
    assert not panel.ae_lock.isChecked()
    assert not panel.settings().awb_lock
    panel.ae_lock.setChecked(True)
    assert not panel.lock_all.isChecked()  # only one of three
    panel.af_lock.setChecked(True)
    panel.awb_lock.setChecked(True)
    assert panel.lock_all.isChecked()


def test_config_ack_shows_what_was_actually_applied(window):
    window._on_hello(HELLO)
    panel = window.controls
    panel.iso_slider.setValue(3000)
    assert panel.settings().iso == 3000

    applied = dict(panel.settings().to_json())
    applied["iso"] = 800
    window._on_config_ack(applied)

    assert panel.settings().iso == 800
    assert panel.iso_slider.value() == 800
    assert "800" in panel.ack_label.text()
    assert window.cam_settings.iso == 800


def test_settings_survive_a_reconnect(window):
    window._on_hello(HELLO)
    window.controls.iso_slider.setValue(400)
    assert window.app_settings.camera_settings()["iso"] == 400
    # a fresh HELLO must restore our value, not the device default
    window._on_hello(HELLO)
    assert window.controls.settings().iso == 400


# --------------------------------------------------------------------------- #
# connection state
# --------------------------------------------------------------------------- #

def test_capture_is_disabled_while_disconnected(loaded):
    window, _ = loaded
    window._on_disconnected("socket closed")
    # the window itself is never shown in the tests, so ask about the widget's
    # own hidden flag rather than effective visibility
    assert not window.banner.isHidden()
    assert not window.transport.capture_button.isEnabled()
    window.capture_frame()  # must be a no-op, not an exception
    assert window._pending_capture is None

    window._on_connected()
    window._on_hello(HELLO)
    assert window.banner.isHidden()
    assert window.transport.capture_button.isEnabled()


def test_capture_timeout_clears_the_in_flight_state(loaded):
    window, _ = loaded
    window._on_connected()
    window._pending_capture = "c0009"
    window._on_capture_timeout()
    assert window._pending_capture is None
    assert "timed out" in window.status_label.text()


def test_phone_error_is_surfaced(loaded):
    window, _ = loaded
    window._on_connected()
    window._pending_capture = "c0009"
    window._on_error({"request_id": "c0009", "code": "CAPTURE_FAILED", "message": "busy"})
    assert window._pending_capture is None
    assert "CAPTURE_FAILED" in window.status_label.text()


# --------------------------------------------------------------------------- #
# dialogs
# --------------------------------------------------------------------------- #

def test_new_project_dialog_values(qapp, tmp_path):
    dialog = NewProjectDialog(None, default_dir=str(tmp_path))
    assert dialog.fps_spin.value() == 12
    assert dialog.aspect_combo.currentText() == "16:9"
    dialog.name_edit.setText("My Film!")
    values = dialog.values()
    assert values["path"] == tmp_path / "My-Film"
    assert values["fps"] == 12
    assert values["aspect"] == "16:9"
    dialog.aspect_combo.setCurrentText("No crop")
    assert dialog.values()["aspect"] is None
    dialog.deleteLater()


def test_slugify():
    assert slugify("  Hello World  ") == "Hello-World"
    assert slugify("///") == "untitled"


def test_export_dialog_summary(loaded, tmp_path):
    from stopmotion.ui.export_dialog import ExportDialog

    window, project = loaded
    window.set_duration_ms([project.frames[0].id], 250)  # 3 holds
    window.set_hidden([project.frames[1].id], True)

    dialog = ExportDialog(project, None, default_dir=str(tmp_path))
    # 4 visible frames: 3 + 1 + 1 + 1 = 6 holds -> 0.5 s at 12 fps
    assert dialog.summary.text() == "4 visible frames, 0.5 s at 12 fps"
    assert dialog.resolution_combo.currentText() == "1080p"
    settings = dialog.settings()
    assert (settings.width, settings.height) == (1920, 1080)
    assert settings.fps == 12
    dialog.deleteLater()


# --------------------------------------------------------------------------- #
# standalone widgets
# --------------------------------------------------------------------------- #

def test_viewport_standalone_is_safe_without_a_project(qapp):
    view = Viewport()
    view.resize(320, 240)
    view.grab()
    view.set_mode(REVIEW)
    view.grab()
    view.play()  # nothing to play; must not spin a timer
    assert not view.is_playing()
    view.deleteLater()


def test_filmstrip_standalone_is_safe_without_a_project(qapp):
    strip = Filmstrip()
    strip.resize(400, 140)
    strip.grab()
    strip.handle_click(0, NOMOD)  # out of range, no crash
    assert strip.selection() == []
    strip.deleteLater()
