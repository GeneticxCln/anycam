"""Drawing on the video: the draw bar and its strokes."""
from __future__ import annotations



from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QFrame,
    QHBoxLayout, QLabel, QPushButton, QSlider, QWidget,
)

from ..model import (Layer, new_id)
from .. import animated
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class DrawMixin:
    """Drawing on the video: the draw bar and its strokes."""

    def _build_draw_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("panel")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 4, 8, 4)
        self.draw_tool_buttons: dict[str, QPushButton] = {}
        for key, label in self.DRAW_TOOLS:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(key == "pen")
            button.clicked.connect(lambda _=False, k=key: self._draw_tool(k))
            self.draw_tool_buttons[key] = button
            layout.addWidget(button)
        layout.addSpacing(8)
        self.draw_colour_buttons: dict[str, QPushButton] = {}
        for colour in self.DRAW_COLOURS:
            swatch = QPushButton()
            swatch.setFixedSize(22, 22)
            swatch.setToolTip(colour)
            swatch.setStyleSheet(
                f"background:{colour}; border:2px solid #26343d; border-radius:11px;")
            swatch.clicked.connect(lambda _=False, c=colour: self._draw_colour(c))
            self.draw_colour_buttons[colour] = swatch
            layout.addWidget(swatch)
        more = QPushButton("\u2026")
        more.setFixedWidth(28)
        more.setToolTip("Pick another colour")
        more.clicked.connect(self._pick_draw_colour)
        layout.addWidget(more)
        layout.addSpacing(8)
        layout.addWidget(QLabel("Size"))
        self.draw_width_slider = QSlider(Qt.Orientation.Horizontal)
        self.draw_width_slider.setRange(1, 20)
        self.draw_width_slider.setValue(4)
        self.draw_width_slider.setFixedWidth(90)
        self.draw_width_slider.valueChanged.connect(self._draw_width)
        layout.addWidget(self.draw_width_slider)
        layout.addStretch(1)
        undo = QPushButton("Undo")
        undo.clicked.connect(self._draw_undo)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._draw_clear)
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.clicked.connect(lambda: self.toggle_draw(False))
        for button in (undo, clear, done):
            layout.addWidget(button)
        bar.hide()
        return bar

    def toggle_draw(self, on: bool | None = None) -> None:
        """Enter or leave draw mode (the pointer then draws instead of moving layers)."""
        if on is None:
            on = self.preview.edit_mode != "draw"
        self.preview.set_edit_mode("draw" if on else "move")
        self.draw_bar.setVisible(bool(on))
        button = getattr(self, "draw_button", None)
        if button is not None:
            button.blockSignals(True)
            button.setChecked(bool(on))
            button.blockSignals(False)
        self._status("Draw: drag on the picture. Done returns to editing." if on
                     else "Editing")

    def _draw_tool(self, key: str) -> None:
        self.preview.draw_tool = key
        for name, button in self.draw_tool_buttons.items():
            button.setChecked(name == key)

    def _draw_colour(self, colour: str) -> None:
        self.preview.draw_color = colour

    def _pick_draw_colour(self) -> None:
        chosen = QColorDialog.getColor(QColor(self.preview.draw_color), self, "Drawing colour")
        if chosen.isValid():
            self._draw_colour(chosen.name())

    def _draw_width(self, value: int) -> None:
        self.preview.draw_width = value * 0.0015

    def _drawing_layer(self) -> Layer | None:
        """The layer strokes go to: the selected drawing layer, else the topmost."""
        selected = self.preview.selected_layer()
        if selected is not None and selected.kind == "drawing":
            return selected
        drawings = [layer for layer in self.scene.layers if layer.kind == "drawing"]
        return max(drawings, key=lambda layer: layer.z) if drawings else None

    def _draw_stroke_finished(self, stroke: dict) -> None:
        layer = self._drawing_layer()
        if layer is None:
            layer = Layer(new_id("layer"), "drawing", "Drawing",
                          source=animated.dump_strokes([stroke]),
                          box=(0.0, 0.0, 1.0, 1.0), z=0)
            self._insert_layer(layer)
            self._mark_dirty()
            return
        strokes = animated.parse_strokes(layer.source)
        strokes.append(stroke)
        self._apply_strokes(layer, strokes)

    def _apply_strokes(self, layer: Layer, strokes: list[dict]) -> None:
        layer.source = animated.dump_strokes(strokes)
        self._mark_dirty()
        if self.engine is not None and not self.engine.set_layer_strokes(layer.id, strokes):
            self.restart_engine()

    def _draw_undo(self) -> None:
        layer = self._drawing_layer()
        strokes = animated.parse_strokes(layer.source) if layer is not None else []
        if not strokes:
            self._status("Nothing to undo")
            return
        strokes.pop()
        self._apply_strokes(layer, strokes)

    def _draw_clear(self) -> None:
        layer = self._drawing_layer()
        if layer is not None and animated.parse_strokes(layer.source):
            self._apply_strokes(layer, [])
            self._status("Drawing cleared")
