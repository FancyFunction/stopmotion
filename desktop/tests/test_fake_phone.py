"""End-to-end tests against tools/fake_phone.py.

No Android hardware is involved: the fake phone is booted in a subprocess and
spoken to over a real TCP socket, first with a bare protocol client and then
through the real :class:`PhoneConnection` worker thread.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
DESKTOP_DIR = TESTS_DIR.parent
REPO_DIR = DESKTOP_DIR.parent
FAKE_PHONE = REPO_DIR / "tools" / "fake_phone.py"

sys.path.insert(0, str(DESKTOP_DIR))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from stopmotion.camera import CameraCapabilities, CameraSettings  # noqa: E402
from stopmotion.protocol import MessageReader, MsgType, encode  # noqa: E402

BOOT_TIMEOUT = 30.0
IO_TIMEOUT = 25.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakePhoneProcess:
    """A running tools/fake_phone.py."""

    def __init__(self, *extra: str) -> None:
        self.port = free_port()
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(FAKE_PHONE),
                "--port",
                str(self.port),
                "--sensor",
                "640x480",
                "--capture-delay",
                "0.1",
                *extra,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        self._wait_until_listening()

    def _wait_until_listening(self) -> None:
        deadline = time.monotonic() + BOOT_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"fake_phone exited early ({self.proc.returncode}):\n"
                    f"{self.proc.stdout.read() if self.proc.stdout else ''}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("fake_phone never started listening")

    def connect(self) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=IO_TIMEOUT)
        sock.settimeout(IO_TIMEOUT)
        return sock

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.proc.stdout:
            self.proc.stdout.close()


@pytest.fixture
def phone():
    process = FakePhoneProcess()
    try:
        yield process
    finally:
        process.kill()


class Client:
    """A minimal desktop: socket + MessageReader."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.reader = MessageReader()
        self.inbox: list[tuple] = []

    def send(self, msg, header=None, blob=None) -> None:
        self.sock.sendall(encode(msg, header, blob))

    def next_of(self, wanted: MsgType, timeout: float = IO_TIMEOUT):
        """Read until a message of the wanted type turns up."""
        deadline = time.monotonic() + timeout
        while True:
            for i, item in enumerate(self.inbox):
                if item[0] is wanted:
                    return self.inbox.pop(i)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for {wanted.name}")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError(f"phone hung up while waiting for {wanted.name}")
            self.inbox.extend(self.reader.feed(chunk))


def test_hello_preview_and_capture_round_trip(phone):
    client = Client(phone.connect())

    # --- HELLO
    _msg, hello, _blob = client.next_of(MsgType.HELLO)
    caps = CameraCapabilities.from_hello(hello)
    assert caps.hardware_level == "LEVEL_3"
    assert caps.sensor_size == (640, 480)
    assert (1920, 1080) in caps.preview_sizes
    assert "DAYLIGHT" in caps.awb_modes
    assert caps.exposure_ns.lo > 0 and caps.iso.hi >= caps.iso.lo
    assert caps.focus_calibration == "APPROXIMATE"

    # --- SET_CONFIG is clamped and acknowledged
    wanted = CameraSettings.defaults(caps)
    wanted.iso = 10**6          # far above what the device offers
    wanted.awb_mode = "NOT_A_MODE"
    client.send(MsgType.SET_CONFIG, wanted.to_json())
    _msg, ack, _blob = client.next_of(MsgType.CONFIG_ACK)
    applied = CameraSettings.from_json(ack)
    assert applied == wanted.clamped(caps)
    assert applied.iso == caps.iso.hi
    assert applied.awb_mode == "DAYLIGHT"
    assert (applied.ae_lock, applied.af_lock, applied.awb_lock) == (True, True, True)

    # --- preview frames arrive, are JPEG, and change from frame to frame
    client.send(MsgType.PREVIEW_CTL, {"action": "start"})
    first = client.next_of(MsgType.PREVIEW_FRAME)
    second = client.next_of(MsgType.PREVIEW_FRAME)
    for _msg, header, blob in (first, second):
        assert blob and blob.startswith(b"\xff\xd8") and blob.endswith(b"\xff\xd9")
        assert (header["w"], header["h"]) == (
            applied.preview_width,
            applied.preview_height,
        )
    assert second[1]["seq"] == first[1]["seq"] + 1
    assert first[2] != second[2], "preview frames must visibly differ"

    # --- capture
    client.send(MsgType.CAPTURE, {"request_id": "req-1"})
    _msg, result, jpeg = client.next_of(MsgType.CAPTURE_RESULT)
    assert result["request_id"] == "req-1"
    assert (result["w"], result["h"]) == caps.sensor_size
    assert jpeg and jpeg.startswith(b"\xff\xd8")
    assert len(jpeg) > 1000
    assert CameraSettings.from_json(result["settings"]) == applied

    # --- liveness
    client.send(MsgType.PING)
    assert client.next_of(MsgType.PONG)[1] == {}


def test_fail_capture_flag_returns_an_error():
    process = FakePhoneProcess("--fail-capture")
    try:
        client = Client(process.connect())
        client.next_of(MsgType.HELLO)
        client.send(MsgType.CAPTURE, {"request_id": "boom"})
        _msg, error, _blob = client.next_of(MsgType.ERROR)
        assert error["request_id"] == "boom"
        assert error["code"] == "CAPTURE_FAILED"
        assert error["message"]
    finally:
        process.kill()


def test_drop_after_hangs_up_mid_stream():
    process = FakePhoneProcess("--drop-after", "3")
    try:
        client = Client(process.connect())
        client.next_of(MsgType.HELLO)
        client.send(MsgType.PREVIEW_CTL, {"action": "start"})
        for _ in range(3):
            client.next_of(MsgType.PREVIEW_FRAME)
        with pytest.raises(AssertionError, match="hung up|timed out"):
            client.next_of(MsgType.PREVIEW_FRAME, timeout=5.0)
    except (ConnectionResetError, OSError):
        pass  # an abrupt RST is an equally valid "cable yanked"
    finally:
        process.kill()


# ---------------------------------------------------------------------------
# the real PhoneConnection against the fake phone
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])
    yield app


def pump(app, predicate, timeout: float = IO_TIMEOUT) -> bool:
    """Spin the Qt event loop until `predicate` holds or we give up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def test_phone_connection_end_to_end(qapp, phone):
    from stopmotion.connection import PhoneConnection

    seen: dict = {"frames": [], "errors": [], "down": None}
    conn = PhoneConnection(port=phone.port, capture_timeout=20.0)
    conn.connected.connect(lambda: seen.update(up=True))
    conn.hello.connect(lambda h: seen.update(hello=h))
    conn.config_ack.connect(lambda a: seen.update(ack=a))
    conn.preview_frame.connect(lambda img, seq: seen["frames"].append((img, seq)))
    conn.capture_result.connect(lambda h, b: seen.update(capture=(h, b)))
    conn.error.connect(seen["errors"].append)
    conn.disconnected.connect(lambda why: seen.update(down=why))

    try:
        conn.start()
        assert pump(qapp, lambda: "hello" in seen), "no HELLO"
        assert seen.get("up") is True
        assert conn.is_connected

        caps = CameraCapabilities.from_hello(seen["hello"])
        settings = CameraSettings.defaults(caps)
        conn.send_config(settings)
        assert pump(qapp, lambda: "ack" in seen), "no CONFIG_ACK"
        assert CameraSettings.from_json(seen["ack"]) == settings.clamped(caps)

        conn.set_preview(True)
        assert pump(qapp, lambda: len(seen["frames"]) >= 2), "no preview frames"
        image, seq = seen["frames"][0]
        assert not image.isNull(), "preview JPEG was not decoded on the IO thread"
        assert (image.width(), image.height()) == (
            settings.preview_width,
            settings.preview_height,
        )
        assert seq >= 1

        request_id = conn.request_capture()
        assert request_id
        assert pump(qapp, lambda: "capture" in seen), "no CAPTURE_RESULT"
        header, jpeg = seen["capture"]
        assert header["request_id"] == request_id
        assert (header["w"], header["h"]) == caps.sensor_size
        assert jpeg.startswith(b"\xff\xd8")

        conn.set_preview(False)
        assert seen["errors"] == []
        assert seen["down"] is None
    finally:
        conn.stop()

    qapp.processEvents()
    assert not conn.is_connected


def test_capture_timeout_is_reported(qapp):
    """A phone that never answers CAPTURE must surface an error, not hang."""
    from stopmotion.connection import PhoneConnection

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)

    errors: list[dict] = []
    conn = PhoneConnection(port=port, capture_timeout=0.4)
    conn.error.connect(errors.append)
    peer = None
    try:
        conn.start()
        listener.settimeout(IO_TIMEOUT)
        peer, _addr = listener.accept()  # accept but stay silent
        assert pump(qapp, lambda: conn.is_connected, timeout=10.0)
        request_id = conn.request_capture()
        assert pump(qapp, lambda: bool(errors), timeout=10.0), "no timeout error"
        assert errors[0]["code"] == "CAPTURE_TIMEOUT"
        assert errors[0]["request_id"] == request_id
    finally:
        conn.stop()
        if peer is not None:
            peer.close()
        listener.close()


def test_repeated_start_stop_cycles_are_clean(qapp, phone):
    """The UI reconnect timer calls start() over and over; that must not leak
    threads, warn, or crash."""
    from stopmotion.connection import PhoneConnection

    conn = PhoneConnection(port=phone.port)
    hellos: list[dict] = []
    conn.hello.connect(hellos.append)
    try:
        for _ in range(3):
            conn.start()
            assert pump(qapp, lambda: conn.is_connected, timeout=10.0)
            conn.stop()
            assert not conn.is_connected
        assert pump(qapp, lambda: len(hellos) >= 3, timeout=5.0)
    finally:
        conn.stop()


def test_start_after_a_dropped_link_reconnects(qapp):
    """After the phone hangs up, start() must be able to build a fresh link."""
    from stopmotion.connection import PhoneConnection

    process = FakePhoneProcess("--drop-after", "2")
    conn = PhoneConnection(port=process.port)
    state: dict = {"frames": 0, "downs": []}
    conn.preview_frame.connect(lambda *_: state.update(frames=state["frames"] + 1))
    conn.disconnected.connect(state["downs"].append)
    try:
        conn.start()
        assert pump(qapp, lambda: conn.is_connected, timeout=10.0)
        conn.set_preview(True)
        assert pump(qapp, lambda: bool(state["downs"]), timeout=20.0), "phone never hung up"
        assert state["frames"] >= 1

        # the worker has retired itself, so a plain start() works again
        assert pump(qapp, lambda: not conn.is_connected, timeout=5.0)
        conn.start()
        assert pump(qapp, lambda: conn.is_connected, timeout=10.0)
    finally:
        conn.stop()
        process.kill()


def test_connection_reports_a_refused_link(qapp):
    from stopmotion.connection import PhoneConnection

    port = free_port()  # nothing is listening there
    reasons: list[str] = []
    conn = PhoneConnection(port=port)
    conn.disconnected.connect(reasons.append)
    try:
        conn.start()
        assert pump(qapp, lambda: bool(reasons), timeout=15.0), "no disconnect reason"
        assert "cannot reach the phone" in reasons[0]
    finally:
        conn.stop()


# ---------------------------------------------------------------------------
# DeviceManager, driven by a stub adb (no phone, no SDK needed)
# ---------------------------------------------------------------------------


def write_stub_adb(tmp_path: Path, listing: str) -> Path:
    """A shell script that answers `adb devices -l` with a canned listing."""
    adb = tmp_path / "adb"
    adb.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "devices" ]; then\n'
        f"cat <<'LISTING'\n{listing}LISTING\n"
        "fi\n"
        "exit 0\n"
    )
    adb.chmod(0o755)
    return adb


def test_parse_devices_handles_real_adb_output():
    from stopmotion.device import _parse_devices

    listing = (
        "List of devices attached\n"
        "R3CW10G4B2X            device usb:1-3 product:a52q model:SM_A525F\n"
        "emulator-5554          offline\n"
        "ZY223XYZ               unauthorized\n"
        "junkline\n"
        "\n"
    )
    assert _parse_devices(listing) == [
        ("R3CW10G4B2X", "device"),
        ("emulator-5554", "offline"),
        ("ZY223XYZ", "unauthorized"),
    ]


def test_device_manager_reports_a_missing_adb(qapp, monkeypatch):
    import shutil

    from stopmotion import device as device_mod

    monkeypatch.setattr(device_mod, "ADB_FALLBACKS", ())
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    manager = device_mod.DeviceManager(adb="/nowhere/adb")
    assert manager.adb_path is None
    assert not manager.available

    messages: list[str] = []
    manager.status.connect(messages.append)
    manager.start()
    manager.stop()
    assert messages and "adb not found" in messages[0]
    # commands fail politely rather than raising
    assert manager.setup_forward() is False
    assert manager.launch_app() is False


def test_device_manager_sees_an_attached_device(qapp, tmp_path):
    from stopmotion.device import DeviceManager

    adb = write_stub_adb(
        tmp_path, "List of devices attached\nFAKE123\tdevice product:fake\n"
    )
    manager = DeviceManager(port=free_port(), adb=str(adb))
    attached: list[str] = []
    statuses: list[str] = []
    manager.device_attached.connect(attached.append)
    manager.status.connect(statuses.append)
    try:
        manager.start()
        assert pump(qapp, lambda: bool(attached), timeout=15.0), statuses
        assert attached == ["FAKE123"]
        assert manager.serial == "FAKE123"
        # a second poll must not re-announce the same device
        assert pump(qapp, lambda: False, timeout=3.0) is False
        assert attached == ["FAKE123"]
        assert manager.setup_forward() is True
    finally:
        manager.stop()


def test_device_manager_reports_an_unauthorised_device(qapp, tmp_path):
    from stopmotion.device import DeviceManager

    adb = write_stub_adb(
        tmp_path, "List of devices attached\nFAKE123\tunauthorized\n"
    )
    manager = DeviceManager(port=free_port(), adb=str(adb))
    statuses: list[str] = []
    manager.status.connect(statuses.append)
    try:
        manager.start()
        assert pump(qapp, lambda: any("authorised" in s for s in statuses), timeout=15.0)
    finally:
        manager.stop()


def test_fake_phone_runs_standalone():
    """`python tools/fake_phone.py --help` works without the package installed."""
    proc = subprocess.run(
        [sys.executable, str(FAKE_PHONE), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        cwd=str(Path.home()),  # not the repo: the script must find its own imports
    )
    assert proc.returncode == 0, proc.stderr
    assert "--drop-after" in proc.stdout and "--fail-capture" in proc.stdout
