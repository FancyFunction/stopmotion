"""Capability parsing and settings clamping."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stopmotion.camera import (  # noqa: E402
    DEFAULT_EXPOSURE_NS,
    CameraCapabilities,
    CameraSettings,
    Range,
)

HELLO = {
    "hardware_level": "LEVEL_3",
    "sensor_size": [4032, 3024],
    "preview_sizes": [[1280, 720], [1920, 1080], [640, 480]],
    "exposure_ns": {"min": 125_000, "max": 250_000_000},
    "iso": {"min": 50, "max": 3200},
    "focus_diopters": {"min": 0.0, "max": 10.0},
    "focus_calibration": "APPROXIMATE",
    "awb_modes": ["AUTO", "DAYLIGHT", "SHADE"],
}


@pytest.fixture
def caps() -> CameraCapabilities:
    return CameraCapabilities.from_hello(HELLO)


# ------------------------------------------------------------------- Range


def test_range_basics():
    r = Range(2, 8)
    assert (r.clamp(0), r.clamp(5), r.clamp(99)) == (2.0, 5.0, 8.0)
    assert r.mid() == 5.0
    assert r.span == 6.0
    assert r.contains(2) and r.contains(8) and not r.contains(8.1)


def test_range_survives_a_device_reporting_it_backwards():
    assert Range(10, 1) == Range(1, 10)


@pytest.mark.parametrize(
    "raw",
    [{"min": 1, "max": 9}, {"lo": 1, "hi": 9}, [1, 9], (1, 9)],
)
def test_range_parse_accepts_several_shapes(raw):
    assert Range.parse(raw, (0, 0)) == Range(1, 9)


def test_range_parse_falls_back_on_junk():
    assert Range.parse(None, (3, 4)) == Range(3, 4)
    assert Range.parse({"nonsense": 1}, (3, 4)) == Range(3, 4)
    assert Range.parse("abc", (3, 4)) == Range(3, 4)


# ------------------------------------------------------------ capabilities


def test_from_hello_parses_everything(caps):
    assert caps.hardware_level == "LEVEL_3"
    assert caps.sensor_size == (4032, 3024)
    assert caps.preview_sizes == [(1920, 1080), (1280, 720), (640, 480)]  # largest first
    assert caps.exposure_ns == Range(125_000, 250_000_000)
    assert caps.iso == Range(50, 3200)
    assert caps.focus_diopters == Range(0.0, 10.0)
    assert caps.focus_calibration == "APPROXIMATE"
    assert caps.awb_modes == ["AUTO", "DAYLIGHT", "SHADE"]
    assert caps.focus_is_absolute


def test_from_hello_accepts_alternative_field_shapes():
    caps = CameraCapabilities.from_hello(
        {
            "hardware_level": "full",
            "sensor": {"width": 4000, "height": 3000},
            "preview_sizes": ["1920x1080", {"width": 1280, "height": 720}],
            "exposure_ns": [1000, 2000],
            "iso": [100, 800],
            "focus_diopters": [0, 5],
            "focus_distance_calibration": "uncalibrated",
            "awb_modes": ["auto", "daylight"],
        }
    )
    assert caps.hardware_level == "FULL"
    assert caps.sensor_size == (4000, 3000)
    assert caps.preview_sizes == [(1920, 1080), (1280, 720)]
    assert caps.awb_modes == ["AUTO", "DAYLIGHT"]
    assert caps.focus_calibration == "UNCALIBRATED"
    assert not caps.focus_is_absolute


def test_from_hello_never_raises_on_garbage():
    caps = CameraCapabilities.from_hello({"sensor_size": "nope", "iso": "nope"})
    assert caps.sensor_size == (1920, 1080)
    assert caps.awb_modes == ["AUTO"]
    assert caps.iso.hi > caps.iso.lo
    assert CameraCapabilities.from_hello({}).hardware_level == "UNKNOWN"


def test_hello_round_trip(caps):
    assert CameraCapabilities.from_hello(caps.to_hello()) == caps


def test_nearest_preview_size(caps):
    assert caps.nearest_preview_size(1920, 1080) == (1920, 1080)
    assert caps.nearest_preview_size(1300, 730) == (1280, 720)
    assert caps.nearest_preview_size(4000, 3000) == (1920, 1080)
    assert caps.nearest_preview_size(100, 100) == (640, 480)


def test_nearest_preview_size_without_a_reported_list():
    caps = CameraCapabilities.from_hello({})
    assert caps.preview_sizes == []
    assert caps.nearest_preview_size(1920, 1080) == (1920, 1080)


# ---------------------------------------------------------------- defaults


def test_defaults_lock_everything(caps):
    s = CameraSettings.defaults(caps)
    assert (s.ae_lock, s.af_lock, s.awb_lock) == (True, True, True)


def test_defaults_are_sensible_for_stopmotion(caps):
    s = CameraSettings.defaults(caps)
    assert s.exposure_ns == DEFAULT_EXPOSURE_NS       # ~1/60 s
    assert s.iso == 100                               # low end of 50..3200
    assert s.focus_diopters == 5.0                    # middle of the usable range
    assert s.awb_mode == "DAYLIGHT"
    assert (s.preview_width, s.preview_height) == (1920, 1080)
    assert s.preview_quality == 70


def test_defaults_stay_near_the_bottom_of_a_high_iso_range():
    caps = CameraCapabilities.from_hello({**HELLO, "iso": [400, 12800]})
    assert CameraSettings.defaults(caps).iso == 400


def test_defaults_are_clamped_into_a_narrow_device(caps):
    caps = CameraCapabilities.from_hello(
        {**HELLO, "exposure_ns": [1_000, 5_000_000], "focus_diopters": [1.0, 3.0]}
    )
    s = CameraSettings.defaults(caps)
    assert s.exposure_ns == 5_000_000
    assert s.focus_diopters == 2.0


def test_defaults_fall_back_when_daylight_is_missing():
    caps = CameraCapabilities.from_hello({**HELLO, "awb_modes": ["OFF", "INCANDESCENT"]})
    assert CameraSettings.defaults(caps).awb_mode == "OFF"


def test_defaults_snap_preview_onto_a_supported_size():
    caps = CameraCapabilities.from_hello({**HELLO, "preview_sizes": [[1280, 720]]})
    s = CameraSettings.defaults(caps)
    assert (s.preview_width, s.preview_height) == (1280, 720)


# ----------------------------------------------------------------- clamping


def test_clamped_clips_every_value(caps):
    s = CameraSettings(
        exposure_ns=10**12,
        iso=99999,
        focus_diopters=42.0,
        awb_mode="DAYLIGHT",
        ae_lock=True,
        af_lock=False,
        awb_lock=True,
        preview_width=1920,
        preview_height=1080,
        preview_quality=250,
    ).clamped(caps)
    assert s.exposure_ns == 250_000_000
    assert s.iso == 3200
    assert s.focus_diopters == 10.0
    assert s.preview_quality == 100
    assert (s.ae_lock, s.af_lock, s.awb_lock) == (True, False, True)  # locks untouched


def test_clamped_lifts_values_below_the_floor(caps):
    s = CameraSettings.defaults(caps)
    s.exposure_ns = 1
    s.iso = 0
    s.focus_diopters = -3.0
    s.preview_quality = 0
    out = s.clamped(caps)
    assert (out.exposure_ns, out.iso, out.focus_diopters, out.preview_quality) == (
        125_000,
        50,
        0.0,
        1,
    )


def test_clamped_replaces_an_unsupported_awb_mode(caps):
    s = CameraSettings.defaults(caps)
    s.awb_mode = "TWILIGHT"  # not offered by this device
    assert s.clamped(caps).awb_mode == "DAYLIGHT"

    odd = CameraCapabilities.from_hello({**HELLO, "awb_modes": ["AUTO", "SHADE"]})
    assert s.clamped(odd).awb_mode == "AUTO"


def test_clamped_is_idempotent_and_does_not_mutate(caps):
    s = CameraSettings.defaults(caps)
    once = s.clamped(caps)
    assert once == once.clamped(caps)
    original = CameraSettings.defaults(caps)
    original.iso = 99999
    copy = original.clamped(caps)
    assert original.iso == 99999 and copy.iso == 3200


def test_clamped_snaps_the_preview_size(caps):
    s = CameraSettings.defaults(caps)
    s.preview_width, s.preview_height = 1000, 560
    out = s.clamped(caps)
    assert (out.preview_width, out.preview_height) == (1280, 720)


# ------------------------------------------------------------------- json


def test_settings_json_round_trip(caps):
    s = CameraSettings.defaults(caps)
    assert CameraSettings.from_json(s.to_json()) == s


def test_to_json_uses_the_wire_field_names(caps):
    keys = set(CameraSettings.defaults(caps).to_json())
    assert keys == {
        "exposure_ns",
        "iso",
        "focus_diopters",
        "awb_mode",
        "ae_lock",
        "af_lock",
        "awb_lock",
        "preview_width",
        "preview_height",
        "preview_quality",
    }


def test_from_json_tolerates_a_frame_settings_subset():
    s = CameraSettings.from_json(
        {"exposure_ns": 8_000_000, "iso": 200, "focus_diopters": 2.5, "awb_mode": "DAYLIGHT"}
    )
    assert (s.exposure_ns, s.iso, s.focus_diopters, s.awb_mode) == (
        8_000_000,
        200,
        2.5,
        "DAYLIGHT",
    )
    assert (s.preview_width, s.preview_height) == (1920, 1080)


def test_from_json_accepts_nested_locks_and_bad_types():
    s = CameraSettings.from_json(
        {"locks": {"ae": True, "af": False, "awb": True}, "iso": "not a number"}
    )
    assert (s.ae_lock, s.af_lock, s.awb_lock) == (True, False, True)
    assert s.iso == 100
    assert CameraSettings.from_json({}).awb_mode == "DAYLIGHT"
