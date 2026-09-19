"""adb wrangling: find the tool, watch for the phone, open the tunnel.

Every ``adb`` invocation has a timeout and every one of them reports failure as
a human-readable status string rather than an exception -- a missing adb or an
unplugged cable is a normal state for this app, not a crash.

Device polling runs on a thread-pool worker: the very first ``adb devices`` call
of a session starts the adb server, which can take a couple of seconds, and that
must not freeze the UI.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

__all__ = ["DeviceManager", "find_adb", "ANDROID_PACKAGE", "ANDROID_ACTIVITY"]

ANDROID_PACKAGE = "de.kruse.stopmotion"
ANDROID_ACTIVITY = ".MainActivity"

#: Checked after ``$PATH`` -- the SDK location on this machine.
ADB_FALLBACKS = (
    os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
    "/home/christian/Android/Sdk/platform-tools/adb",
    "/opt/android-sdk/platform-tools/adb",
)

POLL_INTERVAL_MS = 1500
DEVICES_TIMEOUT_S = 10.0
COMMAND_TIMEOUT_S = 15.0

#: adb device states that mean "plugged in but not usable".
_UNUSABLE_STATES = {
    "unauthorized": "Phone is connected but not authorised — accept the USB debugging prompt.",
    "offline": "Phone is connected but offline — replug the cable.",
    "no permissions": "No permission to access the phone — check your udev rules.",
    "authorizing": "Authorising the phone…",
    "connecting": "Connecting to the phone…",
}


def find_adb(explicit: str | None = None) -> str | None:
    """Locate the adb binary: explicit path, then ``$PATH``, then known SDKs."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    on_path = shutil.which("adb")
    if on_path:
        candidates.append(on_path)
    candidates.extend(ADB_FALLBACKS)

    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        # An explicit value may also be a bare command name on $PATH.
        resolved = shutil.which(candidate) if candidate else None
        if resolved:
            return resolved
    return None


def _parse_devices(stdout: str) -> list[tuple[str, str]]:
    """Parse ``adb devices -l`` into ``[(serial, state), ...]``."""
    devices: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "no" and len(parts) > 2 and parts[2] == "permissions":
            state = "no permissions"
        devices.append((serial, state))
    return devices


class _PollSignals(QObject):
    done = Signal(object, str)  # devices (list | None), error text


class _PollTask(QRunnable):
    """One off-thread ``adb devices -l``."""

    def __init__(self, adb: str) -> None:
        super().__init__()
        self.adb = adb
        self.signals = _PollSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # pragma: no cover - exercised only with real adb
        try:
            proc = subprocess.run(
                [self.adb, "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=DEVICES_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.signals.done.emit(None, "adb devices timed out")
            return
        except OSError as exc:
            self.signals.done.emit(None, f"cannot run adb: {exc}")
            return

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            self.signals.done.emit(None, detail[-1] if detail else "adb devices failed")
            return
        self.signals.done.emit(_parse_devices(proc.stdout), "")


class DeviceManager(QObject):
    """Watches ``adb devices`` and owns the port forward.

    Signals:
        device_attached(serial): a usable device appeared.
        device_detached(): the device we were tracking went away.
        status(text): human-readable line for the status bar.
    """

    device_attached = Signal(str)
    device_detached = Signal()
    status = Signal(str)

    def __init__(self, port: int = 8099, adb: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.port = int(port)
        self.adb_path = find_adb(adb)
        self._serial: str | None = None
        self._last_status: str | None = None
        self._busy = False
        self._running = False
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

    # ---------------------------------------------------------------- state

    @property
    def serial(self) -> str | None:
        """Serial of the currently attached device, or None."""
        return self._serial

    @property
    def available(self) -> bool:
        return self.adb_path is not None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self.adb_path is None:
            self._emit_status(
                "adb not found — install Android platform-tools or put adb on $PATH."
            )
            return
        self._emit_status("Looking for a phone…")
        self._timer.start()
        self._poll()

    def stop(self) -> None:
        self._running = False
        self._timer.stop()
        # Let an in-flight poll finish so its signals cannot outlive us.
        self._pool.waitForDone(int(DEVICES_TIMEOUT_S * 1000) + 1000)
        self._busy = False

    # -------------------------------------------------------------- polling

    def _poll(self) -> None:
        if not self._running or self._busy or self.adb_path is None:
            return
        self._busy = True
        task = _PollTask(self.adb_path)
        task.signals.done.connect(self._on_devices)
        self._pool.start(task)

    @Slot(object, str)
    def _on_devices(self, devices, error: str) -> None:
        self._busy = False
        if not self._running:
            return

        if devices is None:
            self._emit_status(f"adb problem: {error}")
            return

        usable = [serial for serial, state in devices if state == "device"]
        if usable:
            serial = self._serial if self._serial in usable else usable[0]
            if serial != self._serial:
                self._serial = serial
                extra = f" (+{len(usable) - 1} more)" if len(usable) > 1 else ""
                self._emit_status(f"Phone {serial} attached{extra}")
                self.device_attached.emit(serial)
            return

        if self._serial is not None:
            self._serial = None
            self._emit_status("Phone disconnected")
            self.device_detached.emit()
            return

        for _serial, state in devices:
            message = _UNUSABLE_STATES.get(state)
            if message:
                self._emit_status(message)
                return
        self._emit_status("No phone detected — connect it over USB.")

    def _emit_status(self, text: str) -> None:
        if text != self._last_status:
            self._last_status = text
            self.status.emit(text)

    # ------------------------------------------------------------- commands

    def setup_forward(self) -> bool:
        """``adb forward tcp:PORT tcp:PORT``."""
        ok, out = self._run(["forward", f"tcp:{self.port}", f"tcp:{self.port}"])
        if ok:
            self._emit_status(f"Port {self.port} forwarded to the phone")
        else:
            self._emit_status(f"adb forward failed: {out}")
        return ok

    def remove_forward(self) -> bool:
        """Tear the tunnel down again (best effort)."""
        ok, _out = self._run(["forward", "--remove", f"tcp:{self.port}"])
        return ok

    def launch_app(self) -> bool:
        """``adb shell am start -n <pkg>/.MainActivity``."""
        ok, out = self._run(
            ["shell", "am", "start", "-n", f"{ANDROID_PACKAGE}/{ANDROID_ACTIVITY}"]
        )
        # `am start` exits 0 even when it prints an error, so check the text too.
        if ok and "Error" in out:
            ok = False
        if ok:
            self._emit_status("Phone app launched")
        else:
            self._emit_status(f"Could not launch the phone app: {out or 'unknown error'}")
        return ok

    def _run(self, args: list[str]) -> tuple[bool, str]:
        """Run one short adb command. Returns ``(ok, output-or-error)``."""
        if self.adb_path is None:
            return False, "adb not found"
        cmd = [self.adb_path]
        if self._serial:
            cmd += ["-s", self._serial]
        cmd += args
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"adb {' '.join(args)} timed out"
        except OSError as exc:
            return False, f"cannot run adb: {exc}"

        output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return proc.returncode == 0, output
