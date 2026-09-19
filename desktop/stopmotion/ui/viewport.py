"""The big central image area.

Two modes, toggled with SPACE:

* ``live``   -- paints the incoming MJPEG preview, composites the onion skin
                (the previous 1..3 *visible* captured stills) on top and draws
                the crop rectangle as a guide with the discarded region dimmed.
* ``review`` -- plays the captured frames back at project fps, honouring each
                frame's hold count and skipping hidden frames.

The onion skin is the reason this app exists, so the still cache is filled
eagerly: whenever a capture arrives the main window hands us the decoded
QImage and we keep a preview-sized copy.  Anything not in the cache is pulled
lazily from disk through ``still_loader``.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

LIVE = "live"
REVIEW = "review"

#: relative opacity of the 1st/2nd/3rd onion layer (nearer frames more opaque)
ONION_FALLOFF = (1.0, 0.6, 0.38)

#: what we downscale captured stills to when we have no preview size yet
DEFAULT_STILL_SIZE = QSize(960, 540)


class Viewport(QWidget):
    """Live preview + onion skin + crop guides, or review playback."""

    mode_changed = Signal(str)
    playhead_changed = Signal(str)  # frame id ("" when there is nothing to show)
    playing_changed = Signal(bool)
    capture_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(False)

        self._mode = LIVE
        self._preview: QImage | None = None
        self._preview_size = DEFAULT_STILL_SIZE

        # onion skin
        self._still_cache: dict[str, QImage] = {}
        self._onion_ids: list[str] = []
        self._onion_enabled = True
        self._onion_depth = 1
        self._onion_opacity = 0.45
        #: set by MainWindow -- frame id -> full QImage (or None)
        self.still_loader: Callable[[str], QImage | None] | None = None

        # crop guide
        self._crop: object | None = None
        self._capture_size: tuple[int, int] = (0, 0)
        self._show_crop = True

        # review playback
        self._project: object | None = None
        self._ticks: list[str] = []  # one entry per hold unit, frame ids
        self._tick = 0
        self._loop = True
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._advance)

        self._message = "No connection"

    # ------------------------------------------------------------------ mode

    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        mode = REVIEW if mode == REVIEW else LIVE
        if mode == self._mode:
            return
        self._mode = mode
        if mode == REVIEW:
            self._rebuild_ticks()
        else:
            self.pause()
        self.mode_changed.emit(mode)
        self.update()

    def toggle_mode(self) -> None:
        self.set_mode(LIVE if self._mode == REVIEW else REVIEW)

    # --------------------------------------------------------------- preview

    def set_preview_image(self, image: QImage) -> None:
        if image is None or image.isNull():
            return
        self._preview = image
        if image.size().isValid() and not image.size().isEmpty():
            self._preview_size = image.size()
        if self._mode == LIVE:
            self.update()

    def clear_preview(self, message: str = "No preview") -> None:
        self._preview = None
        self._message = message
        self.update()

    def set_message(self, message: str) -> None:
        self._message = message
        if self._preview is None:
            self.update()

    # ------------------------------------------------------------ onion skin

    def cache_still(self, frame_id: str, image: QImage) -> None:
        """Store a preview-sized copy of a captured still."""
        if image is None or image.isNull():
            return
        target = self._preview_size
        if not target.isValid() or target.isEmpty():
            target = DEFAULT_STILL_SIZE
        scaled = image.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._still_cache[frame_id] = scaled

    def drop_still(self, frame_id: str) -> None:
        self._still_cache.pop(frame_id, None)

    def clear_stills(self) -> None:
        self._still_cache.clear()

    def cached_still_ids(self) -> list[str]:
        return list(self._still_cache)

    def set_onion_frames(self, ids: list[str]) -> None:
        """Nearest first.  MainWindow decides which frames these are."""
        ids = list(ids)
        if ids != self._onion_ids:
            self._onion_ids = ids
            if self._mode == LIVE:
                self.update()

    def onion_frames(self) -> list[str]:
        return list(self._onion_ids)

    def set_onion_enabled(self, enabled: bool) -> None:
        self._onion_enabled = bool(enabled)
        self.update()

    def onion_enabled(self) -> bool:
        return self._onion_enabled

    def set_onion_depth(self, depth: int) -> None:
        self._onion_depth = max(1, min(3, int(depth)))
        self.update()

    def onion_depth(self) -> int:
        return self._onion_depth

    def set_onion_opacity(self, opacity: float) -> None:
        self._onion_opacity = min(1.0, max(0.0, float(opacity)))
        self.update()

    def onion_opacity(self) -> float:
        return self._onion_opacity

    def _onion_image(self, frame_id: str) -> QImage | None:
        image = self._still_cache.get(frame_id)
        if image is not None:
            return image
        if self.still_loader is None:
            return None
        loaded = self.still_loader(frame_id)
        if loaded is None or loaded.isNull():
            return None
        self.cache_still(frame_id, loaded)
        return self._still_cache.get(frame_id)

    # ------------------------------------------------------------ crop guide

    def set_crop(self, crop: object | None, capture_size: tuple[int, int]) -> None:
        self._crop = crop
        self._capture_size = tuple(capture_size or (0, 0))  # type: ignore[assignment]
        self.update()

    def set_crop_guides(self, show: bool) -> None:
        self._show_crop = bool(show)
        self.update()

    def crop_guides(self) -> bool:
        return self._show_crop

    # -------------------------------------------------------------- playback

    def set_project(self, project: object | None) -> None:
        self._project = project
        self._still_cache.clear()
        self._onion_ids = []
        self._rebuild_ticks()
        self.update()

    def timeline_changed(self) -> None:
        self._rebuild_ticks()
        self.update()

    def _visible_frames(self) -> list:
        project = self._project
        if project is None:
            return []
        try:
            return list(project.visible_frames())
        except Exception:
            frames = getattr(project, "frames", []) or []
            return [f for f in frames if not getattr(f, "hidden", False)]

    def _rebuild_ticks(self) -> None:
        current = self.current_frame_id()
        ticks: list[str] = []
        for frame in self._visible_frames():
            holds = max(1, int(getattr(frame, "holds", 1) or 1))
            ticks.extend([frame.id] * holds)
        self._ticks = ticks
        if not ticks:
            self._tick = 0
            self.pause()
            return
        if current and current in ticks:
            self._tick = ticks.index(current)
        else:
            self._tick = min(self._tick, len(ticks) - 1)

    def _interval_ms(self) -> int:
        fps = 12
        if self._project is not None:
            fps = int(getattr(self._project, "fps", 12) or 12)
        return max(1, int(round(1000.0 / max(1, fps))))

    def is_playing(self) -> bool:
        return self._timer.isActive()

    def play(self) -> None:
        self._rebuild_ticks()
        if not self._ticks:
            return
        if self._mode != REVIEW:
            self.set_mode(REVIEW)
        if self._tick >= len(self._ticks) - 1:
            self._tick = 0
        self._timer.start(self._interval_ms())
        self.playing_changed.emit(True)
        self._emit_playhead()
        self.update()

    def pause(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self.playing_changed.emit(False)
            self.update()

    def toggle_play(self) -> None:
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def set_loop(self, loop: bool) -> None:
        self._loop = bool(loop)

    def loop(self) -> bool:
        return self._loop

    def _advance(self) -> None:
        if not self._ticks:
            self.pause()
            return
        if self._tick + 1 >= len(self._ticks):
            if self._loop:
                self._tick = 0
            else:
                self.pause()
                return
        else:
            self._tick += 1
        self._emit_playhead()
        self.update()

    def step(self, delta: int) -> None:
        """Step by whole frames (not hold ticks)."""
        self.pause()
        frames = [f.id for f in self._visible_frames()]
        if not frames:
            return
        current = self.current_frame_id()
        index = frames.index(current) if current in frames else 0
        index = max(0, min(len(frames) - 1, index + int(delta)))
        self.seek_to_frame(frames[index])

    def seek_to_frame(self, frame_id: str) -> None:
        self._rebuild_ticks()
        if frame_id in self._ticks:
            self._tick = self._ticks.index(frame_id)
            self._emit_playhead()
            self.update()

    def current_frame_id(self) -> str:
        if self._ticks and 0 <= self._tick < len(self._ticks):
            return self._ticks[self._tick]
        return ""

    def _emit_playhead(self) -> None:
        self.playhead_changed.emit(self.current_frame_id())

    def _review_image(self) -> QImage | None:
        frame_id = self.current_frame_id()
        if not frame_id:
            return None
        image = self._still_cache.get(frame_id)
        if image is not None:
            return image
        if self.still_loader is not None:
            loaded = self.still_loader(frame_id)
            if loaded is not None and not loaded.isNull():
                self.cache_still(frame_id, loaded)
                return self._still_cache.get(frame_id)
        return None

    # ------------------------------------------------------------- geometry

    def _fit_rect(self, image_size: QSize) -> QRect:
        area = self.rect()
        if image_size.isEmpty() or area.isEmpty():
            return area
        scale = min(area.width() / image_size.width(), area.height() / image_size.height())
        width = max(1, int(image_size.width() * scale))
        height = max(1, int(image_size.height() * scale))
        return QRect(
            area.x() + (area.width() - width) // 2,
            area.y() + (area.height() - height) // 2,
            width,
            height,
        )

    def image_rect(self) -> QRect:
        """Where the current image lands on screen (public for tests)."""
        image = self._preview if self._mode == LIVE else self._review_image()
        if image is None or image.isNull():
            return QRect()
        return self._fit_rect(image.size())

    def crop_rect_on(self, target: QRect) -> QRect:
        """Map the project crop (sensor coordinates) onto ``target``."""
        crop = self._crop
        if crop is None or target.isEmpty():
            return QRect()
        sw, sh = self._capture_size
        try:
            cx, cy = float(crop.x), float(crop.y)
            cw, ch = float(crop.w), float(crop.h)
        except AttributeError:
            return QRect()
        if not sw or not sh or cw <= 0 or ch <= 0:
            return QRect()
        fx, fy = cx / sw, cy / sh
        fw, fh = cw / sw, ch / sh
        return QRect(
            target.x() + int(round(fx * target.width())),
            target.y() + int(round(fy * target.height())),
            max(1, int(round(fw * target.width()))),
            max(1, int(round(fh * target.height()))),
        ).intersected(target)

    # ---------------------------------------------------------------- paint

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(24, 24, 27))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        if self._mode == LIVE:
            self._paint_live(painter)
        else:
            self._paint_review(painter)

        self._paint_badge(painter)
        painter.end()

    def _paint_live(self, painter: QPainter) -> None:
        base = self._preview
        if base is None or base.isNull():
            self._paint_message(painter, self._message)
            return
        target = self._fit_rect(base.size())
        painter.drawImage(target, base)
        self._paint_onion(painter, target)
        self._paint_crop(painter, target)

    def _paint_onion(self, painter: QPainter, target: QRect) -> None:
        if not self._onion_enabled or self._onion_opacity <= 0.0:
            return
        ids = self._onion_ids[: self._onion_depth]
        for index, frame_id in enumerate(ids):
            image = self._onion_image(frame_id)
            if image is None or image.isNull():
                continue
            falloff = ONION_FALLOFF[min(index, len(ONION_FALLOFF) - 1)]
            painter.setOpacity(min(1.0, self._onion_opacity * falloff))
            painter.drawImage(self._fit_rect(image.size()), image)
        painter.setOpacity(1.0)

    def _paint_crop(self, painter: QPainter, target: QRect) -> None:
        if not self._show_crop:
            return
        crop = self.crop_rect_on(target)
        if crop.isEmpty():
            return
        dim = QColor(0, 0, 0, 120)
        # dim everything outside the kept region
        painter.fillRect(QRect(target.x(), target.y(), target.width(), crop.y() - target.y()), dim)
        painter.fillRect(
            QRect(target.x(), crop.bottom() + 1, target.width(), target.bottom() - crop.bottom()),
            dim,
        )
        painter.fillRect(QRect(target.x(), crop.y(), crop.x() - target.x(), crop.height()), dim)
        painter.fillRect(
            QRect(crop.right() + 1, crop.y(), target.right() - crop.right(), crop.height()), dim
        )

        pen = QPen(QColor(255, 220, 90, 200))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(crop.adjusted(0, 0, -1, -1))
        # thirds
        pen.setColor(QColor(255, 220, 90, 70))
        painter.setPen(pen)
        for i in (1, 2):
            x = crop.x() + crop.width() * i // 3
            y = crop.y() + crop.height() * i // 3
            painter.drawLine(x, crop.y(), x, crop.bottom())
            painter.drawLine(crop.x(), y, crop.right(), y)

    def _paint_review(self, painter: QPainter) -> None:
        image = self._review_image()
        if image is None or image.isNull():
            self._paint_message(painter, "No frames to review")
            return
        painter.drawImage(self._fit_rect(image.size()), image)

    def _paint_message(self, painter: QPainter, text: str) -> None:
        painter.setPen(QColor(150, 150, 155))
        font = QFont(painter.font())
        font.setPointSizeF(max(10.0, font.pointSizeF() + 2))
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)

    def _paint_badge(self, painter: QPainter) -> None:
        label = "LIVE" if self._mode == LIVE else "REVIEW"
        if self._mode == LIVE and self._onion_enabled and self._onion_ids:
            label += f"  ·  onion {min(self._onion_depth, len(self._onion_ids))}"
            label += f" @ {int(self._onion_opacity * 100)}%"
        elif self._mode == REVIEW:
            total = len(self._ticks)
            label += f"  ·  {self._tick + 1}/{total}" if total else ""
        rect = QRect(8, 8, max(120, 9 * len(label)), 22)
        painter.fillRect(rect, QColor(0, 0, 0, 140))
        painter.setPen(QColor(240, 240, 240) if self._mode == LIVE else QColor(120, 220, 255))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    # ----------------------------------------------------------------- keys

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt)
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_mode()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.capture_requested.emit()
            event.accept()
            return
        if self._mode == REVIEW:
            if key == Qt.Key.Key_Left:
                self.step(-1)
                event.accept()
                return
            if key == Qt.Key.Key_Right:
                self.step(1)
                event.accept()
                return
            if key == Qt.Key.Key_P:
                self.toggle_play()
                event.accept()
                return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt)
        return QSize(960, 540)
