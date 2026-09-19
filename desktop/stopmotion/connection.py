"""The phone link: one TCP connection to 127.0.0.1:<port>, owned by a QThread.

Threading rules enforced here:

* the socket is created, read, written and closed **only** on the worker thread;
* the UI thread never touches it -- outbound messages go through a queue and a
  queued signal that wakes the worker's event loop, so writes stay serialised
  and ordered;
* preview JPEGs are decoded with ``QImage.loadFromData`` on the worker thread,
  so only finished ``QImage``s cross the signal boundary.

Reads are driven by a ``QSocketNotifier`` rather than a blocking ``recv`` loop,
which keeps the worker's event loop free to service timers (ping, capture
timeout) and queued writes.
"""

from __future__ import annotations

import queue
import socket
import time
import uuid

from PySide6.QtCore import (
    QObject,
    QSocketNotifier,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QImage

from .camera import CameraSettings
from .protocol import MessageReader, MsgType, ProtocolError, encode

__all__ = ["PhoneConnection", "DEFAULT_PORT"]

DEFAULT_PORT = 8099
CONNECT_TIMEOUT_S = 4.0
RECV_CHUNK = 256 * 1024

#: How often we poke the phone, and how long silence may last before we call it.
PING_INTERVAL_MS = 2000
DEAD_AFTER_S = 6.0

DEFAULT_CAPTURE_TIMEOUT_S = 15.0


class _IOWorker(QObject):
    """Lives on the IO thread and owns the socket."""

    connected = Signal()
    disconnected = Signal(str)
    hello = Signal(dict)
    preview_frame = Signal(QImage, int)
    capture_result = Signal(dict, bytes)
    config_ack = Signal(dict)
    error = Signal(dict)

    def __init__(self, host: str, port: int, outbox: "queue.SimpleQueue[bytes]") -> None:
        super().__init__()
        self._host = host
        self._port = int(port)
        self._outbox = outbox
        self._sock: socket.socket | None = None
        self._notifier: QSocketNotifier | None = None
        self._reader = MessageReader()
        self._ping_timer: QTimer | None = None
        self._last_rx = 0.0
        self._pending_captures: set[str] = set()
        self._capture_timers: dict[str, QTimer] = {}
        self._closed = False

    # ------------------------------------------------------------ lifecycle

    @Slot()
    def start_io(self) -> None:
        try:
            sock = socket.create_connection(
                (self._host, self._port), timeout=CONNECT_TIMEOUT_S
            )
        except OSError as exc:
            self._fail(f"cannot reach the phone on {self._host}:{self._port} ({exc})")
            return

        sock.settimeout(None)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:  # pragma: no cover - platform dependent
            pass

        self._sock = sock
        self._last_rx = time.monotonic()
        self._notifier = QSocketNotifier(sock.fileno(), QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._on_readable)

        self._ping_timer = QTimer(self)
        self._ping_timer.setInterval(PING_INTERVAL_MS)
        self._ping_timer.timeout.connect(self._on_ping_tick)
        self._ping_timer.start()

        self.connected.emit()
        # Anything queued before the socket existed goes out now.
        self._drain()

    @Slot()
    def shutdown(self) -> None:
        self._teardown()
        thread = QThread.currentThread()
        if thread is not None:
            thread.quit()

    def _teardown(self) -> None:
        for request_id in list(self._capture_timers):
            self._cancel_capture(request_id)
        if self._ping_timer is not None:
            self._ping_timer.stop()
            self._ping_timer.deleteLater()
            self._ping_timer = None
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier.activated.disconnect()
            self._notifier.deleteLater()
            self._notifier = None
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:  # pragma: no cover
                pass
            self._sock = None
        self._pending_captures.clear()

    def _fail(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._teardown()
        self.disconnected.emit(reason)

    # --------------------------------------------------------------- output

    @Slot()
    def wake(self) -> None:
        """Queued from the UI thread after something was put in the outbox."""
        self._drain()

    def _drain(self) -> None:
        if self._sock is None:
            return
        while True:
            try:
                data = self._outbox.get_nowait()
            except queue.Empty:
                return
            try:
                self._sock.sendall(data)
            except OSError as exc:
                self._fail(f"write failed: {exc}")
                return

    # ---------------------------------------------------------------- input

    @Slot()
    def _on_readable(self) -> None:
        if self._sock is None:
            return
        try:
            chunk = self._sock.recv(RECV_CHUNK)
        except (BlockingIOError, InterruptedError):  # pragma: no cover
            return
        except OSError as exc:
            self._fail(f"read failed: {exc}")
            return

        if not chunk:
            self._fail("the phone closed the connection")
            return

        self._last_rx = time.monotonic()
        try:
            messages = self._reader.feed(chunk)
        except ProtocolError as exc:
            self._fail(f"protocol error: {exc}")
            return

        for msg, header, blob in messages:
            self._dispatch(msg, header, blob)
            if self._closed:
                return

    def _dispatch(self, msg: MsgType, header: dict, blob: bytes | None) -> None:
        if msg is MsgType.PREVIEW_FRAME:
            if not blob:
                return
            image = QImage()
            if not image.loadFromData(blob):
                self.error.emit(
                    {
                        "code": "PREVIEW_DECODE",
                        "message": f"could not decode preview frame {header.get('seq')}",
                    }
                )
                return
            self.preview_frame.emit(image, int(header.get("seq", -1)))
        elif msg is MsgType.HELLO:
            self.hello.emit(header)
        elif msg is MsgType.CONFIG_ACK:
            self.config_ack.emit(header)
        elif msg is MsgType.CAPTURE_RESULT:
            self._cancel_capture(str(header.get("request_id", "")))
            self.capture_result.emit(header, blob or b"")
        elif msg is MsgType.ERROR:
            self._cancel_capture(str(header.get("request_id", "")))
            self.error.emit(header)
        elif msg is MsgType.PING:
            self._send_now(encode(MsgType.PONG))
        elif msg is MsgType.PONG:
            pass  # _last_rx already updated
        elif msg is MsgType.CAPTURE or msg is MsgType.SET_CONFIG or msg is MsgType.PREVIEW_CTL:
            # Desktop-to-phone types; a phone sending them is confused. Ignore.
            pass

    def _send_now(self, data: bytes) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendall(data)
        except OSError as exc:
            self._fail(f"write failed: {exc}")

    # -------------------------------------------------------------- timers

    @Slot()
    def _on_ping_tick(self) -> None:
        if self._sock is None:
            return
        if time.monotonic() - self._last_rx > DEAD_AFTER_S:
            self._fail("the phone stopped responding")
            return
        self._send_now(encode(MsgType.PING))

    @Slot(str, float)
    def arm_capture_timeout(self, request_id: str, timeout_s: float) -> None:
        """Owned timers, not QTimer.singleShot: they must be cancellable, and
        nothing may still be armed in this thread when it exits."""
        self._pending_captures.add(request_id)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda rid=request_id: self._on_capture_timeout(rid))
        self._capture_timers[request_id] = timer
        timer.start(max(1, int(timeout_s * 1000)))

    def _cancel_capture(self, request_id: str) -> None:
        self._pending_captures.discard(request_id)
        timer = self._capture_timers.pop(request_id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _on_capture_timeout(self, request_id: str) -> None:
        if request_id not in self._pending_captures or self._closed:
            return
        self._cancel_capture(request_id)
        try:
            self.error.emit(
                {
                    "request_id": request_id,
                    "code": "CAPTURE_TIMEOUT",
                    "message": "The phone did not return a capture in time.",
                }
            )
        except RuntimeError:  # pragma: no cover - worker already destroyed
            pass


class PhoneConnection(QObject):
    """Public, UI-thread-facing handle on the phone link.

    Signals:
        connected():                     socket is up.
        disconnected(reason):            link is down; emitted exactly once.
        hello(dict):                     HELLO header, feed to CameraCapabilities.
        preview_frame(QImage, int):      decoded preview image and its sequence number.
        capture_result(dict, bytes):     header {request_id,w,h,settings} and JPEG.
        config_ack(dict):                settings the phone actually applied.
        error(dict):                     {request_id?, code, message}.
    """

    connected = Signal()
    disconnected = Signal(str)
    hello = Signal(dict)
    preview_frame = Signal(QImage, int)
    capture_result = Signal(dict, bytes)
    config_ack = Signal(dict)
    error = Signal(dict)

    # Internal, UI thread -> worker thread.
    _wake = Signal()
    _arm_capture = Signal(str, float)
    _shutdown = Signal()

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        parent=None,
        host: str = "127.0.0.1",
        capture_timeout: float = DEFAULT_CAPTURE_TIMEOUT_S,
    ) -> None:
        super().__init__(parent)
        self.port = int(port)
        self.host = host
        self.capture_timeout = float(capture_timeout)
        self._thread: QThread | None = None
        self._worker: _IOWorker | None = None
        self._outbox: "queue.SimpleQueue[bytes]" = queue.SimpleQueue()
        self._is_connected = False
        self._capture_counter = 0

    # ------------------------------------------------------------ lifecycle

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def start(self) -> None:
        """Spawn the IO thread and connect. Safe to call when already running."""
        if self._thread is not None:
            return

        # A fresh connection starts with an empty outbox.
        while True:
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                break

        thread = QThread()
        thread.setObjectName("stopmotion-io")
        worker = _IOWorker(self.host, self.port, self._outbox)
        worker.moveToThread(thread)

        thread.started.connect(worker.start_io)
        self._wake.connect(worker.wake, Qt.ConnectionType.QueuedConnection)
        self._arm_capture.connect(
            worker.arm_capture_timeout, Qt.ConnectionType.QueuedConnection
        )
        self._shutdown.connect(worker.shutdown, Qt.ConnectionType.QueuedConnection)

        worker.connected.connect(self._on_connected)
        worker.disconnected.connect(self._on_disconnected)
        worker.hello.connect(self.hello)
        worker.preview_frame.connect(self.preview_frame)
        worker.capture_result.connect(self.capture_result)
        worker.config_ack.connect(self.config_ack)
        worker.error.connect(self.error)

        self._thread = thread
        self._worker = worker
        thread.start()

    def stop(self) -> None:
        """Shut the IO thread down cleanly. Idempotent.

        The local ``worker`` reference matters: PySide owns that QObject, so
        dropping our last reference to it destroys the C++ object *here*, on the
        UI thread. Doing that while its thread is still running tears down the
        socket notifier and timers from the wrong thread and crashes. So we hold
        it until the thread has actually finished, and only then let it go.
        """
        thread, self._thread = self._thread, None
        worker, self._worker = self._worker, None
        self._is_connected = False
        if thread is None:
            return

        if thread.isRunning():
            self._shutdown.emit()          # worker closes the socket, then quit()s
            if not thread.wait(3000):
                thread.quit()
                if not thread.wait(2000):  # pragma: no cover - should never happen
                    thread.terminate()
                    thread.wait(1000)
        else:
            thread.wait(1000)
        del worker, thread

    # -------------------------------------------------------------- sending

    def _send(self, data: bytes) -> None:
        self._outbox.put(data)
        self._wake.emit()

    def send_config(self, settings: CameraSettings) -> None:
        """Push camera settings to the phone (SET_CONFIG)."""
        payload = settings.to_json() if hasattr(settings, "to_json") else dict(settings)
        self._send(encode(MsgType.SET_CONFIG, payload))

    def set_preview(self, on: bool) -> None:
        """Start or stop the preview stream (PREVIEW_CTL)."""
        self._send(
            encode(MsgType.PREVIEW_CTL, {"action": "start" if on else "stop"})
        )

    def request_capture(self, timeout: float | None = None) -> str:
        """Ask for a full-resolution still.

        Returns the request_id immediately; the answer arrives later as
        ``capture_result`` or, if the phone never replies, as an ``error`` with
        code ``CAPTURE_TIMEOUT``.
        """
        self._capture_counter += 1
        request_id = f"c{self._capture_counter:04d}_{uuid.uuid4().hex[:8]}"
        self._send(encode(MsgType.CAPTURE, {"request_id": request_id}))
        self._arm_capture.emit(
            request_id, float(self.capture_timeout if timeout is None else timeout)
        )
        return request_id

    def ping(self) -> None:
        """Send a PING out of band (the worker does this on its own too)."""
        self._send(encode(MsgType.PING))

    # --------------------------------------------------------------- slots

    @Slot()
    def _on_connected(self) -> None:
        self._is_connected = True
        self.connected.emit()

    @Slot(str)
    def _on_disconnected(self, reason: str) -> None:
        self._is_connected = False
        self.disconnected.emit(reason)
        # Retire the now-idle thread so a later start() gets a clean one.
        # The context object means the callback is dropped if we are destroyed.
        QTimer.singleShot(0, self, self._retire_thread)

    @Slot()
    def _retire_thread(self) -> None:
        if self._thread is not None:
            try:
                self.stop()
            except RuntimeError:  # pragma: no cover - already being torn down
                self._thread = None
                self._worker = None
