"""The preview widget: the canvas picture, selection, drag handles and the GPU view."""
from __future__ import annotations



from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QSizePolicy, QWidget,
)

from ..geometry import clamp
from ..model import (Layer, Scene)
from .theme import STYLE  # noqa: F401


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
        if layer is None or not layer.visible or layer.locked or event.key() not in arrows:
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
            changed_layers = [item for item in (self.selected_layers() or [layer])
                              if not item.locked]
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
        if layer.locked:
            return          # selecting a locked layer is fine; dragging it is not
        self.edit_started.emit(layer.id)
        group = []
        if mode == "move":
            group = [(item.id, tuple(item.box)) for item in self.selected_layers()
                     if not item.locked]
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
