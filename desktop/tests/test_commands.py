"""Tests for the undo/redo layer.

The headline requirement is selection restore: undo a delete of frames 2 and 3
and those two frames must come back *selected*; redo and the selection the
delete left behind must come back too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QUndoStack  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from stopmotion.commands import (  # noqa: E402
    CaptureFrame,
    DeleteFrames,
    DuplicateFrames,
    ReorderFrames,
    SetHidden,
    SetHolds,
    TimelineContext,
)
from stopmotion.project import Project  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes" + b"\xff\xd9"


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class FakeContext:
    """Stand-in for MainWindow; implements the TimelineContext protocol."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self._selection: list[str] = []
        self.changes = 0

    def selection(self) -> list[str]:
        return list(self._selection)

    def set_selection(self, ids: list[str]) -> None:
        self._selection = list(ids)

    def timeline_changed(self) -> None:
        self.changes += 1


@pytest.fixture
def ctx(tmp_path) -> FakeContext:
    project = Project.create(tmp_path / "film", "film", 12, (1920, 1080), "16:9")
    for _ in range(5):
        project.add_frame(JPEG, {"iso": 100}, (1920, 1080))
    return FakeContext(project)


@pytest.fixture
def stack() -> QUndoStack:
    s = QUndoStack()
    s.setUndoLimit(200)
    return s


def ids(ctx: FakeContext) -> list[str]:
    return [f.id for f in ctx.project.frames]


def reload_ids(ctx: FakeContext) -> list[str]:
    """Frame order as it is actually persisted on disk."""
    return [f.id for f in Project.open(ctx.project.path).frames]


def test_context_is_recognised_as_a_timeline_context(ctx):
    assert isinstance(ctx, TimelineContext)


# --------------------------------------------------------------------------- #
# CaptureFrame
# --------------------------------------------------------------------------- #

def test_capture_frame_do_undo_redo(ctx, stack):
    ctx.set_selection(["f_0002"])
    stack.push(CaptureFrame(ctx, JPEG, {"iso": 800}, (1920, 1080)))

    assert ids(ctx) == ["f_0001", "f_0002", "f_0003", "f_0004", "f_0005", "f_0006"]
    assert ctx.selection() == ["f_0006"], "a new capture becomes the selection"
    assert reload_ids(ctx)[-1] == "f_0006"
    jpeg_path = ctx.project.frame_path(ctx.project.frames[-1])
    assert jpeg_path.read_bytes() == JPEG

    stack.undo()
    assert ids(ctx) == ["f_0001", "f_0002", "f_0003", "f_0004", "f_0005"]
    assert ctx.selection() == ["f_0002"], "undo restores the pre-capture selection"
    assert jpeg_path.exists(), "undo must not erase pixels"
    assert reload_ids(ctx) == ids(ctx)

    stack.redo()
    assert ids(ctx)[-1] == "f_0006"
    assert ctx.selection() == ["f_0006"]


def test_capture_after_undone_capture_does_not_reuse_the_id(ctx, stack):
    stack.push(CaptureFrame(ctx, JPEG, {}, (1920, 1080)))
    stack.undo()
    stack.push(CaptureFrame(ctx, JPEG, {}, (1920, 1080)))
    assert ids(ctx)[-1] == "f_0007"


# --------------------------------------------------------------------------- #
# DeleteFrames  -- the selection-restore headline case
# --------------------------------------------------------------------------- #

def test_delete_two_frames_and_undo_reselects_them(ctx, stack):
    ctx.set_selection(["f_0002", "f_0003"])
    stack.push(DeleteFrames(ctx, ["f_0002", "f_0003"]))

    assert ids(ctx) == ["f_0001", "f_0004", "f_0005"]
    after_delete = ctx.selection()
    assert after_delete == ["f_0004"], "the delete leaves the next survivor selected"

    stack.undo()
    assert ids(ctx) == ["f_0001", "f_0002", "f_0003", "f_0004", "f_0005"]
    assert ctx.selection() == ["f_0002", "f_0003"], "undo brings them back selected"

    stack.redo()
    assert ids(ctx) == ["f_0001", "f_0004", "f_0005"]
    assert ctx.selection() == after_delete, "redo restores the post-delete selection"


def test_delete_never_unlinks_jpegs(ctx, stack):
    paths = [ctx.project.frame_path(f) for f in ctx.project.frames[1:3]]
    stack.push(DeleteFrames(ctx, ["f_0002", "f_0003"]))
    assert all(p.exists() for p in paths)
    assert reload_ids(ctx) == ["f_0001", "f_0004", "f_0005"]


def test_delete_non_contiguous_selection_restores_exact_positions(ctx, stack):
    ctx.set_selection(["f_0001", "f_0003", "f_0005"])
    stack.push(DeleteFrames(ctx, ["f_0001", "f_0003", "f_0005"]))
    assert ids(ctx) == ["f_0002", "f_0004"]

    stack.undo()
    assert ids(ctx) == ["f_0001", "f_0002", "f_0003", "f_0004", "f_0005"]
    assert ctx.selection() == ["f_0001", "f_0003", "f_0005"]

    stack.redo()
    assert ids(ctx) == ["f_0002", "f_0004"]

    stack.undo()
    assert ids(ctx) == ["f_0001", "f_0002", "f_0003", "f_0004", "f_0005"]
    assert reload_ids(ctx) == ids(ctx)


def test_delete_preserves_frame_state_across_undo(ctx, stack):
    ctx.project.frames[2].holds = 7
    ctx.project.frames[2].hidden = True
    stack.push(DeleteFrames(ctx, ["f_0003"]))
    stack.undo()
    restored = ctx.project.frame_by_id("f_0003")
    assert restored.holds == 7 and restored.hidden is True


def test_delete_the_tail_selects_the_previous_survivor(ctx, stack):
    stack.push(DeleteFrames(ctx, ["f_0004", "f_0005"]))
    assert ctx.selection() == ["f_0003"]


def test_delete_everything_leaves_an_empty_selection(ctx, stack):
    stack.push(DeleteFrames(ctx, ids(ctx)))
    assert ids(ctx) == []
    assert ctx.selection() == []
    stack.undo()
    assert len(ids(ctx)) == 5


# --------------------------------------------------------------------------- #
# SetHolds / SetHidden
# --------------------------------------------------------------------------- #

def test_set_holds_do_undo_redo(ctx, stack):
    ctx.project.frames[1].holds = 4
    ctx.set_selection(["f_0002", "f_0004"])
    stack.push(SetHolds(ctx, ["f_0002", "f_0004"], 3))

    assert [f.holds for f in ctx.project.frames] == [1, 3, 1, 3, 1]
    assert ctx.selection() == ["f_0002", "f_0004"]
    assert ctx.project.duration_ms() == round(9 * 1000 / 12)

    stack.undo()
    assert [f.holds for f in ctx.project.frames] == [1, 4, 1, 1, 1]
    assert ctx.selection() == ["f_0002", "f_0004"]

    stack.redo()
    assert [f.holds for f in ctx.project.frames] == [1, 3, 1, 3, 1]
    assert [f.holds for f in Project.open(ctx.project.path).frames] == [1, 3, 1, 3, 1]


def test_set_holds_clamps_to_at_least_one(ctx, stack):
    stack.push(SetHolds(ctx, ["f_0001"], 0))
    assert ctx.project.frames[0].holds == 1


def test_set_hidden_do_undo_redo(ctx, stack):
    ctx.set_selection(["f_0002", "f_0004"])
    stack.push(SetHidden(ctx, ["f_0002", "f_0004"], True))

    assert [f.hidden for f in ctx.project.frames] == [False, True, False, True, False]
    assert len(ctx.project.frames) == 5, "hidden frames stay in the array"
    assert [f.id for f in ctx.project.visible_frames()] == ["f_0001", "f_0003", "f_0005"]
    assert ctx.project.duration_ms(True) == 250
    assert ctx.project.duration_ms(False) == round(5 * 1000 / 12)
    assert ctx.selection() == ["f_0002", "f_0004"]

    stack.undo()
    assert not any(f.hidden for f in ctx.project.frames)
    assert ctx.selection() == ["f_0002", "f_0004"]

    stack.redo()
    assert [f.hidden for f in ctx.project.frames] == [False, True, False, True, False]


# --------------------------------------------------------------------------- #
# DuplicateFrames
# --------------------------------------------------------------------------- #

def test_duplicate_inserts_copies_after_their_sources(ctx, stack):
    ctx.project.frames[1].holds = 3
    ctx.set_selection(["f_0002", "f_0004"])
    stack.push(DuplicateFrames(ctx, ["f_0002", "f_0004"]))

    assert ids(ctx) == ["f_0001", "f_0002", "f_0006", "f_0003", "f_0004", "f_0007", "f_0005"]
    assert ctx.selection() == ["f_0006", "f_0007"], "the copies end up selected"
    copy = ctx.project.frame_by_id("f_0006")
    assert copy.holds == 3
    assert copy.file == ctx.project.frame_by_id("f_0002").file, "copies share the immutable JPEG"
    assert reload_ids(ctx) == ids(ctx)

    stack.undo()
    assert ids(ctx) == ["f_0001", "f_0002", "f_0003", "f_0004", "f_0005"]
    assert ctx.selection() == ["f_0002", "f_0004"]

    stack.redo()
    assert ids(ctx) == ["f_0001", "f_0002", "f_0006", "f_0003", "f_0004", "f_0007", "f_0005"]
    assert ctx.selection() == ["f_0006", "f_0007"]


def test_duplicate_then_delete_the_source_keeps_the_pixels(ctx, stack):
    stack.push(DuplicateFrames(ctx, ["f_0002"]))
    shared = ctx.project.frame_path(ctx.project.frame_by_id("f_0006"))
    stack.push(DeleteFrames(ctx, ["f_0002"]))
    assert shared.exists()
    assert ctx.project.purge_unreferenced() == []


# --------------------------------------------------------------------------- #
# ReorderFrames -- the off-by-one
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "move,insert_at,expected",
    [
        # Drop gap indices are in the *pre-move* list's coordinates.
        (["f_0002", "f_0003"], 5, ["f_0001", "f_0004", "f_0005", "f_0002", "f_0003"]),
        (["f_0002", "f_0003"], 4, ["f_0001", "f_0004", "f_0002", "f_0003", "f_0005"]),
        (["f_0002", "f_0003"], 0, ["f_0002", "f_0003", "f_0001", "f_0004", "f_0005"]),
        (["f_0004"], 1, ["f_0001", "f_0004", "f_0002", "f_0003", "f_0005"]),
        (["f_0001"], 5, ["f_0002", "f_0003", "f_0004", "f_0005", "f_0001"]),
        (["f_0001", "f_0005"], 3, ["f_0002", "f_0003", "f_0001", "f_0005", "f_0004"]),
        (["f_0002"], 1, ["f_0001", "f_0002", "f_0003", "f_0004", "f_0005"]),
        (["f_0002"], 2, ["f_0001", "f_0002", "f_0003", "f_0004", "f_0005"]),
        (["f_0001", "f_0002", "f_0003"], 5, ["f_0004", "f_0005", "f_0001", "f_0002", "f_0003"]),
    ],
)
def test_reorder_insert_index_arithmetic(ctx, stack, move, insert_at, expected):
    stack.push(ReorderFrames(ctx, move, insert_at))
    assert ids(ctx) == expected


def test_reorder_moving_a_block_forward_is_not_off_by_one(ctx, stack):
    """f_0002+f_0003 dropped in the gap before f_0005 must land before f_0005.

    The naive implementation removes them first and then inserts at index 4 of
    the shortened list, which puts them after f_0005.
    """
    ctx.set_selection(["f_0002", "f_0003"])
    stack.push(ReorderFrames(ctx, ["f_0002", "f_0003"], 4))
    assert ids(ctx) == ["f_0001", "f_0004", "f_0002", "f_0003", "f_0005"]
    assert ids(ctx).index("f_0003") < ids(ctx).index("f_0005")


def test_reorder_do_undo_redo_with_selection(ctx, stack):
    ctx.set_selection(["f_0002", "f_0003"])
    before = ids(ctx)
    stack.push(ReorderFrames(ctx, ["f_0002", "f_0003"], 4))

    moved = ids(ctx)
    assert moved == ["f_0001", "f_0004", "f_0002", "f_0003", "f_0005"]
    assert ctx.selection() == ["f_0002", "f_0003"], "moved frames stay selected"
    assert reload_ids(ctx) == moved

    stack.undo()
    assert ids(ctx) == before
    assert ctx.selection() == ["f_0002", "f_0003"]
    assert reload_ids(ctx) == before

    stack.redo()
    assert ids(ctx) == moved
    assert ctx.selection() == ["f_0002", "f_0003"]


def test_reorder_is_a_no_op_for_an_empty_selection(ctx, stack):
    before = ids(ctx)
    stack.push(ReorderFrames(ctx, [], 2))
    assert ids(ctx) == before


# --------------------------------------------------------------------------- #
# stack behaviour
# --------------------------------------------------------------------------- #

def test_every_command_saves_and_notifies(ctx, stack):
    ctx.changes = 0
    stack.push(SetHolds(ctx, ["f_0001"], 2))
    stack.push(SetHidden(ctx, ["f_0002"], True))
    stack.push(DeleteFrames(ctx, ["f_0003"]))
    assert ctx.changes == 3
    stack.undo()
    stack.undo()
    assert ctx.changes == 5

    on_disk = Project.open(ctx.project.path)
    assert [f.id for f in on_disk.frames] == ids(ctx)
    assert [f.holds for f in on_disk.frames] == [f.holds for f in ctx.project.frames]
    assert [f.hidden for f in on_disk.frames] == [f.hidden for f in ctx.project.frames]


def test_a_long_mixed_history_unwinds_exactly(ctx, stack):
    start = ids(ctx)
    stack.push(SetHolds(ctx, ["f_0002"], 5))
    stack.push(DeleteFrames(ctx, ["f_0001", "f_0004"]))
    stack.push(DuplicateFrames(ctx, ["f_0003"]))
    stack.push(ReorderFrames(ctx, ["f_0005"], 0))
    stack.push(SetHidden(ctx, ["f_0002"], True))
    stack.push(CaptureFrame(ctx, JPEG, {}, (1920, 1080)))

    for _ in range(6):
        stack.undo()

    assert ids(ctx) == start
    assert [f.holds for f in ctx.project.frames] == [1] * 5
    assert not any(f.hidden for f in ctx.project.frames)
    assert reload_ids(ctx) == start

    for _ in range(6):
        stack.redo()
    # The duplicate keeps the id it allocated on its first run (f_0006) and the
    # capture keeps f_0007: redo re-inserts the same frames, it does not mint
    # new ones, and no id is ever recycled.
    assert ids(ctx) == ["f_0005", "f_0002", "f_0003", "f_0006", "f_0007"]
    assert ctx.project.frame_by_id("f_0002").hidden is True
    assert ctx.project.frame_by_id("f_0002").holds == 5


def test_undo_limit_is_the_uis_business_not_the_commands(stack):
    assert stack.undoLimit() == 200
