"""AnyCam's standalone Qt GUI.

The window is deliberately plain QtWidgets: the preview is a painted QImage
from the engine's appsink, which keeps the GUI testable with Qt's ``offscreen``
platform and avoids a QML scene-graph dependency in headless CI.
"""
from __future__ import annotations

import copy
import fcntl
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressDialog, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QSplitter, QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)

from . import config, overlay, portal
from .geometry import clamp
from .layouts import GRID_KINDS, LAYOUT_PRESETS, arrange, layout_preset
from .model import (Effects, Layer, Project, Scene, camera_grid_scene,
                    default_project, new_id, two_camera_scene)
from .engine.outputs.recorder import Recorder
from .engine.outputs.vcam import VcamOutput, pipewire_available
from .pipeline import AnyCamEngine, nvenc_available, recording_codec, va_present
from .phone_dialog import PhoneEvents, PhonePairDialog
from .pool import SCREEN_IDLE_GRACE_S, SourcePool
from . import animated, effect_thumbs, effects as effect_library
from . import emoji as emoji_library, frames as frames_library, particles as particles_library
from .sources import ANIMATION_PRESETS

STYLE = """
QMainWindow, QWidget { background: #0b0d10; color: #d7e3ea; font-family: "Noto Sans", "DejaVu Sans"; font-size: 13px; }
QFrame#panel { background: #101418; border: 1px solid #1e2830; border-radius: 10px; }
QLabel#title { color: #ffffff; font-size: 20px; font-weight: 700; }
QLabel#muted { color: #7d8b96; }
QLabel#accent { color: #5eead0; font-weight: 700; }
QLabel#hint { background: #12211f; border: 1px solid #5eead0; border-radius: 7px; padding: 7px 10px; color: #d7e3ea; }
QPushButton { background: #172026; border: 1px solid #26343d; border-radius: 7px; padding: 7px 12px; }
QPushButton:hover { border-color: #5eead0; }
QPushButton:pressed { background: #1d2b31; }
QPushButton#primary { background: #5eead0; color: #07120f; font-weight: 700; border: none; }
QPushButton#preset { text-align: left; padding: 7px 10px; font-weight: 600; }
QPushButton#preset:checked { border-color: #5eead0; color: #5eead0; background: #12211f; }
QPushButton#slotAdd { background: transparent; border: 2px solid transparent; border-radius: 10px; color: #5eead0; font-size: 30px; font-weight: 300; }
QPushButton#slotAdd:hover { border: 2px dashed #5eead0; background: rgba(18, 33, 31, 180); }
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

# Colour looks.  Each is a set of videobalance values in the units of
# model.Effects and applies live; the film looks and the filters in
# anycam.effects are separate, stackable stages.
EFFECT_STYLES: dict[str, str] = {
    "None": "none",
    "Sepia": "sepia",
    "Heat camera": "heat",
    "X-Ray": "xray",
    "Cross process": "xpro",
    "Yellow / blue": "yellowblue",
}
EFFECT_PRESETS: dict[str, dict[str, float]] = {
    "Original": {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0,
                 "hue": 0.0},
    "Warm": {"brightness": 0.03, "contrast": 1.05, "saturation": 1.12,
             "hue": -0.035},
    "Cool": {"brightness": 0.01, "contrast": 1.04, "saturation": 1.06,
             "hue": 0.045},
    "Mono": {"brightness": 0.01, "contrast": 1.10, "saturation": 0.0,
             "hue": 0.0},
    "Noir": {"brightness": 0.0, "contrast": 1.35, "saturation": 0.0,
             "hue": 0.0},
    "Vivid": {"brightness": 0.0, "contrast": 1.18, "saturation": 1.35,
              "hue": 0.0},
    "Studio": {"brightness": 0.02, "contrast": 1.10, "saturation": 1.08,
               "hue": 0.0},
    "Fade": {"brightness": 0.05, "contrast": 0.85, "saturation": 0.85,
             "hue": 0.0},
    "Golden": {"brightness": 0.04, "contrast": 1.03, "saturation": 1.14,
               "hue": -0.06},
    "Cinematic": {"brightness": 0.0, "contrast": 1.12, "saturation": 0.92,
                  "hue": 0.02},
    "Dream": {"brightness": 0.07, "contrast": 0.93, "saturation": 1.15,
              "hue": 0.02},
    "Moody": {"brightness": -0.035, "contrast": 1.16, "saturation": 0.82,
              "hue": -0.02},
    "Pastel": {"brightness": 0.075, "contrast": 0.88, "saturation": 1.12,
               "hue": 0.015},
    "Neon": {"brightness": 0.0, "contrast": 1.28, "saturation": 1.62,
             "hue": 0.055},
    "Vintage": {"brightness": 0.045, "contrast": 0.96, "saturation": 0.78,
                "hue": -0.045},
    "Teal / Orange": {"brightness": 0.01, "contrast": 1.14,
                      "saturation": 1.22, "hue": 0.035},
    "Infrared": {"brightness": 0.0, "contrast": 1.25, "saturation": 1.72,
                 "hue": -0.12},
    "Silver": {"brightness": 0.02, "contrast": 1.22, "saturation": 0.08,
               "hue": 0.0},
    "Night": {"brightness": -0.065, "contrast": 1.10, "saturation": 0.72,
              "hue": 0.06},
}


# Phrases a disconnected or wedged camera produces.  An error from another
# element (an encoder, the virtual camera's writer) has none of them, so a
# camera node that happens to be missing does not turn it into a disconnect.
_CAMERA_LOSS = re.compile(
    r"no such device|device (?:gone|disconnected|not found|busy|removed)"
    r"|error reading from device|could not read from resource")


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


class _PreviewOverlay(QWidget):
    """The editing marks drawn above the GPU video view.

    Children paint over their parent, so with a GPU preview the selection,
    guides and strokes are drawn by this transparent child instead of by
    ``PreviewWidget`` itself.  Mouse input passes through to the preview.
    It is repainted with its parent, never per video frame.
    """

    def __init__(self, preview: "PreviewWidget") -> None:
        super().__init__(preview)
        self.preview = preview
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, _event) -> None:
        if not self.preview.gpu_live:
            return  # the image preview paints its own marks
        painter = QPainter(self)
        self.preview._paint_marks(painter)
        painter.end()


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
    context_menu_requested = Signal(str, QPoint)
    stroke_finished = Signal(object)     # a finished drawing stroke (dict)
    # The device-pixel size the canvas is shown at changed (resize, screen
    # scale); the engine scales the preview to it so painting is 1:1.
    display_size_changed = Signal(int, int)

    HANDLE = 9.0
    MIN_BOX = 0.02
    MIN_CROP_SPAN = 0.10
    SNAP_PX = 6.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image: QImage | None = None
        self._image_owner: object = None
        self.scene: Scene | None = None
        self.selected_id = ""
        self.selected_ids: list[str] = []
        self.edit_mode = "move"          # move | crop | draw
        self.draw_tool = "pen"           # pen | marker | arrow | box
        self.draw_color = "#ff3b5c"
        self.draw_width = 0.006          # fraction of the canvas width
        self._stroke: dict | None = None
        # Source size per layer, so the crop window can follow the picture
        # inside the box instead of the box (which may contain bars).
        self.layer_sizes: dict[str, tuple[int, int]] = {}
        # Project canvas size in pixels (the preview image is a scaled-down copy);
        # spacing guides report distances in these units.
        self.canvas_size: tuple[int, int] | None = None
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
        # The selection outline is a transient hint: it shows when a layer is
        # picked and fades out again, so a full-frame source does not leave a
        # green border around the whole program.  Off by default (classic UI).
        self.selection_auto_hide = False
        self._selection_visible = True
        self._selection_timer = QTimer(self)
        self._selection_timer.setSingleShot(True)
        self._selection_timer.setInterval(1600)
        self._selection_timer.timeout.connect(self._hide_selection)
        self.setMinimumSize(QSize(640, 360))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # GPU preview (see gl_preview): a Qt Quick view under a transparent
        # overlay.  ``gpu_live`` is True while the engine draws into it.
        self.video_view = None
        self._overlay: _PreviewOverlay | None = None
        self.gpu_live = False

    # ------------------------------------------------------------ GPU view
    def enable_gpu(self) -> bool:
        """Add the GPU video view and its overlay; False leaves the image path."""
        from . import gl_preview

        if self.video_view is not None:
            return True
        gl_preview.use_opengl_scene_graph()
        view = gl_preview.make_video_view(self)
        if view is None:
            return False
        view.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        view.setGeometry(self.rect())
        view.lower()
        self.video_view = view
        self._overlay = _PreviewOverlay(self)
        self._overlay.setGeometry(self.rect())
        self._overlay.raise_()
        return True

    def disable_gpu(self) -> None:
        """Back to the image preview for good (the engine could not use it)."""
        for child in (self.video_view, self._overlay):
            if child is not None:
                child.hide()
                child.deleteLater()
        self.video_view = None
        self._overlay = None
        self.gpu_live = False
        self.update()

    def set_gpu_live(self, live: bool) -> None:
        """Show the video (live) or the idle text in the GPU view."""
        live = bool(live) and self.video_view is not None
        self.gpu_live = live
        if self.video_view is not None:
            root = self.video_view.rootObject()
            if root is not None:
                root.setProperty("live", live)
            self.video_view.lower()
        if self._overlay is not None:
            self._overlay.raise_()
            # Children added later (the Stage's "+" slots) stay on top.
            for child in self.children():
                if isinstance(child, QWidget) and child not in (self.video_view, self._overlay):
                    child.raise_()
        self.update()

    def has_frame(self) -> bool:
        return self.gpu_live or (self.image is not None and not self.image.isNull())

    def set_image(self, image: QImage, owner: object = None) -> None:
        """Show ``image``.  ``owner`` keeps the memory a zero-copy QImage
        points into alive for as long as the image is shown."""
        self.image = image
        self._image_owner = owner
        self.update()

    def display_size(self) -> tuple[int, int]:
        """Device pixels the canvas occupies inside the widget (letterboxed)."""
        if self.canvas_size and self.canvas_size[0] > 0 and self.canvas_size[1] > 0:
            aspect = QSize(int(self.canvas_size[0]), int(self.canvas_size[1]))
        elif self.image is not None and not self.image.isNull():
            aspect = self.image.size()
        else:
            aspect = QSize(16, 9)
        fitted = aspect.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        ratio = self.devicePixelRatioF()
        return (max(2, round(fitted.width() * ratio)),
                max(2, round(fitted.height() * ratio)))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        for child in (self.video_view, self._overlay):
            if child is not None:
                child.setGeometry(self.rect())
        width, height = self.display_size()
        self.display_size_changed.emit(width, height)

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
        if mode not in ("move", "crop", "draw"):
            return
        self.edit_mode = mode
        self._drag = None
        self._stroke = None
        if mode == "draw":
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()
        self.update()

    # ----------------------------------------------------------------- drawing
    def _begin_stroke(self, pos: QPointF) -> None:
        if not self.canvas_rect().contains(pos):
            return
        x, y = self.point_norm(pos)
        self._stroke = {"tool": self.draw_tool, "color": self.draw_color,
                        "width": self.draw_width, "points": [[x, y]]}
        self.update()

    def _extend_stroke(self, pos: QPointF) -> None:
        if self._stroke is None:
            return
        x, y = self.point_norm(pos)
        x, y = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
        points = self._stroke["points"]
        if self._stroke["tool"] in ("arrow", "box"):
            points[:] = [points[0], [x, y]]        # a shape: start and current end
        elif abs(x - points[-1][0]) + abs(y - points[-1][1]) > 0.0015:
            points.append([x, y])
        self.update()

    def _finish_stroke(self) -> None:
        stroke, self._stroke = self._stroke, None
        self.update()
        if stroke is None:
            return
        if stroke["tool"] in ("arrow", "box") and len(stroke["points"]) < 2:
            return                                  # a click, not a shape
        self.stroke_finished.emit(stroke)

    def _paint_stroke(self, painter: QPainter) -> None:
        """The stroke being drawn, so it follows the pointer before it is saved."""
        stroke = self._stroke
        canvas = self.canvas_rect()
        if stroke is None or canvas.isEmpty():
            return
        colour = QColor(stroke["color"])
        width = max(2.0, stroke["width"] * canvas.width())
        points = [QPointF(canvas.x() + x * canvas.width(), canvas.y() + y * canvas.height())
                  for x, y in stroke["points"]]
        tool = stroke["tool"]
        if tool == "marker":
            colour.setAlpha(110)
            width *= 4
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(colour, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                   Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        if tool == "box" and len(points) >= 2:
            painter.drawRect(QRectF(points[0], points[-1]).normalized())
        elif len(points) == 1:
            painter.drawPoint(points[0])
        else:
            painter.drawPolyline(points)

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

    def set_selection_auto_hide(self, enabled: bool) -> None:
        """Show the selection outline on demand and hide it when idle."""
        self.selection_auto_hide = bool(enabled)
        if not enabled:
            self._selection_timer.stop()
            self._selection_visible = True
            self.update()
        elif self.selected_ids:
            self._show_selection()
        else:
            self._hide_selection()

    def _show_selection(self) -> None:
        self._selection_visible = True
        self.update()
        if self.selection_auto_hide and self.selected_ids:
            self._selection_timer.start()

    def _hide_selection(self) -> None:
        self._selection_timer.stop()
        if not self._selection_visible:
            return
        self._selection_visible = False
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
        if ids:
            self._show_selection()
        else:
            self._hide_selection()
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
            self._show_selection()
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
        if self.gpu_live and self.canvas_size and min(self.canvas_size) > 0:
            frame = QSize(int(self.canvas_size[0]), int(self.canvas_size[1]))
        elif self.image is None or self.image.isNull():
            return QRectF()
        else:
            frame = self.image.size()
        scaled = frame.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
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
            if layer.kind == "drawing":
                continue   # full-canvas and on top: it must not swallow clicks
            if getattr(layer, "role", "") == "backdrop":
                continue   # the stage is behind everything: never picked by a click
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
        if self.edit_mode == "draw":
            self._begin_stroke(event.position())
            return
        self._show_selection()
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
        if self.edit_mode == "draw":
            if self._stroke is not None:
                if not (event.buttons() & Qt.MouseButton.LeftButton):
                    self._finish_stroke()
                else:
                    self._extend_stroke(event.position())
            return
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
        if event.button() == Qt.MouseButton.LeftButton and self._stroke is not None:
            self._extend_stroke(event.position())
            self._finish_stroke()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self._end_drag()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        """Right-click a layer: pick it, then let the window offer actions."""
        target = self.layer_at(QPointF(event.pos()))
        if target is not None:
            self.select_layer(target.id)
        self.context_menu_requested.emit(target.id if target else "",
                                         event.globalPos())
        event.accept()

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
            dx, dy = self._clamp_group_move(self._drag["group"], dx, dy)
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
    def _clamp_group_move(group: list[tuple[str, tuple]], dx: float,
                          dy: float) -> tuple[float, float]:
        """Stop the whole group at the canvas edge.

        Clamping each layer separately let the one nearest the edge stop while
        the rest kept moving, which changed their positions relative to each
        other mid-drag.
        """
        if not group:
            return dx, dy
        low_x = max(-box[0] for _id, box in group)
        high_x = min(1.0 - box[0] - box[2] for _id, box in group)
        low_y = max(-box[1] for _id, box in group)
        high_y = min(1.0 - box[1] - box[3] for _id, box in group)
        if low_x <= high_x:
            dx = max(low_x, min(high_x, dx))
        if low_y <= high_y:
            dy = max(low_y, min(high_y, dy))
        return dx, dy

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

    def _canvas_pixels(self) -> tuple[int, int]:
        """Canvas size in pixels; the preview image is only a scaled copy."""
        if self.canvas_size:
            return self.canvas_size
        return (self.image.width(), self.image.height())

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
                                int(round(gap * self._canvas_pixels()[0]))))
        if vertical:
            gap, start, end, fixed = min(vertical, key=lambda item: item[0])
            if gap > 0.001:
                spacing.append(("h", start, end, fixed,
                                int(round(gap * self._canvas_pixels()[1]))))
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
        self._show_selection()
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
        if self.gpu_live:
            # The video view draws the canvas and the overlay child draws the
            # marks; nothing here changes per frame.
            return
        painter.fillRect(self.rect(), QColor("#07090b"))
        if self.image is None or self.image.isNull():
            painter.setPen(QColor("#41525c"))
            font = painter.font()
            font.setPointSize(13)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Preview stopped\nAnyCam")
            return
        target = self.canvas_rect()
        ratio = self.devicePixelRatioF()
        if (abs(self.image.width() - target.width() * ratio) > 1
                or abs(self.image.height() - target.height() * ratio) > 1):
            # Fitted while drawing (bilinear).  QImage.scaled(Smooth) per
            # frame cost ~1 ms at full screen against ~0.4 ms here.
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, self.image)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        self._paint_marks(painter)

    def _paint_marks(self, painter: QPainter) -> None:
        """Selection, guides and the stroke being drawn."""
        if self.edit_mode != "draw":
            self._paint_selection(painter)
            self._paint_guides(painter)
        self._paint_stroke(painter)

    def _paint_selection(self, painter: QPainter) -> None:
        if self.scene is None or not self._selection_visible:
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


def _slider_row(label: str, low: int, high: int, value: int, callback,
                display=None) -> tuple[QWidget, QSlider, QLabel]:
    holder = QWidget()
    row = QGridLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    title = QLabel(label)
    format_value = display or str
    value_label = QLabel(format_value(value))
    value_label.setObjectName("accent")
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(low, high)
    slider.setValue(value)
    row.addWidget(title, 0, 0)
    row.addWidget(value_label, 0, 1, alignment=Qt.AlignmentFlag.AlignRight)
    row.addWidget(slider, 1, 0, 1, 2)
    slider.valueChanged.connect(
        lambda v: (value_label.setText(format_value(v)), callback(v)))
    return holder, slider, value_label


class OverlayDialog(QDialog):
    """Edit a ticker's text, or a lower third's name and title, and its colours."""

    def __init__(self, layer: Layer, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit {layer.name}")
        self._layer = layer
        self._colour = layer.color or "#ffffff"
        self._background = layer.background or "#101418cc"
        form = QFormLayout(self)
        if layer.kind == "lower_third":
            name, _sep, title = str(layer.source or "").partition("|")
            self.first = QLineEdit(name)
            self.second = QLineEdit(title)
            form.addRow("Name", self.first)
            form.addRow("Title", self.second)
        else:
            self.first = QLineEdit(layer.source)
            self.second = None
            form.addRow("Text", self.first)
        self.colour_button = QPushButton(self._colour)
        self.colour_button.clicked.connect(self._pick_colour)
        self.background_button = QPushButton(self._background)
        self.background_button.clicked.connect(self._pick_background)
        form.addRow("Text colour", self.colour_button)
        form.addRow("Background", self.background_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._paint()

    def _paint(self) -> None:
        self.colour_button.setText(self._colour)
        self.background_button.setText(self._background)

    def _pick_colour(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._colour), self, "Text colour")
        if chosen.isValid():
            self._colour = chosen.name()
            self._paint()

    def _pick_background(self) -> None:
        chosen = QColorDialog.getColor(
            QColor(overlay.parse_color(self._background, (16, 20, 24, 204))[0],
                   *overlay.parse_color(self._background, (16, 20, 24, 204))[1:]),
            self, "Background", QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if chosen.isValid():
            self._background = (f"#{chosen.red():02x}{chosen.green():02x}"
                                f"{chosen.blue():02x}{chosen.alpha():02x}")
            self._paint()

    def values(self) -> tuple[str, str, str]:
        """``(source, text colour, background)`` as stored on the layer."""
        if self.second is not None:
            source = f"{self.first.text().strip()}|{self.second.text().strip()}"
        else:
            source = self.first.text().strip()
        return source, self._colour, self._background


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
        # Screen-capture retention/reuse events belong in Diagnostics, next to
        # the virtual camera events they explain (a rebuild that holds the
        # capture also holds the vcam's last frame).
        self._source_pool.log_event = self.log_event
        from . import inference

        inference.set_preference(self.project.output.effects_device)
        from . import segment

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
            if layer.kind in ("ticker", "lower_third"):
                edit = menu.addAction(f"Edit {layer.name}…")
                edit.setData("edit")
            duplicate = menu.addAction("Duplicate layer")
            duplicate.setData("duplicate")
            remove = menu.addAction(f"Remove {layer.name}")
            remove.setData("remove")
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
        elif data == "duplicate":
            if layer_id:
                self.preview.select_layer(layer_id)
            self.duplicate_layer()
        elif data == "remove":
            self.remove_layers([layer_id])

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
        self._refresh_scene_list()
        return page

    def _layers_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.layer_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.layer_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.layer_list.model().rowsMoved.connect(self._layers_reordered)
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
                   ("Screen", "screen"), ("Phone camera", "phone"),
                   ("Network URL", "network"),
                   ("Ticker", "ticker"), ("Lower third", "lower_third"),
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

    # Browse: the thumbnail library.  Adjust: every individual control.
    FRAME_GROUPS = {f"{group} frames": group for group in frames_library.GROUPS}
    EFFECT_CATEGORIES = ("Looks", "Portrait", "Face", "Masks", "Framing", "Distort",
                         "Retro TV", "Touch-up", "Frames", "Shapes",
                         *FRAME_GROUPS, "Animations", "Emoji rain")
    _CAPTURE_KINDS = ("camera", "screen", "video", "network", "phone", "test")

    def _effects_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        # Adjust is built first: it owns the widgets the sync code fills in.
        adjust = self._effects_adjust_page()
        tabs.addTab(self._effects_browse_page(), "Browse")
        tabs.addTab(adjust, "Adjust")
        self.effects_tabs = tabs
        layout.addWidget(tabs)
        self._sync_effects_controls()
        return page

    def _effects_adjust_page(self) -> QWidget:
        page = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        fx = self._current_effects()
        self._syncing_effects = False
        self._effect_sliders: dict[str, QSlider] = {}
        self._effect_controls: list[QWidget] = []
        self._chroma_controls: list[QWidget] = []

        target = QGroupBox("Target")
        target_layout = QVBoxLayout(target)
        self.effects_target = QLabel("No layer selected")
        self.effects_target.setObjectName("accent")
        target_layout.addWidget(self.effects_target)
        self.effects_hint = QLabel(
            "Select a layer in the preview or Layers tab. Effects are per "
            "layer, so changing one source never alters the rest of the scene.")
        self.effects_hint.setObjectName("muted")
        self.effects_hint.setWordWrap(True)
        target_layout.addWidget(self.effects_hint)
        layout.addWidget(target)

        look = QGroupBox("Look")
        look_layout = QGridLayout(look)
        look_layout.addWidget(QLabel("Preset"), 0, 0)
        self.effect_preset = QComboBox()
        self.effect_preset.addItem("Custom", "")
        for name in EFFECT_PRESETS:
            self.effect_preset.addItem(name, name)
        self.effect_preset.currentIndexChanged.connect(self._apply_effect_preset)
        look_layout.addWidget(self.effect_preset, 0, 1)
        reset = QPushButton("Reset all effects")
        reset.clicked.connect(self._reset_effects)
        look_layout.addWidget(reset, 1, 0, 1, 2)
        layout.addWidget(look)
        self._effect_controls.extend((self.effect_preset, reset))

        colour = QGroupBox("Colour")
        colour_layout = QVBoxLayout(colour)
        for key, label, low, high, value, callback, display in (
            ("brightness", "Brightness", -100, 100, int(round(fx.brightness * 100)),
             lambda v: self._set_effect("brightness", v), None),
            ("contrast", "Contrast", 0, 200, int(round(fx.contrast * 100)),
             lambda v: self._set_effect("contrast", v), None),
            ("saturation", "Saturation", 0, 200, int(round(fx.saturation * 100)),
             lambda v: self._set_effect("saturation", v), None),
            ("hue", "Hue", -180, 180, int(round(fx.hue * 180)),
             lambda v: self._set_effect("hue", v), lambda v: f"{v}\u00b0"),
        ):
            holder, slider, _label = _slider_row(
                label, low, high, value, callback, display=display)
            self._effect_sliders[key] = slider
            colour_layout.addWidget(holder)
        layout.addWidget(colour)
        self._effect_controls.append(colour)

        style = QGroupBox("Film look")
        style_layout = QGridLayout(style)
        style_layout.addWidget(QLabel("Look"), 0, 0)
        self.effect_style = QComboBox()
        for label, value in EFFECT_STYLES.items():
            self.effect_style.addItem(label, value)
        index = self.effect_style.findData(fx.color_effect)
        self.effect_style.setCurrentIndex(index if index >= 0 else 0)
        self.effect_style.currentIndexChanged.connect(
            lambda: self._set_effect("color_effect", self.effect_style.currentData()))
        style_layout.addWidget(self.effect_style, 0, 1)
        self.effect_mirror = QCheckBox("Mirror horizontally")
        self.effect_mirror.setChecked(bool(fx.mirror))
        self.effect_mirror.toggled.connect(
            lambda checked: self._set_effect("mirror", bool(checked)))
        style_layout.addWidget(self.effect_mirror, 1, 0, 1, 2)
        style_note = QLabel(
            "Look-up-table looks and mirror are per-pixel/scanline work, "
            "so they stay live at camera speed.")
        style_note.setObjectName("muted")
        style_note.setWordWrap(True)
        style_layout.addWidget(style_note, 2, 0, 1, 2)
        layout.addWidget(style)
        self._effect_controls.append(style)

        frame = QGroupBox("Window frame")
        frame_layout = QGridLayout(frame)
        frame_layout.addWidget(QLabel("Shape"), 0, 0)
        self.frame_style = QComboBox()
        for value, label in overlay.WINDOW_FRAME_STYLES.items():
            self.frame_style.addItem(label, value)
        index = self.frame_style.findData(fx.frame_style)
        self.frame_style.setCurrentIndex(index if index >= 0 else 0)
        self.frame_style.currentIndexChanged.connect(self._frame_style_changed)
        frame_layout.addWidget(self.frame_style, 0, 1)
        frame_layout.addWidget(QLabel("Colour"), 1, 0)
        frame_colour_row = QHBoxLayout()
        self.frame_color = QLineEdit(fx.frame_color)
        self.frame_color.setMaxLength(9)
        self.frame_color.setPlaceholderText("#5eead0")
        self.frame_color.editingFinished.connect(
            lambda: self._set_effect("frame_color", self.frame_color.text()))
        frame_pick = QPushButton("Pick\u2026")
        frame_pick.clicked.connect(self._pick_frame_color)
        frame_colour_row.addWidget(self.frame_color, 1)
        frame_colour_row.addWidget(frame_pick)
        frame_layout.addLayout(frame_colour_row, 1, 1)
        holder, frame_width, _label = _slider_row(
            "Thickness", 1, 24, int(fx.frame_width),
            lambda v: self._set_effect("frame_width", v),
            display=lambda v: f"{v} px")
        self._effect_sliders["frame_width"] = frame_width
        frame_layout.addWidget(holder, 2, 0, 1, 2)
        frame_layout.addWidget(QLabel("Matte"), 3, 0)
        matte_row = QHBoxLayout()
        self.frame_matte = QLineEdit(fx.frame_matte)
        self.frame_matte.setMaxLength(9)
        self.frame_matte.setPlaceholderText("#0b0d10")
        self.frame_matte.editingFinished.connect(
            lambda: self._set_effect("frame_matte", self.frame_matte.text()))
        matte_pick = QPushButton("Pick\u2026")
        matte_pick.clicked.connect(self._pick_frame_matte)
        matte_row.addWidget(self.frame_matte, 1)
        matte_row.addWidget(matte_pick)
        frame_layout.addLayout(matte_row, 3, 1)
        frame_note = QLabel(
            "Frames follow the picture. Shaped windows (circle, heart, star ...), "
            "the TV screen and the vignette fill the area outside with the "
            "matte colour; the other styles draw a border in the frame colour.")
        frame_note.setObjectName("muted")
        frame_note.setWordWrap(True)
        frame_layout.addWidget(frame_note, 4, 0, 1, 2)
        layout.addWidget(frame)
        self._effect_controls.append(frame)
        self._frame_controls = [self.frame_color, frame_pick, frame_width,
                                self.frame_matte, matte_pick]

        chroma = QGroupBox("Chroma key")
        chroma_layout = QGridLayout(chroma)
        self.chroma_enabled = QCheckBox("Key out a colour")
        self.chroma_enabled.setChecked(fx.chroma_enabled)
        self.chroma_enabled.toggled.connect(self._chroma_toggled)
        chroma_layout.addWidget(self.chroma_enabled, 0, 0, 1, 2)
        chroma_layout.addWidget(QLabel("Key colour"), 1, 0)
        colour_row = QHBoxLayout()
        self.chroma_color = QLineEdit(fx.chroma_key)
        self.chroma_color.setMaxLength(9)
        self.chroma_color.setPlaceholderText("#00ff00")
        self.chroma_color.editingFinished.connect(
            lambda: self._set_effect("chroma_key", self.chroma_color.text()))
        pick = QPushButton("Pick\u2026")
        pick.clicked.connect(self._pick_chroma_color)
        colour_row.addWidget(self.chroma_color, 1)
        colour_row.addWidget(pick)
        chroma_layout.addLayout(colour_row, 1, 1)
        for key, label, value in (
            ("chroma_similarity", "Tolerance", int(round(fx.chroma_similarity * 100))),
            ("chroma_smoothness", "Edge softness", int(round(fx.chroma_smoothness * 100))),
        ):
            holder, slider, _label = _slider_row(
                label, 0, 100, value,
                lambda v, a=key: self._set_effect(a, v / 100.0))
            self._effect_sliders[key] = slider
            chroma_layout.addWidget(holder, 2 if key == "chroma_similarity" else 3,
                                    0, 1, 2)
        chroma_note = QLabel(
            "Pick the backdrop colour. Tolerance widens the key; edge softness "
            "blends the boundary instead of leaving a hard cutout.")
        chroma_note.setObjectName("muted")
        chroma_note.setWordWrap(True)
        chroma_layout.addWidget(chroma_note, 4, 0, 1, 2)
        layout.addWidget(chroma)
        self._effect_controls.append(chroma)
        self._chroma_controls.extend((self.chroma_color, pick,
                                      self._effect_sliders["chroma_similarity"],
                                      self._effect_sliders["chroma_smoothness"]))
        self._update_frame_controls()

        layout.addStretch(1)

        live = QLabel("Changes apply live \u00b7 no canvas rebuild, no output interruption")
        live.setObjectName("muted")
        live.setWordWrap(True)
        layout.addWidget(live)
        scroll.setWidget(body)
        container = QVBoxLayout(page)
        container.addWidget(scroll)
        self._sync_effects_controls()
        return page

    def edit_overlay(self, layer_id: str, values: tuple[str, str, str] | None = None) -> bool:
        """Edit a ticker or lower third; ``values`` skips the dialog (tests, scripts)."""
        layer = self.scene.layer(layer_id)
        if layer is None or layer.kind not in ("ticker", "lower_third"):
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

    # ------------------------------------------------------- effects browser
    def _effects_browse_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.browse_target = QLabel("No layer selected")
        self.browse_target.setObjectName("accent")
        layout.addWidget(self.browse_target)
        self.effects_all = QCheckBox("Apply to all sources")
        self.effects_all.setToolTip(
            "Apply the chosen effect to every visible camera, screen, video, "
            "network and pattern layer instead of only the selected one.")
        layout.addWidget(self.effects_all)

        row = QHBoxLayout()
        self.effect_category = QComboBox()
        for name in self.EFFECT_CATEGORIES:
            self.effect_category.addItem(name, name)
        self.effect_category.currentIndexChanged.connect(self._populate_effect_grid)
        row.addWidget(self.effect_category, 1)
        self.filter_slot = QComboBox()
        self.filter_slot.addItem("Filter 1", 1)
        self.filter_slot.addItem("Filter 2", 2)
        self.filter_slot.setToolTip(
            "A layer has two filter slots, so two filters can be on at once "
            "(for example a portrait effect and a distortion).")
        self.filter_slot.currentIndexChanged.connect(self._filter_slot_changed)
        row.addWidget(self.filter_slot)
        self.thumb_refresh = QPushButton("Use picture")
        self.thumb_refresh.setToolTip(
            "Redraw the previews from what the program shows right now.")
        self.thumb_refresh.clicked.connect(self._refresh_effect_previews)
        row.addWidget(self.thumb_refresh)
        layout.addLayout(row)

        tools = QHBoxLayout()
        self.effect_search = QLineEdit()
        self.effect_search.setPlaceholderText("Search all effects\u2026")
        self.effect_search.setClearButtonEnabled(True)
        self.effect_search.textChanged.connect(self._populate_effect_grid)
        tools.addWidget(self.effect_search, 1)
        for label, tip, handler in (
                ("Copy", "Copy this layer's effects", self._copy_effects),
                ("Paste", "Paste the copied effects onto the selected layer(s)",
                 self._paste_effects),
                ("Clear", "Remove every effect from the selected layer(s)",
                 self._clear_effects)):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            tools.addWidget(button)
        layout.addLayout(tools)

        self.effect_grid = QListWidget()
        self.effect_grid.setObjectName("effectGrid")
        # The app-wide item padding breaks icon-mode sizing (tiles were clipped
        # to a strip), so the grid carries its own rules.
        self.effect_grid.setStyleSheet(
            "QListWidget#effectGrid::item { padding: 0px; margin: 1px; "
            "border: 2px solid transparent; border-radius: 7px; color: #9fb0bb; }"
            "QListWidget#effectGrid::item:selected { border: 2px solid #5eead0; "
            "background: #12302d; color: #5eead0; }"
            "QListWidget#effectGrid::item:hover { background: #16222a; }")
        self.effect_grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.effect_grid.setIconSize(QSize(112, 63))
        self.effect_grid.setGridSize(QSize(126, 96))
        self.effect_grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.effect_grid.setMovement(QListWidget.Movement.Static)
        self.effect_grid.setWordWrap(True)
        self.effect_grid.setSpacing(2)
        self.effect_grid.setUniformItemSizes(True)
        self.effect_grid.itemClicked.connect(self._effect_item_clicked)
        self.effect_grid.installEventFilter(self)
        layout.addWidget(self.effect_grid, 1)

        holder, strength, label = _slider_row(
            "Strength", 0, 100, 50,
            lambda v: self._set_effect(self._slot_attr() + "_amount", v / 100.0))
        self._effect_sliders["filter_amount"] = strength
        self.filter_strength_row = holder
        layout.addWidget(holder)
        self.effect_note = QLabel(
            "Click a tile to apply it. Effects stack: a look, a filter and a "
            "frame can all be on at once. Changes are live.")
        self.effect_note.setObjectName("muted")
        self.effect_note.setWordWrap(True)
        layout.addWidget(self.effect_note)

        self._thumb_sample_image = None
        self._thumb_cache: dict[tuple, QPixmap] = {}
        self._thumb_queue: list[tuple] = []
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(15)
        self._thumb_timer.timeout.connect(self._render_pending_thumbs)
        self._effect_entries: list[dict] = []
        self._populate_effect_grid()
        return page

    def _effect_catalog(self, category: str) -> list[dict]:
        entries = self._effect_catalog_raw(category)
        for entry in entries:
            entry.setdefault("category", category)
        return entries

    def _search_catalog(self, text: str) -> list[dict]:
        """Every tile, in any category, whose name or description matches."""
        needle = text.strip().lower()
        found: list[dict] = []
        for category in self.EFFECT_CATEGORIES:
            for entry in self._effect_catalog(category):
                if entry["label"].startswith("No "):
                    continue          # "No filter" / "No frame" belong to their category
                haystack = f"{entry['label']} {entry.get('tip', '')} {category}".lower()
                if all(word in haystack for word in needle.split()):
                    found.append(entry)
        return found

    def _effect_catalog_raw(self, category: str) -> list[dict]:
        """The tiles of one category: label, what they set, how they preview."""
        entries: list[dict] = []
        if category == "Looks":
            for name, values in EFFECT_PRESETS.items():
                entries.append({"label": name, "kind": "look",
                                "apply": dict(values), "preview": dict(values)})
            for label, value in EFFECT_STYLES.items():
                if value != "none":
                    entries.append({"label": label, "kind": "film",
                                    "apply": {"color_effect": value},
                                    "preview": {"color_effect": value}})
        elif category in effect_library.FILTER_CATEGORIES:
            entries.append({"label": "No filter", "kind": "filter",
                            "apply": {"filter": "none"}, "preview": {}})
            for spec in effect_library.available_filters(category):
                values = {"filter": spec.key, "filter_amount": spec.default}
                entries.append({"label": spec.label, "kind": "filter",
                                "apply": values, "preview": values,
                                "tip": spec.description,
                                "amount": bool(spec.prop)})
        elif category in self.FRAME_GROUPS:
            entries.append({"label": "No frame", "kind": "frame",
                            "apply": {"frame_style": "none"}, "preview": {}})
            for key, (label, group, _painter) in frames_library.THEMES.items():
                if group == self.FRAME_GROUPS[category]:
                    values = {"frame_style": key}
                    entries.append({"label": label, "kind": "frame", "apply": values,
                                    "preview": dict(values),
                                    "tip": f"{label}: a finished {group.lower()} frame"})
        elif category == "Animations":
            entries.append({"label": "No animation", "kind": "particles", "source": "",
                            "apply": {}, "preview": {}, "tip": "Remove the animation"})
            for key, (label, group, _factory) in particles_library.PRESETS.items():
                entries.append({"label": label, "kind": "particles", "source": key,
                                "apply": {}, "preview": {}, "particles": key,
                                "tip": f"{group}: adds a moving {label.lower()} layer on top"})
        elif category == "Emoji rain":
            entries.append({"label": "No animation", "kind": "particles", "source": "",
                            "apply": {}, "preview": {}, "tip": "Remove the animation"})
            for code, name, group, words in emoji_library.available():
                entries.append({"label": name, "kind": "particles", "source": f"emoji:{code}",
                                "apply": {}, "preview": {}, "particles": f"emoji:{code}",
                                "tip": f"{group}: makes {name.lower()} fall across the picture ({words})"})
        elif category in ("Frames", "Shapes"):
            entries.append({"label": "No frame", "kind": "frame",
                            "apply": {"frame_style": "none"}, "preview": {}})
            for key, label in overlay.WINDOW_FRAME_STYLES.items():
                shaped = key in overlay.SHAPED_FRAME_STYLES
                if (key == "none" or key in frames_library.THEMES
                        or shaped != (category == "Shapes")):
                    continue
                values = {"frame_style": key}
                entries.append({"label": label, "kind": "frame",
                                "apply": values,
                                "preview": {**values, "frame_color": "#ff4d7a",
                                            "frame_width": 6}})
        return entries

    def _slot_attr(self) -> str:
        """The Effects field of the chosen filter slot: ``filter`` or ``filter2``."""
        combo = getattr(self, "filter_slot", None)
        return "filter2" if combo is not None and combo.currentData() == 2 else "filter"

    def _filter_slot_changed(self, *_args) -> None:
        self._sync_effects_controls()

    def _effect_entry_matches(self, entry: dict, fx: Effects) -> bool:
        apply = entry["apply"]
        if entry["kind"] == "look":
            return all(abs(float(getattr(fx, key)) - float(value)) < 1e-4
                       for key, value in apply.items())
        if entry["kind"] == "particles":
            current = self._animation_layers()
            if not entry["source"]:
                return not current
            return bool(current) and current[0].source == entry["source"]
        if entry["kind"] == "filter":
            return getattr(fx, self._slot_attr()) == apply["filter"]
        return all(getattr(fx, key) == value for key, value in apply.items()
                   if key != "filter_amount")

    def _thumb_sample(self):
        if self._thumb_sample_image is None:
            self._thumb_sample_image = self._current_picture_sample()
        return self._thumb_sample_image

    def _current_picture_sample(self):
        """The program picture for previews, or the built-in sample."""
        from PIL import Image

        image = getattr(self.preview, "image", None)
        if image is not None and not image.isNull():
            rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
            width, height = rgba.width(), rgba.height()
            data = bytes(rgba.constBits())[: width * height * 4]
            return Image.frombytes("RGBA", (width, height), data).convert("RGB")
        return effect_thumbs.default_sample()

    def _populate_effect_grid(self, *_args) -> None:
        category = str(self.effect_category.currentData() or "Looks")
        query = self.effect_search.text() if hasattr(self, "effect_search") else ""
        self._effect_entries = (self._search_catalog(query) if query.strip()
                                else self._effect_catalog(category))
        self.effect_grid.clear()
        self._thumb_queue.clear()
        sample_id = id(self._thumb_sample_image)
        for index, entry in enumerate(self._effect_entries):
            item = QListWidgetItem(entry["label"])
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(entry.get("tip") or entry["label"])
            # An ::item style rule makes Qt size a tile from its text alone,
            # which clipped the thumbnail to a strip; state the size outright.
            item.setSizeHint(QSize(122, 92))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter
                                  | Qt.AlignmentFlag.AlignTop)
            key_category = entry.get("category", category)
            cached = self._thumb_cache.get((key_category, entry["label"], sample_id))
            if cached is not None:
                item.setIcon(QIcon(cached))
            else:
                self._thumb_queue.append((item, key_category, entry, sample_id))
            self.effect_grid.addItem(item)
        if self._thumb_queue and self.effect_grid.isVisible():
            self._thumb_timer.start()
        self._select_current_effect_tile()

    def _render_pending_thumbs(self) -> None:
        """Draw a couple of tiles per tick so the window stays responsive.

        Only while the grid is on screen: tiles for a tab nobody opened (or a
        window that has closed) are not worth the GUI thread's time, and the
        queue is picked up again when the grid is shown.
        """
        if not self.effect_grid.isVisible():
            self._thumb_timer.stop()
            return
        for _ in range(2):
            if not self._thumb_queue:
                self._thumb_timer.stop()
                return
            item, category, entry, sample_id = self._thumb_queue.pop(0)
            try:
                if entry.get("particles"):
                    picture = effect_thumbs.particle_thumbnail(
                        entry["particles"], self._thumb_sample())
                else:
                    fx = Effects(**entry["preview"])
                    picture = effect_thumbs.render_thumbnail(fx, self._thumb_sample())
            except Exception:  # noqa: BLE001 - a tile without a preview is fine
                picture = None
            if picture is None:
                continue
            width, height = picture.size
            image = QImage(picture.tobytes(), width, height, width * 3,
                           QImage.Format.Format_RGB888).copy()
            pixmap = QPixmap.fromImage(image)
            self._thumb_cache[(category, entry["label"], sample_id)] = pixmap
            try:
                item.setIcon(QIcon(pixmap))
            except RuntimeError:
                pass  # the list was repopulated while this tile was queued

    def _refresh_effect_previews(self) -> None:
        self._thumb_sample_image = self._current_picture_sample()
        self._thumb_cache.clear()
        self._populate_effect_grid()

    def _copy_effects(self) -> None:
        layer = self._effects_layer()
        if layer is None:
            self._status("Select a layer to copy effects from")
            return
        self._effects_clipboard = copy.deepcopy(layer.effects)
        self._status(f"Copied the effects of {layer.name}")

    def _paste_effects(self) -> None:
        clipboard = getattr(self, "_effects_clipboard", None)
        targets = self._effect_targets()
        if clipboard is None or not targets:
            self._status("Copy a layer's effects first, then select where to paste")
            return
        for layer in targets:
            layer.effects = copy.deepcopy(clipboard)
            self._apply_effects(layer)
        self._sync_effects_controls()
        self._mark_dirty()
        self._status(f"Pasted effects onto {len(targets)} layer(s)")

    def _clear_effects(self) -> None:
        targets = self._effect_targets()
        if not targets:
            self._status("Select a layer in the preview or Layers tab first")
            return
        for layer in targets:
            layer.effects = Effects()
            self._apply_effects(layer)
        self._sync_effects_controls()
        self._mark_dirty()
        self._status(f"Cleared the effects of {len(targets)} layer(s)")

    def _effect_targets(self) -> list[Layer]:
        if self.effects_all.isChecked():
            return [layer for layer in self.scene.layers
                    if layer.visible and layer.kind in self._CAPTURE_KINDS]
        layer = self._effects_layer()
        return [layer] if layer is not None else []

    def _animation_layers(self) -> list[Layer]:
        return [layer for layer in self.scene.layers if layer.kind == "particles"]

    def _set_animation(self, source: str) -> None:
        """Show one animation at a time: swap it live, toggle it off, or clear it.

        Several full-canvas animation layers piled up (one per click) and
        starved the canvas, so there is only ever one.  Picking another swaps the
        generator inside the running layer; picking the active one again, or
        "No animation", removes it.
        """
        from . import particles

        current = self._animation_layers()
        if not source or (current and current[0].source == source):
            if current:
                self.remove_layers([layer.id for layer in current])
                self._status("Animation removed")
            self._select_current_effect_tile()
            return
        if current:
            keep, extra = current[0], current[1:]
            if extra:
                self.remove_layers([layer.id for layer in extra])
            before = self._layout_snapshot()
            keep.source = source
            keep.name = particles.label_for(source)
            if self.engine is not None and not self.engine.update_overlay(keep):
                self.remove_layers([keep.id])
                self.add_layer("particles", source=source)
            self._refresh_layers()
            self._record_history(before)
            self._mark_dirty()
        else:
            self.add_layer("particles", source=source)
        self._status(f"Animation: {particles.label_for(source)} (click it again to remove)")
        self._select_current_effect_tile()

    def _effect_item_clicked(self, item) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is None or not 0 <= int(index) < len(self._effect_entries):
            return
        entry = self._effect_entries[int(index)]
        if entry["kind"] == "particles":
            self._set_animation(entry["source"])
            return
        targets = self._effect_targets()
        if not targets:
            self._status("Select a layer in the preview or Layers tab first")
            return
        attr = self._slot_attr()
        for layer in targets:
            for key, value in entry["apply"].items():
                if entry["kind"] == "filter":
                    # Filter tiles go to the chosen slot.
                    key = key.replace("filter", attr, 1)
                setattr(layer.effects, key, value)
            self._apply_effects(layer)
        self._sync_effects_controls()
        self._mark_dirty()
        count = f" on {len(targets)} sources" if len(targets) > 1 else ""
        self._status(f"{entry['label']}{count}")

    def _select_current_effect_tile(self) -> None:
        layer = self._effects_layer()
        if layer is None or not hasattr(self, "effect_grid"):
            return
        self.effect_grid.blockSignals(True)
        try:
            self.effect_grid.clearSelection()
            for index, entry in enumerate(self._effect_entries):
                if self._effect_entry_matches(entry, layer.effects):
                    self.effect_grid.setCurrentRow(index)
                    break
        finally:
            self.effect_grid.blockSignals(False)

    def _sync_filter_strength(self, fx: Effects) -> None:
        attr = self._slot_attr()
        spec = effect_library.FILTERS.get(getattr(fx, attr))
        has_amount = bool(spec is not None and spec.prop)
        self.filter_strength_row.setVisible(has_amount)
        slider = self._effect_sliders.get("filter_amount")
        if slider is not None and has_amount:
            slider.blockSignals(True)
            slider.setValue(int(round(getattr(fx, attr + "_amount") * 100)))
            slider.blockSignals(False)

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

        # Voice processing (webrtcdsp): steady background noise such as fans,
        # hum and hiss.  Applied to recordings and streams.
        self.denoise_combo = QComboBox()
        for label, value in (("Off", "off"), ("Low", "low"), ("Moderate", "moderate"),
                             ("High", "high"), ("Very high", "very-high")):
            self.denoise_combo.addItem(label, value)
        index = self.denoise_combo.findData(self.project.audio.mic_denoise)
        self.denoise_combo.setCurrentIndex(index if index >= 0 else 0)
        self.denoise_combo.setToolTip(
            "Removes steady background noise (fans, hum, hiss) from the "
            "microphone. Higher levels remove more but can colour the voice.")
        self.denoise_combo.currentIndexChanged.connect(self._mic_processing_changed)
        grid.addWidget(QLabel("Noise suppression"), 5, 0)
        grid.addWidget(self.denoise_combo, 5, 1)
        self.auto_gain_check = QCheckBox("Automatic gain (even out loud and quiet speech)")
        self.auto_gain_check.setChecked(self.project.audio.mic_auto_gain)
        self.auto_gain_check.toggled.connect(self._mic_processing_changed)
        grid.addWidget(self.auto_gain_check, 6, 0, 1, 2)
        layout.addWidget(box)

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

    def _desktop_audio_changed(self, _index: int) -> None:
        device = self.desktop_combo.currentData() or ""
        self.project.audio.desktop_device = device
        self.project.audio.desktop_muted = False
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        self._status("Desktop audio off" if not device
                     else f"Desktop audio: {device}")

    def _mic_processing_changed(self, *_args) -> None:
        self.project.audio.mic_denoise = self.denoise_combo.currentData() or "off"
        self.project.audio.mic_auto_gain = self.auto_gain_check.isChecked()
        self._mark_dirty()
        # webrtcdsp reads its settings when it starts: rebuild to apply.
        if self.engine is not None and self.project.audio.mic_enabled:
            self.restart_engine()
        level = self.denoise_combo.currentText()
        self._status(f"Noise suppression: {level}"
                     + (" · automatic gain" if self.project.audio.mic_auto_gain else ""))

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

        music = QGroupBox("Music (audio file)")
        music_grid = QGridLayout(music)
        self.media_edit = QLineEdit(self.project.audio.media_file)
        self.media_edit.textChanged.connect(lambda *_: self._mark_dirty())
        browse = QPushButton("Browse")
        browse.clicked.connect(self._pick_media)
        music_grid.addWidget(QLabel("File"), 0, 0)
        music_grid.addWidget(self.media_edit, 0, 1)
        music_grid.addWidget(browse, 0, 2)
        self.media_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.media_gain_slider.setRange(-30, 20)
        self.media_gain_slider.setValue(int(self.project.audio.media_gain_db))
        self.media_gain_slider.valueChanged.connect(self._media_gain_changed)
        self.media_gain_label = QLabel(f"{self.project.audio.media_gain_db:+.0f} dB")
        self.media_gain_label.setObjectName("accent")
        music_row = QHBoxLayout()
        music_row.addWidget(self.media_gain_slider, 1)
        music_row.addWidget(self.media_gain_label)
        music_grid.addWidget(QLabel("Gain"), 1, 0)
        music_grid.addLayout(music_row, 1, 1, 1, 2)
        music_note = QLabel("Music is mixed with the microphone through an "
                            "audiomixer at 48 kHz stereo.")
        music_note.setObjectName("muted")
        music_note.setWordWrap(True)
        music_grid.addWidget(music_note, 2, 0, 1, 3)
        layout.addWidget(music)

        desktop = QGroupBox("Desktop audio (optional)")
        desktop_grid = QGridLayout(desktop)
        self.desktop_combo = QComboBox()
        self.desktop_combo.addItem("Off", "")
        for source in config.list_audio_sources():
            if source.is_monitor:
                self.desktop_combo.addItem(source.description or source.name,
                                           source.name)
        index = self.desktop_combo.findData(self.project.audio.desktop_device)
        if index >= 0:
            self.desktop_combo.setCurrentIndex(index)
        self.desktop_combo.currentIndexChanged.connect(
            self._desktop_audio_changed)
        desktop_grid.addWidget(QLabel("Source"), 0, 0)
        desktop_grid.addWidget(self.desktop_combo, 0, 1)
        desktop_note = QLabel(
            "Monitor sources are the 'what you hear' streams from "
            "PipeWire/PulseAudio. They are mixed with the mic.")
        desktop_note.setObjectName("muted")
        desktop_note.setWordWrap(True)
        desktop_grid.addWidget(desktop_note, 1, 0, 1, 2)
        layout.addWidget(desktop)

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

        from . import models as model_catalog

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

    # -------------------------------------------------------------- project
    @property
    def scene(self) -> Scene:
        return self.project.scene

    def _current_effects(self) -> Effects:
        layer = self._effects_layer()
        return layer.effects if layer is not None else Effects()

    def _effects_layer(self) -> Layer | None:
        """The layer the Effects panel edits.

        That is the selected layer.  With nothing selected, or only an animation
        layer selected (effects mean nothing to those), it is the main picture:
        the top visible camera, screen, video, network or pattern layer.  Without
        this, clicking a frame or filter straight after opening the app did
        nothing but flash a status message.
        """
        if not hasattr(self, "preview"):
            return None
        selected = self.preview.selected_layer()
        if selected is not None and selected.kind != "particles":
            return selected
        for kinds in (self._CAPTURE_KINDS, ("image",)):
            # Live sources first; a still image only when it is the picture.
            # Never the stage: it is a background, not the picture.
            main = [layer for layer in self.scene.layers
                    if layer.visible and layer.kind in kinds
                    and getattr(layer, "role", "") != "backdrop"]
            if main:
                return max(main, key=lambda layer: layer.z)
        return selected

    def _apply_effects(self, layer: Layer) -> None:
        """Push effect values onto the running layer without rebuilding."""
        if self.engine is None:
            return
        apply = getattr(self.engine, "set_layer_effects", None)
        if apply is not None and not apply(layer.id, layer.effects):
            if (getattr(layer, "role", "") == "backdrop"
                    and layer.id in getattr(self.engine, "_layer_chains", {})):
                # This layer was built without a live effect rack (a stage's
                # fast path): rebuild so the effect is not silently dropped.
                self.restart_engine()
        self.preview.update()

    def _set_effect(self, attr: str, value) -> None:
        if getattr(self, "_syncing_effects", False):
            return
        layer = self._effects_layer()
        if layer is None:
            self._status("Select a layer in the preview or Layers tab first")
            return
        fx = layer.effects
        if attr == "brightness":
            fx.brightness = max(-1.0, min(1.0, float(value) / 100.0))
        elif attr in ("contrast", "saturation"):
            setattr(fx, attr, max(0.0, min(2.0, float(value) / 100.0)))
        elif attr == "hue":
            fx.hue = max(-1.0, min(1.0, float(value) / 180.0))
        elif attr in ("chroma_similarity", "chroma_smoothness"):
            setattr(fx, attr, max(0.0, min(1.0, float(value))))
        elif attr == "chroma_key":
            fx.chroma_key = str(value).strip() or "#00ff00"
        elif attr == "color_effect":
            choice = str(value or "none").lower()
            fx.color_effect = choice if choice in EFFECT_STYLES.values() else "none"
        elif attr == "mirror":
            fx.mirror = bool(value)
            if (layer.kind in ("image", "logo") and any(layer.crop)
                    and self.engine is not None):
                # The crop of a picture is rendered into its PNG and depends on
                # the mirror (left/right swap), so it cannot change live.
                self._mark_dirty()
                self.restart_engine()
                return
        elif attr == "frame_style":
            choice = str(value or "none").lower()
            fx.frame_style = choice if choice in overlay.WINDOW_FRAME_STYLES else "none"
            self._update_frame_controls()
        elif attr == "frame_color":
            fx.frame_color = str(value).strip() or "#5eead0"
        elif attr == "frame_matte":
            fx.frame_matte = str(value).strip() or "#0b0d10"
        elif attr in ("filter_amount", "filter2_amount"):
            setattr(fx, attr, max(0.0, min(1.0, float(value))))
        elif attr == "frame_width":
            fx.frame_width = max(1, min(48, int(value)))
        else:
            setattr(fx, attr, value)
        self._apply_effects(layer)
        self._mark_dirty()

    def _chroma_toggled(self, enabled: bool) -> None:
        if getattr(self, "_syncing_effects", False):
            return
        self._set_effect("chroma_enabled", bool(enabled))
        self._sync_effects_controls()

    def _frame_style_changed(self, _index: int = 0) -> None:
        self._set_effect("frame_style", self.frame_style.currentData())

    def _pick_frame_color(self) -> None:
        current = QColor(self.frame_color.text().strip())
        if not current.isValid():
            current = QColor("#5eead0")
        colour = QColorDialog.getColor(current, self, "Window frame colour")
        if not colour.isValid():
            return
        self.frame_color.setText(colour.name())
        self._set_effect("frame_color", colour.name())

    def _pick_frame_matte(self) -> None:
        current = QColor(self.frame_matte.text().strip())
        if not current.isValid():
            current = QColor("#0b0d10")
        colour = QColorDialog.getColor(current, self, "Matte colour")
        if not colour.isValid():
            return
        self.frame_matte.setText(colour.name())
        self._set_effect("frame_matte", colour.name())

    def _update_frame_controls(self) -> None:
        layer = self._effects_layer()
        enabled = bool(layer is not None
                       and layer.effects.frame_style != "none")
        for widget in getattr(self, "_frame_controls", []):
            widget.setEnabled(enabled)

    def _pick_chroma_color(self) -> None:
        current = QColor(self.chroma_color.text().strip())
        if not current.isValid():
            current = QColor("#00ff00")
        colour = QColorDialog.getColor(current, self, "Chroma key colour")
        if not colour.isValid():
            return
        self.chroma_color.setText(colour.name())
        self._set_effect("chroma_key", colour.name())

    def _apply_effect_preset(self, _index: int = 0) -> None:
        if getattr(self, "_syncing_effects", False):
            return
        name = self.effect_preset.currentData()
        if not name or name not in EFFECT_PRESETS:
            return
        layer = self._effects_layer()
        if layer is None:
            self._status("Select a layer before choosing an effect preset")
            return
        for attr, value in EFFECT_PRESETS[name].items():
            setattr(layer.effects, attr, float(value))
        self._apply_effects(layer)
        self._sync_effects_controls()
        self._mark_dirty()
        self._status(f"Effect preset: {name}")

    def _match_effect_preset(self, fx: Effects) -> str:
        for name, values in EFFECT_PRESETS.items():
            if all(abs(float(getattr(fx, attr, 0.0)) - value) < 1e-4
                   for attr, value in values.items()):
                return name
        return ""

    def _reset_effects(self) -> None:
        layer = self._effects_layer()
        if layer is None:
            self._status("Select a layer before resetting effects")
            return
        layer.effects = Effects()
        self._apply_effects(layer)
        self._sync_effects_controls()
        self._mark_dirty()
        self._status(f"Reset effects on {layer.name}")

    def _sync_effects_controls(self) -> None:
        """Mirror the selected layer's effect values into the panel."""
        if not hasattr(self, "effects_target"):
            return
        layer = self._effects_layer()
        self._syncing_effects = True
        try:
            enabled = layer is not None
            for widget in self._effect_controls:
                widget.setEnabled(enabled)
            if hasattr(self, "effect_grid"):
                self.browse_target.setText(
                    f"{layer.name}  \u00b7  {layer.kind}" if layer is not None
                    else "No layer selected")
                if layer is not None:
                    self._sync_filter_strength(layer.effects)
                    self._select_current_effect_tile()
                else:
                    self.filter_strength_row.setVisible(False)
            if layer is None:
                self.effects_target.setText("No layer selected")
                self.effects_hint.setText(
                    "Select a layer in the preview or Layers tab. Effects are "
                    "per layer, so changing one source never alters the rest "
                    "of the scene.")
                self._update_frame_controls()
                self._update_chroma_controls()
                self.effect_preset.setCurrentIndex(0)
                return
            fx = layer.effects
            self.effects_target.setText(f"{layer.name}  \u00b7  {layer.kind}")
            self.effects_hint.setText(
                "These settings apply only to the selected layer. Changes are "
                "written straight to the running graph.")
            self._effect_sliders["brightness"].setValue(
                int(round(fx.brightness * 100)))
            self._effect_sliders["contrast"].setValue(
                int(round(fx.contrast * 100)))
            self._effect_sliders["saturation"].setValue(
                int(round(fx.saturation * 100)))
            self._effect_sliders["hue"].setValue(int(round(fx.hue * 180)))
            style_index = self.effect_style.findData(fx.color_effect)
            self.effect_style.setCurrentIndex(style_index if style_index >= 0 else 0)
            self.effect_mirror.setChecked(bool(fx.mirror))
            frame_index = self.frame_style.findData(fx.frame_style)
            self.frame_style.setCurrentIndex(frame_index if frame_index >= 0 else 0)
            self.frame_color.setText(fx.frame_color or "#5eead0")
            self.frame_matte.setText(fx.frame_matte or "#0b0d10")
            self._effect_sliders["frame_width"].setValue(int(fx.frame_width))
            self._update_frame_controls()
            self._effect_sliders["chroma_similarity"].setValue(
                int(round(fx.chroma_similarity * 100)))
            self._effect_sliders["chroma_smoothness"].setValue(
                int(round(fx.chroma_smoothness * 100)))
            self.chroma_enabled.setChecked(bool(fx.chroma_enabled))
            self.chroma_color.setText(fx.chroma_key or "#00ff00")
            self._update_chroma_controls()
            name = self._match_effect_preset(fx)
            index = self.effect_preset.findData(name)
            self.effect_preset.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._syncing_effects = False

    def _update_chroma_controls(self) -> None:
        enabled = bool(getattr(self, "chroma_enabled", None)
                       and self.chroma_enabled.isChecked()
                       and self._effects_layer() is not None)
        for widget in getattr(self, "_chroma_controls", []):
            widget.setEnabled(enabled)

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
            self._sync_effects_controls()

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
        # Width, height and rate are three fields: rebuild once after the last
        # change instead of reopening the camera three times in a row.
        timer = getattr(self, "_camera_mode_timer", None)
        if timer is None:
            timer = self._camera_mode_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(300)
            timer.timeout.connect(self._apply_camera_mode)
        timer.start()
        width, height, fps = values
        mode = (f"{width}×{height}" if width and height else "auto size")
        self._status(f"Camera capture: {mode} @ {fps or 'auto'} fps")

    def _apply_camera_mode(self) -> None:
        if self.engine is not None:
            self.restart_engine()

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
        # Match the device as a whole name: "/dev/video1" is a substring of
        # "/dev/video10" (the virtual camera), which swapped a healthy camera
        # for a placeholder whenever the vcam reported an error.
        explicit = {layer_id: device for layer_id, device in cameras.items()
                    if re.search(re.escape(device.lower()) + r"(?![\w.-])", text)}
        if explicit:
            return explicit
        # Pool errors name their capture channel; blame only the cameras that
        # capture feeds, and nothing at all when it feeds a different kind.
        channel = re.search(r"anycam-src-\d+", text)
        if channel is not None and self.engine is not None:
            fed = self.engine.layers_for_channel(channel.group(0))
            if fed:
                return {layer_id: device for layer_id, device in cameras.items()
                        if layer_id in fed}
        # Only the camera source element identifies a camera failure.  The old
        # list also matched "no space left" and "failed to allocate", so a full
        # disk or a GPU allocation error from any element quarantined every
        # camera and swapped them for placeholder cards.
        return cameras if "v4l2src" in text else {}

    def _has_screen_layer(self) -> bool:
        """Whether this scene is using a portal screen capture."""
        return any(layer.kind == "screen" for layer in self.scene.layers)

    def _handle_pipeline_error(self, message: str) -> None:
        """Treat an unplugged or wedged camera as a temporary loss."""
        # Blame cameras only when the error is about a camera.  A camera node
        # that happens to be missing used to turn every error (an encoder, the
        # virtual camera) into "camera disconnected" and an immediate rebuild,
        # which repeated as long as the real error did.
        blamed = self._camera_error_layers(message)
        absent = self._missing_camera_layers()
        text = message.lower()
        about_cameras = (bool(blamed) or "v4l2src" in text
                         or (bool(absent) and _CAMERA_LOSS.search(text) is not None))
        missing = {**(absent if about_cameras else {}), **blamed}
        if not missing:
            self._status(message)
            self.stop_engine()
            self._status(message)  # keep the failure visible after finalisation
            return
        self._lost_cameras.update(missing)
        # A camera that fails again after a reconnect must not be reopened in
        # a tight loop; repeated opens are what wedges some UVC devices.
        self._camera_retry_ms = min(10000, int(self._camera_retry_ms * 1.5))
        if (self.engine is not None and self._has_screen_layer()
                and message.startswith("Camera source error:")):
            # Do not rebuild a PiP just because the camera failed.  A rebuild
            # stops the screen pipewiresrc and needs another portal remote; if
            # the share cannot be reopened the whole PiP disappears.  Drop the
            # failed capture and retry it on the existing canvas channel.
            dropped = [layer_id for layer_id in missing
                       if self.engine.drop_capture(layer_id)]
            if len(dropped) == len(missing):
                devices = ", ".join(sorted(set(missing.values())))
                self._status(
                    f"{devices} unavailable · screen preview kept live · "
                    f"retrying in {self._camera_retry_ms / 1000:.1f}s")
                self._camera_watch.start(self._camera_retry_ms)
                return
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
        if self.engine is not None and self._has_screen_layer():
            # Reattach the camera capture on its existing channel.  Rebuilding
            # here would stop the live screen pipewiresrc and lose the PiP.
            failed = [layer_id for layer_id in restored
                      if not self.engine.attach_capture(layer_id)]
            if failed:
                self._lost_cameras.update(
                    {layer_id: restored[layer_id] for layer_id in failed})
                self._camera_retry_ms = min(10000, int(self._camera_retry_ms * 1.7))
                self._camera_watch.start(self._camera_retry_ms)
                self._status(
                    "Camera returned but the live capture could not resume; "
                    f"retrying in {self._camera_retry_ms / 1000:.1f}s"
                )
                return
            self._status(
                "Camera reconnected: "
                + ", ".join(sorted(set(restored.values()))))
            if self._lost_cameras:
                self._camera_watch.start(self._camera_retry_ms)
            else:
                self._camera_retry_ms = 2000
                self._camera_watch.stop()
            return
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
        # A UVC replug renumbers the node (for example video0 -> video3).
        # Refresh a cached choice that no longer exists instead of adding a
        # dead source to the running PiP.
        if (not self._camera_choices
                or any(not Path(device).exists()
                       for _label, device in self._camera_choices)):
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
        self._sync_layout_combo()
        self._clear_history()
        self._refresh_scenes()
        self._refresh_layers()
        self._load_playlist_state()
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
        if self.engine is not None:
            self.restart_engine()

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
        if self.engine is not None:
            self.restart_engine()
        # The deleted scene's screen shares: end their portal captures once
        # the canvas no longer uses them (they used to run until exit).
        # _close_screen_session keeps a session another layer still shares.
        for layer in removed.layers:
            if layer.kind == "screen":
                self._close_screen_session(layer.id)

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
        elif kind == "lower_third":
            if not source:
                name, ok = QInputDialog.getText(self, "Lower third", "Name:")
                if not ok or not name.strip():
                    return None
                title, _ok = QInputDialog.getText(
                    self, "Lower third", "Title or subtitle (optional):")
                source = f"{name.strip()}|{title.strip()}"
            layer = Layer(new_id("layer"), "lower_third",
                          source.partition("|")[0] or "Lower third", source=source,
                          box=(0.04, 0.66, 0.50, 0.20), z=0, color="#ffffff",
                          background="#101418e6")
        elif kind == "particles":
            from . import particles
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
                    self._status(f"Screen capture unavailable: {exc}")
                    return layer
                attached = self.engine.add_layer(
                    layer, screen_fd=fd, screen_size=size)
            else:
                attached = self.engine.add_layer(layer)
            if not attached:
                self.restart_engine()
        return layer

    def _request_screen_cast(self, restore_token: str = "") -> portal.ScreenSession:
        """Open the system picker without freezing the Qt event loop.

        ``restore_token`` (from an earlier launch) lets the portal skip the
        picker for the same source; it is only passed when there is one.
        """
        session = self._open_screen_cast(
            {"restore_token": restore_token} if restore_token else {})
        # Keep the newest token for the next launch's saved screen layers.
        config.save_screen_restore_token(getattr(session, "restore_token", ""))
        return session

    def _open_screen_cast(self, options: dict) -> portal.ScreenSession:
        if self.headless:
            return portal.request_screen_cast(**options)
        app = QApplication.instance()
        if app is None:
            return portal.request_screen_cast(**options)
        dialog = QProgressDialog(
            "Choose a screen or window in the system picker…",
            "", 0, 0, self)
        dialog.setWindowTitle("Desktop capture")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setCancelButton(None)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.show()
        try:
            return portal.request_screen_cast(pump=app.processEvents, **options)
        finally:
            dialog.close()
            dialog.deleteLater()

    def _screen_session_in_use(self, session) -> bool:
        """Whether a layer that still exists is already showing ``session``."""
        return any(self._screen_sessions.get(layer.id) is session
                   for scene in self.project.scenes for layer in scene.layers)

    def _add_screen_layer(
            self, box: tuple[float, float, float, float] | None = None,
            *, new_share: bool = False) -> Layer | None:
        """Add a screen stream.

        The open portal stream is reused only when no layer is showing it any
        more (a removed share can be put back without another picker).  While
        a layer is using it, a second "screen" is a different share, so the
        portal is asked again; reusing the one stream made it impossible to
        add a second window or monitor without restarting the app.
        """
        session = self._last_screen_session
        reuse = (not new_share and session is not None and bool(session.streams)
                 and not self._screen_session_in_use(session))
        if reuse:
            print(
                f"[screen-session] reusing node={session.node_id} for a new "
                "screen layer; no portal request",
                file=sys.stderr, flush=True)
        else:
            try:
                session = self._request_screen_cast()
            except Exception as exc:  # noqa: BLE001 - the portal reports user cancellation too
                self._status(f"Screen capture unavailable: {exc}")
                print(f"AnyCam screen capture failed: {exc}", file=sys.stderr,
                      flush=True)
                if not self.headless:
                    QMessageBox.warning(self, "Screen capture", str(exc))
                return None
            self._last_screen_session = session
        stream = session.streams[0]
        size = (f"{stream.width}×{stream.height}"
                if stream.width > 1 and stream.height > 1 else "Screen")
        layer = Layer(new_id("layer"), "screen", f"Screen {size}",
                      source=str(session.node_id),
                      box=box or (0.0, 0.0, 1.0, 1.0), z=0)
        self._screen_sessions[layer.id] = session
        return self._insert_layer(layer)

    def _ensure_screen_sessions(self) -> bool:
        """Re-request portal streams for saved screen layers after a restart."""
        for layer in list(self.scene.layers):
            if layer.kind != "screen" or layer.id in self._screen_sessions:
                continue
            session = self._last_screen_session
            if session is None or not session.streams:
                try:
                    # Saved layers come back without a picker when the portal
                    # still honours last launch's choice.  Adding a new share
                    # never passes a token: the user is picking then.
                    session = self._request_screen_cast(
                        restore_token=config.load_screen_restore_token())
                except Exception as exc:  # noqa: BLE001
                    self._status(f"Screen capture unavailable: {exc}")
                    return False
                self._last_screen_session = session
            else:
                print(
                    f"[screen-session] reusing node={session.node_id} for "
                    f"saved screen layer {layer.id}; no portal request",
                    file=sys.stderr, flush=True)
            print(
                f"[screen-session] layer={layer.id} node={session.node_id} "
                f"fd={session.fd if session.fd >= 0 else 'on-demand'} streams="
                f"{[(s.width, s.height, s.source_type) for s in session.streams]}",
                file=sys.stderr, flush=True)
            layer.source = str(session.node_id)
            self._screen_sessions[layer.id] = session
        return True

    def _screen_fds(self) -> dict[str, int]:
        """One owned portal remote per live screen layer for this build.

        ``pipewiresrc`` closes its fd when its pipeline stops, so the session's
        original fd cannot be handed to a later rebuild.  ``take_remote``
        consumes it once and opens a fresh remote on the same portal session
        for each subsequent canvas.
        """
        fds: dict[str, int] = {}
        try:
            for layer_id, session in self._screen_sessions.items():
                if self.scene.layer(layer_id) is None:
                    continue
                fds[layer_id] = session.take_remote()
        except Exception:
            for fd in fds.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        return fds

    def _close_screen_session(self, layer_id: str) -> None:
        session = self._screen_sessions.pop(layer_id, None)
        if session is None:
            return
        if not any(other is session for other in self._screen_sessions.values()):
            session.close()

    def _close_screen_sessions(self) -> None:
        sessions = list(self._screen_sessions.values())
        self._screen_sessions.clear()
        self._last_screen_session = None
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
        self.remove_layers([item.data(Qt.ItemDataRole.UserRole) for item in items])

    def remove_layers(self, removed) -> None:
        """Remove layers by id from whatever selected them (list or canvas)."""
        removed = [layer_id for layer_id in removed
                   if layer_id and self.scene.layer(layer_id) is not None]
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
        # The portal session and the screen pipewiresrc stay alive: removing
        # a source must be reversible without a new picker or a black vcam.

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

    def _update_effects_device_note(self) -> None:
        from . import inference

        cuda = inference.gpu_available()
        self.effects_device_note.setText(
            f"Now running on: {inference.describe()}. Background, face and "
            "mask effects use these models; the GPU is shared with the canvas "
            "and the encoder, so the CPU is the default."
            + ("" if cuda else
               " To use the GPU, replace onnxruntime with onnxruntime-gpu and "
               "its CUDA/cuDNN libraries: pip uninstall onnxruntime && "
               "pip install 'onnxruntime-gpu[cuda,cudnn]'."))

    def _update_segmentation_note(self) -> None:
        from . import models as model_catalog

        rvm = model_catalog.CATALOG["rvm"]
        have = model_catalog.model_path("rvm") is not None
        wanted = self.segmentation_combo.currentData() == "rvm"
        downloading = self._model_download is not None
        self.model_download_button.setVisible(wanted and not have)
        self.model_download_button.setEnabled(not downloading)
        if not wanted:
            text = ("MediaPipe selfie segmentation, bundled with AnyCam "
                    "(Apache-2.0). Fast; edges are softer.")
        elif have:
            text = (f"In use: {rvm.label}. Soft edges and hair, stable between "
                    f"frames. Licence {rvm.license}, from "
                    f"<a href='{rvm.source}'>its author</a>.")
        else:
            text = (f"{rvm.note} Click Download to fetch it once "
                    f"({rvm.size / 1e6:.0f} MB from <a href='{rvm.source}'>"
                    "github.com/PeterL1n</a>, checked against a pinned SHA-256). "
                    "Until then the standard model is used.")
        if downloading:
            done, total = self._model_download.get("progress", (0, rvm.size))
            text = f"Downloading… {done / 1e6:.1f} / {total / 1e6:.0f} MB"
        self.segmentation_note.setText(text)

    def _segmentation_changed(self, _index: int = 0) -> None:
        from . import segment

        value = str(self.segmentation_combo.currentData() or "mediapipe")
        self.project.output.segmentation_model = value
        self._mark_dirty()
        segment.set_model(value)
        self._update_segmentation_note()
        self.log_event(f"person cut-out model: chosen {value}, "
                       f"in use {segment.active_model()}")
        self._status(f"Person cut-out: {self.segmentation_combo.currentText()}"
                     + ("" if segment.active_model() == value else
                        " (download it to use it)"))

    def _download_rvm(self) -> None:
        """Fetch the RVM model on a thread; the UI polls for progress."""
        from . import models as model_catalog

        if self._model_download is not None:
            return
        state: dict = {"progress": (0, model_catalog.CATALOG["rvm"].size)}
        self._model_download = state

        def work() -> None:
            try:
                model_catalog.download(
                    "rvm", progress=lambda done, total: state.__setitem__(
                        "progress", (done, total)))
                state["ok"] = True
            except Exception as exc:  # noqa: BLE001 - shown to the user
                state["error"] = str(exc)
            state["finished"] = True

        threading.Thread(target=work, name="anycam-model-download", daemon=True).start()
        self._model_download_timer.start()
        self._update_segmentation_note()

    def _poll_model_download(self) -> None:
        from . import segment

        state = self._model_download
        if state is None:
            self._model_download_timer.stop()
            return
        if not state.get("finished"):
            self._update_segmentation_note()
            return
        self._model_download_timer.stop()
        self._model_download = None
        if state.get("ok"):
            segment.reset()          # pick the new model up on the next refresh
            self._status("Robust Video Matting downloaded and in use")
            self.log_event("model rvm downloaded and verified")
        else:
            error = state.get("error", "unknown error")
            self._status(f"Model download failed: {error}")
            self.log_event(f"model rvm download failed: {error}")
        self._update_segmentation_note()

    def _effects_device_changed(self, _index: int = 0) -> None:
        from . import inference

        value = str(self.effects_device_combo.currentData() or "cpu")
        self.project.output.effects_device = value
        self._mark_dirty()
        inference.set_preference(value)
        self._update_effects_device_note()
        self.log_event(f"AI effects device: {inference.describe()}")
        self._status(f"AI effects run on: {inference.describe()}")

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
        from . import inference, segment

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

    def _screen_source_fps(self) -> float | None:
        """Frames per second actually arriving from the screen layer(s).

        Returns None until there is a previous sample, so the label does not
        flash a meaningless number on the first tick.
        """
        engine = self.engine
        if engine is None:
            return None
        health = getattr(engine.report, "source_health", {}) or {}
        total = 0
        found = False
        for layer in self.scene.layers:
            if layer.kind != "screen":
                continue
            entry = health.get(layer.id)
            if entry is None:
                continue
            found = True
            total += int(entry.get("frames") or 0)
        if not found:
            return None
        now = time.monotonic()
        previous = getattr(self, "_screen_fps_prev", None)
        self._screen_fps_prev = (total, now)
        if previous is None or now <= previous[1]:
            return None
        return max(0.0, (total - previous[0]) / (now - previous[1]))


    def _screen_sizes(self) -> dict[str, tuple[int, int]]:
        """Portal stream size per screen layer (crop base and node check)."""
        sizes: dict[str, tuple[int, int]] = {}
        for layer_id, session in self._screen_sessions.items():
            stream = session.streams[0] if session.streams else None
            # A 1x1 (or 0) report is unusable and used to become a square
            # letterbox that showed only part of the shared picture.
            if (stream is not None and stream.width > 1
                    and stream.height > 1):
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
        else:
            if self.live:
                self._status(f"Will publish to {config.redact_url(url)} when preview starts")
            else:
                self._status("Stream stopped")

    # --------------------------------------------------------------- engine
    def _record_path(self) -> Path:
        """Path for the current recording session (one file, no segments)."""
        if self._recording_path:
            return Path(self._recording_path)
        stamp = self._session_stamp or overlay.timestamped_name("anycam", "")
        self._session_stamp = stamp
        # A canvas resize starts a new file (see _rotate_recording): -2, -3, ...
        suffix = f"-{self._segment_index + 1}" if self._segment_index else ""
        path = (config.recordings_dir(self.project.output.record_dir)
                / f"{stamp}{suffix}.mp4")
        self._recording_path = str(path)
        return path

    def _start_recorder(self) -> bool:
        if self._recorder is not None:
            return True
        path = self._record_path()
        recorder = Recorder()
        audio = (self.audio_on or bool(self.project.audio.media_file)
                 or bool(self.project.audio.desktop_device))
        if not recorder.start(str(path), self.project.output.canvas_w,
                              self.project.output.canvas_h, self.project.output.fps,
                              audio=audio,
                              codec=recording_codec(self.project.output.encoder)):
            self._status(f"Recording could not start: {recorder.error}")
            return False
        self._recorder = recorder
        self._recording_path = str(path)
        return True

    def _rotate_recording(self) -> None:
        """Close the current file and continue in a new one.

        One MP4 has a single picture size.  Feeding frames of another size
        into it corrupts or aborts the recording, so a resize ends the file
        (it is saved and listed) and the rebuilt canvas records into the next.
        """
        if self.engine is not None:
            self.engine.detach_recorder()
        self._finish_recorder()
        self._segment_index += 1

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

    def show_vcam_hint(self, text: str) -> None:
        """Keep a dismissible hint above the preview until replaced."""
        self.vcam_hint.setText(text)
        self.vcam_hint.show()
        self.vcam_dismiss.show()
        self._status(text)

    def dismiss_vcam_hint(self) -> None:
        self.vcam_hint.hide()
        self.vcam_dismiss.hide()

    def hold_vcam_device(self) -> bool:
        """Open the loopback node at launch so apps can select it early.

        No root prompt here: the button still runs the setup path when the
        device is missing.  The idle card in ``VcamOutput`` is what makes the
        node stream before the preview exists.
        """
        if self._vcam_output is not None and self._vcam_output.active:
            return True
        device = self.project.output.vcam_device or config.loopback_device()
        output = VcamOutput()
        if not output.start(device, self.project.output.canvas_w,
                            self.project.output.canvas_h,
                            self.project.output.fps,
                            backend=self._vcam_backend()):
            return False
        self._vcam_output = output
        self._note_vcam_backend(output)
        return True

    def _vcam_backend(self) -> str:
        backend = str(getattr(self.project.output, "vcam_backend", "v4l2"))
        return backend if backend in ("v4l2", "pipewire", "both") else "v4l2"

    def _note_vcam_backend(self, output: VcamOutput) -> None:
        """Log what is published, and why a requested PipeWire node is not."""
        if getattr(output, "publishes_pipewire", False):
            self.log_event("virtual camera: PipeWire camera node “AnyCam” published")
        if getattr(output, "pipewire_error", ""):
            self.log_event(f"virtual camera: no PipeWire node ({output.pipewire_error})")
        self._update_vcam_note()

    def _update_vcam_note(self) -> None:
        note = getattr(self, "vcam_note", None)
        if note is None:
            return
        backend = self._vcam_backend()
        output = getattr(self, "_vcam_output", None)
        parts = []
        if backend in ("v4l2", "both"):
            parts.append("The loopback device is set up automatically on first "
                         "launch (one password prompt) and works in every app.")
        if backend in ("pipewire", "both"):
            parts.append("The PipeWire camera needs no driver, but browsers list "
                         "it only with PipeWire cameras enabled: Firefox "
                         "about:config media.webrtc.camera.allow-pipewire, "
                         "Chrome/Discord/Vesktop --enable-features=WebRtcPipeWireCamera.")
        if output is not None and output.active and getattr(output, "pipewire_error", ""):
            parts.append(f"PipeWire camera not published: {output.pipewire_error}.")
        parts.append("The camera source itself is chosen in Sources or the + menu.")
        note.setText(" ".join(parts))

    def _vcam_backend_changed(self) -> None:
        """Republish the virtual camera on the chosen backend(s) right away."""
        value = str(self.vcam_backend_combo.currentData() or "v4l2")
        if value == self._vcam_backend():
            return
        self.project.output.vcam_backend = value
        self._mark_dirty()
        output = self._vcam_output
        if output is not None and output.active:
            if self.engine is not None:
                self.engine.detach_vcam()
            self._finish_vcam_output()
            if value in ("v4l2", "both"):
                self.setup_vcam_if_needed()
            if self.hold_vcam_device() and self.vcam_on and self.engine is not None:
                self.engine.attach_vcam(self._vcam_output)
            if getattr(self._vcam_output, "publishes_v4l2", False):
                self.ensure_vcam_visible()
        self._update_vcam_note()
        self._status(f"Virtual camera: {self.vcam_backend_combo.currentText()}")

    def setup_vcam_if_needed(self) -> bool:
        """Offer the one-time system setup that makes Discord see AnyCam.

        Runs at launch, before the device is held (a reload needs it closed).
        One password prompt installs the driver settings permanently; every
        later launch is ready with no prompt.  True when the camera is ready.
        """
        if self._vcam_backend() == "pipewire":
            # A PipeWire node needs no kernel module and no root.
            return True
        dev = self.project.output.vcam_device or config.loopback_device()
        reason = config.vcam_setup_needed(dev)
        if not reason:
            return True
        self.log_event(f"virtual camera setup needed: {reason}")
        if self.headless:
            return False
        answer = QMessageBox.question(
            self, "Set up the virtual camera",
            "Discord, Vesktop, Chrome and other apps cannot see AnyCam's "
            f"virtual camera yet: {reason}.\n\n"
            "AnyCam can fix this once, for good. It installs the camera "
            "driver settings and reloads the driver; your password is asked "
            "one time. After that the camera is ready whenever AnyCam is "
            "open.\n\nSet it up now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            self._status("Virtual camera not set up: Discord will not list AnyCam")
            return False
        self._status("Setting up the virtual camera…")
        try:
            error = self._run_with_ui(lambda: config.setup_vcam_system(dev))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            error = str(exc)
        if error:
            self.log_event(f"virtual camera setup failed: {error}")
            QMessageBox.warning(self, "Virtual camera", f"The setup did not finish:\n{error}")
            return False
        self.log_event("virtual camera set up permanently (exclusive_caps=1)")
        self._status("Virtual camera ready: select AnyCam in Discord")
        return True

    def ensure_vcam_visible(self, device: str | None = None) -> None:
        """Make Chromium-based apps list the node AnyCam is feeding.

        ``exclusive_caps=1`` only advertises capture while a writer streams,
        but udev caches the capabilities from device-add time.  Without a
        change event Discord, Vesktop and Chrome keep skipping AnyCam even
        though the module parameter is correct.  This runs once per device and
        only asks for authorization when the cached value is actually wrong.
        """
        if self._vcam_backend() == "pipewire":
            return
        dev = (device or self.project.output.vcam_device
               or config.loopback_device())
        if dev in self._vcam_udev_attempted:
            return
        if config.vcam_exclusive_caps() is not True:
            return
        caps = config.vcam_udev_capabilities(dev)
        if caps is None or "capture" in caps:
            return
        self._vcam_udev_attempted.add(dev)
        result = self._vcam_udev_result = {}
        self._vcam_udev_timer.start()

        def run() -> None:
            # exclusive_caps only advertises capture once a buffer is flowing;
            # give the idle feed a moment to reach v4l2sink before rewiring.
            time.sleep(0.5)
            try:
                result["error"] = config.refresh_vcam_udev(dev)
            except Exception as exc:  # noqa: BLE001 - a hint must not crash the GUI
                result["error"] = str(exc)

        self._vcam_udev_thread = threading.Thread(
            target=run, name="anycam-vcam-udev", daemon=True)
        self._vcam_udev_thread.start()

    def _vcam_udev_poll(self) -> None:
        thread = self._vcam_udev_thread
        if thread is not None and thread.is_alive():
            return
        self._vcam_udev_timer.stop()
        error = str(self._vcam_udev_result.get("error") or "")
        if error:
            self.log_event(
                "virtual camera: Chromium visibility refresh failed — "
                f"{error}")
            return
        self.log_event(
            "virtual camera: announced to Chromium apps (Discord, Chrome)")
        if hasattr(self, "_refresh_diag_state"):
            self._refresh_diag_state()

    def feed_held_vcam(self) -> bool:
        """Make the device held at launch follow the canvas automatically.

        The "Virtual camera ready / start the preview" card is only a
        placeholder: once the engine exists, the held node should show the
        composed scene without a second button press.  The button still turns
        the feed off again.
        """
        output = self._vcam_output
        if output is None or not output.active:
            return False
        self.vcam_on = True
        if hasattr(self, "vcam_button"):
            self.vcam_button.setText("Virtual camera: on")
        if self.engine is not None and not self.engine.attach_vcam(output):
            self._status(self.engine.report.vcam_error
                         or "Virtual camera could not start")
            return False
        return True

    def enable_held_vcam(self) -> bool:
        """Feed the canvas through the loopback node held at launch.

        The idle card says "Start the preview", but a held node whose feed is
        never switched stays on that near-black card forever, which a consumer
        cannot tell apart from a dead camera.  Once AnyCam owns the device, the
        canvas is the virtual camera's picture; turning the button off still
        returns it to the card.
        """
        output = self._vcam_output
        if output is None or not output.active:
            return False
        self.vcam_on = True
        button = getattr(self, "vcam_button", None)
        if button is not None:
            button.setText("Virtual camera: on")
        if self.engine is not None:
            self.engine.attach_vcam(output)
        return True

    def note_vcam_consumers(self) -> None:
        """Warn about apps that opened the node before AnyCam fed it.

        That open fails with EIO and the app shows nothing until it re-opens
        the camera; nothing outside the app can repair it.
        """
        output = self._vcam_output
        if output is None or not output.device:
            return
        names = config.vcam_consumers(output.device)
        if not names:
            return
        label = ", ".join(names[:2]) + ("…" if len(names) > 2 else "")
        self.show_vcam_hint(
            f"{label} already had {Path(output.device).name} open before AnyCam "
            "started feeding it. If it shows no picture, re-select “AnyCam” "
            "in that app's camera list.")

    def _start_vcam_output(self, setup: bool = False) -> bool:
        device = self.project.output.vcam_device or config.loopback_device()
        width = self.project.output.canvas_w
        height = self.project.output.canvas_h
        fps = self.project.output.fps
        output = self._vcam_output
        if output is not None and output.active:
            matches = getattr(output, "matches", None)
            serves = (matches(device, self._vcam_backend()) if callable(matches)
                      else output.device == device)
            if not output.error and serves:
                # Keep the node open while the canvas changes.  OBS's Linux
                # virtual camera is a separate output with one fixed mode;
                # reopening v4l2sink for every canvas resize kills the handle
                # Discord is reading.  The engine tap scales into this mode.
                if self._vcam_canvas_size != (width, height):
                    self._vcam_canvas_size = (width, height)
                    out_w = int(getattr(output, "width", width))
                    out_h = int(getattr(output, "height", height))
                    if (out_w, out_h) != (width, height):
                        self.log_event(
                            f"virtual camera: canvas {width}×{height} scaled to "
                            f"{out_w}×{out_h}")
                return True
            # A sink error leaves the pipeline object alive but dead; a
            # changed device needs new caps.  Only those justify a reopen.
            output.stop()
            self._vcam_output = None
        output = VcamOutput()
        backend = self._vcam_backend()
        if setup and backend != "pipewire" and not Path(device).exists():
            # Creating the device asks polkit, which can take a minute; run the
            # start off the UI thread so the window stays alive meanwhile.
            started = self._run_with_ui(
                lambda: output.start(device, width, height, fps, setup=True,
                                     backend=backend))
        else:
            started = output.start(device, width, height, fps,
                                   setup=setup and backend != "pipewire",
                                   backend=backend)
        if not started:
            self.log_event(
                f"virtual camera: {output.error or 'could not start'}")
            self._status(output.error or "Virtual camera could not start")
            return False
        self._vcam_output = output
        self._vcam_canvas_size = (width, height)
        out_w = int(getattr(output, "width", width))
        out_h = int(getattr(output, "height", height))
        if (out_w, out_h) != (width, height):
            self.log_event(
                f"virtual camera: canvas {width}×{height} scaled to "
                f"{out_w}×{out_h}")
        self._note_vcam_backend(output)
        if not self.headless and getattr(output, "publishes_v4l2", True):
            self.ensure_vcam_visible(device)
        return True

    def _run_with_ui(self, function):
        """Run a blocking call on a thread while Qt keeps handling events."""
        if self.headless:
            return function()
        result: dict = {}

        def work() -> None:
            try:
                result["value"] = function()
            except Exception as exc:  # noqa: BLE001 - re-raised on the UI thread
                result["error"] = exc

        thread = threading.Thread(target=work, name="anycam-blocking",
                                  daemon=True)
        thread.start()
        app = QApplication.instance()
        while thread.is_alive():
            if app is not None:
                app.processEvents()
            thread.join(0.02)
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _recover_vcam(self) -> bool:
        """Repair a virtual camera that dropped while the canvas kept running.

        A v4l2sink error only kills that output pipeline; it is not the user
        turning the camera off.  Flipping ``vcam_on`` in response made every
        later rebuild build without the tap, so the held node sat on the dark
        "ready" card and Discord showed a black camera until the button was
        pressed again.  Rebuild the output and reattach it instead, throttled
        so a device that stays unavailable does not spin the GUI thread.
        """
        if not self.vcam_on or self.engine is None:
            return False
        now = time.monotonic()
        if now - self._vcam_retry_at < 2.0:
            return False
        self._vcam_retry_at = now
        output = self._vcam_output
        if output is not None and (not output.active or output.error):
            self.log_event(
                f"virtual camera: rebuilding output after "
                f"{output.error or 'sink stopped'}")
            self._finish_vcam_output()
            output = None
        if output is None:
            if not self._start_vcam_output(setup=False):
                return False
            output = self._vcam_output
        if output is None:
            return False
        if not self.engine.attach_vcam(output):
            self.log_event(
                f"virtual camera: reattach failed "
                f"({self.engine.report.vcam_error or 'no canvas tap'})")
            return False
        self._vcam_error_shown = ""
        self.vcam_button.setText("Virtual camera: on")
        self.log_event("virtual camera: recovered and re-attached to the canvas")
        self._status("Virtual camera re-attached to the canvas")
        return True

    def _finish_vcam_output(self) -> None:
        output = self._vcam_output
        self._vcam_output = None
        self._vcam_canvas_size = None
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
        if any(layer.kind == "phone" for scene in self.project.scenes
               for layer in scene.layers):
            # A saved scene with a phone: listen so the phone can reconnect.
            self._phone_link(quiet=True)
        if not self._ensure_screen_sessions():
            return
        self.project.output.canvas_w = self._even(self.width_spin.value())
        self.project.output.canvas_h = self._even(self.height_spin.value())
        self.project.output.fps = self.fps_spin.value()
        self.project.output.vcam_device = self.vcam_edit.text().strip() or config.loopback_device()
        self.project.output.screen_flip = str(
            self.screen_flip_combo.currentData() or "automatic")
        self.project.output.rtmp_url = self.rtmp_edit.text().strip()
        self.project.audio.mic_enabled = self.audio_on
        self.project.audio.media_file = self.media_edit.text().strip()
        self.preview.canvas_size = (self.project.output.canvas_w,
                                    self.project.output.canvas_h)
        if self.playlist_ids and self.playlist_index < 0:
            # The first item is what the preview starts on (it was added
            # visible).  Leaving the index at -1 made the first timer step
            # "advance" to that same item, showing it for two intervals.
            self.playlist_index = 0
            self.scene.playlist.index = 0
        if self.playlist_index >= 0 and self.playlist_ids:
            for position, layer_id in enumerate(self.playlist_ids):
                layer = self.scene.layer(layer_id)
                if layer is not None:
                    layer.visible = position == self.playlist_index
        if (self.recording and self._recorder is not None
                and self._recorder.requested_size not in (
                    None, (self.project.output.canvas_w,
                           self.project.output.canvas_h))):
            # The Output tab changed the canvas while recording.  One MP4 has
            # one picture size, so the rebuilt canvas records to a new file;
            # feeding it into the old one dropped every frame.
            self._rotate_recording()
        if self.recording and not self._start_recorder():
            self.recording = False
            self.record_button.setText("Record")
            return
        recorder = self._recorder
        if self.vcam_on and not self._start_vcam_output(setup=True):
            # Keep the request: the held node is still ours, and _recover_vcam
            # retries.  Clearing vcam_on here made this and every later
            # rebuild build without the tap, leaving the dark ready card on
            # the device while the preview looked fine.
            error = (self._vcam_output.error if self._vcam_output is not None
                     else "virtual camera could not start")
            print(f"[vcam] {error}", file=sys.stderr, flush=True)
        vcam_output = self._vcam_output if self.vcam_on else None
        # In crop mode the selected layer is built uncropped, so the editor's
        # window is drawn over the picture it is actually trimming.
        crop_preview = self.preview.selected_id if self.preview.edit_mode == "crop" else ""
        self._crop_preview_layer_id = crop_preview
        try:
            self.engine = AnyCamEngine(
                self.project,
                recorder=recorder,
                # Without an owned output the engine would build its own
                # v4l2sink, opening the loopback node as a second writer.
                vcam=self.vcam_on and vcam_output is not None,
                vcam_output=vcam_output,
                preview=True,
                # A recording that has an audio track keeps it fed (silence if
                # the microphone is off) so its muxer never waits on a dead pad.
                audio=(self.audio_on or bool(self.project.audio.media_file)
                       or bool(self.project.audio.desktop_device)
                       or (recorder is not None and recorder.has_audio)),
                # Only an explicit Go Live publishes; a saved URL is inert.
                rtmp=self.project.output.rtmp_url if self.live else None,
                screen_fds=self._screen_fds(),
                screen_sizes=self._screen_sizes(),
                preview_crop_layer=crop_preview,
                preview_target=self.preview.display_size(),
                preview_gl_sink=(self._attach_gpu_preview
                                 if self.preview.video_view is not None else None),
                skip_layers=set(self._lost_cameras),
                pool=self._source_pool,
                on_frame=self._on_frame,
            ).build()
            self.engine.start()
            gpu = getattr(self.engine, "preview_backend", "") == "gpu"
            if self.preview.video_view is not None and not gpu:
                self.log_event("preview: GPU preview did not start; using the image preview")
                self.preview.disable_gpu()
            self.preview.set_gpu_live(gpu)
            self.preview.layer_sizes = self.engine.source_sizes()
            if self.vcam_on and not self.engine.report.vcam_active:
                # The held output exists but this canvas has no tap: reattach
                # now instead of waiting for the next status tick.
                self._vcam_retry_at = 0.0
                self._recover_vcam()
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
        self._screen_fps_prev = None
        self._source_status = ()
        self.status_chip.setText("WAITING")
        self.start_button.setText("Stop preview")
        # 16 ms so a 60 fps canvas is actually shown at 60 fps.  The preview
        # image is capped at 960x540 (~2.1 MB per copy), which fits the frame
        # budget; a 33 ms tick capped the whole preview at 30 fps.
        self.timer.start(16)
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

    def stop_engine(self, *, clear_preview: bool = True) -> None:
        self.timer.stop()
        if self.engine is not None:
            report = self.engine.stop()
            if report.get("record_path"):
                self.recording_segments.append(report["record_path"])
                self._status(f"Saved {report['record_path']}")
            self.engine = None
        # The pool keeps a screen capture alive briefly so an immediate
        # rebuild reuses it (a fresh damage-driven portal stream can be black
        # until the window repaints).  A real stop must release it afterwards.
        QTimer.singleShot(int(SCREEN_IDLE_GRACE_S * 1000) + 200,
                          self._source_pool.sweep)
        self.status_chip.setText("STOPPED")
        self.start_button.setText("Start preview")
        if clear_preview:
            self.preview.set_gpu_live(False)
            self.preview.image = None
            self.preview.layer_sizes = {}
            self.preview.update()

    def restart_engine(self) -> None:
        was_running = self.engine is not None
        if was_running:
            # Keep the last frame on screen while the replacement graph is
            # built.  Clearing it here made every scene/resolution change look
            # like the preview had disappeared, especially when screen capture
            # or the vcam took a few seconds to come back.
            self.stop_engine(clear_preview=False)
            self.start_engine()

    def eventFilter(self, watched, event):
        grid = getattr(self, "effect_grid", None)
        if watched is grid and event.type() == QEvent.Type.Show and self._thumb_queue:
            self._thumb_timer.start()
        return super().eventFilter(watched, event)

    # ---------------------------------------------------- desktop integration
    def start_desktop_integration(self) -> None:
        """A tray icon and global shortcuts through the portal, when present."""
        from PySide6.QtWidgets import QSystemTrayIcon

        from . import shortcuts

        if self._tray is None and QSystemTrayIcon.isSystemTrayAvailable():
            icon = self.windowIcon()
            if icon.isNull():
                icon = QIcon.fromTheme("camera-web", QIcon.fromTheme("camera-video"))
            tray = QSystemTrayIcon(icon, self)
            tray.setToolTip("AnyCam")
            menu = QMenu(self)
            menu.aboutToShow.connect(lambda: self._fill_tray_menu(menu))
            self._fill_tray_menu(menu)
            tray.setContextMenu(menu)
            tray.activated.connect(self._tray_activated)
            tray.show()
            self._tray = tray
        if self._global_shortcuts is None:
            self._global_shortcuts = shortcuts.GlobalShortcuts(
                self._on_global_shortcut, log=self.log_event,
                on_state=self._update_global_shortcuts_label)
            self._global_shortcuts.start()
        self._update_global_shortcuts_label()

    def _stop_desktop_integration(self) -> None:
        tray, self._tray = self._tray, None
        if tray is not None:
            tray.hide()
            tray.deleteLater()
        keys, self._global_shortcuts = self._global_shortcuts, None
        if keys is not None:
            keys.close()

    def _tray_activated(self, reason) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window_visible()

    def _toggle_window_visible(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _fill_tray_menu(self, menu: QMenu) -> None:
        """Rebuilt each time it opens, so the checks and scenes are current."""
        menu.clear()
        shown = self.isVisible() and not self.isMinimized()
        menu.addAction("Hide AnyCam" if shown else "Show AnyCam",
                       self._toggle_window_visible)
        menu.addSeparator()
        for label, checked, slot in (
                ("Virtual camera", self.vcam_on, self.toggle_vcam),
                ("Record", self.recording, self.toggle_record),
                ("Mute microphone", bool(self.project.audio.mic_muted), self.toggle_mic_mute)):
            action = menu.addAction(label, slot)
            action.setCheckable(True)
            action.setChecked(bool(checked))
        scenes = menu.addMenu("Scenes")
        for index, scene in enumerate(self.project.scenes):
            action = scenes.addAction(scene.name, lambda i=index: self.switch_scene(i))
            action.setCheckable(True)
            action.setChecked(scene.id == self.project.active_scene)
        menu.addSeparator()
        menu.addAction("Quit AnyCam", self.close)

    def _on_global_shortcut(self, shortcut: str) -> None:
        """A portal global shortcut, pressed while any app has focus."""
        self.log_event(f"global shortcut: {shortcut}")
        if shortcut.startswith("scene-") and shortcut[6:].isdigit():
            index = int(shortcut[6:]) - 1
            if 0 <= index < len(self.project.scenes):
                self.switch_scene(index)
        elif shortcut in ("scene-next", "scene-previous"):
            ids = [scene.id for scene in self.project.scenes]
            if ids:
                current = ids.index(self.project.active_scene) if self.project.active_scene in ids else 0
                step = 1 if shortcut == "scene-next" else -1
                self.switch_scene((current + step) % len(ids))
        elif shortcut == "mute":
            self.toggle_mic_mute()
        elif shortcut == "record":
            self.toggle_record()
        elif shortcut == "vcam":
            self.toggle_vcam()
        self._update_global_shortcuts_label()

    def _update_global_shortcuts_label(self) -> None:
        label = getattr(self, "global_shortcuts_label", None)
        if label is None:
            return
        keys = self._global_shortcuts
        if keys is None:
            text = "Global shortcuts: not started."
        elif keys.state == "bound":
            text = ("Global shortcuts (work while other apps have focus): Ctrl+Alt+1…9 "
                    "scenes, Ctrl+Alt+N / P next / previous scene, Ctrl+Alt+M mute, "
                    "Ctrl+Alt+R record, Ctrl+Alt+V virtual camera. Change them in "
                    "your desktop's shortcut settings.")
        elif keys.state == "binding":
            text = "Global shortcuts: waiting for the desktop to confirm them…"
        else:
            text = f"Global shortcuts unavailable ({keys.state.removeprefix('failed: ')}); "\
                   "the shortcuts above work while AnyCam has focus."
        label.setText(text)

    # ------------------------------------------------------------- phone link
    def _phone_link(self, *, quiet: bool = False):
        """The running phone link, started on first use (None if it cannot)."""
        if self._phone_service is not None and self._phone_service.running:
            return self._phone_service
        from .phone.server import default_service

        service = self._phone_service or default_service(
            on_event=self._phone_events.event.emit)
        try:
            service.start()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.log_event(f"phone link: {exc}")
            if quiet or self.headless:
                self._status(f"Phone link unavailable: {exc}")
            else:
                QMessageBox.warning(self, "Phone camera", f"The phone link could not start:\n{exc}")
            return None
        self._phone_service = service
        self.log_event(f"phone link listening on {service.page_url()}")
        return service

    def _stop_phone_link(self) -> None:
        service, self._phone_service = self._phone_service, None
        if service is not None:
            service.stop()

    def _phone_name(self, device: str) -> str:
        service = self._phone_service
        if service is not None:
            return service.pairing.name(device)
        from .phone.auth import Pairing

        return Pairing(config.state_dir() / "phones.json").name(device)

    def _pair_phone(self) -> tuple[str, str] | None:
        """Show the QR code until a phone streams; its ``(device, name)``."""
        service = self._phone_link()
        if service is None:
            return None
        dialog = PhonePairDialog(service, self, log=self.log_event)
        self._phone_dialog = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._phone_dialog = None
        if not accepted or not dialog.device:
            return None
        return dialog.device, dialog.device_name or dialog.device

    def _on_phone_event(self, event: str, device: str, detail: str) -> None:
        """Phone-link events, delivered on the UI thread."""
        if self._phone_dialog is not None:
            self._phone_dialog.on_phone_event(event, device, detail)
        name = self._phone_name(device) if event == "failed" else detail
        message = {
            "paired": f"Phone paired: {name}",
            "connecting": f"{name} is connecting…",
            "streaming": f"{name} is live",
            "stopped": f"{name} stopped its camera",
            "disconnected": f"{name} disconnected",
            "failed": f"{name} could not connect: {detail}",
        }.get(event)
        if message:
            self.log_event(f"phone {device}: {message}")
            self._status(message)

    def closeEvent(self, event) -> None:
        """Finalise the recording and persist edits before the process exits."""
        self._stop_thumbnails()
        self.timer.stop()
        self._camera_watch.stop()
        self._transform_timer.stop()
        self._autosave_timer.stop()
        self.preview.cancel_pending_edits()
        if self.engine is not None:
            self.stop_engine()
        self._finish_recorder()
        self._finish_vcam_output()
        self._stop_phone_link()
        self._stop_desktop_integration()
        self._source_pool.stop()
        self._save_if_needed()
        self._close_screen_sessions()
        event.accept()

    def _stop_thumbnails(self) -> None:
        timer = getattr(self, "_thumb_timer", None)
        if timer is not None:
            timer.stop()
        queue = getattr(self, "_thumb_queue", None)
        if queue is not None:
            queue.clear()

    def shutdown(self) -> None:
        """Backstop for QApplication.aboutToQuit and headless teardown."""
        self._stop_thumbnails()
        self.timer.stop()
        self._camera_watch.stop()
        self._transform_timer.stop()
        self._autosave_timer.stop()
        self.preview.cancel_pending_edits()
        if self.engine is not None:
            self.stop_engine()
        self._finish_recorder()
        self._finish_vcam_output()
        self._stop_phone_link()
        self._stop_desktop_integration()
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
                self.engine.detach_recorder()
            self._finish_recorder()
            return
        self.recording = True
        self._segment_index = 0
        self.recording_segments = []
        self._session_stamp = overlay.timestamped_name("anycam", "")
        self._recording_path = ""
        self.record_button.setText("Stop recording")
        if self.engine is not None:
            if not self._start_recorder():
                self.recording = False
                self.record_button.setText("Record")
                return
            tap_codec = self.engine.record_codec()
            if tap_codec is not None and tap_codec != self._recorder.codec:
                # The encoder in the running canvas was chosen before the
                # encoder setting changed; only a rebuild can switch codec.
                self.restart_engine()
                if self.engine is not None:
                    self._status(f"Recording to {self._recording_path}")
                return
            if not self.engine.attach_recorder(self._recorder):
                self.recording = False
                self.record_button.setText("Record")
                self._finish_recorder()
                self._status("Recording could not attach to the running canvas")
                return
            self._status(f"Recording to {self._recording_path}")
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
        if not self.vcam_on:
            if self.engine is not None:
                self.engine.detach_vcam()
            # The node stays open with the idle card so the next application
            # can select it without waiting for the preview.
            return
        if self.engine is None:
            # A loopback writer with no canvas has nothing to push: the sink
            # never starts streaming, so a consumer that opens the device gets
            # EIO and no camera at all.  The button starts the preview it
            # needs instead of leaving an empty device behind.
            self.start_engine()
            if self.engine is not None and self.vcam_on:
                return
            self.vcam_on = False
            self.vcam_button.setText("Virtual camera")
            self._finish_vcam_output()
            return
        # Clicking the button is a request to use the virtual camera, so use
        # the same pkexec setup path as an engine start.  Without setup=True a
        # missing v4l2loopback device only produced a modprobe instruction.
        if not self._start_vcam_output(setup=True):
            self.vcam_on = False
            self.vcam_button.setText("Virtual camera")
            return
        if not self.engine.attach_vcam(self._vcam_output):
            self.vcam_on = False
            self.vcam_button.setText("Virtual camera")
            self._finish_vcam_output()

    def snapshot(self) -> None:
        image = self.preview.image
        if self.preview.gpu_live and self.preview.video_view is not None:
            # The GPU preview has no CPU frame: read back what it shows, at
            # the size it shows it (the image preview saved its preview-size
            # frame too).
            view = self.preview.video_view
            grabbed = view.grabFramebuffer()
            # The framebuffer is in device pixels but may not say so
            # (devicePixelRatio 1.0 on a 1.5x screen): scale by the width.
            ratio = grabbed.width() / max(1, view.width())
            rect = self.preview.canvas_rect()
            image = grabbed.copy(int(rect.x() * ratio), int(rect.y() * ratio),
                                 int(rect.width() * ratio), int(rect.height() * ratio))
        if image is None or image.isNull():
            self._status("No frame to snapshot")
            return
        path = config.recordings_dir(self.project.output.record_dir) / overlay.timestamped_name("anycam-snapshot", ".png")
        image.save(str(path))
        self._status(f"Snapshot saved to {path}")

    def toggle_multiview(self) -> None:
        if getattr(self, "_multiview", None) is not None:
            try:
                self._multiview.close()
            except Exception:
                pass
            self._multiview = None
            return
        if self.engine is None:
            self._status("Start preview first")
            return
        from .multiview import MultiViewDialog
        pool = getattr(self, "_source_pool", None)
        if pool is None and self.engine is not None:
            pool = getattr(self.engine, "pool", None)
        if pool is None:
            self._status("No source pool available")
            return
        dlg = MultiViewDialog(pool, parent=self)
        self._multiview = dlg
        # Forget it however it was closed, or the next click would only
        # "close" a dialog that is already gone instead of opening a new one.
        dlg.finished.connect(lambda _result=0, d=dlg: self._multiview_closed(d))
        dlg.show()

    def _multiview_closed(self, dialog) -> None:
        if getattr(self, "_multiview", None) is dialog:
            self._multiview = None
        dialog.deleteLater()

    def log_event(self, message: str) -> None:
        """Record a session event; the ManyCam window shows these in Diagnostics."""
        print(f"[diag] {message}", file=sys.stderr, flush=True)

    def _log_preview_problem(self, message: str) -> None:
        """Report a dropped preview frame instead of painting black silently."""
        now = time.monotonic()
        if now - getattr(self, "_preview_problem_at", 0.0) < 1.0:
            return
        self._preview_problem_at = now
        print(f"[preview] dropped frame: {message}", file=sys.stderr, flush=True)

    def _on_frame(self, sample) -> None:
        from gi.repository import Gst

        buf = sample.get_buffer()
        caps = sample.get_caps()
        if buf is None or caps is None or caps.get_size() == 0:
            self._log_preview_problem("sample without buffer or caps")
            return
        structure = caps.get_structure(0)
        if not (structure.has_field("width") and structure.has_field("height")):
            self._log_preview_problem(
                f"caps without dimensions: {structure.to_string()}")
            return
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        fmt = (str(structure.get_value("format"))
               if structure.has_field("format") else "")
        if width <= 0 or height <= 0:
            self._log_preview_problem(f"invalid preview size {width}x{height}")
            return
        if fmt not in ("RGBA", "RGB"):
            # The preview branch pins RGBA; anything else would be read with
            # the wrong stride and painted as garbage or black.
            self._log_preview_problem(f"unexpected preview format {fmt!r}")
            return
        stride = width * (4 if fmt == "RGBA" else 3)
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            self._log_preview_problem("buffer could not be mapped")
            return
        try:
            mapped = len(info.data)
            if mapped < stride * height:
                self._log_preview_problem(
                    f"short frame: {mapped} bytes, need {stride * height}")
                return
            raw = bytes(info.data)[: stride * height]
        finally:
            buf.unmap(info)
        if not raw:
            self._log_preview_problem("empty frame")
            return
        self._pending_frame = (width, height, stride, raw, fmt)
        self.frame_ready.emit()

    def _setup_preview_output(self) -> None:
        """Resize following and the GPU preview, for either window's preview."""
        # A resize (window drag, maximise, layout change) sends many sizes;
        # the engine follows the last one after a short pause.
        self._preview_resize_timer = QTimer(self)
        self._preview_resize_timer.setSingleShot(True)
        self._preview_resize_timer.setInterval(120)
        self._preview_resize_timer.timeout.connect(self._apply_preview_size)
        self.preview.display_size_changed.connect(
            lambda *_: self._preview_resize_timer.start())
        self._enable_gpu_preview()

    def _enable_gpu_preview(self) -> None:
        """Use the GPU preview when this system can run it (see gl_preview)."""
        from . import gl_preview

        reason = gl_preview.unavailable_reason()
        if reason:
            if gl_preview.requested() == "gpu" or "did not load" in reason:
                self.log_event(f"preview: GPU preview unavailable ({reason})")
            return
        if self.preview.enable_gpu():
            self.log_event("preview: GPU preview (qml6glsink)")

    def _attach_gpu_preview(self, sink) -> bool:
        from . import gl_preview

        return gl_preview.attach_sink(sink, self.preview.video_view)

    def _apply_preview_size(self) -> None:
        """Have the engine scale the preview to the widget's shown size."""
        if self.engine is None:
            return
        setter = getattr(self.engine, "set_preview_target", None)
        if callable(setter):
            setter(*self.preview.display_size())

    def _update_preview(self) -> None:
        frame = self._pending_frame
        self._pending_frame = None
        if frame is None:
            return
        width, height, stride, raw, fmt = frame
        qformat = (QImage.Format.Format_RGB888 if fmt == "RGB"
                   else QImage.Format.Format_RGBA8888)
        # No .copy(): the QImage reads ``raw`` in place and the widget keeps
        # ``raw`` alive with it.  The copy cost ~0.75 ms of GUI-thread CPU per
        # frame at panel size.  ``raw`` is immutable bytes, and anything that
        # changes the image (snapshot painting, conversions) detaches first.
        image = QImage(raw, width, height, stride, qformat)
        self.preview.set_image(image, owner=raw)

    def _update_source_status(self) -> None:
        """A running canvas is not proof that its inputs are fresh."""
        if self.engine is None:
            return
        report = self.engine.report
        visible = [layer for layer in self.scene.layers if layer.visible]
        stalled = set(report.stalled_sources)
        stale = [layer.name for layer in visible
                 if layer.id in stalled and layer.id not in self._lost_cameras]
        waiting = [layer.name for layer in visible
                   if layer.id in self._lost_cameras
                   or not report.source_health.get(layer.id, {}).get("frames")]
        slow = [layer for layer in visible if layer.kind == "screen"
                and report.source_health.get(layer.id, {}).get("slow")]
        if stale:
            state = ("STALE", tuple(stale))
            message = "No fresh frames: " + ", ".join(stale)
            if any(layer.kind == "screen" and layer.id in stalled for layer in visible):
                message += " · shared window may be hidden or static"
        elif waiting or (not report.source_health and visible):
            state = ("WAITING", tuple(waiting))
            message = "Waiting for source frames" + (": " + ", ".join(waiting) if waiting else "")
        elif report.instant_fps <= 0:
            state = ("STALLED", ())
            message = "Canvas stopped delivering frames"
        elif slow:
            state = ("SLOW", tuple(layer.id for layer in slow))
            rates = [f"{layer.name}: {report.source_health[layer.id]['fps']:.1f} fps"
                     for layer in slow]
            message = "Slow screen updates · " + ", ".join(rates)
            message += " · may be static or limited by compositor frame delivery"
        else:
            state = ("LIVE", ())
            message = "Source frames resumed"
        self.status_chip.setText(state[0])
        self.status_chip.setToolTip(message)
        previous = getattr(self, "_source_status", ())
        if state != previous:
            self._source_status = state
            if state[0] != "LIVE" or (previous and previous[0] != "LIVE"):
                self._status(message)

    def _tick(self) -> None:
        if self.engine is None:
            return
        # pump() also delivers the newest appsink frame to _on_frame, on this
        # (the main) thread.
        self.engine.pump(0)
        if self._pending_frame is not None:
            self._update_preview()
        mv = getattr(self, "_multiview", None)
        if mv is not None:
            try:
                mv.pump()
            except Exception:
                pass
        grid = getattr(self, "grid", None)
        if grid is not None:
            try:
                grid.pump()
            except Exception:
                pass
        rec = self.engine.sample()
        if self.engine.mic_level_db is not None:
            db = self.engine.mic_level_db
            self.mic_level_label.setText("muted" if db < -80 else f"{db:.0f} dB")
        self._advance_playlist(time.monotonic())
        if rec is not None:
            self._update_source_status()
            stats = (f"{rec['instant_fps']:5.1f} fps · {rec['frames']} frames · "
                     f"CPU {rec['cpu_pct']}% · RSS {rec['rss_mib']} MiB")
            source_fps = self._screen_source_fps()
            if source_fps is not None:
                # The canvas rate says nothing about the *captured* frames: a
                # screen source that stopped producing still repeats its last
                # frame at 60 fps.  Show the source rate next to it.
                stats += f" · screen {source_fps:.0f} fps"
            self.stats_label.setText(stats)
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
        rtmp_error = getattr(self.engine.report, "rtmp_error", "")
        if rtmp_error and rtmp_error != getattr(self, "_rtmp_error_shown", ""):
            self._rtmp_error_shown = rtmp_error
            self.live = False
            if hasattr(self, "live_button"):
                self.live_button.setText("Go Live")
            self._status(f"Stream stopped: {rtmp_error}")
        vcam_error = getattr(self.engine.report, "vcam_error", "")
        if vcam_error:
            if vcam_error != self._vcam_error_shown:
                self._vcam_error_shown = vcam_error
                self.log_event(f"virtual camera: {vcam_error}")
                self._status(vcam_error)
            # A sink error is not the user switching the camera off.
            self._recover_vcam()
        elif self._vcam_error_shown:
            self._vcam_error_shown = ""
        if self.vcam_on and not self.engine.report.vcam_active:
            # The output may have failed before this canvas was built, so no
            # vcam_error was ever latched.  Retry until the tap is attached.
            self._recover_vcam()
        source_errors = getattr(self.engine.report, "source_errors", ())
        non_camera = [e for e in source_errors
                      if not self._camera_error_layers(e)]
        if non_camera:
            msg = non_camera[0]
            if msg != getattr(self, "_source_error_shown", ""):
                self._source_error_shown = msg
                self._status(f"Source error: {msg}")
        if self._render_seconds and time.monotonic() - self._started_at >= self._render_seconds:
            self._render_seconds = 0.0
            QTimer.singleShot(0, self._auto_screenshot)

    def _save_screenshot(self) -> bool:
        path = getattr(self, "auto_screenshot_path", "")
        has_frame = self.preview.has_frame()
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


INSTANCE_SERVER = f"anycam-gui-{os.getuid()}"


def _raise_window(window) -> None:
    window.showNormal()
    window.raise_()
    window.activateWindow()


_INSTANCE_LOCKS: dict[str, int] = {}


def _take_instance_lock(name: str) -> bool | None:
    """Take the per-user launch lock.

    ``True``: this process now owns it (held until exit; the kernel drops it
    even on a crash, so it can never be stale).  ``False``: another live
    process owns it.  ``None``: locking is unavailable here.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or str(config.state_dir())
    try:
        fd = os.open(os.path.join(base, f"{name}.lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    except OSError:
        os.close(fd)
        return None
    previous = _INSTANCE_LOCKS.pop(name, None)
    if previous is not None:
        os.close(previous)
    _INSTANCE_LOCKS[name] = fd
    return True


def _ask_instance_to_show(name: str, attempts: int, wait_ms: int) -> bool:
    """Ask the running GUI to raise its window; True once it was told."""
    for _ in range(max(1, attempts)):
        probe = QLocalSocket()
        probe.connectToServer(name)
        if probe.waitForConnected(wait_ms):
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(200)
            probe.disconnectFromServer()
            return True
        time.sleep(0.2)
    return False


def claim_gui_instance(name: str = INSTANCE_SERVER
                       ) -> tuple[bool, QLocalServer | None]:
    """Become the only AnyCam GUI, or forward the launch to the running one.

    Two windows mean two writers racing for the single v4l2loopback node; the
    losing writer leaves consumers (Discord) on a black camera.  A second
    launch therefore raises the existing window and exits instead of opening
    another one.

    Ownership is a file lock, not "nobody answered the socket within 500 ms":
    a first window that is busy (a rebuild can block its UI thread for seconds)
    used to look dead, so the second launch deleted its socket and started a
    competing writer.  A held lock means a live owner, however busy it is.
    """
    lock = _take_instance_lock(name)
    if lock is False:
        if not _ask_instance_to_show(name, attempts=12, wait_ms=500):
            print("[guard] AnyCam is already running but did not answer; "
                  "not starting a second window", file=sys.stderr, flush=True)
        return False, None
    if lock is None and _ask_instance_to_show(name, attempts=1, wait_ms=500):
        return False, None
    # We own the launch (or locking is unavailable and nobody answered): any
    # socket file left behind belongs to a crashed instance.
    QLocalServer.removeServer(name)
    server = QLocalServer()
    if not server.listen(name):
        print(f"[guard] single-instance socket failed: {server.errorString()}",
              file=sys.stderr, flush=True)
        server.deleteLater()
        # The lock already guarantees there is no second writer; only the
        # "raise the existing window" convenience is lost.
        return (lock is True), None
    return True, server


def serve_instance_requests(server: QLocalServer, window) -> None:
    """Raise the window when a second launch connects to this instance."""
    def on_connection() -> None:
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            if connection is not None:
                connection.disconnectFromServer()
                connection.deleteLater()
        _raise_window(window)

    server.newConnection.connect(on_connection)


def run_gui(project: Project | None = None) -> int:
    app = QApplication(sys.argv[:1])
    primary, instance_server = claim_gui_instance()
    if not primary:
        return 0
    window = AnyCamWindow(project)
    if instance_server is not None:
        instance_server.setParent(window)
        window._instance_server = instance_server
        serve_instance_requests(instance_server, window)
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    # Hold the loopback node from launch: apps that remember "AnyCam" must be
    # able to open it before the preview is started.
    if window.hold_vcam_device():
        window.feed_held_vcam()
        window.note_vcam_consumers()
        window.ensure_vcam_visible()
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
