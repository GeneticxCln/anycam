"""The Stream and Output tabs and canvas settings."""
from __future__ import annotations



from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QGroupBox, QLabel, QLineEdit, QListWidget,
    QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from .. import config, streaming
from ..engine.outputs.vcam import pipewire_available
from ..pipeline import nvenc_available, va_present
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class OutputPanelMixin:
    """The Stream and Output tabs and canvas settings."""

    def _stream_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        box = QGroupBox("Live stream")
        grid = QGridLayout(box)
        self.rtmp_edit = QLineEdit(self.project.output.rtmp_url)
        self.rtmp_edit.setPlaceholderText("rtmp://host/live/stream-key")
        self.rtmp_edit.textChanged.connect(lambda *_: self._mark_dirty())
        grid.addWidget(QLabel("RTMP URL"), 0, 0)
        grid.addWidget(self.rtmp_edit, 0, 1, 1, 2)
        self.stream_preset = QComboBox()
        for key, (label, _base) in streaming.PRESETS.items():
            self.stream_preset.addItem(label, key)
        self.stream_key_edit = QLineEdit()
        self.stream_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.stream_key_edit.setPlaceholderText("stream key")
        preset, key = streaming.split_url(self.project.output.rtmp_url)
        self.stream_preset.setCurrentIndex(max(0, self.stream_preset.findData(preset)))
        if preset != "custom":
            self.stream_key_edit.setText(key)
        self.stream_preset.currentIndexChanged.connect(self._stream_preset_changed)
        self.stream_key_edit.textEdited.connect(self._stream_preset_changed)
        grid.addWidget(QLabel("Platform"), 1, 0)
        grid.addWidget(self.stream_preset, 1, 1)
        grid.addWidget(self.stream_key_edit, 1, 2)
        self.stream_bitrate = QSpinBox()
        self.stream_bitrate.setRange(0, 200_000)
        self.stream_bitrate.setSingleStep(500)
        self.stream_bitrate.setSpecialValueText("Automatic")
        self.stream_bitrate.setSuffix(" kbit/s")
        self.stream_bitrate.setValue(self.project.output.stream_bitrate_kbps)
        self.stream_bitrate.setKeyboardTracking(False)
        self.stream_bitrate.valueChanged.connect(self._stream_bitrate_changed)
        grid.addWidget(QLabel("Video bitrate"), 2, 0)
        grid.addWidget(self.stream_bitrate, 2, 1, 1, 2)
        grid.addWidget(QLabel("Also publish to"), 3, 0)
        self.extra_list = QListWidget()
        self.extra_list.setMaximumHeight(70)
        self.extra_list.addItems(self.project.output.rtmp_extra_urls)
        grid.addWidget(self.extra_list, 3, 1, 1, 2)
        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("another rtmp:// or rtmps:// address")
        self.extra_edit.returnPressed.connect(self._add_extra_destination)
        add = QPushButton("Add")
        add.clicked.connect(self._add_extra_destination)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_extra_destination)
        grid.addWidget(self.extra_edit, 4, 1)
        grid.addWidget(add, 4, 2)
        grid.addWidget(remove, 5, 2)
        self.live_button = QPushButton("Go Live")
        self.live_button.setObjectName("primary")
        self.live_button.clicked.connect(self.toggle_live)
        grid.addWidget(self.live_button, 6, 0, 1, 3)
        self.stream_status = QLabel("")
        self.stream_status.setWordWrap(True)
        grid.addWidget(self.stream_status, 7, 0, 1, 3)
        live_note = QLabel("The saved URL is used only while Go Live is on; "
                           "starting preview never publishes. A dropped connection is "
                           "retried after 1, 2, 4 \u2026 30 seconds.")
        live_note.setObjectName("muted")
        live_note.setWordWrap(True)
        grid.addWidget(live_note, 8, 0, 1, 3)
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _recording_group(self) -> QGroupBox:
        box = QGroupBox("Recording")
        grid = QGridLayout(box)
        self.record_dir_edit = QLineEdit(self.project.output.record_dir)
        self.record_dir_edit.setPlaceholderText(str(config.recordings_dir("")))
        self.record_dir_edit.editingFinished.connect(self._recording_settings_changed)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._pick_record_dir)
        self.record_bitrate = QSpinBox()
        self.record_bitrate.setRange(0, 200_000)
        self.record_bitrate.setSingleStep(500)
        self.record_bitrate.setSpecialValueText("Automatic")
        self.record_bitrate.setSuffix(" kbit/s")
        self.record_bitrate.setValue(self.project.output.record_bitrate_kbps)
        self.record_bitrate.setKeyboardTracking(False)
        self.record_bitrate.valueChanged.connect(self._recording_settings_changed)
        self.record_min_free = QSpinBox()
        self.record_min_free.setRange(0, 1_000_000)
        self.record_min_free.setSingleStep(100)
        self.record_min_free.setSpecialValueText("Off")
        self.record_min_free.setSuffix(" MiB")
        self.record_min_free.setValue(self.project.output.record_min_free_mb)
        self.record_min_free.setKeyboardTracking(False)
        self.record_min_free.valueChanged.connect(self._recording_settings_changed)
        self.record_free_label = QLabel("")
        self.record_free_label.setObjectName("muted")
        grid.addWidget(QLabel("Save to"), 0, 0)
        grid.addWidget(self.record_dir_edit, 0, 1)
        grid.addWidget(browse, 0, 2)
        grid.addWidget(QLabel("Video bitrate"), 1, 0)
        grid.addWidget(self.record_bitrate, 1, 1, 1, 2)
        grid.addWidget(QLabel("Stop when free space is under"), 2, 0)
        grid.addWidget(self.record_min_free, 2, 1, 1, 2)
        grid.addWidget(self.record_free_label, 3, 0, 1, 3)
        self._update_record_free()
        return box

    def _pick_record_dir(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        path = QFileDialog.getExistingDirectory(self, "Recordings folder",
                                                self.record_dir_edit.text() or "")
        if path:
            self.record_dir_edit.setText(path)
            self._recording_settings_changed()

    def _recording_settings_changed(self, *_args) -> None:
        out = self.project.output
        out.record_dir = self.record_dir_edit.text().strip()
        out.record_bitrate_kbps = int(self.record_bitrate.value())
        out.record_min_free_mb = int(self.record_min_free.value())
        self._update_record_free()
        self._mark_dirty()

    def _update_record_free(self) -> None:
        folder = config.recordings_dir(self.project.output.record_dir)
        self.record_free_label.setText(
            f"{config.disk_free_mb(folder) / 1024:.1f} GiB free in {folder}")

    def _stream_preset_changed(self, *_args) -> None:
        preset = str(self.stream_preset.currentData() or "custom")
        if preset == "custom":
            return
        self.rtmp_edit.setText(streaming.preset_url(preset, self.stream_key_edit.text()))

    def _stream_bitrate_changed(self, value: int) -> None:
        self.project.output.stream_bitrate_kbps = int(value)
        self._mark_dirty()

    def add_extra_destination(self, url: str) -> str:
        """Add a destination; returns "" or why it was refused."""
        url = url.strip()
        problem = streaming.validate_stream_url(url)
        if not problem and (url == self.rtmp_edit.text().strip()
                            or url in self.project.output.rtmp_extra_urls):
            problem = "that address is already in the list"
        if problem:
            self._status(problem)
            return problem
        self.project.output.rtmp_extra_urls.append(url)
        self.extra_list.addItem(url)
        self._mark_dirty()
        if self.live and self.engine is not None:
            self.restart_engine()
        return ""

    def _add_extra_destination(self) -> None:
        if not self.add_extra_destination(self.extra_edit.text()):
            self.extra_edit.clear()

    def _remove_extra_destination(self) -> None:
        row = self.extra_list.currentRow()
        if row < 0 or row >= len(self.project.output.rtmp_extra_urls):
            return
        del self.project.output.rtmp_extra_urls[row]
        self.extra_list.takeItem(row)
        self._mark_dirty()
        if self.live and self.engine is not None:
            self.restart_engine()

    def update_stream_status(self) -> None:
        label = getattr(self, "stream_status", None)
        if label is None:
            return
        rows = self.engine.report.rtmp_targets if self.engine is not None else []
        icons = {"live": "\u25cf live", "connecting": "\u25cc connecting",
                 "waiting": "\u21bb reconnecting", "failed": "\u2715 failed", "off": "off"}
        lines = []
        for row in rows:
            text = f"{icons.get(row['state'], row['state'])}  {row['url']}"
            if row["state"] == "waiting" and row.get("retry_in_s"):
                text += f"  (retry in {row['retry_in_s']:.0f} s)"
            if row["error"] and row["state"] != "live":
                text += f"\n    {row['error'][:140]}"
            lines.append(text)
        label.setText("\n".join(lines))

    def _output_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        box = QGroupBox("Canvas")
        grid = QGridLayout(box)
        self.width_spin = QSpinBox(); self.width_spin.setRange(320, 7680); self.width_spin.setValue(self.project.output.canvas_w)
        self.height_spin = QSpinBox(); self.height_spin.setRange(240, 4320); self.height_spin.setValue(self.project.output.canvas_h)
        self.fps_spin = QSpinBox(); self.fps_spin.setRange(1, 120); self.fps_spin.setValue(self.project.output.fps)
        # H.264 needs even dimensions: snap instead of letting x264 fail later.
        for spin in (self.width_spin, self.height_spin):
            spin.setSingleStep(2)
            spin.valueChanged.connect(
                lambda value, field=spin: field.setValue(value - value % 2)
                if value % 2 else None)
        # A size or rate edit takes effect while the preview runs: after a short
        # pause (so typing "1280" does not rebuild at 1, 12, 128 ...) the canvas
        # is rebuilt with the new format; nothing happens when it is stopped.
        self._canvas_apply_timer = QTimer(self)
        self._canvas_apply_timer.setSingleShot(True)
        self._canvas_apply_timer.setInterval(800)
        self._canvas_apply_timer.timeout.connect(self._apply_canvas_edit)
        for spin in (self.width_spin, self.height_spin, self.fps_spin):
            spin.valueChanged.connect(lambda *_: self._mark_dirty())
            spin.valueChanged.connect(lambda *_: self._canvas_apply_timer.start())
        grid.addWidget(QLabel("Width"), 0, 0); grid.addWidget(self.width_spin, 0, 1)
        grid.addWidget(QLabel("Height"), 1, 0); grid.addWidget(self.height_spin, 1, 1)
        grid.addWidget(QLabel("FPS"), 2, 0); grid.addWidget(self.fps_spin, 2, 1)
        canvas_hint = QLabel("A new size or rate restarts the running preview; the virtual "
                             "camera keeps the mode it opened with.")
        canvas_hint.setObjectName("muted")
        canvas_hint.setWordWrap(True)
        grid.addWidget(canvas_hint, 3, 0, 1, 2)
        layout.addWidget(box)

        encoder_box = QGroupBox("Video encoder")
        encoder_grid = QGridLayout(encoder_box)
        self.encoder_combo = QComboBox()
        for label, value in (
            ("Auto (NVENC, then VA-API, then x264)", "auto"),
            ("NVIDIA NVENC H.264", "nvenc"),
            ("NVIDIA NVENC H.265 (HEVC)", "nvenc_h265"),
            ("NVIDIA NVENC AV1", "nvenc_av1"),
            ("Intel/AMD VA-API H.264", "vaapi"),
            ("Intel/AMD VA-API H.265 (HEVC)", "vaapi_h265"),
            ("Intel/AMD VA-API AV1", "vaapi_av1"),
            ("CPU x264", "cpu"),
        ):
            # VA-API choices only where the encoders are installed.
            if value.startswith("vaapi"):
                codec = {"vaapi": "h264", "vaapi_h265": "h265", "vaapi_av1": "av1"}[value]
                if not va_present(codec):
                    continue
            self.encoder_combo.addItem(label, value)
        saved_encoder = str(self.project.output.encoder or "auto").lower()
        chosen = self.encoder_combo.findData(saved_encoder)
        self.encoder_combo.setCurrentIndex(chosen if chosen >= 0 else 0)
        self.encoder_combo.currentIndexChanged.connect(self._encoder_changed)
        encoder_grid.addWidget(QLabel("Encoder"), 0, 0)
        encoder_grid.addWidget(self.encoder_combo, 0, 1)
        nv_ready = nvenc_available()
        encoder_note = QLabel(
            "NVENC detected. Recording can use H.264, H.265 or AV1 on the GPU "
            "through CUDA; missing codecs fall back to H.264 NVENC, then x264. "
            "RTMP/FLV always streams H.264." if nv_ready else
            "NVENC was not detected. Auto records with CPU x264; install the "
            "GStreamer nvcodec plugin and an NVIDIA driver to enable GPU encoding.")
        encoder_note.setObjectName("muted")
        encoder_note.setWordWrap(True)
        encoder_grid.addWidget(encoder_note, 1, 0, 1, 2)
        layout.addWidget(encoder_box)

        layout.addWidget(self._recording_group())

        effects_box = QGroupBox("AI effects")
        effects_grid = QGridLayout(effects_box)
        self.effects_device_combo = QComboBox()
        self.effects_device_combo.addItem("CPU (default)", "cpu")
        self.effects_device_combo.addItem("NVIDIA GPU (CUDA)", "gpu")
        chosen = self.effects_device_combo.findData(self.project.output.effects_device)
        self.effects_device_combo.setCurrentIndex(chosen if chosen >= 0 else 0)
        self.effects_device_combo.currentIndexChanged.connect(self._effects_device_changed)
        effects_grid.addWidget(QLabel("Run models on"), 0, 0)
        effects_grid.addWidget(self.effects_device_combo, 0, 1)
        self.effects_device_note = QLabel()
        self.effects_device_note.setObjectName("muted")
        self.effects_device_note.setWordWrap(True)
        effects_grid.addWidget(self.effects_device_note, 1, 0, 1, 2)
        self._update_effects_device_note()

        from .. import models as model_catalog

        rvm = model_catalog.CATALOG["rvm"]
        self.segmentation_combo = QComboBox()
        self.segmentation_combo.addItem("Standard (MediaPipe, built in)", "mediapipe")
        self.segmentation_combo.addItem("High quality (Robust Video Matting)", "rvm")
        chosen = self.segmentation_combo.findData(self.project.output.segmentation_model)
        self.segmentation_combo.setCurrentIndex(chosen if chosen >= 0 else 0)
        self.segmentation_combo.currentIndexChanged.connect(self._segmentation_changed)
        effects_grid.addWidget(QLabel("Person cut-out"), 2, 0)
        effects_grid.addWidget(self.segmentation_combo, 2, 1)
        self.model_download_button = QPushButton(
            f"Download ({rvm.size / 1e6:.0f} MB)")
        self.model_download_button.clicked.connect(self._download_rvm)
        effects_grid.addWidget(self.model_download_button, 2, 2)
        self.segmentation_note = QLabel()
        self.segmentation_note.setObjectName("muted")
        self.segmentation_note.setWordWrap(True)
        self.segmentation_note.setTextFormat(Qt.TextFormat.RichText)
        self.segmentation_note.setOpenExternalLinks(True)
        effects_grid.addWidget(self.segmentation_note, 3, 0, 1, 3)
        self._model_download: dict | None = None
        self._model_download_timer = QTimer(self)
        self._model_download_timer.setInterval(150)
        self._model_download_timer.timeout.connect(self._poll_model_download)
        self._update_segmentation_note()
        layout.addWidget(effects_box)

        screen_box = QGroupBox("Screen capture")
        screen_grid = QGridLayout(screen_box)
        self.screen_flip_combo = QComboBox()
        for label, value in (
            ("Automatic (follow stream orientation)", "automatic"),
            ("Rotate 180°", "rotate-180"),
            ("Flip vertically", "vertical-flip"),
            ("No correction", "none"),
        ):
            self.screen_flip_combo.addItem(label, value)
        index = self.screen_flip_combo.findData(self.project.output.screen_flip)
        self.screen_flip_combo.setCurrentIndex(index if index >= 0 else 0)
        self.screen_flip_combo.currentIndexChanged.connect(
            self._screen_flip_changed)
        self.screen_rotate_button = QPushButton("Rotate 180°")
        self.screen_rotate_button.setCheckable(True)
        self.screen_rotate_button.setToolTip(
            "Rotate the screen capture 180°; uncheck to remove the rotation")
        self.screen_rotate_button.setChecked(
            self.project.output.screen_flip == "rotate-180")
        self.screen_rotate_button.toggled.connect(self._screen_rotate_toggled)
        screen_grid.addWidget(QLabel("Rotation"), 0, 0)
        screen_grid.addWidget(self.screen_flip_combo, 0, 1)
        screen_grid.addWidget(self.screen_rotate_button, 0, 2)
        screen_note = QLabel(
            "Automatic follows the orientation tag reported by the "
            "compositor. Use Rotate 180° or Flip vertically only if this "
            "share is wrong.")
        screen_note.setObjectName("muted")
        screen_note.setWordWrap(True)
        screen_grid.addWidget(screen_note, 1, 0, 1, 3)
        layout.addWidget(screen_box)

        box2 = QGroupBox("Video output (virtual camera)")
        grid2 = QGridLayout(box2)
        self.vcam_edit = QLineEdit(self.project.output.vcam_device)
        self.vcam_edit.textChanged.connect(lambda *_: self._mark_dirty())
        grid2.addWidget(QLabel("Device"), 0, 0)
        grid2.addWidget(self.vcam_edit, 0, 1)
        self.vcam_backend_combo = QComboBox()
        for label, value in (
            ("Loopback device: every app (default)", "v4l2"),
            ("PipeWire camera: no driver, PipeWire-aware apps only", "pipewire"),
            ("Both", "both"),
        ):
            self.vcam_backend_combo.addItem(label, value)
        if not pipewire_available():
            for row in (1, 2):
                item = self.vcam_backend_combo.model().item(row)
                if item is not None:
                    item.setEnabled(False)
        backend_index = self.vcam_backend_combo.findData(self._vcam_backend())
        self.vcam_backend_combo.setCurrentIndex(max(0, backend_index))
        self.vcam_backend_combo.currentIndexChanged.connect(
            self._vcam_backend_changed)
        grid2.addWidget(QLabel("Publish as"), 1, 0)
        grid2.addWidget(self.vcam_backend_combo, 1, 1)
        self.vcam_note = QLabel()
        self.vcam_note.setObjectName("muted")
        self.vcam_note.setWordWrap(True)
        grid2.addWidget(self.vcam_note, 2, 0, 1, 2)
        self._update_vcam_note()
        layout.addWidget(box2)

        perf_box = QGroupBox("Canvas")
        perf_grid = QGridLayout(perf_box)
        self.canvas_combo = QComboBox()
        self.canvas_combo.addItem("Automatic (GPU when it works)", "auto")
        self.canvas_combo.addItem("CPU", "cpu")
        index = self.canvas_combo.findData(self.project.output.canvas_backend)
        self.canvas_combo.setCurrentIndex(max(0, index))
        self.canvas_combo.currentIndexChanged.connect(self._canvas_backend_changed)
        perf_grid.addWidget(QLabel("Mix layers on"), 0, 0)
        perf_grid.addWidget(self.canvas_combo, 0, 1)
        self.canvas_note = QLabel()
        self.canvas_note.setObjectName("muted")
        self.canvas_note.setWordWrap(True)
        perf_grid.addWidget(self.canvas_note, 1, 0, 1, 2)
        self._update_canvas_note()
        layout.addWidget(perf_box)

        box4 = QGroupBox("Shortcuts")
        grid4 = QGridLayout(box4)
        keys = [("Ctrl+P", "start / stop preview"), ("Ctrl+R", "record"),
                ("Ctrl+Shift+V", "virtual camera"), ("F5", "snapshot"),
                ("Ctrl+M", "mute microphone"), ("Ctrl+1…9", "switch scene"),
                ("← ↑ → ↓", "nudge selected layer (Shift = 10×)"),
                ("Ctrl+Alt+←↑→↓", "align selected layers to canvas edges"),
                ("Ctrl+Alt+H / V", "centre selected layers"),
                ("Ctrl+D", "duplicate selected layers"),
                ("Ctrl+Z", "undo layer edit"), ("Ctrl+Shift+Z", "redo layer edit")]
        for row, (key, what) in enumerate(keys):
            chip = QLabel(key)
            chip.setObjectName("accent")
            grid4.addWidget(chip, row, 0)
            grid4.addWidget(QLabel(what), row, 1)
        self.global_shortcuts_label = QLabel()
        self.global_shortcuts_label.setObjectName("muted")
        self.global_shortcuts_label.setWordWrap(True)
        grid4.addWidget(self.global_shortcuts_label, len(keys), 0, 1, 2)
        self._update_global_shortcuts_label()
        layout.addWidget(box4)

        save = QPushButton("Save project")
        save.clicked.connect(self.save_project)
        layout.addWidget(save)
        reload_button = QPushButton("Reload saved project")
        reload_button.clicked.connect(self.reload_project)
        layout.addWidget(reload_button)
        layout.addStretch(1)
        return page

    def _encoder_changed(self, _index: int = 0) -> None:
        value = str(self.encoder_combo.currentData() or "auto").lower()
        if value == self.project.output.encoder:
            return
        self.project.output.encoder = value
        self._mark_dirty()
        existing_tap = (self.engine is not None
                        and getattr(self.engine, "_recorder_valve", None) is not None)
        if self.recording:
            self._status("Encoder change applies after this recording and a preview restart")
        elif existing_tap:
            self._status("Encoder change saved · restart preview to rebuild the recording tap")
        elif self.engine is not None:
            self._status("Encoder change applies to the next recording")
        else:
            self._status(f"Recording encoder: {self.encoder_combo.currentText()}")

    def _screen_flip_changed(self, _index: int) -> None:
        value = str(self.screen_flip_combo.currentData() or "automatic")
        self.screen_rotate_button.blockSignals(True)
        self.screen_rotate_button.setChecked(value == "rotate-180")
        self.screen_rotate_button.blockSignals(False)
        if value == self.project.output.screen_flip:
            return
        self.project.output.screen_flip = value
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

    def _screen_rotate_toggled(self, checked: bool) -> None:
        value = "rotate-180" if checked else "none"
        index = self.screen_flip_combo.findData(value)
        if index >= 0:
            self.screen_flip_combo.setCurrentIndex(index)

    def _apply_canvas_edit(self) -> None:
        """Apply the Width/Height/FPS boxes to the project (and the running canvas)."""
        self.set_output_format(self.width_spin.value(), self.height_spin.value(),
                               self.fps_spin.value())

    def set_output_format(self, width: int, height: int, fps: int) -> None:
        """Apply a canvas size and rate, keeping the Output tab in sync."""
        width = self._even(width)
        height = self._even(height)
        fps = max(1, min(120, int(fps)))
        output = self.project.output
        if (output.canvas_w, output.canvas_h, output.fps) == (width, height, fps):
            return
        resolution_changed = (output.canvas_w, output.canvas_h) != (width, height)
        output.canvas_w, output.canvas_h, output.fps = width, height, fps
        self.preview.canvas_size = (width, height)
        for name, value in (("width_spin", width), ("height_spin", height),
                            ("fps_spin", fps)):
            spin = getattr(self, name, None)
            if spin is not None:
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
        self._sync_output_controls()
        self._mark_dirty()
        if resolution_changed and self.recording and self._recorder is not None:
            self._rotate_recording()
        if self.engine is not None:
            self.restart_engine()
        self._status(f"Output {width}×{height} · {fps} fps")

    def _sync_output_controls(self) -> None:
        """Hook for subclasses that show the canvas format elsewhere."""

    def _canvas_backend_changed(self) -> None:
        value = str(self.canvas_combo.currentData() or "auto")
        if value == self.project.output.canvas_backend:
            return
        self.project.output.canvas_backend = value
        self._gpu_canvas_failed = False
        self._mark_dirty()
        self._update_canvas_note()
        if self.engine is not None:
            self.restart_engine()

    def _update_canvas_note(self) -> None:
        note = getattr(self, "canvas_note", None)
        if note is None:
            return
        from ..pipeline import canvas_requested, gl_canvas_problem

        engine = getattr(self, "engine", None)
        built = getattr(engine, "canvas_backend", "") if engine is not None else ""
        if canvas_requested():
            text = f"ANYCAM_CANVAS={canvas_requested()} overrides this setting."
        elif self.project.output.canvas_backend == "cpu":
            text = "Layers are mixed on the CPU (the compositor element)."
        elif getattr(self, "_gpu_canvas_failed", False):
            text = "The GPU canvas failed to start this session; using the CPU."
        elif gl_canvas_problem():
            text = f"GPU canvas unavailable ({gl_canvas_problem()}); using the CPU."
        else:
            text = ("Layers are uploaded and mixed on the GPU; one download feeds "
                    "recording and the virtual camera.")
        if built:
            text += f" Running: {built.upper()} canvas."
        note.setText(text)

    def _canvas_choice(self) -> str:
        """"gpu" or "cpu" for the next engine (see OutputConfig.canvas_backend)."""
        from ..pipeline import canvas_requested

        forced = canvas_requested()
        if forced:
            return forced
        if getattr(self, "_gpu_canvas_failed", False):
            return "cpu"
        return "cpu" if self.project.output.canvas_backend == "cpu" else "gpu"
