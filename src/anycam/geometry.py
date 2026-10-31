"""Geometry helpers shared by the compositor and the UI.

All layer rectangles are stored normalised to the canvas (0..1), while the
compositor needs integer pixel pads.  Keeping the conversion in one place makes
the preview, the drag handles and the GStreamer pad properties agree.
"""
from __future__ import annotations

from dataclasses import dataclass


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int


def box_pixels(layer, canvas_w: int, canvas_h: int) -> Rect:
    """Pixel rectangle for a layer's normalised box (the compositor pad)."""
    x, y, w, h = layer.box
    return Rect(
        int(round(clamp(x) * canvas_w)),
        int(round(clamp(y) * canvas_h)),
        max(1, int(round(clamp(w, 0.001, 1.0) * canvas_w))),
        max(1, int(round(clamp(h, 0.001, 1.0) * canvas_h))),
    )


def crop_pixels(src_w: int, src_h: int, crop: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """Convert normalised (left, top, right, bottom) crop to videocrop pixels."""
    left, top, right, bottom = (clamp(v) for v in crop)
    l = min(int(round(src_w * left)), max(0, src_w - 1))
    t = min(int(round(src_h * top)), max(0, src_h - 1))
    r = min(int(round(src_w * right)), max(0, src_w - 1 - l))
    b = min(int(round(src_h * bottom)), max(0, src_h - 1 - t))
    # keep at least one pixel of source
    return l, t, r, b


def fit_contain(src_w: int, src_h: int, box_w: int, box_h: int, box_x: int = 0, box_y: int = 0) -> Rect:
    """Fit a source into a box preserving aspect, centred."""
    src_w = max(1, int(src_w))
    src_h = max(1, int(src_h))
    box_w = max(1, int(box_w))
    box_h = max(1, int(box_h))
    scale = min(box_w / src_w, box_h / src_h)
    w = max(1, int(round(src_w * scale)))
    h = max(1, int(round(src_h * scale)))
    x = int(box_x) + (box_w - w) // 2
    y = int(box_y) + (box_h - h) // 2
    return Rect(x, y, w, h)


def layer_geometry(layer, src_w: int, src_h: int, canvas_w: int, canvas_h: int) -> dict:
    """Pixel pad geometry for a compositor sink pad.

    ``layer.box`` is (x, y, w, h) normalised to the canvas.  The source crop is
    applied *before* the compositor, so the fitted aspect uses the cropped size.
    """
    box = box_pixels(layer, canvas_w, canvas_h)
    box_x, box_y, box_w, box_h = box.x, box.y, box.w, box.h
    l, t, r, b = crop_pixels(src_w, src_h, layer.crop)
    cropped_w = max(1, src_w - l - r)
    cropped_h = max(1, src_h - t - b)
    rect = fit_contain(cropped_w, cropped_h, box_w, box_h, box_x, box_y)
    return {
        "xpos": rect.x,
        "ypos": rect.y,
        "width": rect.w,
        "height": rect.h,
        "crop": (l, t, r, b),
    }
