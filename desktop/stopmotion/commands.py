"""Undo/redo layer for timeline edits.

Every mutation of the timeline goes through a :class:`TimelineCommand`, which
records the selection before and after the change.  ``undo()`` restores
``sel_before``, ``redo()`` restores ``sel_after`` -- so undoing a delete of
frames 2 and 3 brings them back *and* selects them again, and redoing the
delete re-selects whatever the delete left selected.

Deletes remove manifest entries only.  JPEG files are immutable and are never
unlinked here; that is what makes undo of a delete possible at all.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from PySide6.QtGui import QUndoCommand

from .project import Frame, Project, utc_now

__all__ = [
    "TimelineContext",
    "TimelineCommand",
    "CaptureFrame",
    "DeleteFrames",
    "SetHolds",
    "SetHidden",
    "DuplicateFrames",
    "ReorderFrames",
]


@runtime_checkable
class TimelineContext(Protocol):
    """What a command needs from the UI.  ``MainWindow`` implements this."""

    project: Project

    def selection(self) -> list[str]:
        ...

    def set_selection(self, ids: list[str]) -> None:
        ...

    def timeline_changed(self) -> None:
        ...


class TimelineCommand(QUndoCommand):
    """Base class: subclasses implement ``_apply()`` / ``_revert()``.

    The base saves the project, restores the matching selection and pokes the
    UI.  Note that Qt calls :meth:`redo` once when the command is pushed onto
    the stack, which is what performs the edit in the first place.
    """

    def __init__(
        self,
        ctx: TimelineContext,
        text: str,
        sel_before: list[str] | None = None,
        sel_after: list[str] | None = None,
    ) -> None:
        super().__init__(text)
        self.ctx = ctx
        self.sel_before: list[str] = list(sel_before if sel_before is not None else ctx.selection())
        self.sel_after: list[str] = list(sel_after or [])

    # -- subclass hooks ---------------------------------------------------- #

    def _apply(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _revert(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- Qt entry points ---------------------------------------------------- #

    def redo(self) -> None:
        self._apply()
        self._settle(self.sel_after)

    def undo(self) -> None:
        self._revert()
        self._settle(self.sel_before)

    # -- shared plumbing ---------------------------------------------------- #

    def _settle(self, selection: list[str]) -> None:
        project = self.ctx.project
        project.dirty = True
        project.save()
        self.ctx.set_selection([fid for fid in selection if project.index_of(fid) >= 0])
        self.ctx.timeline_changed()

    @property
    def project(self) -> Project:
        return self.ctx.project

    def _ordered(self, ids: list[str]) -> list[str]:
        """Filter to ids that exist, in timeline order."""
        wanted = set(ids)
        return [f.id for f in self.project.frames if f.id in wanted]


# --------------------------------------------------------------------------- #

class CaptureFrame(TimelineCommand):
    """Append a freshly captured still.

    The JPEG is written to disk once, on the first ``redo``.  Undo drops the
    manifest entry; a later redo re-inserts the same entry pointing at the same
    (immutable) file, so the frame keeps its id.
    """

    def __init__(self, ctx: TimelineContext, jpeg: bytes, settings: dict,
                 size: tuple[int, int] | None = None) -> None:
        super().__init__(ctx, "Capture frame", sel_before=ctx.selection(), sel_after=[])
        self._jpeg = bytes(jpeg)
        self._settings = dict(settings or {})
        self._size = size
        self._frame: Frame | None = None
        self._index: int = -1

    def _apply(self) -> None:
        if self._frame is None:
            self._frame = self.project.add_frame(self._jpeg, self._settings, self._size)
            self._index = self.project.index_of(self._frame.id)
            self._jpeg = b""  # written; do not keep a full-res still in RAM per command
            self.sel_after = [self._frame.id]
        else:
            self.project.insert_frame(self._index, self._frame)

    def _revert(self) -> None:
        if self._frame is not None:
            removed = self.project.remove_frame(self._frame.id)
            if removed is not None:
                self._index = removed[0]

    @property
    def frame(self) -> Frame | None:
        return self._frame


class DeleteFrames(TimelineCommand):
    """Remove manifest entries.  Files stay on disk (orphaned until purged)."""

    def __init__(self, ctx: TimelineContext, ids: list[str]) -> None:
        super().__init__(ctx, "Delete frames", sel_before=ctx.selection())
        project = ctx.project
        wanted = set(ids)
        # (index, frame) pairs in ascending index order, so undo can reinsert
        # non-contiguous selections back into exactly their old slots.
        self._removed: list[tuple[int, Frame]] = [
            (i, f) for i, f in enumerate(project.frames) if f.id in wanted
        ]
        self.setText("Delete frame" if len(self._removed) == 1 else "Delete frames")
        self.sel_after = self._survivor_selection()

    def _survivor_selection(self) -> list[str]:
        """After deleting, select the next surviving frame (else the previous)."""
        frames = self.project.frames
        gone = {f.id for _, f in self._removed}
        if not gone or len(gone) >= len(frames):
            return []
        first = self._removed[0][0]
        for f in frames[first:]:
            if f.id not in gone:
                return [f.id]
        for f in reversed(frames[:first]):
            if f.id not in gone:
                return [f.id]
        return []

    def _apply(self) -> None:
        for _, frame in self._removed:
            self.project.remove_frame(frame.id)

    def _revert(self) -> None:
        for index, frame in self._removed:  # ascending: each insert fixes the next
            self.project.insert_frame(index, frame)


class SetHolds(TimelineCommand):
    """Set the hold count (frame duration in project-fps ticks)."""

    def __init__(self, ctx: TimelineContext, ids: list[str], holds: int) -> None:
        super().__init__(ctx, "Set duration", sel_before=ctx.selection())
        self._holds = max(1, int(holds))
        project = ctx.project
        self._old: list[tuple[str, int]] = []
        for fid in self._ordered(list(ids)):
            frame = project.frame_by_id(fid)
            if frame is not None:
                self._old.append((fid, int(frame.holds)))
        self.sel_after = [fid for fid, _ in self._old]

    def _apply(self) -> None:
        for fid, _ in self._old:
            frame = self.project.frame_by_id(fid)
            if frame is not None:
                frame.holds = self._holds

    def _revert(self) -> None:
        for fid, old in self._old:
            frame = self.project.frame_by_id(fid)
            if frame is not None:
                frame.holds = old


class SetHidden(TimelineCommand):
    """Hide or show frames.  Hidden frames stay in the array and in the filmstrip."""

    def __init__(self, ctx: TimelineContext, ids: list[str], hidden: bool) -> None:
        super().__init__(ctx, "Hide frames" if hidden else "Show frames",
                         sel_before=ctx.selection())
        self._hidden = bool(hidden)
        project = ctx.project
        self._old: list[tuple[str, bool]] = []
        for fid in self._ordered(list(ids)):
            frame = project.frame_by_id(fid)
            if frame is not None:
                self._old.append((fid, bool(frame.hidden)))
        self.sel_after = [fid for fid, _ in self._old]

    def _apply(self) -> None:
        for fid, _ in self._old:
            frame = self.project.frame_by_id(fid)
            if frame is not None:
                frame.hidden = self._hidden

    def _revert(self) -> None:
        for fid, old in self._old:
            frame = self.project.frame_by_id(fid)
            if frame is not None:
                frame.hidden = old


class DuplicateFrames(TimelineCommand):
    """Duplicate frames in place.

    A duplicate gets a brand new id but points at the *same* JPEG: the files
    are immutable, so there is no reason to copy bytes.  Each copy is inserted
    directly after its source.
    """

    def __init__(self, ctx: TimelineContext, ids: list[str]) -> None:
        super().__init__(ctx, "Duplicate frames", sel_before=ctx.selection())
        sources = self._ordered(list(ids))
        # (insert index in the final list, new frame) - built lazily on first apply
        # because ids must be allocated from the project's monotonic counter.
        self._sources = sources
        self._copies: list[Frame] = []
        self.setText("Duplicate frame" if len(sources) == 1 else "Duplicate frames")

    def _apply(self) -> None:
        project = self.project
        if not self._copies:
            for fid in self._sources:
                src = project.frame_by_id(fid)
                if src is None:
                    continue
                copy = src.copy()
                copy.id = project.allocate_id()
                copy.captured = utc_now()
                self._copies.append(copy)
            self.sel_after = [c.id for c in self._copies]
        # Insert in reverse source order so earlier indices stay valid.
        for fid, copy in reversed(list(zip(self._sources, self._copies))):
            at = project.index_of(fid)
            project.insert_frame(len(project.frames) if at < 0 else at + 1, copy)

    def _revert(self) -> None:
        for copy in self._copies:
            self.project.remove_frame(copy.id)


class ReorderFrames(TimelineCommand):
    """Move a (possibly multi-frame, possibly non-contiguous) selection.

    ``insert_at`` is a *gap index into the current list*, as a drag-and-drop
    drop indicator reports it: 0 means "before the first frame", ``len(frames)``
    means "after the last".  Frames that currently sit before the gap and are
    themselves being moved shift the gap left by one each -- that is the classic
    off-by-one, and it is corrected here rather than at every call site.
    """

    def __init__(self, ctx: TimelineContext, ids: list[str], insert_at: int) -> None:
        super().__init__(ctx, "Reorder frames", sel_before=ctx.selection())
        project = ctx.project
        self._moving = self._ordered(list(ids))
        self._insert_at = int(insert_at)
        self._old_order: list[str] = [f.id for f in project.frames]
        self.sel_after = list(self._moving)

    def _apply(self) -> None:
        project = self.project
        moving = set(self._moving)
        if not moving:
            return
        indices = [i for i, f in enumerate(project.frames) if f.id in moving]
        insert_at = max(0, min(self._insert_at, len(project.frames)))
        # Removing the moved frames shifts the gap left by the number of moved
        # frames that lay before it.
        target = insert_at - sum(1 for i in indices if i < insert_at)

        moved = [project.frames[i] for i in indices]
        rest = [f for i, f in enumerate(project.frames) if i not in set(indices)]
        target = max(0, min(target, len(rest)))
        project.frames = rest[:target] + moved + rest[target:]

    def _revert(self) -> None:
        project = self.project
        by_id = {f.id: f for f in project.frames}
        restored = [by_id[fid] for fid in self._old_order if fid in by_id]
        # Anything created after this command (shouldn't happen on a stack, but
        # be defensive) keeps its relative position at the end.
        known = set(self._old_order)
        restored += [f for f in project.frames if f.id not in known]
        project.frames = restored
