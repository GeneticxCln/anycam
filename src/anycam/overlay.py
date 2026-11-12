"""Text, badge and logo rendering for the compositor.

AnyCam keeps its overlays as original RGBA PNGs generated at runtime.  This
keeps the GStreamer graph simple (one imagefreeze layer per overlay) and means
the same assets are used by the headless demo, the GUI and the tests.
"""
from __future__ import annotations

import functools
import hashlib
import os
import random
import re
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import config
from .geometry import crop_pixels

# Rendered overlays are cheap to regenerate (every render rewrites its file),
# but a crop drag writes a new box-sized PNG per step.  Keep the cache bounded.
OVERLAY_CACHE_MAX_BYTES = 256 * 1024 * 1024
OVERLAY_CACHE_MAX_FILES = 1500
_PRUNE_INTERVAL_S = 30.0
_last_prune = 0.0


def save_png_atomic(image: Image.Image, out: Path) -> Path:
    """Write ``image`` to ``out`` so a reader never sees a half-written PNG.

    Overlay PNGs are rewritten under the same name while a running pipeline,
    an old engine or the GUI may still open them.  The picture goes to a
    private temporary name first and is renamed over the target (atomic on one
    filesystem).  The temporary has no ``.png`` suffix, so the cache pruner
    leaves it alone.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(f".{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        image.save(temp, "PNG")
        os.replace(temp, out)
    finally:
        temp.unlink(missing_ok=True)
    return out


def prune_overlay_cache(directory: Path | None = None, *,
                        max_bytes: int = OVERLAY_CACHE_MAX_BYTES,
                        max_files: int = OVERLAY_CACHE_MAX_FILES,
                        keep: Path | None = None) -> int:
    """Delete the least recently written overlay PNGs beyond the limits.

    ``keep`` (the file just written) is never removed.  A file a running
    pipeline already loaded stays valid on Linux even once unlinked.  Returns
    the number of files removed.
    """
    directory = directory or (config.cache_dir() / "overlays")
    try:
        entries = [(path, path.stat()) for path in directory.glob("*.png")]
    except OSError:
        return 0
    entries.sort(key=lambda item: item[1].st_mtime)
    total = sum(stat.st_size for _path, stat in entries)
    count = len(entries)
    removed = 0
    for path, stat in entries:
        if total <= max_bytes and count <= max_files:
            break
        if keep is not None and path == keep:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= stat.st_size
        count -= 1
        removed += 1
    return removed


def _maybe_prune(keep: Path) -> None:
    """Prune at most every ``_PRUNE_INTERVAL_S`` seconds, off the hot path."""
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < _PRUNE_INTERVAL_S:
        return
    _last_prune = now
    prune_overlay_cache(keep=keep)


_FONT_CANDIDATES = (
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
)


def font_path() -> str:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return path
    return ""


def parse_color(value: str, default: tuple[int, int, int, int] = (255, 255, 255, 255)) -> tuple[int, int, int, int]:
    value = (value or "").strip().lstrip("#")
    try:
        if len(value) == 6:
            return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), 255)
        if len(value) == 8:
            return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), int(value[6:8], 16))
    except ValueError:
        pass
    return default


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    path = font_path()
    if path:
        return ImageFont.truetype(path, max(8, int(size)))
    return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int], start: int) -> ImageFont.FreeTypeFont:
    size = max(8, int(start))
    while size > 8:
        font = _load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= box[0] and bbox[3] - bbox[1] <= box[1]:
            return font
        size = int(size * 0.9)
    return _load_font(8)


def render_text_image(
    text: str,
    width: int,
    height: int,
    font_size: int,
    color: str = "#ffffff",
    background: str = "#101418cc",
    style: str = "badge",
) -> Image.Image:
    """Text on a badge (or bare) with a transparent surround, as an RGBA picture."""
    width, height = max(8, int(width)), max(8, int(height))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if style == "badge":
        bg = parse_color(background, (16, 20, 24, 204))
        radius = max(4, int(min(width, height) * 0.18))
        draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=bg)
    padding_x = max(4, int(width * 0.045))
    padding_y = max(2, int(height * 0.12))
    font = _fit_font(draw, text, (width - 2 * padding_x, height - 2 * padding_y), font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    tx = (width - (bbox[2] - bbox[0])) // 2 - bbox[0]
    ty = (height - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=parse_color(color))
    return image


def render_text_png(
    text: str,
    width: int,
    height: int,
    font_size: int,
    color: str = "#ffffff",
    background: str = "#101418cc",
    style: str = "badge",
    out: Path | None = None,
) -> Path:
    """Render a text badge with a transparent surround."""
    width, height = max(8, int(width)), max(8, int(height))
    image = render_text_image(text, width, height, font_size, color, background, style)
    if out is None:
        digest = hashlib.sha1(
            f"{text}|{width}x{height}|{font_size}|{color}|{background}|{style}".encode()
        ).hexdigest()[:16]
        out = config.cache_dir() / "overlays" / f"text-{digest}.png"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_png_atomic(image, out)
    return out


# Border styles draw only a line or bezel over the picture.  Shaped windows
# (circle ... star) also cover everything outside the shape with the matte
# colour, so the picture really appears cut to that shape.
WINDOW_FRAME_STYLES: dict[str, str] = {
    "none": "None",
    "line": "Line",
    "rounded": "Rounded",
    "double": "Double line",
    "corners": "Corner brackets",
    "neon": "Neon glow",
    "vignette": "Vignette",
    "polaroid": "Polaroid",
    "tv": "TV screen",
    "film": "Film strip",
    "circle": "Circle window",
    "oval": "Oval window",
    "window": "Rounded window",
    "hexagon": "Hexagon window",
    "diamond": "Diamond window",
    "heart": "Heart window",
    "star": "Star window",
}

def _merge_themes() -> None:
    from . import frames
    for key, (label, _group, _painter) in frames.THEMES.items():
        WINDOW_FRAME_STYLES.setdefault(key, label)


_merge_themes()

# Styles that cut the picture to a shape.
SHAPED_FRAME_STYLES = ("circle", "oval", "window", "hexagon", "diamond", "heart", "star")
# These keep their proportions; the others stretch to the box.
_UNIFORM_SHAPES = ("circle", "hexagon", "heart", "star")
_SUPERSAMPLE = 3


def _shape_points(style: str, width: int, height: int,
                  margin: float) -> list[tuple[float, float]]:
    """Outline of a shaped window inscribed in the box, in pixels."""
    import math

    left, top = margin, margin
    span_w, span_h = max(2.0, width - 2 * margin), max(2.0, height - 2 * margin)

    def fit(points, uniform=True):
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
        scale_x = span_w / (max_x - min_x)
        scale_y = span_h / (max_y - min_y)
        if uniform:
            scale_x = scale_y = min(scale_x, scale_y)
        offset_x = left + (span_w - (max_x - min_x) * scale_x) / 2.0
        offset_y = top + (span_h - (max_y - min_y) * scale_y) / 2.0
        return [(offset_x + (x - min_x) * scale_x,
                 offset_y + (y - min_y) * scale_y) for x, y in points]

    if style == "hexagon":
        return fit([(math.cos(math.pi / 6 + i * math.pi / 3),
                     math.sin(math.pi / 6 + i * math.pi / 3)) for i in range(6)])
    if style == "diamond":
        return fit([(0.5, 0.0), (1.0, 0.5), (0.5, 1.0), (0.0, 0.5)], uniform=False)
    if style == "star":
        points = []
        for index in range(10):
            radius = 1.0 if index % 2 == 0 else 0.42
            angle = -math.pi / 2 + index * math.pi / 5
            points.append((math.cos(angle) * radius, math.sin(angle) * radius))
        return fit(points)
    if style == "heart":
        points = []
        for step in range(120):
            t = step / 120 * 2 * math.pi
            points.append((16 * math.sin(t) ** 3,
                           -(13 * math.cos(t) - 5 * math.cos(2 * t)
                             - 2 * math.cos(3 * t) - math.cos(4 * t))))
        return fit(points)
    return []


def _shaped_window(width: int, height: int, style: str, border: tuple,
                   matte: tuple, thickness: int) -> Image.Image:
    """Matte outside a shape, with a border ring along its edge."""
    scale = _SUPERSAMPLE
    big_w, big_h = width * scale, height * scale
    margin = max(2, thickness) * scale
    inside = Image.new("L", (big_w, big_h), 0)
    ring = Image.new("L", (big_w, big_h), 0)
    draw_in, draw_ring = ImageDraw.Draw(inside), ImageDraw.Draw(ring)
    box = [margin, margin, big_w - margin - 1, big_h - margin - 1]
    stroke = max(1, thickness) * scale
    if style == "circle":
        side = min(big_w, big_h) - 2 * margin
        left, top = (big_w - side) // 2, (big_h - side) // 2
        circle = [left, top, left + side, top + side]
        draw_in.ellipse(circle, fill=255)
        draw_ring.ellipse(circle, outline=255, width=stroke)
    elif style == "oval":
        draw_in.ellipse(box, fill=255)
        draw_ring.ellipse(box, outline=255, width=stroke)
    elif style == "window":
        radius = int(min(big_w, big_h) * 0.16)
        draw_in.rounded_rectangle(box, radius=radius, fill=255)
        draw_ring.rounded_rectangle(box, radius=radius, outline=255, width=stroke)
    else:
        points = _shape_points(style, big_w, big_h, margin)
        draw_in.polygon(points, fill=255)
        draw_ring.line(points + [points[0]], fill=255, width=stroke, joint="curve")
    inside = inside.resize((width, height), Image.LANCZOS)
    ring = ring.resize((width, height), Image.LANCZOS)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    outside = inside.point(lambda value: 255 - value)
    image.paste(Image.new("RGBA", (width, height), matte), (0, 0), outside)
    image.paste(Image.new("RGBA", (width, height), border), (0, 0), ring)
    return image


def _vignette(width: int, height: int, matte: tuple) -> Image.Image:
    import numpy as np

    ys, xs = np.mgrid[0:height, 0:width]
    dx = (xs - width / 2.0) / (width / 2.0)
    dy = (ys - height / 2.0) / (height / 2.0)
    distance = np.sqrt(dx * dx + dy * dy) / 1.4142
    alpha = np.clip((distance - 0.45) / 0.55, 0.0, 1.0) ** 1.6 * 215
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[..., 0], pixels[..., 1], pixels[..., 2] = matte[0], matte[1], matte[2]
    pixels[..., 3] = alpha.astype(np.uint8)
    return Image.fromarray(pixels, "RGBA")


def _neon(width: int, height: int, rgba: tuple, thickness: int) -> Image.Image:
    from PIL import ImageFilter

    margin = max(3, thickness * 2)
    line = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(line).rounded_rectangle(
        [margin, margin, width - margin - 1, height - margin - 1],
        radius=max(3, int(min(width, height) * 0.06)), outline=rgba,
        width=max(1, thickness))
    glow = line.filter(ImageFilter.GaussianBlur(max(2.0, thickness * 1.8)))
    # Brighter core inside the same outline, so it reads as a lit tube.
    core = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    light = tuple(min(255, int(c + (255 - c) * 0.6)) for c in rgba[:3]) + (255,)
    ImageDraw.Draw(core).rounded_rectangle(
        [margin, margin, width - margin - 1, height - margin - 1],
        radius=max(3, int(min(width, height) * 0.06)), outline=light,
        width=max(1, thickness // 3))
    image = Image.alpha_composite(glow, glow)
    image = Image.alpha_composite(image, line)
    return Image.alpha_composite(image, core)


def _themes():
    from . import frames
    return frames


def render_window_frame(
    width: int,
    height: int,
    style: str = "rounded",
    color: str = "#5eead0",
    thickness: int = 6,
    out: Path | None = None,
    matte: str = "#0b0d10",
) -> Path:
    """Render a transparent overlay that frames one layer's picture.

    Border styles draw a line or bezel.  Shaped windows (``SHAPED_FRAME_STYLES``)
    cover everything outside the shape with ``matte`` and draw the border
    colour along the edge.  ``film`` and ``polaroid`` are matted borders too.
    """
    width, height = max(8, int(width)), max(8, int(height))
    style = style if style in WINDOW_FRAME_STYLES else "rounded"
    # The border must fit the box: a thick border on a small tile made the
    # inner rectangles invert and Pillow raised "y1 must be >= y0", which
    # aborted the whole canvas build (and every launch, as it is saved).
    thickness = max(1, min(48, int(thickness), min(width, height) // 12))
    if out is None:
        digest = hashlib.sha1(
            f"frame4|{_frames_revision()}|{style}|{color}|{matte}|{thickness}|{width}x{height}".encode()
        ).hexdigest()[:16]
        cached = config.cache_dir() / "overlays" / f"frame-{digest}.png"
        # The name is the whole recipe, so an existing file is the answer.
        # Re-rendering it on every effect-slider tick cost ~40 ms each.
        if cached.is_file() and cached.stat().st_size > 0:
            return cached
        out = cached
    rgba = parse_color(color, (94, 234, 208, 255))
    matte_rgba = parse_color(matte, (11, 13, 16, 255))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if style == "none" or min(width, height) < 32:
        # Nothing to draw, or a tile too small to show a frame at all.
        pass
    elif style in _themes().THEMES:
        try:
            image = _themes().render(style, width, height, thickness, color)
        except ValueError as exc:
            # A themed painter that cannot fit this box must not take the
            # layer down with it: fall back to a plain border.
            print(f"[frame] {style} at {width}x{height}: {exc}; using a plain border",
                  file=sys.stderr, flush=True)
            margin = max(1, thickness // 2 + 1)
            draw.rectangle([margin, margin, width - margin - 1, height - margin - 1],
                           outline=rgba, width=thickness)
    elif style in SHAPED_FRAME_STYLES:
        image = _shaped_window(width, height, style, rgba, matte_rgba, thickness)
    elif style == "vignette":
        image = _vignette(width, height, matte_rgba)
    elif style == "neon":
        image = _neon(width, height, rgba, thickness)
    elif style == "polaroid":
        side = max(thickness, int(min(width, height) * 0.05))
        bottom = max(side * 3, int(height * 0.17))
        draw.rectangle([0, 0, width - 1, height - 1], fill=rgba)
        draw.rectangle([side, side, width - side - 1, height - bottom - 1],
                       fill=(0, 0, 0, 0))
        draw.rectangle([side - 1, side - 1, width - side, height - bottom],
                       outline=(0, 0, 0, 60))
    elif style == "tv":
        bezel = max(thickness * 2, int(min(width, height) * 0.07))
        radius = max(6, int(min(width, height) * 0.09))
        draw.rectangle([0, 0, width - 1, height - 1], fill=matte_rgba)
        draw.rounded_rectangle([bezel, bezel, width - bezel - 1, height - bezel - 1],
                               radius=radius, fill=(0, 0, 0, 0))
        draw.rounded_rectangle([bezel - 2, bezel - 2, width - bezel + 1, height - bezel + 1],
                               radius=radius + 2, outline=rgba, width=max(1, thickness // 3))
        led = max(2, bezel // 5)
        draw.ellipse([width - bezel - led * 3, height - bezel // 2 - led // 2,
                      width - bezel - led * 2, height - bezel // 2 + led // 2 + 1], fill=rgba)
    elif style == "film":
        draw.rectangle([0, 0, width - 1, height - 1], fill=rgba)
        inner = max(thickness, int(min(width, height) * 0.09))
        draw.rectangle([inner, inner, width - inner - 1, height - inner - 1],
                       fill=(0, 0, 0, 0))
        hole = max(2, thickness)
        step = max(hole * 3, 2)
        for x in range(inner + hole, width - inner - hole, step):
            draw.rectangle([x, max(1, inner // 2 - hole // 2),
                            x + hole, max(1, inner // 2 + hole // 2)],
                           fill=(16, 20, 24, 255))
            draw.rectangle([x, height - inner // 2 - hole // 2,
                            x + hole, height - inner // 2 + hole // 2],
                           fill=(16, 20, 24, 255))
    else:
        margin = max(1, thickness // 2 + 1)
        outer = [margin, margin, width - margin - 1, height - margin - 1]
        if style == "rounded":
            radius = max(3, int(min(width, height) * 0.08))
            draw.rounded_rectangle(outer, radius=radius, outline=rgba, width=thickness)
        elif style == "line":
            draw.rectangle(outer, outline=rgba, width=thickness)
        elif style == "double":
            draw.rectangle(outer, outline=rgba, width=max(1, thickness // 2))
            inset = margin + max(2, thickness * 2)
            draw.rectangle([inset, inset, width - inset - 1, height - inset - 1],
                           outline=rgba, width=max(1, thickness // 2))
        elif style == "corners":
            tick = max(12, min(width, height) // 6)
            for x, y, dx, dy in (
                (margin, margin, 1, 1),
                (width - margin - 1, margin, -1, 1),
                (margin, height - margin - 1, 1, -1),
                (width - margin - 1, height - margin - 1, -1, -1),
            ):
                draw.line([x, y, x + dx * tick, y], fill=rgba, width=thickness)
                draw.line([x, y, x, y + dy * tick], fill=rgba, width=thickness)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_png_atomic(image, out)
    return out


@functools.lru_cache(maxsize=1)
def _frames_revision() -> str:
    """A short hash of ``frames.py``: any frame painter change renders anew."""
    from . import frames

    try:
        return hashlib.sha1(Path(frames.__file__).read_bytes()).hexdigest()[:8]
    except OSError:
        return "0"


def _stages_revision() -> str:
    """A short hash of ``stages.py``: any painter change renders anew."""
    from . import stages

    try:
        return hashlib.sha1(Path(stages.__file__).read_bytes()).hexdigest()[:8]
    except OSError:
        return "0"


# Bump when a palette or painter below changes: it is part of the cache key.
# (The key used to hash the function's bytecode, which leaves out the
# palettes - they are constants - so an edited palette kept its old picture.)
BACKGROUND_REV = 2

BACKGROUND_NAMES = (
    "Aurora", "Midnight", "Sunset", "Studio", "Ocean",
    "Neon Grid", "Bokeh", "Carbon", "Paper", "Vignette",
    # Textures, light and structure (anycam.stages).
    "Film Grain", "Focal Glow", "Frosted Glass", "Blueprint", "Topography",
    "Split Tone", "Concrete", "Fluted Glass", "Terrazzo",
)


def render_background(name: str, width: int, height: int,
                      out: Path | None = None) -> Path:
    """Render a fast, cached static backdrop at the project canvas size."""
    width, height = max(64, int(width)), max(64, int(height))
    name = name if name in BACKGROUND_NAMES else "Studio"
    from . import stages

    if name in stages.STATIC:
        if out is None:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            # The painters' code is part of the key: a cache keyed by name and
            # size kept serving the old picture after a painter changed.
            out = (config.cache_dir() / "backgrounds"
                   / f"{slug}-{width}x{height}-{_stages_revision()}.png")
        out = Path(out)
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            picture = stages.STATIC[name](width, height)
            save_png_atomic(picture.convert("RGB"), out)
        return out
    palettes = {
        "Aurora": ((8, 20, 35), (24, 82, 92), (94, 234, 208)),
        "Midnight": ((3, 7, 18), (13, 27, 50), (79, 111, 178)),
        "Sunset": ((30, 8, 30), (174, 68, 58), (255, 179, 94)),
        "Studio": ((35, 40, 44), (61, 69, 74), (190, 200, 203)),
        "Ocean": ((2, 18, 32), (6, 74, 102), (64, 196, 204)),
        "Neon Grid": ((8, 2, 20), (45, 8, 76), (236, 63, 180)),
        "Bokeh": ((18, 12, 28), (63, 30, 70), (255, 190, 120)),
        "Carbon": ((16, 18, 20), (33, 37, 41), (92, 101, 108)),
        "Paper": ((214, 198, 166), (238, 226, 199), (122, 100, 72)),
        "Vignette": ((7, 9, 12), (27, 32, 37), (120, 135, 145)),
    }
    top, bottom, accent = palettes[name]
    image = Image.new("RGBA", (width, height), (*top, 255))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        colour = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=(*colour, 255))
    rng = random.Random(name)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow = ImageDraw.Draw(layer)
    for _ in range(7):
        radius = rng.randint(max(20, width // 14), max(30, width // 5))
        cx = rng.randint(-radius, width + radius)
        cy = rng.randint(-radius, height + radius)
        alpha = rng.randint(24, 74)
        glow.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                     fill=(*accent, alpha))
    image = Image.alpha_composite(image, layer)
    draw = ImageDraw.Draw(image)
    if name == "Neon Grid":
        grid = max(20, width // 28)
        for x in range(-height, width + height, grid):
            draw.line([(x, 0), (x - height, height)], fill=(*accent, 45), width=1)
        for y in range(0, height, grid):
            draw.line([(0, y), (width, y)], fill=(*accent, 35), width=1)
    elif name == "Carbon":
        for offset in range(-height, width, 28):
            draw.line([(offset, 0), (offset + height, height)],
                      fill=(*accent, 28), width=2)
    elif name == "Paper":
        for _ in range(max(400, width * height // 5000)):
            x, y = rng.randrange(width), rng.randrange(height)
            draw.point((x, y), fill=(*accent, rng.randint(12, 34)))
    elif name == "Vignette":
        edge = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ed = ImageDraw.Draw(edge)
        ed.rectangle([0, 0, width - 1, height - 1], outline=(0, 0, 0, 220),
                     width=max(12, min(width, height) // 12))
        image = Image.alpha_composite(image, edge)
    if out is None:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        revision = f"{BACKGROUND_REV}-{_stages_revision()}"
        out = config.cache_dir() / "backgrounds" / f"{slug}-{width}x{height}-{revision}.png"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_png_atomic(image.convert("RGB"), out)
    return out


def render_face_frame(
    color: str = "#5eead0",
    style: str = "frame",
    size: int = 320,
    out: Path | None = None,
) -> Path:
    """Render the transparent frame that follows a detected face.

    The compositor scales this RGBA image into the face's bounding box, so a
    single small asset works for every camera resolution.  ``spotlight`` adds a
    soft translucent surround; ``frame`` is the plain corner-bracket style.
    """
    size = max(48, int(size))
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rgba = parse_color(color, (94, 234, 208, 255))
    if style == "spotlight":
        draw.rectangle([0, 0, size - 1, size - 1], fill=(0, 0, 0, 70))
        draw.rounded_rectangle(
            [size * 0.08, size * 0.08, size * 0.92, size * 0.92],
            radius=size * 0.12, outline=(*rgba[:3], 150), width=max(2, size // 80),
        )
    margin = max(6, size // 24)
    width = max(3, size // 48)
    draw.rounded_rectangle(
        [margin, margin, size - margin - 1, size - margin - 1],
        radius=size // 10, outline=rgba, width=width,
    )
    # Deliberately small corner ticks make the tracking frame legible even when
    # the face box is only a few percent of the canvas.
    tick = size // 7
    for x, y, dx, dy in (
        (margin, margin, 1, 1),
        (size - margin - 1, margin, -1, 1),
        (margin, size - margin - 1, 1, -1),
        (size - margin - 1, size - margin - 1, -1, -1),
    ):
        draw.line([x, y, x + dx * tick, y], fill=rgba, width=width)
        draw.line([x, y, x, y + dy * tick], fill=rgba, width=width)
    if out is None:
        digest = hashlib.sha1(f"{style}|{color}|{size}".encode()).hexdigest()[:16]
        out = config.cache_dir() / "overlays" / f"face-{digest}.png"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_png_atomic(image, out)
    return out


def render_camera_placeholder(
    name: str,
    device: str,
    width: int,
    height: int,
    out: Path | None = None,
    title: str = "Camera disconnected",
) -> Path:
    """Render the opaque card shown while a camera layer is disconnected.

    The compositor scales the PNG into the camera's normal layer box, so the
    slot keeps its position instead of collapsing or showing black.  The card
    is deliberately generated here (rather than being a bundled asset) so the
    layer name and device path can be included in the label.
    """
    width, height = max(8, int(width)), max(8, int(height))
    image = Image.new("RGBA", (width, height), (16, 20, 24, 255))
    draw = ImageDraw.Draw(image)
    border = max(1, min(width, height) // 140)
    draw.rectangle([0, 0, width - 1, height - 1],
                   outline=(65, 82, 92, 255), width=border)
    # A faint diagonal hatch reads as "no signal" without hiding the text.
    step = max(18, min(width, height) // 5)
    for offset in range(-height, width, step * 2):
        draw.line([(offset, 0), (offset + height, height)],
                  fill=(24, 30, 35, 255), width=1)

    icon_w = max(24, min(int(width * 0.22), int(height * 0.30)))
    icon_h = max(16, int(icon_w * 0.66))
    cx, cy = width // 2, int(height * 0.38)
    body = [cx - icon_w // 2, cy - icon_h // 2,
            cx + icon_w // 2, cy + icon_h // 2]
    stroke = max(1, icon_h // 18)
    draw.rounded_rectangle(body, radius=max(2, icon_h // 6),
                           fill=(26, 33, 39, 255), outline=(94, 234, 208, 255),
                           width=stroke)
    lens = max(3, icon_h // 4)
    draw.ellipse([cx - lens, cy - lens, cx + lens, cy + lens],
                 outline=(94, 234, 208, 255), width=stroke)
    bump_w, bump_h = max(6, icon_w // 4), max(3, icon_h // 5)
    draw.rectangle([cx - bump_w // 2, body[1] - bump_h,
                    cx + bump_w // 2, body[1] + 1], fill=(26, 33, 39, 255))

    detail = (name or "Camera").strip()
    if device:
        detail = f"{detail} · {device}" if detail else device
    max_chars = max(12, int(width / max(6.0, height * 0.055)))
    if len(detail) > max_chars:
        detail = detail[: max_chars - 1].rstrip() + "…"

    def centered(text: str, y: int, font, fill: tuple[int, int, int, int]) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        tx = (width - (bbox[2] - bbox[0])) // 2 - bbox[0]
        ty = y - (bbox[3] - bbox[1]) // 2 - bbox[1]
        draw.text((tx, ty), text, font=font, fill=fill)

    title_font = _fit_font(draw, title, (int(width * 0.84), int(height * 0.16)),
                           max(9, int(height * 0.075)))
    centered(title, int(height * 0.60), title_font, (215, 227, 234, 255))
    detail_font = _fit_font(draw, detail, (int(width * 0.84), int(height * 0.11)),
                            max(8, int(height * 0.045)))
    centered(detail, int(height * 0.75), detail_font, (148, 163, 174, 255))

    if out is None:
        digest = hashlib.sha1(
            f"camera|{title}|{name}|{device}|{width}x{height}".encode()
        ).hexdigest()[:16]
        out = config.cache_dir() / "overlays" / f"camera-placeholder-{digest}.png"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_png_atomic(image, out)
    return out


def render_media_error_card(
    name: str,
    source: str,
    width: int,
    height: int,
    out: Path | None = None,
) -> Path:
    """Opaque card for an image/video/logo/face asset that is missing."""
    return render_camera_placeholder(
        name, source, width, height, out=out, title="Media unavailable"
    )


def _fingerprint(source: Path) -> str:
    """Content identity for the overlay cache.

    Using only the file stem and box size made two different photos called
    ``photo.png`` render the same cached picture.
    """
    try:
        stat = source.stat()
        seed = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        seed = str(source)
    return hashlib.sha1(seed.encode()).hexdigest()[:16]


def fit_rgba(source: Path, width: int, height: int, out: Path | None = None) -> Path:
    """Fit an image/logo into a layer box, preserving aspect, transparent fill."""
    width, height = max(8, int(width)), max(8, int(height))
    with Image.open(source) as opened:
        image = opened.convert("RGBA")
    scale = min(width / image.width, height / image.height)
    resized = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2), resized)
    if out is None:
        out = (config.cache_dir() / "overlays"
               / f"img-{source.stem}-{_fingerprint(source)}-{width}x{height}.png")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_png_atomic(canvas, out)
    _maybe_prune(out)
    return out


def apply_transform(image: Image.Image, width: int, height: int, transform) -> Image.Image:
    """``image`` flipped, turned, framed and fitted into ``width`` x ``height``.

    ``transform`` is :func:`anycam.geometry.layer_transform`'s tuple.  This is
    the still-picture twin of the video chain's flip/crop/scale stages and uses
    the same geometry, so a photo and a camera agree.
    """
    from . import geometry

    fit, flip_h, flip_v, rotation, zoom, pan = transform
    if flip_h:
        image = ImageOps.mirror(image)
    if flip_v:
        image = ImageOps.flip(image)
    if rotation:
        image = image.transpose({90: Image.Transpose.ROTATE_270,
                                 180: Image.Transpose.ROTATE_180,
                                 270: Image.Transpose.ROTATE_90}[rotation])
    left, top, right, bottom = geometry.framing_margins(
        image.width, image.height, width, height, fit, zoom, pan)
    image = image.crop((left, top, image.width - right, image.height - bottom))
    if fit == "contain":
        scale = min(width / image.width, height / image.height)
        size = (max(1, int(round(image.width * scale))),
                max(1, int(round(image.height * scale))))
    else:
        size = (width, height)
    resized = image.resize(size, Image.LANCZOS)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.paste(resized, ((width - size[0]) // 2, (height - size[1]) // 2), resized)
    return canvas


def prepare_rgba(source: Path, width: int, height: int,
                 crop: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
                 out: Path | None = None, transform=None) -> Path:
    """EXIF-correct, crop and fit a photo/logo into a layer box.

    Cropping before fitting means the normalised crop is a fraction of the
    image the user picked, not of the (smaller) layer box.
    """
    width, height = max(8, int(width)), max(8, int(height))
    with Image.open(source) as opened:
        # Phone photos carry their rotation in EXIF; without this a portrait
        # shot lands sideways in the composition.
        image = ImageOps.exif_transpose(opened).convert("RGBA")
    left, top, right, bottom = crop_pixels(image.width, image.height, crop)
    image = image.crop((left, top,
                        max(left + 1, image.width - right),
                        max(top + 1, image.height - bottom)))
    if transform is not None:
        canvas = apply_transform(image, width, height, transform)
    else:
        scale = min(width / image.width, height / image.height)
        resized = image.resize((max(1, int(image.width * scale)),
                               max(1, int(image.height * scale))), Image.LANCZOS)
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        canvas.paste(resized, ((width - resized.width) // 2,
                               (height - resized.height) // 2), resized)
    if out is None:
        crop_key = "-".join(f"{value:.4f}" for value in crop)
        if transform is not None:
            crop_key += "-" + "-".join(str(part) for part in transform)
        out = (config.cache_dir() / "overlays"
               / f"img-{source.stem}-{_fingerprint(source)}-{width}x{height}-{crop_key}.png")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_png_atomic(canvas, out)
    _maybe_prune(out)
    return out


def image_size(layer) -> tuple[int, int] | None:
    """Natural, EXIF-corrected pixel size of an image/logo layer's source.

    The crop editor needs this to draw its window over the visible picture
    rather than over the layer box (which may include letterbox bands).
    """
    if getattr(layer, "kind", "") not in ("logo", "image"):
        return None
    source = Path(getattr(layer, "source", "") or "").expanduser()
    if not source.exists():
        return None
    try:
        with Image.open(source) as opened:
            width, height = ImageOps.exif_transpose(opened).size
        return (int(width), int(height))
    except (OSError, ValueError):
        return None


def layer_pixels(layer, canvas_w: int, canvas_h: int) -> tuple[int, int, int, int]:
    from .geometry import clamp

    x, y, w, h = layer.box
    return (
        int(round(clamp(x) * canvas_w)),
        int(round(clamp(y) * canvas_h)),
        max(8, int(round(clamp(w, 0.01, 1.0) * canvas_w))),
        max(8, int(round(clamp(h, 0.01, 1.0) * canvas_h))),
    )


def render_layer(layer, canvas_w: int, canvas_h: int,
                 crop: tuple[float, float, float, float] | None = None) -> Path:
    """Return an RGBA PNG for a text or logo layer.

    ``crop`` overrides the layer's own insets; the crop editor passes zeros so
    the preview can show the uncropped picture while the model keeps the crop.
    """
    x, y, w, h = layer_pixels(layer, canvas_w, canvas_h)
    if layer.kind == "text":
        return render_text_png(
            layer.source or layer.name,
            w, h,
            font_size=max(8, int(layer.font_size * canvas_h)),
            color=layer.color,
            background=layer.background,
            style=layer.style,
        )
    if layer.kind in ("logo", "image"):
        source = Path(layer.source).expanduser()
        if not source.exists():
            raise FileNotFoundError(f"overlay asset not found: {source}")
        source_crop = getattr(layer, "crop", (0.0, 0.0, 0.0, 0.0)) if crop is None else crop
        if getattr(getattr(layer, "effects", None), "mirror", False):
            # The picture is cropped here but mirrored later in the chain, so
            # the crop the user drew on the mirrored preview is the opposite
            # edge of the source (every other layer kind mirrors first).
            left, top, right, bottom = source_crop
            source_crop = (right, top, left, bottom)
        from . import geometry
        transform = geometry.layer_transform(layer) if geometry.has_transform(layer) else None
        return prepare_rgba(source, w, h, source_crop, transform=transform)
    raise ValueError(f"layer {layer.kind} is not an overlay")


def render_scene(scene, canvas_w: int, canvas_h: int) -> dict[str, Path]:
    """Render every static overlay layer of a scene, keyed by layer id."""
    return {
        layer.id: render_layer(layer, canvas_w, canvas_h)
        for layer in scene.ordered()
        if layer.kind in ("text", "logo", "image")
    }


def timestamped_name(prefix: str = "anycam", suffix: str = ".png") -> str:
    from datetime import datetime

    # Milliseconds avoid collisions when a graph rebuild starts a new segment
    # in the same second as the previous one.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return f"{prefix}-{stamp}{suffix}"
