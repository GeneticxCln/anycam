"""The Stream and Output tabs and canvas settings."""
from __future__ import annotations



from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QGroupBox, QLabel, QLineEdit,
    QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

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
        grid.addWidget(self.rtmp_edit, 0, 1)
        self.live_button = QPushButton("Go Live")
        self.live_button.setObjectName("primary")
        self.live_button.clicked.connect(self.toggle_live)
        grid.addWidget(self.live_button, 1, 0, 1, 2)
        live_note = QLabel("The saved URL is used only while Go Live is on; "
                           "starting preview never publishes.")
        live_note.setObjectName("muted")
        live_note.setWordWrap(True)
        grid.addWidget(live_note, 2, 0, 1, 2)
        layout.addWidget(box)
        layout.addStretch(1)
        return page

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
        for spin in (self.width_spin, self.height_spin, self.fps_spin):
            spin.valueChanged.connect(lambda *_: self._mark_dirty())
        grid.addWidget(QLabel("Width"), 0, 0); grid.addWidget(self.width_spin, 0, 1)
        grid.addWidget(QLabel("Height"), 1, 0); grid.addWidget(self.height_spin, 1, 1)
        grid.addWidget(QLabel("FPS"), 2, 0); grid.addWidget(self.fps_spin, 2, 1)
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
