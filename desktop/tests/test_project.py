"""Tests for the project model, manifest I/O and timing maths."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stopmotion.project import (  # noqa: E402
    SCHEMA_VERSION,
    Crop,
    Frame,
    Project,
    ProjectError,
)

JPEG = b"\xff\xd8\xff\xe0" + b"stopmotion-test-jpeg" + b"\xff\xd9"


def make_project(tmp_path: Path, fps: int = 12, crop: Crop | str | None = "16:9") -> Project:
    return Project.create(tmp_path / "film", "my-film", fps, (4032, 3024), crop)


def add(project: Project, n: int = 1) -> list[Frame]:
    return [project.add_frame(JPEG, {"iso": 200}, (4032, 3024)) for _ in range(n)]


# --------------------------------------------------------------------------- #
# Crop
# --------------------------------------------------------------------------- #

def test_crop_for_aspect_centres_16_9_in_4_3_sensor():
    crop = Crop.for_aspect("16:9", 4032, 3024)
    assert (crop.w, crop.h) == (4032, 2268)
    assert crop.x == 0
    assert crop.y == 378          # (3024 - 2268) / 2
    assert crop.aspect == "16:9"


def test_crop_for_aspect_is_height_limited_on_a_wide_sensor():
    crop = Crop.for_aspect("1:1", 1920, 1080)
    assert (crop.w, crop.h) == (1080, 1080)
    assert crop.x == 420
    assert crop.y == 0


@pytest.mark.parametrize(
    "aspect,sw,sh",
    [("16:9", 4033, 3025), ("4:3", 1001, 1001), ("2.35:1", 4000, 3000), ("9:16", 3000, 4001)],
)
def test_crop_dimensions_and_offsets_are_always_even(aspect, sw, sh):
    crop = Crop.for_aspect(aspect, sw, sh)
    assert crop.w % 2 == 0 and crop.h % 2 == 0
    assert crop.x % 2 == 0 and crop.y % 2 == 0
    assert crop.x + crop.w <= sw
    assert crop.y + crop.h <= sh


def test_crop_rejects_nonsense_aspect():
    with pytest.raises(ValueError):
        Crop.for_aspect("banana", 1920, 1080)


# --------------------------------------------------------------------------- #
# create / save / open round-trip
# --------------------------------------------------------------------------- #

def test_create_lays_out_the_folder_and_manifest(tmp_path):
    project = make_project(tmp_path)
    assert project.manifest_path.exists()
    assert project.frames_dir.is_dir()
    data = json.loads(project.manifest_path.read_text())
    assert data["version"] == SCHEMA_VERSION
    assert data["name"] == "my-film"
    assert data["fps"] == 12
    assert data["capture"] == {"width": 4032, "height": 3024}
    assert data["crop"]["aspect"] == "16:9"
    assert data["frames"] == []


def test_round_trip_through_disk(tmp_path):
    project = make_project(tmp_path)
    frames = add(project, 3)
    frames[1].holds = 4
    frames[2].hidden = True
    project.save()

    reopened = Project.open(project.path)
    assert reopened.name == "my-film"
    assert reopened.fps == 12
    assert reopened.capture_size == (4032, 3024)
    assert reopened.crop is not None and reopened.crop.aspect == "16:9"
    assert [f.id for f in reopened.frames] == ["f_0001", "f_0002", "f_0003"]
    assert [f.holds for f in reopened.frames] == [1, 4, 1]
    assert [f.hidden for f in reopened.frames] == [False, False, True]
    assert reopened.frames[0].settings == {"iso": 200}
    assert all(not f.missing for f in reopened.frames)
    assert reopened.frame_path(reopened.frames[0]).read_bytes() == JPEG


def test_open_accepts_the_manifest_path_as_well_as_the_folder(tmp_path):
    project = make_project(tmp_path)
    add(project, 1)
    assert len(Project.open(project.manifest_path).frames) == 1


def test_open_without_a_manifest_raises(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ProjectError):
        Project.open(tmp_path / "empty")


def test_open_rejects_a_future_schema_version(tmp_path):
    project = make_project(tmp_path)
    data = json.loads(project.manifest_path.read_text())
    data["version"] = SCHEMA_VERSION + 7
    project.manifest_path.write_text(json.dumps(data))
    with pytest.raises(ProjectError) as exc:
        Project.open(project.path)
    assert "newer version" in str(exc.value)


def test_open_migrates_a_manifest_with_no_version_field(tmp_path):
    project = make_project(tmp_path)
    add(project, 1)
    data = json.loads(project.manifest_path.read_text())
    del data["version"]
    project.manifest_path.write_text(json.dumps(data))

    reopened = Project.open(project.path)
    assert len(reopened.frames) == 1
    reopened.save()
    assert json.loads(reopened.manifest_path.read_text())["version"] == SCHEMA_VERSION


def test_open_rejects_a_corrupt_manifest(tmp_path):
    project = make_project(tmp_path)
    project.manifest_path.write_text("{not json at all")
    with pytest.raises(ProjectError):
        Project.open(project.path)


# --------------------------------------------------------------------------- #
# atomic writes
# --------------------------------------------------------------------------- #

def test_save_leaves_no_temp_litter(tmp_path):
    project = make_project(tmp_path)
    add(project, 3)
    for _ in range(5):
        project.save()
    stray = [p.name for p in project.path.iterdir() if p.name != "project.json" and p.is_file()]
    assert stray == []
    stray_frames = [p.name for p in project.frames_dir.iterdir() if not p.name.endswith(".jpg")]
    assert stray_frames == []


def test_save_is_atomic_via_replace(tmp_path, monkeypatch):
    """If the replace fails the old manifest survives intact and no temp is left."""
    project = make_project(tmp_path)
    add(project, 2)
    good = project.manifest_path.read_bytes()

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated power cut")

    monkeypatch.setattr(os, "replace", boom)
    project.name = "half-written"
    with pytest.raises(OSError):
        project.save()
    monkeypatch.setattr(os, "replace", real_replace)

    assert project.manifest_path.read_bytes() == good
    assert [p.name for p in project.path.iterdir() if p.is_file()] == ["project.json"]


def test_add_frame_writes_the_jpeg_and_updates_the_manifest(tmp_path):
    project = make_project(tmp_path)
    frame = project.add_frame(JPEG, {"iso": 400}, (4032, 3024))
    assert frame.file == "frames/f_0001.jpg"
    assert project.frame_path(frame).read_bytes() == JPEG
    assert json.loads(project.manifest_path.read_text())["frames"][0]["id"] == "f_0001"
    assert frame.captured.endswith("Z")


# --------------------------------------------------------------------------- #
# ids
# --------------------------------------------------------------------------- #

def test_ids_are_sequential_and_zero_padded(tmp_path):
    project = make_project(tmp_path)
    assert [f.id for f in add(project, 3)] == ["f_0001", "f_0002", "f_0003"]


def test_ids_are_never_reused_after_deletion(tmp_path):
    project = make_project(tmp_path)
    add(project, 3)
    project.remove_frame("f_0003")
    project.remove_frame("f_0002")
    project.save()

    assert project.add_frame(JPEG, {}, None).id == "f_0004"


def test_ids_are_never_reused_across_a_reopen(tmp_path):
    """The high-water mark survives deleting the newest frame and closing."""
    project = make_project(tmp_path)
    add(project, 3)
    project.remove_frame("f_0003")
    project.save()

    reopened = Project.open(project.path)
    assert reopened.add_frame(JPEG, {}, None).id == "f_0004"
    assert reopened.add_frame(JPEG, {}, None).id == "f_0005"


def test_open_rejects_duplicate_ids(tmp_path):
    project = make_project(tmp_path)
    add(project, 1)
    data = json.loads(project.manifest_path.read_text())
    data["frames"].append(dict(data["frames"][0]))
    project.manifest_path.write_text(json.dumps(data))
    with pytest.raises(ProjectError):
        Project.open(project.path)


# --------------------------------------------------------------------------- #
# missing files
# --------------------------------------------------------------------------- #

def test_open_tolerates_a_hand_deleted_jpeg(tmp_path):
    project = make_project(tmp_path)
    frames = add(project, 3)
    project.frame_path(frames[1]).unlink()

    reopened = Project.open(project.path)
    assert len(reopened.frames) == 3
    assert [f.missing for f in reopened.frames] == [False, True, False]
    # The entry survives a resave; `missing` is runtime state, not manifest state.
    reopened.save()
    assert "missing" not in json.loads(reopened.manifest_path.read_text())["frames"][1]


# --------------------------------------------------------------------------- #
# delete / purge semantics
# --------------------------------------------------------------------------- #

def test_remove_frame_never_unlinks_the_jpeg(tmp_path):
    project = make_project(tmp_path)
    frames = add(project, 2)
    path = project.frame_path(frames[0])
    project.remove_frame(frames[0].id)
    project.save()
    assert path.exists(), "delete must orphan the file, never erase it"
    assert len(Project.open(project.path).frames) == 1


def test_purge_unreferenced_deletes_only_orphans(tmp_path):
    project = make_project(tmp_path)
    frames = add(project, 3)
    orphan = project.frame_path(frames[1])
    kept = [project.frame_path(frames[0]), project.frame_path(frames[2])]
    project.remove_frame(frames[1].id)
    project.save()

    deleted = project.purge_unreferenced()
    assert deleted == [orphan]
    assert not orphan.exists()
    assert all(p.exists() for p in kept)
    assert project.purge_unreferenced() == []


def test_purge_keeps_a_file_shared_by_two_entries(tmp_path):
    """Duplicated frames share one immutable JPEG; purging must respect that."""
    project = make_project(tmp_path)
    original = add(project, 1)[0]
    clone = original.copy()
    clone.id = project.allocate_id()
    project.insert_frame(1, clone)
    project.save()

    project.remove_frame(original.id)
    project.save()
    assert project.purge_unreferenced() == []
    assert project.frame_path(clone).exists()


# --------------------------------------------------------------------------- #
# timing
# --------------------------------------------------------------------------- #

def test_duration_with_holds(tmp_path):
    project = make_project(tmp_path, fps=12)
    frames = add(project, 4)
    for frame, holds in zip(frames, (1, 3, 2, 6)):
        frame.holds = holds
    assert project.total_holds() == 12
    assert project.duration_ms() == 1000
    assert project.duration_ms(visible_only=False) == 1000


def test_hidden_frames_count_for_total_runtime_only(tmp_path):
    project = make_project(tmp_path, fps=10)
    frames = add(project, 5)
    for frame in frames:
        frame.holds = 2
    frames[1].hidden = True
    frames[3].hidden = True

    assert [f.id for f in project.visible_frames()] == ["f_0001", "f_0003", "f_0005"]
    assert project.duration_ms(visible_only=True) == 600     # 3 frames * 2 holds @ 10fps
    assert project.duration_ms(visible_only=False) == 1000   # 5 frames * 2 holds
    assert len(project.frames) == 5, "hidden frames stay in the array"


def test_duration_rounds_to_the_nearest_millisecond(tmp_path):
    project = make_project(tmp_path, fps=12)
    add(project, 1)
    assert project.duration_ms() == 83     # 1/12 s
    add(project, 1)
    assert project.duration_ms() == 167    # 2/12 s


def test_index_of_and_frame_by_id(tmp_path):
    project = make_project(tmp_path)
    add(project, 3)
    assert project.index_of("f_0002") == 1
    assert project.index_of("f_9999") == -1
    assert project.frame_by_id("f_0002").id == "f_0002"
    assert project.frame_by_id("nope") is None


def test_project_with_no_crop_round_trips(tmp_path):
    project = make_project(tmp_path, crop=None)
    add(project, 1)
    assert Project.open(project.path).crop is None
