"""The playlist tab and its rotation."""
from __future__ import annotations


import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from .. import config, playlist
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
        self.playlist_list.currentRowChanged.connect(self._playlist_selection_changed)
        row = QHBoxLayout()
        add = QPushButton("+ Add media")
        add.clicked.connect(self._add_playlist_items)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_playlist)
        pdf = QPushButton("+ PDF slides")
        pdf.setToolTip("Every page of a PDF becomes a slide, stepped by hand")
        pdf.clicked.connect(self._pick_pdf)
        row.addWidget(add)
        row.addWidget(pdf)
        row.addWidget(clear)
        layout.addLayout(row)
        item_row = QHBoxLayout()
        self.playlist_up = QPushButton("Up")
        self.playlist_up.clicked.connect(lambda: self.move_playlist_item(-1))
        self.playlist_down = QPushButton("Down")
        self.playlist_down.clicked.connect(lambda: self.move_playlist_item(1))
        self.playlist_remove = QPushButton("Remove item")
        self.playlist_remove.clicked.connect(lambda: self.remove_playlist_item())
        for button in (self.playlist_up, self.playlist_down, self.playlist_remove):
            item_row.addWidget(button)
        layout.addLayout(item_row)
        transport = QHBoxLayout()
        self.playlist_prev = QPushButton("Previous")
        self.playlist_prev.clicked.connect(lambda: self.playlist_step(-1))
        self.playlist_pause = QPushButton("Pause")
        self.playlist_pause.setCheckable(True)
        self.playlist_pause.toggled.connect(self.set_playlist_paused)
        self.playlist_next = QPushButton("Next")
        self.playlist_next.clicked.connect(lambda: self.playlist_step(1))
        for button in (self.playlist_prev, self.playlist_pause, self.playlist_next):
            transport.addWidget(button)
        layout.addLayout(transport)
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
        self.playlist_shuffle = QCheckBox("Shuffle")
        self.playlist_shuffle.toggled.connect(self._playlist_settings_changed)
        grid.addWidget(self.playlist_shuffle, 2, 0, 1, 2)
        self.playlist_item_seconds = QDoubleSpinBox()
        self.playlist_item_seconds.setRange(0.0, playlist.MAX_SECONDS)
        self.playlist_item_seconds.setSpecialValueText("Playlist interval")
        self.playlist_item_seconds.setSuffix(" s")
        self.playlist_item_seconds.setEnabled(False)
        self.playlist_item_seconds.valueChanged.connect(self._playlist_item_seconds_changed)
        grid.addWidget(QLabel("Selected item stays"), 3, 0)
        grid.addWidget(self.playlist_item_seconds, 3, 1)
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
        self.media_loop_check = QCheckBox("Repeat")
        self.media_loop_check.setChecked(self.project.audio.media_loop)
        self.media_loop_check.toggled.connect(self._music_options_changed)
        self.media_shuffle_check = QCheckBox("Shuffle")
        self.media_shuffle_check.setChecked(self.project.audio.media_shuffle)
        self.media_shuffle_check.toggled.connect(self._music_options_changed)
        music_grid.addWidget(self.media_loop_check, 2, 0)
        music_grid.addWidget(self.media_shuffle_check, 2, 1)
        self.music_list = QListWidget()
        self.music_list.setMaximumHeight(90)
        self.music_list.addItems(self.project.audio.media_playlist)
        music_grid.addWidget(QLabel("Then play"), 3, 0)
        music_grid.addWidget(self.music_list, 3, 1, 1, 2)
        music_buttons = QHBoxLayout()
        add_music = QPushButton("Add tracks")
        add_music.clicked.connect(self._add_music_tracks)
        remove_music = QPushButton("Remove")
        remove_music.clicked.connect(self._remove_music_track)
        music_buttons.addWidget(add_music)
        music_buttons.addWidget(remove_music)
        music_buttons.addStretch(1)
        music_grid.addLayout(music_buttons, 4, 1, 1, 2)
        music_note = QLabel("Music is mixed with the microphone through an "
                            "audiomixer at 48 kHz stereo.")
        music_note.setObjectName("muted")
        music_note.setWordWrap(True)
        music_grid.addWidget(music_note, 5, 0, 1, 3)
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
        self.playlist_shuffle.blockSignals(True)
        self.playlist_shuffle.setChecked(bool(state.shuffle))
        self.playlist_shuffle.blockSignals(False)
        self.playlist_pause.blockSignals(True)
        self.playlist_pause.setChecked(bool(state.paused))
        self.playlist_pause.blockSignals(False)
        state.durations = {key: value for key, value in state.durations.items() if key in valid}
        self.playlist_list.clear()
        for layer_id in ids:
            layer = self.scene.layer(layer_id)
            if layer is not None:
                self.playlist_list.addItem(self._playlist_label(layer))
        self.playlist_list.setCurrentRow(index if index >= 0 else -1)

    def _store_playlist_state(self) -> None:
        state = self.scene.playlist
        valid = {layer.id for layer in self.scene.layers}
        state.layer_ids = [layer_id for layer_id in self.playlist_ids if layer_id in valid]
        state.interval = float(self.playlist_interval.value())
        state.loop = bool(self.playlist_enabled.isChecked())
        state.shuffle = bool(self.playlist_shuffle.isChecked())
        state.paused = bool(self.playlist_pause.isChecked())
        state.index = int(self.playlist_index)

    def _playlist_settings_changed(self, _value=None) -> None:
        self._store_playlist_state()
        self.playlist_last = time.monotonic()
        self._mark_dirty()

    def add_pdf_slides(self, path: str) -> int:
        """Put each page of a PDF in the playlist, paused so the pages are stepped by hand."""
        from .. import content

        pages, error = self._run_with_ui(lambda: content.pdf_pages(path))
        if error:
            self._status(error)
            return 0
        for page in pages:
            layer = Layer(new_id("layer"), "image", f"{Path(path).stem} p{pages.index(page) + 1}",
                          source=str(page), box=(0.0, 0.0, 1.0, 1.0), z=2,
                          visible=not self.playlist_ids)
            self.scene.add_layer(layer)
            self.playlist_ids.append(layer.id)
            self.playlist_list.addItem(f"{layer.name}  \u00b7  slide")
        self.playlist_pause.setChecked(True)
        self._store_playlist_state()
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        self._status(f"{len(pages)} slides added; step them with the playlist next/previous keys")
        return len(pages)

    def _pick_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a PDF", "", "PDF (*.pdf)")
        if path:
            self.add_pdf_slides(path)

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

    def _playlist_label(self, layer) -> str:
        own = self.scene.playlist.durations.get(layer.id)
        return f"{layer.name}  ·  {layer.kind}" + (f"  ·  {own:g} s" if own else "")

    def _playlist_selection_changed(self, row: int) -> None:
        ids = self.playlist_ids
        valid = 0 <= row < len(ids)
        self.playlist_item_seconds.setEnabled(valid)
        self.playlist_item_seconds.blockSignals(True)
        self.playlist_item_seconds.setValue(
            self.scene.playlist.durations.get(ids[row], 0.0) if valid else 0.0)
        self.playlist_item_seconds.blockSignals(False)

    def _playlist_item_seconds_changed(self, seconds: float) -> None:
        row = self.playlist_list.currentRow()
        if not 0 <= row < len(self.playlist_ids):
            return
        layer_id = self.playlist_ids[row]
        if seconds > 0:
            self.scene.playlist.durations[layer_id] = float(seconds)
        else:
            self.scene.playlist.durations.pop(layer_id, None)
        layer = self.scene.layer(layer_id)
        if layer is not None and self.playlist_list.item(row) is not None:
            self.playlist_list.item(row).setText(self._playlist_label(layer))
        self._mark_dirty()

    def set_playlist_paused(self, paused: bool) -> None:
        self.scene.playlist.paused = bool(paused)
        self.playlist_pause.setText("Resume" if paused else "Pause")
        self.playlist_last = time.monotonic()      # the timer starts again from now
        self._mark_dirty()

    def _show_playlist_item(self, index: int) -> None:
        self.playlist_index = index
        self.scene.playlist.index = index
        self.playlist_last = time.monotonic()
        for position, layer_id in enumerate(self.playlist_ids):
            if self.engine is not None:
                self.engine.set_layer_visible(layer_id, position == index)
            else:
                layer = self.scene.layer(layer_id)
                if layer is not None:
                    layer.visible = position == index
        self.playlist_list.setCurrentRow(index)

    def playlist_step(self, direction: int) -> bool:
        """Jump to the next (+1) or previous (-1) item now."""
        if not self.playlist_ids:
            return False
        if direction >= 0:
            target = playlist.next_index(
                self.playlist_ids, self.playlist_index, loop=True,
                shuffle=self.playlist_shuffle.isChecked())
        else:
            target = playlist.previous_index(self.playlist_ids, self.playlist_index, loop=True)
        if target is None:
            return False
        self._show_playlist_item(target)
        return True

    def move_playlist_item(self, delta: int) -> bool:
        row = self.playlist_list.currentRow()
        target = row + int(delta)
        if not (0 <= row < len(self.playlist_ids) and 0 <= target < len(self.playlist_ids)):
            return False
        ids = self.playlist_ids
        ids[row], ids[target] = ids[target], ids[row]
        if self.playlist_index == row:
            self.playlist_index = target
        elif self.playlist_index == target:
            self.playlist_index = row
        self._store_playlist_state()
        self._load_playlist_state()
        self.playlist_list.setCurrentRow(target)
        self._mark_dirty()
        return True

    def remove_playlist_item(self, row: int | None = None) -> bool:
        """Take one item out of the rotation and out of the scene."""
        row = self.playlist_list.currentRow() if row is None else row
        if not 0 <= row < len(self.playlist_ids):
            return False
        layer_id = self.playlist_ids[row]
        self.remove_layers([layer_id])      # also drops it from playlist_ids
        if layer_id in self.playlist_ids:
            self.playlist_ids.remove(layer_id)
        self.scene.playlist.durations.pop(layer_id, None)
        self._store_playlist_state()
        self._load_playlist_state()
        self._mark_dirty()
        return True

    def _music_options_changed(self, *_args) -> None:
        self.project.audio.media_loop = self.media_loop_check.isChecked()
        self.project.audio.media_shuffle = self.media_shuffle_check.isChecked()
        self._mark_dirty()

    def _add_music_tracks(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose music", "", "Audio (*.wav *.mp3 *.ogg *.flac *.m4a *.aac)")
        self.add_music_tracks(paths)

    def add_music_tracks(self, paths) -> int:
        added = [path for path in paths if path]
        self.project.audio.media_playlist.extend(added)
        self.music_list.addItems(added)
        if added:
            self._mark_dirty()
            if self.engine is not None:
                self.restart_engine()
        return len(added)

    def _remove_music_track(self) -> None:
        row = self.music_list.currentRow()
        if row < 0 or row >= len(self.project.audio.media_playlist):
            return
        del self.project.audio.media_playlist[row]
        self.music_list.takeItem(row)
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

    def _advance_playlist(self, now: float) -> None:
        if self.engine is None or not self.playlist_ids or self.playlist_pause.isChecked():
            return
        state = self.scene.playlist
        current = (self.playlist_ids[self.playlist_index]
                   if 0 <= self.playlist_index < len(self.playlist_ids) else "")
        interval = playlist.item_seconds(state, current) if current else self.playlist_interval.value()
        if now - self.playlist_last < interval:
            return
        self.playlist_last = now
        target = playlist.next_index(
            self.playlist_ids, self.playlist_index,
            loop=self.playlist_enabled.isChecked(), shuffle=self.playlist_shuffle.isChecked())
        if target is None:
            return
        self._show_playlist_item(target)
        self.playlist_last = now          # the caller's clock is the one compared next tick

    def _apply_playlist_visibility(self) -> None:
        """Show only the playlist's current layer (the rotation's state)."""
        if self.playlist_index >= 0 and self.playlist_ids:
            for position, layer_id in enumerate(self.playlist_ids):
                layer = self.scene.layer(layer_id)
                if layer is not None:
                    layer.visible = position == self.playlist_index
