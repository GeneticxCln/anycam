"""The main window: construction, the tab bar and the preview context menu."""
from __future__ import annotations


import threading

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog,
    QFrame,
    QHBoxLayout, QLabel, QMainWindow, QMenu, QPushButton, QSplitter, QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)

from .. import config, portal
from ..layouts import LAYOUT_PRESETS
from ..model import (Project)
from ..engine.outputs.recorder import Recorder
from ..engine.outputs.vcam import VcamOutput
from ..pipeline import AnyCamEngine
from ..phone_dialog import PhoneEvents, PhonePairDialog
from ..pool import SourcePool
from .. import frames as frames_library
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401
from .cameras import _CAMERA_LOSS  # noqa: F401
from .effects_panel import EffectsPanelMixin  # noqa: E402
from .general_panel import GeneralPanelMixin  # noqa: E402
from .draw_panel import DrawMixin  # noqa: E402
from .backdrop_panel import BackdropMixin  # noqa: E402
from .audio_panel import AudioPanelMixin  # noqa: E402
from .playlist_panel import PlaylistMixin  # noqa: E402
from .output_panel import OutputPanelMixin  # noqa: E402
from .scenes import SceneMixin  # noqa: E402
from .layers_panel import LayersMixin  # noqa: E402
from .undo import UndoMixin  # noqa: E402
from .cameras import CameraMixin  # noqa: E402
from .screens import ScreenMixin  # noqa: E402
from .outputs import OutputsMixin  # noqa: E402
from .project_io import ProjectIOMixin  # noqa: E402
from .engine_control import EngineMixin  # noqa: E402
from .desktop import DesktopMixin  # noqa: E402
from .phone import PhoneMixin  # noqa: E402


class AnyCamWindow(GeneralPanelMixin, EffectsPanelMixin, DrawMixin, BackdropMixin, AudioPanelMixin, PlaylistMixin, OutputPanelMixin, SceneMixin, LayersMixin, UndoMixin, CameraMixin, ScreenMixin, OutputsMixin, ProjectIOMixin, EngineMixin, DesktopMixin, PhoneMixin,
                  QMainWindow):
    """The standalone studio window."""

    frame_ready = Signal()

    def __init__(self, project: Project | None = None):
        super().__init__()
        self._load_warning = ""
        # Windows created with an explicit project are demo/screenshot/test
        # sessions and must not overwrite the user's saved project.
        self._persist = project is None
        self._dirty = False
        self.project = project or self._load_saved_project()
        # Prefer the serial-based by-id path.  A replugged USB camera often
        # returns as a different /dev/videoN; the by-id symlink follows it.
        for scene in self.project.scenes:
            for layer in scene.layers:
                if layer.kind == "camera" and layer.source:
                    layer.source = config.camera_identity(layer.source)
        self.engine: AnyCamEngine | None = None
        # Captures are ref-counted, so one pool lives for the window's
        # lifetime and engines acquire/release their sources against it.
        self._source_pool = SourcePool()
        # Screen-capture retention/reuse events belong in Diagnostics, next to
        # the virtual camera events they explain (a rebuild that holds the
        # capture also holds the vcam's last frame).
        self._source_pool.log_event = self.log_event
        from .. import inference

        inference.set_preference(self.project.output.effects_device)
        from .. import segment

        segment.set_model(self.project.output.segmentation_model)
        # The phone link (QR pairing + WebRTC from the phone) starts on first
        # use and runs until the window closes, so a paired phone can
        # reconnect at any time.  Its events arrive on network threads.
        self._phone_service = None
        self._phone_dialog: PhonePairDialog | None = None
        self._phone_events = PhoneEvents(self)
        self._phone_events.event.connect(self._on_phone_event)
        # Desktop integration (tray icon, portal global shortcuts); started by
        # run_gui for the real window, never for tests or screenshots.
        self._tray = None
        self._global_shortcuts = None
        self.recording = False
        self._recorder: Recorder | None = None
        self._recording_path = ""
        self._vcam_output: VcamOutput | None = None
        # Set when a GPU canvas graph failed to start; the session stays on
        # the CPU compositor until the setting is changed.
        self._gpu_canvas_failed = False
        self.vcam_on = False
        # Last vcam error already reported, and the throttle for repairing a
        # dead v4l2sink without rebuilding the canvas on every tick.
        self._vcam_error_shown = ""
        self._vcam_retry_at = 0.0
        # Canvas size the held output was last fed at; the output itself keeps
        # a fixed camera mode so it never reopens mid-stream.
        self._vcam_canvas_size: tuple[int, int] | None = None
        # udev caches V4L2 capabilities from device-add time, so a correct
        # exclusive_caps=1 module can still be invisible to Chromium clients.
        self._vcam_udev_thread: threading.Thread | None = None
        self._vcam_udev_result: dict[str, str] = {}
        self._vcam_udev_attempted: set[str] = set()
        self._vcam_udev_timer = QTimer(self)
        self._vcam_udev_timer.setInterval(200)
        self._vcam_udev_timer.timeout.connect(self._vcam_udev_poll)

        # Streaming is a separate action: preview must never publish a saved URL.
        self.live = False
        self.audio_on = bool(getattr(self.project.audio, "mic_enabled", False))
        self._screen_error_shown = ""
        # Previous (frames, time) for the captured-frames readout.
        self._screen_fps_prev: tuple[float, float] | None = None
        # Layer currently built without its crop because the editor is open.
        self._crop_preview_layer_id = ""
        self._screen_sessions: dict[str, portal.ScreenSession] = {}
        # One portal authorization per app launch.  Removing and re-adding a
        # screen layer must reuse it instead of showing the window picker.
        self._last_screen_session: portal.ScreenSession | None = None
        # Layer id -> (box before, box after) for each capture layer inset to
        # reveal a backdrop, so clearing the backdrop can put them back.
        self._backdrop_insets: dict[str, tuple[tuple, tuple]] = {}
        self._camera_choices: list[tuple[str, str]] = []
        # A camera unplugged mid-stream is dropped from the graph instead of
        # killing the whole scene; this timer rebuilds once the node returns.
        self._lost_cameras: dict[str, str] = {}
        self._camera_retry_ms = 2000
        self._quiet_engine_errors = False
        self._camera_watch = QTimer(self)
        self._camera_watch.setInterval(self._camera_retry_ms)
        self._camera_watch.timeout.connect(self._reconnect_cameras)
        self.playlist_ids = list(self.scene.playlist.layer_ids)
        self.playlist_index = self.scene.playlist.index
        self.playlist_last = 0.0
        self.recording_segments: list[str] = []
        self._segment_index = 0
        self._session_stamp = ""
        self._transform_pending = ""
        self.history = LayoutHistory()
        self._layout_before_edit: list[dict] | None = None
        self._opacity_before: list[dict] | None = None
        self._syncing_selection = False
        self._transform_timer = QTimer(self)
        self._transform_timer.setSingleShot(True)
        self._transform_timer.setInterval(350)
        self._transform_timer.timeout.connect(self._commit_transform)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(800)
        self._autosave_timer.timeout.connect(self._autosave)
        self.headless = False
        self.screenshot_ok = False
        self.setWindowTitle("AnyCam")
        self.resize(1440, 900)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self.preview.stroke_finished.connect(self._draw_stroke_finished)
        self._install_shortcuts()
        self._pending_frame = None
        self.frame_ready.connect(self._update_preview)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._started_at = 0.0
        self._render_seconds = 0.0
        self.apply_interface_polish()

    # ------------------------------------------------------------------- ui
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 10)
        outer.setSpacing(10)

        header = QHBoxLayout()
        logo = QLabel()
        logo_path = config.assets_dir() / "anycam-logo.png"
        if logo_path.exists():
            logo.setPixmap(QPixmap(str(logo_path)).scaled(
                34, 34, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        header.addWidget(logo)
        title = QLabel("AnyCam")
        title.setObjectName("title")
        header.addWidget(title)
        subtitle = QLabel("Virtual camera studio")
        subtitle.setObjectName("muted")
        header.addWidget(subtitle)
        header.addStretch(1)
        self.scene_combo = QComboBox()
        self._refresh_scenes()
        self.scene_combo.currentIndexChanged.connect(self._scene_changed)
        header.addWidget(QLabel("Scene"))
        header.addWidget(self.scene_combo)
        self.status_chip = QLabel("STOPPED")
        self.status_chip.setObjectName("accent")
        header.addWidget(self.status_chip)
        outer.addLayout(header)

        # Shown when an application opened the loopback node before AnyCam
        # was feeding it: that open fails and only the application can retry.
        self.vcam_hint = QLabel("")
        self.vcam_hint.setObjectName("hint")
        self.vcam_hint.setWordWrap(True)
        self.vcam_hint.hide()
        self.vcam_dismiss = QPushButton("Dismiss")
        self.vcam_dismiss.setObjectName("muted")
        self.vcam_dismiss.hide()
        self.vcam_dismiss.clicked.connect(self.dismiss_vcam_hint)
        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_row.addWidget(self.vcam_hint, 1)
        hint_row.addWidget(self.vcam_dismiss, 0)
        outer.addLayout(hint_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)

        preview_panel = QFrame()
        preview_panel.setObjectName("panel")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(10, 10, 10, 10)
        self.preview = PreviewWidget()
        self.preview.set_snap_enabled(self.project.editor.snap_enabled)
        self.preview.set_snap_threshold(self.project.editor.snap_px)
        self.preview.set_scene(self.scene)
        self.preview.selection_changed.connect(self._preview_selection)
        self.preview.geometry_changed.connect(self._preview_geometry)
        self.preview.edit_started.connect(self._preview_edit_started)
        self.preview.edit_finished.connect(self._preview_edit_finished)
        self.preview.context_menu_requested.connect(self._preview_context_menu)
        self._setup_preview_output()
        preview_layout.addWidget(self.preview, 1)
        self.draw_bar = self._build_draw_bar()
        preview_layout.addWidget(self.draw_bar)
        controls = QHBoxLayout()
        self.start_button = QPushButton("Start preview")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.toggle_engine)
        self.record_button = QPushButton("Record")
        self.record_button.clicked.connect(self.toggle_record)
        self.vcam_button = QPushButton("Virtual camera")
        self.vcam_button.clicked.connect(self.toggle_vcam)
        self.snapshot_button = QPushButton("Snapshot")
        self.snapshot_button.clicked.connect(self.snapshot)
        self.multiview_button = QPushButton("Multi-view")
        self.multiview_button.clicked.connect(self.toggle_multiview)
        self.draw_button = QPushButton("Draw")
        self.draw_button.setCheckable(True)
        self.draw_button.setToolTip("Draw on the picture: pen, marker, arrows and boxes")
        self.draw_button.clicked.connect(self.toggle_draw)
        for button in (self.start_button, self.record_button, self.vcam_button,
                       self.snapshot_button, self.multiview_button, self.draw_button):
            controls.addWidget(button)
        controls.addSpacing(10)
        controls.addWidget(QLabel("Layout"))
        self.layout_combo = QComboBox()
        for preset in LAYOUT_PRESETS:
            self.layout_combo.addItem(preset.label, preset.key)
        controls.addWidget(self.layout_combo)
        apply_layout = QPushButton("Apply layout")
        apply_layout.clicked.connect(lambda: self.apply_layout())
        controls.addWidget(apply_layout)
        controls.addStretch(1)
        self.stats_label = QLabel("idle")
        self.stats_label.setObjectName("muted")
        controls.addWidget(self.stats_label)
        preview_layout.addLayout(controls)
        splitter.addWidget(preview_panel)

        side = QTabWidget()
        side.setMinimumWidth(370)
        side.setMaximumWidth(430)
        side.addTab(self._scene_tab(), "Scene")
        side.addTab(self._layers_tab(), "Layers")
        side.addTab(self._effects_tab(), "Effects")
        side.addTab(self._backdrop_tab(), "Stage")
        side.addTab(self._audio_tab(), "Audio")
        side.addTab(self._playlist_tab(), "Playlist")
        side.addTab(self._output_tab(), "Output")
        side.addTab(self._stream_tab(), "Stream")
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 1)

        self.setStatusBar(QStatusBar())
        if self._load_warning:
            self._status(self._load_warning)
        else:
            self._status("Ready. No virtual camera output yet.")

    # --------------------------------------------------------------- context menu
    def _preview_menu(self, layer_id: str) -> QMenu:
        """Shared canvas menu: source actions first, program outputs second."""
        menu = QMenu(self)
        layer = self.scene.layer(layer_id) if layer_id else None
        if layer is not None:
            title = menu.addAction(f"{layer.name}  ·  {layer.kind}")
            title.setEnabled(False)
            menu.addSeparator()

        inputs = menu.addMenu("Input")
        for label, kind in (
            ("Add camera…", "camera"),
            ("Add video file…", "video"),
            ("Add image…", "image"),
            ("Add native desktop share…", "screen"),
            ("Add a different desktop share…", "screen-new"),
            ("Add phone camera (QR code)…", "phone"),
            ("Add network stream URL…", "network"),
            ("Add text…", "text"),
            ("Add scrolling ticker…", "ticker"),
            ("Add lower third…", "lower_third"),
            ("Add clock", "clock"),
            ("Add timer…", "timer"),
            ("Add animated GIF…", "gif"),
            ("Add test pattern", "test"),
        ):
            if kind == "screen-new" and self._last_screen_session is None:
                continue
            action = inputs.addAction(label)
            action.setData(f"add:{kind}")

        outputs = menu.addMenu("Output")
        for label, attr, data in (
            ("Virtual camera", "vcam_on", "vcam"),
            ("Record program", "recording", "record"),
            ("Go Live", "live", "live"),
        ):
            action = outputs.addAction(label)
            action.setCheckable(True)
            action.setChecked(bool(getattr(self, attr, False)))
            action.setData(data)
        outputs.addSeparator()
        snapshot = outputs.addAction("Snapshot")
        snapshot.setData("snapshot")

        if layer is not None:
            menu.addSeparator()
            if layer.kind in ("ticker", "lower_third", "clock", "timer"):
                edit = menu.addAction(f"Edit {layer.name}…")
                edit.setData("edit")
            rename = menu.addAction("Rename…")
            rename.setData("rename")
            lock = menu.addAction("Unlock layer" if layer.locked else "Lock layer")
            lock.setData("lock")
            if layer.kind == "camera":
                controls = menu.addAction("Camera controls…")
                controls.setData("camera-controls")
            if layer.kind in ("screen", "video", "network", "camera"):
                region = menu.addAction("Show only a region…")
                region.setData("region")
            duplicate = menu.addAction("Duplicate layer")
            duplicate.setData("duplicate")
            remove = menu.addAction(f"Remove {layer.name}")
            remove.setData("remove")
            remove.setEnabled(not layer.locked)
        return menu

    def _preview_context_menu(self, layer_id: str, position) -> None:
        menu = self._preview_menu(layer_id)
        chosen = menu.exec(position)
        if chosen is None:
            return
        data = str(chosen.data() or "")
        if data.startswith("add:"):
            self.add_layer(data.split(":", 1)[1])
        elif data == "vcam":
            self.toggle_vcam()
        elif data == "record":
            self.toggle_record()
        elif data == "live":
            self.toggle_live()
        elif data == "snapshot":
            self.snapshot()
        elif data == "edit":
            self.edit_overlay(layer_id)
        elif data == "rename":
            self.rename_layer(layer_id)
        elif data == "lock":
            layer = self.scene.layer(layer_id)
            if layer is not None:
                self.set_layer_locked(layer_id, not layer.locked)
        elif data == "camera-controls":
            self.open_camera_controls(layer_id)
        elif data == "region":
            self.select_screen_region(layer_id)
        elif data == "duplicate":
            if layer_id:
                self.preview.select_layer(layer_id)
            self.duplicate_layer()
        elif data == "remove":
            self.remove_layers([layer_id])

    # Browse: the thumbnail library.  Adjust: every individual control.
    FRAME_GROUPS = {f"{group} frames": group for group in frames_library.GROUPS}

    EFFECT_CATEGORIES = ("Looks", "Portrait", "Face", "Masks", "Framing", "Distort",
                         "Retro TV", "Touch-up", "Frames", "Shapes",
                         *FRAME_GROUPS, "Animations", "Emoji rain")

    _CAPTURE_KINDS = ("camera", "screen", "video", "network", "phone", "test")

    def edit_overlay(self, layer_id: str, values: tuple[str, str, str] | None = None) -> bool:
        """Edit a ticker or lower third; ``values`` skips the dialog (tests, scripts)."""
        layer = self.scene.layer(layer_id)
        if layer is None or layer.kind not in ("ticker", "lower_third", "clock", "timer"):
            return False
        if values is None:
            dialog = OverlayDialog(layer, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            values = dialog.values()
        source, colour, background = values
        if not source.strip("| "):
            self._status("Text cannot be empty")
            return False
        layer.source, layer.color, layer.background = source, colour, background
        if layer.kind == "lower_third":
            layer.name = source.partition("|")[0] or layer.name
        self._refresh_layers()
        self._mark_dirty()
        # Applied to the running canvas; a rebuild only if it is not there yet.
        if self.engine is not None and not self.engine.update_overlay(layer):
            self.restart_engine()
        self._status(f"Updated {layer.name}")
        return True

    # -------------------------------------------------------- drawing on video
    DRAW_COLOURS = ("#ff3b5c", "#facc15", "#5eead0", "#60a5fa", "#ffffff", "#111827")

    DRAW_TOOLS = (("pen", "Pen"), ("marker", "Marker"), ("arrow", "Arrow"),
                  ("box", "Box"))

    def eventFilter(self, watched, event):
        grid = getattr(self, "effect_grid", None)
        if watched is grid and event.type() == QEvent.Type.Show and self._thumb_queue:
            self._thumb_timer.start()
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if getattr(self, "_gpu_preview_pending", False) and self.engine is not None:
            # The canvas was rebuilt while hidden, so it runs the image preview
            # behind the GPU view.  Rebuild once the view has been shown and
            # rendered, so the GPU preview takes over again.
            self._gpu_preview_pending = False
            QTimer.singleShot(150, self._resume_gpu_preview)
