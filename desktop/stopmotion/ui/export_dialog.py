"""Export dialog: path, resolution, quality preset, live summary, progress."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..export import Exporter, ExportSettings

#: label -> output height
RESOLUTIONS = [("720p", 720), ("1080p", 1080), ("1440p", 1440), ("2160p (4K)", 2160)]

#: label -> (crf, x264 preset)
QUALITIES = {
    "Draft (fast, larger artefacts)": (26, "veryfast"),
    "Good": (21, "medium"),
    "High": (18, "medium"),
    "Archive (slow, near lossless)": (14, "slow"),
}
DEFAULT_QUALITY = "High"


def _even(value: int) -> int:
    value = int(round(value))
    return value if value % 2 == 0 else value + 1


class ExportDialog(QDialog):
    def __init__(self, project, parent=None, *, ffmpeg: str = "ffmpeg",
                 default_resolution: str = "1080p", default_quality: str = DEFAULT_QUALITY,
                 default_dir: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export movie")
        self.setModal(True)
        self.setMinimumWidth(520)

        self._project = project
        self._exporter = Exporter(ffmpeg, self)
        self._exporter.progress.connect(self._on_progress)
        self._exporter.finished.connect(self._on_finished)
        self._running = False
        self.result_ok: bool | None = None

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        directory = Path(default_dir) if default_dir else Path(getattr(project, "path", "."))
        name = str(getattr(project, "name", "movie")) or "movie"
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit(str(directory / f"{name}.mp4"))
        path_layout.addWidget(self.path_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_layout.addWidget(browse)
        form.addRow("Output file", path_row)

        self.resolution_combo = QComboBox()
        for label, height in RESOLUTIONS:
            self.resolution_combo.addItem(label, height)
        index = self.resolution_combo.findText(default_resolution)
        self.resolution_combo.setCurrentIndex(index if index >= 0 else 1)
        self.resolution_combo.currentIndexChanged.connect(self._update_summary)
        form.addRow("Resolution", self.resolution_combo)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(list(QUALITIES))
        qindex = self.quality_combo.findText(default_quality)
        self.quality_combo.setCurrentIndex(qindex if qindex >= 0 else 2)
        self.quality_combo.currentIndexChanged.connect(self._update_summary)
        form.addRow("Quality", self.quality_combo)

        root.addLayout(form)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        font = self.summary.font()
        font.setBold(True)
        self.summary.setFont(font)
        root.addWidget(self.summary)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color: #909095;")
        root.addWidget(self.detail)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self._on_close)
        buttons.addWidget(self.close_button)
        self.export_button = QPushButton("Export")
        self.export_button.setDefault(True)
        self.export_button.clicked.connect(self._on_export)
        buttons.addWidget(self.export_button)
        root.addLayout(buttons)

        self._update_summary()

    # ------------------------------------------------------------- summary

    def _fps(self) -> int:
        return max(1, int(getattr(self._project, "fps", 12) or 12))

    def _visible_count(self) -> int:
        try:
            return len(self._project.visible_frames())
        except Exception:
            frames = getattr(self._project, "frames", []) or []
            return len([f for f in frames if not getattr(f, "hidden", False)])

    def _duration_seconds(self) -> float:
        try:
            return self._project.duration_ms(True) / 1000.0
        except Exception:
            holds = 0
            for frame in getattr(self._project, "frames", []) or []:
                if not getattr(frame, "hidden", False):
                    holds += max(1, int(getattr(frame, "holds", 1) or 1))
            return holds / self._fps()

    def _aspect(self) -> float:
        crop = getattr(self._project, "crop", None)
        if crop is not None and getattr(crop, "h", 0):
            return float(crop.w) / float(crop.h)
        size = getattr(self._project, "capture_size", None) or (16, 9)
        try:
            if size[1]:
                return float(size[0]) / float(size[1])
        except Exception:
            pass
        return 16.0 / 9.0

    def output_size(self) -> tuple[int, int]:
        height = int(self.resolution_combo.currentData() or 1080)
        return _even(height * self._aspect()), _even(height)

    def _update_summary(self) -> None:
        count = self._visible_count()
        seconds = self._duration_seconds()
        self.summary.setText(
            f"{count} visible frame{'s' if count != 1 else ''}, "
            f"{seconds:.1f} s at {self._fps()} fps"
        )
        width, height = self.output_size()
        crf, preset = QUALITIES[self.quality_combo.currentText()]
        crop = getattr(self._project, "crop", None)
        crop_text = (
            f"crop {crop.w}×{crop.h} @ {crop.x},{crop.y}" if crop is not None else "no crop"
        )
        self.detail.setText(
            f"{width}×{height} · H.264 crf {crf} preset {preset} · {crop_text} · "
            "hidden frames excluded"
        )
        self.export_button.setEnabled(count > 0 and not self._running)

    # -------------------------------------------------------------- actions

    def _browse(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Export to", self.path_edit.text(), "MP4 video (*.mp4)"
        )
        if chosen:
            self.path_edit.setText(chosen)

    def settings(self) -> ExportSettings:
        width, height = self.output_size()
        crf, preset = QUALITIES[self.quality_combo.currentText()]
        return ExportSettings(
            out_path=Path(self.path_edit.text()).expanduser(),
            width=width,
            height=height,
            crf=crf,
            preset=preset,
            fps=self._fps(),
        )

    def _on_export(self) -> None:
        if self._running:
            return
        settings = self.settings()
        try:
            settings.out_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status.setText(f"Cannot write there: {exc}")
            return
        self._running = True
        self.result_ok = None
        self.export_button.setEnabled(False)
        self.close_button.setText("Cancel")
        self.progress.setValue(0)
        self.status.setText("Encoding…")
        self._exporter.start(self._project, settings)

    def _on_progress(self, percent: int) -> None:
        self.progress.setValue(max(0, min(100, int(percent))))

    def _on_finished(self, ok: bool, message: str) -> None:
        self._running = False
        self.result_ok = bool(ok)
        self.close_button.setText("Close")
        self.export_button.setEnabled(True)
        if ok:
            self.progress.setValue(100)
            self.status.setText(f"Done — {message or self.path_edit.text()}")
        else:
            self.status.setText(f"Failed: {message}")

    def _on_close(self) -> None:
        if self._running:
            self._exporter.cancel()
            self.status.setText("Cancelled")
            self._running = False
            self.close_button.setText("Close")
            self.export_button.setEnabled(True)
            return
        self.accept() if self.result_ok else self.reject()

    def reject(self) -> None:  # noqa: D102
        if self._running:
            self._exporter.cancel()
            self._running = False
        super().reject()
