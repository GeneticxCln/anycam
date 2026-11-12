"""Geometry helpers shared by the compositor and the UI.

All layer rectangles are stored normalised to the canvas (0..1), while the
compositor needs integer pixel pads.  Keeping the conversion in one place makes
the preview, the drag handles and the GStreamer pad properties agree.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


# A layer box may leave the canvas (a picture sliding in from the side, a
# camera enlarged past the frame): the compositor clips what falls outside.
BOX_POSITION_RANGE = (-2.0, 3.0)
BOX_SIZE_RANGE = (0.001, 4.0)

FIT_MODES = ("contain", "cover", "stretch")
ROTATIONS = (0, 90, 180, 270)
MAX_ZOOM = 8.0


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
        int(round(clamp(x, *BOX_POSITION_RANGE) * canvas_w)),
        int(round(clamp(y, *BOX_POSITION_RANGE) * canvas_h)),
        max(1, int(round(clamp(w, *BOX_SIZE_RANGE) * canvas_w))),
        max(1, int(round(clamp(h, *BOX_SIZE_RANGE) * canvas_h))),
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


# ------------------------------------------------------------ frame transform
def fit_mode(value) -> str:
    """``value`` as one of :data:`FIT_MODES` (anything unknown is ``contain``)."""
    text = str(value or "").strip().lower()
    return text if text in FIT_MODES else "contain"


def rotation_degrees(value) -> int:
    """``value`` snapped to 0, 90, 180 or 270 clockwise degrees."""
    try:
        degrees = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return (round(degrees / 90) * 90) % 360


def rotated_size(width: int, height: int, rotation: int) -> tuple[int, int]:
    """Frame size after a quarter-turn rotation."""
    if rotation_degrees(rotation) in (90, 270):
        return int(height), int(width)
    return int(width), int(height)


def _probe_transforms() -> dict[tuple[bool, bool, int], str]:
    """``videoflip`` direction for every (flip_h, flip_v, rotation) combination.

    A layer is flipped first and then turned clockwise.  The eight pictures
    the dihedral group can produce are matched against the eight directions
    ``videoflip`` offers (measured against GStreamer in the tests), so the
    table cannot disagree with the maths.
    """
    probe = np.arange(6).reshape(2, 3)
    directions = {
        "identity": probe,
        "90r": np.rot90(probe, -1),
        "180": np.rot90(probe, 2),
        "90l": np.rot90(probe, 1),
        "horiz": probe[:, ::-1],
        "vert": probe[::-1],
        "ul-lr": probe.T,
        "ur-ll": np.rot90(probe, 2).T,
    }
    table: dict[tuple[bool, bool, int], str] = {}
    for flip_h in (False, True):
        for flip_v in (False, True):
            for rotation in ROTATIONS:
                picture = probe
                if flip_h:
                    picture = picture[:, ::-1]
                if flip_v:
                    picture = picture[::-1]
                picture = np.rot90(picture, -(rotation // 90))
                for name, candidate in directions.items():
                    if candidate.shape == picture.shape and (candidate == picture).all():
                        table[(flip_h, flip_v, rotation)] = name
                        break
    return table


_TRANSFORMS = _probe_transforms()


def flip_direction(flip_h: bool = False, flip_v: bool = False, rotation: int = 0) -> str:
    """The ``videoflip`` ``video-direction`` for a flip/rotate combination."""
    return _TRANSFORMS[(bool(flip_h), bool(flip_v), rotation_degrees(rotation))]


def layer_transform(layer) -> tuple[str, bool, bool, int, float, tuple[float, float]]:
    """(fit, flip_h, flip_v, rotation, zoom, pan) of a layer, sanitised."""
    try:
        zoom = float(getattr(layer, "zoom", 1.0))
    except (TypeError, ValueError):
        zoom = 1.0
    if zoom != zoom:      # NaN
        zoom = 1.0
    pan = getattr(layer, "pan", None) or (0.5, 0.5)
    try:
        pan_x, pan_y = clamp(pan[0]), clamp(pan[1])
    except (TypeError, ValueError, IndexError):
        pan_x = pan_y = 0.5
    return (fit_mode(getattr(layer, "fit", "contain")),
            bool(getattr(layer, "flip_h", False)),
            bool(getattr(layer, "flip_v", False)),
            rotation_degrees(getattr(layer, "rotation", 0)),
            clamp(zoom, 1.0, MAX_ZOOM), (pan_x, pan_y))


def has_transform(layer) -> bool:
    """True when a layer needs more than the plain aspect-preserving fit."""
    fit, flip_h, flip_v, rotation, zoom, pan = layer_transform(layer)
    return (fit != "contain" or flip_h or flip_v or rotation != 0
            or zoom > 1.0 + 1e-6)


def framing_margins(src_w: int, src_h: int, box_w: int, box_h: int, fit: str,
                    zoom: float = 1.0, pan: tuple[float, float] = (0.5, 0.5)
                    ) -> tuple[int, int, int, int]:
    """``videocrop`` margins (left, top, right, bottom) for a framed layer.

    ``cover`` keeps the part of the picture that fills the box (the rest is
    cut away); ``zoom`` > 1 magnifies about ``pan`` (0..1 across the picture),
    for any fit.  The window never leaves the picture and keeps the aspect of
    what it must fill, so scaling it afterwards does not distort.
    """
    src_w, src_h = max(1, int(src_w)), max(1, int(src_h))
    window_w, window_h = float(src_w), float(src_h)
    if fit_mode(fit) == "cover":
        aspect = max(1, int(box_w)) / max(1, int(box_h))
        if src_w / src_h > aspect:
            window_w = src_h * aspect
        else:
            window_h = src_w / aspect
    zoom = clamp(zoom, 1.0, MAX_ZOOM)
    window_w, window_h = window_w / zoom, window_h / zoom
    left = clamp(pan[0]) * (src_w - window_w)
    top = clamp(pan[1]) * (src_h - window_h)
    # Even numbers: subsampled YUV sources cannot be cropped on odd pixels.
    left_px = int(round(left / 2.0)) * 2
    top_px = int(round(top / 2.0)) * 2
    width_px = max(2, int(round(window_w / 2.0)) * 2)
    height_px = max(2, int(round(window_h / 2.0)) * 2)
    width_px = min(width_px, src_w - (src_w % 2) or src_w)
    height_px = min(height_px, src_h - (src_h % 2) or src_h)
    left_px = max(0, min(left_px, src_w - width_px))
    top_px = max(0, min(top_px, src_h - height_px))
    return left_px, top_px, src_w - left_px - width_px, src_h - top_px - height_px
