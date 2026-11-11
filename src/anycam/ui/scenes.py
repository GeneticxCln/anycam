"""Scenes: switching, adding, renaming, layouts."""
from __future__ import annotations



from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .. import config
from ..engine import transitions
from ..layouts import GRID_KINDS, arrange, layout_preset
from ..model import (Layer, Scene, camera_grid_scene,
                    new_id, two_camera_scene)
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class SceneMixin:
    """Scenes: switching, adding, renaming, layouts."""

    def switch_scene(self, index: int) -> None:
        combo = getattr(self, "scene_combo", None)
        if (0 <= index < len(self.project.scenes) and combo is not None
                and combo.count() == len(self.project.scenes)):
            # The combo's handler also syncs the layout picker and scene cards
            # that the shortcut path used to leave stale.
            if combo.currentIndex() != index:
                combo.setCurrentIndex(index)
            self._status(f"Scene: {self.project.scenes[index].name}")
            return
        if 0 <= index < len(self.project.scenes):
            self._store_playlist_state()
            self._forget_lost_cameras()
            self.project.active_scene = self.project.scenes[index].id
            self._clear_history()
            self._refresh_scenes()
            self._refresh_scene_list()
            self._refresh_layers()
            self._load_playlist_state()
            self._mark_dirty()
            self._status(f"Scene: {self.project.scenes[index].name}")
            self._show_scene_live()

    # -------------------------------------------------------------- project
    @property
    def scene(self) -> Scene:
        return self.project.scene

    def _scene_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.scene_list = QListWidget()
        self.scene_list.currentRowChanged.connect(self._scene_row_changed)
        layout.addWidget(self.scene_list, 1)
        row = QHBoxLayout()
        add = QPushButton("Add scene")
        add.clicked.connect(self.add_scene)
        remove = QPushButton("Remove")
        remove.setObjectName("danger")
        remove.clicked.connect(self.remove_scene)
        row.addWidget(add)
        row.addWidget(remove)
        layout.addLayout(row)
        arrive = QHBoxLayout()
        arrive.addWidget(QLabel("Arrives with"))
        self.transition_combo = QComboBox()
        for kind in transitions.TRANSITIONS:
            self.transition_combo.addItem(transitions.LABELS[kind], kind)
        self.transition_combo.setToolTip(
            "How the selected scene appears when you switch to it")
        self.transition_combo.currentIndexChanged.connect(self._scene_transition_changed)
        self.transition_ms = QSpinBox()
        self.transition_ms.setRange(100, 5000)
        self.transition_ms.setSingleStep(100)
        self.transition_ms.setSuffix(" ms")
        self.transition_ms.setKeyboardTracking(False)
        self.transition_ms.valueChanged.connect(self._scene_transition_changed)
        arrive.addWidget(self.transition_combo, 1)
        arrive.addWidget(self.transition_ms)
        layout.addLayout(arrive)
        self._sync_transition_controls()
        self._refresh_scene_list()
        return page

    # ---------------------------------------------------------- studio mode
    def _build_studio_controls(self, header: QHBoxLayout) -> None:
        """Stage a scene, then take it: CUT is instant, TRANS uses its transition."""
        self.studio_check = QCheckBox("Studio")
        self.studio_check.setToolTip(
            "Stage the next scene, then take it live with Cut or Transition")
        self.studio_check.toggled.connect(self._studio_toggled)
        self.next_scene_combo = QComboBox()
        self.next_scene_combo.setToolTip("The scene that goes live next")
        self.cut_button = QPushButton("Cut")
        self.cut_button.clicked.connect(lambda: self.take_next_scene(cut=True))
        self.trans_button = QPushButton("Transition")
        self.trans_button.clicked.connect(lambda: self.take_next_scene(cut=False))
        header.addWidget(self.studio_check)
        for widget in (self.next_scene_combo, self.cut_button, self.trans_button):
            header.addWidget(widget)
        self._studio_toggled(False)

    def _studio_toggled(self, on: bool) -> None:
        for widget in (self.next_scene_combo, self.cut_button, self.trans_button):
            widget.setVisible(bool(on))
        if on:
            self._refresh_next_scene()

    def _refresh_next_scene(self) -> None:
        combo = getattr(self, "next_scene_combo", None)
        if combo is None:
            return
        combo.blockSignals(True)
        wanted = combo.currentData()
        combo.clear()
        for scene in self.project.scenes:
            if scene.id != self.project.active_scene:
                combo.addItem(scene.name, scene.id)
        index = combo.findData(wanted)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)
        for button in (self.cut_button, self.trans_button):
            button.setEnabled(combo.count() > 0)

    def take_next_scene(self, cut: bool = False) -> bool:
        """Put the staged scene live; ``cut`` ignores its transition."""
        scene_id = self.next_scene_combo.currentData() if hasattr(self, "next_scene_combo") else None
        index = next((i for i, scene in enumerate(self.project.scenes) if scene.id == scene_id), -1)
        if index < 0:
            return False
        target = self.project.scenes[index]
        self._take_override = ("cut", 0.0) if cut else (
            (target.transition, transitions.transition_seconds(target.transition_ms))
            if target.transition != "cut" else ("fade", 0.5))
        try:
            self.switch_scene(index)
        finally:
            self._take_override = None
        return True

    def _sync_transition_controls(self) -> None:
        if not hasattr(self, "transition_combo"):
            return
        scene = self.scene
        self.transition_combo.blockSignals(True)
        self.transition_ms.blockSignals(True)
        index = self.transition_combo.findData(transitions.transition_kind(scene.transition))
        self.transition_combo.setCurrentIndex(max(0, index))
        self.transition_ms.setValue(int(scene.transition_ms))
        self.transition_ms.setEnabled(scene.transition != "cut")
        self.transition_combo.blockSignals(False)
        self.transition_ms.blockSignals(False)

    def _scene_transition_changed(self, _value=None) -> None:
        scene = self.scene
        scene.transition = str(self.transition_combo.currentData() or "cut")
        scene.transition_ms = int(self.transition_ms.value())
        self.transition_ms.setEnabled(scene.transition != "cut")
        self._mark_dirty()

    def _refresh_scenes(self) -> None:
        self._sync_transition_controls()
        self._refresh_next_scene()
        self.scene_combo.blockSignals(True)
        self.scene_combo.clear()
        for scene in self.project.scenes:
            self.scene_combo.addItem(scene.name, scene.id)
        index = self.scene_combo.findData(self.project.active_scene)
        self.scene_combo.setCurrentIndex(max(0, index))
        self.scene_combo.blockSignals(False)

    def _refresh_scene_list(self) -> None:
        if not hasattr(self, "scene_list"):
            return
        self.scene_list.blockSignals(True)
        self.scene_list.clear()
        for scene in self.project.scenes:
            item = QListWidgetItem(f"{scene.name}  ·  {len(scene.layers)} layers")
            item.setData(Qt.ItemDataRole.UserRole, scene.id)
            self.scene_list.addItem(item)
            if scene.id == self.project.active_scene:
                self.scene_list.setCurrentItem(item)
        self.scene_list.blockSignals(False)

    def _sync_layout_combo(self) -> None:
        """Show the preset's saved layout in the classic layout combo."""
        combo = getattr(self, "layout_combo", None)
        if combo is None:
            return
        index = combo.findData(self.scene.layout)
        if index < 0:
            return
        combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _scene_changed(self, _index: int) -> None:
        scene_id = self.scene_combo.currentData()
        if not scene_id or scene_id == self.project.active_scene:
            return
        self._store_playlist_state()
        self._forget_lost_cameras()
        self.project.active_scene = scene_id
        self._sync_layout_combo()
        self._clear_history()
        self._refresh_scene_list()
        self._refresh_layers()
        self._load_playlist_state()
        self._mark_dirty()
        self._show_scene_live()

    def _scene_row_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self.scene_list.item(row)
        if item is None:
            return
        scene_id = item.data(Qt.ItemDataRole.UserRole)
        if scene_id == self.project.active_scene:
            return
        self._store_playlist_state()
        self._forget_lost_cameras()
        self.project.active_scene = scene_id
        self._sync_layout_combo()
        self._clear_history()
        self._refresh_scenes()
        self._refresh_layers()
        self._load_playlist_state()
        self._mark_dirty()
        self._show_scene_live()

    def add_scene(self) -> None:
        scene = Scene(new_id("scene"), f"Scene {len(self.project.scenes) + 1}")
        scene.add_layer(Layer(new_id("layer"), "camera", "StreamCam",
                              source=config.camera_identity(config.camera_device()), z=1))
        self._append_scene(scene)

    def add_two_camera_scene(self) -> None:
        """Add the side-by-side preset using the first two camera devices."""
        self._refresh_cameras()
        devices = [device for _label, device in self._camera_choices]
        if len(devices) < 2:
            self._status("Two camera devices are required for this preset "
                         f"(found {len(devices)})")
            return
        self._append_scene(two_camera_scene(devices[0], devices[1]))
        self._status(f"Two-camera scene: {devices[0]} + {devices[1]}")

    def add_camera_grid_scene(self) -> None:
        """Ask for a camera per 2x2 cell, then add the grid as a new scene."""
        self._refresh_cameras()
        choices = list(self._camera_choices)
        if len(choices) < 3:
            self._status("Three camera devices are required for the grid preset "
                         f"(found {len(choices)})")
            return
        dialog = CameraGridDialog(choices, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        slots = dialog.slots()
        count = len([device for device in slots if device])
        names = {3: "Three cameras", 4: "Four cameras"}
        scene = camera_grid_scene(slots, name=names.get(count, "Camera grid"))
        self._append_scene(scene)
        used = ", ".join(device for device in slots if device)
        self._status(f"{count}-camera grid: {used}")

    def apply_layout(self, key: str = "") -> list[Layer]:
        """Tile the scene's visible capture layers into the chosen layout.

        Text and logo overlays keep their position.  Sources beyond the
        layout's cell count are left exactly as the user positioned them.
        """
        combo = getattr(self, "layout_combo", None)
        preset = layout_preset(key or (combo.currentData() if combo is not None else ""))
        if preset is None:
            return []
        sources = [layer for layer in self.scene.layers
                   if layer.visible and layer.kind in GRID_KINDS]
        before = self._layout_snapshot()
        arranged = arrange(self.scene, preset)
        self.scene.layout = preset.key
        self._refresh_layers()
        if arranged:
            self._record_history(before)
        if self.engine is not None and arranged:
            # A layout only repositions existing pads.  Rebuilding the graph
            # would tear down every capture (and the vcam tap) mid-stream,
            # which is what blanked Discord on a layout switch.  Push the new
            # boxes onto the running compositor instead.
            for layer in arranged:
                self.engine.set_layer_box(layer.id, layer.box)
            self.preview.update()
        if not arranged:
            self._status(f"Layout: {preset.label} · choose a source for each +")
            return arranged
        extra = len(sources) - len(arranged)
        note = f" · {extra} left unchanged" if extra else ""
        self._status(f"Layout: {preset.label} · {len(arranged)} sources{note}")
        return arranged

    def _append_scene(self, scene: Scene) -> None:
        self._store_playlist_state()
        self._forget_lost_cameras()
        self.project.scenes.append(scene)
        self.project.active_scene = scene.id
        self._clear_history()
        self._refresh_scenes()
        self._refresh_scene_list()
        self._refresh_layers()
        self._load_playlist_state()
        self._mark_dirty()
        self._show_scene_live()

    def duplicate_scene(self, scene_id: str = "") -> Scene | None:
        """Copy a scene (layers get new ids) and switch to the copy."""
        scene = next((s for s in self.project.scenes
                      if s.id == (scene_id or self.project.active_scene)), None)
        if scene is None:
            return None
        copy_ = Scene.from_dict(scene.to_dict())
        copy_.id = new_id("scene")
        copy_.name = f"{scene.name} copy"
        renamed = {}
        for layer in copy_.layers:
            renamed[layer.id] = layer.id = new_id("layer")
        copy_.playlist.layer_ids = [renamed.get(i, i) for i in copy_.playlist.layer_ids]
        self._append_scene(copy_)
        # Next to the original rather than at the end.
        index = next(i for i, item in enumerate(self.project.scenes) if item.id == scene.id)
        self.move_scene(copy_.id, index + 1 - (len(self.project.scenes) - 1))
        return copy_

    def move_scene(self, scene_id: str, delta: int) -> bool:
        """Move a scene ``delta`` places in the list (the order of Ctrl+1..9)."""
        scenes = self.project.scenes
        index = next((i for i, item in enumerate(scenes) if item.id == scene_id), -1)
        target = max(0, min(len(scenes) - 1, index + int(delta)))
        if index < 0 or target == index:
            return False
        scenes.insert(target, scenes.pop(index))
        self._refresh_scenes()
        self._refresh_scene_list()
        self._mark_dirty()
        self._status(f"Moved {scenes[target].name} to position {target + 1}")
        return True

    def rename_scene(self, scene_id: str = "") -> None:
        """Rename a preset from its context menu or the Scene list."""
        scene = next((s for s in self.project.scenes
                      if s.id == (scene_id or self.project.active_scene)), None)
        if scene is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Rename scene", "Scene name:", text=scene.name)
        if not accepted or not name.strip():
            return
        scene.name = name.strip()
        self._refresh_scenes()
        self._refresh_scene_list()
        self._mark_dirty()

    def remove_scene(self, scene_id: str = "") -> None:
        if len(self.project.scenes) <= 1:
            return
        scene_id = scene_id or self.project.active_scene
        removed = next((s for s in self.project.scenes if s.id == scene_id), None)
        if removed is None:
            return
        self.project.scenes = [s for s in self.project.scenes if s.id != scene_id]
        if self.project.active_scene not in [s.id for s in self.project.scenes]:
            self.project.active_scene = self.project.scenes[0].id
        self._forget_lost_cameras()
        self._clear_history()
        self._refresh_scenes()
        self._refresh_scene_list()
        self._refresh_layers()
        self._load_playlist_state()
        self._mark_dirty()
        if any(layer.kind == "screen" for layer in removed.layers):
            # A removed share must end with its graph, not stay detached.
            if self.engine is not None:
                self.restart_engine()
        else:
            self._show_scene_live()
        # The deleted scene's screen shares: end their portal captures once
        # the canvas no longer uses them (they used to run until exit).
        # _close_screen_session keeps a session another layer still shares.
        for layer in removed.layers:
            if layer.kind == "screen":
                self._close_screen_session(layer.id)

    def _show_scene_live(self) -> None:
        """Put the active scene on the running canvas; rebuild only if it cannot.

        A rebuild stops every source, the preview, the recording tap and the
        virtual camera's feed for seconds.  Swapping the layers on the same
        compositor does none of that.
        """
        if self.engine is None:
            return
        self._apply_playlist_visibility()
        try:
            scene = self.scene
            kind, seconds = (getattr(self, "_take_override", None)
                             or (transitions.transition_kind(scene.transition),
                                 transitions.transition_seconds(scene.transition_ms)))
            switched = self.engine.switch_scene(scene, kind, seconds)
        except Exception as exc:  # noqa: BLE001 - fall back to the full rebuild
            self.log_event(f"scene: live switch failed ({exc}); rebuilding")
            switched = False
        if not switched:
            self.restart_engine()
            return
        self.preview.layer_sizes = self.engine.source_sizes()
        self._source_status = ()
        self._update_canvas_note()
