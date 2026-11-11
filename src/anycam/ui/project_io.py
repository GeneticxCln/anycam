"""Loading, saving and autosaving the project."""
from __future__ import annotations


import shutil
from pathlib import Path


from .. import config
from ..model import (Project, default_project)
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class ProjectIOMixin:
    """Loading, saving and autosaving the project."""

    def _load_saved_project(self) -> Project:
        path = config.project_path()
        if not path.exists():
            return default_project()
        try:
            project = Project.load(path)
            if project.load_warnings:
                # Tolerant loading kept what was usable; say what was not.
                self._load_warning = (
                    f"Loaded {path.name} with repairs: "
                    + "; ".join(project.load_warnings[:3])
                    + (f" (+{len(project.load_warnings) - 3} more)"
                       if len(project.load_warnings) > 3 else ""))
            return project
        except Exception as exc:  # noqa: BLE001 - a corrupt save must not brick the app
            # Keep the unreadable file: the next autosave writes defaults over
            # the path, and the old project may still be recoverable by hand.
            try:
                shutil.copy2(path, path.with_name(f"{path.name}.corrupt"))
            except OSError:
                pass
            self._load_warning = f"Could not load {path}: {exc}. Using defaults."
            return default_project()

    def _write_project(self) -> Path:
        """Synchronise the widgets into the model and save the project."""
        self._store_playlist_state()
        self.project.output.canvas_w = self._even(self.width_spin.value())
        self.project.output.canvas_h = self._even(self.height_spin.value())
        self.project.output.fps = self.fps_spin.value()
        self.project.output.vcam_device = self.vcam_edit.text().strip()
        self.project.output.encoder = str(
            self.encoder_combo.currentData() or "auto").lower()
        self.project.output.screen_flip = str(
            self.screen_flip_combo.currentData() or "automatic")
        self.project.output.rtmp_url = self.rtmp_edit.text().strip()
        self.project.audio.mic_enabled = self.audio_on
        self.project.audio.media_file = self.media_edit.text().strip()
        self.project.editor.snap_enabled = self.preview.snap_enabled
        self.project.editor.snap_px = self.preview.snap_px
        return self.project.save(config.project_path())

    def save_project(self) -> None:
        try:
            path = self._write_project()
        except Exception as exc:  # noqa: BLE001 - a failed save must not crash the UI
            self._status(f"Could not save project: {exc}")
            return
        self._dirty = False
        self._autosave_timer.stop()
        self._status(f"Saved project to {path}")

    def _mark_dirty(self) -> None:
        if not self._persist or self.headless:
            return
        self._dirty = True
        self._autosave_timer.start()

    def _autosave(self) -> None:
        if not self._persist or self.headless or not self._dirty:
            return
        try:
            self._write_project()
        except Exception as exc:  # noqa: BLE001 - keep the edit in memory
            self._status(f"Autosave failed: {exc}")
            return
        self._dirty = False

    def _save_if_needed(self) -> None:
        self._autosave_timer.stop()
        if not self._persist or self.headless or not self._dirty:
            return
        try:
            self._write_project()
            self._dirty = False
        except Exception as exc:  # noqa: BLE001 - closing must still work
            self._status(f"Could not save project: {exc}")

    def reload_project(self) -> None:
        """Re-read the saved project and refresh the controls that mirror it."""
        path = config.project_path()
        if not path.exists():
            self._status(f"No saved project at {path}")
            return
        try:
            project = Project.load(path)
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not load {path}: {exc}")
            return
        was_running = self.engine is not None
        if was_running:
            self.stop_engine()
        self._close_screen_sessions()
        self.project = project
        self._dirty = False
        self._autosave_timer.stop()
        self._clear_history()
        self._refresh_scenes()
        self._refresh_scene_list()
        self._refresh_layers()
        self._load_playlist_state()
        for spin, value in ((self.width_spin, self.project.output.canvas_w),
                            (self.height_spin, self.project.output.canvas_h),
                            (self.fps_spin, self.project.output.fps)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        flip_index = self.screen_flip_combo.findData(
            self.project.output.screen_flip)
        self.screen_flip_combo.blockSignals(True)
        self.screen_flip_combo.setCurrentIndex(flip_index if flip_index >= 0 else 0)
        self.screen_flip_combo.blockSignals(False)
        self.screen_rotate_button.blockSignals(True)
        self.screen_rotate_button.setChecked(
            self.project.output.screen_flip == "rotate-180")
        self.screen_rotate_button.blockSignals(False)
        encoder_index = self.encoder_combo.findData(
            str(self.project.output.encoder or "auto").lower())
        self.encoder_combo.blockSignals(True)
        self.encoder_combo.setCurrentIndex(
            encoder_index if encoder_index >= 0 else 0)
        self.encoder_combo.blockSignals(False)
        canvas_index = self.canvas_combo.findData(self.project.output.canvas_backend)
        self.canvas_combo.blockSignals(True)
        self.canvas_combo.setCurrentIndex(max(0, canvas_index))
        self.canvas_combo.blockSignals(False)
        backend_index = self.vcam_backend_combo.findData(self._vcam_backend())
        self.vcam_backend_combo.blockSignals(True)
        self.vcam_backend_combo.setCurrentIndex(max(0, backend_index))
        self.vcam_backend_combo.blockSignals(False)
        self._update_vcam_note()
        for edit, value in ((self.vcam_edit, self.project.output.vcam_device),
                            (self.rtmp_edit, self.project.output.rtmp_url),
                            (self.media_edit, self.project.audio.media_file)):
            edit.blockSignals(True)
            edit.setText(value)
            edit.blockSignals(False)
        # The audio widgets mirror the model too; they used to keep the values
        # from the project that was open before the reload.
        self.audio_on = bool(self.project.audio.mic_enabled)
        self.audio_check.blockSignals(True)
        self.audio_check.setChecked(self.audio_on)
        self.audio_check.blockSignals(False)
        self.mic_gain_slider.blockSignals(True)
        self.mic_gain_slider.setValue(int(self.project.audio.mic_gain_db))
        self.mic_gain_slider.blockSignals(False)
        self.mic_gain_label.setText(f"{self.project.audio.mic_gain_db:+.0f} dB")
        self.mute_check.blockSignals(True)
        self.mute_check.setChecked(self.project.audio.mic_muted)
        self.mute_check.blockSignals(False)
        self.media_gain_slider.blockSignals(True)
        self.media_gain_slider.setValue(int(self.project.audio.media_gain_db))
        self.media_gain_slider.blockSignals(False)
        self.media_gain_label.setText(f"{self.project.audio.media_gain_db:+.0f} dB")
        for combo, value in ((self.denoise_combo, self.project.audio.mic_denoise),
                             (self.effects_device_combo, self.project.output.effects_device),
                             (self.segmentation_combo, self.project.output.segmentation_model)):
            index = combo.findData(value)
            combo.blockSignals(True)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)
        self.auto_gain_check.blockSignals(True)
        self.auto_gain_check.setChecked(bool(self.project.audio.mic_auto_gain))
        self.auto_gain_check.blockSignals(False)
        from .. import inference, segment

        inference.set_preference(self.project.output.effects_device)
        segment.set_model(self.project.output.segmentation_model)
        self._update_effects_device_note()
        self._update_segmentation_note()
        # State that belonged to the project that was open before.
        self._backdrop_insets.clear()
        self._lost_cameras.clear()
        self.preview.canvas_size = (self.project.output.canvas_w,
                                    self.project.output.canvas_h)
        self._sync_output_controls()
        self._sync_editor_controls()
        repaired = (f" · repaired: {'; '.join(project.load_warnings[:2])}"
                    if project.load_warnings else "")
        self._status(f"Loaded project from {path}{repaired}")
        if was_running:
            self.start_engine()
