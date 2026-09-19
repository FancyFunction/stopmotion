"""Entry point.

    python -m stopmotion [project-folder]

Environment:

* ``STOPMOTION_FAKE=1`` skips all adb/device setup and connects straight to
  127.0.0.1:8099, which is where ``tools/fake_phone.py`` listens.  Used for
  development on a machine with no phone attached.
* ``STOPMOTION_PORT`` overrides the TCP port (default 8099).
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path


def _truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in ("0", "false", "no", "off", "")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    from PySide6.QtCore import QCoreApplication, QTimer
    from PySide6.QtWidgets import QApplication

    from .settings import APPLICATION, ORGANISATION
    from .ui.main_window import MainWindow

    QCoreApplication.setOrganizationName(ORGANISATION)
    QCoreApplication.setApplicationName(APPLICATION)

    app = QApplication.instance() or QApplication(argv)

    fake = _truthy(os.environ.get("STOPMOTION_FAKE"))
    try:
        port = int(os.environ.get("STOPMOTION_PORT", "8099"))
    except ValueError:
        port = 8099

    window = MainWindow(fake=fake, port=port)

    geometry = window.app_settings.window_geometry()
    if geometry is not None:
        window.restoreGeometry(geometry)
    state = window.app_settings.window_state()
    if state is not None:
        window.restoreState(state)

    window.show()

    # optional positional argument: a project folder to reopen
    candidates = [a for a in argv[1:] if not a.startswith("-")]
    if candidates and Path(candidates[0]).exists():
        window.open_project_path(candidates[0])

    # Ctrl+C in the terminal should kill the app, not be swallowed by Qt
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    nudge = QTimer()
    nudge.start(200)
    nudge.timeout.connect(lambda: None)

    QTimer.singleShot(0, window.start)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
