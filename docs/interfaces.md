# Internal interface contract

Authoritative for the parallel implementation streams. Every module below is
written by a different stream; code against these signatures exactly. If you
must deviate, note it in your decisions file and keep the change minimal.

Package root: `desktop/stopmotion/`. Qt binding: PySide6.

## protocol.py  (stream B)

```python
class MsgType(IntEnum):
    HELLO = 1; SET_CONFIG = 2; CONFIG_ACK = 3; PREVIEW_CTL = 4
    PREVIEW_FRAME = 5; CAPTURE = 6; CAPTURE_RESULT = 7; ERROR = 8
    PING = 9; PONG = 10

def encode(msg: MsgType, header: dict | None = None, blob: bytes | None = None) -> bytes
def decode(msg: MsgType, payload: bytes) -> tuple[dict, bytes | None]

class MessageReader:
    """Incremental parser. feed(data) -> list[tuple[MsgType, dict, bytes|None]]"""
    def feed(self, data: bytes) -> list[tuple[MsgType, dict, bytes | None]]
```

Wire format: `[4B BE total_len][1B type][payload]`, where `total_len` covers the
type byte plus payload. For messages carrying binary data the payload is
`[2B BE header_len][utf-8 JSON header][raw bytes]`; for pure-JSON messages the
payload is `[2B BE header_len][utf-8 JSON header]` with no trailing bytes.
Maximum accepted message size: 64 MiB.

## device.py  (stream B)

```python
class DeviceManager(QObject):
    device_attached = Signal(str)      # serial
    device_detached = Signal()
    status          = Signal(str)      # human-readable, for the status bar

    def __init__(self, port: int = 8099, adb: str | None = None, parent=None)
    def start(self) -> None            # begins polling `adb devices` on a timer
    def stop(self) -> None
    def setup_forward(self) -> bool    # adb forward tcp:PORT tcp:PORT
    def launch_app(self) -> bool       # adb shell am start -n <pkg>/.MainActivity
```

## camera.py  (stream B)

```python
@dataclass(frozen=True)
class Range: lo: float; hi: float

@dataclass(frozen=True)
class CameraCapabilities:
    hardware_level: str
    sensor_size: tuple[int, int]
    preview_sizes: list[tuple[int, int]]
    exposure_ns: Range
    iso: Range
    focus_diopters: Range
    focus_calibration: str          # CALIBRATED | APPROXIMATE | UNCALIBRATED
    awb_modes: list[str]
    @classmethod
    def from_hello(cls, hello: dict) -> "CameraCapabilities"

@dataclass
class CameraSettings:
    exposure_ns: int; iso: int; focus_diopters: float; awb_mode: str
    ae_lock: bool; af_lock: bool; awb_lock: bool
    preview_width: int; preview_height: int; preview_quality: int
    def to_json(self) -> dict
    @classmethod
    def from_json(cls, d: dict) -> "CameraSettings"
    def clamped(self, caps: CameraCapabilities) -> "CameraSettings"
    @classmethod
    def defaults(cls, caps: CameraCapabilities) -> "CameraSettings"
```

## connection.py  (stream B)

```python
class PhoneConnection(QObject):
    connected      = Signal()
    disconnected   = Signal(str)          # reason
    hello          = Signal(dict)
    preview_frame  = Signal(QImage, int)  # image, seq
    capture_result = Signal(dict, bytes)  # header {request_id,w,h,settings}, jpeg
    config_ack     = Signal(dict)
    error          = Signal(dict)

    def __init__(self, port: int = 8099, parent=None)
    def start(self) -> None               # connect + spawn IO thread
    def stop(self) -> None
    def send_config(self, settings: CameraSettings) -> None
    def set_preview(self, on: bool) -> None
    def request_capture(self) -> str      # returns request_id
```

JPEG decoding of preview frames happens on the IO thread via
`QImage.loadFromData`; only decoded `QImage`s cross the signal boundary.

## project.py  (stream C)

```python
@dataclass
class Frame:
    id: str; file: str; holds: int; hidden: bool
    captured: str; settings: dict

@dataclass
class Crop:
    aspect: str; x: int; y: int; w: int; h: int
    @staticmethod
    def for_aspect(aspect: str, sw: int, sh: int) -> "Crop"   # centred

class Project:
    path: Path; name: str; fps: int; crop: Crop | None
    capture_size: tuple[int, int]; frames: list[Frame]

    @classmethod
    def create(cls, path, name, fps, capture_size, crop) -> "Project"
    @classmethod
    def open(cls, path) -> "Project"
    def save(self) -> None                       # atomic: tmp + os.replace
    def add_frame(self, jpeg: bytes, settings: dict, size) -> Frame
    def frame_path(self, frame: Frame) -> Path
    def visible_frames(self) -> list[Frame]
    def index_of(self, frame_id: str) -> int
    def duration_ms(self, visible_only: bool = True) -> int
    def purge_unreferenced(self) -> list[Path]   # returns deleted paths
```

Frame ids: `f_0001`, monotonically increasing, never reused.

## commands.py  (stream C)

```python
class TimelineCommand(QUndoCommand):
    """Base. Subclasses implement _apply()/_revert(); the base handles
    selection restore and marks the project dirty."""
    def __init__(self, ctx: "TimelineContext", text: str,
                 sel_before: list[str], sel_after: list[str])

class TimelineContext(Protocol):
    project: Project
    def selection(self) -> list[str]
    def set_selection(self, ids: list[str]) -> None
    def timeline_changed(self) -> None      # UI refresh hook

class CaptureFrame(TimelineCommand)      # (ctx, jpeg, settings, size)
class DeleteFrames(TimelineCommand)      # (ctx, ids)
class SetHolds(TimelineCommand)          # (ctx, ids, holds)
class SetHidden(TimelineCommand)         # (ctx, ids, hidden)
class DuplicateFrames(TimelineCommand)   # (ctx, ids)
class ReorderFrames(TimelineCommand)     # (ctx, ids, insert_at)
```

Every command restores `sel_before` on undo and `sel_after` on redo.
Deletes remove manifest entries only; JPEG files are never unlinked.

## export.py  (stream C)

```python
@dataclass
class ExportSettings:
    out_path: Path; width: int; height: int
    crf: int = 18; preset: str = "medium"; fps: int | None = None

class Exporter(QObject):
    progress = Signal(int)        # 0..100
    finished = Signal(bool, str)  # ok, message
    def __init__(self, ffmpeg: str = "ffmpeg", parent=None)
    def start(self, project: Project, settings: ExportSettings) -> None
    def cancel(self) -> None
```

Implementation: build an ffmpeg concat-demuxer list of the visible frames with
per-entry `duration holds/fps` (final entry repeated, as the demuxer requires),
then `-fps_mode cfr -r <fps> -vf crop=...,scale=... -c:v libx264 -pix_fmt yuv420p`.
Progress from parsing `frame=` on stderr. Runs in a QThread; `cancel()` kills it.

## ui/  (stream D)

`MainWindow` owns `DeviceManager`, `PhoneConnection`, `Project`, `QUndoStack`
and implements `TimelineContext`.
