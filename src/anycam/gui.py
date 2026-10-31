"""AnyCam's standalone Qt GUI.

The window is deliberately plain QtWidgets: the preview is a painted QImage
from the engine's appsink, which keeps the GUI testable with Qt's ``offscreen``
platform and avoids a QML scene-graph dependency in headless CI.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSpinBox, QSplitter, QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)

from . import config, overlay, portal
from .geometry import clamp
from .model import (Effects, Layer, Project, Scene, camera_grid_scene,
                    default_project, new_id, two_camera_scene)
from .engine.outputs.recorder import Recorder
from .engine.outputs.vcam import VcamOutput
from .pipeline import AnyCamEngine
from .pool import SourcePool

STYLE = """
QMainWindow, QWidget { background: #0b0d10; color: #d7e3ea; font-family: "Noto Sans", "DejaVu Sans"; font-size: 13px; }
QFrame#panel { background: #101418; border: 1px solid #1e2830; border-radius: 10px; }
QLabel#title { color: #ffffff; font-size: 20px; font-weight: 700; }
QLabel#muted { color: #7d8b96; }
QLabel#accent { color: #5eead0; font-weight: 700; }
QPushButton { background: #172026; border: 1px solid #26343d; border-radius: 7px; padding: 7px 12px; }
QPushButton:hover { border-color: #5eead0; }
QPushButton:pressed { background: #1d2b31; }
QPushButton#primary { background: #5eead0; color: #07120f; font-weight: 700; border: none; }
QPushButton#danger { background: #3a1d22; border-color: #7a2f3a; }
QListWidget { background: #0e1216; border: 1px solid #1e2830; border-radius: 7px; }
QListWidget::item { padding: 6px; }
QListWidget::item:selected { background: #173a37; color: #5eead0; }
QTabWidget::pane { border: 1px solid #1e2830; border-radius: 8px; }
QTabBar::tab { background: #101418; padding: 7px 12px; border: 1px solid #1e2830; }
QTabBar::tab:selected { color: #5eead0; }
QSlider::groove:horizontal { height: 4px; background: #1e2830; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; margin: -5px 0; border-radius: 7px; background: #5eead0; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #0e1216; border: 1px solid #26343d; border-radius: 6px; padding: 5px; }
"""


class LayoutHistory:
    """Snapshot-based undo/redo for a scene's layers.

    A snapshot stores every layer's serialised state, in scene order, so one
    undo step can cover a move, a stacking/opacity change, or a structural
    edit such as adding, removing or duplicating a layer.  Playlist and scene
    changes still clear the stacks because they also change the active graph.
    """

    def __init__(self, limit: int = 100):
        self.limit = limit
        self._undo: list[list[dict]] = []
        self._redo: list[list[dict]] = []

    @staticmethod
    def snapshot(scene: Scene) -> list[dict]:
        return [layer.to_dict() for layer in scene.layers]

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def push(self, before: list[dict], after: list[dict]) -> bool:
        if before == after:
            return False
        self._undo.append(before)
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        return True

    def undo(self, current: list[dict]) -> list[dict] | None:
        if not self._undo:
            return None
        self._redo.append(current)
        return self._undo.pop()

    def redo(self, current: list[dict]) -> list[dict] | None:
        if not self._redo:
            return None
        self._undo.append(current)
        return self._redo.pop()

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()


class PreviewWidget(QWidget):
    """Paints the composited frame and edits layer boxes directly on it.

    Pointer positions are converted to canvas-normalised coordinates (0..1),
    the same space as ``Layer.box`` and ``Layer.crop``.    Ctrl/Shift-click
    toggles a multi-selection; a move drag translates every selected layer and
    can snap its bounding box to canvas or other-layer edges/centres, drawing
    alignment lines and the nearest spacing gap.  Snap tolerance is adjustable
    and can be turned off from the Layers tab.  Move/resize edits are mirrored
    onto the live compositor pad; crop edits, text/logo/image resizes and
    keyboard nudges emit ``edit_finished(id, True)`` when a graph rebuild is
    needed (immediately on release, or after a short debounce for arrows).
    """

    selection_changed = Signal(str)
    geometry_changed = Signal(str)
    edit_started = Signal(str)
    edit_finished = Signal(str, bool)

    HANDLE = 9.0
    MIN_BOX = 0.02
    MIN_CROP_SPAN = 0.10
    SNAP_PX = 6.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image: QImage | None = None
        self.scene: Scene | None = None
        self.selected_id = ""
        self.selected_ids: list[str] = []
        self.edit_mode = "move"          # move | crop
        # Source size per layer, so the crop window can follow the picture
        # inside the box instead of the box (which may contain bars).
        self.layer_sizes: dict[str, tuple[int, int]] = {}
        self._drag: dict | None = None
        self._guides: list[tuple[str, float]] = []
        self._spacing: list[tuple[str, float, float, float, int]] = []
        self.snap_enabled = True
        self.snap_px = self.SNAP_PX
        self._nudge_layer = ""
        self._nudge_rebuild = False
        self._nudge_timer = QTimer(self)
        self._nudge_timer.setSingleShot(True)
        self._nudge_timer.setInterval(350)
        self._nudge_timer.timeout.connect(self._finish_nudge)
        self.setMinimumSize(QSize(640, 360))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_image(self, image: QImage) -> None:
        self.image = image
        self.update()

    # ------------------------------------------------------------- selection
    def set_scene(self, scene: Scene | None) -> None:
        self.scene = scene
        if scene is not None:
            self.selected_ids = [lid for lid in self.selected_ids if scene.layer(lid) is not None]
            if self.selected_id not in self.selected_ids:
                self.selected_id = self.selected_ids[-1] if self.selected_ids else ""
        else:
            self.selected_ids = []
            self.selected_id = ""
        self.update()

    def set_edit_mode(self, mode: str) -> None:
        if mode not in ("move", "crop"):
            return
        self.edit_mode = mode
        self._drag = None
        self.unsetCursor()
        self.update()

    def cancel_pending_edits(self) -> None:
        """Drop a queued keyboard nudge so it cannot fire after shutdown."""
        self._nudge_timer.stop()
        self._nudge_layer = ""
        self._nudge_rebuild = False

    def set_snap_enabled(self, enabled: bool) -> None:
        """Turn guide snapping on or off; guides still show when off."""
        self.snap_enabled = bool(enabled)
        self.update()

    def set_snap_threshold(self, pixels: float) -> None:
        """Set the alignment-guide snap tolerance in canvas pixels."""
        self.snap_px = max(0.0, float(pixels))
        self.update()

    def set_selection(self, layer_ids, primary: str = "") -> None:
        ids: list[str] = []
        for layer_id in layer_ids:
            if (layer_id and layer_id not in ids and self.scene is not None
                    and self.scene.layer(layer_id) is not None):
                ids.append(layer_id)
        primary = primary if primary in ids else (ids[-1] if ids else "")
        changed = ids != self.selected_ids or primary != self.selected_id
        self.selected_ids = ids
        self.selected_id = primary
        self.update()
        if changed:
            self.selection_changed.emit(primary)

    def select_layer(self, layer_id: str, additive: bool = False) -> None:
        layer_id = layer_id or ""
        if not layer_id:
            self.set_selection([], "")
            return
        if additive:
            ids = list(self.selected_ids)
            if layer_id in ids:
                ids.remove(layer_id)
                primary = ids[-1] if ids else ""
            else:
                ids.append(layer_id)
                primary = layer_id
            self.set_selection(ids, primary)
            return
        self.set_selection([layer_id], layer_id)

    def set_primary(self, layer_id: str) -> None:
        if layer_id in self.selected_ids and layer_id != self.selected_id:
            self.selected_id = layer_id
            self.update()
            self.selection_changed.emit(layer_id)

    def selected_layer(self) -> Layer | None:
        if self.scene is None or not self.selected_id:
            return None
        return self.scene.layer(self.selected_id)

    def selected_layers(self) -> list[Layer]:
        if self.scene is None:
            return []
        layers = [self.scene.layer(lid) for lid in self.selected_ids]
        return [layer for layer in layers if layer is not None and layer.visible]

    # -------------------------------------------------------------- geometry
    def canvas_rect(self) -> QRectF:
        """The letterboxed canvas inside the widget, in widget pixels."""
        if self.image is None or self.image.isNull():
            return QRectF()
        scaled = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF((self.width() - scaled.width()) / 2.0,
                      (self.height() - scaled.height()) / 2.0,
                      scaled.width(), scaled.height())

    def box_rect(self, layer: Layer) -> QRectF:
        canvas = self.canvas_rect()
        if canvas.isEmpty():
            return QRectF()
        x, y, w, h = layer.box
        return QRectF(canvas.x() + x * canvas.width(),
                      canvas.y() + y * canvas.height(),
                      w * canvas.width(), h * canvas.height())

    def picture_rect(self, layer: Layer) -> QRectF:
        """The visible source inside the layer box, excluding letterbox bars."""
        box = self.box_rect(layer)
        size = self.layer_sizes.get(layer.id)
        if box.isEmpty() or not size or size[0] <= 0 or size[1] <= 0:
            return box
        scale = min(box.width() / size[0], box.height() / size[1])
        width, height = size[0] * scale, size[1] * scale
        return QRectF(box.x() + (box.width() - width) / 2.0,
                      box.y() + (box.height() - height) / 2.0,
                      width, height)

    def crop_rect(self, layer: Layer) -> QRectF:
        rect = self.picture_rect(layer)
        if rect.isEmpty():
            return rect
        left, top, right, bottom = layer.crop
        return QRectF(rect.x() + left * rect.width(),
                      rect.y() + top * rect.height(),
                      max(1.0, rect.width() * (1.0 - left - right)),
                      max(1.0, rect.height() * (1.0 - top - bottom)))

    def point_norm(self, pos: QPointF) -> tuple[float, float]:
        canvas = self.canvas_rect()
        if canvas.isEmpty():
            return (0.0, 0.0)
        return ((pos.x() - canvas.x()) / canvas.width(),
                (pos.y() - canvas.y()) / canvas.height())

    def layer_at(self, pos: QPointF) -> Layer | None:
        if self.scene is None or not self.canvas_rect().contains(pos):
            return None
        for layer in reversed(self.scene.ordered()):
            if self.box_rect(layer).contains(pos):
                return layer
        return None

    @classmethod
    def _handle_at(cls, rect: QRectF, pos: QPointF) -> str:
        if rect.isEmpty():
            return ""
        near = cls.HANDLE
        left = abs(pos.x() - rect.left()) <= near
        right = abs(pos.x() - rect.right()) <= near
        top = abs(pos.y() - rect.top()) <= near
        bottom = abs(pos.y() - rect.bottom()) <= near
        inside_x = rect.left() - near <= pos.x() <= rect.right() + near
        inside_y = rect.top() - near <= pos.y() <= rect.bottom() + near
        if left and top:
            return "lt"
        if right and top:
            return "rt"
        if left and bottom:
            return "lb"
        if right and bottom:
            return "rb"
        if left and inside_y:
            return "l"
        if right and inside_y:
            return "r"
        if top and inside_x:
            return "t"
        if bottom and inside_x:
            return "b"
        return ""

    @staticmethod
    def _cursor_for(handle: str) -> Qt.CursorShape:
        if not handle:
            return Qt.CursorShape.SizeAllCursor
        if handle in ("lt", "rb"):
            return Qt.CursorShape.SizeFDiagCursor
        if handle in ("rt", "lb"):
            return Qt.CursorShape.SizeBDiagCursor
        if handle in ("l", "r"):
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeVerCursor

    # --------------------------------------------------------------- editing
    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self.setFocus()
        pos = event.position()
        layer = self.selected_layer()
        if layer is not None and layer.visible:
            rect = self.crop_rect(layer) if self.edit_mode == "crop" else self.box_rect(layer)
            handle = self._handle_at(rect, pos)
            if handle:
                mode = "crop-resize" if self.edit_mode == "crop" else "resize"
                self._begin_drag(layer, pos, mode, handle)
                return
        target = self.layer_at(pos)
        if target is None:
            self.select_layer("")
            return
        additive = bool(event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
            | Qt.KeyboardModifier.MetaModifier))
        if additive:
            self.select_layer(target.id, additive=True)
            return
        if target.id not in self.selected_ids:
            self.select_layer(target.id)
        elif target.id != self.selected_id:
            self.set_primary(target.id)
        mode = "crop-move" if self.edit_mode == "crop" else "move"
        self._begin_drag(target, pos, mode, "")

    def mouseMoveEvent(self, event) -> None:
        if self._drag is not None:
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                self._end_drag()
                return
            self._apply_drag(event.position())
            return
        layer = self.selected_layer()
        rect = None
        if layer is not None:
            rect = self.crop_rect(layer) if self.edit_mode == "crop" else self.box_rect(layer)
        handle = self._handle_at(rect, event.position()) if rect is not None else ""
        if handle:
            self.setCursor(self._cursor_for(handle))
        elif self.layer_at(event.position()) is not None:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self._end_drag()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        layer = self.selected_layer()
        arrows = {
            Qt.Key.Key_Left: (-1.0, 0.0),
            Qt.Key.Key_Right: (1.0, 0.0),
            Qt.Key.Key_Up: (0.0, -1.0),
            Qt.Key.Key_Down: (0.0, 1.0),
        }
        if layer is None or not layer.visible or event.key() not in arrows:
            super().keyPressEvent(event)
            return
        if self._nudge_layer and self._nudge_layer != layer.id:
            self._finish_nudge()
        if not self._nudge_layer:
            self.edit_started.emit(layer.id)
        step = 0.02 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 0.002
        dx, dy = arrows[event.key()]
        if self.edit_mode == "crop":
            layer.crop = self._moved_crop(layer.crop, dx * step, dy * step)
            changed_layers = [layer]
            rebuild = True
        else:
            changed_layers = self.selected_layers() or [layer]
            for item in changed_layers:
                item.box = self._shift_box(item.box, dx * step, dy * step)
            rebuild = any(item.kind in ("text", "logo", "image")
                          for item in changed_layers)
        self.update()
        for item in changed_layers:
            self.geometry_changed.emit(item.id)
        self._nudge_layer = layer.id
        self._nudge_rebuild = rebuild
        self._nudge_timer.start()
        event.accept()

    def _finish_nudge(self) -> None:
        self._nudge_timer.stop()
        layer_id = self._nudge_layer
        rebuild = self._nudge_rebuild
        self._nudge_layer = ""
        self._nudge_rebuild = False
        if layer_id and self.scene is not None and self.scene.layer(layer_id) is not None:
            self.edit_finished.emit(layer_id, rebuild)

    def _begin_drag(self, layer: Layer, pos: QPointF, mode: str, handle: str) -> None:
        self.edit_started.emit(layer.id)
        group = []
        if mode == "move":
            group = [(item.id, tuple(item.box)) for item in self.selected_layers()]
        if not group:
            group = [(layer.id, tuple(layer.box))]
        self._guides = []
        self._spacing = []
        self._drag = {
            "layer": layer.id,
            "mode": mode,
            "handle": handle,
            "origin": self.point_norm(pos),
            "box": tuple(layer.box),
            "crop": tuple(layer.crop),
            "group": group,
            "changed": False,
        }
        self.setCursor(self._cursor_for(handle))
        self.update()

    def _apply_drag(self, pos: QPointF) -> None:
        if self._drag is None or self.scene is None:
            return
        layer = self.scene.layer(self._drag["layer"])
        if layer is None:
            return
        x, y = self.point_norm(pos)
        dx = x - self._drag["origin"][0]
        dy = y - self._drag["origin"][1]
        mode = self._drag["mode"]
        if mode in ("crop-move", "crop-resize"):
            # Crop insets are fractions of the picture, not of the layer box.
            canvas = self.canvas_rect()
            picture = self.picture_rect(layer)
            if not canvas.isEmpty() and picture.width() > 0 and picture.height() > 0:
                dx *= canvas.width() / picture.width()
                dy *= canvas.height() / picture.height()
        if mode == "move":
            dx, dy, guides, spacing = self._snap_move(self._drag["group"], dx, dy)
            for layer_id, start in self._drag["group"]:
                item = self.scene.layer(layer_id)
                if item is None:
                    continue
                item.box = self._shift_box(start, dx, dy)
                self.geometry_changed.emit(item.id)
            self._guides = guides
            self._spacing = spacing
            self._drag["changed"] = True
            self.update()
            return
        self._guides = []
        self._spacing = []
        if mode == "crop-move":
            layer.crop = self._moved_crop(self._drag["crop"], dx, dy)
        elif mode == "resize":
            layer.box = self._resized_box(self._drag["box"], self._drag["handle"], dx, dy)
        else:
            layer.crop = self._resized_crop(self._drag["crop"], self._drag["handle"], dx, dy)
        self._drag["changed"] = True
        self.update()
        self.geometry_changed.emit(layer.id)

    @staticmethod
    def _shift_box(box: tuple, dx: float, dy: float) -> tuple:
        x, y, w, h = box
        return (clamp(x + dx, 0.0, 1.0 - w),
                clamp(y + dy, 0.0, 1.0 - h), w, h)

    def _snap_move(self, group: list[tuple[str, tuple]], dx: float, dy: float):
        """Snap a moving group to canvas/layer edges and report guide lines."""
        canvas = self.canvas_rect()
        if canvas.isEmpty() or not group:
            return dx, dy, [], []
        left = min(box[0] for _, box in group)
        top = min(box[1] for _, box in group)
        right = max(box[0] + box[2] for _, box in group)
        bottom = max(box[1] + box[3] for _, box in group)
        width, height = right - left, bottom - top
        left += dx
        top += dy
        right += dx
        bottom += dy
        selected = set(self.selected_ids)
        x_targets = {0.0, 0.5, 1.0}
        y_targets = {0.0, 0.5, 1.0}
        for other in (self.scene.ordered() if self.scene is not None else []):
            if other.id in selected:
                continue
            ox, oy, ow, oh = other.box
            x_targets.update((ox, ox + ow / 2.0, ox + ow))
            y_targets.update((oy, oy + oh / 2.0, oy + oh))

        def nearest(edges, targets, threshold):
            best = None
            for edge in edges:
                for target in targets:
                    delta = target - edge
                    if abs(delta) <= threshold and (best is None or abs(delta) < abs(best[0])):
                        best = (delta, target)
            return best

        threshold_x = max(0.0, self.snap_px) / canvas.width()
        threshold_y = max(0.0, self.snap_px) / canvas.height()
        snap_x = nearest((left, left + width / 2.0, right), x_targets,
                         threshold_x) if self.snap_enabled and threshold_x > 0 else None
        snap_y = nearest((top, top + height / 2.0, bottom), y_targets,
                         threshold_y) if self.snap_enabled and threshold_y > 0 else None
        if snap_x:
            dx += snap_x[0]
            left += snap_x[0]
        if snap_y:
            dy += snap_y[0]
            top += snap_y[0]
        guides = []
        # With snapping on, a guide marks the exact snap or a near miss.  With
        # snapping off, use the full tolerance so the guide still explains the
        # alignment the pointer is approaching.
        guide_scale = 0.5 if self.snap_enabled else 1.0
        x_eps = guide_scale * max(0.0, self.snap_px) / canvas.width()
        y_eps = guide_scale * max(0.0, self.snap_px) / canvas.height()
        for target in sorted(x_targets):
            if min(abs(left - target), abs(left + width / 2.0 - target),
                   abs(left + width - target)) <= x_eps:
                guides.append(("v", target))
        for target in sorted(y_targets):
            if min(abs(top - target), abs(top + height / 2.0 - target),
                   abs(top + height - target)) <= y_eps:
                guides.append(("h", target))
        spacing = self._spacing_guides(left, top, width, height, selected)
        return dx, dy, guides, spacing

    def _spacing_guides(self, left: float, top: float, width: float,
                        height: float, selected: set[str]) -> list[tuple]:
        """Nearest horizontal and vertical gaps to non-selected layers, in px."""
        canvas = self.canvas_rect()
        if canvas.isEmpty() or self.scene is None:
            return []
        right, bottom = left + width, top + height
        horizontal = []
        vertical = []
        for other in self.scene.ordered():
            if other.id in selected:
                continue
            ox, oy, ow, oh = other.box
            oright, obottom = ox + ow, oy + oh
            if min(bottom, obottom) - max(top, oy) > 0:
                if oright <= left:
                    horizontal.append((left - oright, oright, left, top + height / 2.0))
                elif ox >= right:
                    horizontal.append((ox - right, right, ox, top + height / 2.0))
            if min(right, oright) - max(left, ox) > 0:
                if obottom <= top:
                    vertical.append((top - obottom, obottom, top, left + width / 2.0))
                elif oy >= bottom:
                    vertical.append((oy - bottom, bottom, oy, left + width / 2.0))
        spacing = []
        if horizontal:
            gap, start, end, fixed = min(horizontal, key=lambda item: item[0])
            if gap > 0.001:
                spacing.append(("v", start, end, fixed,
                                int(round(gap * self.image.width()))))
        if vertical:
            gap, start, end, fixed = min(vertical, key=lambda item: item[0])
            if gap > 0.001:
                spacing.append(("h", start, end, fixed,
                                int(round(gap * self.image.height()))))
        return spacing

    def _resized_box(self, start: tuple, handle: str, dx: float, dy: float) -> tuple:
        x, y, w, h = start
        left, top, right, bottom = x, y, x + w, y + h
        if "l" in handle:
            left = clamp(left + dx, 0.0, right - self.MIN_BOX)
        if "r" in handle:
            right = clamp(right + dx, left + self.MIN_BOX, 1.0)
        if "t" in handle:
            top = clamp(top + dy, 0.0, bottom - self.MIN_BOX)
        if "b" in handle:
            bottom = clamp(bottom + dy, top + self.MIN_BOX, 1.0)
        return (left, top, right - left, bottom - top)

    def _resized_crop(self, start: tuple, handle: str, dx: float, dy: float) -> tuple:
        left, top, right, bottom = start
        if "l" in handle:
            left = clamp(left + dx, 0.0, 1.0 - right - self.MIN_CROP_SPAN)
        if "r" in handle:
            right = clamp(right - dx, 0.0, 1.0 - left - self.MIN_CROP_SPAN)
        if "t" in handle:
            top = clamp(top + dy, 0.0, 1.0 - bottom - self.MIN_CROP_SPAN)
        if "b" in handle:
            bottom = clamp(bottom - dy, 0.0, 1.0 - top - self.MIN_CROP_SPAN)
        return (left, top, right, bottom)

    def _moved_crop(self, start: tuple, dx: float, dy: float) -> tuple:
        left, top, right, bottom = start
        span_w = 1.0 - left - right
        span_h = 1.0 - top - bottom
        # Move the whole window while preserving its size: the far inset is
        # reconstructed from the new near inset and the original span.
        left = clamp(left + dx, 0.0, 1.0 - span_w)
        right = 1.0 - left - span_w
        top = clamp(top + dy, 0.0, 1.0 - span_h)
        bottom = 1.0 - top - span_h
        return (left, top, right, bottom)

    def _end_drag(self) -> None:
        drag = self._drag
        self._drag = None
        self._guides = []
        self._spacing = []
        self.unsetCursor()
        self.update()
        if not drag:
            return
        if not drag["changed"]:
            # Clear the window's pending snapshot even for a click with no
            # movement, so the next edit starts from the current layout.
            self.edit_finished.emit(drag["layer"], False)
            return
        layers = []
        if drag["mode"] == "move":
            layers = [self.scene.layer(lid) for lid, _ in drag["group"]]
        elif self.scene is not None:
            layers = [self.scene.layer(drag["layer"])]
        rebuild = drag["mode"].startswith("crop") or any(
            layer is not None and layer.kind in ("text", "logo", "image")
            for layer in layers)
        self.edit_finished.emit(drag["layer"], bool(rebuild))

    # --------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#07090b"))
        if self.image is None or self.image.isNull():
            painter.setPen(QColor("#41525c"))
            font = painter.font()
            font.setPointSize(13)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Preview stopped\nAnyCam")
            return
        scaled = self.image.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawImage(x, y, scaled)
        self._paint_selection(painter)
        self._paint_guides(painter)

    def _paint_selection(self, painter: QPainter) -> None:
        if self.scene is None:
            return
        primary = self.selected_layer()
        for layer_id in self.selected_ids:
            layer = self.scene.layer(layer_id)
            if layer is None:
                continue
            box = self.box_rect(layer)
            if box.isEmpty():
                continue
            if layer is not primary:
                pen = QPen(QColor(94, 234, 208, 170))
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(box)
                continue
            active = self.crop_rect(layer) if self.edit_mode == "crop" else box
            if self.edit_mode == "crop" and not active.isEmpty():
                picture = self.picture_rect(layer)
                dim = QColor(0, 0, 0, 120)
                for leftover in (
                    QRectF(picture.left(), picture.top(), picture.width(), active.top() - picture.top()),
                    QRectF(picture.left(), active.bottom(), picture.width(), picture.bottom() - active.bottom()),
                    QRectF(picture.left(), active.top(), active.left() - picture.left(), active.height()),
                    QRectF(active.right(), active.top(), picture.right() - active.right(), active.height()),
                ):
                    if leftover.isValid():
                        painter.fillRect(leftover, dim)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QColor("#41525c"))
            painter.drawRect(box)
            painter.setPen(QColor("#5eead0"))
            painter.drawRect(active)
            painter.setBrush(QColor("#5eead0"))
            cx, cy = active.center().x(), active.center().y()
            for point in (active.topLeft(), QPointF(cx, active.top()), active.topRight(),
                          QPointF(active.right(), cy), active.bottomRight(),
                          QPointF(cx, active.bottom()), active.bottomLeft(),
                          QPointF(active.left(), cy)):
                painter.drawRect(QRectF(point.x() - 3.5, point.y() - 3.5, 7.0, 7.0))
            painter.setPen(QColor("#d7e3ea"))
            painter.drawText(QPointF(box.left() + 4.0, max(12.0, box.top() - 6.0)), layer.name)

    def _paint_guides(self, painter: QPainter) -> None:
        canvas = self.canvas_rect()
        if canvas.isEmpty():
            return
        if self._guides:
            pen = QPen(QColor(94, 234, 208, 150))
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            for orientation, value in self._guides:
                if orientation == "v":
                    x = canvas.x() + value * canvas.width()
                    painter.drawLine(QPointF(x, canvas.top()), QPointF(x, canvas.bottom()))
                else:
                    y = canvas.y() + value * canvas.height()
                    painter.drawLine(QPointF(canvas.left(), y), QPointF(canvas.right(), y))
        if self._spacing:
            painter.setPen(QPen(QColor("#d7e3ea")))
            font = painter.font()
            font.setPointSize(8)
            painter.setFont(font)
            for orientation, start, end, fixed, gap in self._spacing:
                if orientation == "v":
                    y = canvas.y() + fixed * canvas.height()
                    x1 = canvas.x() + start * canvas.width()
                    x2 = canvas.x() + end * canvas.width()
                    painter.drawLine(QPointF(x1, y), QPointF(x2, y))
                    painter.drawLine(QPointF(x1, y - 3), QPointF(x1, y + 3))
                    painter.drawLine(QPointF(x2, y - 3), QPointF(x2, y + 3))
                    label = QPointF((x1 + x2) / 2.0 - 8, y - 5)
                else:
                    x = canvas.x() + fixed * canvas.width()
                    y1 = canvas.y() + start * canvas.height()
                    y2 = canvas.y() + end * canvas.height()
                    painter.drawLine(QPointF(x, y1), QPointF(x, y2))
                    painter.drawLine(QPointF(x - 3, y1), QPointF(x + 3, y1))
                    painter.drawLine(QPointF(x - 3, y2), QPointF(x + 3, y2))
                    label = QPointF(x + 5, (y1 + y2) / 2.0)
                painter.drawText(label, f"{gap} px")


def _slider_row(label: str, low: int, high: int, value: int, callback) -> tuple[QWidget, QSlider, QLabel]:
    holder = QWidget()
    row = QGridLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    title = QLabel(label)
    value_label = QLabel(str(value))
    value_label.setObjectName("accent")
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(low, high)
    slider.setValue(value)
    row.addWidget(title, 0, 0)
    row.addWidget(value_label, 0, 1, alignment=Qt.AlignmentFlag.AlignRight)
    row.addWidget(slider, 1, 0, 1, 2)
    slider.valueChanged.connect(lambda v: (value_label.setText(str(v)), callback(v)))
    return holder, slider, value_label


class CameraGridDialog(QDialog):
    """Assign detected cameras to the four cells of a 2x2 scene."""

    SLOT_LABELS = ("Top left", "Top right", "Bottom left", "Bottom right")

    def __init__(self, choices: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("New camera grid")
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Assign a camera to each 2\u00d72 cell. Empty cells stay blank; "
            "at least three cameras must be selected."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.combos: list[QComboBox] = []
        for index, label in enumerate(self.SLOT_LABELS):
            combo = QComboBox()
            combo.addItem("Empty", "")
            for name, device in choices:
                combo.addItem(name, device)
            if index < len(choices):
                combo.setCurrentIndex(index + 1)
            self.combos.append(combo)
            form.addRow(label, combo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def slots(self) -> list[str]:
        return [str(combo.currentData() or "") for combo in self.combos]

    def _accept_if_valid(self) -> None:
        devices = [device for device in self.slots() if device]
        if len(devices) < 3:
            QMessageBox.warning(self, "Camera grid",
                                "Select at least three cameras for the grid.")
            return
        if len(set(devices)) != len(devices):
            QMessageBox.warning(self, "Camera grid",
                                "Each grid cell must use a different camera.")
            return
        self.accept()


class AnyCamWindow(QMainWindow):
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
        self.recording = False
        self._recorder: Recorder | None = None
        self._recording_path = ""
        self._vcam_output: VcamOutput | None = None
        self.vcam_on = False
        # Streaming is a separate action: preview must never publish a saved URL.
        self.live = False
        self.audio_on = bool(getattr(self.project.audio, "mic_enabled", False))
        self._screen_error_shown = ""
        # Layer currently built without its crop because the editor is open.
        self._crop_preview_layer_id = ""
        self._screen_sessions: dict[str, portal.ScreenSession] = {}
        self._camera_choices: list[tuple[str, str]] = []
        self._face_detector = None
        self._face_layer_id = next(
            (layer.id for layer in self.scene.layers if layer.kind == "face"), "")
        self._face_last = 0.0
        self._face_size = (0, 0)
        self._frame_bytes = b""
        self._face_error = ""
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
        self._sync_face_overlay()
        self._install_shortcuts()
        self._pending_frame = None
        self.frame_ready.connect(self._update_preview)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._started_at = 0.0
        self._render_seconds = 0.0

    def _install_shortcuts(self) -> None:
        """Keyboard shortcuts for the actions an operator needs mid-stream."""
        bindings = {
            "Ctrl+P": self.toggle_engine,
            "Ctrl+R": self.toggle_record,
            "Ctrl+Shift+V": self.toggle_vcam,
            "F5": self.snapshot,
            "Ctrl+M": self.toggle_mic_mute,
            "Ctrl+Z": self.undo,
            "Ctrl+Shift+Z": self.redo,
            "Ctrl+Y": self.redo,
            "Ctrl+D": self.duplicate_layer,
            "Ctrl+Alt+Left": lambda: self._align_layers("left"),
            "Ctrl+Alt+Right": lambda: self._align_layers("right"),
            "Ctrl+Alt+Up": lambda: self._align_layers("top"),
            "Ctrl+Alt+Down": lambda: self._align_layers("bottom"),
            "Ctrl+Alt+H": lambda: self._align_layers("hcenter"),
            "Ctrl+Alt+V": lambda: self._align_layers("vcenter"),
        }
        for index in range(1, 10):
            bindings[f"Ctrl+{index}"] = lambda i=index - 1: self.switch_scene(i)
        self.shortcuts = []
        for key, callback in bindings.items():
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)

    def switch_scene(self, index: int) -> None:
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
            if self.engine is not None:
                self.restart_engine()

    def toggle_mic_mute(self) -> None:
        muted = not self.project.audio.mic_muted
        self.project.audio.mic_muted = muted
        if hasattr(self, "mute_check"):
            self.mute_check.blockSignals(True)
            self.mute_check.setChecked(muted)
            self.mute_check.blockSignals(False)
        self._mark_dirty()
        if self.engine is not None:
            self.engine.set_mic_muted(muted)
        self._status("Microphone muted" if muted else "Microphone live")

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
        preview_layout.addWidget(self.preview, 1)
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
        for button in (self.start_button, self.record_button, self.vcam_button, self.snapshot_button):
            controls.addWidget(button)
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
        side.addTab(self._audio_tab(), "Audio")
        side.addTab(self._playlist_tab(), "Playlist")
        side.addTab(self._output_tab(), "Output")
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 1)

        self.setStatusBar(QStatusBar())
        if self._load_warning:
            self._status(self._load_warning)
        else:
            self._status("Ready. No virtual camera output yet.")

    def _load_saved_project(self) -> Project:
        path = config.project_path()
        if not path.exists():
            return default_project()
        try:
            return Project.load(path)
        except Exception as exc:  # noqa: BLE001 - a corrupt save must not brick the app
            # Keep the unreadable file: the next autosave writes defaults over
            # the path, and the old project may still be recoverable by hand.
            try:
                shutil.copy2(path, path.with_name(f"{path.name}.corrupt"))
            except OSError:
                pass
            self._load_warning = f"Could not load {path}: {exc}. Using defaults."
            return default_project()

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
        two_camera = QPushButton("Add two-camera scene")
        two_camera.clicked.connect(self.add_two_camera_scene)
        layout.addWidget(two_camera)
        grid = QPushButton("Add camera grid...")
        grid.clicked.connect(self.add_camera_grid_scene)
        layout.addWidget(grid)
        self._refresh_scene_list()
        return page

    def _layers_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.layer_list.itemChanged.connect(self._layer_visibility)
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
            spin.setRange(0.0, 1.0)
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
                   ("Screen", "screen"), ("Network URL", "network"))
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

    def _effects_tab(self) -> QWidget:
        page = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        fx = self._current_effects()

        box = QGroupBox("Colour")
        box_layout = QVBoxLayout(box)
        for label, low, high, value, attr in (
            ("Brightness", -100, 100, int(fx.brightness * 100), "brightness"),
            ("Contrast", 0, 300, int(fx.contrast * 100), "contrast"),
            ("Saturation", 0, 300, int(fx.saturation * 100), "saturation"),
            ("Blur", 0, 40, int(fx.blur), "blur"),
        ):
            holder, slider, _label = _slider_row(label, low, high, value,
                                                 lambda v, a=attr: self._set_effect(a, v))
            setattr(self, f"slider_{attr}", slider)
            box_layout.addWidget(holder)
        layout.addWidget(box)

        chroma = QGroupBox("Chroma key")
        chroma_layout = QGridLayout(chroma)
        self.chroma_enabled = QCheckBox("Enable")
        self.chroma_enabled.setChecked(fx.chroma_enabled)
        self.chroma_enabled.toggled.connect(lambda v: self._set_effect("chroma_enabled", v))
        self.chroma_color = QLineEdit(fx.chroma_key)
        self.chroma_color.setMaxLength(9)
        self.chroma_color.editingFinished.connect(
            lambda: self._set_effect("chroma_key", self.chroma_color.text()))
        chroma_layout.addWidget(self.chroma_enabled, 0, 0, 1, 2)
        chroma_layout.addWidget(QLabel("Key colour"), 1, 0)
        chroma_layout.addWidget(self.chroma_color, 1, 1)
        holder, slider, _ = _slider_row("Tolerance", 0, 100, int(fx.chroma_similarity * 100),
                                        lambda v: self._set_effect("chroma_similarity", v / 100.0))
        chroma_layout.addWidget(holder, 2, 0, 1, 2)
        layout.addWidget(chroma)

        background = QGroupBox("Background (not implemented)")
        background.setEnabled(False)
        bg_layout = QGridLayout(background)
        self.background_mode = QComboBox()
        self.background_mode.addItems(["none", "blur", "replace"])
        self.background_mode.setCurrentText(fx.background_mode)
        self.background_mode.currentTextChanged.connect(lambda v: self._set_effect("background_mode", v))
        bg_layout.addWidget(QLabel("Mode"), 0, 0)
        bg_layout.addWidget(self.background_mode, 0, 1)
        holder, slider, _ = _slider_row("Blur strength", 0, 40, int(fx.background_blur),
                                        lambda v: self._set_effect("background_blur", float(v)))
        bg_layout.addWidget(holder, 1, 0, 1, 2)
        background_note = QLabel("Background blur/replacement needs segmentation; "
                                 "the controls are disabled in this build.")
        background_note.setObjectName("muted")
        background_note.setWordWrap(True)
        bg_layout.addWidget(background_note, 2, 0, 1, 2)
        layout.addWidget(background)

        face = QGroupBox("Face effect")
        face_layout = QGridLayout(face)
        self.face_effect = QComboBox()
        self.face_effect.addItems(["none", "spotlight", "frame"])
        self.face_effect.setCurrentText(fx.face_effect)
        self.face_effect.currentTextChanged.connect(lambda v: self._set_effect("face_effect", v))
        face_layout.addWidget(QLabel("Effect"), 0, 0)
        face_layout.addWidget(self.face_effect, 0, 1)
        holder, _slider, _label = _slider_row(
            "Padding", 0, 100, int(fx.face_strength * 100),
            lambda v: self._set_effect("face_strength", v / 100.0))
        face_layout.addWidget(holder, 1, 0, 1, 2)
        face_note = QLabel("The bundled YuNet detector runs locally on preview frames. "
                           "The compositor overlay follows the largest detected face; "
                           "no image leaves this machine.")
        face_note.setObjectName("muted")
        face_note.setWordWrap(True)
        face_layout.addWidget(face_note, 2, 0, 1, 2)
        layout.addWidget(face)
        layout.addStretch(1)

        apply = QPushButton("Apply effects")
        apply.setObjectName("primary")
        apply.clicked.connect(self.restart_engine)
        layout.addWidget(apply)
        scroll.setWidget(body)
        container = QVBoxLayout(page)
        container.addWidget(scroll)
        return page

    def _audio_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        box = QGroupBox("Microphone")
        grid = QGridLayout(box)
        self.audio_check = QCheckBox("Include microphone in recordings and streams")
        self.audio_check.setChecked(self.audio_on)
        self.audio_check.toggled.connect(self._toggle_audio)
        grid.addWidget(self.audio_check, 0, 0, 1, 2)

        self.mic_combo = QComboBox()
        for source in config.list_audio_sources():
            label = source.description or source.name
            self.mic_combo.addItem(label, source.name)
        current = self.project.audio.mic_device or config.microphone()
        index = self.mic_combo.findData(current)
        if index >= 0:
            self.mic_combo.setCurrentIndex(index)
        self.mic_combo.currentIndexChanged.connect(self._mic_changed)
        grid.addWidget(QLabel("Device"), 1, 0)
        grid.addWidget(self.mic_combo, 1, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_mics)
        grid.addWidget(refresh, 1, 2)

        self.mic_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_gain_slider.setRange(-30, 20)
        self.mic_gain_slider.setValue(int(self.project.audio.mic_gain_db))
        self.mic_gain_slider.valueChanged.connect(self._mic_gain_changed)
        self.mic_gain_label = QLabel(f"{self.project.audio.mic_gain_db:+.0f} dB")
        self.mic_gain_label.setObjectName("accent")
        gain_row = QHBoxLayout()
        gain_row.addWidget(self.mic_gain_slider, 1)
        gain_row.addWidget(self.mic_gain_label)
        grid.addWidget(QLabel("Gain"), 2, 0)
        grid.addLayout(gain_row, 2, 1)

        self.mute_check = QCheckBox("Mute microphone")
        self.mute_check.setChecked(self.project.audio.mic_muted)
        self.mute_check.toggled.connect(self._mic_mute_changed)
        grid.addWidget(self.mute_check, 3, 0, 1, 2)
        self.mic_level_label = QLabel("—")
        self.mic_level_label.setObjectName("accent")
        grid.addWidget(QLabel("Level"), 4, 0)
        grid.addWidget(self.mic_level_label, 4, 1)
        layout.addWidget(box)

        box2 = QGroupBox("Media audio (mixed with the mic)")
        grid2 = QGridLayout(box2)
        self.media_edit = QLineEdit(self.project.audio.media_file)
        self.media_edit.textChanged.connect(lambda *_: self._mark_dirty())
        browse = QPushButton("Browse")
        browse.clicked.connect(self._pick_media)
        grid2.addWidget(QLabel("File"), 0, 0)
        grid2.addWidget(self.media_edit, 0, 1)
        grid2.addWidget(browse, 0, 2)
        self.media_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.media_gain_slider.setRange(-30, 20)
        self.media_gain_slider.setValue(int(self.project.audio.media_gain_db))
        self.media_gain_slider.valueChanged.connect(self._media_gain_changed)
        self.media_gain_label = QLabel(f"{self.project.audio.media_gain_db:+.0f} dB")
        self.media_gain_label.setObjectName("accent")
        media_row = QHBoxLayout()
        media_row.addWidget(self.media_gain_slider, 1)
        media_row.addWidget(self.media_gain_label)
        grid2.addWidget(QLabel("Gain"), 1, 0)
        grid2.addLayout(media_row, 1, 1, 1, 2)
        note = QLabel("Media audio is decoded and mixed with the microphone through "
                      "an audiomixer; both inputs are resampled to 48 kHz stereo.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        grid2.addWidget(note, 2, 0, 1, 3)
        layout.addWidget(box2)
        layout.addStretch(1)
        return page

    def _refresh_mics(self) -> None:
        current = self.mic_combo.currentData() or self.project.audio.mic_device
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        for source in config.list_audio_sources():
            self.mic_combo.addItem(source.description or source.name, source.name)
        index = self.mic_combo.findData(current)
        if index >= 0:
            self.mic_combo.setCurrentIndex(index)
        self.mic_combo.blockSignals(False)
        self._status(f"{self.mic_combo.count()} microphone(s) found")

    def _toggle_audio(self, enabled: bool) -> None:
        self.audio_on = enabled
        self.project.audio.mic_enabled = enabled
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        self._status("Microphone enabled" if enabled else "Microphone disabled")

    def _mic_changed(self, _index: int) -> None:
        self.project.audio.mic_device = self.mic_combo.currentData() or ""
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

    def _mic_gain_changed(self, value: int) -> None:
        self.project.audio.mic_gain_db = float(value)
        self.mic_gain_label.setText(f"{value:+d} dB")
        self._mark_dirty()
        if self.engine is not None:
            self.engine.set_mic_gain(float(value))

    def _mic_mute_changed(self, muted: bool) -> None:
        self.project.audio.mic_muted = bool(muted)
        self._mark_dirty()
        if self.engine is not None:
            self.engine.set_mic_muted(bool(muted))

    def _media_gain_changed(self, value: int) -> None:
        self.project.audio.media_gain_db = float(value)
        self.media_gain_label.setText(f"{value:+d} dB")
        self._mark_dirty()
        if self.engine is not None and self.engine.media_volume is not None:
            self.engine.media_volume.set_property("volume", 10 ** (value / 20.0))

    def _pick_media(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose audio", "", "Audio (*.wav *.mp3 *.ogg *.flac *.m4a *.aac)")
        if path:
            self.media_edit.setText(path)
            self.project.audio.media_file = path
            self._mark_dirty()
            if self.engine is not None:
                self.restart_engine()

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
        row = QHBoxLayout()
        add = QPushButton("+ Add media")
        add.clicked.connect(self._add_playlist_items)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_playlist)
        row.addWidget(add)
        row.addWidget(clear)
        layout.addLayout(row)
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
        layout.addLayout(grid)
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
        self.playlist_list.clear()
        for layer_id in ids:
            layer = self.scene.layer(layer_id)
            if layer is not None:
                self.playlist_list.addItem(f"{layer.name}  ·  {layer.kind}")
        self.playlist_list.setCurrentRow(index if index >= 0 else -1)

    def _store_playlist_state(self) -> None:
        state = self.scene.playlist
        valid = {layer.id for layer in self.scene.layers}
        state.layer_ids = [layer_id for layer_id in self.playlist_ids if layer_id in valid]
        state.interval = float(self.playlist_interval.value())
        state.loop = bool(self.playlist_enabled.isChecked())
        state.index = int(self.playlist_index)

    def _playlist_settings_changed(self, _value=None) -> None:
        self._store_playlist_state()
        self.playlist_last = time.monotonic()
        self._mark_dirty()

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

    def _advance_playlist(self, now: float) -> None:
        if self.engine is None or not self.playlist_ids:
            return
        interval = self.playlist_interval.value()
        if now - self.playlist_last < interval:
            return
        self.playlist_last = now
        if self.playlist_index + 1 >= len(self.playlist_ids):
            if not self.playlist_enabled.isChecked():
                return
            next_index = 0
        else:
            next_index = self.playlist_index + 1
        self.playlist_index = next_index
        self.scene.playlist.index = next_index
        for position, layer_id in enumerate(self.playlist_ids):
            self.engine.set_layer_visible(layer_id, position == self.playlist_index)
        self.playlist_list.setCurrentRow(self.playlist_index)

    def _output_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        box = QGroupBox("Canvas")
        grid = QGridLayout(box)
        self.width_spin = QSpinBox(); self.width_spin.setRange(320, 3840); self.width_spin.setValue(self.project.output.canvas_w)
        self.height_spin = QSpinBox(); self.height_spin.setRange(240, 2160); self.height_spin.setValue(self.project.output.canvas_h)
        self.fps_spin = QSpinBox(); self.fps_spin.setRange(1, 60); self.fps_spin.setValue(self.project.output.fps)
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

        box2 = QGroupBox("Virtual camera")
        grid2 = QGridLayout(box2)
        self.vcam_edit = QLineEdit(self.project.output.vcam_device)
        self.vcam_edit.textChanged.connect(lambda *_: self._mark_dirty())
        grid2.addWidget(QLabel("Device"), 0, 0)
        grid2.addWidget(self.vcam_edit, 0, 1)
        self.vcam_note = QLabel("Run scripts/enable-v4l2loopback.sh once (needs sudo).")
        self.vcam_note.setObjectName("muted")
        self.vcam_note.setWordWrap(True)
        grid2.addWidget(self.vcam_note, 1, 0, 1, 2)
        layout.addWidget(box2)

        box3 = QGroupBox("Stream")
        grid3 = QGridLayout(box3)
        self.rtmp_edit = QLineEdit(self.project.output.rtmp_url)
        self.rtmp_edit.setPlaceholderText("rtmp://host/live/stream-key")
        self.rtmp_edit.textChanged.connect(lambda *_: self._mark_dirty())
        grid3.addWidget(QLabel("RTMP URL"), 0, 0)
        grid3.addWidget(self.rtmp_edit, 0, 1)
        self.live_button = QPushButton("Go Live")
        self.live_button.setObjectName("primary")
        self.live_button.clicked.connect(self.toggle_live)
        grid3.addWidget(self.live_button, 1, 0, 1, 2)
        live_note = QLabel("The saved URL is used only while Go Live is on; "
                           "starting preview never publishes.")
        live_note.setObjectName("muted")
        live_note.setWordWrap(True)
        grid3.addWidget(live_note, 2, 0, 1, 2)
        layout.addWidget(box3)

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
        layout.addWidget(box4)

        save = QPushButton("Save project")
        save.clicked.connect(self.save_project)
        layout.addWidget(save)
        reload_button = QPushButton("Reload saved project")
        reload_button.clicked.connect(self.reload_project)
        layout.addWidget(reload_button)
        layout.addStretch(1)
        return page

    # -------------------------------------------------------------- project
    @property
    def scene(self) -> Scene:
        return self.project.scene

    def _current_effects(self) -> Effects:
        for layer in self.scene.ordered():
            if layer.kind in ("camera", "test", "video", "screen", "network"):
                return layer.effects
        return self.scene.layers[0].effects if self.scene.layers else Effects()

    def _effects_layer(self) -> Layer | None:
        for layer in self.scene.ordered():
            if layer.kind in ("camera", "test", "video", "screen", "network"):
                return layer
        return None

    def _set_effect(self, attr: str, value) -> None:
        layer = self._effects_layer()
        if layer is None:
            return
        if attr == "brightness":
            layer.effects.brightness = value / 100.0
        elif attr in ("contrast", "saturation"):
            setattr(layer.effects, attr, value / 100.0)
        elif attr == "blur":
            layer.effects.blur = float(value)
        elif attr == "face_strength":
            layer.effects.face_strength = max(0.0, min(1.0, float(value)))
        else:
            setattr(layer.effects, attr, value)
        if attr == "face_effect":
            changed = self._sync_face_overlay()
            if changed and self.engine is not None:
                self.restart_engine()
        self._mark_dirty()

    def _face_target(self) -> Layer | None:
        """The camera-like layer whose effects currently drive the face overlay."""
        for layer in self.scene.ordered():
            if layer.kind in ("camera", "test", "video", "screen", "network") \
                    and layer.effects.face_effect != "none":
                return layer
        return None

    def _adopt_face_layer(self) -> None:
        self._face_layer_id = next(
            (layer.id for layer in self.scene.layers if layer.kind == "face"), "")
        self._sync_face_overlay()

    def _sync_face_overlay(self) -> bool:
        """Add, refresh or remove the compositor layer for the face effect.

        Returns True when the scene changed and a running graph needs a rebuild.
        """
        target = self._face_target()
        existing = self.scene.layer(self._face_layer_id) if self._face_layer_id else None
        if existing is None:
            existing = next(
                (layer for layer in self.scene.layers if layer.kind == "face"), None)
            self._face_layer_id = existing.id if existing is not None else ""

        if target is None:
            if existing is None:
                return False
            self.scene.remove_layer(existing.id)
            self._face_layer_id = ""
            self._refresh_layers()
            self._mark_dirty()
            return True

        style = target.effects.face_effect
        png = overlay.render_face_frame(style=style)
        if existing is None:
            layer = Layer(
                new_id("layer"), "face", "Face overlay", source=str(png),
                box=(0.32, 0.22, 0.36, 0.48), z=90, visible=False,
            )
            self.scene.add_layer(layer)
            self._face_layer_id = layer.id
            self._refresh_layers()
            self._mark_dirty()
            return True
        if existing.source != str(png):
            existing.source = str(png)
            self._mark_dirty()
            return True
        return False

    def _track_face(self) -> None:
        """Run YuNet on the latest composited frame and move the face layer."""
        if self.engine is None or not self._face_layer_id or not self._face_bytes():
            return
        now = time.monotonic()
        if now - self._face_last < 0.2:
            return
        self._face_last = now
        layer = self.scene.layer(self._face_layer_id)
        target = self._face_target()
        if layer is None or target is None:
            return
        if self._face_detector is None:
            try:
                from .face import FaceDetector

                self._face_detector = FaceDetector()
            except Exception as exc:  # noqa: BLE001 - optional onnxruntime/model
                self._face_detector = False
                if not self._face_error:
                    self._face_error = str(exc)
                    self._status(f"Face overlay unavailable: {exc}")
                return
        if self._face_detector is False:
            return
        import numpy as np

        width, height, raw = self._face_size[0], self._face_size[1], self._frame_bytes
        if not width or not height or not raw:
            return
        try:
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)[:, :, :3]
            faces = self._face_detector.detect(frame)
        except Exception as exc:  # noqa: BLE001 - inference must not kill preview
            if not self._face_error:
                self._face_error = str(exc)
                self._status(f"Face detection failed: {exc}")
            return
        if not faces:
            if layer.visible:
                self.engine.set_layer_visible(layer.id, False)
            return
        face = max(faces, key=lambda item: item.score)
        pad = 0.08 + 0.22 * max(0.0, min(1.0, float(target.effects.face_strength)))
        box = (
            clamp(face.x - face.w * pad),
            clamp(face.y - face.h * pad),
            clamp(face.w * (1.0 + 2.0 * pad), 0.03, 1.0),
            clamp(face.h * (1.0 + 2.0 * pad), 0.03, 1.0),
        )
        self.engine.set_layer_box(layer.id, box)
        self.engine.set_layer_visible(layer.id, True)

    def _face_bytes(self) -> bool:
        return bool(self._frame_bytes) and bool(self._face_size[0])

    def _refresh_scenes(self) -> None:
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

    def _refresh_layers(self) -> None:
        if not hasattr(self, "layer_list"):
            return
        selected = list(self.preview.selected_ids) if hasattr(self, "preview") else []
        primary = self.preview.selected_id if hasattr(self, "preview") else ""
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for layer in sorted(self.scene.layers, key=lambda l: l.z):
            item = QListWidgetItem(f"{layer.name}  ·  {layer.kind}")
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

    def _scan_camera_choices(self) -> list[tuple[str, str]]:
        """Return (label, device) for usable V4L2 capture nodes."""
        choices: list[tuple[str, str]] = []
        seen: set[str] = set()
        # Never offer AnyCam's own virtual camera as an input; selecting it
        # would feed the compositor output back into itself.
        vcam = self.project.output.vcam_device or config.loopback_device()
        for camera in config.list_video_devices():
            stable = config.camera_identity(camera.device)
            if not camera.formats or stable in seen:
                continue
            name = camera.name or "Camera"
            if camera.device == vcam or stable == vcam or "anycam" in name.lower():
                continue
            # The label stays human-readable; the stored value is the stable
            # path so the combo still matches after a replug.
            choices.append((f"{name} · {camera.device}", stable))
            seen.add(stable)
        default = config.camera_identity(config.camera_device())
        if (default and default not in seen and default != vcam
                and Path(default).exists()):
            choices.insert(0, (f"Default · {default}", default))
        return choices

    def _refresh_cameras(self) -> None:
        """Rescan V4L2 devices without disturbing the selected layer."""
        if not hasattr(self, "camera_combo"):
            return
        self._camera_choices = self._scan_camera_choices()
        layer = self.preview.selected_layer()
        self._sync_camera_combo(layer.source if layer and layer.kind == "camera" else "")
        self.camera_combo.setEnabled(layer is not None and layer.kind == "camera")

    def _sync_camera_combo(self, source: str) -> None:
        choices = list(self._camera_choices)
        if source and all(device != source for _label, device in choices):
            choices.insert(0, (f"Unavailable · {source}", source))
        if not choices:
            choices = [("No V4L2 camera detected", "")]
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        for label, device in choices:
            self.camera_combo.addItem(label, device)
        index = self.camera_combo.findData(source) if source else 0
        self.camera_combo.setCurrentIndex(index if index >= 0 else 0)
        self.camera_combo.blockSignals(False)

    def _camera_device_changed(self, _index: int) -> None:
        layer = self.preview.selected_layer()
        source = self.camera_combo.currentData()
        if (layer is None or layer.kind != "camera" or not source
                or layer.source == source):
            return
        layer.source = config.camera_identity(str(source))
        self._lost_cameras.pop(layer.id, None)
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        self._status(f"Camera source: {source}")

    def _camera_mode_changed(self, _value: int = 0) -> None:
        layer = self.preview.selected_layer()
        if layer is None or layer.kind != "camera":
            return
        values = (self.camera_width.value(), self.camera_height.value(),
                  self.camera_fps.value())
        if (layer.camera_width, layer.camera_height, layer.camera_fps) == values:
            return
        layer.camera_width, layer.camera_height, layer.camera_fps = values
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        width, height, fps = values
        mode = (f"{width}×{height}" if width and height else "auto size")
        self._status(f"Camera capture: {mode} @ {fps or 'auto'} fps")

    # --------------------------------------------------- camera reconnection
    def _missing_camera_layers(self) -> dict[str, str]:
        """Camera layers whose V4L2 node is gone, mapped from layer id to device."""
        return {
            layer.id: layer.source
            for layer in self.scene.layers
            if layer.kind == "camera" and layer.source
            and not Path(layer.source).exists()
        }

    def _camera_error_layers(self, message: str) -> dict[str, str]:
        """Map a pipeline error to camera layers even when the node still exists.

        UVC/Logitech devices can stay present but refuse a new stream after too
        many opens.  Quarantining them like an unplugged camera puts a
        placeholder in the graph and lets the reconnect watch retry with
        backoff instead of stopping the preview until a reboot.
        """
        cameras = {layer.id: layer.source for layer in self.scene.layers
                   if layer.kind == "camera" and layer.source}
        if not cameras:
            return {}
        text = message.lower()
        explicit = {layer_id: device for layer_id, device in cameras.items()
                    if device.lower() in text}
        if explicit:
            return explicit
        markers = (
            "v4l2", "video4linux", "resource busy", "no space left",
            "could not open device", "failed to allocate", "input/output error",
            "not a capture device",
        )
        return cameras if any(marker in text for marker in markers) else {}

    def _handle_pipeline_error(self, message: str) -> None:
        """Treat an unplugged or wedged camera as a temporary loss."""
        missing = self._missing_camera_layers()
        if not missing:
            missing = self._camera_error_layers(message)
        if not missing:
            self._status(message)
            self.stop_engine()
            self._status(message)  # keep the failure visible after finalisation
            return
        self._lost_cameras.update(missing)
        # A camera that fails again after a reconnect must not be reopened in
        # a tight loop; repeated opens are what wedges some UVC devices.
        self._camera_retry_ms = min(10000, int(self._camera_retry_ms * 1.5))
        self._rebuild_without_lost_cameras(message)

    def _rebuild_without_lost_cameras(self, message: str = "") -> None:
        """Rebuild with a placeholder card for each lost camera."""
        if self.engine is not None:
            self.stop_engine()
        devices = ", ".join(sorted(set(self._lost_cameras.values())))
        live = [layer for layer in self.scene.layers
                if layer.id not in self._lost_cameras]
        prefix = f"{message} · " if message else ""
        self._quiet_engine_errors = True
        try:
            self.start_engine()
        finally:
            self._quiet_engine_errors = False
        if self.engine is None:
            self._status(f"{prefix}{devices} disconnected · preview could not restart")
            self._camera_watch.start(self._camera_retry_ms)
            return
        summary = ("placeholder shown; other layers live" if live
                   else "showing placeholders")
        self._status(f"{prefix}{devices} disconnected · {summary}")
        self._camera_watch.start(self._camera_retry_ms)

    def _reconnect_cameras(self) -> None:
        """Rebuild the preview when a lost camera device reappears."""
        if not self._lost_cameras:
            self._camera_watch.stop()
            return
        restored = {layer_id: device
                    for layer_id, device in self._lost_cameras.items()
                    if Path(device).exists()}
        if not restored:
            return
        for layer_id in restored:
            del self._lost_cameras[layer_id]
        self._quiet_engine_errors = True
        try:
            if self.engine is not None:
                self.restart_engine()
            else:
                self.start_engine()
        finally:
            self._quiet_engine_errors = False
        if self.engine is None:
            # The node exists but the capture did not come back (UVC needs a
            # moment after a replug, or the device is still busy).  Keep the
            # placeholder and retry with backoff instead of giving up; giving up
            # here is what used to force a reboot.
            self._lost_cameras.update(restored)
            self._camera_retry_ms = min(10000, int(self._camera_retry_ms * 1.7))
            self._camera_watch.start(self._camera_retry_ms)
            self._status(
                "Camera returned but the preview could not restart; "
                f"retrying in {self._camera_retry_ms / 1000:.1f}s"
            )
            return
        # Do not rescan V4L2 here: ``v4l2-ctl`` opens every node, exactly while
        # the camera we just reopened is negotiating its stream.  The combo
        # already holds the stable by-id path.
        if self._lost_cameras:
            self._camera_watch.start(self._camera_retry_ms)
        else:
            self._camera_watch.stop()
        devices = ", ".join(sorted(set(restored.values())))
        self._status(f"Camera reconnected: {devices}")

    def _forget_lost_cameras(self) -> None:
        """Drop reconnect state when the scene or the preview changes."""
        self._lost_cameras.clear()
        self._camera_retry_ms = 2000
        self._camera_watch.stop()

    def _choose_camera_device(self) -> str:
        if not self._camera_choices:
            self._camera_choices = self._scan_camera_choices()
        choices = self._camera_choices
        if not choices:
            self._status("No V4L2 camera detected")
            return ""
        if len(choices) == 1:
            return choices[0][1]
        labels = [label for label, _device in choices]
        label, accepted = QInputDialog.getItem(
            self, "Camera source", "V4L2 device:", labels, 0, False)
        if not accepted or not label:
            return ""
        return choices[labels.index(label)][1]

    def _camera_layer_name(self, source: str) -> str:
        for label, device in self._camera_choices:
            if device == source:
                return label.split(" · ", 1)[0]
        return "Camera"

    def _scene_changed(self, _index: int) -> None:
        scene_id = self.scene_combo.currentData()
        if not scene_id or scene_id == self.project.active_scene:
            return
        self._store_playlist_state()
        self._forget_lost_cameras()
        self.project.active_scene = scene_id
        self._clear_history()
        self._refresh_scene_list()
        self._refresh_layers()
        self._load_playlist_state()
        self._adopt_face_layer()
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

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
        self._clear_history()
        self._refresh_scenes()
        self._refresh_layers()
        self._load_playlist_state()
        self._adopt_face_layer()
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

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
        self._adopt_face_layer()
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

    def remove_scene(self) -> None:
        if len(self.project.scenes) <= 1:
            return
        scene_id = self.project.active_scene
        removed = next((s for s in self.project.scenes if s.id == scene_id), None)
        screen_ids = ([layer.id for layer in removed.layers
                       if layer.id in self._screen_sessions] if removed else [])
        self.project.scenes = [s for s in self.project.scenes if s.id != scene_id]
        self.project.active_scene = self.project.scenes[0].id
        self._forget_lost_cameras()
        self._clear_history()
        self._refresh_scenes()
        self._refresh_scene_list()
        self._refresh_layers()
        self._load_playlist_state()
        self._adopt_face_layer()
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        for layer_id in screen_ids:
            self._close_screen_session(layer_id)

    def add_layer(self, kind: str, source: str = "") -> Layer | None:
        """Add a source layer, using a picker only when no source is supplied."""
        if kind == "camera":
            if not source:
                source = (config.camera_device() if self.headless
                          else self._choose_camera_device())
            if not source:
                return None
            source = config.camera_identity(source)
            layer = Layer(new_id("layer"), "camera", self._camera_layer_name(source),
                          source=source, box=(0.62, 0.60, 0.34, 0.34), z=30)
        elif kind == "test":
            layer = Layer(new_id("layer"), "test", "Test pattern", box=(0.62, 0.60, 0.34, 0.34), z=30)
        elif kind == "text":
            layer = Layer(new_id("layer"), "text", "Caption", source="Your text here",
                          box=(0.05, 0.75, 0.30, 0.08), z=30, font_size=0.04)
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
            if kind == "video":
                box = (0.62, 0.60, 0.34, 0.34)
            else:
                box = (0.04, 0.04, 0.18, 0.18)
            layer = Layer(new_id("layer"), kind, path.stem, source=str(path),
                          box=box, z=30)
        elif kind == "network":
            if not source:
                source, ok = QInputDialog.getText(
                    self, "Network source", "RTSP or HTTP URL:")
                if not ok:
                    return None
            uri = source.strip()
            parsed = urlparse(uri)
            if (parsed.scheme.lower() not in ("rtsp", "rtsps", "http", "https")
                    or not parsed.netloc):
                self._status("Network source must be an rtsp://, rtsps://, http:// or https:// URL")
                return None
            layer = Layer(new_id("layer"), "network", parsed.netloc or "Network",
                          source=uri, box=(0.0, 0.0, 1.0, 1.0), z=30)
        elif kind == "screen":
            return self._add_screen_layer()
        else:
            return None
        return self._insert_layer(layer)

    def _insert_layer(self, layer: Layer) -> Layer:
        before = self._layout_snapshot()
        self.scene.add_layer(layer)
        self._refresh_layers()
        self.preview.select_layer(layer.id)
        self._record_history(before)
        if self.engine is not None:
            self.restart_engine()
        return layer

    def _add_screen_layer(self) -> Layer | None:
        """Ask the portal for a screen stream and add it as a layer."""
        try:
            session = portal.request_screen_cast()
        except Exception as exc:  # noqa: BLE001 - the portal reports user cancellation too
            self._status(f"Screen capture unavailable: {exc}")
            if not self.headless:
                QMessageBox.warning(self, "Screen capture", str(exc))
            return None
        stream = session.streams[0]
        size = f"{stream.width}×{stream.height}" if stream.width and stream.height else "Screen"
        layer = Layer(new_id("layer"), "screen", f"Screen {size}",
                      source=str(session.node_id), box=(0.0, 0.0, 1.0, 1.0), z=30)
        self._screen_sessions[layer.id] = session
        return self._insert_layer(layer)

    def _ensure_screen_sessions(self) -> bool:
        """Re-request portal streams for saved screen layers after a restart."""
        for layer in list(self.scene.layers):
            if layer.kind != "screen" or layer.id in self._screen_sessions:
                continue
            try:
                session = portal.request_screen_cast()
            except Exception as exc:  # noqa: BLE001
                self._status(f"Screen capture unavailable: {exc}")
                return False
            layer.source = str(session.node_id)
            self._screen_sessions[layer.id] = session
        return True

    def _screen_fds(self) -> dict[str, int]:
        return {layer_id: session.fd
                for layer_id, session in self._screen_sessions.items()
                if session.fd >= 0 and self.scene.layer(layer_id) is not None}

    def _close_screen_session(self, layer_id: str) -> None:
        session = self._screen_sessions.pop(layer_id, None)
        if session is None:
            return
        if not any(other is session for other in self._screen_sessions.values()):
            session.close()

    def _close_screen_sessions(self) -> None:
        sessions = list(self._screen_sessions.values())
        self._screen_sessions.clear()
        closed: set[int] = set()
        for session in sessions:
            if id(session) not in closed:
                session.close()
                closed.add(id(session))

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
        removed = [item.data(Qt.ItemDataRole.UserRole) for item in items]
        screen_ids = [layer_id for layer_id in removed
                      if layer_id in self._screen_sessions]
        before = self._layout_snapshot()
        removed_face = self._face_layer_id in removed
        if removed_face:
            target = self._face_target()
            if target is not None:
                target.effects.face_effect = "none"
        for layer_id in removed:
            self.scene.remove_layer(layer_id)
            self._lost_cameras.pop(layer_id, None)
            self.playlist_ids = [item for item in self.playlist_ids if item != layer_id]
        self.playlist_index = min(self.playlist_index, len(self.playlist_ids) - 1)
        self._face_layer_id = "" if removed_face else self._face_layer_id
        self._refresh_layers()
        self._record_history(before)
        if self.engine is not None:
            # Stop the old pipewiresrc before closing its portal fd.
            self.restart_engine()
        for layer_id in screen_ids:
            self._close_screen_session(layer_id)

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
        layer = self.scene.layer(layer_id) if layer_id else None
        if layer is not None:
            if len(self.preview.selected_ids) > 1:
                self._status(f"{len(self.preview.selected_ids)} layers selected")
            else:
                self._status(f"Selected {layer.name} · drag in the preview to edit")

    # ------------------------------------------------------------- undo/redo
    def _layout_snapshot(self) -> list[dict]:
        return LayoutHistory.snapshot(self.scene)

    def _record_history(self, before: list[dict] | None) -> None:
        if before is None:
            return
        if self.history.push(before, self._layout_snapshot()):
            self._sync_history_buttons()
            self._mark_dirty()

    def _sync_history_buttons(self) -> None:
        if hasattr(self, "undo_button"):
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)

    def _clear_history(self) -> None:
        self.history.clear()
        self._layout_before_edit = None
        self._opacity_before = None
        self._transform_timer.stop()
        self._transform_pending = ""
        self._sync_history_buttons()

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

    def undo(self) -> None:
        snapshot = self.history.undo(self._layout_snapshot())
        if snapshot is None:
            self._status("Nothing to undo")
            return
        self._restore_layout(snapshot)
        self._sync_history_buttons()
        self._status("Undo")

    def redo(self) -> None:
        snapshot = self.history.redo(self._layout_snapshot())
        if snapshot is None:
            self._status("Nothing to redo")
            return
        self._restore_layout(snapshot)
        self._sync_history_buttons()
        self._status("Redo")

    def _restore_layout(self, snapshot: list[dict]) -> None:
        current = {layer.id: layer for layer in self.scene.layers}
        restored = []
        for state in snapshot:
            fresh = Layer.from_dict(state)
            existing = current.get(fresh.id)
            if existing is None:
                # A removed layer has no live object to update: rebuild it
                # from the full snapshot (source, effects, styling, ...).
                restored.append(fresh)
            else:
                # Keep object identity and only restore the layout fields the
                # history command owns, so an unrecorded effects or visibility
                # change is not silently rolled back by an undo.
                existing.box = fresh.box
                existing.crop = fresh.crop
                existing.alpha = fresh.alpha
                existing.z = fresh.z
                restored.append(existing)
        self.scene.layers = restored
        self._transform_timer.stop()
        self._transform_pending = ""
        self._layout_before_edit = None
        self._opacity_before = None
        self._refresh_layers()
        self._adopt_face_layer()
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()

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

    def _sync_transform_fields(self) -> None:
        if not hasattr(self, "box_spins"):
            return
        layer = self.preview.selected_layer()
        enabled = layer is not None
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
        if key in ("x", "y"):
            delta = 0.0
            if key == "x":
                delta = clamp(value, 0.0, 1.0 - w) - x
            else:
                delta = clamp(value, 0.0, 1.0 - h) - y
            for item in targets:
                ix, iy, iw, ih = item.box
                if key == "x":
                    item.box = (clamp(ix + delta, 0.0, 1.0 - iw), iy, iw, ih)
                else:
                    item.box = (ix, clamp(iy + delta, 0.0, 1.0 - ih), iw, ih)
        else:
            targets = [layer]
            if key == "w":
                w = clamp(value, PreviewWidget.MIN_BOX, 1.0 - x)
            else:
                h = clamp(value, PreviewWidget.MIN_BOX, 1.0 - y)
            layer.box = (x, y, w, h)
        self.preview.update()
        self._sync_transform_fields()
        self._record_history(before)
        if self.engine is not None:
            for item in targets:
                self.engine.set_layer_box(item.id, item.box)
        if any(item.kind in ("text", "logo", "image") for item in targets):
            self._schedule_transform_commit(layer.id)

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

    @staticmethod
    def _even(value: int) -> int:
        """Round down to an even size (H.264 cannot encode odd frames)."""
        return max(2, int(value) - int(value) % 2)

    def _write_project(self) -> Path:
        """Synchronise the widgets into the model and save the project."""
        self._store_playlist_state()
        self.project.output.canvas_w = self._even(self.width_spin.value())
        self.project.output.canvas_h = self._even(self.height_spin.value())
        self.project.output.fps = self.fps_spin.value()
        self.project.output.vcam_device = self.vcam_edit.text().strip()
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
        self._adopt_face_layer()
        for spin, value in ((self.width_spin, self.project.output.canvas_w),
                            (self.height_spin, self.project.output.canvas_h),
                            (self.fps_spin, self.project.output.fps)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
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
        self._sync_editor_controls()
        self._status(f"Loaded project from {path}")
        if was_running:
            self.start_engine()

    def _screen_sizes(self) -> dict[str, tuple[int, int]]:
        """Portal stream size per screen layer (crop base and node check)."""
        sizes: dict[str, tuple[int, int]] = {}
        for layer_id, session in self._screen_sessions.items():
            stream = session.streams[0] if session.streams else None
            if stream is not None and stream.width and stream.height:
                sizes[layer_id] = (int(stream.width), int(stream.height))
        return sizes

    def toggle_live(self) -> None:
        """Start/stop publishing without touching preview or recording."""
        url = self.rtmp_edit.text().strip()
        if not self.live and not url:
            self._status("Set an RTMP URL before going live")
            return
        self.live = not self.live
        self.live_button.setText("Stop stream" if self.live else "Go Live")
        if self.engine is not None:
            self.restart_engine()
        if self.live:
            self._status(f"Publishing to {config.redact_url(url)}")
        else:
            self._status("Stream stopped")

    # --------------------------------------------------------------- engine
    def _record_path(self) -> Path:
        """Path for the current recording session (one file, no segments)."""
        if self._recording_path:
            return Path(self._recording_path)
        stamp = self._session_stamp or overlay.timestamped_name("anycam", "")
        self._session_stamp = stamp
        path = config.recordings_dir(self.project.output.record_dir) / f"{stamp}.mp4"
        self._recording_path = str(path)
        return path

    def _start_recorder(self) -> bool:
        if self._recorder is not None:
            return True
        path = self._record_path()
        recorder = Recorder()
        audio = self.audio_on or bool(self.project.audio.media_file)
        if not recorder.start(str(path), self.project.output.canvas_w,
                              self.project.output.canvas_h, self.project.output.fps,
                              audio=audio):
            self._status(f"Recording could not start: {recorder.error}")
            return False
        self._recorder = recorder
        self._recording_path = str(path)
        return True

    def _finish_recorder(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        self._recorder = None
        report = recorder.stop()
        self._recording_path = ""
        path = str(report.get("path") or "")
        if report.get("ok") and path:
            if path not in self.recording_segments:
                self.recording_segments.append(path)
            self._status(f"Saved {path}")
        else:
            self._status(
                f"Recording failed: {report.get('error') or 'no frames'}")

    def _start_vcam_output(self) -> bool:
        if self._vcam_output is not None and self._vcam_output.active:
            return True
        device = self.project.output.vcam_device or config.loopback_device()
        output = VcamOutput()
        if not output.start(device, self.project.output.canvas_w,
                            self.project.output.canvas_h, self.project.output.fps):
            self._status(output.error or "Virtual camera could not start")
            return False
        self._vcam_output = output
        return True

    def _finish_vcam_output(self) -> None:
        output = self._vcam_output
        self._vcam_output = None
        if output is None:
            return
        report = output.stop()
        if report.get("error"):
            self._status(str(report["error"]))

    def start_engine(self) -> None:
        if self.engine is not None:
            return
        # A saved project may name a camera that is not plugged in yet.  Keep
        # its box as a placeholder and start the rest of the scene instead of
        # failing the whole preview.
        missing = self._missing_camera_layers()
        if missing:
            self._lost_cameras.update(missing)
            self._camera_watch.start()
        self._sync_face_overlay()
        if not self._ensure_screen_sessions():
            return
        self.project.output.canvas_w = self._even(self.width_spin.value())
        self.project.output.canvas_h = self._even(self.height_spin.value())
        self.project.output.fps = self.fps_spin.value()
        self.project.output.vcam_device = self.vcam_edit.text().strip() or config.loopback_device()
        self.project.output.rtmp_url = self.rtmp_edit.text().strip()
        self.project.audio.mic_enabled = self.audio_on
        self.project.audio.media_file = self.media_edit.text().strip()
        if self.playlist_index >= 0 and self.playlist_ids:
            for position, layer_id in enumerate(self.playlist_ids):
                layer = self.scene.layer(layer_id)
                if layer is not None:
                    layer.visible = position == self.playlist_index
        if self.recording and not self._start_recorder():
            self.recording = False
            self.record_button.setText("Record")
            return
        recorder = self._recorder
        if self.vcam_on and not self._start_vcam_output():
            self.vcam_on = False
            self.vcam_button.setText("Virtual camera")
        vcam_output = self._vcam_output if self.vcam_on else None
        # In crop mode the selected layer is built uncropped, so the editor's
        # window is drawn over the picture it is actually trimming.
        crop_preview = self.preview.selected_id if self.preview.edit_mode == "crop" else ""
        self._crop_preview_layer_id = crop_preview
        try:
            self.engine = AnyCamEngine(
                self.project,
                recorder=recorder,
                vcam=self.vcam_on,
                vcam_output=vcam_output,
                preview=True,
                audio=self.audio_on or bool(self.project.audio.media_file),
                # Only an explicit Go Live publishes; a saved URL is inert.
                rtmp=self.project.output.rtmp_url if self.live else None,
                screen_fds=self._screen_fds(),
                screen_sizes=self._screen_sizes(),
                preview_crop_layer=crop_preview,
                skip_layers=set(self._lost_cameras),
                pool=self._source_pool,
                on_frame=self._on_frame,
            ).build()
            self.engine.start()
            self.preview.layer_sizes = self.engine.source_sizes()
        except Exception as exc:  # noqa: BLE001
            self.engine = None
            # A failed rebuild must not leave the Record button armed with a
            # recorder writing in the background.
            if self.recording or self._recorder is not None:
                self.recording = False
                self.record_button.setText("Record")
                self._finish_recorder()
            message = f"Could not start the pipeline:\n{exc}"
            self._status(message.replace("\n", " "))
            if self.headless:
                print(message, file=sys.stderr, flush=True)
            elif not self._quiet_engine_errors:
                QMessageBox.critical(self, "AnyCam", message)
            return
        self._started_at = time.monotonic()
        self.playlist_last = self._started_at
        if self.playlist_index >= len(self.playlist_ids):
            self.playlist_index = len(self.playlist_ids) - 1
        live_layers = [layer for layer in self.scene.layers
                       if layer.id not in self._lost_cameras]
        self.status_chip.setText(
            "LIVE" if live_layers or not self._lost_cameras else "WAITING")
        self.start_button.setText("Stop preview")
        self.timer.start(33)
        if self.engine.report.vcam_error:
            self._status(self.engine.report.vcam_error)
        elif self.engine.report.media_errors:
            self._status(
                "Preview running · missing media: "
                + ", ".join(self.engine.report.media_errors))
        elif self.engine.report.media_error:
            self._status(f"Preview running · {self.engine.report.media_error}")
        elif self.engine.report.camera_probe_error:
            self._status(f"Preview running · {self.engine.report.camera_probe_error}")
        else:
            self._status("Preview running")
        if self._lost_cameras:
            devices = ", ".join(sorted(set(self._lost_cameras.values())))
            self._status(f"Preview running · waiting for camera: {devices}")

    def stop_engine(self) -> None:
        self.timer.stop()
        if self.engine is not None:
            report = self.engine.stop()
            if report.get("record_path"):
                self.recording_segments.append(report["record_path"])
                self._status(f"Saved {report['record_path']}")
            self.engine = None
        self.status_chip.setText("STOPPED")
        self.start_button.setText("Start preview")
        self.preview.image = None
        self.preview.layer_sizes = {}
        self.preview.update()

    def restart_engine(self) -> None:
        was_running = self.engine is not None
        if was_running:
            self.stop_engine()
            self.start_engine()

    def closeEvent(self, event) -> None:
        """Finalise the recording and persist edits before the process exits."""
        self.timer.stop()
        self._camera_watch.stop()
        self._transform_timer.stop()
        self._autosave_timer.stop()
        self.preview.cancel_pending_edits()
        if self.engine is not None:
            self.stop_engine()
        self._finish_recorder()
        self._finish_vcam_output()
        self._source_pool.stop()
        self._save_if_needed()
        self._close_screen_sessions()
        event.accept()

    def shutdown(self) -> None:
        """Backstop for QApplication.aboutToQuit and headless teardown."""
        self.timer.stop()
        self._camera_watch.stop()
        self._transform_timer.stop()
        self._autosave_timer.stop()
        self.preview.cancel_pending_edits()
        if self.engine is not None:
            self.stop_engine()
        self._finish_recorder()
        self._finish_vcam_output()
        self._source_pool.stop()
        self._save_if_needed()
        self._close_screen_sessions()

    def toggle_engine(self) -> None:
        if self.engine is None:
            self.start_engine()
        else:
            self._forget_lost_cameras()
            self.stop_engine()

    def toggle_record(self) -> None:
        if self.recording:
            self.recording = False
            self.record_button.setText("Record")
            if self.engine is not None:
                self.restart_engine()
            self._finish_recorder()
            return
        self.recording = True
        self._segment_index = 0
        self.recording_segments = []
        self._session_stamp = overlay.timestamped_name("anycam", "")
        self._recording_path = ""
        self.record_button.setText("Stop recording")
        if self.engine is not None:
            self.restart_engine()
        elif not self._start_recorder():
            self.recording = False
            self.record_button.setText("Record")
        else:
            self._status(f"Recording to {self._recording_path}")

    def toggle_vcam(self) -> None:
        self.vcam_on = not self.vcam_on
        self.vcam_button.setText("Virtual camera: on" if self.vcam_on else "Virtual camera")
        self.project.output.vcam_device = (self.vcam_edit.text().strip()
                                           or config.loopback_device())
        if self.vcam_on and not self._start_vcam_output():
            self.vcam_on = False
            self.vcam_button.setText("Virtual camera")
            return
        if self.engine is not None:
            self.restart_engine()
        if not self.vcam_on:
            self._finish_vcam_output()

    def snapshot(self) -> None:
        image = self.preview.image
        if image is None or image.isNull():
            self._status("No frame to snapshot")
            return
        path = config.recordings_dir() / overlay.timestamped_name("anycam-snapshot", ".png")
        image.save(str(path))
        self._status(f"Snapshot saved to {path}")

    def _on_frame(self, sample) -> None:
        from gi.repository import Gst

        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        width = caps.get_value("width")
        height = caps.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return
        try:
            # The preview caps are always RGBA, so every row is width * 4
            # bytes (row padding is not used by GStreamer video buffers).
            stride = width * 4
            raw = bytes(info.data)[: stride * height]
        finally:
            buf.unmap(info)
        self._pending_frame = (width, height, stride, raw)
        self.frame_ready.emit()

    def _update_preview(self) -> None:
        frame = self._pending_frame
        self._pending_frame = None
        if frame is None:
            return
        width, height, stride, raw = frame
        self._frame_bytes = raw
        self._face_size = (width, height)
        image = QImage(self._frame_bytes, width, height, stride, QImage.Format.Format_RGBA8888).copy()
        self.preview.set_image(image)

    def _tick(self) -> None:
        if self.engine is None:
            return
        # pump() also delivers the newest appsink frame to _on_frame, on this
        # (the main) thread.
        self.engine.pump(0)
        if self._pending_frame is not None:
            self._update_preview()
        self._track_face()
        rec = self.engine.sample()
        if self.engine.mic_level_db is not None:
            db = self.engine.mic_level_db
            self.mic_level_label.setText("muted" if db < -80 else f"{db:.0f} dB")
        self._advance_playlist(time.monotonic())
        if rec is not None:
            self.stats_label.setText(
                f"{rec['fps']:5.1f} fps · {rec['frames']} frames · "
                f"CPU {rec['cpu_pct']}% · RSS {rec['rss_mib']} MiB")
        screen_error = getattr(self.engine.report, "screen_error", "")
        if screen_error and screen_error != self._screen_error_shown:
            self._screen_error_shown = screen_error
            self._status(screen_error)
        source_errors = getattr(self.engine.report, "source_errors", ())
        camera_error = next(
            (error for error in source_errors if self._camera_error_layers(error)), "")
        if camera_error:
            self._handle_pipeline_error(f"Camera source error: {camera_error}")
            return
        if self.engine.report.error:
            self._handle_pipeline_error(
                f"Pipeline error: {self.engine.report.error}")
            return
        if self._render_seconds and time.monotonic() - self._started_at >= self._render_seconds:
            self._render_seconds = 0.0
            QTimer.singleShot(0, self._auto_screenshot)

    def _save_screenshot(self) -> bool:
        path = getattr(self, "auto_screenshot_path", "")
        has_frame = self.preview.image is not None and not self.preview.image.isNull()
        if not path:
            return has_frame
        pixmap = self.grab()
        saved = pixmap.save(path)
        print(f"screenshot {path} {pixmap.width()}x{pixmap.height()} "
              f"frame={'yes' if has_frame else 'no'}", flush=True)
        return saved and has_frame

    def _auto_screenshot(self) -> None:
        if getattr(self, "_screenshot_done", False):
            return
        self._screenshot_done = True
        self.screenshot_ok = self._save_screenshot()
        self.stop_engine()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _screenshot_deadline(self) -> None:
        """Hard stop for headless rendering, even if the pipeline never ran."""
        if getattr(self, "_screenshot_done", False):
            return
        self._screenshot_done = True
        self.screenshot_ok = self._save_screenshot()
        print("screenshot deadline reached", file=sys.stderr, flush=True)
        self.stop_engine()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _status(self, text: str) -> None:
        self.statusBar().showMessage(text)


def run_gui(project: Project | None = None) -> int:
    app = QApplication(sys.argv[:1])
    window = AnyCamWindow(project)
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    code = app.exec()
    # GStreamer 1.28's v4l2src segfaults during Python/GStreamer teardown on
    # this machine (niri/Wayland + GStreamer 1.28.8). Skip cleanup and let the
    # OS reclaim resources; the app has already written everything it needs.
    os._exit(code if code is not None else 0)


def run_screenshot(out: str, seconds: float, project: Project | None = None,
                   source: str = "") -> int:
    """Render the GUI on Qt's offscreen platform and save a PNG."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if project is None and source:
        from .demo import build_demo_project
        project = build_demo_project(source, config.CANVAS_W, config.CANVAS_H, config.CANVAS_FPS)
    app = QApplication(sys.argv[:1])
    window = AnyCamWindow(project)
    window.headless = True
    window.auto_screenshot_path = out
    window._render_seconds = max(0.1, seconds)
    window._screenshot_done = False
    window.resize(1440, 900)
    app.aboutToQuit.connect(window.shutdown)
    # Independent of the engine: a failed pipeline must still produce an exit,
    # and the PNG is still useful as a diagnostic of the stopped UI.
    QTimer.singleShot(int((window._render_seconds + 5.0) * 1000), window._screenshot_deadline)
    window.show()
    window.start_engine()
    app.exec()
    window.shutdown()
    status = 0 if (window.screenshot_ok and Path(out).exists() and Path(out).stat().st_size > 0) else 1
    # Same teardown guard as run_gui: GStreamer 1.28 can segfault during
    # Python/GStreamer interpreter shutdown, after the work is complete.
    os._exit(status)
