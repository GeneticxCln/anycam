"""The playlist tab and its rotation."""
from __future__ import annotations


import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from .. import config
from ..model import (Layer, new_id)
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class PlaylistMixin:
    """The playlist tab and its rotation."""

    def _playlist_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        info = QLabel("Rotate images and video clips into the scene on a timer. "
                      "Items are composited full-frame underneath the overlays.")
        info.setObjectName("muted")
        info.setWordWrap(True)
        layout.addWidget(info)
        self.playlist_list = QListWidget()
        layout.addWidget(self.playlist_list, 1)
        row = QHBoxLayout()
        add = QPushButton("+ Add media")
        add.clicked.connect(self._add_playlist_items)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_playlist)
        row.addWidget(add)
        row.addWidget(clear)
        layout.addLayout(row)
        grid = QGridLayout()
        self.playlist_interval = QDoubleSpinBox()
        self.playlist_interval.setRange(0.5, 120.0)
        self.playlist_interval.setValue(3.0)
        self.playlist_interval.setSuffix(" s")
        self.playlist_interval.valueChanged.connect(self._playlist_settings_changed)
        grid.addWidget(QLabel("Interval"), 0, 0)
        grid.addWidget(self.playlist_interval, 0, 1)
        self.playlist_enabled = QCheckBox("Loop playlist")
        self.playlist_enabled.setChecked(True)
        self.playlist_enabled.toggled.connect(self._playlist_settings_changed)
        grid.addWidget(self.playlist_enabled, 1, 0, 1, 2)
        layout.addLayout(grid)

        music = QGroupBox("Music (audio file)")
        music_grid = QGridLayout(music)
        self.media_edit = QLineEdit(self.project.audio.media_file)
        self.media_edit.textChanged.connect(lambda *_: self._mark_dirty())
        browse = QPushButton("Browse")
        browse.clicked.connect(self._pick_media)
        music_grid.addWidget(QLabel("File"), 0, 0)
        music_grid.addWidget(self.media_edit, 0, 1)
        music_grid.addWidget(browse, 0, 2)
        self.media_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.media_gain_slider.setRange(-30, 20)
        self.media_gain_slider.setValue(int(self.project.audio.media_gain_db))
        self.media_gain_slider.valueChanged.connect(self._media_gain_changed)
        self.media_gain_label = QLabel(f"{self.project.audio.media_gain_db:+.0f} dB")
        self.media_gain_label.setObjectName("accent")
        music_row = QHBoxLayout()
        music_row.addWidget(self.media_gain_slider, 1)
        music_row.addWidget(self.media_gain_label)
        music_grid.addWidget(QLabel("Gain"), 1, 0)
        music_grid.addLayout(music_row, 1, 1, 1, 2)
        music_note = QLabel("Music is mixed with the microphone through an "
                            "audiomixer at 48 kHz stereo.")
        music_note.setObjectName("muted")
        music_note.setWordWrap(True)
        music_grid.addWidget(music_note, 2, 0, 1, 3)
        layout.addWidget(music)

        desktop = QGroupBox("Desktop audio (optional)")
        desktop_grid = QGridLayout(desktop)
        self.desktop_combo = QComboBox()
        self.desktop_combo.addItem("Off", "")
        for source in config.list_audio_sources():
            if source.is_monitor:
                self.desktop_combo.addItem(source.description or source.name,
                                           source.name)
        index = self.desktop_combo.findData(self.project.audio.desktop_device)
        if index >= 0:
            self.desktop_combo.setCurrentIndex(index)
        self.desktop_combo.currentIndexChanged.connect(
            self._desktop_audio_changed)
        desktop_grid.addWidget(QLabel("Source"), 0, 0)
        desktop_grid.addWidget(self.desktop_combo, 0, 1)
        desktop_note = QLabel(
            "Monitor sources are the 'what you hear' streams from "
            "PipeWire/PulseAudio. They are mixed with the mic.")
        desktop_note.setObjectName("muted")
        desktop_note.setWordWrap(True)
        desktop_grid.addWidget(desktop_note, 1, 0, 1, 2)
        layout.addWidget(desktop)

        layout.addStretch(1)
        self._load_playlist_state()
        return page

    def _load_playlist_state(self) -> None:
        """Mirror the active scene's persisted playlist into the widgets."""
        state = self.scene.playlist
        valid = {layer.id for layer in self.scene.layers}
        ids = [layer_id for layer_id in state.layer_ids if layer_id in valid]
        state.layer_ids = ids
        self.playlist_ids = ids
        index = max(-1, min(int(state.index), len(ids) - 1)) if ids else -1
        self.playlist_index = index
        self.playlist_interval.blockSignals(True)
        self.playlist_interval.setValue(max(0.5, min(120.0, float(state.interval))))
        self.playlist_interval.blockSignals(False)
        self.playlist_enabled.blockSignals(True)
        self.playlist_enabled.setChecked(bool(state.loop))
        self.playlist_enabled.blockSignals(False)
        self.playlist_list.clear()
        for layer_id in ids:
            layer = self.scene.layer(layer_id)
            if layer is not None:
                self.playlist_list.addItem(f"{layer.name}  ·  {layer.kind}")
        self.playlist_list.setCurrentRow(index if index >= 0 else -1)

    def _store_playlist_state(self) -> None:
        state = self.scene.playlist
        valid = {layer.id for layer in self.scene.layers}
        state.layer_ids = [layer_id for layer_id in self.playlist_ids if layer_id in valid]
        state.interval = float(self.playlist_interval.value())
        state.loop = bool(self.playlist_enabled.isChecked())
        state.index = int(self.playlist_index)

    def _playlist_settings_changed(self, _value=None) -> None:
        self._store_playlist_state()
        self.playlist_last = time.monotonic()
        self._mark_dirty()

    def _add_playlist_items(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose playlist media", "",
            "Media (*.png *.jpg *.jpeg *.webp *.mp4 *.mkv *.webm *.mov)")
        for path in paths:
            kind = "video" if Path(path).suffix.lower() in (".mp4", ".mkv", ".webm", ".mov") else "image"
            layer = Layer(new_id("layer"), kind, Path(path).stem, source=path,
                          box=(0.0, 0.0, 1.0, 1.0), z=2, visible=not self.playlist_ids)
            self.scene.add_layer(layer)
            self.playlist_ids.append(layer.id)
            self.playlist_list.addItem(f"{layer.name}  ·  {layer.kind}")
        if paths:
            self._store_playlist_state()
            self._clear_history()
            self._refresh_layers()
            self._mark_dirty()
            if self.engine is not None:
                self.restart_engine()

    def _clear_playlist(self) -> None:
        for layer_id in self.playlist_ids:
            self.scene.remove_layer(layer_id)
        self.playlist_ids = []
        self.playlist_index = -1
        self.playlist_list.clear()
        self._store_playlist_state()
        self._clear_history()
        self._refresh_layers()
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

    def _advance_playlist(self, now: float) -> None:
        if self.engine is None or not self.playlist_ids:
            return
        interval = self.playlist_interval.value()
        if now - self.playlist_last < interval:
            return
        self.playlist_last = now
        if self.playlist_index + 1 >= len(self.playlist_ids):
            if not self.playlist_enabled.isChecked():
                return
            next_index = 0
        else:
            next_index = self.playlist_index + 1
        self.playlist_index = next_index
        self.scene.playlist.index = next_index
        for position, layer_id in enumerate(self.playlist_ids):
            self.engine.set_layer_visible(layer_id, position == self.playlist_index)
        self.playlist_list.setCurrentRow(self.playlist_index)

    def _apply_playlist_visibility(self) -> None:
        """Show only the playlist's current layer (the rotation's state)."""
        if self.playlist_index >= 0 and self.playlist_ids:
            for position, layer_id in enumerate(self.playlist_ids):
                layer = self.scene.layer(layer_id)
                if layer is not None:
                    layer.visible = position == self.playlist_index
