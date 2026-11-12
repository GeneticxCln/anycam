"""The Stage tab: backdrops and animated backgrounds."""
from __future__ import annotations



from PySide6.QtWidgets import (
    QCheckBox, QGridLayout, QGroupBox, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import overlay
from ..model import (Layer, new_id)
from ..sources import ANIMATION_PRESETS
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class BackdropMixin:
    """The Stage tab: backdrops and animated backgrounds."""

    def _backdrop_tab(self) -> QWidget:
        page = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)

        intro = QLabel(
            "The stage is the canvas behind your camera tiles: it fills the "
            "space around them and does not hide your room. To blur or "
            "replace what is behind you, use Effects → Portrait → Virtual "
            "background. Static stages are generated at the canvas size; "
            "animated ones use live GPU 3D shaders when OpenGL is available.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        static = QGroupBox("Static stages")
        static_grid = QGridLayout(static)
        for index, name in enumerate(overlay.BACKGROUND_NAMES):
            button = QPushButton(name)
            button.clicked.connect(
                lambda _checked=False, value=name: self._apply_background(value))
            static_grid.addWidget(button, index // 2, index % 2)
        layout.addWidget(static)

        motion = QGroupBox("Animated stages")
        motion_grid = QGridLayout(motion)
        for index, (key, spec) in enumerate(ANIMATION_PRESETS.items()):
            button = QPushButton(str(spec["label"]))
            button.clicked.connect(
                lambda _checked=False, value=key: self._apply_animation(value))
            motion_grid.addWidget(button, index // 2, index % 2)
        layout.addWidget(motion)

        actions = QGroupBox("Composition")
        actions_layout = QVBoxLayout(actions)
        self.inset_on_backdrop = QCheckBox(
            "Inset cameras when a stage is applied")
        self.inset_on_backdrop.setChecked(True)
        actions_layout.addWidget(self.inset_on_backdrop)
        inset = QPushButton("Inset capture layers now")
        inset.clicked.connect(self._inset_capture_layers)
        actions_layout.addWidget(inset)
        clear = QPushButton("Clear stage")
        clear.clicked.connect(self._clear_backdrop)
        actions_layout.addWidget(clear)
        self.backdrop_status = QLabel("No stage selected")
        self.backdrop_status.setObjectName("accent")
        self.backdrop_status.setWordWrap(True)
        actions_layout.addWidget(self.backdrop_status)
        layout.addWidget(actions)

        layout.addStretch(1)
        scroll.setWidget(body)
        container = QVBoxLayout(page)
        container.addWidget(scroll)
        return page

    def _active_backdrops(self) -> list[Layer]:
        return [layer for layer in self.scene.layers
                if getattr(layer, "role", "") == "backdrop"]

    def _remove_backdrops(self) -> None:
        """Delete the current backdrop layers, not just hide them.

        A hidden backdrop stayed in the scene (and was rebuilt as a full-canvas
        chain on every restart), so each switch of look added another one.
        """
        old = [layer.id for layer in self._active_backdrops()]
        if old:
            self.remove_layers(old)

    def _replace_backdrop(self, layer: Layer) -> Layer:
        had_backdrop = bool(self._active_backdrops())
        self._remove_backdrops()
        new_layer = self._insert_layer(layer)
        others = [item.z for item in self.scene.layers if item is not new_layer]
        new_layer.z = min(others, default=1) - 1
        if self.engine is not None:
            self.engine.refresh_layer_order()
        self._refresh_layers()
        # Inset once, when the first backdrop appears.  Switching from Aurora
        # to Sunset must not shrink the camera a second time.
        if not had_backdrop and self.inset_on_backdrop.isChecked():
            self._inset_capture_layers()
        return new_layer

    def _apply_background(self, name: str) -> None:
        width = int(self.project.output.canvas_w)
        height = int(self.project.output.canvas_h)
        path = overlay.render_background(name, width, height)
        layer = Layer(
            new_id("layer"), "image", f"Stage · {name}",
            source=str(path), box=(0.0, 0.0, 1.0, 1.0), z=0,
            role="backdrop",
        )
        self._replace_backdrop(layer)
        self.backdrop_status.setText(f"Stage: {name}")
        self._status(f"Stage: {name}")

    def _apply_animation(self, key: str) -> None:
        spec = ANIMATION_PRESETS.get(key, {})
        label = str(spec.get("label", key))
        layer = Layer(
            new_id("layer"), "animation", f"Motion · {label}",
            source=key, box=(0.0, 0.0, 1.0, 1.0), z=0,
            role="backdrop",
        )
        self._replace_backdrop(layer)
        self.backdrop_status.setText(f"Motion: {label}")
        self._status(f"Animated stage: {label}")

    def _clear_backdrop(self) -> None:
        had_backdrop = bool(self._active_backdrops())
        self._remove_backdrops()
        self._restore_inset_layers()
        if had_backdrop:
            self._mark_dirty()
        self.backdrop_status.setText("No stage selected")
        self._status("Stage cleared")

    def _inset_capture_layers(self) -> None:
        changed = 0
        for layer in self.scene.layers:
            if layer.kind not in self._CAPTURE_KINDS:
                continue
            previous = self._backdrop_insets.get(layer.id)
            if previous is not None and all(
                    abs(a - b) < 1e-6 for a, b in zip(layer.box, previous[1])):
                # Already inset and not moved since: insetting again would
                # record the inset box as the "original" and Clear stage could
                # no longer restore the real one.
                continue
            x, y, w, h = layer.box
            box = (x + w * 0.055, y + h * 0.055, w * 0.89, h * 0.89)
            self._backdrop_insets[layer.id] = (tuple(layer.box), box)
            layer.box = box
            if self.engine is not None:
                self.engine.set_layer_box(layer.id, box)
            changed += 1
        self.preview.update()
        self._mark_dirty()
        if changed:
            self._status(f"Inset {changed} capture layer(s) to reveal the stage")

    def _restore_inset_layers(self) -> None:
        """Undo the backdrop inset on layers the user has not moved since."""
        restored = 0
        for layer_id, (original, inset) in list(self._backdrop_insets.items()):
            layer = self.scene.layer(layer_id)
            if layer is None:
                continue  # another scene's layer, or removed: keep/forget below
            del self._backdrop_insets[layer_id]
            if all(abs(a - b) < 1e-6 for a, b in zip(layer.box, inset)):
                layer.box = original
                if self.engine is not None:
                    self.engine.set_layer_box(layer_id, original)
                restored += 1
        if restored:
            self.preview.update()
            self._mark_dirty()
