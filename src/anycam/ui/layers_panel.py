"""The Layers tab and the transform editor."""
from __future__ import annotations


from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QPushButton, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

from .. import animated, config, content, geometry
from ..geometry import clamp
from ..model import (Layer, new_id)
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class LayersMixin:
    """The Layers tab and the transform editor."""

    def _layers_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.layer_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.layer_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.layer_list.model().rowsMoved.connect(self._layers_reordered)
        self.layer_list.itemChanged.connect(self._layer_visibility)
        self.layer_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.layer_list.customContextMenuRequested.connect(self._layer_list_menu)
        self.layer_list.itemDoubleClicked.connect(
            lambda item: self.rename_layer(item.data(Qt.ItemDataRole.UserRole)))
        self.layer_list.itemSelectionChanged.connect(self._layers_selection_changed)
        layout.addWidget(self.layer_list, 1)

        transform = QGroupBox("Transform")
        transform_layout = QVBoxLayout(transform)
        modes = QHBoxLayout()
        self.move_button = QPushButton("Move / resize")
        self.move_button.setCheckable(True)
        self.move_button.setChecked(True)
        self.crop_button = QPushButton("Crop")
        self.crop_button.setCheckable(True)
        group = QButtonGroup(transform)
        group.setExclusive(True)
        group.addButton(self.move_button)
        group.addButton(self.crop_button)
        self.move_button.clicked.connect(lambda: self._set_edit_mode("move"))
        self.crop_button.clicked.connect(lambda: self._set_edit_mode("crop"))
        modes.addWidget(self.move_button)
        modes.addWidget(self.crop_button)
        transform_layout.addLayout(modes)
        self.transform_hint = QLabel(
            "Drag in the preview to move; drag a handle to resize. Arrow keys "
            "nudge (Shift = 10×); Ctrl+Alt+arrows align to the canvas.")
        self.transform_hint.setObjectName("muted")
        self.transform_hint.setWordWrap(True)
        transform_layout.addWidget(self.transform_hint)

        snap_row = QHBoxLayout()
        self.snap_check = QCheckBox("Snap to guides")
        self.snap_check.setChecked(self.preview.snap_enabled)
        self.snap_check.toggled.connect(self._snap_toggled)
        self.snap_threshold = QSpinBox()
        self.snap_threshold.setRange(0, 32)
        self.snap_threshold.setValue(int(round(self.preview.snap_px)))
        self.snap_threshold.setSuffix(" px")
        self.snap_threshold.valueChanged.connect(self._snap_threshold_changed)
        snap_row.addWidget(self.snap_check)
        snap_row.addStretch(1)
        snap_row.addWidget(QLabel("Tolerance"))
        snap_row.addWidget(self.snap_threshold)
        transform_layout.addLayout(snap_row)

        fields = QGridLayout()
        fields.setHorizontalSpacing(6)
        fields.setVerticalSpacing(3)
        self.box_spins: dict[str, QDoubleSpinBox] = {}
        self.crop_spins: dict[str, QDoubleSpinBox] = {}
        fields.addWidget(QLabel("Box (normalised 0–1)"), 0, 0, 1, 4)
        for col, (label, key) in enumerate((("X", "x"), ("Y", "y"),
                                            ("W", "w"), ("H", "h"))):
            fields.addWidget(QLabel(label), 1, col)
            spin = QDoubleSpinBox()
            # Typing a box that leaves the canvas is allowed (it is clipped).
            if key in ("x", "y"):
                spin.setRange(*geometry.BOX_POSITION_RANGE)
            else:
                spin.setRange(PreviewWidget.MIN_BOX, geometry.BOX_SIZE_RANGE[1])
            spin.setDecimals(3)
            spin.setSingleStep(0.005)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(
                lambda value, field=key: self._box_field_changed(field, value))
            self.box_spins[key] = spin
            fields.addWidget(spin, 2, col)
        fields.addWidget(QLabel("Crop insets L / T / R / B"), 3, 0, 1, 4)
        for col, (label, key) in enumerate((("L", "l"), ("T", "t"),
                                            ("R", "r"), ("B", "b"))):
            fields.addWidget(QLabel(label), 4, col)
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 0.9)
            spin.setDecimals(3)
            spin.setSingleStep(0.005)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(
                lambda value, field=key: self._crop_field_changed(field, value))
            self.crop_spins[key] = spin
            fields.addWidget(spin, 5, col)
        transform_layout.addLayout(fields)
        self.reset_crop_button = QPushButton("Reset crop")
        self.reset_crop_button.clicked.connect(self._reset_crop)
        transform_layout.addWidget(self.reset_crop_button)
        layout.addWidget(transform)
        layout.addWidget(self._framing_group())

        camera = QGroupBox("Camera source")
        camera_layout = QGridLayout(camera)
        self.camera_combo = QComboBox()
        self.camera_combo.currentIndexChanged.connect(self._camera_device_changed)
        self.camera_refresh = QPushButton("Refresh")
        self.camera_refresh.clicked.connect(self._refresh_cameras)
        camera_layout.addWidget(QLabel("V4L2 device"), 0, 0)
        camera_layout.addWidget(self.camera_combo, 0, 1)
        camera_layout.addWidget(self.camera_refresh, 0, 2)
        self.camera_width = QSpinBox()
        self.camera_width.setRange(0, 7680)
        self.camera_width.setSpecialValueText("Auto")
        self.camera_width.setKeyboardTracking(False)
        self.camera_width.valueChanged.connect(self._camera_mode_changed)
        self.camera_height = QSpinBox()
        self.camera_height.setRange(0, 4320)
        self.camera_height.setSpecialValueText("Auto")
        self.camera_height.setKeyboardTracking(False)
        self.camera_height.valueChanged.connect(self._camera_mode_changed)
        self.camera_fps = QSpinBox()
        self.camera_fps.setRange(0, 240)
        self.camera_fps.setSpecialValueText("Auto")
        self.camera_fps.setKeyboardTracking(False)
        self.camera_fps.valueChanged.connect(self._camera_mode_changed)
        size_row = QHBoxLayout()
        size_row.setSpacing(4)
        size_row.addWidget(self.camera_width)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.camera_height)
        camera_layout.addWidget(QLabel("Capture size"), 1, 0)
        camera_layout.addLayout(size_row, 1, 1)
        camera_layout.addWidget(QLabel("Capture FPS"), 2, 0)
        camera_layout.addWidget(self.camera_fps, 2, 1)
        self.camera_note = QLabel(
            "Each camera layer keeps its own device and capture mode. Auto "
            "follows the layer box and canvas rate; 0 in a field means Auto.")
        self.camera_note.setObjectName("muted")
        self.camera_note.setWordWrap(True)
        camera_layout.addWidget(self.camera_note, 3, 0, 1, 3)
        layout.addWidget(camera)

        appearance = QGroupBox("Opacity and layer order")
        appearance_layout = QGridLayout(appearance)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.valueChanged.connect(self._opacity_changed)
        self.opacity_slider.sliderPressed.connect(self._begin_opacity_edit)
        self.opacity_slider.sliderReleased.connect(self._finish_opacity_edit)
        self.opacity_label = QLabel("100%")
        self.opacity_label.setObjectName("accent")
        appearance_layout.addWidget(QLabel("Opacity"), 0, 0)
        appearance_layout.addWidget(self.opacity_slider, 0, 1)
        appearance_layout.addWidget(self.opacity_label, 0, 2)
        self.layer_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.layer_volume_slider.setRange(0, 200)
        self.layer_volume_slider.setValue(100)
        self.layer_volume_slider.setToolTip("The sound of a video file (video layers only)")
        self.layer_volume_slider.valueChanged.connect(self._layer_volume_changed)
        self.layer_volume_label = QLabel("100%")
        self.layer_volume_label.setObjectName("accent")
        self.layer_mute_check = QCheckBox("Mute")
        self.layer_mute_check.toggled.connect(self._layer_volume_changed)
        appearance_layout.addWidget(QLabel("Volume"), 1, 0)
        appearance_layout.addWidget(self.layer_volume_slider, 1, 1)
        appearance_layout.addWidget(self.layer_volume_label, 1, 2)
        appearance_layout.addWidget(self.layer_mute_check, 2, 1)
        self.order_buttons = []
        order_row = QHBoxLayout()
        for text, action in (("Raise", "raise"), ("Lower", "lower"),
                             ("Top", "top"), ("Bottom", "bottom")):
            button = QPushButton(text)
            button.clicked.connect(
                lambda _=False, act=action: self._move_layer(act))
            self.order_buttons.append(button)
            order_row.addWidget(button)
        appearance_layout.addLayout(order_row, 1, 0, 1, 3)
        history_row = QHBoxLayout()
        self.undo_button = QPushButton("Undo")
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self.undo)
        self.redo_button = QPushButton("Redo")
        self.redo_button.setEnabled(False)
        self.redo_button.clicked.connect(self.redo)
        history_row.addWidget(self.undo_button)
        history_row.addWidget(self.redo_button)
        appearance_layout.addLayout(history_row, 2, 0, 1, 3)
        layout.addWidget(appearance)

        grid = QGridLayout()
        sources = (("Camera", "camera"), ("Test pattern", "test"),
                   ("Text", "text"), ("Logo", "logo"),
                   ("Image", "image"), ("Video file", "video"),
                   ("Screen", "screen"), ("Phone camera", "phone"),
                   ("Network URL", "network"),
                   ("Ticker", "ticker"), ("Lower third", "lower_third"),
                   ("Clock", "clock"), ("Timer", "timer"),
                   ("Animated GIF", "gif"))
        for index, (label, kind) in enumerate(sources):
            button = QPushButton(f"+ {label}")
            button.clicked.connect(lambda _=False, k=kind: self.add_layer(k))
            grid.addWidget(button, index // 2, index % 2)
        layout.addLayout(grid)
        action_row = QHBoxLayout()
        duplicate = QPushButton("Duplicate selected")
        duplicate.clicked.connect(self.duplicate_layer)
        remove = QPushButton("Remove selected")
        remove.setObjectName("danger")
        remove.clicked.connect(self.remove_layer)
        action_row.addWidget(duplicate)
        action_row.addWidget(remove)
        layout.addLayout(action_row)
        self._refresh_cameras()
        self._refresh_layers()
        return page

    def _refresh_layers(self) -> None:
        if not hasattr(self, "layer_list"):
            return
        selected = list(self.preview.selected_ids) if hasattr(self, "preview") else []
        primary = self.preview.selected_id if hasattr(self, "preview") else ""
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for layer in sorted(self.scene.layers, key=lambda l: l.z):
            item = QListWidgetItem(f"{layer.name}  ·  {layer.kind}"
                                   + ("  ·  locked" if layer.locked else ""))
            item.setData(Qt.ItemDataRole.UserRole, layer.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if layer.visible else Qt.CheckState.Unchecked)
            self.layer_list.addItem(item)
            if layer.id == primary:
                self.layer_list.setCurrentItem(item)
            if layer.id in selected:
                item.setSelected(True)
        self.layer_list.blockSignals(False)
        if hasattr(self, "preview"):
            self.preview.set_scene(self.scene)
            self._sync_transform_fields()
            self._sync_effects_controls()

    def _layer_list_menu(self, position) -> None:
        item = self.layer_list.itemAt(position)
        layer_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else ""
        self._preview_context_menu(layer_id, self.layer_list.viewport().mapToGlobal(position))

    def rename_layer(self, layer_id: str = "", name: str | None = None) -> bool:
        """Give a layer a new name (asks for one when ``name`` is not given)."""
        layer = self.scene.layer(layer_id or self.preview.selected_id)
        if layer is None:
            return False
        if name is None:
            name, accepted = QInputDialog.getText(
                self, "Rename layer", "Layer name:", text=layer.name)
            if not accepted:
                return False
        name = " ".join(str(name).split())
        if not name or name == layer.name:
            return False
        before = self._layout_snapshot()
        layer.name = name
        self._refresh_layers()
        self._record_history(before)
        self._mark_dirty()
        self._status(f"Renamed layer to {name}")
        return True

    def set_layer_locked(self, layer_id: str, locked: bool) -> bool:
        """Lock a layer against accidental dragging, nudging and removal (or unlock it)."""
        layer = self.scene.layer(layer_id)
        if layer is None or layer.locked == bool(locked):
            return False
        layer.locked = bool(locked)
        self._refresh_layers()
        self._mark_dirty()
        self._status(f"{layer.name} {'locked' if layer.locked else 'unlocked'}")
        return True

    def _layers_reordered(self, *_args) -> None:
        # Map the new list order back to scene.layers by z-order.
        ids = [self.layer_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self.layer_list.count())]
        by_id = {layer.id: layer for layer in self.scene.layers}
        ordered = [by_id[lid] for lid in ids if lid in by_id]
        if len(ordered) != len(self.scene.layers):
            return  # the list is mid-update; do not drop layers from the scene
        before = self._layout_snapshot()
        # Reassign z so the list order is the stacking order.
        for i, layer in enumerate(ordered):
            layer.z = (i + 1) * 10
        self.scene.layers = ordered
        self._record_history(before)
        self._mark_dirty()
        if self.engine is not None:
            # Stacking is only each pad's zorder: no rebuild, so a screen
            # share and the virtual camera are not interrupted by a drag.
            self.engine.refresh_layer_order()

    def add_layer(self, kind: str, source: str = "",
                  box: tuple[float, float, float, float] | None = None) -> Layer | None:
        """Add a source layer, using a picker only when no source is supplied.

        ``box`` overrides the default drop position; the source grid uses it
        to put a new layer straight into the cell the user clicked.
        """
        if kind == "camera":
            if not source:
                source = (config.camera_device() if self.headless
                          else self._choose_camera_device())
            if not source:
                return None
            source = config.camera_identity(source)
            layer = Layer(new_id("layer"), "camera", self._camera_layer_name(source),
                          source=source, box=(0.62, 0.60, 0.34, 0.34), z=0)
        elif kind == "test":
            layer = Layer(new_id("layer"), "test", "Test pattern", box=(0.62, 0.60, 0.34, 0.34), z=0)
        elif kind == "text":
            layer = Layer(new_id("layer"), "text", "Caption", source="Your text here",
                          box=(0.05, 0.75, 0.30, 0.08), z=0, font_size=0.04)
        elif kind == "ticker":
            if not source:
                text, ok = QInputDialog.getText(
                    self, "Ticker", "Text to scroll along the bottom:",
                    text="Welcome to the stream")
                if not ok or not text.strip():
                    return None
                source = text.strip()
            layer = Layer(new_id("layer"), "ticker", "Ticker", source=source,
                          box=(0.0, 0.9, 1.0, 0.1), z=0, color="#ffffff",
                          background="#101418cc")
        elif kind == "clock":
            layer = Layer(new_id("layer"), "clock", "Clock", source=source or "%H:%M:%S",
                          box=(0.80, 0.03, 0.17, 0.07), z=0, color="#ffffff",
                          background="#101418cc")
        elif kind == "timer":
            if not source:
                text, ok = QInputDialog.getText(
                    self, "Timer", "Countdown from (mm:ss), or type stopwatch:",
                    text="05:00")
                if not ok or not text.strip():
                    return None
                source = text.strip()
            layer = Layer(new_id("layer"), "timer", "Timer", source=source,
                          box=(0.80, 0.11, 0.17, 0.07), z=0, color="#ffffff",
                          background="#101418cc")
        elif kind == "lower_third":
            if not source:
                name, ok = QInputDialog.getText(self, "Lower third", "Name:")
                if not ok or not name.strip():
                    return None
                title, _ok = QInputDialog.getText(
                    self, "Lower third", "Title or subtitle (optional):")
                style, picked = QInputDialog.getItem(
                    self, "Lower third", "Style:", list(animated.LOWER_THIRD_TEMPLATES),
                    0, False)
                source = animated.join_lower_third(name, title, style if picked else "Classic")
            layer = Layer(new_id("layer"), "lower_third",
                          source.partition("|")[0] or "Lower third", source=source,
                          box=(0.04, 0.66, 0.50, 0.20), z=0, color="#ffffff",
                          background="#101418e6")
        elif kind == "particles":
            from .. import particles
            layer = Layer(new_id("layer"), "particles", particles.label_for(source),
                          source=source or "confetti", box=(0.0, 0.0, 1.0, 1.0), z=0)
        elif kind == "gif":
            if not source:
                path, _ = QFileDialog.getOpenFileName(
                    self, "Choose an animated image", "",
                    "Animated images (*.gif *.webp *.apng *.png)")
                if not path:
                    return None
                source = path
            path = Path(source).expanduser()
            if not path.exists():
                self._status(f"Source file not found: {path}")
                return None
            layer = Layer(new_id("layer"), "gif", path.stem, source=str(path),
                          box=(0.74, 0.05, 0.22, 0.30), z=0)
        elif kind in ("logo", "image", "video"):
            if not source:
                if kind == "video":
                    title, patterns = "Choose a video", "Video (*.mp4 *.mkv *.webm *.mov *.avi *.m4v)"
                else:
                    title, patterns = ("Choose a logo" if kind == "logo" else "Choose an image",
                                       "Images (*.png *.jpg *.jpeg *.webp)")
                path, _ = QFileDialog.getOpenFileName(self, title, "", patterns)
                if not path:
                    return None
                source = path
            path = Path(source).expanduser()
            if not path.exists():
                self._status(f"Source file not found: {path}")
                return None
            # A caller's box (a layout slot's "+") wins over the default spot;
            # overwriting it put "Add image…" from a slot in the corner.
            default = ((0.62, 0.60, 0.34, 0.34) if kind == "video"
                       else (0.04, 0.04, 0.18, 0.18))
            layer = Layer(new_id("layer"), kind, path.stem, source=str(path),
                          box=box or default, z=0)
        elif kind == "phone":
            if not source:
                if self.headless:
                    return None
                paired = self._pair_phone()
                if paired is None:
                    return None
                source, name = paired
            else:
                # A phone layer needs the link listening so the phone can
                # (re)connect, however the layer got here.
                self._phone_link(quiet=True)
                name = self._phone_name(source)
            for existing in self.scene.layers:
                if existing.kind == "phone" and existing.source == source:
                    # The phone is already in this scene; it just reconnected.
                    self.preview.select_layer(existing.id)
                    self._status(f"{existing.name} is connected")
                    return existing
            layer = Layer(new_id("layer"), "phone", name, source=source,
                          box=box or (0.0, 0.0, 1.0, 1.0), z=0)
        elif kind == "network":
            if not source:
                source, ok = QInputDialog.getText(
                    self, "Network source",
                    "RTSP or HTTP URL (phone cameras: IP Webcam, DroidCam, "
                    "iVCam — e.g. http://phone-ip:8080/video):")
                if not ok:
                    return None
            uri = source.strip()
            if content.needs_resolving(uri):
                # A video-site page is not a stream: ask yt-dlp for the real address.
                self._status("Looking up the stream\u2026")
                resolved, problem = self._run_with_ui(lambda: content.resolve_stream(uri))
                if problem:
                    self._status(problem)
                    return None
                uri = resolved
            parsed = urlparse(uri)
            if (parsed.scheme.lower() not in ("rtsp", "rtsps", "http", "https")
                    or not parsed.netloc):
                self._status("Network source must be an rtsp://, rtsps://, http:// or https:// URL")
                return None
            layer = Layer(new_id("layer"), "network", config.host_of(uri) or "Network",
                          source=uri, box=(0.0, 0.0, 1.0, 1.0), z=0)
        elif kind in ("screen", "screen-new"):
            # "screen-new" always opens the system picker, for a share that is
            # not the one already open.
            return self._add_screen_layer(box=box, new_share=kind == "screen-new")
        else:
            return None
        if box is not None:
            layer.box = tuple(float(value) for value in box)
        return self._insert_layer(layer)

    def _insert_layer(self, layer: Layer) -> Layer:
        before = self._layout_snapshot()
        self.scene.add_layer(layer)
        self._refresh_layers()
        if layer.kind != "particles":
            # An animation is an overlay: keep the selection on the picture the
            # user is working on so frames and filters keep going there.
            self.preview.select_layer(layer.id)
        self._record_history(before)
        if self.engine is not None:
            # Add to the running canvas.  A rebuild would destroy the screen
            # pipewiresrc (and its portal session) and make Discord go black.
            attached = False
            if layer.kind == "screen" and self.engine.can_reattach_screen(layer):
                # The removed share is still PLAYING; no new remote is needed.
                attached = self.engine.add_layer(layer)
            elif layer.kind == "screen":
                session = self._screen_sessions.get(layer.id)
                size = None
                if session is not None and session.streams:
                    stream = session.streams[0]
                    if stream.width > 1 and stream.height > 1:
                        size = (stream.width, stream.height)
                try:
                    # pipewiresrc owns and closes this fd with its pipeline.
                    # A fresh portal remote is required for every consumer.
                    fd = session.take_remote() if session is not None else -1
                except Exception as exc:  # noqa: BLE001 - surface, do not kill UI
                    # Take the layer out again: left in the scene, every later
                    # start failed on it ("Could not start the pipeline").
                    self._screen_sessions.pop(layer.id, None)
                    if self._last_screen_session is session:
                        self._last_screen_session = None
                    if self.scene.layer(layer.id) is not None:
                        self.scene.remove_layer(layer.id)
                    self._refresh_layers()
                    self._status(f"Screen capture unavailable: {exc}. Add the share again.")
                    return layer
                attached = self.engine.add_layer(
                    layer, screen_fd=fd, screen_size=size)
            else:
                attached = self.engine.add_layer(layer)
            if not attached:
                self.restart_engine()
        return layer

    def duplicate_layer(self) -> None:
        """Clone the selected layers above their source, as one undo step."""
        sources = [self.scene.layer(layer_id)
                   for layer_id in self.preview.selected_ids]
        sources = [layer for layer in sources if layer is not None]
        if not sources:
            self._status("Select a layer first")
            return
        before = self._layout_snapshot()
        top = max((layer.z for layer in self.scene.layers), default=0)
        copies = []
        for index, source in enumerate(sources):
            clone = Layer.from_dict(source.to_dict())
            clone.id = new_id("layer")
            clone.name = f"{source.name} copy"
            x, y, w, h = source.box
            clone.box = (clamp(x + 0.02, 0.0, 1.0 - w),
                         clamp(y + 0.02, 0.0, 1.0 - h), w, h)
            clone.z = top + 1 + index
            self.scene.add_layer(clone)
            copies.append(clone.id)
            if source.id in self._screen_sessions:
                # Two layers can share one portal stream; the session is only
                # closed when the last referencing layer is removed.
                self._screen_sessions[clone.id] = self._screen_sessions[source.id]
        self._refresh_layers()
        self.preview.set_selection(copies, copies[-1])
        self._record_history(before)
        if self.engine is not None:
            self.restart_engine()
        self._status(f"Duplicated {len(copies)} layer(s)")

    def remove_layer(self) -> None:
        items = self.layer_list.selectedItems()
        if not items:
            return
        self.remove_layers([item.data(Qt.ItemDataRole.UserRole) for item in items])

    def remove_layers(self, removed) -> None:
        """Remove layers by id from whatever selected them (list or canvas)."""
        removed = [layer_id for layer_id in removed
                   if layer_id and self.scene.layer(layer_id) is not None]
        locked = [layer_id for layer_id in removed if self.scene.layer(layer_id).locked]
        if locked:
            removed = [layer_id for layer_id in removed if layer_id not in locked]
            self._status("Locked layers were kept: unlock them to remove them")
        if not removed:
            return
        before = self._layout_snapshot()
        if self.engine is not None:
            # Detach before removing from the model: the engine needs the
            # layer's spec to release exactly that capture.  Rebuilding here
            # tore down the screen pipewiresrc and killed the portal session
            # even when only the camera was removed.
            for layer_id in removed:
                self.engine.detach_layer(layer_id)
        for layer_id in removed:
            self.scene.remove_layer(layer_id)
            self._lost_cameras.pop(layer_id, None)
            self.playlist_ids = [item for item in self.playlist_ids if item != layer_id]
        self.playlist_index = min(self.playlist_index, len(self.playlist_ids) - 1)
        self._refresh_layers()
        self._record_history(before)
        if self.engine is not None and not any(
                layer.visible for layer in self.scene.layers):
            # The permanent zorder=0 standby keeps the canvas alive with a
            # branded slate instead of the removed source's last frame.
            self.log_event(
                "all sources removed; showing the AnyCam standby background")

    def _layers_selection_changed(self) -> None:
        if self._syncing_selection:
            return
        items = self.layer_list.selectedItems()
        ids = [item.data(Qt.ItemDataRole.UserRole) for item in items]
        current = self.layer_list.currentItem()
        primary = current.data(Qt.ItemDataRole.UserRole) if current in items else (
            ids[-1] if ids else "")
        self._syncing_selection = True
        self.preview.set_selection(ids, primary)
        self._syncing_selection = False
        self._sync_transform_fields()
        self._sync_effects_controls()
        layer = self.preview.selected_layer()
        if layer is not None:
            if len(ids) > 1:
                self._status(f"{len(ids)} layers selected · drag to move together")
            else:
                self._status(f"Selected {layer.name} · drag in the preview to edit")

    def _preview_selection(self, layer_id: str) -> None:
        """Mirror a preview click into the Layers list without a signal loop."""
        if not self._syncing_selection:
            self._syncing_selection = True
            self.layer_list.blockSignals(True)
            self.layer_list.clearSelection()
            self.layer_list.setCurrentRow(-1)
            for row in range(self.layer_list.count()):
                item = self.layer_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == layer_id:
                    self.layer_list.setCurrentItem(item)
            for row in range(self.layer_list.count()):
                item = self.layer_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) in self.preview.selected_ids:
                    item.setSelected(True)
            self.layer_list.blockSignals(False)
            self._syncing_selection = False
        wanted = layer_id if self.preview.edit_mode == "crop" else ""
        if wanted != self._crop_preview_layer_id:
            self._crop_preview_layer_id = wanted
            if self.engine is not None:
                self.restart_engine()
        self._sync_transform_fields()
        self._sync_effects_controls()
        layer = self.scene.layer(layer_id) if layer_id else None
        if layer is not None:
            if len(self.preview.selected_ids) > 1:
                self._status(f"{len(self.preview.selected_ids)} layers selected")
            else:
                self._status(f"Selected {layer.name} · drag in the preview to edit")

    def _layer_visibility(self, item: QListWidgetItem) -> None:
        layer = self.scene.layer(item.data(Qt.ItemDataRole.UserRole))
        if layer is None:
            return
        layer.visible = item.checkState() == Qt.CheckState.Checked
        self._mark_dirty()
        if self.engine is not None:
            # Toggle the compositor pad alpha; rebuilding the graph here would
            # end the current MP4 segment for a purely cosmetic change.
            self.engine.set_layer_visible(layer.id, layer.visible)

    def _move_layer(self, action: str) -> None:
        layer = self.preview.selected_layer()
        if layer is None:
            self._status("Select a layer first")
            return
        before = self._layout_snapshot()
        layers = sorted(self.scene.layers,
                        key=lambda item: (item.z, self.scene.layers.index(item)))
        index = next(i for i, item in enumerate(layers) if item.id == layer.id)
        if action == "raise":
            target = min(len(layers) - 1, index + 1)
        elif action == "lower":
            target = max(0, index - 1)
        elif action == "top":
            target = len(layers) - 1
        else:
            target = 0
        if target == index:
            self._status(f"{layer.name} is already at the {'top' if action in ('raise', 'top') else 'bottom'}")
            return
        layers.pop(index)
        layers.insert(target, layer)
        for position, item in enumerate(layers):
            item.z = position
        self._refresh_layers()
        self._sync_transform_fields()
        self._record_history(before)
        if self.engine is not None:
            self.engine.refresh_layer_order()
        self._status(f"{layer.name}: {action}")

    def _align_layers(self, action: str) -> None:
        """Snap the selected layers to a canvas edge or centre line."""
        targets = self.preview.selected_layers()
        if not targets:
            primary = self.preview.selected_layer()
            if primary is None:
                self._status("Select a layer first")
                return
            targets = [primary]
        before = self._layout_snapshot()
        for layer in targets:
            x, y, w, h = layer.box
            if action == "left":
                x = 0.0
            elif action == "right":
                x = 1.0 - w
            elif action == "top":
                y = 0.0
            elif action == "bottom":
                y = 1.0 - h
            elif action == "hcenter":
                x = (1.0 - w) / 2.0
            elif action == "vcenter":
                y = (1.0 - h) / 2.0
            else:
                return
            layer.box = (clamp(x, 0.0, 1.0 - w), clamp(y, 0.0, 1.0 - h), w, h)
            if self.engine is not None:
                self.engine.set_layer_box(layer.id, layer.box)
        self.preview.update()
        self._sync_transform_fields()
        self._record_history(before)
        label = {"left": "left", "right": "right", "top": "top",
                 "bottom": "bottom", "hcenter": "centre horizontally",
                 "vcenter": "centre vertically"}[action]
        count = len(targets)
        self._status(f"Aligned {'1 layer' if count == 1 else f'{count} layers'} {label}")

    # ------------------------------------------------------------- framing
    def _framing_group(self) -> QGroupBox:
        group = QGroupBox("Frame")
        grid = QGridLayout(group)
        self.fit_combo = QComboBox()
        for label, value in (("Contain (show everything)", "contain"),
                             ("Cover (fill, trim the overflow)", "cover"),
                             ("Stretch (fill, ignore the shape)", "stretch")):
            self.fit_combo.addItem(label, value)
        self.fit_combo.currentIndexChanged.connect(self._framing_changed)
        self.flip_h_check = QCheckBox("Flip horizontally")
        self.flip_v_check = QCheckBox("Flip vertically")
        self.flip_h_check.toggled.connect(self._framing_changed)
        self.flip_v_check.toggled.connect(self._framing_changed)
        self.rotation_combo = QComboBox()
        for degrees in geometry.ROTATIONS:
            self.rotation_combo.addItem(f"{degrees}°", degrees)
        self.rotation_combo.currentIndexChanged.connect(self._framing_changed)
        self.zoom_spin = QDoubleSpinBox()
        self.zoom_spin.setRange(1.0, geometry.MAX_ZOOM)
        self.zoom_spin.setSingleStep(0.1)
        self.zoom_spin.setSuffix("×")
        self.zoom_spin.setKeyboardTracking(False)
        self.zoom_spin.valueChanged.connect(self._framing_live_changed)
        self.pan_spins = {}
        for key in ("x", "y"):
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 1.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.05)
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(self._framing_live_changed)
            self.pan_spins[key] = spin
        grid.addWidget(QLabel("Fit"), 0, 0)
        grid.addWidget(self.fit_combo, 0, 1, 1, 3)
        grid.addWidget(self.flip_h_check, 1, 0, 1, 2)
        grid.addWidget(self.flip_v_check, 1, 2, 1, 2)
        grid.addWidget(QLabel("Rotate"), 2, 0)
        grid.addWidget(self.rotation_combo, 2, 1)
        grid.addWidget(QLabel("Zoom"), 3, 0)
        grid.addWidget(self.zoom_spin, 3, 1)
        grid.addWidget(QLabel("Pan X / Y"), 4, 0)
        grid.addWidget(self.pan_spins["x"], 4, 1)
        grid.addWidget(self.pan_spins["y"], 4, 2)
        return group

    def _sync_framing_fields(self, layer: Layer | None) -> None:
        widgets = (self.fit_combo, self.flip_h_check, self.flip_v_check,
                   self.rotation_combo, self.zoom_spin, *self.pan_spins.values())
        for widget in widgets:
            widget.blockSignals(True)
            widget.setEnabled(layer is not None)
        fit, flip_h, flip_v, rotation, zoom, pan = geometry.layer_transform(
            layer if layer is not None else Layer("none", "test", ""))
        self.fit_combo.setCurrentIndex(max(0, self.fit_combo.findData(fit)))
        self.flip_h_check.setChecked(flip_h)
        self.flip_v_check.setChecked(flip_v)
        self.rotation_combo.setCurrentIndex(max(0, self.rotation_combo.findData(rotation)))
        self.zoom_spin.setValue(zoom)
        self.pan_spins["x"].setValue(pan[0])
        self.pan_spins["y"].setValue(pan[1])
        for widget in widgets:
            widget.blockSignals(False)

    def _layer_volume_changed(self, _value=None) -> None:
        layer = self.preview.selected_layer()
        if layer is None or layer.kind != "video":
            return
        layer.volume = self.layer_volume_slider.value() / 100.0
        layer.muted = self.layer_mute_check.isChecked()
        self.layer_volume_label.setText(f"{self.layer_volume_slider.value()}%")
        self._mark_dirty()
        if self.engine is not None:
            self.engine.set_layer_volume(layer.id, layer.volume, layer.muted)

    def _sync_layer_volume(self, layer: Layer | None) -> None:
        is_video = layer is not None and layer.kind == "video"
        for widget in (self.layer_volume_slider, self.layer_mute_check):
            widget.blockSignals(True)
            widget.setEnabled(is_video)
        self.layer_volume_slider.setValue(int(round((layer.volume if is_video else 1.0) * 100)))
        self.layer_mute_check.setChecked(bool(layer.muted) if is_video else False)
        self.layer_volume_label.setText(f"{self.layer_volume_slider.value()}%")
        for widget in (self.layer_volume_slider, self.layer_mute_check):
            widget.blockSignals(False)

    def _framing_changed(self, _value=None) -> None:
        """Fit, flip and rotation shape the chain, so they rebuild the layer."""
        layer = self.preview.selected_layer()
        if layer is None:
            return
        before = self._layout_snapshot()
        layer.fit = str(self.fit_combo.currentData() or "contain")
        layer.flip_h = self.flip_h_check.isChecked()
        layer.flip_v = self.flip_v_check.isChecked()
        layer.rotation = int(self.rotation_combo.currentData() or 0)
        self._record_history(before)
        self._mark_dirty()
        self.preview.update()
        self._schedule_transform_commit(layer.id)

    def _framing_live_changed(self, _value=None) -> None:
        """Zoom and pan move a live crop; only a layer built without one restarts."""
        layer = self.preview.selected_layer()
        if layer is None:
            return
        before = self._layout_snapshot()
        layer.zoom = geometry.clamp(self.zoom_spin.value(), 1.0, geometry.MAX_ZOOM)
        layer.pan = (self.pan_spins["x"].value(), self.pan_spins["y"].value())
        self._record_history(before)
        self._mark_dirty()
        if self.engine is not None and not self.engine.set_layer_framing(layer.id):
            self._schedule_transform_commit(layer.id)

    def _sync_transform_fields(self) -> None:
        if not hasattr(self, "box_spins"):
            return
        layer = self.preview.selected_layer()
        enabled = layer is not None
        self._sync_framing_fields(layer)
        self._sync_layer_volume(layer)
        for spin in (*self.box_spins.values(), *self.crop_spins.values()):
            spin.setEnabled(enabled)
        self.reset_crop_button.setEnabled(enabled)
        self.opacity_slider.setEnabled(enabled)
        self.opacity_label.setEnabled(enabled)
        for button in self.order_buttons:
            button.setEnabled(enabled)
        is_camera = layer is not None and layer.kind == "camera"
        self.camera_combo.setEnabled(is_camera)
        self.camera_refresh.setEnabled(True)
        for spin, value in (
                (self.camera_width, layer.camera_width if is_camera else 0),
                (self.camera_height, layer.camera_height if is_camera else 0),
                (self.camera_fps, layer.camera_fps if is_camera else 0)):
            spin.blockSignals(True)
            spin.setValue(int(value))
            spin.blockSignals(False)
            spin.setEnabled(is_camera)
        if is_camera:
            self._sync_camera_combo(layer.source or "")
        else:
            self.camera_combo.blockSignals(True)
            self.camera_combo.clear()
            self.camera_combo.addItem("Select a camera layer", "")
            self.camera_combo.blockSignals(False)
        box_values = dict(zip(("x", "y", "w", "h"),
                              layer.box if layer else (0.0, 0.0, 1.0, 1.0)))
        crop_values = dict(zip(("l", "t", "r", "b"),
                               layer.crop if layer else (0.0, 0.0, 0.0, 0.0)))
        for key, spin in self.box_spins.items():
            spin.blockSignals(True)
            spin.setValue(box_values[key])
            spin.blockSignals(False)
        for key, spin in self.crop_spins.items():
            spin.blockSignals(True)
            spin.setValue(crop_values[key])
            spin.blockSignals(False)
        opacity = int(round((layer.alpha if layer else 1.0) * 100))
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(opacity)
        self.opacity_slider.blockSignals(False)
        self.opacity_label.setText(f"{opacity}%")

    def _box_field_changed(self, key: str, value: float) -> None:
        layer = self.preview.selected_layer()
        if layer is None:
            return
        before = self._layout_snapshot()
        x, y, w, h = layer.box
        targets = self.preview.selected_layers() or [layer]
        low, high = geometry.BOX_POSITION_RANGE
        if key in ("x", "y"):
            delta = (clamp(value, low, high) - (x if key == "x" else y))
            for item in targets:
                ix, iy, iw, ih = item.box
                if key == "x":
                    item.box = (clamp(ix + delta, low, high), iy, iw, ih)
                else:
                    item.box = (ix, clamp(iy + delta, low, high), iw, ih)
        else:
            targets = [layer]
            top = geometry.BOX_SIZE_RANGE[1]
            if key == "w":
                w = clamp(value, PreviewWidget.MIN_BOX, top)
            else:
                h = clamp(value, PreviewWidget.MIN_BOX, top)
            layer.box = (x, y, w, h)
        self.preview.update()
        self._sync_transform_fields()
        self._record_history(before)
        if self.engine is not None:
            for item in targets:
                self.engine.set_layer_box(item.id, item.box)
        if any(item.kind in ("text", "logo", "image") for item in targets):
            self._schedule_transform_commit(layer.id)

    def _crop_field_changed(self, key: str, value: float) -> None:
        layer = self.preview.selected_layer()
        if layer is None:
            return
        before = self._layout_snapshot()
        left, top, right, bottom = layer.crop
        span = PreviewWidget.MIN_CROP_SPAN
        if key == "l":
            left = clamp(value, 0.0, 1.0 - right - span)
        elif key == "t":
            top = clamp(value, 0.0, 1.0 - bottom - span)
        elif key == "r":
            right = clamp(value, 0.0, 1.0 - left - span)
        else:
            bottom = clamp(value, 0.0, 1.0 - top - span)
        layer.crop = (left, top, right, bottom)
        self.preview.update()
        self._sync_transform_fields()
        self._record_history(before)
        self._schedule_transform_commit(layer.id)

    def set_layer_region(self, layer_id: str, x: float, y: float, w: float, h: float) -> bool:
        """Show only a region of a source: ``x, y, w, h`` as fractions of its picture.

        This is the screen-region share: pick the screen or window once, then
        keep just the part you want (a terminal, a slide, one monitor corner).
        It is the crop, expressed as the window to keep.
        """
        layer = self.scene.layer(layer_id)
        if layer is None:
            return False
        x, y = clamp(x, 0.0, 1.0), clamp(y, 0.0, 1.0)
        w = clamp(w, PreviewWidget.MIN_CROP_SPAN, 1.0 - x)
        h = clamp(h, PreviewWidget.MIN_CROP_SPAN, 1.0 - y)
        before = self._layout_snapshot()
        layer.crop = (x, y, clamp(1.0 - x - w), clamp(1.0 - y - h))
        self._record_history(before)
        self._sync_transform_fields()
        self._preview_edit_finished(layer.id, True)
        return True

    def select_screen_region(self, layer_id: str = "") -> bool:
        """Start choosing the region of a screen share by dragging its crop window."""
        layer = self.scene.layer(layer_id or self.preview.selected_id)
        if layer is None or layer.kind not in ("screen", "video", "network", "camera"):
            return False
        self.preview.select_layer(layer.id)
        self._set_edit_mode("crop")
        self._status("Drag the crop window over the part of the picture to keep, "
                     "or use Reset crop to show it all")
        return True

    def _schedule_transform_commit(self, layer_id: str) -> None:
        if self.engine is None:
            return
        self._transform_pending = layer_id
        self._transform_timer.start()

    def _commit_transform(self) -> None:
        layer_id = self._transform_pending
        self._transform_pending = ""
        if not layer_id or self.engine is None:
            return
        if self.scene.layer(layer_id) is None:
            return
        self.restart_engine()

    def _opacity_changed(self, value: int) -> None:
        layer = self.preview.selected_layer()
        if layer is None:
            return
        if self._opacity_before is None:
            self._opacity_before = self._layout_snapshot()
        targets = self.preview.selected_layers() or [layer]
        for item in targets:
            item.alpha = clamp(value / 100.0)
            if self.engine is not None:
                self.engine.set_layer_alpha(item.id, item.alpha)
        self.opacity_label.setText(f"{value}%")
        self.preview.update()
        if not self.opacity_slider.isSliderDown():
            self._finish_opacity_edit()

    def _set_edit_mode(self, mode: str) -> None:
        self.preview.set_edit_mode(mode)
        self.move_button.setChecked(mode == "move")
        self.crop_button.setChecked(mode == "crop")
        wanted = self.preview.selected_id if mode == "crop" else ""
        if wanted != self._crop_preview_layer_id:
            self._crop_preview_layer_id = wanted
            if self.engine is not None:
                self.restart_engine()
        if mode == "crop":
            self.transform_hint.setText(
                "Drag inside the crop window to pan it; drag an edge or corner "
                "handle to trim the source.")
            self._status("Crop mode: drag the handles on the selected layer")
        else:
            self.transform_hint.setText(
                "Select a layer, then drag it in the preview. Drag an edge or corner "
                "handle to resize it.")
            self._status("Move mode: drag the selected layer or its handles")

    def _reset_crop(self) -> None:
        layer = self.preview.selected_layer()
        if layer is None:
            self._status("Select a layer first")
            return
        before = self._layout_snapshot()
        layer.crop = (0.0, 0.0, 0.0, 0.0)
        self.preview.update()
        self._sync_transform_fields()
        self._record_history(before)
        if self.engine is not None:
            self.restart_engine()
        self._status(f"Reset crop on {layer.name}")

    def _preview_geometry(self, layer_id: str) -> None:
        layer = self.scene.layer(layer_id)
        if layer is None:
            return
        if self.engine is not None and self.preview.edit_mode == "move":
            self.engine.set_layer_box(layer.id, layer.box)
        if self.preview.edit_mode == "crop":
            left, top, right, bottom = layer.crop
            self._status(f"{layer.name} crop  L {left:.3f}  T {top:.3f}  "
                         f"R {right:.3f}  B {bottom:.3f}")
        else:
            x, y, w, h = layer.box
            self._status(f"{layer.name}  x {x:.3f}  y {y:.3f}  w {w:.3f}  h {h:.3f}")
        self._sync_transform_fields()

    def _preview_edit_finished(self, layer_id: str, rebuild: bool) -> None:
        self._commit_layout_edit()
        layer = self.scene.layer(layer_id)
        if layer is None:
            return
        if rebuild and self.engine is not None:
            self.restart_engine()
        if rebuild:
            self._status(f"Updated {layer.name}")
        else:
            x, y, w, h = layer.box
            self._status(f"{layer.name}  x {x:.3f}  y {y:.3f}  w {w:.3f}  h {h:.3f}")
        self._sync_transform_fields()

    # ------------------------------------------------------- precise placement
    def _snap_toggled(self, enabled: bool) -> None:
        self.preview.set_snap_enabled(enabled)
        self.project.editor.snap_enabled = bool(enabled)
        self._mark_dirty()

    def _snap_threshold_changed(self, pixels: int) -> None:
        self.preview.set_snap_threshold(float(pixels))
        self.project.editor.snap_px = float(pixels)
        self._mark_dirty()

    def _sync_editor_controls(self) -> None:
        self.preview.set_snap_enabled(self.project.editor.snap_enabled)
        self.preview.set_snap_threshold(self.project.editor.snap_px)
        for widget, value in ((self.snap_check, self.project.editor.snap_enabled),
                              (self.snap_threshold,
                               int(round(self.project.editor.snap_px)))):
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            else:
                widget.setValue(int(value))
            widget.blockSignals(False)

    @staticmethod
    def _even(value: int) -> int:
        """Round down to an even size (H.264 cannot encode odd frames)."""
        return max(2, int(value) - int(value) % 2)

    def _preview_edit_started(self, _layer_id: str) -> None:
        if self._layout_before_edit is None:
            self._layout_before_edit = self._layout_snapshot()

    def _commit_layout_edit(self) -> None:
        before = self._layout_before_edit
        self._layout_before_edit = None
        self._record_history(before)

    def _begin_opacity_edit(self) -> None:
        if self._opacity_before is None:
            self._opacity_before = self._layout_snapshot()

    def _finish_opacity_edit(self) -> None:
        before = self._opacity_before
        self._opacity_before = None
        self._record_history(before)
