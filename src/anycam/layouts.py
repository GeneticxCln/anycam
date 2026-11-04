"""Multi-camera layout presets, ManyCam-style.

A layout is pure model data: it assigns every visible capture layer a
normalised box on the canvas.  The compositor, the preview and the virtual
camera already understand ``Layer.box``, so applying a layout needs no
pipeline changes and survives a project save/load.

Presets carry explicit slots rather than only a row/column count, which is what
makes "one big camera plus a small inset" expressible: the big slot covers the
frame and the small one sits in a corner on top.  Slots are filled in stacking
order (lowest ``z`` first).  Text and logo overlays are never moved.
"""
from __future__ import annotations

from dataclasses import dataclass

# Layers that can fill a layout slot.  Overlays (text, logo, playlist) keep
# their own position, so a title stays a title.
GRID_KINDS = ("camera", "screen", "video", "network", "test")

# Gutter between adjacent slots, as a fraction of the canvas.
GRID_GAP = 0.012

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class LayoutPreset:
    key: str
    label: str
    boxes: tuple[Box, ...]
    short: str = ""

    @property
    def cells(self) -> int:
        return len(self.boxes)


def _axis(length: float, count: int, gap: float) -> tuple[float, float]:
    """Cell size and usable gap along one axis.

    A gap that cannot fit collapses to zero instead of producing negative
    cells.
    """
    if count > 1 and gap * (count - 1) >= length:
        gap = 0.0
    return max((length - gap * (count - 1)) / count, 0.001), gap


def grid_boxes(columns: int, rows: int, *, gap: float = GRID_GAP,
               margin: float = 0.0) -> list[Box]:
    """Normalised boxes for a ``columns`` × ``rows`` grid, row-major."""
    columns = max(1, int(columns))
    rows = max(1, int(rows))
    gap = max(0.0, float(gap))
    margin = max(0.0, float(margin))
    if margin * 2.0 >= 1.0:
        margin = 0.0
    width, column_gap = _axis(1.0 - 2.0 * margin, columns, gap)
    height, row_gap = _axis(1.0 - 2.0 * margin, rows, gap)
    boxes: list[Box] = []
    for row in range(rows):
        for column in range(columns):
            boxes.append((
                round(margin + column * (width + column_gap), 6),
                round(margin + row * (height + row_gap), 6),
                round(width, 6),
                round(height, 6),
            ))
    return boxes


def _three_up() -> tuple[Box, ...]:
    """One large source on the left, two stacked on the right."""
    width = round((1.0 - GRID_GAP) / 2.0, 6)
    height = round((1.0 - GRID_GAP) / 2.0, 6)
    right = round(width + GRID_GAP, 6)
    return (
        (0.0, 0.0, width, 1.0),
        (right, 0.0, width, height),
        (right, round(height + GRID_GAP, 6), width, height),
    )


LAYOUT_PRESETS = (
    LayoutPreset("full", "1 camera · full screen",
                 ((0.0, 0.0, 1.0, 1.0),), short="1 cam"),
    LayoutPreset("split-2", "2 cameras · side by side",
                 tuple(grid_boxes(2, 1)), short="2 cams"),
    LayoutPreset("three-up", "3 cameras · one large, two small", _three_up(),
                 short="3 cams\nlarge"),
    LayoutPreset("three-across", "3 cameras · side by side",
                 tuple(grid_boxes(3, 1)), short="3 cams"),
    LayoutPreset("grid-2x2", "4 cameras · 2×2 grid",
                 tuple(grid_boxes(2, 2)), short="2×2"),
    LayoutPreset("pip", "Big + small camera (picture in picture)",
                 ((0.0, 0.0, 1.0, 1.0), (0.66, 0.60, 0.32, 0.38)), short="PiP"),
)

PRESETS_BY_KEY = {preset.key: preset for preset in LAYOUT_PRESETS}


def layout_preset(key: str) -> LayoutPreset | None:
    """Look up a preset by key; ``None`` for an unknown key."""
    return PRESETS_BY_KEY.get(str(key or ""))


def capture_layers(scene, *, kinds: tuple[str, ...] = GRID_KINDS):
    """Visible capture layers in stacking order (the layout fill order)."""
    return sorted((layer for layer in scene.layers
                   if layer.visible and layer.kind in kinds),
                  key=lambda layer: layer.z)


def arrange(scene, preset: LayoutPreset, *,
            kinds: tuple[str, ...] = GRID_KINDS):
    """Place the scene's capture layers into ``preset``'s slots.

    Returns the arranged layers.  Layers beyond the preset's cell count keep
    their current box: a 2×2 layout with five cameras arranges four and leaves
    the fifth where the user put it.
    """
    layers = capture_layers(scene, kinds=kinds)
    arranged = layers[:len(preset.boxes)]
    for layer, box in zip(arranged, preset.boxes):
        layer.box = box
    return arranged
