"""Camera control panel and the transport bar.

Nothing about the camera is hardcoded: every widget in :class:`ControlsPanel`
is built at runtime from the capability ranges that arrived in HELLO.  Until a
HELLO lands the panel shows a placeholder.

The desktop is the source of truth for settings, so the panel always holds a
complete :class:`CameraSettings` and hands a fresh copy out on every change.
"""

from __future__ import annotations

import copy
import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..camera import CameraCapabilities, CameraSettings

SLIDER_STEPS = 1000


def _format_exposure(ns: float) -> str:
    ns = max(1.0, float(ns))
    seconds = ns / 1e9
    if ns >= 1e6:
        primary = f"{ns / 1e6:.1f} ms"
    else:
        primary = f"{ns / 1e3:.0f} µs"
    if seconds > 0:
        return f"{primary}  (1/{max(1, int(round(1.0 / seconds)))} s)"
    return primary


def _log_from_slider(value: int, lo: float, hi: float) -> float:
    t = min(1.0, max(0.0, value / SLIDER_STEPS))
    if lo <= 0 or hi <= lo:
        return lo + (hi - lo) * t
    return lo * math.pow(hi / lo, t)


def _log_to_slider(value: float, lo: float, hi: float) -> int:
    if lo <= 0 or hi <= lo:
        if hi <= lo:
            return 0
        t = (value - lo) / (hi - lo)
    else:
        value = min(hi, max(lo, value))
        t = math.log(value / lo) / math.log(hi / lo)
    return int(round(min(1.0, max(0.0, t)) * SLIDER_STEPS))


def _lin_from_slider(value: int, lo: float, hi: float) -> float:
    t = min(1.0, max(0.0, value / SLIDER_STEPS))
    return lo + (hi - lo) * t


def _lin_to_slider(value: float, lo: float, hi: float) -> int:
    if hi <= lo:
        return 0
    t = (float(value) - lo) / (hi - lo)
    return int(round(min(1.0, max(0.0, t)) * SLIDER_STEPS))


class ControlsPanel(QWidget):
    """Right-hand dock: camera parameters built from the HELLO capabilities."""

    settings_changed = Signal(object)  # CameraSettings
    resend_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._caps: CameraCapabilities | None = None
        self._settings: CameraSettings | None = None
        self._loading = False

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(8, 8, 8, 8)
        self._root.setSpacing(8)

        self._placeholder = QLabel("Waiting for the phone to report its camera capabilities…")
        self._placeholder.setWordWrap(True)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._root.addWidget(self._placeholder)
        self._root.addStretch(1)

        self._body: QWidget | None = None
        self.lock_all: QCheckBox | None = None
        self.ae_lock: QCheckBox | None = None
        self.af_lock: QCheckBox | None = None
        self.awb_lock: QCheckBox | None = None
        self.exposure_slider: QSlider | None = None
        self.iso_slider: QSlider | None = None
        self.focus_slider: QSlider | None = None
        self.awb_combo: QComboBox | None = None
        self.preview_size_combo: QComboBox | None = None
        self.quality_slider: QSlider | None = None
        self.ack_label: QLabel | None = None

    # ------------------------------------------------------------ building

    def capabilities(self) -> CameraCapabilities | None:
        return self._caps

    def settings(self) -> CameraSettings | None:
        return self._settings

    def set_capabilities(
        self, caps: CameraCapabilities, settings: CameraSettings | None = None
    ) -> None:
        """(Re)build every widget from the reported ranges."""
        self._caps = caps
        if self._body is not None:
            self._body.setParent(None)
            self._body.deleteLater()
            self._body = None
        self._placeholder.setVisible(False)

        base = settings if settings is not None else CameraSettings.defaults(caps)
        try:
            base = base.clamped(caps)
        except Exception:
            pass
        self._settings = base

        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layout.addWidget(self._build_lock_box(base))
        layout.addWidget(self._build_exposure_box(caps, base))
        layout.addWidget(self._build_preview_box(caps, base))

        self.ack_label = QLabel("")
        self.ack_label.setWordWrap(True)
        self.ack_label.setStyleSheet("color: #d0a040;")
        layout.addWidget(self.ack_label)

        resend = QPushButton("Re-send settings to phone")
        resend.clicked.connect(self.resend_requested.emit)
        layout.addWidget(resend)

        info = QLabel(
            f"{caps.hardware_level}  ·  sensor {caps.sensor_size[0]}×{caps.sensor_size[1]}\n"
            f"focus calibration: {caps.focus_calibration}"
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #909095;")
        layout.addWidget(info)
        layout.addStretch(1)

        self._body = body
        self._root.insertWidget(0, body)
        self._sync_widgets()

    def _build_lock_box(self, settings: CameraSettings) -> QWidget:
        box = QGroupBox("Stability")
        outer = QVBoxLayout(box)

        self.lock_all = QCheckBox("LOCK ALL  (AE / AF / AWB)")
        font = self.lock_all.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 1.5)
        self.lock_all.setFont(font)
        self.lock_all.setToolTip(
            "Locks auto-exposure, auto-focus and auto-white-balance.\n"
            "Without this consecutive frames flicker and the movie is unusable."
        )
        self.lock_all.setChecked(
            bool(settings.ae_lock and settings.af_lock and settings.awb_lock)
        )
        self.lock_all.toggled.connect(self._on_lock_all)
        outer.addWidget(self.lock_all)

        row = QHBoxLayout()
        self.ae_lock = QCheckBox("AE")
        self.af_lock = QCheckBox("AF")
        self.awb_lock = QCheckBox("AWB")
        for widget, value in (
            (self.ae_lock, settings.ae_lock),
            (self.af_lock, settings.af_lock),
            (self.awb_lock, settings.awb_lock),
        ):
            widget.setChecked(bool(value))
            widget.toggled.connect(self._on_individual_lock)
            row.addWidget(widget)
        row.addStretch(1)
        outer.addLayout(row)

        hint = QLabel("Keep this on for flicker-free stopmotion.")
        hint.setStyleSheet("color: #909095;")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        return box

    def _build_exposure_box(
        self, caps: CameraCapabilities, settings: CameraSettings
    ) -> QWidget:
        box = QGroupBox("Camera")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        # exposure (log scale over the reported range)
        self.exposure_slider = QSlider(Qt.Orientation.Horizontal)
        self.exposure_slider.setRange(0, SLIDER_STEPS)
        self.exposure_slider.setToolTip(
            f"{_format_exposure(caps.exposure_ns.lo)} … {_format_exposure(caps.exposure_ns.hi)}"
        )
        self._exposure_value = QLabel("")
        self.exposure_slider.valueChanged.connect(self._on_exposure)
        form.addRow(self._titled("Exposure time", self._exposure_value), self.exposure_slider)

        # iso
        self.iso_slider = QSlider(Qt.Orientation.Horizontal)
        self.iso_slider.setRange(int(caps.iso.lo), max(int(caps.iso.lo) + 1, int(caps.iso.hi)))
        self.iso_slider.setToolTip(f"{int(caps.iso.lo)} … {int(caps.iso.hi)}")
        self._iso_value = QLabel("")
        self.iso_slider.valueChanged.connect(self._on_iso)
        form.addRow(self._titled("ISO", self._iso_value), self.iso_slider)

        # focus
        uncalibrated = str(caps.focus_calibration).upper() == "UNCALIBRATED"
        self.focus_slider = QSlider(Qt.Orientation.Horizontal)
        self.focus_slider.setRange(0, SLIDER_STEPS)
        self._focus_value = QLabel("")
        self.focus_slider.valueChanged.connect(self._on_focus)
        self._focus_uncalibrated = uncalibrated
        focus_title = "Focus (relative scale)" if uncalibrated else "Focus distance"
        if uncalibrated:
            self.focus_slider.setToolTip(
                "This device reports LENS_INFO_FOCUS_DISTANCE_CALIBRATION=UNCALIBRATED,\n"
                "so the value is a relative position, not metres or diopters."
            )
        else:
            self.focus_slider.setToolTip(
                f"{caps.focus_diopters.lo:.2f} … {caps.focus_diopters.hi:.2f} diopters"
            )
        form.addRow(self._titled(focus_title, self._focus_value), self.focus_slider)

        # awb
        self.awb_combo = QComboBox()
        for mode in caps.awb_modes:
            self.awb_combo.addItem(str(mode))
        self.awb_combo.currentTextChanged.connect(self._on_awb)
        form.addRow(QLabel("White balance"), self.awb_combo)
        return box

    def _build_preview_box(
        self, caps: CameraCapabilities, settings: CameraSettings
    ) -> QWidget:
        box = QGroupBox("Preview stream")
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.preview_size_combo = QComboBox()
        for size in caps.preview_sizes:
            w, h = int(size[0]), int(size[1])
            self.preview_size_combo.addItem(f"{w}×{h}", (w, h))
        self.preview_size_combo.currentIndexChanged.connect(self._on_preview_size)
        form.addRow(QLabel("Size"), self.preview_size_combo)

        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(10, 100)
        self._quality_value = QLabel("")
        self.quality_slider.valueChanged.connect(self._on_quality)
        form.addRow(self._titled("JPEG quality", self._quality_value), self.quality_slider)
        return box

    @staticmethod
    def _titled(text: str, value_label: QLabel) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        title = QLabel(text)
        layout.addWidget(title)
        value_label.setStyleSheet("color: #7fb8e8;")
        layout.addWidget(value_label)
        return holder

    # ------------------------------------------------------------- updating

    def set_settings(self, settings: CameraSettings) -> None:
        """Adopt settings without emitting (used on load and on CONFIG_ACK)."""
        self._settings = settings
        self._sync_widgets()

    def _sync_widgets(self) -> None:
        if self._settings is None or self._caps is None or self._body is None:
            return
        self._loading = True
        try:
            s, caps = self._settings, self._caps
            self.exposure_slider.setValue(
                _log_to_slider(s.exposure_ns, caps.exposure_ns.lo, caps.exposure_ns.hi)
            )
            self._exposure_value.setText(_format_exposure(s.exposure_ns))
            self.iso_slider.setValue(int(s.iso))
            self._iso_value.setText(str(int(s.iso)))
            self.focus_slider.setValue(
                _lin_to_slider(s.focus_diopters, caps.focus_diopters.lo, caps.focus_diopters.hi)
            )
            self._focus_value.setText(self._focus_text(s.focus_diopters))
            index = self.awb_combo.findText(str(s.awb_mode))
            if index >= 0:
                self.awb_combo.setCurrentIndex(index)
            size_index = self.preview_size_combo.findData((s.preview_width, s.preview_height))
            if size_index >= 0:
                self.preview_size_combo.setCurrentIndex(size_index)
            self.quality_slider.setValue(int(s.preview_quality))
            self._quality_value.setText(f"{int(s.preview_quality)}")
            self.ae_lock.setChecked(bool(s.ae_lock))
            self.af_lock.setChecked(bool(s.af_lock))
            self.awb_lock.setChecked(bool(s.awb_lock))
            self.lock_all.setChecked(bool(s.ae_lock and s.af_lock and s.awb_lock))
        finally:
            self._loading = False

    def _focus_text(self, diopters: float) -> str:
        caps = self._caps
        if caps is None:
            return f"{diopters:.2f}"
        if self._focus_uncalibrated:
            pct = _lin_to_slider(diopters, caps.focus_diopters.lo, caps.focus_diopters.hi)
            return f"{pct / 10.0:.0f} %  (relative)"
        if diopters <= 0.0001:
            return "∞"
        return f"{diopters:.2f} dpt  ≈ {1.0 / diopters:.2f} m"

    def _emit(self) -> None:
        if self._loading or self._settings is None:
            return
        self.settings_changed.emit(copy.copy(self._settings))

    # -------------------------------------------------------------- handlers

    def _on_lock_all(self, checked: bool) -> None:
        if self._loading or self._settings is None:
            return
        self._loading = True
        try:
            for widget in (self.ae_lock, self.af_lock, self.awb_lock):
                widget.setChecked(checked)
        finally:
            self._loading = False
        self._settings.ae_lock = bool(checked)
        self._settings.af_lock = bool(checked)
        self._settings.awb_lock = bool(checked)
        self._emit()

    def _on_individual_lock(self, _checked: bool) -> None:
        if self._loading or self._settings is None:
            return
        self._settings.ae_lock = self.ae_lock.isChecked()
        self._settings.af_lock = self.af_lock.isChecked()
        self._settings.awb_lock = self.awb_lock.isChecked()
        self._loading = True
        try:
            self.lock_all.setChecked(
                self._settings.ae_lock and self._settings.af_lock and self._settings.awb_lock
            )
        finally:
            self._loading = False
        self._emit()

    def _on_exposure(self, value: int) -> None:
        if self._settings is None or self._caps is None:
            return
        ns = int(
            _log_from_slider(value, self._caps.exposure_ns.lo, self._caps.exposure_ns.hi)
        )
        self._settings.exposure_ns = ns
        self._exposure_value.setText(_format_exposure(ns))
        self._emit()

    def _on_iso(self, value: int) -> None:
        if self._settings is None:
            return
        self._settings.iso = int(value)
        self._iso_value.setText(str(int(value)))
        self._emit()

    def _on_focus(self, value: int) -> None:
        if self._settings is None or self._caps is None:
            return
        diopters = _lin_from_slider(
            value, self._caps.focus_diopters.lo, self._caps.focus_diopters.hi
        )
        self._settings.focus_diopters = float(diopters)
        self._focus_value.setText(self._focus_text(diopters))
        self._emit()

    def _on_awb(self, text: str) -> None:
        if self._settings is None or not text:
            return
        self._settings.awb_mode = text
        self._emit()

    def _on_preview_size(self, index: int) -> None:
        if self._settings is None or index < 0:
            return
        data = self.preview_size_combo.itemData(index)
        if not data:
            return
        self._settings.preview_width = int(data[0])
        self._settings.preview_height = int(data[1])
        self._emit()

    def _on_quality(self, value: int) -> None:
        if self._settings is None:
            return
        self._settings.preview_quality = int(value)
        self._quality_value.setText(str(int(value)))
        self._emit()

    # ------------------------------------------------------------ CONFIG_ACK

    def apply_ack(self, applied: dict) -> None:
        """Show what the phone *actually* applied, not what we asked for."""
        if self._settings is None or not isinstance(applied, dict):
            return
        requested = self._settings
        try:
            merged = CameraSettings.from_json({**requested.to_json(), **applied})
        except Exception:
            merged = requested
        diffs: list[str] = []
        for field, label, fmt in (
            ("exposure_ns", "exposure", lambda v: _format_exposure(v)),
            ("iso", "ISO", lambda v: str(int(v))),
            ("focus_diopters", "focus", lambda v: f"{float(v):.2f}"),
            ("awb_mode", "AWB", str),
            ("preview_width", "preview w", lambda v: str(int(v))),
            ("preview_height", "preview h", lambda v: str(int(v))),
            ("preview_quality", "quality", lambda v: str(int(v))),
        ):
            before = getattr(requested, field, None)
            after = getattr(merged, field, None)
            if before is None or after is None:
                continue
            if isinstance(before, float) or isinstance(after, float):
                changed = abs(float(before) - float(after)) > 1e-6
            else:
                changed = before != after
            if changed:
                diffs.append(f"{label} {fmt(before)} → {fmt(after)}")
        self._settings = merged
        self._sync_widgets()
        if self.ack_label is not None:
            self.ack_label.setText(
                "Phone clamped: " + "; ".join(diffs) if diffs else ""
            )


class TransportBar(QWidget):
    """Row under the viewport: mode, playback, onion skin, capture."""

    mode_toggle_requested = Signal()
    play_pause_requested = Signal()
    step_requested = Signal(int)
    loop_changed = Signal(bool)
    onion_enabled_changed = Signal(bool)
    onion_depth_changed = Signal(int)
    onion_opacity_changed = Signal(float)
    capture_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        self.mode_button = QPushButton("Mode: LIVE")
        self.mode_button.setToolTip("Toggle Live / Review  (Space)")
        self.mode_button.clicked.connect(self.mode_toggle_requested.emit)
        layout.addWidget(self.mode_button)

        self.step_back = QPushButton("⏮")
        self.step_back.setFixedWidth(36)
        self.step_back.clicked.connect(lambda: self.step_requested.emit(-1))
        layout.addWidget(self.step_back)

        self.play_button = QPushButton("▶")
        self.play_button.setFixedWidth(46)
        self.play_button.clicked.connect(self.play_pause_requested.emit)
        layout.addWidget(self.play_button)

        self.step_fwd = QPushButton("⏭")
        self.step_fwd.setFixedWidth(36)
        self.step_fwd.clicked.connect(lambda: self.step_requested.emit(1))
        layout.addWidget(self.step_fwd)

        self.loop_check = QCheckBox("Loop")
        self.loop_check.setChecked(True)
        self.loop_check.toggled.connect(self.loop_changed.emit)
        layout.addWidget(self.loop_check)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

        self.onion_check = QCheckBox("Onion")
        self.onion_check.setChecked(True)
        self.onion_check.setToolTip("Toggle the onion skin overlay  (O)")
        self.onion_check.toggled.connect(self.onion_enabled_changed.emit)
        layout.addWidget(self.onion_check)

        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(1, 3)
        self.depth_spin.setPrefix("depth ")
        self.depth_spin.valueChanged.connect(self.onion_depth_changed.emit)
        layout.addWidget(self.depth_spin)

        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(45)
        self.opacity_slider.setFixedWidth(140)
        self.opacity_slider.setToolTip("Onion skin opacity")
        self.opacity_slider.valueChanged.connect(self._on_opacity)
        layout.addWidget(self.opacity_slider)
        self.opacity_label = QLabel("45%")
        self.opacity_label.setFixedWidth(40)
        layout.addWidget(self.opacity_label)

        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #909095;")
        self.info_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.info_label, 1)

        self.capture_button = QPushButton("CAPTURE")
        font = self.capture_button.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 3)
        self.capture_button.setFont(font)
        self.capture_button.setMinimumHeight(48)
        self.capture_button.setMinimumWidth(180)
        self.capture_button.setToolTip("Capture a frame  (Ctrl+Return, F5, or Enter in the viewport)")
        self.capture_button.clicked.connect(self.capture_requested.emit)
        layout.addWidget(self.capture_button)

    def _on_opacity(self, value: int) -> None:
        self.opacity_label.setText(f"{value}%")
        self.onion_opacity_changed.emit(value / 100.0)

    # -- state pushed in by MainWindow ------------------------------------

    def set_mode(self, mode: str) -> None:
        self.mode_button.setText(f"Mode: {mode.upper()}")
        review = mode == "review"
        for widget in (self.play_button, self.step_back, self.step_fwd, self.loop_check):
            widget.setEnabled(review)
        for widget in (self.onion_check, self.depth_spin, self.opacity_slider):
            widget.setEnabled(not review)

    def set_playing(self, playing: bool) -> None:
        self.play_button.setText("⏸" if playing else "▶")

    def set_capture_state(self, enabled: bool, text: str = "CAPTURE") -> None:
        self.capture_button.setEnabled(enabled)
        self.capture_button.setText(text)

    def set_info(self, text: str) -> None:
        self.info_label.setText(text)

    def set_onion(self, enabled: bool, depth: int, opacity: float) -> None:
        blocked = [self.onion_check, self.depth_spin, self.opacity_slider]
        for widget in blocked:
            widget.blockSignals(True)
        try:
            self.onion_check.setChecked(bool(enabled))
            self.depth_spin.setValue(int(depth))
            self.opacity_slider.setValue(int(round(opacity * 100)))
            self.opacity_label.setText(f"{int(round(opacity * 100))}%")
        finally:
            for widget in blocked:
                widget.blockSignals(False)


class DisconnectBanner(QFrame):
    """Fat warning strip shown across the top of the central area."""

    reconnect_requested = Signal()
    launch_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame { background: #6b2020; border: 1px solid #a33; }"
            "QLabel { color: #ffe9e9; font-weight: bold; }"
        )
        layout = QGridLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        self.label = QLabel("Phone disconnected")
        layout.addWidget(self.label, 0, 0)
        self.reconnect_button = QPushButton("Reconnect now")
        self.reconnect_button.clicked.connect(self.reconnect_requested.emit)
        layout.addWidget(self.reconnect_button, 0, 1)
        self.launch_button = QPushButton("Launch phone app")
        self.launch_button.clicked.connect(self.launch_requested.emit)
        layout.addWidget(self.launch_button, 0, 2)
        layout.setColumnStretch(0, 1)
        self.setVisible(False)

    def show_message(self, text: str, *, can_launch: bool = False) -> None:
        self.label.setText(text)
        self.launch_button.setVisible(can_launch)
        self.setVisible(True)
