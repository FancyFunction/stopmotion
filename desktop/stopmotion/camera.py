"""Camera capability and settings model.

Pure stdlib: no Qt import, so the fake phone and the tests can use it too.

The desktop is the source of truth for camera settings (Plan.md section 2). The
phone reports what it can do in HELLO; everything the desktop sends is clamped
into those limits first, and the phone echoes back what it actually applied in
CONFIG_ACK.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

__all__ = [
    "Range",
    "CameraCapabilities",
    "CameraSettings",
    "AWB_MODES",
    "DEFAULT_EXPOSURE_NS",
    "DEFAULT_PREVIEW_SIZE",
    "DEFAULT_PREVIEW_QUALITY",
]

#: The Camera2 CONTROL_AWB_MODE presets, in the platform's own order.
AWB_MODES = [
    "OFF",
    "AUTO",
    "INCANDESCENT",
    "FLUORESCENT",
    "WARM_FLUORESCENT",
    "DAYLIGHT",
    "CLOUDY_DAYLIGHT",
    "TWILIGHT",
    "SHADE",
]

#: 1/60 s -- a safe hand-held-free shutter for a static rig under mains light.
DEFAULT_EXPOSURE_NS = 16_666_667
DEFAULT_ISO = 100
DEFAULT_AWB_MODE = "DAYLIGHT"
DEFAULT_PREVIEW_SIZE = (1920, 1080)
DEFAULT_PREVIEW_QUALITY = 70

#: Fallbacks used when HELLO omits a field entirely.
_FALLBACK_EXPOSURE = (100_000.0, 500_000_000.0)      # 1/10000 s .. 1/2 s
_FALLBACK_ISO = (100.0, 1600.0)
_FALLBACK_FOCUS = (0.0, 10.0)
_FALLBACK_SENSOR = (1920, 1080)


def _clamp(value: float, lo: float, hi: float) -> float:
    if lo > hi:
        lo, hi = hi, lo
    return lo if value < lo else (hi if value > hi else value)


@dataclass(frozen=True)
class Range:
    """An inclusive numeric range as reported by the device."""

    lo: float
    hi: float

    def __post_init__(self) -> None:
        lo, hi = float(self.lo), float(self.hi)
        if hi < lo:  # tolerate a device reporting them the wrong way round
            lo, hi = hi, lo
        object.__setattr__(self, "lo", lo)
        object.__setattr__(self, "hi", hi)

    def clamp(self, value: float) -> float:
        return _clamp(float(value), self.lo, self.hi)

    def mid(self) -> float:
        return (self.lo + self.hi) / 2.0

    def contains(self, value: float) -> bool:
        return self.lo <= float(value) <= self.hi

    @property
    def span(self) -> float:
        return self.hi - self.lo

    def to_json(self) -> dict:
        return {"min": self.lo, "max": self.hi}

    @classmethod
    def parse(cls, value, fallback: tuple[float, float]) -> "Range":
        """Tolerant parser.

        Accepts ``{"min":x,"max":y}``, ``{"lo":x,"hi":y}``, ``[x, y]`` or a bare
        number (a degenerate range). Anything unusable yields ``fallback``.
        """
        lo = hi = None
        if isinstance(value, dict):
            for key in ("min", "lo", "low", "lower", "start"):
                if key in value:
                    lo = value[key]
                    break
            for key in ("max", "hi", "high", "upper", "end"):
                if key in value:
                    hi = value[key]
                    break
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            lo, hi = value[0], value[1]
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            lo = hi = value

        try:
            if lo is None or hi is None:
                raise TypeError
            return cls(float(lo), float(hi))
        except (TypeError, ValueError):
            return cls(float(fallback[0]), float(fallback[1]))


def _parse_size(value, fallback: tuple[int, int] | None = None) -> tuple[int, int] | None:
    """Accept ``[w,h]``, ``{"width":w,"height":h}`` or ``"1920x1080"``."""
    w = h = None
    if isinstance(value, dict):
        w = value.get("width", value.get("w"))
        h = value.get("height", value.get("h"))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        w, h = value[0], value[1]
    elif isinstance(value, str) and "x" in value.lower():
        parts = value.lower().split("x")
        if len(parts) == 2:
            w, h = parts[0].strip(), parts[1].strip()

    try:
        if w is None or h is None:
            raise TypeError
        size = (int(w), int(h))
    except (TypeError, ValueError):
        return fallback
    if size[0] <= 0 or size[1] <= 0:
        return fallback
    return size


@dataclass(frozen=True)
class CameraCapabilities:
    """What the phone told us it can do, parsed out of HELLO."""

    hardware_level: str
    sensor_size: tuple[int, int]
    preview_sizes: list[tuple[int, int]]
    exposure_ns: Range
    iso: Range
    focus_diopters: Range
    focus_calibration: str  # CALIBRATED | APPROXIMATE | UNCALIBRATED
    awb_modes: list[str]

    @property
    def focus_is_absolute(self) -> bool:
        """True when focus distance is a meaningful diopter value."""
        return self.focus_calibration.upper() in ("CALIBRATED", "APPROXIMATE")

    @classmethod
    def from_hello(cls, hello: dict) -> "CameraCapabilities":
        """Parse a HELLO header.

        Deliberately forgiving: a missing or malformed field falls back to a
        conservative default rather than raising, because a phone that cannot be
        parsed is worse than a phone with slightly wrong sliders.
        """
        hello = hello if isinstance(hello, dict) else {}

        hardware_level = str(hello.get("hardware_level") or "UNKNOWN").upper()

        sensor = hello.get("sensor_size", hello.get("sensor"))
        if sensor is None and ("sensor_width" in hello or "sensor_height" in hello):
            sensor = [hello.get("sensor_width"), hello.get("sensor_height")]
        sensor_size = _parse_size(sensor, _FALLBACK_SENSOR) or _FALLBACK_SENSOR

        preview_sizes: list[tuple[int, int]] = []
        raw_sizes = hello.get("preview_sizes")
        if isinstance(raw_sizes, (list, tuple)):
            for entry in raw_sizes:
                size = _parse_size(entry)
                if size is not None and size not in preview_sizes:
                    preview_sizes.append(size)
        preview_sizes.sort(key=lambda s: s[0] * s[1], reverse=True)

        awb_modes: list[str] = []
        raw_awb = hello.get("awb_modes")
        if isinstance(raw_awb, (list, tuple)):
            for mode in raw_awb:
                text = str(mode).upper()
                if text and text not in awb_modes:
                    awb_modes.append(text)
        if not awb_modes:
            awb_modes = ["AUTO"]

        calibration = str(
            hello.get("focus_calibration", hello.get("focus_distance_calibration"))
            or "UNCALIBRATED"
        ).upper()

        return cls(
            hardware_level=hardware_level,
            sensor_size=sensor_size,
            preview_sizes=preview_sizes,
            exposure_ns=Range.parse(hello.get("exposure_ns"), _FALLBACK_EXPOSURE),
            iso=Range.parse(hello.get("iso"), _FALLBACK_ISO),
            focus_diopters=Range.parse(hello.get("focus_diopters"), _FALLBACK_FOCUS),
            focus_calibration=calibration,
            awb_modes=awb_modes,
        )

    def to_hello(self) -> dict:
        """Inverse of :meth:`from_hello` (used by tools/fake_phone.py)."""
        return {
            "hardware_level": self.hardware_level,
            "sensor_size": list(self.sensor_size),
            "preview_sizes": [list(s) for s in self.preview_sizes],
            "exposure_ns": self.exposure_ns.to_json(),
            "iso": self.iso.to_json(),
            "focus_diopters": self.focus_diopters.to_json(),
            "focus_calibration": self.focus_calibration,
            "awb_modes": list(self.awb_modes),
        }

    def nearest_preview_size(self, width: int, height: int) -> tuple[int, int]:
        """Snap a requested preview size onto something the device offers.

        Distance is measured on pixel count in log space (i.e. by ratio, not by
        difference) so that "half the pixels" and "twice the pixels" are treated
        as equally far away; aspect ratio breaks ties.
        """
        if not self.preview_sizes:
            return (max(1, int(width)), max(1, int(height)))
        want_w, want_h = max(1, int(width)), max(1, int(height))
        want_area = math.log(want_w * want_h)
        want_aspect = want_w / want_h
        return min(
            self.preview_sizes,
            key=lambda s: (
                abs(math.log(s[0] * s[1]) - want_area),
                abs((s[0] / s[1]) - want_aspect),
            ),
        )

    def pick_awb(self, requested: str | None) -> str:
        """The requested AWB mode if supported, else the best available."""
        if requested:
            text = str(requested).upper()
            if text in self.awb_modes:
                return text
        if DEFAULT_AWB_MODE in self.awb_modes:
            return DEFAULT_AWB_MODE
        return self.awb_modes[0] if self.awb_modes else DEFAULT_AWB_MODE


@dataclass
class CameraSettings:
    """A full set of camera settings -- exactly what SET_CONFIG carries."""

    exposure_ns: int
    iso: int
    focus_diopters: float
    awb_mode: str
    ae_lock: bool
    af_lock: bool
    awb_lock: bool
    preview_width: int
    preview_height: int
    preview_quality: int

    def to_json(self) -> dict:
        return {
            "exposure_ns": int(self.exposure_ns),
            "iso": int(self.iso),
            "focus_diopters": float(self.focus_diopters),
            "awb_mode": str(self.awb_mode),
            "ae_lock": bool(self.ae_lock),
            "af_lock": bool(self.af_lock),
            "awb_lock": bool(self.awb_lock),
            "preview_width": int(self.preview_width),
            "preview_height": int(self.preview_height),
            "preview_quality": int(self.preview_quality),
        }

    @classmethod
    def from_json(cls, d: dict) -> "CameraSettings":
        """Tolerant inverse of :meth:`to_json`.

        Missing keys fall back to the module defaults, so a frame's stored
        ``settings`` sub-dict (exposure/iso/focus/awb only, per project.json)
        round-trips through here as well.
        """
        d = d if isinstance(d, dict) else {}
        locks = d.get("locks") if isinstance(d.get("locks"), dict) else {}

        def _bool(key: str, alt: str) -> bool:
            if key in d:
                return bool(d[key])
            if alt in locks:
                return bool(locks[alt])
            return True

        def _num(key, default, cast):
            try:
                return cast(d[key])
            except (KeyError, TypeError, ValueError):
                return default

        size = _parse_size(d.get("preview_size"), None)
        width = _num("preview_width", size[0] if size else DEFAULT_PREVIEW_SIZE[0], int)
        height = _num("preview_height", size[1] if size else DEFAULT_PREVIEW_SIZE[1], int)

        return cls(
            exposure_ns=_num("exposure_ns", DEFAULT_EXPOSURE_NS, int),
            iso=_num("iso", DEFAULT_ISO, int),
            focus_diopters=_num("focus_diopters", 0.0, float),
            awb_mode=str(d.get("awb_mode") or DEFAULT_AWB_MODE).upper(),
            ae_lock=_bool("ae_lock", "ae"),
            af_lock=_bool("af_lock", "af"),
            awb_lock=_bool("awb_lock", "awb"),
            preview_width=width,
            preview_height=height,
            preview_quality=_num("preview_quality", DEFAULT_PREVIEW_QUALITY, int),
        )

    def clamped(self, caps: CameraCapabilities) -> "CameraSettings":
        """A copy with every value inside what the device actually supports."""
        width, height = caps.nearest_preview_size(self.preview_width, self.preview_height)
        return replace(
            self,
            exposure_ns=int(round(caps.exposure_ns.clamp(self.exposure_ns))),
            iso=int(round(caps.iso.clamp(self.iso))),
            focus_diopters=float(caps.focus_diopters.clamp(self.focus_diopters)),
            awb_mode=caps.pick_awb(self.awb_mode),
            preview_width=width,
            preview_height=height,
            preview_quality=int(_clamp(int(self.preview_quality), 1, 100)),
        )

    @classmethod
    def defaults(cls, caps: CameraCapabilities) -> "CameraSettings":
        """Sensible stopmotion starting point for this device.

        Everything locked: on a static rig, drift in exposure/focus/white
        balance between frames is the one artefact that ruins a shot, and it is
        far more damaging than a slightly wrong absolute value.
        """
        # Low-ish ISO: on a static rig noise costs more than shutter speed does.
        # Aim at ISO 100 but never above the bottom quarter of the device range.
        iso_ceiling = caps.iso.lo + 0.25 * caps.iso.span
        iso = caps.iso.clamp(min(float(DEFAULT_ISO), max(caps.iso.lo, iso_ceiling)))

        settings = cls(
            exposure_ns=DEFAULT_EXPOSURE_NS,
            iso=int(round(iso)),
            focus_diopters=caps.focus_diopters.mid(),
            awb_mode=caps.pick_awb(DEFAULT_AWB_MODE),
            ae_lock=True,
            af_lock=True,
            awb_lock=True,
            preview_width=DEFAULT_PREVIEW_SIZE[0],
            preview_height=DEFAULT_PREVIEW_SIZE[1],
            preview_quality=DEFAULT_PREVIEW_QUALITY,
        )
        return settings.clamped(caps)
