"""More static stages: textures, light and structure, rendered once and cached.

Each painter returns an RGB picture at the canvas size.  ``overlay.render_background``
caches the result as a PNG, so the cost (well under a second at 1080p) is paid
once per stage and size.
"""

from __future__ import annotations

import math
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def _grid(width: int, height: int):
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    return xs, ys


def _image(rgb: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(rgb * 255.0, 0, 255).astype(np.uint8), "RGB")


def _mix(a, b, t):
    a, b = np.asarray(a, np.float32), np.asarray(b, np.float32)
    return a + (b - a) * np.asarray(t, np.float32)[..., None]


def _noise(width: int, height: int, scale: int, seed: int) -> np.ndarray:
    """Smooth value noise in 0..1: a small random grid enlarged bilinearly."""
    rng = np.random.default_rng(seed)
    small = (rng.random((max(2, height // scale + 2), max(2, width // scale + 2))) * 255).astype(np.uint8)
    big = Image.fromarray(small, "L").resize((width, height), Image.BICUBIC)
    return np.asarray(big, np.float32) / 255.0


def _fractal(width: int, height: int, seed: int, octaves=(160, 60, 22, 8, 3)) -> np.ndarray:
    total = np.zeros((height, width), np.float32)
    weight = 0.0
    for index, scale in enumerate(octaves):
        amplitude = 0.55 ** index
        total += _noise(width, height, scale, seed + index) * amplitude
        weight += amplitude
    return total / weight


def _vignette(width: int, height: int, strength: float) -> np.ndarray:
    xs, ys = _grid(width, height)
    dx, dy = (xs / width - 0.5) * 2.0, (ys / height - 0.5) * 2.0
    return 1.0 - strength * np.clip((dx * dx + dy * dy) / 2.0, 0.0, 1.0) ** 1.2


def film_grain(width: int, height: int) -> Image.Image:
    """A deep plum colour with film grain and darkened edges."""
    xs, ys = _grid(width, height)
    diagonal = (xs / width * 0.6 + ys / height * 0.4)
    rgb = _mix((0.32, 0.12, 0.30), (0.12, 0.05, 0.16), diagonal)
    grain = np.random.default_rng(31).normal(0.0, 0.045, (height, width)).astype(np.float32)
    rgb = rgb + grain[..., None]
    return _image(rgb * _vignette(width, height, 0.75)[..., None])


def focal_glow(width: int, height: int) -> Image.Image:
    """A blurred, off-centre burst of colour, like a light behind the camera."""
    xs, ys = _grid(width, height)
    x, y = xs / height, ys / height
    aspect = width / height
    rgb = np.ones((height, width, 3), np.float32) * np.asarray((0.03, 0.04, 0.10), np.float32)
    for cx, cy, size, colour in ((aspect * 0.70, 0.38, 0.16, (1.00, 0.45, 0.25)),
                                 (aspect * 0.78, 0.30, 0.05, (1.00, 0.80, 0.55)),
                                 (aspect * 0.22, 0.80, 0.10, (0.20, 0.55, 0.85))):
        glow = np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / size)
        rgb += glow[..., None] * np.asarray(colour, np.float32) * 0.85
    return _image(rgb)


def frosted_glass(width: int, height: int) -> Image.Image:
    """Frosted glass panels floating over soft abstract shapes."""
    small_w, small_h = max(32, width // 8), max(18, height // 8)
    shapes = Image.new("RGB", (small_w, small_h), (22, 26, 48))
    draw = ImageDraw.Draw(shapes)
    rng = random.Random(41)
    for colour in ((120, 90, 255), (255, 110, 170), (60, 210, 220), (255, 190, 90)):
        r = rng.randint(small_h // 4, small_h // 2)
        cx, cy = rng.randint(0, small_w), rng.randint(0, small_h)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    soft = shapes.filter(ImageFilter.GaussianBlur(small_h / 9)).resize((width, height), Image.BICUBIC)
    frosted = shapes.filter(ImageFilter.GaussianBlur(small_h / 4)).resize((width, height), Image.BICUBIC)
    out = soft.copy()
    mask = Image.new("L", (width, height), 0)
    panels = ImageDraw.Draw(mask)
    edges = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    lines = ImageDraw.Draw(edges)
    radius = max(8, height // 30)
    for box in ((0.10, 0.16, 0.46, 0.70), (0.52, 0.30, 0.88, 0.86), (0.36, 0.06, 0.66, 0.30)):
        rect = [int(box[0] * width), int(box[1] * height), int(box[2] * width), int(box[3] * height)]
        panels.rounded_rectangle(rect, radius=radius, fill=255)
        lines.rounded_rectangle(rect, radius=radius, outline=(255, 255, 255, 70),
                                width=max(1, height // 400))
    milky = Image.blend(frosted, Image.new("RGB", (width, height), (235, 238, 250)), 0.22)
    out.paste(milky, (0, 0), mask)
    return Image.alpha_composite(out.convert("RGBA"), edges).convert("RGB")


def blueprint(width: int, height: int) -> Image.Image:
    """An architect's blueprint: grid, ticks, crosshairs and guide circles."""
    image = Image.new("RGB", (width, height), (16, 52, 102))
    draw = ImageDraw.Draw(image)
    minor = max(8, height // 45)
    major = minor * 5
    thin, bold = (40, 84, 140), (80, 130, 190)
    for x in range(0, width, minor):
        draw.line([(x, 0), (x, height)], fill=bold if x % major == 0 else thin, width=1)
    for y in range(0, height, minor):
        draw.line([(0, y), (width, y)], fill=bold if y % major == 0 else thin, width=1)
    line = (180, 210, 240)
    tick = max(3, minor // 2)
    for x in range(0, width, major):
        for y in range(0, height, major):
            draw.line([(x - tick, y), (x + tick, y)], fill=line)
            draw.line([(x, y - tick), (x, y + tick)], fill=line)
    for cx, cy, r in ((0.28, 0.40, 0.20), (0.74, 0.62, 0.14), (0.74, 0.62, 0.07)):
        px, py, pr = cx * width, cy * height, r * height
        draw.ellipse([px - pr, py - pr, px + pr, py + pr], outline=line, width=max(1, height // 540))
        draw.line([(px - pr * 1.3, py), (px + pr * 1.3, py)], fill=line)
        draw.line([(px, py - pr * 1.3), (px, py + pr * 1.3)], fill=line)
    draw.line([(0.28 * width, 0.40 * height), (0.74 * width, 0.62 * height)], fill=line)
    return image


def topography(width: int, height: int) -> Image.Image:
    """Contour lines of an imaginary elevation map."""
    field = _fractal(width, height, 7, octaves=(320, 140, 60))
    levels = field * 26.0
    fraction = levels - np.floor(levels)
    edge = np.minimum(fraction, 1.0 - fraction)
    gy, gx = np.gradient(levels)
    steep = np.sqrt(gx * gx + gy * gy) + 1e-3
    line = np.clip(1.0 - edge / (steep * 1.3), 0.0, 1.0)
    major = (np.floor(levels).astype(np.int32) % 5 == 0)
    strength = line * np.where(major, 0.9, 0.45)
    base = _mix((0.09, 0.14, 0.13), (0.13, 0.20, 0.18), field)
    return _image(base + strength[..., None] * np.asarray((0.45, 0.80, 0.66), np.float32) * 0.6)


def split_tone(width: int, height: int) -> Image.Image:
    """A hard diagonal split between two contrasting colours."""
    xs, ys = _grid(width, height)
    side = (xs / width * 1.0 + ys / height * 0.55) - 0.78
    left = _mix((0.98, 0.42, 0.36), (0.88, 0.30, 0.34), ys / height)
    right = _mix((0.13, 0.12, 0.34), (0.07, 0.07, 0.20), ys / height)
    rgb = np.where((side < 0)[..., None], left, right)
    seam = np.exp(-(side * width / 3.0) ** 2)[..., None] * 0.35
    return _image(rgb + seam)


def concrete(width: int, height: int) -> Image.Image:
    """A raw concrete or plaster wall with fine pores."""
    # Fine octaves only: the coarse ones made it look like clouds.
    field = _fractal(width, height, 3, octaves=(48, 18, 7, 3))
    rng = np.random.default_rng(55)
    pores = (rng.random((height, width)) > 0.9985).astype(np.float32)
    pores = np.asarray(Image.fromarray((pores * 255).astype(np.uint8), "L")
                       .filter(ImageFilter.GaussianBlur(1.2)), np.float32) / 255.0
    grain = rng.normal(0.0, 0.025, (height, width)).astype(np.float32)
    tone = 0.47 + (field - 0.5) * 0.22 + grain - pores * 0.8
    rgb = tone[..., None] * np.asarray((1.0, 0.99, 0.96), np.float32)
    return _image(rgb * _vignette(width, height, 0.35)[..., None])


def fluted_glass(width: int, height: int) -> Image.Image:
    """Reeded glass ribs stretching vivid colours behind them."""
    xs, ys = _grid(width, height)
    x, y = xs / height, ys / height
    aspect = width / height
    colours = np.zeros((height, width, 3), np.float32) + np.asarray((0.10, 0.06, 0.20), np.float32)
    for cx, cy, size, colour in ((aspect * 0.30, 0.45, 0.10, (1.0, 0.35, 0.55)),
                                 (aspect * 0.62, 0.62, 0.12, (0.30, 0.55, 1.0)),
                                 (aspect * 0.82, 0.25, 0.07, (1.0, 0.75, 0.30))):
        colours += np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / size)[..., None] * np.asarray(colour, np.float32)
    rib = max(10, height // 22)
    local = (xs % rib) / rib                         # 0..1 across one rib
    # Each rib magnifies and shifts what is behind it, like a lens.
    source_x = np.clip(xs - (local - 0.5) * rib * 1.6, 0, width - 1).astype(np.int32)
    refracted = colours[ys.astype(np.int32), source_x]
    shade = 0.78 + 0.32 * np.sin(local * np.pi) - 0.18 * (local > 0.94)
    return _image(refracted * shade[..., None])


def terrazzo(width: int, height: int) -> Image.Image:
    """Terrazzo: stone chips scattered over a warm neutral."""
    image = Image.new("RGB", (width, height), (232, 224, 210))
    draw = ImageDraw.Draw(image)
    rng = random.Random(61)
    palette = ((196, 104, 74), (136, 160, 132), (40, 52, 84), (222, 170, 72), (150, 140, 128))
    count = max(160, width * height // 9000)
    for _ in range(count):
        cx, cy = rng.uniform(0, width), rng.uniform(0, height)
        size = rng.uniform(height * 0.006, height * 0.03)
        sides = rng.randint(3, 6)
        start = rng.uniform(0, math.tau)
        points = [(cx + math.cos(start + i * math.tau / sides) * size * rng.uniform(0.6, 1.2),
                   cy + math.sin(start + i * math.tau / sides) * size * rng.uniform(0.6, 1.2))
                  for i in range(sides)]
        draw.polygon(points, fill=rng.choice(palette))
    speckle = np.asarray(image, np.float32) / 255.0
    noise = np.random.default_rng(62).normal(0.0, 0.02, speckle.shape[:2]).astype(np.float32)
    return _image(speckle + noise[..., None])


STATIC = {
    "Film Grain": film_grain,
    "Focal Glow": focal_glow,
    "Frosted Glass": frosted_glass,
    "Blueprint": blueprint,
    "Topography": topography,
    "Split Tone": split_tone,
    "Concrete": concrete,
    "Fluted Glass": fluted_glass,
    "Terrazzo": terrazzo,
}
