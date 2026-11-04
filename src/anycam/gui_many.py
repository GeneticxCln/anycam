"""ManyCam-inspired GUI layout for AnyCam.

The window keeps three things on screen: the program monitor, a numbered
preset deck, and a sidebar with layouts, effects and scenes.  Everything else
(sources, audio, playlist, output) lives in a separate settings window opened
from the corner button, so the main window is only about composing.

An empty scene shows its layout as empty slots.  Each empty slot is a dashed
``+`` button over the monitor; clicking it assigns a camera, screen, video,
image, phone URL or test pattern to that slot.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import config
from .gui import AnyCamWindow, PreviewWidget
from .layouts import GRID_KINDS, LAYOUT_PRESETS, layout_preset
from .model import Layer, Project, Scene, empty_project, new_id


def _layout_pixmap(preset, width: int = 72, height: int = 42) -> QPixmap:
    """Draw a tiny preview of a layout: the slot rectangles."""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#0b0d10"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    for index, (x, y, w, h) in enumerate(preset.boxes):
        painter.setBrush(QColor("#5eead4") if index == 0 else QColor("#31424c"))
        painter.drawRoundedRect(
            QRectF(x * width + 2, y * height + 2,
                   max(2.0, w * width - 4), max(2.0, h * height - 4)), 3, 3)
    painter.end()
    return pixmap


def _scene_pixmap(scene, width: int = 84, height: int = 48) -> QPixmap:
    """Composition thumbnail for a preset: its boxes, coloured by kind."""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#0b0d10"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    palette = {
        "camera": QColor("#5eead4"),
        "screen": QColor("#7aa2f7"),
        "network": QColor("#f6c177"),
        "video": QColor("#bb9af7"),
        "test": QColor("#9ece6a"),
        "image": QColor("#7dcfff"),
        "text": QColor("#d7e3ea"),
        "logo": QColor("#d7e3ea"),
    }
    for layer in sorted(scene.layers, key=lambda item: item.z):
        if not layer.visible:
            continue
        x, y, w, h = layer.box
        rect = QRectF(x * width + 1, y * height + 1,
                      max(2.0, w * width - 2), max(2.0, h * height - 2))
        if layer.kind in ("text", "logo"):
            rect.setWidth(max(2.0, rect.width() * 0.6))
            rect.setHeight(max(1.0, rect.height() * 0.5))
        painter.setBrush(palette.get(layer.kind, QColor("#31424c")))
        painter.drawRoundedRect(rect, 2, 2)
    painter.end()
    return pixmap


def _is_legacy_default(project: Project) -> bool:
    """The old built-in starter (camera + AnyCam/LIVE titles), untouched.

    Only that exact placeholder is replaced with the empty single layout;
    a project the user edited is loaded as-is.
    """
    if len(project.scenes) != 1:
        return False
    scene = project.scenes[0]
    if scene.name != "Camera" or len(scene.layers) != 3:
        return False
    kinds = [layer.kind for layer in scene.layers]
    return (kinds == ["camera", "text", "text"]
            and not scene.layers[0].source
            and scene.layers[1].source == "AnyCam"
            and scene.layers[2].source == "LIVE")


class ManyCamWindow(AnyCamWindow):
    # ------------------------------------------------------------------- ui
    def _load_saved_project(self) -> Project:
        path = config.project_path()
        if not path.exists():
            return empty_project()
        project = super()._load_saved_project()
        if _is_legacy_default(project):
            return empty_project()
        return project

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)

        # ---- top bar -------------------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(8)
        logo_path = config.assets_dir() / "anycam-logo.png"
        if logo_path.exists():
            logo = QLabel()
            logo.setPixmap(QPixmap(str(logo_path)).scaled(
                28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            header.addWidget(logo)
        title = QLabel("AnyCam")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        self.scene_combo = QComboBox()
        self.scene_combo.setVisible(False)
        header.addWidget(self.scene_combo)
        self.settings_button = QPushButton("Settings")
        self.settings_button.setToolTip("Sources, audio, playlist and output")
        self.settings_button.clicked.connect(lambda: self._open_settings())
        header.addWidget(self.settings_button)
        self.status_chip = QLabel("STOPPED")
        self.status_chip.setObjectName("accent")
        header.addWidget(self.status_chip)
        outer.addLayout(header)

        self.scene_combo.currentIndexChanged.connect(self._scene_changed)
        self._refresh_scenes()

        # ---- main area: program monitor (left) + side panel (right) -------
        body = QHBoxLayout()
        body.setSpacing(6)
        outer.addLayout(body, 1)

        preview_panel = QFrame()
        preview_panel.setObjectName("panel")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(6, 6, 6, 6)

        monitor_header = QHBoxLayout()
        program = QLabel("Program")
        program.setObjectName("muted")
        monitor_header.addWidget(program)
        monitor_header.addStretch(1)
        self._syncing_format = False
        self._format_sizes = [(1280, 720), (1920, 1080),
                              (2560, 1440), (3840, 2160)]
        self.resolution_combo = QComboBox()
        for index, (width, height) in enumerate(self._format_sizes):
            self.resolution_combo.addItem(f"{width}×{height}", index)
        self.fps_combo = QComboBox()
        for rate in (30, 60):
            self.fps_combo.addItem(f"{rate} fps", rate)
        self._sync_output_controls()
        monitor_header.addWidget(self.resolution_combo)
        monitor_header.addWidget(self.fps_combo)
        self.resolution_combo.currentIndexChanged.connect(self._format_changed)
        self.fps_combo.currentIndexChanged.connect(self._format_changed)
        preview_layout.addLayout(monitor_header)

        self.preview = PreviewWidget()
        self.preview.set_snap_enabled(self.project.editor.snap_enabled)
        self.preview.set_snap_threshold(self.project.editor.snap_px)
        self.preview.set_scene(self.scene)
        self.preview.set_selection_auto_hide(True)
        self.preview.selection_changed.connect(self._preview_selection)
        self.preview.context_menu_requested.connect(self._preview_context_menu)
        self.preview.geometry_changed.connect(self._preview_geometry)
        self.preview.edit_started.connect(self._preview_edit_started)
        self.preview.edit_finished.connect(self._preview_edit_finished)
        self.preview.installEventFilter(self)
        self.stage_add_buttons: list[QPushButton] = []
        self._stage_slots: list[tuple[QPushButton, tuple]] = []
        self._stage_canvas: tuple | None = None
        preview_layout.addWidget(self.preview, 1)

        controls = QHBoxLayout()
        self.start_button = QPushButton("Start preview")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.toggle_engine)
        self.record_button = QPushButton("Record")
        self.record_button.clicked.connect(self.toggle_record)
        self.vcam_button = QPushButton("Virtual cam")
        self.vcam_button.clicked.connect(self.toggle_vcam)
        self.snapshot_button = QPushButton("Snapshot")
        self.snapshot_button.clicked.connect(self.snapshot)
        self.multiview_button = QPushButton("Multi-view")
        self.multiview_button.clicked.connect(self.toggle_multiview)
        for button in (self.start_button, self.record_button, self.vcam_button,
                       self.snapshot_button, self.multiview_button):
            controls.addWidget(button)
        controls.addStretch(1)
        self.stats_label = QLabel("idle")
        self.stats_label.setObjectName("muted")
        controls.addWidget(self.stats_label)
        preview_layout.addLayout(controls)
        body.addWidget(preview_panel, 3)

        side = QTabWidget()
        side.setMinimumWidth(320)
        side.setMaximumWidth(380)
        side.addTab(self._layouts_tab(), "Layouts")
        side.addTab(self._effects_tab(), "Effects")
        side.addTab(self._scene_tab(), "Scenes")
        self.side_tabs = side
        body.addWidget(side, 1)

        # ---- numbered preset deck, directly under the monitor -------------
        deck = QFrame()
        deck.setObjectName("panel")
        deck_layout = QHBoxLayout(deck)
        deck_layout.setContentsMargins(6, 4, 6, 4)
        deck_layout.setSpacing(6)
        preset_label = QLabel("Presets")
        preset_label.setObjectName("muted")
        deck_layout.addWidget(preset_label)
        self.scene_cards = QHBoxLayout()
        self.scene_cards.setSpacing(4)
        scene_box = QWidget()
        scene_box.setLayout(self.scene_cards)
        deck_layout.addWidget(scene_box, 1)
        add_preset = QPushButton("+")
        add_preset.setFixedWidth(32)
        add_preset.setToolTip("New empty preset")
        add_preset.clicked.connect(self.add_scene)
        deck_layout.addWidget(add_preset)
        outer.addWidget(deck)
        self._refresh_scene_cards()

        self.setStatusBar(QStatusBar())
        if self._load_warning:
            self._status(self._load_warning)
        else:
            self._status("Ready. No virtual camera output yet.")

        # Settings is a separate window; build it once, keep it hidden.
        self._settings: QDialog | None = None
        self._settings = self._build_settings()
        self._refresh_stage_slots()

    # --------------------------------------------------------------- settings
    def _build_settings(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle("AnyCam settings")
        dialog.resize(520, 680)
        tabs = QTabWidget(dialog)
        tabs.addTab(self._layers_tab(), "Sources")
        tabs.addTab(self._audio_tab(), "Audio")
        tabs.addTab(self._playlist_tab(), "Media")
        tabs.addTab(self._output_tab(), "Output")
        tabs.addTab(self._stream_tab(), "Stream")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(tabs)
        self.settings_tabs = tabs
        return dialog

    def _open_settings(self, tab: int = 0) -> None:
        dialog = self._settings
        if dialog is None:
            dialog = self._settings = self._build_settings()
        if not dialog.isVisible():
            dialog.move(self.x() + self.width() - dialog.width() - 24,
                        self.y() + 64)
        if 0 <= tab < self.settings_tabs.count():
            self.settings_tabs.setCurrentIndex(tab)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    # ---------------------------------------------------------------- scenes
    def _refresh_scenes(self) -> None:
        super()._refresh_scenes()
        self._refresh_scene_cards()

    def _refresh_scene_cards(self) -> None:
        if not hasattr(self, "scene_cards"):
            return
        while self.scene_cards.count():
            widget = self.scene_cards.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for index, scene in enumerate(self.project.scenes, start=1):
            button = QPushButton(f"{index}  {scene.name}")
            button.setObjectName("preset")
            button.setIcon(QIcon(_scene_pixmap(scene)))
            button.setIconSize(QSize(72, 40))
            button.setCheckable(True)
            button.setChecked(scene.id == self.project.active_scene)
            button.clicked.connect(
                lambda _checked=False, sid=scene.id: self._switch_scene_card(sid))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda pos, sid=scene.id, btn=button:
                self._scene_context_menu(sid, btn.mapToGlobal(pos)))
            self.scene_cards.addWidget(button)

    def _scene_context_menu(self, scene_id: str, position) -> None:
        menu = QMenu(self)
        rename = menu.addAction("Rename…")
        remove = menu.addAction("Remove")
        remove.setEnabled(len(self.project.scenes) > 1)
        chosen = menu.exec(position)
        if chosen is rename:
            self.rename_scene(scene_id)
        elif chosen is remove:
            self.remove_scene(scene_id)

    def add_scene(self) -> None:
        """Add a new preset that starts as an empty single layout."""
        scene = Scene(new_id("scene"), f"Scene {len(self.project.scenes) + 1}",
                      layout="full")
        self._append_scene(scene)

    def _scene_changed(self, index: int) -> None:
        super()._scene_changed(index)
        self._refresh_scene_cards()
        self._sync_layout_controls()
        self._refresh_stage_slots()

    def _switch_scene_card(self, scene_id: str) -> None:
        index = self.scene_combo.findData(scene_id)
        if index >= 0 and index != self.scene_combo.currentIndex():
            self.scene_combo.setCurrentIndex(index)

    # ---------------------------------------------------------------- layers
    def _preview_menu(self, layer_id: str) -> QMenu:
        """Canvas right-click menu for the layer under the pointer."""
        menu = QMenu(self)
        layer = self.scene.layer(layer_id) if layer_id else None
        if layer is not None:
            action = menu.addAction(f"Remove {layer.name}")
            action.setData(layer.id)
        return menu

    def _preview_context_menu(self, layer_id: str, position) -> None:
        menu = self._preview_menu(layer_id)
        if not menu.actions():
            return
        chosen = menu.exec(position)
        if chosen is not None and chosen.data():
            self.remove_layers([str(chosen.data())])

    def _refresh_layers(self) -> None:
        super()._refresh_layers()
        self._refresh_stage_slots()

    # ---------------------------------------------------------------- layout
    def _layouts_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel(
            "Choose a layout, then click a + on the canvas to fill each slot. "
            "Titles and logos are not part of a layout.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._current_layout_key = self.scene.layout or "full"
        grid = QGridLayout()
        grid.setSpacing(6)
        self.layout_buttons: dict[str, QToolButton] = {}
        self._layout_group = QButtonGroup(page)
        self._layout_group.setExclusive(True)
        for index, preset in enumerate(LAYOUT_PRESETS):
            button = QToolButton()
            button.setCheckable(True)
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIcon(QIcon(_layout_pixmap(preset)))
            button.setIconSize(QSize(72, 42))
            button.setText(preset.short)
            button.setToolTip(preset.label)
            button.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Fixed)
            self._layout_group.addButton(button, index)
            self.layout_buttons[preset.key] = button
            grid.addWidget(button, index // 2, index % 2)
        layout.addLayout(grid)
        layout.addStretch(1)
        self.layout_buttons[self._current_layout_key].setChecked(True)
        self._layout_group.idClicked.connect(self._layout_clicked)
        return page

    def _layout_clicked(self, index: int) -> None:
        if 0 <= index < len(LAYOUT_PRESETS):
            self.apply_layout(LAYOUT_PRESETS[index].key)

    def _active_layout(self):
        return (layout_preset(self.scene.layout)
                or layout_preset(getattr(self, "_current_layout_key", "full"))
                or LAYOUT_PRESETS[0])

    def _sync_layout_controls(self) -> None:
        if not hasattr(self, "layout_buttons"):
            return
        if self.scene.layout:
            self._current_layout_key = self.scene.layout
        button = self.layout_buttons.get(self._current_layout_key)
        if button is not None:
            button.setChecked(True)

    def _slot_layers(self, preset) -> dict[int, Layer]:
        """Which layer occupies each slot of ``preset`` right now."""
        wanted = {tuple(round(value, 6) for value in box): index
                  for index, box in enumerate(preset.boxes)}
        slots: dict[int, Layer] = {}
        for layer in sorted(self.scene.layers, key=lambda item: item.z):
            if not layer.visible or layer.kind not in GRID_KINDS:
                continue
            index = wanted.get(tuple(round(value, 6) for value in layer.box))
            if index is not None and index not in slots:
                slots[index] = layer
        return slots

    def _slot_context_menu(self, button: QPushButton, position) -> None:
        """Right-click on a + overlay: forward it to the layer underneath.

        The + button covers its whole empty slot, so without this a floating
        source below it could not be right-clicked or removed.
        """
        local = QPointF(button.mapTo(self.preview, position))
        layer = self.preview.layer_at(local)
        if layer is not None:
            self._preview_context_menu(layer.id, button.mapToGlobal(position))
            return
        for candidate, box in self._stage_slots:
            if candidate is button:
                preset = self._active_layout()
                try:
                    index = list(preset.boxes).index(box)
                except ValueError:
                    index = -1
                self._slot_menu(index, button)
                return

    def _slot_menu(self, index: int, button: QPushButton) -> None:
        preset = self._active_layout()
        if not (0 <= index < len(preset.boxes)):
            return
        box = preset.boxes[index]
        menu = QMenu(self)
        for layer in sorted(self.scene.layers, key=lambda item: item.z):
            if layer.kind not in GRID_KINDS or not layer.visible:
                continue
            action = menu.addAction(f"Use {layer.name}")
            action.setData(layer.id)
        if menu.actions():
            menu.addSeparator()
        for label, kind in (("Add camera…", "camera"),
                            ("Add video…", "video"),
                            ("Add image…", "image"),
                            ("Add phone URL…", "network"),
                            ("Add test pattern", "test")):
            action = menu.addAction(label)
            action.setData(f"add:{kind}")

        screen = menu.addAction("Share desktop screen or window…")
        screen.setData("add:screen")
        share_audio = menu.addMenu("Share desktop audio")
        monitors = [source for source in config.list_audio_sources()
                    if source.is_monitor]
        if not monitors:
            action = share_audio.addAction("No monitor source found")
            action.setEnabled(False)
        for source in monitors:
            action = share_audio.addAction(source.description or source.name)
            action.setData(f"desktop:{source.name}")
        if self.project.audio.desktop_device:
            share_audio.addSeparator()
            action = share_audio.addAction("Stop desktop audio")
            action.setData("desktop:")

        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        data = str(chosen.data() or "")
        if data.startswith("desktop:"):
            self._share_desktop_audio(data.split(":", 1)[1])
            return
        if data.startswith("add:"):
            layer = self.add_layer(data.split(":", 1)[1], box=box)
            if layer is not None:
                self.scene.layout = preset.key
                self._current_layout_key = preset.key
                self._refresh_scene_cards()
                if self.engine is None and self.isVisible():
                    self.toggle_engine()
            return
        layer = self.scene.layer(data)
        if layer is not None:
            self._assign_slot(index, layer)

    def _share_desktop_audio(self, device: str) -> None:
        combo = getattr(self, "desktop_combo", None)
        if combo is not None:
            index = combo.findData(device)
            if index < 0 and device:
                combo.addItem(device, device)
                index = combo.count() - 1
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)
        self.project.audio.desktop_device = device
        self.project.audio.desktop_muted = False
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        self._status("Desktop audio off" if not device
                     else f"Desktop audio: {device}")

    def _assign_slot(self, index: int, layer: Layer) -> None:
        """Put ``layer`` in slot ``index``, swapping out the current occupant."""
        preset = self._active_layout()
        if not (0 <= index < len(preset.boxes)):
            return
        before = self._layout_snapshot()
        previous = layer.box
        occupant = self._slot_layers(preset).get(index)
        layer.box = preset.boxes[index]
        layer.visible = True
        if occupant is not None and occupant is not layer:
            occupant.box = previous
        self.scene.layout = preset.key
        self._current_layout_key = preset.key
        self._refresh_layers()
        self._record_history(before)
        self._refresh_scene_cards()
        if self.engine is not None:
            self.restart_engine()
        elif self.isVisible():
            self.toggle_engine()
        self._status(f"{layer.name} → slot {index + 1}")

    def apply_layout(self, key: str = "") -> list[Layer]:
        """Apply a layout, select it, and remember it on the preset."""
        key = key or "full"
        arranged = super().apply_layout(key)
        self._current_layout_key = key
        button = self.layout_buttons.get(key)
        if button is not None:
            button.setChecked(True)
        self._refresh_stage_slots()
        self._refresh_scene_cards()
        return arranged

    # ----------------------------------------------------------- canvas slots
    def _refresh_stage_slots(self) -> None:
        """One dashed + over every empty slot of the active layout."""
        for button, _box in getattr(self, "_stage_slots", []):
            button.deleteLater()
        self._stage_slots = []
        self.stage_add_buttons = []
        self._stage_canvas = None
        if not hasattr(self, "preview"):
            return
        preset = self._active_layout()
        assigned = self._slot_layers(preset)
        for index, box in enumerate(preset.boxes):
            if index in assigned:
                continue
            button = QPushButton("+", self.preview)
            button.setObjectName("slotAdd")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip("Add a source to this slot")
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda pos, btn=button: self._slot_context_menu(btn, pos))
            button.clicked.connect(
                lambda _=False, i=index, btn=button: self._slot_menu(i, btn))
            button.show()
            self._stage_slots.append((button, box))
            self.stage_add_buttons.append(button)
        self._position_stage_slots(force=True)

    def _position_stage_slots(self, force: bool = False) -> None:
        if not hasattr(self, "preview"):
            return
        canvas = self.preview.canvas_rect()
        if canvas.isEmpty() or canvas.width() < 2 or canvas.height() < 2:
            canvas = QRectF(0, 0, max(1, self.preview.width()),
                            max(1, self.preview.height()))
        key = (round(canvas.x(), 1), round(canvas.y(), 1),
               round(canvas.width(), 1), round(canvas.height(), 1))
        if not force and key == self._stage_canvas:
            return
        self._stage_canvas = key
        for button, box in self._stage_slots:
            x, y, w, h = box
            rect = QRectF(canvas.x() + x * canvas.width(),
                          canvas.y() + y * canvas.height(),
                          w * canvas.width(), h * canvas.height()).toRect()
            rect = rect.adjusted(4, 4, -4, -4)
            if rect.width() < 18 or rect.height() < 18:
                button.hide()
                continue
            button.setGeometry(rect)
            button.show()
            button.raise_()

    def eventFilter(self, watched, event):
        if watched is getattr(self, "preview", None) \
                and event.type() == QEvent.Type.Resize:
            self._position_stage_slots()
        return super().eventFilter(watched, event)

    def _tick(self) -> None:
        super()._tick()
        self._position_stage_slots()

    # ---------------------------------------------------------------- format
    def _format_changed(self, _index: int = 0) -> None:
        if self._syncing_format:
            return
        index = self.resolution_combo.currentIndex()
        sizes = getattr(self, "_format_sizes", [])
        if not 0 <= index < len(sizes):
            return
        size = sizes[index]
        rate = self.fps_combo.currentData()
        if not rate:
            return
        self.set_output_format(int(size[0]), int(size[1]), int(rate))

    def _sync_output_controls(self) -> None:
        if not hasattr(self, "resolution_combo"):
            return
        self._syncing_format = True
        try:
            size = (self.project.output.canvas_w, self.project.output.canvas_h)
            sizes = getattr(self, "_format_sizes", None)
            if sizes is None:
                return
            if size not in sizes:
                sizes.append(size)
                self.resolution_combo.addItem(
                    f"{size[0]}×{size[1]}", len(sizes) - 1)
            self.resolution_combo.setCurrentIndex(sizes.index(size))
            rate = self.project.output.fps
            fps_index = self.fps_combo.findData(rate)
            if fps_index < 0:
                self.fps_combo.addItem(f"{rate} fps", rate)
                fps_index = self.fps_combo.count() - 1
            self.fps_combo.setCurrentIndex(fps_index)
        finally:
            self._syncing_format = False

    # --------------------------------------------------------------- runtime
    def _autostart_preview(self) -> None:
        # An empty preset stays stopped until the first source is chosen.
        if self.engine is None and self.isVisible() and self.scene.layers:
            self.toggle_engine()


def run_gui(project: Project | None = None) -> int:
    app = QApplication(sys.argv[:1])
    window = ManyCamWindow(project)
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    QTimer.singleShot(200, window._autostart_preview)
    code = app.exec()
    os._exit(code if code is not None else 0)


def run_screenshot(out: str, seconds: float, project: Project | None = None,
                   source: str = "") -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if project is None and source:
        from .demo import build_demo_project
        project = build_demo_project(source, config.CANVAS_W, config.CANVAS_H,
                                     config.CANVAS_FPS)
    app = QApplication(sys.argv[:1])
    window = ManyCamWindow(project)
    window.headless = True
    window.auto_screenshot_path = out
    window._render_seconds = max(0.1, seconds)
    window._screenshot_done = False
    window.resize(1440, 900)
    app.aboutToQuit.connect(window.shutdown)
    QTimer.singleShot(int((window._render_seconds + 5.0) * 1000),
                      window._screenshot_deadline)
    window.show()
    window.start_engine()
    app.exec()
    window.shutdown()
    status = 0 if (window.screenshot_ok and Path(out).exists()
                   and Path(out).stat().st_size > 0) else 1
    os._exit(status)
