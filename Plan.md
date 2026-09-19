# Implementation Plan

Companion to [Requirements.md](Requirements.md). Records the design decisions reached
and the order of work.

## 1. Settled decisions

| Area | Decision |
|---|---|
| Phone side | Custom Android app (Kotlin, CameraX + Camera2Interop) |
| Transport | USB only, `adb forward` + TCP socket on localhost |
| Desktop | Python 3.12 + PySide6, Linux only |
| Preview | MJPEG, 1920×1080, ~15 fps, quality configurable |
| Capture | Full-sensor JPEG q95, in-memory on phone → socket → disk on desktop |
| Camera controls | Exposure time, ISO, focus distance, white balance preset, all lockable |
| Onion skin | Core feature: previous captured still, opacity slider, 1–3 frame depth |
| Timing model | Project fps + integer hold counts per frame, shown as ms |
| Persistence | Project folder: `frames/*.jpg` + `project.json`, atomic writes |
| Editing | Multi-select, delete, hide, duplicate, reorder, set duration |
| Undo | In-memory command stack, depth 200, restores selection |
| Export | FFmpeg → H.264/MP4, crop to 16:9, hidden frames excluded |
| Scope | Personal tool, no auth, no packaging, run from checkout |

## 2. Architecture

```
┌─────────────────────── Android phone ───────────────────────┐
│  CameraX                                                     │
│    Preview        ──→ on-screen mirror (locked landscape)    │
│    ImageAnalysis  ──→ JPEG encode ──┐                        │
│    ImageCapture   ──→ JPEG bytes  ──┤                        │
│                                      ↓                       │
│  ServerSocket on 127.0.0.1:8099  (foreground Activity)       │
└──────────────────────────┬───────────────────────────────────┘
                           │  USB, adb forward tcp:8099 tcp:8099
┌──────────────────────────┴───────────────────────────────────┐
│  Desktop (PySide6)                                           │
│    DeviceManager   — adb discovery, tunnel, auto-relaunch    │
│    Connection      — QThread, framed socket I/O              │
│    CameraModel     — capabilities, current settings (truth)  │
│    Project         — frames, manifest I/O                    │
│    CommandStack    — QUndoStack, selection-aware             │
│    UI              — viewport, filmstrip, controls, export   │
└──────────────────────────────────────────────────────────────┘
```

The desktop is the source of truth for camera settings. The phone is a dumb
actuator: it reports its capabilities on connect and applies whatever it is told.

## 3. Repo layout

```
stopmotion/
├── Requirements.md
├── Plan.md
├── justfile
├── desktop/
│   ├── pyproject.toml            # uv-managed
│   └── stopmotion/
│       ├── __main__.py
│       ├── device.py             # adb wrangling
│       ├── protocol.py           # framing, message types
│       ├── connection.py         # socket thread
│       ├── camera.py             # capability + settings model
│       ├── project.py            # manifest, frame store
│       ├── commands.py           # QUndoCommand subclasses
│       ├── export.py             # ffmpeg driver
│       └── ui/
│           ├── main_window.py
│           ├── viewport.py       # live + review, onion skin, crop guides
│           ├── filmstrip.py      # selection model
│           ├── controls.py       # camera panel
│           └── export_dialog.py
└── android/
    ├── settings.gradle.kts
    └── app/src/main/java/.../
        ├── MainActivity.kt
        ├── CameraController.kt   # CameraX + Camera2Interop
        ├── Capabilities.kt       # CameraCharacteristics → JSON
        └── SocketServer.kt       # framed protocol
```

`justfile` targets: `just desktop`, `just android-build`, `just android-install`,
`just run` (install + launch phone app + start desktop).

## 4. Wire protocol

One TCP connection. Every message:

```
[4 bytes BE: length of type+payload] [1 byte: type] [payload]
```

Binary payloads carry a JSON header first:

```
[2 bytes BE: header length] [JSON header] [raw bytes]
```

| Type | Dir | Payload |
|---|---|---|
| `0x01 HELLO` | P→D | JSON: hardware level, sensor sizes, exposure/ISO/focus ranges, AWB modes, focus-distance calibration |
| `0x02 SET_CONFIG` | D→P | JSON: exposure_ns, iso, focus_diopters, awb_mode, locks, preview size/quality |
| `0x03 CONFIG_ACK` | P→D | JSON: settings actually applied (may be clamped) |
| `0x04 PREVIEW_CTL` | D→P | JSON: `{start\|stop}` |
| `0x05 PREVIEW_FRAME` | P→D | header `{seq, w, h}` + JPEG |
| `0x06 CAPTURE` | D→P | JSON: `{request_id}` |
| `0x07 CAPTURE_RESULT` | P→D | header `{request_id, w, h, settings}` + JPEG |
| `0x08 ERROR` | P→D | JSON: `{request_id?, code, message}` |
| `0x09 PING` / `0x0A PONG` | both | empty — liveness |

A full-res still monopolises the socket for a few hundred ms. That is intended:
preview pauses during capture anyway.

### Camera control notes

Manual exposure/ISO/focus are not in the CameraX surface API — they need
`Camera2Interop.Extender` setting `CONTROL_AE_MODE=OFF`, `SENSOR_EXPOSURE_TIME`,
`SENSOR_SENSITIVITY`, `CONTROL_AF_MODE=OFF`, `LENS_FOCUS_DISTANCE`.

Two device-dependent caveats to surface in the UI rather than hide:

- `LENS_FOCUS_DISTANCE` is in diopters and only meaningful when
  `LENS_INFO_FOCUS_DISTANCE_CALIBRATION` is `APPROXIMATE` or `CALIBRATED`. On
  `UNCALIBRATED` devices the slider is a relative scale — label it as such.
- White balance is an AWB **preset** (`CONTROL_AWB_MODE`) plus lock, not a kelvin
  value. Arbitrary temperature would mean hand-rolling `COLOR_CORRECTION_GAINS`,
  which is device-specific and not worth it.

## 5. Data model

`project.json`, rewritten atomically (temp + `os.replace`) after every change:

```json
{
  "version": 1,
  "name": "my-film",
  "fps": 12,
  "created": "2026-09-19T16:44:00Z",
  "capture": { "width": 4032, "height": 3024 },
  "crop": { "aspect": "16:9", "x": 0, "y": 378, "w": 4032, "h": 2268 },
  "frames": [
    {
      "id": "f_0001",
      "file": "frames/f_0001.jpg",
      "holds": 2,
      "hidden": false,
      "captured": "2026-09-19T16:45:12Z",
      "settings": {
        "exposure_ns": 8000000,
        "iso": 200,
        "focus_diopters": 2.5,
        "awb_mode": "DAYLIGHT"
      }
    }
  ]
}
```

Rules:

- Frame IDs are stable and never reused. Order is the array order.
- JPEG files are immutable once written. Delete removes the manifest entry only;
  the file is orphaned, not erased. An explicit **Purge unreferenced frames**
  action cleans up and warns that it is irreversible.
- Hidden frames stay in the array and the filmstrip (greyed/hatched), and are
  skipped by playback, export and onion skin.
- The UI shows total runtime and visible-only runtime.

### Commands and selection

Every mutation is a `QUndoCommand` recording `selection_before` and
`selection_after`; `undo()`/`redo()` restore the matching selection. This is what
makes "undo a delete and the frames come back selected" fall out for free.

Commands: `CaptureFrame`, `DeleteFrames`, `SetHolds`, `SetHidden`,
`DuplicateFrames`, `ReorderFrames`. Stack depth 200, in memory only, cleared on
project close.

Filmstrip selection: plain click replaces the selection, shift+click extends a
range, ctrl+click toggles one frame.

## 6. Milestones

Ordered so the riskiest unknowns are proven first and every milestone ends in
something runnable.

**M0 — Scaffolding.** `git init`. Gradle app skeleton that builds and installs.
`uv` project with PySide6, window that opens. `justfile`.

**M1 — Transport spike.** Phone opens a `ServerSocket`; desktop finds the device
via `adb devices`, runs `adb forward`, connects, exchanges HELLO. Framing codec
with round-trip tests. *Proves the cable works before anything is built on it.*

**M2 — Preview stream.** CameraX `ImageAnalysis` → JPEG → socket. Desktop decodes
with `QImage.loadFromData` on the IO thread, hands frames to the viewport via
signal. Measure achievable fps and latency; tune size/quality. Phone mirrors its
own preview, keeps screen on, locks landscape.

**M3 — Camera controls.** Capabilities from `CameraCharacteristics` → HELLO → UI
sliders built from real ranges. Manual exposure/ISO/focus via `Camera2Interop`,
AWB preset + lock, and a prominent **Lock everything** control (the setting that
actually matters for flicker-free stopmotion). Settings persist on the desktop.

**M4 — Capture and storage.** New Project dialog (name, location, fps, crop).
`ImageCapture` in-memory → socket → `frames/`, manifest updated atomically,
filmstrip thumbnail appears. Nothing written to phone storage at any point.

**M5 — Timeline model.** Selection model, the command classes, `QUndoStack`
wiring, keyboard shortcuts, delete/hide/duplicate/reorder/set-duration.
Duration editor in ms, snapped to hold counts.

**M6 — Onion skin and crop guides.** Captured stills downscaled to preview size
and cached on arrival. Composite 1–3 previous *visible* frames under an opacity
slider; follow the selection when exactly one frame is selected, otherwise show
the last visible frame. Crop rectangle drawn over the live view.

**M7 — Review mode.** Spacebar toggles Live/Review. Play, pause, loop, step,
scrub by dragging the filmstrip, honouring hold counts at project fps, skipping
hidden frames.

**M8 — Export.** FFmpeg via a concat-demuxer list with per-entry durations,
`-fps_mode cfr -r <fps>`, `-vf crop=…,scale=…`, `-pix_fmt yuv420p`, H.264/MP4.
Dialog: path, resolution (1080p default), quality preset, and a summary line
("312 visible frames, 26.0 s at 12 fps"). Progress parsed from ffmpeg output.

**M9 — Robustness.** Disconnect banner, capture disabled while down, `adb devices`
polling for auto-reconnect, settings replayed to the phone on reconnect, capture
timeout → cancel + toast, auto-relaunch of the phone app via `adb shell am start`.
Reopen an existing project from disk.

**M10 — Polish.** Global capture shortcut, purge action, bandwidth/fps readout,
recent projects, error surfacing for clamped settings.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Device caps analysis stream below 1080p in the 3-use-case binding | Query supported sizes in HELLO, pick the largest available; UI shows actual size |
| JPEG encode on phone can't sustain target fps | Quality/size are runtime-configurable; fall back to 720p preview |
| `UNCALIBRATED` focus distance | Label the slider as relative; rely on lock rather than absolute values |
| Thermal throttling / battery drain over a long shoot | USB charges while connected, but monitor; surface a warning if frame interval degrades |
| adb throughput on USB 2.0 | Measured in M1 before anything depends on it |
| Manual settings silently clamped by the device | `CONFIG_ACK` returns what was actually applied; UI shows the real value |
