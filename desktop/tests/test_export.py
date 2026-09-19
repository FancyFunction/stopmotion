"""Export tests.

The centrepiece is a real end-to-end ffmpeg run over generated JPEGs whose
output is measured with ffprobe.  That is the only way to prove the timing
model -- concat durations, the repeated final entry and the frame cap -- really
produces ``sum(holds)`` frames at the project fps.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from stopmotion.export import (  # noqa: E402
    ExportSettings,
    Exporter,
    build_command,
    build_concat_list,
    even,
)
from stopmotion.project import Crop, Project  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not on PATH")


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def make_jpeg(colour: str, w: int = 160, h: int = 120) -> bytes:
    """A real JPEG, made with ffmpeg's lavfi colour source."""
    out = subprocess.run(
        [FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={colour}:s={w}x{h}:d=1",
         "-frames:v", "1", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    assert out[:2] == b"\xff\xd8"
    return out


COLOURS = ["red", "green", "blue", "yellow", "magenta", "cyan"]


def build_project(tmp_path: Path, holds, fps: int = 12, hidden=(), crop=None,
                  size=(160, 120)) -> Project:
    project = Project.create(tmp_path / "film", "film", fps, size, crop)
    for i, h in enumerate(holds):
        frame = project.add_frame(make_jpeg(COLOURS[i % len(COLOURS)], *size), {}, size)
        frame.holds = h
        frame.hidden = i in hidden
    project.save()
    return project


def probe(path: Path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames,width,height,r_frame_rate,pix_fmt",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    stream = data["streams"][0]
    return {
        "frames": int(stream["nb_read_frames"]),
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "rate": stream["r_frame_rate"],
        "pix_fmt": stream["pix_fmt"],
        "duration": float(data["format"]["duration"]),
    }


def run_export(project: Project, settings: ExportSettings, timeout_ms: int = 60000,
               cancel_after_ms: int | None = None) -> tuple[bool, str, list[int]]:
    """Drive a real Exporter to completion on a live event loop."""
    exporter = Exporter()
    result: dict = {}
    progress: list[int] = []
    loop = QEventLoop()

    exporter.progress.connect(progress.append)

    def done(ok: bool, message: str) -> None:
        result["ok"], result["message"] = ok, message
        loop.quit()

    exporter.finished.connect(done)

    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(timeout_ms)

    if cancel_after_ms is not None:
        QTimer.singleShot(cancel_after_ms, exporter.cancel)

    exporter.start(project, settings)
    loop.exec()
    exporter.wait(10000)

    assert "ok" in result, "the exporter never emitted finished()"
    return result["ok"], result["message"], progress


# --------------------------------------------------------------------------- #
# list / command construction
# --------------------------------------------------------------------------- #

def test_even_clamps_down_and_floors_at_two():
    assert [even(v) for v in (1921, 1080, 1079, 1, 0, -4)] == [1920, 1080, 1078, 2, 2, 2]


@needs_ffmpeg
def test_concat_list_repeats_the_final_entry_without_a_duration(tmp_path):
    project = build_project(tmp_path, [1, 3, 2], fps=12)
    text, total = build_concat_list(project, 12)
    lines = [ln for ln in text.splitlines() if ln]

    assert lines[0] == "ffconcat version 1.0"
    files = [ln for ln in lines if ln.startswith("file ")]
    durations = [ln for ln in lines if ln.startswith("duration ")]

    assert len(files) == 4, "three frames plus the repeated final entry"
    assert files[-1] == files[-2], "the last file must be repeated"
    assert len(durations) == 3
    assert lines[-1].startswith("file "), "the repeated entry carries no duration"
    assert durations == ["duration 0.083333", "duration 0.250000", "duration 0.166667"]
    assert total == 6


@needs_ffmpeg
def test_concat_list_skips_hidden_frames(tmp_path):
    project = build_project(tmp_path, [1, 1, 1, 1], fps=12, hidden=(1, 2))
    text, total = build_concat_list(project, 12)
    assert total == 2
    assert len([ln for ln in text.splitlines() if ln.startswith("file ")]) == 3


@needs_ffmpeg
def test_concat_list_refuses_an_empty_or_all_hidden_timeline(tmp_path):
    project = build_project(tmp_path, [1, 1], fps=12, hidden=(0, 1))
    with pytest.raises(ValueError, match="no visible frames"):
        build_concat_list(project, 12)


@needs_ffmpeg
def test_concat_list_refuses_a_missing_jpeg(tmp_path):
    project = build_project(tmp_path, [1, 1], fps=12)
    project.frame_path(project.frames[0]).unlink()
    with pytest.raises(ValueError, match="missing"):
        build_concat_list(project, 12)


@needs_ffmpeg
def test_command_includes_crop_then_scale_and_even_dimensions(tmp_path):
    crop = Crop.for_aspect("16:9", 160, 120)
    project = build_project(tmp_path, [1], fps=12, crop=crop)
    cmd = build_command(project, ExportSettings(tmp_path / "o.mp4", 641, 361), Path("l.txt"))
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == f"crop={crop.w}:{crop.h}:{crop.x}:{crop.y},scale=640:360"
    assert "-pix_fmt" in cmd and cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert cmd[cmd.index("-fps_mode") + 1] == "cfr"
    assert cmd[cmd.index("-r") + 1] == "12"


@needs_ffmpeg
def test_command_omits_the_crop_filter_when_the_project_has_none(tmp_path):
    project = build_project(tmp_path, [1], fps=12, crop=None)
    cmd = build_command(project, ExportSettings(tmp_path / "o.mp4", 320, 240), Path("l.txt"))
    assert cmd[cmd.index("-vf") + 1] == "scale=320:240"


@needs_ffmpeg
def test_command_honours_an_fps_override(tmp_path):
    project = build_project(tmp_path, [1], fps=12)
    cmd = build_command(project, ExportSettings(tmp_path / "o.mp4", 320, 240, fps=24), Path("l"))
    assert cmd[cmd.index("-r") + 1] == "24"


# --------------------------------------------------------------------------- #
# end-to-end: the timing model, measured
# --------------------------------------------------------------------------- #

@needs_ffmpeg
@pytest.mark.parametrize(
    "holds,fps",
    [
        ([1, 1, 1, 1], 12),
        ([1, 3, 1, 2], 4),
        ([2, 2, 2, 1], 4),
        ([5, 1, 1, 1], 12),
        ([1], 12),
        ([4], 8),
        ([1, 1], 30),
        ([3, 1, 2, 1, 1, 4], 12),
    ],
)
def test_end_to_end_frame_count_and_duration_match_the_holds(tmp_path, holds, fps):
    project = build_project(tmp_path, holds, fps=fps)
    out = tmp_path / "out.mp4"
    ok, message, progress = run_export(
        project, ExportSettings(out, 160, 120, crf=23, preset="ultrafast")
    )

    assert ok, message
    assert out.exists() and out.stat().st_size > 0

    expected_frames = sum(holds)
    info = probe(out)
    assert info["frames"] == expected_frames, f"holds={holds} fps={fps}: {info}"
    assert info["duration"] == pytest.approx(expected_frames / fps, abs=1 / (2 * fps))
    assert info["rate"] == f"{fps}/1"
    assert info["pix_fmt"] == "yuv420p"
    assert progress and progress[-1] == 100


@needs_ffmpeg
def test_end_to_end_excludes_hidden_frames_from_the_render(tmp_path):
    # 6 frames, two hidden; visible holds are 2 + 1 + 3 + 1 = 7 at 7 fps = 1.0 s.
    project = build_project(tmp_path, [2, 9, 1, 3, 9, 1], fps=7, hidden=(1, 4))
    out = tmp_path / "out.mp4"
    ok, message, _ = run_export(project, ExportSettings(out, 160, 120, crf=23, preset="ultrafast"))

    assert ok, message
    info = probe(out)
    assert info["frames"] == 7, "hidden frames must not contribute any output frames"
    assert info["duration"] == pytest.approx(1.0, abs=0.08)
    assert project.duration_ms(visible_only=True) == 1000
    assert project.duration_ms(visible_only=False) == round(25 * 1000 / 7)


@needs_ffmpeg
def test_end_to_end_crop_and_scale_produce_the_requested_size(tmp_path):
    crop = Crop.for_aspect("16:9", 160, 120)
    assert (crop.w, crop.h, crop.x, crop.y) == (160, 90, 0, 14)
    project = build_project(tmp_path, [1, 2, 1], fps=12, crop=crop)
    out = tmp_path / "out.mp4"
    ok, message, _ = run_export(
        project, ExportSettings(out, 320, 180, crf=23, preset="ultrafast")
    )

    assert ok, message
    info = probe(out)
    assert (info["width"], info["height"]) == (320, 180)
    assert info["frames"] == 4


@needs_ffmpeg
def test_end_to_end_odd_output_size_is_rounded_to_even(tmp_path):
    project = build_project(tmp_path, [1, 1], fps=12)
    out = tmp_path / "out.mp4"
    ok, message, _ = run_export(project, ExportSettings(out, 161, 121, crf=23, preset="ultrafast"))
    assert ok, message
    info = probe(out)
    assert (info["width"], info["height"]) == (160, 120)


@needs_ffmpeg
def test_progress_is_monotonic_and_bounded(tmp_path):
    project = build_project(tmp_path, [1] * 24, fps=12)
    out = tmp_path / "out.mp4"
    ok, message, progress = run_export(
        project, ExportSettings(out, 160, 120, crf=23, preset="ultrafast")
    )
    assert ok, message
    assert progress == sorted(progress)
    assert all(0 <= p <= 100 for p in progress)
    assert progress[-1] == 100


# --------------------------------------------------------------------------- #
# failure paths
# --------------------------------------------------------------------------- #

@needs_ffmpeg
def test_missing_ffmpeg_reports_a_useful_error(tmp_path):
    project = build_project(tmp_path, [1, 1], fps=12)
    exporter = Exporter(ffmpeg="definitely-not-ffmpeg-xyz")
    result: dict = {}
    loop = QEventLoop()
    exporter.finished.connect(lambda ok, msg: (result.update(ok=ok, msg=msg), loop.quit()))
    QTimer.singleShot(20000, loop.quit)
    exporter.start(project, ExportSettings(tmp_path / "o.mp4", 160, 120))
    loop.exec()
    exporter.wait(5000)

    assert result["ok"] is False
    assert "ffmpeg not found" in result["msg"]
    assert "definitely-not-ffmpeg-xyz" in result["msg"]


@needs_ffmpeg
def test_ffmpeg_failure_includes_the_stderr_tail(tmp_path):
    project = build_project(tmp_path, [1, 1], fps=12)
    out = tmp_path / "out.mp4"
    ok, message, _ = run_export(
        project, ExportSettings(out, 160, 120, crf=23, preset="no-such-preset")
    )
    assert ok is False
    assert "ffmpeg failed" in message
    assert len(message.splitlines()) > 1, "the stderr tail should be attached"
    assert "preset" in message.lower()


@needs_ffmpeg
def test_export_with_nothing_visible_fails_cleanly(tmp_path):
    project = build_project(tmp_path, [1, 1], fps=12, hidden=(0, 1))
    ok, message, _ = run_export(project, ExportSettings(tmp_path / "o.mp4", 160, 120))
    assert ok is False
    assert "no visible frames" in message


@needs_ffmpeg
def test_cancel_stops_the_run_and_reports_cancelled(tmp_path):
    # Long and slow enough that the cancel lands mid-encode.
    project = build_project(tmp_path, [1] * 40, fps=12, size=(640, 480))
    out = tmp_path / "out.mp4"
    ok, message, _ = run_export(
        project,
        ExportSettings(out, 1920, 1080, crf=18, preset="veryslow"),
        cancel_after_ms=250,
    )
    assert ok is False
    assert message == "cancelled"


@needs_ffmpeg
def test_exporter_is_reusable_after_a_run(tmp_path):
    project = build_project(tmp_path, [1, 2], fps=12)
    settings_a = ExportSettings(tmp_path / "a.mp4", 160, 120, crf=23, preset="ultrafast")
    settings_b = ExportSettings(tmp_path / "b.mp4", 160, 120, crf=23, preset="ultrafast")
    assert run_export(project, settings_a)[0]
    assert run_export(project, settings_b)[0]
    assert probe(tmp_path / "a.mp4")["frames"] == probe(tmp_path / "b.mp4")["frames"] == 3
