"""Horizontally scrolling thumbnail strip with the timeline selection model.

Selection semantics (exactly as specified):

* plain left click  -- select only that frame
* SHIFT + click     -- extend the range from the anchor
* CTRL + click      -- toggle that frame in/out of the selection

Dragging a selected thumbnail reorders the whole selection.  The widget never
mutates the project: it emits requests and the main window turns them into
undoable commands.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QAbstractScrollArea, QInputDialog, QMenu

CELL_W = 132
CELL_GAP = 8
MARGIN = 8
LABEL_H = 30
DRAG_THRESHOLD = 6


class Filmstrip(QAbstractScrollArea):
    selection_changed = Signal(list)  # list[str] frame ids, in project order
    delete_requested = Signal(list)
    hide_requested = Signal(list, bool)
    duplicate_requested = Signal(list)
    duration_requested = Signal(list, int)  # ids, milliseconds
    reorder_requested = Signal(list, int)  # ids, insert_at index
    frame_activated = Signal(str)  # double click

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumHeight(140)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setAutoFillBackground(True)

        self._project = None
        self._selection: list[str] = []
        self._anchor: int | None = None
        self._playhead: str = ""
        self._thumbs: dict[str, QPixmap] = {}
        self._thumb_h = 0

        self._press_pos: QPoint | None = None
        self._press_index: int | None = None
        self._dragging = False
        self._drop_index: int | None = None

    # ----------------------------------------------------------- the model

    def set_project(self, project) -> None:
        self._project = project
        self._thumbs.clear()
        self._selection = []
        self._anchor = None
        self._playhead = ""
        self.refresh()

    def project(self):
        return self._project

    def frames(self) -> list:
        if self._project is None:
            return []
        return list(getattr(self._project, "frames", []) or [])

    def count(self) -> int:
        return len(self.frames())

    def refresh(self) -> None:
        """Re-read the project (frames added/removed/reordered)."""
        ids = {f.id for f in self.frames()}
        # forget thumbnails for frames that no longer exist
        for stale in [k for k in self._thumbs if k not in ids]:
            self._thumbs.pop(stale, None)
        self._selection = [i for i in self._selection if i in ids]
        self._update_scrollbar()
        self.viewport().update()

    def invalidate_thumbnail(self, frame_id: str) -> None:
        self._thumbs.pop(frame_id, None)
        self.viewport().update()

    # ------------------------------------------------------------ selection

    def selection(self) -> list[str]:
        return list(self._selection)

    def set_selection(self, ids: list[str], *, emit: bool = False) -> None:
        order = {f.id: i for i, f in enumerate(self.frames())}
        cleaned = sorted({i for i in ids if i in order}, key=lambda i: order[i])
        if cleaned == self._selection:
            if emit:
                self.selection_changed.emit(list(self._selection))
            return
        self._selection = cleaned
        if cleaned:
            self._anchor = order[cleaned[-1]]
        self.viewport().update()
        if emit:
            self.selection_changed.emit(list(self._selection))

    def select_all(self) -> None:
        self.set_selection([f.id for f in self.frames()], emit=True)

    def handle_click(self, index: int, modifiers: Qt.KeyboardModifier) -> None:
        """The selection model.  Exposed directly so tests need no mouse."""
        frames = self.frames()
        if not (0 <= index < len(frames)):
            return
        frame_id = frames[index].id
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        if shift:
            anchor = self._anchor if self._anchor is not None else index
            lo, hi = (anchor, index) if anchor <= index else (index, anchor)
            span = [f.id for f in frames[lo : hi + 1]]
            if ctrl:  # shift+ctrl: add the range to what is already selected
                merged = set(self._selection) | set(span)
                self.set_selection(list(merged), emit=True)
            else:
                self.set_selection(span, emit=True)
            self._anchor = anchor
            return

        if ctrl:
            current = set(self._selection)
            if frame_id in current:
                current.discard(frame_id)
            else:
                current.add(frame_id)
            self.set_selection(list(current), emit=True)
            self._anchor = index
            return

        self.set_selection([frame_id], emit=True)
        self._anchor = index

    # ------------------------------------------------------------ playhead

    def set_playhead(self, frame_id: str) -> None:
        if frame_id != self._playhead:
            self._playhead = frame_id or ""
            self.viewport().update()

    def ensure_visible(self, frame_id: str) -> None:
        frames = self.frames()
        for i, frame in enumerate(frames):
            if frame.id == frame_id:
                left = MARGIN + i * (CELL_W + CELL_GAP)
                right = left + CELL_W
                bar = self.horizontalScrollBar()
                if left < bar.value():
                    bar.setValue(left)
                elif right > bar.value() + self.viewport().width():
                    bar.setValue(right - self.viewport().width())
                return

    # ------------------------------------------------------------- geometry

    def _content_width(self) -> int:
        n = self.count()
        if n == 0:
            return 0
        return MARGIN * 2 + n * CELL_W + (n - 1) * CELL_GAP

    def _update_scrollbar(self) -> None:
        bar = self.horizontalScrollBar()
        extra = max(0, self._content_width() - self.viewport().width())
        bar.setRange(0, extra)
        bar.setPageStep(self.viewport().width())
        bar.setSingleStep(CELL_W // 2)

    def _cell_rect(self, index: int) -> QRect:
        offset = self.horizontalScrollBar().value()
        x = MARGIN + index * (CELL_W + CELL_GAP) - offset
        h = max(40, self.viewport().height() - 2 * MARGIN)
        return QRect(x, MARGIN, CELL_W, h)

    def index_at(self, pos: QPoint) -> int | None:
        for i in range(self.count()):
            if self._cell_rect(i).contains(pos):
                return i
        return None

    def _insert_index_at(self, pos: QPoint) -> int:
        offset = self.horizontalScrollBar().value()
        x = pos.x() + offset - MARGIN
        step = CELL_W + CELL_GAP
        if step <= 0:
            return 0
        return max(0, min(self.count(), int(round(x / step))))

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt)
        super().resizeEvent(event)
        new_h = max(24, self.viewport().height() - 2 * MARGIN - LABEL_H)
        if new_h != self._thumb_h:
            self._thumb_h = new_h
            self._thumbs.clear()
        self._update_scrollbar()

    # ----------------------------------------------------------- thumbnails

    def _thumbnail(self, frame) -> QPixmap | None:
        cached = self._thumbs.get(frame.id)
        if cached is not None:
            return cached
        if self._project is None:
            return None
        try:
            path = self._project.frame_path(frame)
        except Exception:
            return None
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return None
        height = self._thumb_h or max(24, self.viewport().height() - 2 * MARGIN - LABEL_H)
        scaled = pixmap.scaled(
            QSize(CELL_W - 8, height),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumbs[frame.id] = scaled
        return scaled

    def _duration_ms(self, frame) -> int:
        fps = 12
        if self._project is not None:
            fps = max(1, int(getattr(self._project, "fps", 12) or 12))
        holds = max(1, int(getattr(frame, "holds", 1) or 1))
        return int(round(holds * 1000.0 / fps))

    # ---------------------------------------------------------------- paint

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt)
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor(32, 32, 36))
        frames = self.frames()
        if not frames:
            painter.setPen(QColor(140, 140, 145))
            painter.drawText(
                self.viewport().rect(),
                Qt.AlignmentFlag.AlignCenter,
                "No frames yet — press the capture button",
            )
            painter.end()
            return

        clip = self.viewport().rect().adjusted(-CELL_W, 0, CELL_W, 0)
        small = QFont(painter.font())
        small.setPointSizeF(max(7.5, small.pointSizeF() - 1.0))

        for index, frame in enumerate(frames):
            cell = self._cell_rect(index)
            if not cell.intersects(clip):
                continue
            selected = frame.id in self._selection
            hidden = bool(getattr(frame, "hidden", False))

            painter.fillRect(cell, QColor(48, 48, 54) if not selected else QColor(38, 74, 110))

            thumb_area = QRect(cell.x() + 4, cell.y() + 4, cell.width() - 8, cell.height() - LABEL_H)
            pixmap = self._thumbnail(frame)
            if pixmap is not None and not pixmap.isNull():
                tx = thumb_area.x() + (thumb_area.width() - pixmap.width()) // 2
                ty = thumb_area.y() + (thumb_area.height() - pixmap.height()) // 2
                target = QRect(tx, ty, pixmap.width(), pixmap.height())
                painter.setOpacity(0.35 if hidden else 1.0)
                painter.drawPixmap(target, pixmap)
                painter.setOpacity(1.0)
            else:
                painter.fillRect(thumb_area, QColor(60, 60, 66))
                painter.setPen(QColor(130, 130, 135))
                painter.drawText(thumb_area, Qt.AlignmentFlag.AlignCenter, "…")

            if hidden:
                painter.save()
                painter.setClipRect(thumb_area)
                pen = QPen(QColor(200, 200, 210, 90))
                pen.setWidth(2)
                painter.setPen(pen)
                step = 10
                for x in range(thumb_area.x() - thumb_area.height(), thumb_area.right(), step):
                    painter.drawLine(
                        x, thumb_area.bottom(), x + thumb_area.height(), thumb_area.y()
                    )
                painter.restore()

            painter.setFont(small)
            painter.setPen(QColor(235, 235, 240) if not hidden else QColor(160, 160, 165))
            label_rect = QRect(
                cell.x() + 4, cell.bottom() - LABEL_H + 4, cell.width() - 8, LABEL_H - 6
            )
            text = f"#{index + 1}   {self._duration_ms(frame)} ms"
            if hidden:
                text += "   (hidden)"
            metrics = QFontMetrics(small)
            painter.drawText(
                label_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(text, Qt.TextElideMode.ElideRight, label_rect.width()),
            )

            if selected:
                pen = QPen(QColor(120, 190, 255))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                painter.drawRect(cell.adjusted(1, 1, -1, -1))
            if frame.id == self._playhead:
                pen = QPen(QColor(255, 200, 60))
                pen.setWidth(3)
                painter.setPen(pen)
                painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                painter.drawRect(cell.adjusted(2, 2, -2, -2))

        if self._dragging and self._drop_index is not None:
            offset = self.horizontalScrollBar().value()
            x = MARGIN + self._drop_index * (CELL_W + CELL_GAP) - CELL_GAP // 2 - offset
            pen = QPen(QColor(120, 220, 140))
            pen.setWidth(3)
            painter.setPen(pen)
            painter.drawLine(x, MARGIN, x, self.viewport().height() - MARGIN)

        painter.end()

    # ---------------------------------------------------------------- mouse

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt)
        pos = event.position().toPoint()
        index = self.index_at(pos)
        if event.button() == Qt.MouseButton.RightButton:
            if index is not None and self.frames()[index].id not in self._selection:
                self.handle_click(index, Qt.KeyboardModifier.NoModifier)
            self._show_menu(event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._press_pos = pos
        self._press_index = index
        self._dragging = False
        self._drop_index = None
        if index is None:
            self.set_selection([], emit=True)
            return
        frame_id = self.frames()[index].id
        plain = event.modifiers() == Qt.KeyboardModifier.NoModifier
        if plain and frame_id in self._selection and len(self._selection) > 1:
            # keep a multi-selection alive so it can be dragged; a plain click
            # that turns out not to be a drag collapses it on release
            return
        self.handle_click(index, event.modifiers())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt)
        if self._press_pos is None or self._press_index is None:
            return
        pos = event.position().toPoint()
        if not self._dragging:
            if (pos - self._press_pos).manhattanLength() < DRAG_THRESHOLD:
                return
            frames = self.frames()
            if not (0 <= self._press_index < len(frames)):
                return
            if frames[self._press_index].id not in self._selection:
                return
            self._dragging = True
        self._drop_index = self._insert_index_at(pos)
        self.viewport().update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt)
        if self._dragging and self._drop_index is not None and self._selection:
            self.reorder_requested.emit(list(self._selection), int(self._drop_index))
        elif (
            not self._dragging
            and self._press_index is not None
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            # collapse a kept-alive multi-selection
            self.handle_click(self._press_index, Qt.KeyboardModifier.NoModifier)
        self._press_pos = None
        self._press_index = None
        self._dragging = False
        self._drop_index = None
        self.viewport().update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt)
        index = self.index_at(event.position().toPoint())
        if index is not None:
            self.frame_activated.emit(self.frames()[index].id)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt)
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - delta)
        event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt)
        # handled in mousePressEvent so the selection is updated first
        event.accept()

    def _show_menu(self, global_pos: QPoint) -> None:
        ids = self.selection()
        menu = QMenu(self)
        delete = menu.addAction("Delete")
        frames = {f.id: f for f in self.frames()}
        any_visible = any(not getattr(frames[i], "hidden", False) for i in ids if i in frames)
        hide = menu.addAction("Hide" if any_visible else "Unhide")
        duplicate = menu.addAction("Duplicate")
        menu.addSeparator()
        duration = menu.addAction("Set duration…")
        for action in (delete, hide, duplicate, duration):
            action.setEnabled(bool(ids))
        chosen = menu.exec(global_pos)
        if chosen is None or not ids:
            return
        if chosen is delete:
            self.delete_requested.emit(ids)
        elif chosen is hide:
            self.hide_requested.emit(ids, any_visible)
        elif chosen is duplicate:
            self.duplicate_requested.emit(ids)
        elif chosen is duration:
            self.prompt_duration(ids)

    def prompt_duration(self, ids: list[str]) -> None:
        if not ids:
            return
        fps = 12
        if self._project is not None:
            fps = max(1, int(getattr(self._project, "fps", 12) or 12))
        step = int(round(1000.0 / fps))
        frames = {f.id: f for f in self.frames()}
        current = self._duration_ms(frames[ids[0]]) if ids[0] in frames else step
        value, ok = QInputDialog.getInt(
            self,
            "Set duration",
            f"Duration in ms (snapped to {step} ms at {fps} fps):",
            current,
            step,
            step * 240,
            step,
        )
        if ok:
            self.duration_requested.emit(ids, int(value))

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt)
        key = event.key()
        frames = self.frames()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right) and frames:
            current = self._anchor if self._anchor is not None else 0
            delta = -1 if key == Qt.Key.Key_Left else 1
            index = max(0, min(len(frames) - 1, current + delta))
            self.handle_click(index, event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.ensure_visible(frames[index].id)
            event.accept()
            return
        super().keyPressEvent(event)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt)
        return QSize(800, 160)
