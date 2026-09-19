"""FFmpeg export: visible frames -> H.264/MP4, honouring per-frame hold counts.

Timing model
------------
The concat demuxer is fed a list where every entry carries an explicit
``duration holds/fps``.  The demuxer ignores the duration of the *last* entry,
so the last file is written twice -- once with its duration, once bare.  Get
that wrong and the final frame is dropped from the render entirely (measured:
without the repeat, ffmpeg 6.1 drops the whole last entry).

The repeat itself then overshoots by one or two frames at the tail, and by how
many depends on fps and the last hold count, so the output is additionally
pinned to exactly ``sum(holds)`` frames with ``-frames:v``.  The result is
``sum(holds)`` frames over ``sum(holds)/fps`` seconds, exactly, which is what
``test_export.py`` asserts with ffprobe.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .project import Project

__all__ = ["ExportSettings", "Exporter", "build_concat_list", "build_command", "even"]

_FRAME_RE = re.compile(rb"frame=\s*(\d+)")
_STDERR_TAIL_LINES = 40


def even(value: int) -> int:
    """Largest even int <= value, floored at 2.  libx264 + yuv420p needs even dims."""
    value = int(value)
    if value < 2:
        return 2
    return value - (value % 2)


@dataclass
class ExportSettings:
    out_path: Path
    width: int
    height: int
    crf: int = 18
    preset: str = "medium"
    fps: int | None = None


def _quote(path: str) -> str:
    """Quote a path for the concat demuxer's ``file`` directive."""
    return "'" + str(path).replace("'", r"'\''") + "'"


def build_concat_list(project: Project, fps: int) -> tuple[str, int]:
    """Return ``(list_text, total_output_frames)`` for the visible frames.

    Raises :class:`ValueError` if there is nothing to export or a JPEG is gone.
    """
    visible = project.visible_frames()
    if not visible:
        raise ValueError("nothing to export: the project has no visible frames")
    if fps <= 0:
        raise ValueError(f"invalid fps: {fps}")

    missing = [f.id for f in visible if not project.frame_path(f).exists()]
    if missing:
        raise ValueError(
            "cannot export, these frames' image files are missing: " + ", ".join(missing[:8])
            + (" ..." if len(missing) > 8 else "")
        )

    lines = ["ffconcat version 1.0"]
    total_holds = 0
    for frame in visible:
        holds = max(1, int(frame.holds))
        total_holds += holds
        lines.append(f"file {_quote(project.frame_path(frame).resolve())}")
        lines.append(f"duration {holds / fps:.6f}")
    # The demuxer discards the last entry's duration, so repeat the final file
    # bare.  Without this the last frame never makes it into the output.
    lines.append(f"file {_quote(project.frame_path(visible[-1]).resolve())}")
    return "\n".join(lines) + "\n", total_holds


def build_command(
    project: Project,
    settings: ExportSettings,
    list_path: Path,
    ffmpeg: str = "ffmpeg",
    total_frames: int | None = None,
) -> list[str]:
    """The exact ffmpeg argv used for an export."""
    fps = int(settings.fps or project.fps)
    w, h = even(settings.width), even(settings.height)

    filters = []
    crop = project.crop
    if crop is not None:
        filters.append(f"crop={even(crop.w)}:{even(crop.h)}:{int(crop.x)}:{int(crop.y)}")
    filters.append(f"scale={w}:{h}")

    cap = ["-frames:v", str(int(total_frames))] if total_frames else []

    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
        "-fps_mode", "cfr",
        "-r", str(fps),
        *cap,
        "-vf", ",".join(filters),
        "-c:v", "libx264",
        "-crf", str(int(settings.crf)),
        "-preset", str(settings.preset),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(settings.out_path),
    ]


class _ExportWorker(QObject):
    """Runs ffmpeg to completion on a worker thread."""

    progress = Signal(int)
    finished = Signal(bool, str)

    def __init__(self, project: Project, settings: ExportSettings, ffmpeg: str) -> None:
        super().__init__()
        self._project = project
        self._settings = settings
        self._ffmpeg = ffmpeg
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    # Called from the GUI thread.
    def cancel(self) -> None:
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def run(self) -> None:
        exe = shutil.which(self._ffmpeg) or (
            self._ffmpeg if os.path.isabs(self._ffmpeg) and os.access(self._ffmpeg, os.X_OK) else None
        )
        if exe is None:
            self.finished.emit(False, f"ffmpeg not found on PATH (looked for {self._ffmpeg!r})")
            return

        fps = int(self._settings.fps or self._project.fps)
        try:
            list_text, total_frames = build_concat_list(self._project, fps)
        except ValueError as exc:
            self.finished.emit(False, str(exc))
            return

        out_path = Path(self._settings.out_path)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.finished.emit(False, f"cannot create output directory: {exc}")
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="stopmotion-export-"))
        list_path = tmpdir / "list.txt"
        try:
            list_path.write_text(list_text, encoding="utf-8")
            cmd = build_command(self._project, self._settings, list_path, exe, total_frames)

            if self._cancelled:
                self.finished.emit(False, "cancelled")
                return

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                self.finished.emit(False, f"could not start ffmpeg: {exc}")
                return

            tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
            last_pct = -1
            assert self._proc.stderr is not None
            buf = b""
            while True:
                chunk = self._proc.stderr.read(512)
                if not chunk:
                    break
                buf += chunk
                # ffmpeg separates progress updates with \r and messages with \n.
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for part in parts:
                    if not part.strip():
                        continue
                    tail.append(part.decode("utf-8", "replace").rstrip())
                    m = _FRAME_RE.search(part)
                    if m and total_frames > 0:
                        pct = min(99, int(int(m.group(1)) * 100 / total_frames))
                        if pct != last_pct:
                            last_pct = pct
                            self.progress.emit(pct)
            if buf.strip():
                tail.append(buf.decode("utf-8", "replace").rstrip())

            rc = self._proc.wait()

            if self._cancelled:
                self.finished.emit(False, "cancelled")
                return
            if rc != 0:
                detail = "\n".join(list(tail)[-12:])
                self.finished.emit(
                    False, f"ffmpeg failed (exit code {rc}).\n{detail}".rstrip()
                )
                return

            self.progress.emit(100)
            secs = total_frames / fps
            self.finished.emit(
                True,
                f"Exported {len(self._project.visible_frames())} visible frames "
                f"({total_frames} output frames, {secs:.2f} s at {fps} fps) to {out_path}",
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.finished.emit(False, f"export failed: {exc}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class Exporter(QObject):
    """Drives ffmpeg on a worker thread.

    Emits :attr:`progress` 0..100 and exactly one :attr:`finished` per
    :meth:`start`.
    """

    progress = Signal(int)
    finished = Signal(bool, str)

    def __init__(self, ffmpeg: str = "ffmpeg", parent=None) -> None:
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self._thread: QThread | None = None
        self._worker: _ExportWorker | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, project: Project, settings: ExportSettings) -> None:
        if self.is_running():
            self.finished.emit(False, "an export is already running")
            return

        thread = QThread()
        worker = _ExportWorker(project, settings, self.ffmpeg)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self.progress)
        worker.finished.connect(self._on_finished)

        self._thread = thread
        self._worker = worker
        thread.start()

    def cancel(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel()

    def wait(self, msecs: int = 30000) -> bool:
        """Block until the worker thread has finished.  For tests and shutdown."""
        thread = self._thread
        if thread is None:
            return True
        return thread.wait(msecs)

    def _on_finished(self, ok: bool, message: str) -> None:
        thread, worker = self._thread, self._worker
        self._thread, self._worker = None, None
        if thread is not None:
            thread.quit()
            thread.wait(5000)
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self.finished.emit(ok, message)
