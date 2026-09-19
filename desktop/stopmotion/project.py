"""Project model: the manifest, the frame store and the timing maths.

The manifest (``project.json``) is the durable truth of a shoot.  It is
rewritten *atomically* after every mutation: a temporary file in the same
directory, fsynced, then ``os.replace``.  A crash mid-shoot can therefore lose
at most the mutation in flight, never the hours of work behind it.

JPEG files are immutable once written.  Deleting a frame removes the manifest
entry only; the file is orphaned but left on disk.  :meth:`Project.purge_unreferenced`
is the one and only thing in this codebase that erases pixels.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "ProjectError",
    "Frame",
    "Crop",
    "Project",
    "utc_now",
]

SCHEMA_VERSION = 1

MANIFEST_NAME = "project.json"
FRAMES_DIR = "frames"

_ID_RE = re.compile(r"^f_(\d+)$")
_ASPECT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[:/xX]\s*(\d+(?:\.\d+)?)\s*$")


class ProjectError(Exception):
    """Raised for unreadable, malformed or unsupported project folders."""


def utc_now() -> str:
    """Timestamp in the manifest's format: UTC, second resolution, ``Z`` suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _even(value: int) -> int:
    """Largest even integer <= value, floored at 2 (H.264 wants even dimensions)."""
    value = int(value)
    if value < 2:
        return 2
    return value - (value % 2)


def _even_offset(value: int) -> int:
    """Largest even integer <= value, floored at 0 (chroma siting wants even offsets)."""
    value = int(value)
    if value < 0:
        return 0
    return value - (value % 2)


# --------------------------------------------------------------------------- #
# Frame
# --------------------------------------------------------------------------- #

@dataclass
class Frame:
    """One captured still.

    ``file`` is relative to the project directory (``frames/f_0001.jpg``).
    ``missing`` is a runtime-only flag set by :meth:`Project.open` when the
    manifest references a JPEG that is not on disk; it is never persisted.
    """

    id: str
    file: str
    holds: int = 1
    hidden: bool = False
    captured: str = ""
    settings: dict = field(default_factory=dict)
    width: int = 0
    height: int = 0
    missing: bool = False

    def to_json(self) -> dict:
        d: dict = {
            "id": self.id,
            "file": self.file,
            "holds": int(self.holds),
            "hidden": bool(self.hidden),
            "captured": self.captured,
            "settings": dict(self.settings),
        }
        if self.width and self.height:
            d["width"] = int(self.width)
            d["height"] = int(self.height)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Frame":
        try:
            fid = str(d["id"])
            file = str(d["file"])
        except (KeyError, TypeError) as exc:
            raise ProjectError(f"frame entry missing required key: {exc}") from exc
        holds = int(d.get("holds", 1))
        if holds < 1:
            holds = 1
        return cls(
            id=fid,
            file=file,
            holds=holds,
            hidden=bool(d.get("hidden", False)),
            captured=str(d.get("captured", "")),
            settings=dict(d.get("settings") or {}),
            width=int(d.get("width", 0) or 0),
            height=int(d.get("height", 0) or 0),
        )

    def copy(self) -> "Frame":
        return Frame(
            id=self.id,
            file=self.file,
            holds=self.holds,
            hidden=self.hidden,
            captured=self.captured,
            settings=dict(self.settings),
            width=self.width,
            height=self.height,
            missing=self.missing,
        )


# --------------------------------------------------------------------------- #
# Crop
# --------------------------------------------------------------------------- #

@dataclass
class Crop:
    """A crop rectangle in sensor pixels."""

    aspect: str
    x: int
    y: int
    w: int
    h: int

    @staticmethod
    def parse_aspect(aspect: str) -> float:
        m = _ASPECT_RE.match(str(aspect))
        if not m:
            raise ValueError(f"unparseable aspect ratio: {aspect!r}")
        num, den = float(m.group(1)), float(m.group(2))
        if num <= 0 or den <= 0:
            raise ValueError(f"aspect ratio must be positive: {aspect!r}")
        return num / den

    @staticmethod
    def for_aspect(aspect: str, sw: int, sh: int) -> "Crop":
        """Largest rect of `aspect` that fits in `sw`x`sh`, centred.

        Width, height and both offsets are clamped to even pixels: H.264 needs
        even dimensions and yuv420p chroma siting wants even offsets.
        """
        ratio = Crop.parse_aspect(aspect)
        sw, sh = int(sw), int(sh)
        if sw < 2 or sh < 2:
            raise ValueError(f"sensor size too small: {sw}x{sh}")

        if sw / sh > ratio:
            # Sensor is wider than the target: height-limited.
            h = sh
            w = int(round(h * ratio))
        else:
            w = sw
            h = int(round(w / ratio))

        w = _even(min(w, sw))
        h = _even(min(h, sh))
        x = min(_even_offset((sw - w) // 2), _even_offset(sw - w))
        y = min(_even_offset((sh - h) // 2), _even_offset(sh - h))
        return Crop(aspect=str(aspect), x=x, y=y, w=w, h=h)

    def to_json(self) -> dict:
        return {"aspect": self.aspect, "x": int(self.x), "y": int(self.y),
                "w": int(self.w), "h": int(self.h)}

    @classmethod
    def from_json(cls, d: dict) -> "Crop":
        return cls(
            aspect=str(d.get("aspect", "")),
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            w=int(d["w"]),
            h=int(d["h"]),
        )


# --------------------------------------------------------------------------- #
# Project
# --------------------------------------------------------------------------- #

class Project:
    """A project folder: ``project.json`` plus ``frames/*.jpg``."""

    def __init__(
        self,
        path: Path | str,
        name: str,
        fps: int,
        capture_size: tuple[int, int],
        crop: Crop | None,
        frames: list[Frame] | None = None,
        created: str | None = None,
        next_id: int = 1,
    ) -> None:
        self.path = Path(path)
        self.name = name
        self.fps = int(fps) if int(fps) > 0 else 12
        self.capture_size: tuple[int, int] = (int(capture_size[0]), int(capture_size[1]))
        self.crop: Crop | None = crop
        self.frames: list[Frame] = list(frames or [])
        self.created: str = created or utc_now()
        self.dirty: bool = False
        self._next_id: int = max(1, int(next_id))

    # -- construction ------------------------------------------------------ #

    @classmethod
    def create(
        cls,
        path: Path | str,
        name: str,
        fps: int,
        capture_size: tuple[int, int],
        crop: Crop | str | None = None,
    ) -> "Project":
        """Create the folder structure and write an empty manifest."""
        path = Path(path)
        if isinstance(crop, str):
            crop = Crop.for_aspect(crop, capture_size[0], capture_size[1])
        proj = cls(path=path, name=name, fps=fps, capture_size=capture_size, crop=crop)
        path.mkdir(parents=True, exist_ok=True)
        proj.frames_dir.mkdir(parents=True, exist_ok=True)
        proj.save()
        return proj

    @classmethod
    def open(cls, path: Path | str) -> "Project":
        """Open an existing project folder, validating and migrating the manifest.

        A manifest that references a JPEG which is not on disk does not raise:
        the frame is kept and flagged ``missing`` so the UI can grey it out.
        """
        path = Path(path)
        manifest = path / MANIFEST_NAME if path.is_dir() else path
        if manifest.name != MANIFEST_NAME:
            manifest = path / MANIFEST_NAME
        root = manifest.parent

        if not manifest.exists():
            raise ProjectError(f"no {MANIFEST_NAME} in {root}")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(f"cannot read {manifest}: {exc}") from exc
        if not isinstance(data, dict):
            raise ProjectError(f"{manifest} is not a JSON object")

        data = cls._migrate(data)

        capture = data.get("capture") or {}
        capture_size = (int(capture.get("width", 0) or 0), int(capture.get("height", 0) or 0))

        crop_raw = data.get("crop")
        crop = Crop.from_json(crop_raw) if isinstance(crop_raw, dict) else None

        frames_raw = data.get("frames", [])
        if not isinstance(frames_raw, list):
            raise ProjectError("manifest 'frames' must be a list")
        frames = [Frame.from_json(f) for f in frames_raw]

        seen: set[str] = set()
        for f in frames:
            if f.id in seen:
                raise ProjectError(f"duplicate frame id in manifest: {f.id}")
            seen.add(f.id)
            f.missing = not (root / f.file).exists()

        highest = 0
        for f in frames:
            m = _ID_RE.match(f.id)
            if m:
                highest = max(highest, int(m.group(1)))
        next_id = max(int(data.get("next_id", 0) or 0), highest + 1, 1)

        proj = cls(
            path=root,
            name=str(data.get("name") or root.name),
            fps=int(data.get("fps", 12) or 12),
            capture_size=capture_size,
            crop=crop,
            frames=frames,
            created=str(data.get("created") or ""),
            next_id=next_id,
        )
        return proj

    @staticmethod
    def _migrate(data: dict) -> dict:
        """Bring a manifest up to :data:`SCHEMA_VERSION`."""
        raw = data.get("version", 1)
        try:
            version = int(raw)
        except (TypeError, ValueError):
            raise ProjectError(f"manifest has a non-numeric version: {raw!r}")
        if version < 1:
            raise ProjectError(f"manifest version {version} is not valid")
        if version > SCHEMA_VERSION:
            raise ProjectError(
                f"project was written by a newer version of the app "
                f"(manifest version {version}, this build understands {SCHEMA_VERSION})"
            )
        # v1 is current; future migrations chain here.
        data["version"] = SCHEMA_VERSION
        return data

    # -- paths ------------------------------------------------------------- #

    @property
    def frames_dir(self) -> Path:
        return self.path / FRAMES_DIR

    @property
    def manifest_path(self) -> Path:
        return self.path / MANIFEST_NAME

    def frame_path(self, frame: Frame) -> Path:
        return self.path / frame.file

    # -- persistence ------------------------------------------------------- #

    def to_json(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "name": self.name,
            "fps": int(self.fps),
            "created": self.created,
            "next_id": int(self._next_id),
            "capture": {"width": int(self.capture_size[0]), "height": int(self.capture_size[1])},
            "crop": self.crop.to_json() if self.crop else None,
            "frames": [f.to_json() for f in self.frames],
        }

    def save(self) -> None:
        """Write the manifest atomically: temp file in the same dir + os.replace."""
        self.path.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_json(), indent=2, ensure_ascii=False) + "\n"
        _atomic_write(self.manifest_path, payload.encode("utf-8"))
        self.dirty = False

    # -- frames ------------------------------------------------------------ #

    def allocate_id(self) -> str:
        """Next frame id.  Monotonic; never reused, even after a delete."""
        fid = f"f_{self._next_id:04d}"
        self._next_id += 1
        return fid

    def add_frame(
        self,
        jpeg: bytes,
        settings: dict,
        size: tuple[int, int] | None = None,
    ) -> Frame:
        """Write a JPEG into ``frames/`` and append a manifest entry.

        The file is written atomically too, so a half-written JPEG can never be
        referenced by a saved manifest.
        """
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        fid = self.allocate_id()
        rel = f"{FRAMES_DIR}/{fid}.jpg"
        _atomic_write(self.path / rel, bytes(jpeg))

        w, h = (int(size[0]), int(size[1])) if size else (0, 0)
        if w and h and self.capture_size == (0, 0):
            self.capture_size = (w, h)

        frame = Frame(
            id=fid,
            file=rel,
            holds=1,
            hidden=False,
            captured=utc_now(),
            settings=dict(settings or {}),
            width=w,
            height=h,
        )
        self.frames.append(frame)
        self.save()
        return frame

    def frame_by_id(self, frame_id: str) -> Frame | None:
        for f in self.frames:
            if f.id == frame_id:
                return f
        return None

    def index_of(self, frame_id: str) -> int:
        """Index in the timeline, or ``-1`` if the id is unknown."""
        for i, f in enumerate(self.frames):
            if f.id == frame_id:
                return i
        return -1

    def insert_frame(self, index: int, frame: Frame) -> None:
        index = max(0, min(int(index), len(self.frames)))
        self.frames.insert(index, frame)

    def remove_frame(self, frame_id: str) -> tuple[int, Frame] | None:
        """Remove a manifest entry.  The JPEG on disk is *not* touched."""
        idx = self.index_of(frame_id)
        if idx < 0:
            return None
        return idx, self.frames.pop(idx)

    def visible_frames(self) -> list[Frame]:
        return [f for f in self.frames if not f.hidden]

    # -- timing ------------------------------------------------------------ #

    def total_holds(self, visible_only: bool = True) -> int:
        src = self.visible_frames() if visible_only else self.frames
        return sum(max(1, int(f.holds)) for f in src)

    def duration_ms(self, visible_only: bool = True) -> int:
        """Runtime in milliseconds for the given set of frames at the project fps."""
        if self.fps <= 0:
            return 0
        return int(round(self.total_holds(visible_only) * 1000.0 / self.fps))

    # -- housekeeping ------------------------------------------------------- #

    def purge_unreferenced(self) -> list[Path]:
        """Delete JPEGs in ``frames/`` that no manifest entry references.

        This is irreversible and is the only code path that unlinks pixels.
        """
        if not self.frames_dir.is_dir():
            return []
        referenced = {(self.path / f.file).resolve() for f in self.frames}
        deleted: list[Path] = []
        for candidate in sorted(self.frames_dir.iterdir()):
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in (".jpg", ".jpeg"):
                continue
            if candidate.resolve() in referenced:
                continue
            try:
                candidate.unlink()
            except OSError:
                continue
            deleted.append(candidate)
        return deleted


# --------------------------------------------------------------------------- #
# atomic write
# --------------------------------------------------------------------------- #

def _atomic_write(target: Path, data: bytes) -> None:
    """Write `data` to `target` atomically, leaving no temp litter behind."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    # Durability of the rename itself.
    try:
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
