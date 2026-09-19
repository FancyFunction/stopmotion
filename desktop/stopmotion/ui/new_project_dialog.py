"""New Project dialog: name, location, fps, crop aspect."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

#: "No crop" is a real option -- some shots want the full sensor frame.
ASPECTS = ["16:9", "4:3", "3:2", "1:1", "9:16", "No crop"]
NO_CROP = "No crop"


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    return slug or "untitled"


class NewProjectDialog(QDialog):
    def __init__(self, parent=None, *, default_dir: str = "", default_fps: int = 12,
                 default_aspect: str = "16:9") -> None:
        super().__init__(parent)
        self.setWindowTitle("New project")
        self.setModal(True)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.name_edit = QLineEdit("my-film")
        self.name_edit.textChanged.connect(self._update_preview)
        form.addRow("Name", self.name_edit)

        location_row = QWidget()
        location_layout = QHBoxLayout(location_row)
        location_layout.setContentsMargins(0, 0, 0, 0)
        self.location_edit = QLineEdit(default_dir or str(Path.home()))
        self.location_edit.textChanged.connect(self._update_preview)
        location_layout.addWidget(self.location_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        location_layout.addWidget(browse)
        form.addRow("Location", location_row)

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(int(default_fps or 12))
        self.fps_spin.setSuffix(" fps")
        self.fps_spin.valueChanged.connect(self._update_preview)
        form.addRow("Frame rate", self.fps_spin)

        self.aspect_combo = QComboBox()
        self.aspect_combo.addItems(ASPECTS)
        index = self.aspect_combo.findText(default_aspect or "16:9")
        self.aspect_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow("Crop aspect", self.aspect_combo)

        root.addLayout(form)

        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("color: #909095;")
        root.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.buttons = buttons
        root.addWidget(buttons)

        self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.name_edit.selectAll()
        self._update_preview()

    # ---------------------------------------------------------------------

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Project location", self.location_edit.text()
        )
        if chosen:
            self.location_edit.setText(chosen)

    def project_path(self) -> Path:
        return Path(self.location_edit.text()).expanduser() / slugify(self.name_edit.text())

    def _update_preview(self) -> None:
        fps = self.fps_spin.value()
        self.preview.setText(
            f"Folder: {self.project_path()}\n"
            f"One hold = {1000.0 / max(1, fps):.0f} ms at {fps} fps"
        )

    def _accept(self) -> None:
        path = self.project_path()
        if path.exists() and any(path.iterdir()):
            self.preview.setText(
                f"{path} already exists and is not empty — pick another name or location."
            )
            self.preview.setStyleSheet("color: #e08080;")
            return
        self.accept()

    def values(self) -> dict:
        aspect = self.aspect_combo.currentText()
        return {
            "name": self.name_edit.text().strip() or "untitled",
            "path": self.project_path(),
            "fps": int(self.fps_spin.value()),
            "aspect": None if aspect == NO_CROP else aspect,
        }
