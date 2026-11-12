"""The Audio tab: microphone, desktop audio and media."""
from __future__ import annotations



from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QProgressBar, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from .. import config
from ..engine import audio_helpers
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class AudioPanelMixin:
    """The Audio tab: microphone, desktop audio and media."""

    def _audio_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        box = QGroupBox("Microphone")
        grid = QGridLayout(box)
        self.audio_check = QCheckBox("Include microphone in recordings and streams")
        self.audio_check.setChecked(self.audio_on)
        self.audio_check.toggled.connect(self._toggle_audio)
        grid.addWidget(self.audio_check, 0, 0, 1, 2)

        self.mic_combo = QComboBox()
        for source in config.list_audio_sources():
            label = source.description or source.name
            self.mic_combo.addItem(label, source.name)
        current = self.project.audio.mic_device or config.microphone()
        index = self.mic_combo.findData(current)
        if index >= 0:
            self.mic_combo.setCurrentIndex(index)
        self.mic_combo.currentIndexChanged.connect(self._mic_changed)
        grid.addWidget(QLabel("Device"), 1, 0)
        grid.addWidget(self.mic_combo, 1, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_mics)
        grid.addWidget(refresh, 1, 2)

        self.mic_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_gain_slider.setRange(-30, 20)
        self.mic_gain_slider.setValue(int(self.project.audio.mic_gain_db))
        self.mic_gain_slider.valueChanged.connect(self._mic_gain_changed)
        self.mic_gain_label = QLabel(f"{self.project.audio.mic_gain_db:+.0f} dB")
        self.mic_gain_label.setObjectName("accent")
        gain_row = QHBoxLayout()
        gain_row.addWidget(self.mic_gain_slider, 1)
        gain_row.addWidget(self.mic_gain_label)
        grid.addWidget(QLabel("Gain"), 2, 0)
        grid.addLayout(gain_row, 2, 1)

        self.mute_check = QCheckBox("Mute microphone")
        self.mute_check.setChecked(self.project.audio.mic_muted)
        self.mute_check.toggled.connect(self._mic_mute_changed)
        grid.addWidget(self.mute_check, 3, 0, 1, 2)
        self.mic_level_label = QLabel("—")
        self.mic_level_label.setObjectName("accent")
        grid.addWidget(QLabel("Level"), 4, 0)
        grid.addWidget(self.mic_level_label, 4, 1)

        # Voice processing (webrtcdsp): steady background noise such as fans,
        # hum and hiss.  Applied to recordings and streams.
        self.denoise_combo = QComboBox()
        for label, value in (("Off", "off"), ("Low", "low"), ("Moderate", "moderate"),
                             ("High", "high"), ("Very high", "very-high")):
            self.denoise_combo.addItem(label, value)
        index = self.denoise_combo.findData(self.project.audio.mic_denoise)
        self.denoise_combo.setCurrentIndex(index if index >= 0 else 0)
        self.denoise_combo.setToolTip(
            "Removes steady background noise (fans, hum, hiss) from the "
            "microphone. Higher levels remove more but can colour the voice.")
        self.denoise_combo.currentIndexChanged.connect(self._mic_processing_changed)
        grid.addWidget(QLabel("Noise suppression"), 5, 0)
        grid.addWidget(self.denoise_combo, 5, 1)
        self.auto_gain_check = QCheckBox("Automatic gain (even out loud and quiet speech)")
        self.auto_gain_check.setChecked(self.project.audio.mic_auto_gain)
        self.auto_gain_check.toggled.connect(self._mic_processing_changed)
        grid.addWidget(self.auto_gain_check, 6, 0, 1, 2)
        layout.addWidget(box)
        layout.addWidget(self._mixer_group())
        layout.addWidget(self._sync_group())
        layout.addWidget(self._outputs_group())

        layout.addStretch(1)
        return page

    # ----------------------------------------------------------- the mixer
    METER_INPUTS = (("mic", "Microphone"), ("desktop", "Desktop"), ("media", "Music"),
                    ("master", "Output"))

    def _mixer_group(self) -> QGroupBox:
        box = QGroupBox("Mixer")
        grid = QGridLayout(box)
        self.meter_bars: dict[str, QProgressBar] = {}
        for row, (key, label) in enumerate(self.METER_INPUTS):
            bar = QProgressBar()
            bar.setRange(-60, 0)
            bar.setValue(-60)
            bar.setTextVisible(False)
            bar.setMaximumHeight(10)
            bar.setAccessibleName(f"{label} level")
            self.meter_bars[key] = bar
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(bar, row, 1)
        self.dynamics_combo = QComboBox()
        for key, label in audio_helpers.DYNAMICS_LABELS.items():
            self.dynamics_combo.addItem(label, key)
        self.dynamics_combo.setCurrentIndex(max(0, self.dynamics_combo.findData(
            self.project.audio.mic_dynamics)))
        self.dynamics_combo.setToolTip(
            "Noise gate silences the room between words; the compressor evens out loud peaks")
        self.dynamics_combo.currentIndexChanged.connect(self._voice_fx_changed)
        self.eq_combo = QComboBox()
        for key, label in audio_helpers.EQ_LABELS.items():
            self.eq_combo.addItem(label, key)
        self.eq_combo.setCurrentIndex(max(0, self.eq_combo.findData(self.project.audio.mic_eq)))
        self.eq_combo.currentIndexChanged.connect(self._voice_fx_changed)
        row = len(self.METER_INPUTS)
        grid.addWidget(QLabel("Gate / compressor"), row, 0)
        grid.addWidget(self.dynamics_combo, row, 1)
        grid.addWidget(QLabel("Equaliser"), row + 1, 0)
        grid.addWidget(self.eq_combo, row + 1, 1)
        return box

    def _sync_group(self) -> QGroupBox:
        box = QGroupBox("Sync offset (hold audio back)")
        grid = QGridLayout(box)
        self.delay_spins: dict[str, QSpinBox] = {}
        for row, (key, label) in enumerate((("mic_delay_ms", "Microphone"),
                                            ("desktop_delay_ms", "Desktop"),
                                            ("media_delay_ms", "Music"))):
            spin = QSpinBox()
            spin.setRange(0, audio_helpers.MAX_DELAY_MS)
            spin.setSingleStep(10)
            spin.setSuffix(" ms")
            spin.setKeyboardTracking(False)
            spin.setValue(int(getattr(self.project.audio, key)))
            spin.valueChanged.connect(lambda value, field=key: self._audio_delay_changed(field, value))
            self.delay_spins[key] = spin
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(spin, row, 1)
        return box

    def _outputs_group(self) -> QGroupBox:
        box = QGroupBox("Audio outputs")
        grid = QGridLayout(box)
        self.monitor_combo = QComboBox()
        self.monitor_combo.addItem("Off", "")
        self.monitor_combo.addItem("Default output", "default")
        for sink in config.list_audio_sinks():
            self.monitor_combo.addItem(sink.description or sink.name, sink.name)
        self.monitor_combo.setCurrentIndex(max(0, self.monitor_combo.findData(
            self.project.audio.monitor_device)))
        self.monitor_combo.setToolTip("Listen to the mix (what stream viewers hear)")
        self.monitor_combo.currentIndexChanged.connect(self._audio_outputs_changed)
        self.virtual_mic_check = QCheckBox(
            f"Virtual microphone (\u201c{audio_helpers.VIRTUAL_MIC_NAME}\u201d for other apps)")
        self.virtual_mic_check.setChecked(self.project.audio.virtual_mic)
        self.virtual_mic_check.toggled.connect(self._audio_outputs_changed)
        self.audio_output_note = QLabel("")
        self.audio_output_note.setObjectName("muted")
        self.audio_output_note.setWordWrap(True)
        grid.addWidget(QLabel("Monitor"), 0, 0)
        grid.addWidget(self.monitor_combo, 0, 1)
        grid.addWidget(self.virtual_mic_check, 1, 0, 1, 2)
        grid.addWidget(self.audio_output_note, 2, 0, 1, 2)
        return box

    def _voice_fx_changed(self, *_args) -> None:
        self.project.audio.mic_dynamics = str(self.dynamics_combo.currentData() or "off")
        self.project.audio.mic_eq = str(self.eq_combo.currentData() or "flat")
        self._mark_dirty()
        if self.engine is not None and self.project.audio.mic_enabled:
            self.restart_engine()

    def _audio_delay_changed(self, field: str, value: int) -> None:
        setattr(self.project.audio, field, audio_helpers.delay_ms(value))
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

    def _audio_outputs_changed(self, *_args) -> None:
        self.project.audio.monitor_device = str(self.monitor_combo.currentData() or "")
        self.project.audio.virtual_mic = self.virtual_mic_check.isChecked()
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

    def update_audio_meters(self) -> None:
        """Move the mixer bars to the engine's latest levels (UI timer)."""
        bars = getattr(self, "meter_bars", None)
        engine = self.engine
        if not bars:
            return
        levels = engine.audio_levels() if engine is not None else {}
        for key, bar in bars.items():
            peak = levels.get(key, (-100.0, -100.0))[0]
            bar.setValue(int(max(-60.0, min(0.0, peak))))
        report = engine.report if engine is not None else None
        if report is not None:
            notes = [text for text in (
                f"Monitor: {report.audio_monitor}" if report.audio_monitor else "",
                f"Monitor unavailable: {report.audio_monitor_error}" if report.audio_monitor_error else "",
                f"Virtual microphone: {report.virtual_mic}" if report.virtual_mic else "",
                f"Virtual microphone unavailable: {report.virtual_mic_error}"
                if report.virtual_mic_error else "") if text]
            self.audio_output_note.setText("  ·  ".join(notes))

    def _refresh_mics(self) -> None:
        current = self.mic_combo.currentData() or self.project.audio.mic_device
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        for source in config.list_audio_sources():
            self.mic_combo.addItem(source.description or source.name, source.name)
        index = self.mic_combo.findData(current)
        if index >= 0:
            self.mic_combo.setCurrentIndex(index)
        self.mic_combo.blockSignals(False)
        self._status(f"{self.mic_combo.count()} microphone(s) found")

    def _desktop_audio_changed(self, _index: int) -> None:
        device = self.desktop_combo.currentData() or ""
        self.project.audio.desktop_device = device
        self.project.audio.desktop_muted = False
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        self._status("Desktop audio off" if not device
                     else f"Desktop audio: {device}")

    def _mic_processing_changed(self, *_args) -> None:
        self.project.audio.mic_denoise = self.denoise_combo.currentData() or "off"
        self.project.audio.mic_auto_gain = self.auto_gain_check.isChecked()
        self._mark_dirty()
        # webrtcdsp reads its settings when it starts: rebuild to apply.
        if self.engine is not None and self.project.audio.mic_enabled:
            self.restart_engine()
        level = self.denoise_combo.currentText()
        self._status(f"Noise suppression: {level}"
                     + (" · automatic gain" if self.project.audio.mic_auto_gain else ""))

    def _toggle_audio(self, enabled: bool) -> None:
        self.audio_on = enabled
        self.project.audio.mic_enabled = enabled
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        self._status("Microphone enabled" if enabled else "Microphone disabled")

    def _mic_changed(self, _index: int) -> None:
        self.project.audio.mic_device = self.mic_combo.currentData() or ""
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

    def _mic_gain_changed(self, value: int) -> None:
        self.project.audio.mic_gain_db = float(value)
        self.mic_gain_label.setText(f"{value:+d} dB")
        self._mark_dirty()
        if self.engine is not None:
            self.engine.set_mic_gain(float(value))

    def _mic_mute_changed(self, muted: bool) -> None:
        self.project.audio.mic_muted = bool(muted)
        self._mark_dirty()
        if self.engine is not None:
            self.engine.set_mic_muted(bool(muted))

    def _media_gain_changed(self, value: int) -> None:
        self.project.audio.media_gain_db = float(value)
        self.media_gain_label.setText(f"{value:+d} dB")
        self._mark_dirty()
        if self.engine is not None and self.engine.media_volume is not None:
            self.engine.media_volume.set_property("volume", 10 ** (value / 20.0))

    def _pick_media(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose audio", "", "Audio (*.wav *.mp3 *.ogg *.flac *.m4a *.aac)")
        if path:
            self.media_edit.setText(path)
            self.project.audio.media_file = path
            self._mark_dirty()
            if self.engine is not None:
                self.restart_engine()

    def toggle_mic_mute(self) -> None:
        muted = not self.project.audio.mic_muted
        self.project.audio.mic_muted = muted
        if hasattr(self, "mute_check"):
            self.mute_check.blockSignals(True)
            self.mute_check.setChecked(muted)
            self.mute_check.blockSignals(False)
        self._mark_dirty()
        if self.engine is not None:
            self.engine.set_mic_muted(muted)
        self._status("Microphone muted" if muted else "Microphone live")
