#!/usr/bin/env python3
"""A fake phone: speaks the stopmotion wire protocol on 127.0.0.1:8099.

No Android device required. It behaves like a plausible LEVEL_3 phone:

* sends HELLO with realistic capabilities on connect;
* streams synthetic preview JPEGs at ~15 fps that visibly change every frame
  (orbiting dot, sweeping bar, big frame counter, drifting background) so that
  preview, onion skin and review mode can be checked by eye;
* honours SET_CONFIG by clamping it and echoing CONFIG_ACK;
* answers CAPTURE with a full-resolution JPEG after a realistic delay;
* answers PING with PONG.

Run it::

    desktop/.venv/bin/python tools/fake_phone.py
    desktop/.venv/bin/python tools/fake_phone.py --drop-after 60 --fail-capture

Only the standard library plus PySide6 (already a dependency of the desktop app)
is used; PySide6 is here purely as a JPEG encoder and runs headless via
``QT_QPA_PLATFORM=offscreen``.
"""

from __future__ import annotations

import argparse
import math
import os
import socket
import sys
import threading
import time

# Import the desktop package without needing it installed.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DESKTOP = os.path.join(_REPO, "desktop")
if _DESKTOP not in sys.path:
    sys.path.insert(0, _DESKTOP)

# Must be set before PySide6 is imported: this is a headless dev tool.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
)

from stopmotion.camera import CameraCapabilities, CameraSettings, Range  # noqa: E402
from stopmotion.protocol import (  # noqa: E402
    MessageReader,
    MsgType,
    ProtocolError,
    encode,
)

DEFAULT_PORT = 8099

#: What this pretend phone claims it can do.
HELLO_EXTRA = {
    "protocol_version": 1,
    "device": "FakePhone (tools/fake_phone.py)",
    "manufacturer": "kruse",
    "model": "Pixelish 7",
}


def build_capabilities(sensor: tuple[int, int]) -> CameraCapabilities:
    return CameraCapabilities(
        hardware_level="LEVEL_3",
        sensor_size=sensor,
        preview_sizes=[(1920, 1080), (1280, 720), (960, 540), (640, 480)],
        # 1/8000 s .. 1/4 s, in nanoseconds.
        exposure_ns=Range(125_000, 250_000_000),
        iso=Range(50, 3200),
        focus_diopters=Range(0.0, 10.0),
        focus_calibration="APPROXIMATE",
        awb_modes=[
            "AUTO",
            "INCANDESCENT",
            "FLUORESCENT",
            "WARM_FLUORESCENT",
            "DAYLIGHT",
            "CLOUDY_DAYLIGHT",
            "TWILIGHT",
            "SHADE",
        ],
    )


# --------------------------------------------------------------------------
# synthetic image generation
# --------------------------------------------------------------------------

_bg_cache: dict[tuple[int, int, int], QImage] = {}
_bg_lock = threading.Lock()


def _background(width: int, height: int, tint: int) -> QImage:
    """A cached gradient backdrop; `tint` cycles slowly so frames differ."""
    key = (width, height, tint)
    with _bg_lock:
        cached = _bg_cache.get(key)
        if cached is not None:
            return cached

    image = QImage(width, height, QImage.Format.Format_RGB888)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, 0, height)
    gradient.setColorAt(0.0, QColor(24, 28, 40 + tint))
    gradient.setColorAt(1.0, QColor(70 + tint, 52, 44))
    painter.fillRect(0, 0, width, height, gradient)

    # A static "set": floor line and a couple of props, so a moving subject has
    # something to move against and onion skin has fixed reference geometry.
    painter.setPen(QPen(QColor(120, 120, 140), max(2, height // 360)))
    floor = int(height * 0.78)
    painter.drawLine(0, floor, width, floor)
    painter.setBrush(QColor(90, 80, 70))
    painter.setPen(Qt.PenStyle.NoPen)
    box = int(height * 0.12)
    painter.drawRect(int(width * 0.08), floor - box, box, box)
    painter.drawRect(int(width * 0.80), floor - box // 2, box, box // 2)
    painter.end()

    with _bg_lock:
        if len(_bg_cache) > 24:
            _bg_cache.clear()
        _bg_cache[key] = image
    return image


def render_frame(
    width: int,
    height: int,
    seq: int,
    caption: str,
    still: bool = False,
) -> QImage:
    """Draw one synthetic frame. Consecutive `seq` values look clearly different."""
    tint = (seq // 12) % 24
    image = _background(width, height, tint).copy()
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    floor = int(height * 0.78)
    radius = max(8, int(height * 0.06))

    # A ball bouncing left to right along the floor: position AND height change
    # every frame, which is exactly what onion skin needs to be visible.
    # Stills step much faster than preview frames, so that two successive
    # captures are obviously different when composited under an onion skin.
    period = 12 if still else 96
    phase = (seq % period) / period
    x = int(width * (0.12 + 0.72 * phase))
    y = floor - radius - int(abs(math.sin(phase * math.pi * 3)) * height * 0.35)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 150 + (seq * 7) % 100, 40))
    painter.drawEllipse(x - radius, y - radius, radius * 2, radius * 2)

    # A sweeping vertical bar, for tearing/latency checks.
    bar_x = int((seq * 37) % max(1, width))
    painter.setBrush(QColor(80, 200, 255, 160))
    painter.drawRect(bar_x, 0, max(3, width // 240), height)

    # Big frame counter.
    painter.setPen(QColor(255, 255, 255))
    font = QFont()
    font.setPointSize(max(10, height // 14))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(int(width * 0.04), int(height * 0.16), f"{seq:05d}")

    painter.setPen(QColor(200, 220, 255))
    small = QFont()
    small.setPointSize(max(7, height // 34))
    painter.setFont(small)
    painter.drawText(int(width * 0.04), int(height * 0.24), caption)

    if still:
        painter.setPen(QColor(255, 80, 80))
        painter.drawText(int(width * 0.04), int(height * 0.31), "FULL-RES CAPTURE")
        painter.setPen(QPen(QColor(255, 80, 80), max(3, height // 200)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        inset = max(4, height // 100)
        painter.drawRect(inset, inset, width - 2 * inset, height - 2 * inset)

    painter.end()
    return image


def encode_jpeg(image: QImage, quality: int) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "JPEG", int(quality)):  # pragma: no cover
        raise RuntimeError("Qt failed to encode a JPEG")
    return bytes(buffer.data())


# --------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------


class ClientSession:
    """One connected desktop."""

    def __init__(self, sock: socket.socket, opts: argparse.Namespace) -> None:
        self.sock = sock
        self.opts = opts
        self.caps = build_capabilities(tuple(opts.sensor))
        self.settings = CameraSettings.defaults(self.caps)
        self.reader = MessageReader()
        self.send_lock = threading.Lock()
        self.busy = threading.Lock()  # a full-res capture owns the socket
        self.streaming = False
        self.alive = True
        self.seq = 0
        self.frames_sent = 0
        self.captures = 0
        self._preview_thread: threading.Thread | None = None

    # ------------------------------------------------------------- plumbing

    def log(self, *parts) -> None:
        if not self.opts.quiet:
            print("[fake-phone]", *parts, flush=True)

    def send(self, msg: MsgType, header: dict | None = None, blob: bytes | None = None) -> bool:
        data = encode(msg, header, blob)
        with self.send_lock:
            if not self.alive:
                return False
            try:
                self.sock.sendall(data)
            except OSError as exc:
                self.log("send failed:", exc)
                self.alive = False
                return False
        return True

    def close(self, why: str) -> None:
        if self.alive:
            self.log("closing:", why)
        self.alive = False
        self.streaming = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # ---------------------------------------------------------------- serve

    def serve(self) -> None:
        hello = dict(HELLO_EXTRA)
        hello.update(self.caps.to_hello())
        hello["settings"] = self.settings.to_json()
        self.send(MsgType.HELLO, hello)
        self.log(
            f"HELLO sent: {self.caps.hardware_level}, sensor "
            f"{self.caps.sensor_size[0]}x{self.caps.sensor_size[1]}"
        )

        try:
            while self.alive:
                try:
                    chunk = self.sock.recv(65536)
                except OSError as exc:
                    self.log("recv failed:", exc)
                    break
                if not chunk:
                    break
                try:
                    messages = self.reader.feed(chunk)
                except ProtocolError as exc:
                    self.log("protocol error from desktop:", exc)
                    break
                for msg, header, blob in messages:
                    self.handle(msg, header, blob)
                    if not self.alive:
                        break
        finally:
            self.streaming = False
            thread = self._preview_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
            self.close("client gone")

    def handle(self, msg: MsgType, header: dict, blob: bytes | None) -> None:
        if msg is MsgType.PING:
            self.send(MsgType.PONG)
        elif msg is MsgType.PREVIEW_CTL:
            self.on_preview_ctl(header)
        elif msg is MsgType.SET_CONFIG:
            self.on_set_config(header)
        elif msg is MsgType.CAPTURE:
            self.on_capture(header)
        elif msg is MsgType.PONG:
            pass
        else:
            self.log("ignoring unexpected message:", msg.name)

    # -------------------------------------------------------------- handlers

    def on_preview_ctl(self, header: dict) -> None:
        action = header.get("action", header.get("state", header.get("preview")))
        if isinstance(action, bool):
            want = action
        else:
            want = str(action).lower() in ("start", "on", "true", "1", "run")
        if want and not self.streaming:
            self.streaming = True
            self._preview_thread = threading.Thread(
                target=self._preview_loop, name="fake-preview", daemon=True
            )
            self._preview_thread.start()
            self.log("preview started")
        elif not want and self.streaming:
            self.streaming = False
            self.log("preview stopped")

    def on_set_config(self, header: dict) -> None:
        requested = CameraSettings.from_json(header)
        applied = requested.clamped(self.caps)
        self.settings = applied
        ack = applied.to_json()
        ack["clamped"] = applied.to_json() != requested.to_json()
        self.send(MsgType.CONFIG_ACK, ack)
        self.log(
            "CONFIG_ACK:",
            f"exp={applied.exposure_ns}ns iso={applied.iso} "
            f"focus={applied.focus_diopters:.2f} awb={applied.awb_mode} "
            f"locks={int(applied.ae_lock)}{int(applied.af_lock)}{int(applied.awb_lock)} "
            f"preview={applied.preview_width}x{applied.preview_height}"
            f"@q{applied.preview_quality}"
            + (" (clamped)" if ack["clamped"] else ""),
        )

    def on_capture(self, header: dict) -> None:
        request_id = str(header.get("request_id", ""))
        self.captures += 1
        with self.busy:  # a real still monopolises the socket; so does this one
            time.sleep(max(0.0, self.opts.capture_delay))
            if self.opts.fail_capture:
                self.send(
                    MsgType.ERROR,
                    {
                        "request_id": request_id,
                        "code": "CAPTURE_FAILED",
                        "message": "Simulated capture failure (--fail-capture).",
                    },
                )
                self.log("CAPTURE", request_id, "-> simulated failure")
                return

            width, height = self.caps.sensor_size
            image = render_frame(
                width,
                height,
                self.captures,
                f"still #{self.captures}  {time.strftime('%H:%M:%S')}",
                still=True,
            )
            jpeg = encode_jpeg(image, 95)
            self.send(
                MsgType.CAPTURE_RESULT,
                {
                    "request_id": request_id,
                    "w": width,
                    "h": height,
                    "settings": self.settings.to_json(),
                },
                jpeg,
            )
            self.log(f"CAPTURE {request_id} -> {width}x{height}, {len(jpeg)} bytes")

    # --------------------------------------------------------------- preview

    def _preview_loop(self) -> None:
        interval = 1.0 / max(1.0, float(self.opts.fps))
        next_due = time.monotonic()
        while self.alive and self.streaming:
            settings = self.settings
            width, height = settings.preview_width, settings.preview_height
            self.seq += 1
            image = render_frame(
                width,
                height,
                self.seq,
                f"preview {width}x{height} q{settings.preview_quality} "
                f"iso{settings.iso} {settings.awb_mode}",
            )
            jpeg = encode_jpeg(image, settings.preview_quality)

            with self.busy:  # never interleave with a full-res capture
                if not (self.alive and self.streaming):
                    break
                if not self.send(
                    MsgType.PREVIEW_FRAME,
                    {"seq": self.seq, "w": width, "h": height},
                    jpeg,
                ):
                    break
            self.frames_sent += 1

            if self.opts.drop_after and self.frames_sent >= self.opts.drop_after:
                self.log(f"--drop-after {self.opts.drop_after}: dropping the link")
                self.close("simulated disconnect")
                return

            next_due += interval
            delay = next_due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:  # we fell behind; don't try to catch up in a burst
                next_due = time.monotonic()


def serve_forever(opts: argparse.Namespace) -> int:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((opts.host, opts.port))
    except OSError as exc:
        print(f"[fake-phone] cannot bind {opts.host}:{opts.port}: {exc}", file=sys.stderr)
        return 2
    server.listen(8)
    if not opts.quiet:
        print(f"[fake-phone] listening on {opts.host}:{opts.port}", flush=True)

    try:
        while True:
            try:
                sock, peer = server.accept()
            except KeyboardInterrupt:
                break
            except OSError as exc:  # pragma: no cover
                print(f"[fake-phone] accept failed: {exc}", file=sys.stderr)
                break
            if not opts.quiet:
                print(f"[fake-phone] desktop connected from {peer[0]}:{peer[1]}", flush=True)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            session = ClientSession(sock, opts)
            try:
                session.serve()
            except KeyboardInterrupt:
                session.close("interrupted")
                break
            if opts.once:
                break
    finally:
        server.close()
    return 0


def _parse_size(text: str) -> tuple[int, int]:
    try:
        width, height = text.lower().split("x")
        return int(width), int(height)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected WIDTHxHEIGHT, got {text!r}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--fps", type=float, default=15.0, help="preview frame rate")
    parser.add_argument(
        "--sensor",
        type=_parse_size,
        default=(4032, 3024),
        help="full-resolution capture size (default 4032x3024)",
    )
    parser.add_argument(
        "--capture-delay",
        type=float,
        default=0.4,
        help="seconds to stall before answering a CAPTURE (default 0.4)",
    )
    parser.add_argument(
        "--drop-after",
        type=int,
        default=0,
        metavar="N",
        help="hang up after sending N preview frames, to simulate a cable yank",
    )
    parser.add_argument(
        "--fail-capture",
        action="store_true",
        help="answer every CAPTURE with an ERROR instead of an image",
    )
    parser.add_argument(
        "--once", action="store_true", help="exit after the first client disconnects"
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    opts = parser.parse_args(argv)

    # QGuiApplication is required before Qt will touch fonts or image plugins,
    # even offscreen. We never run its event loop.
    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0] if sys.argv else "fake_phone"])
    _ = app

    try:
        return serve_forever(opts)
    except KeyboardInterrupt:
        print("[fake-phone] bye", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
