"""Application preferences, backed by QSettings.

Thin typed wrapper so the rest of the UI never touches raw QSettings keys or
has to think about the string/bool/int coercion QSettings does on the INI
backend.  Everything here is desktop-side only: the phone never sees it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QSettings

ORGANISATION = "kruse"
APPLICATION = "stopmotion"

MAX_RECENT = 10


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class AppSettings:
    """Preferences store.  Construct one per process (MainWindow owns it)."""

    def __init__(self, settings: QSettings | None = None) -> None:
        self._s = settings if settings is not None else QSettings(ORGANISATION, APPLICATION)

    # -- plumbing ---------------------------------------------------------

    @property
    def backend(self) -> QSettings:
        return self._s

    def sync(self) -> None:
        self._s.sync()

    # -- recent projects --------------------------------------------------

    def recent_projects(self) -> list[str]:
        raw = self._s.value("recent/projects", [])
        if isinstance(raw, str):
            raw = [raw]
        elif raw is None:
            raw = []
        out: list[str] = []
        for item in raw:
            text = str(item)
            if text and text not in out:
                out.append(text)
        return out

    def add_recent_project(self, path: str | Path) -> None:
        text = str(Path(path).expanduser().resolve())
        items = [p for p in self.recent_projects() if p != text]
        items.insert(0, text)
        self._s.setValue("recent/projects", items[:MAX_RECENT])

    def forget_recent_project(self, path: str | Path) -> None:
        text = str(path)
        items = [p for p in self.recent_projects() if p != text]
        self._s.setValue("recent/projects", items)

    def clear_recent_projects(self) -> None:
        self._s.setValue("recent/projects", [])

    def last_project_dir(self) -> str:
        return str(self._s.value("recent/last_dir", str(Path.home())))

    def set_last_project_dir(self, path: str | Path) -> None:
        self._s.setValue("recent/last_dir", str(path))

    # -- camera settings (desktop is the source of truth) -----------------

    def camera_settings(self) -> dict | None:
        raw = self._s.value("camera/settings", "")
        if not raw:
            return None
        try:
            data = json.loads(str(raw))
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def set_camera_settings(self, data: dict) -> None:
        try:
            self._s.setValue("camera/settings", json.dumps(data))
        except (TypeError, ValueError):
            pass

    # -- onion skin -------------------------------------------------------

    def onion_enabled(self) -> bool:
        return _as_bool(self._s.value("onion/enabled"), True)

    def set_onion_enabled(self, value: bool) -> None:
        self._s.setValue("onion/enabled", bool(value))

    def onion_depth(self) -> int:
        return max(1, min(3, _as_int(self._s.value("onion/depth"), 1)))

    def set_onion_depth(self, value: int) -> None:
        self._s.setValue("onion/depth", max(1, min(3, int(value))))

    def onion_opacity(self) -> float:
        return min(1.0, max(0.0, _as_float(self._s.value("onion/opacity"), 0.45)))

    def set_onion_opacity(self, value: float) -> None:
        self._s.setValue("onion/opacity", min(1.0, max(0.0, float(value))))

    # -- guides -----------------------------------------------------------

    def crop_guides(self) -> bool:
        return _as_bool(self._s.value("view/crop_guides"), True)

    def set_crop_guides(self, value: bool) -> None:
        self._s.setValue("view/crop_guides", bool(value))

    def loop_playback(self) -> bool:
        return _as_bool(self._s.value("view/loop"), True)

    def set_loop_playback(self, value: bool) -> None:
        self._s.setValue("view/loop", bool(value))

    # -- new project defaults --------------------------------------------

    def default_fps(self) -> int:
        return _as_int(self._s.value("project/fps"), 12)

    def set_default_fps(self, value: int) -> None:
        self._s.setValue("project/fps", int(value))

    def default_aspect(self) -> str:
        return str(self._s.value("project/aspect", "16:9"))

    def set_default_aspect(self, value: str) -> None:
        self._s.setValue("project/aspect", str(value))

    # -- export defaults --------------------------------------------------

    def export_resolution(self) -> str:
        return str(self._s.value("export/resolution", "1080p"))

    def set_export_resolution(self, value: str) -> None:
        self._s.setValue("export/resolution", str(value))

    def export_quality(self) -> str:
        return str(self._s.value("export/quality", "High"))

    def set_export_quality(self, value: str) -> None:
        self._s.setValue("export/quality", str(value))

    def export_dir(self) -> str:
        return str(self._s.value("export/dir", str(Path.home())))

    def set_export_dir(self, value: str | Path) -> None:
        self._s.setValue("export/dir", str(value))

    # -- window geometry --------------------------------------------------

    def save_window_state(self, geometry: QByteArray, state: QByteArray) -> None:
        self._s.setValue("window/geometry", geometry)
        self._s.setValue("window/state", state)

    def window_geometry(self) -> QByteArray | None:
        value = self._s.value("window/geometry")
        return value if isinstance(value, QByteArray) else None

    def window_state(self) -> QByteArray | None:
        value = self._s.value("window/state")
        return value if isinstance(value, QByteArray) else None
