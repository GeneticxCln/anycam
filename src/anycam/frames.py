"""Themed, multi-colour picture frames (Christmas, Halloween, gold, cyber HUD...).

Each theme is a function that paints a transparent RGBA overlay the size of the
layer.  They mix gradients, stripes, drawn art and the bundled emoji pack, so a
frame is a finished picture instead of one coloured line.  ``THEMES`` maps the
style key to ``(label, group, painter)``; ``overlay.render_window_frame`` calls
:func:`render` for those keys.
"""
from __future__ import annotations

import colorsys
import math
import random
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import emoji

RGBA = tuple[int, int, int, int]


def _hex(value: str, alpha: int = 255) -> RGBA:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha)


# --- building blocks ----------------------------------------------------------

def _canvas(w: int, h: int) -> Image.Image:
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def _ring(w: int, h: int, band: int, radius: int = 0, inset: int = 0) -> Image.Image:
    """An 'L' mask of a border of ``band`` pixels, optionally rounded."""
    outer = Image.new("L", (w * 2, h * 2), 0)
    d = ImageDraw.Draw(outer)
    box = [inset * 2, inset * 2, w * 2 - 1 - inset * 2, h * 2 - 1 - inset * 2]
    d.rounded_rectangle(box, radius=radius * 2, fill=255)
    inner = [(inset + band) * 2, (inset + band) * 2,
             w * 2 - 1 - (inset + band) * 2, h * 2 - 1 - (inset + band) * 2]
    d.rounded_rectangle(inner, radius=max(0, radius - band // 3) * 2, fill=0)
    return outer.resize((w, h), Image.LANCZOS)


def _gradient(w: int, h: int, stops: list[RGBA], angle: float = 0.0) -> Image.Image:
    """A linear gradient through ``stops`` at ``angle`` degrees."""
    steps = 256
    strip = Image.new("RGBA", (steps, 1))
    for x in range(steps):
        pos = x / (steps - 1) * (len(stops) - 1)
        i = min(int(pos), len(stops) - 2)
        f = pos - i
        a, b = stops[i], stops[i + 1]
        strip.putpixel((x, 0), tuple(int(a[k] + (b[k] - a[k]) * f) for k in range(4)))
    diag = int(math.hypot(w, h)) + 2
    big = strip.resize((diag, diag), Image.BILINEAR)
    big = big.rotate(-angle, resample=Image.BILINEAR)
    left, top = (diag - w) // 2, (diag - h) // 2
    return big.crop((left, top, left + w, top + h))


def _stripes(w: int, h: int, colors: list[RGBA], period: int, slant: bool = True) -> Image.Image:
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    n = 0
    for x in range(-h - period, w + period, period):
        color = colors[n % len(colors)]
        n += 1
        if slant:
            d.polygon([(x, 0), (x + period, 0), (x + period + h, h), (x + h, h)], fill=color)
        else:
            d.rectangle([x, 0, x + period, h], fill=color)
    return img


def _fill_ring(canvas: Image.Image, paint: Image.Image, mask: Image.Image) -> None:
    layer = paint.copy()
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    canvas.alpha_composite(layer)


def _shadowed(canvas: Image.Image, strength: int = 120, blur: int = 3) -> Image.Image:
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow.putalpha(canvas.getchannel("A").point(lambda v: min(strength, v)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    shadow.alpha_composite(canvas)
    return shadow


def _perimeter(w: int, h: int, inset: int, spacing: float):
    """Points (x, y, corner) evenly spaced around the rectangle ``inset`` px in."""
    left, top, right, bottom = inset, inset, w - 1 - inset, h - 1 - inset
    pts = []
    for edge, (x0, y0, x1, y1) in enumerate((
            (left, top, right, top), (right, top, right, bottom),
            (right, bottom, left, bottom), (left, bottom, left, top))):
        length = math.hypot(x1 - x0, y1 - y0)
        count = max(1, int(length / spacing))
        for i in range(count):
            t = i / count
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, edge))
    return pts


def _place(canvas: Image.Image, code: str, cx: float, cy: float, size: int,
           angle: float = 0.0) -> None:
    pic = emoji.load(code)
    if pic is None:
        return
    pic = pic.resize((size, size), Image.LANCZOS)
    if angle:
        pic = pic.rotate(angle, resample=Image.BICUBIC, expand=True)
    canvas.alpha_composite(pic, (int(cx - pic.width / 2), int(cy - pic.height / 2)))


def _emoji_border(canvas: Image.Image, codes: list[str], band: int, size: float = 1.25,
                  spacing: float = 1.15, corner: str | None = None,
                  corner_scale: float = 2.0, seed: int = 1) -> None:
    w, h = canvas.size
    rnd = random.Random(seed)
    px = max(10, int(band * size))
    pts = _perimeter(w, h, band // 2, px * spacing)
    for i, (x, y, _edge) in enumerate(pts):
        _place(canvas, codes[i % len(codes)], x, y, px, rnd.uniform(-14, 14))
    if corner:
        big = int(px * corner_scale)
        for cx, cy in ((band, band), (w - band, band), (band, h - band), (w - band, h - band)):
            _place(canvas, corner, cx, cy, big)


# --- themes -------------------------------------------------------------------

def _band_size(w: int, h: int, thickness: int) -> int:
    # Capped well inside half the short side: at thickness 48 on 1080p the
    # band was 628 px, the inner rectangle inverted and Pillow raised.
    return max(8, min(int(min(w, h) * 0.012 * max(1, thickness)) + 6,
                      int(min(w, h) * 0.35)))


def christmas(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    mask = _ring(w, h, band)
    _fill_ring(img, _stripes(w, h, [(206, 32, 41, 255), (250, 250, 250, 255)], band), mask)
    edge = _ring(w, h, max(2, band // 6), inset=band - max(2, band // 6))
    _fill_ring(img, Image.new("RGBA", (w, h), (24, 110, 52, 255)), edge)
    img = _shadowed(img)
    _emoji_border(img, ["1f384", "1f514", "2744", "1f381", "1f36c", "2b50"], band, 1.1,
                  1.5, corner="1f385", corner_scale=2.2)
    return img


def christmas_lights(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    colors = [(255, 59, 48), (255, 204, 0), (52, 199, 89), (10, 132, 255), (255, 45, 150)]
    pts = _perimeter(w, h, band // 2, band * 1.7)
    wire = (20, 70, 30, 255)
    for (x1, y1, _), (x2, y2, _) in zip(pts, pts[1:] + pts[:1]):
        d.line([x1, y1, x2, y2], fill=wire, width=max(2, band // 8))
    glow = _canvas(w, h)
    gd = ImageDraw.Draw(glow)
    r = max(4, int(band * 0.42))
    for i, (x, y, _) in enumerate(pts):
        c = colors[i % len(colors)]
        gd.ellipse([x - r * 2, y - r * 2, x + r * 2, y + r * 2], fill=c[:3] + (90,))
    glow = glow.filter(ImageFilter.GaussianBlur(r))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img)
    for i, (x, y, _) in enumerate(pts):
        c = colors[i % len(colors)]
        d.ellipse([x - r, y - r * 1.2, x + r, y + r * 1.2], fill=c + (255,))
        d.ellipse([x - r * 0.45, y - r * 0.8, x + r * 0.05, y - r * 0.25],
                  fill=(255, 255, 255, 190))
    return img


def winter(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    rnd = random.Random(3)
    mask = _ring(w, h, band)
    _fill_ring(img, _gradient(w, h, [(214, 236, 255, 255), (255, 255, 255, 255),
                                     (180, 215, 250, 255)], 45), mask)
    d = ImageDraw.Draw(img)
    for x in range(0, w, max(6, band // 2)):          # snow drifts on top and bottom
        r = rnd.randint(band // 3, band // 2 + 3)
        d.ellipse([x - r, band // 2 - r // 2, x + r, band // 2 + r], fill=(255, 255, 255, 255))
        d.ellipse([x - r, h - band // 2 - r, x + r, h - band // 2 + r // 2],
                  fill=(255, 255, 255, 255))
    img = _shadowed(img, 90)
    _emoji_border(img, ["2744", "26c4", "2744", "1f9e3", "2744", "1f9ca"], band, 1.0, 2.2,
                  corner="2744", corner_scale=2.0, seed=4)
    return img


def halloween(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    mask = _ring(w, h, band)
    _fill_ring(img, _stripes(w, h, [(255, 120, 10, 255), (30, 14, 44, 255)], band, False), mask)
    img = _shadowed(img)
    d = ImageDraw.Draw(img)
    web = (235, 235, 245, 230)
    reach = band * 3
    for cx, cy, sx, sy in ((0, 0, 1, 1), (w, 0, -1, 1), (0, h, 1, -1), (w, h, -1, -1)):
        for k in range(1, 6):
            r = reach * k / 5
            pts = [(cx + sx * r * math.cos(a), cy + sy * r * math.sin(a))
                   for a in (i * math.pi / 2 / 4 for i in range(5))]
            d.line(pts, fill=web, width=1)
        for i in range(5):
            a = i * math.pi / 2 / 4
            d.line([cx, cy, cx + sx * reach * math.cos(a), cy + sy * reach * math.sin(a)],
                   fill=web, width=1)
    _emoji_border(img, ["1f383", "1f987", "1f47b", "1f577", "1f480", "1f9d9"], band, 1.1, 1.6,
                  corner="1f578", corner_scale=2.0, seed=5)
    return img


def valentine(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(255, 105, 150, 255), (255, 182, 205, 255),
                                     (220, 40, 100, 255)], 30), _ring(w, h, band, band // 2))
    img = _shadowed(img)
    _emoji_border(img, ["2764", "1f496", "1f495", "1f339", "1f48c", "1f498", "1f49c"], band,
                  1.1, 1.3, corner="1f496", corner_scale=2.1, seed=6)
    return img


def birthday(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    flags = [(255, 82, 82), (255, 202, 40), (66, 165, 245), (102, 187, 106), (171, 71, 188)]
    n = max(6, w // (band * 2))
    for i in range(n):                                  # bunting strung across the top
        x0 = w * i / n
        x1 = w * (i + 1) / n
        for j in range(3):
            fx0 = x0 + (x1 - x0) * j / 3
            fx1 = x0 + (x1 - x0) * (j + 1) / 3
            sag = math.sin((fx0 + fx1) / 2 / w * math.pi * n) ** 2
            y = band * 0.3 + sag * band * 0.6
            d.polygon([(fx0, y), (fx1, y), ((fx0 + fx1) / 2, y + band * 1.5)],
                      fill=flags[(i * 3 + j) % len(flags)] + (255,))
    d.line([(0, band * 0.3)] + [(w * k / 40, band * 0.3 + math.sin(w * k / 40 / w * math.pi * n) ** 2 * band * 0.6) for k in range(41)], fill=(90, 70, 50, 255), width=2)
    rnd = random.Random(7)
    for _ in range(int(w * h / 2500)):                  # confetti along the sides
        side = rnd.random() < 0.5
        x = rnd.uniform(0, band * 1.2) if side else rnd.uniform(w - band * 1.2, w)
        y = rnd.uniform(band, h)
        c = rnd.choice(flags) + (255,)
        d.rectangle([x, y, x + 4, y + 8], fill=c)
    _emoji_border(img, ["1f388", "1f389", "1f381", "1f382", "1f38a"], int(band * 1.2), 1.0, 2.4,
                  corner="1f973", corner_scale=1.8, seed=8)
    return img


def new_year(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(15, 18, 52, 255), (62, 34, 120, 255), (15, 18, 52, 255)], 60),
               _ring(w, h, band))
    d = ImageDraw.Draw(img)
    rnd = random.Random(9)
    for _ in range(int(w * h / 900)):
        x, y = rnd.uniform(0, w), rnd.uniform(0, h)
        if min(x, w - x, y, h - y) < band:
            r = rnd.uniform(0.6, 2.2)
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 235, 150, 255))
    gold = _ring(w, h, max(2, band // 7), inset=band)
    _fill_ring(img, _gradient(w, h, [(255, 221, 120, 255), (190, 140, 40, 255),
                                     (255, 235, 160, 255)], 45), gold)
    _emoji_border(img, ["1f386", "1f387", "1f942", "2728", "1f37e", "1f389"], band, 1.0, 2.0,
                  corner="1f386", corner_scale=2.0, seed=10)
    return img


def autumn(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(120, 60, 20, 255), (190, 100, 30, 255),
                                     (120, 60, 20, 255)], 20), _ring(w, h, band, band // 3))
    img = _shadowed(img)
    _emoji_border(img, ["1f341", "1f342", "1f343", "1f344", "1f33e"], band, 1.2, 0.95,
                  corner="1f383", corner_scale=1.9, seed=11)
    return img


def sakura(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(255, 214, 230, 255), (255, 240, 246, 255),
                                     (255, 190, 215, 255)], 40), _ring(w, h, band, band // 2))
    img = _shadowed(img, 80)
    _emoji_border(img, ["1f338", "1f338", "1f33a", "1f338", "1f98b"], band, 1.15, 1.0,
                  corner="1f338", corner_scale=2.0, seed=12)
    return img


def garden(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(52, 130, 62, 255), (90, 170, 80, 255),
                                     (40, 110, 55, 255)], 80), _ring(w, h, band, band // 2))
    img = _shadowed(img)
    _emoji_border(img, ["1f33b", "1f337", "1f33a", "1f33c", "1f33f", "1f41d", "1f98b"], band,
                  1.2, 0.95, corner="1f33b", corner_scale=1.9, seed=13)
    return img


def tropical(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(0, 170, 190, 255), (250, 210, 120, 255),
                                     (255, 120, 120, 255)], 25), _ring(w, h, band, band // 2))
    img = _shadowed(img)
    _emoji_border(img, ["1f334", "1f33a", "1f41a", "1f420", "2600", "1f30a"], band, 1.15, 1.1,
                  corner="1f334", corner_scale=2.0, seed=14)
    return img


def ocean(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(8, 70, 150, 255), (30, 160, 220, 255),
                                     (8, 70, 150, 255)], 90), _ring(w, h, band, band // 2))
    d = ImageDraw.Draw(img)
    for x in range(0, w, max(8, band)):                 # white foam curls
        d.arc([x, band - band // 2, x + band, band + band // 2], 180, 360,
              fill=(255, 255, 255, 220), width=2)
    img = _shadowed(img)
    _emoji_border(img, ["1f419", "1f420", "1f988", "1f41a", "1f30a"], band, 1.1, 1.3,
                  corner="1f419", corner_scale=1.9, seed=15)
    return img


def animals(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(255, 206, 120, 255), (255, 240, 200, 255),
                                     (255, 190, 100, 255)], 30), _ring(w, h, band, band // 2))
    img = _shadowed(img, 90)
    _emoji_border(img, ["1f431", "1f436", "1f430", "1f98a", "1f43c", "1f43b", "1f438", "1f43e"],
                  band, 1.15, 1.0, corner="1f43e", corner_scale=1.8, seed=16)
    return img


def space(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(5, 5, 25, 255), (45, 20, 90, 255), (5, 20, 60, 255)], 50),
               _ring(w, h, band))
    d = ImageDraw.Draw(img)
    rnd = random.Random(17)
    for _ in range(int(w * h / 500)):
        x, y = rnd.uniform(0, w), rnd.uniform(0, h)
        if min(x, w - x, y, h - y) < band:
            r = rnd.choice((0.7, 1.0, 1.6))
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, rnd.randint(150, 255)))
    _emoji_border(img, ["2b50", "1f680", "1f6f8", "1f31f", "1f319", "1f47d"], band, 1.0, 2.0,
                  corner="1f320", corner_scale=2.0, seed=18)
    return img


def party_confetti(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    rnd = random.Random(19)
    palette = [(255, 71, 87), (255, 165, 2), (46, 213, 115), (30, 144, 255), (255, 107, 237),
               (255, 234, 167)]
    for _ in range(int(w * h / 350)):
        x, y = rnd.uniform(0, w), rnd.uniform(0, h)
        if min(x, w - x, y, h - y) < band * 1.1:
            c = rnd.choice(palette) + (255,)
            s = rnd.randint(3, max(4, band // 3))
            if rnd.random() < 0.5:
                d.ellipse([x, y, x + s, y + s], fill=c)
            else:
                d.polygon([(x, y), (x + s * 1.6, y + s * .4), (x + s * .6, y + s * 1.4)], fill=c)
    return img


def rainbow(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    stops = [tuple(int(255 * c) for c in colorsys.hsv_to_rgb(i / 6, .85, 1.0)) + (255,)
             for i in range(7)]
    mask = _ring(w, h, band, band // 2)
    _fill_ring(img, _gradient(w, h, stops, 35), mask)
    glow = img.filter(ImageFilter.GaussianBlur(band // 2))
    glow.alpha_composite(img)
    return glow


def neon_trio(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    pink, blue = (255, 60, 190), (40, 190, 255)
    img = _canvas(w, h)
    for inset, color in ((0, pink), (band // 2 + 2, blue)):
        line = _ring(w, h, max(3, band // 4), band // 2, inset=inset)
        paint = Image.new("RGBA", (w, h), color + (255,))
        glow = _canvas(w, h)
        _fill_ring(glow, paint, line)
        halo = glow.filter(ImageFilter.GaussianBlur(band // 2))
        img.alpha_composite(halo)
        img.alpha_composite(halo)
        hot = _canvas(w, h)
        _fill_ring(hot, Image.new("RGBA", (w, h), (255, 255, 255, 255)),
                   _ring(w, h, max(1, band // 10), band // 2, inset=inset + band // 10))
        img.alpha_composite(glow)
        img.alpha_composite(hot)
    return img


def _neon_single(color):
    def paint(w, h, thickness, _color):
        band = _band_size(w, h, thickness)
        img = _canvas(w, h)
        line = _ring(w, h, max(3, band // 3), band // 2, inset=band // 3)
        coloured = _canvas(w, h)
        _fill_ring(coloured, Image.new("RGBA", (w, h), color + (255,)), line)
        halo = coloured.filter(ImageFilter.GaussianBlur(band // 2))
        img.alpha_composite(halo)
        img.alpha_composite(halo)
        img.alpha_composite(coloured)
        core = _canvas(w, h)
        _fill_ring(core, Image.new("RGBA", (w, h), (255, 255, 255, 235)),
                   _ring(w, h, max(1, band // 8), band // 2, inset=band // 3 + band // 10))
        img.alpha_composite(core)
        return img
    return paint


def cyber_hud(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    cyan, mag, dark = (0, 240, 255, 255), (255, 0, 140, 255), (6, 16, 32, 215)
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    cut = band * 2
    # tinted outer plate with chamfered corners
    d.polygon([(cut, 0), (w - cut, 0), (w, cut), (w, h - cut), (w - cut, h), (cut, h),
               (0, h - cut), (0, cut)], fill=dark)
    inner = band
    d.rectangle([inner, inner, w - inner - 1, h - inner - 1], fill=(0, 0, 0, 0))
    glow = _canvas(w, h)
    gd = ImageDraw.Draw(glow)
    L = max(24, min(w, h) // 4)
    t = max(2, band // 4)
    for sx, sy, x0, y0 in ((1, 1, 0, 0), (-1, 1, w - 1, 0), (1, -1, 0, h - 1), (-1, -1, w - 1, h - 1)):
        x, y = x0 + sx * inner // 2, y0 + sy * inner // 2
        gd.line([x, y + sy * cut // 2, x + sx * cut // 2, y], fill=cyan, width=t)
        gd.line([x + sx * cut // 2, y, x + sx * L, y], fill=cyan, width=t)
        gd.line([x, y + sy * cut // 2, x, y + sy * L], fill=cyan, width=t)
        gd.line([x + sx * (L + 8), y, x + sx * (L + 8 + L // 3), y], fill=mag, width=t)
        gd.line([x, y + sy * (L + 8), x, y + sy * (L + 8 + L // 3)], fill=mag, width=t)
    for i in range(1, 12):                               # ruler ticks along the top
        x = w * i / 12
        gd.line([x, inner // 2, x, inner // 2 + (band // 2 if i % 3 else band)], fill=cyan, width=1)
    gd.rectangle([w - cut * 2.2, h - inner * 0.9, w - cut, h - inner * 0.3], fill=mag)
    gd.rectangle([cut, h - inner * 0.9, cut * 2.2, h - inner * 0.3], fill=cyan)
    halo = glow.filter(ImageFilter.GaussianBlur(band // 3))
    img.alpha_composite(halo)
    img.alpha_composite(glow)
    for y in range(0, h, 4):                             # faint scanlines over the plate
        d.line([0, y, w, y], fill=(0, 0, 0, 22))
    return img


def gaming(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    stops = [(255, 0, 80, 255), (255, 200, 0, 255), (0, 255, 120, 255), (0, 170, 255, 255),
             (170, 0, 255, 255), (255, 0, 80, 255)]
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, stops, 20), _ring(w, h, band, 6))
    _fill_ring(img, Image.new("RGBA", (w, h), (10, 10, 14, 255)),
               _ring(w, h, max(2, band // 4), 6, inset=band - max(2, band // 4)))
    glow = img.filter(ImageFilter.GaussianBlur(band // 2))
    glow.alpha_composite(img)
    return glow


def vhs(w, h, thickness, _color):
    """Camcorder look: black bars with PLAY / REC text and a chromatic ghost."""
    from . import overlay
    band = _band_size(w, h, thickness)
    bar = int(band * 1.7)
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, bar], fill=(8, 8, 12, 235))
    d.rectangle([0, h - bar, w, h], fill=(8, 8, 12, 235))
    d.rectangle([0, 0, band // 3, h], fill=(8, 8, 12, 235))
    d.rectangle([w - band // 3, 0, w, h], fill=(8, 8, 12, 235))
    for y in range(0, bar, 3):                           # scanlines in the bars only
        d.line([0, y, w, y], fill=(255, 255, 255, 18))
        d.line([0, h - bar + y, w, h - bar + y], fill=(255, 255, 255, 18))
    font = overlay._load_font(max(10, int(bar * 0.55)))
    d.text((band, bar // 2), "\u25b6 PLAY", font=font, fill=(235, 235, 235, 255), anchor="lm")
    d.text((w - band, bar // 2), "SP  0:03:27", font=font, fill=(235, 235, 235, 255), anchor="rm")
    d.text((band, h - bar // 2), "AUTO TRACKING", font=font, fill=(235, 235, 235, 255),
           anchor="lm")
    d.ellipse([w - band - bar * 0.5, h - bar * 0.75, w - band, h - bar * 0.25],
              fill=(235, 40, 40, 255))
    for sx, color in ((-2, (255, 0, 60)), (2, (0, 220, 255))):   # chromatic ghosting
        shifted = ImageChops.offset(img.copy(), sx, 0)
        tint = Image.new("RGBA", (w, h), color + (110,))
        tint.putalpha(ImageChops.multiply(shifted.getchannel("A"), Image.new("L", (w, h), 100)))
        img.alpha_composite(tint)
    return img


def comic(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], fill=(255, 214, 0, 255))
    step = max(5, band // 2)
    for y in range(0, h, step):                          # halftone dots
        for x in range((y // step % 2) * step // 2, w, step):
            r = step * 0.28
            d.ellipse([x - r, y - r, x + r, y + r], fill=(235, 60, 60, 255))
    d.rectangle([band, band, w - band - 1, h - band - 1], fill=(0, 0, 0, 0))
    d.rectangle([0, 0, w - 1, h - 1], outline=(10, 10, 10, 255), width=max(3, band // 4))
    d.rectangle([band - 1, band - 1, w - band, h - band], outline=(10, 10, 10, 255),
                width=max(3, band // 4))
    burst = max(30, band * 3)
    pts = []
    for i in range(16):                                  # "POW" starburst in the corner
        r = burst * (1.0 if i % 2 == 0 else 0.55)
        a = i * math.pi * 2 / 16
        pts.append((burst + r * math.cos(a), burst + r * math.sin(a)))
    d.polygon(pts, fill=(255, 255, 255, 255), outline=(10, 10, 10, 255))
    from . import overlay
    font = overlay._load_font(max(12, int(burst * 0.62)))
    d.text((burst, burst), "POW!", font=font, fill=(220, 30, 40, 255), anchor="mm",
           stroke_width=max(1, burst // 30), stroke_fill=(10, 10, 10, 255))
    return img


def gold(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    outer = _ring(w, h, band, 6)
    _fill_ring(img, _gradient(w, h, [(255, 236, 160, 255), (190, 135, 30, 255),
                                     (255, 225, 120, 255), (140, 95, 20, 255),
                                     (255, 236, 160, 255)], 40), outer)
    line = _ring(w, h, max(2, band // 8), 4, inset=band // 2 - 1)
    _fill_ring(img, Image.new("RGBA", (w, h), (110, 70, 10, 255)), line)
    d = ImageDraw.Draw(img)
    r = band * 0.9
    for cx, cy in ((band * .5, band * .5), (w - band * .5, band * .5),
                   (band * .5, h - band * .5), (w - band * .5, h - band * .5)):
        d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)],
                  fill=(255, 232, 140, 255), outline=(110, 70, 10, 255))
        d.ellipse([cx - r / 3, cy - r / 3, cx + r / 3, cy + r / 3], fill=(170, 30, 40, 255))
    return _shadowed(img, 110)


def wood(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    rnd = random.Random(21)
    grain = Image.new("RGBA", (w, h), (150, 96, 52, 255))
    d = ImageDraw.Draw(grain)
    for y in range(0, h, 2):
        shade = rnd.randint(-22, 22)
        d.line([0, y, w, y + rnd.randint(-3, 3)],
               fill=(150 + shade, 96 + shade // 2, 52 + shade // 3, 255), width=2)
    img = _canvas(w, h)
    _fill_ring(img, grain, _ring(w, h, band, 4))
    bevel = _ring(w, h, max(2, band // 7), 3)
    _fill_ring(img, Image.new("RGBA", (w, h), (235, 190, 130, 150)), bevel)
    _fill_ring(img, Image.new("RGBA", (w, h), (60, 34, 14, 255)),
               _ring(w, h, max(2, band // 7), 3, inset=band - max(2, band // 7)))
    return _shadowed(img, 130)


def stamp(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    paper = _ring(w, h, band)
    _fill_ring(img, Image.new("RGBA", (w, h), (252, 249, 238, 255)), paper)
    holes = Image.new("L", (w, h), 255)
    hd = ImageDraw.Draw(holes)
    r = max(3, band // 3)
    step = r * 3
    for x in range(step // 2, w, step):
        hd.ellipse([x - r, -r, x + r, r], fill=0)
        hd.ellipse([x - r, h - r - 1, x + r, h + r], fill=0)
    for y in range(step // 2, h, step):
        hd.ellipse([-r, y - r, r, y + r], fill=0)
        hd.ellipse([w - r - 1, y - r, w + r, y + r], fill=0)
    img.putalpha(ImageChops.multiply(img.getchannel("A"), holes))
    return _shadowed(img, 100)


def washi(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    tapes = [((255, 175, 200), "dots"), ((150, 210, 255), "stripes"),
             ((255, 225, 140), "dots"), ((175, 235, 190), "stripes")]
    tw, th = band * 4, int(band * 1.3)
    for (cx, cy, ang), (color, kind) in zip(
            ((band, band, -38), (w - band, band, 38), (band, h - band, 38), (w - band, h - band, -38)),
            tapes):
        tape = Image.new("RGBA", (tw, th), color + (215,))
        td = ImageDraw.Draw(tape)
        if kind == "dots":
            for x in range(6, tw, 14):
                for y in range(6, th, 14):
                    td.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 255, 255, 210))
        else:
            for x in range(0, tw, 12):
                td.rectangle([x, 0, x + 5, th], fill=(255, 255, 255, 170))
        tape = tape.rotate(ang, expand=True, resample=Image.BICUBIC)
        img.alpha_composite(_shadowed(tape, 80, 2), (int(cx - tape.width / 2), int(cy - tape.height / 2)))
    return img


def candy(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _stripes(w, h, [(255, 110, 170, 255), (255, 255, 255, 255),
                                    (120, 210, 255, 255), (255, 255, 255, 255)],
                             max(6, band // 2)), _ring(w, h, band, band // 2))
    img = _shadowed(img)
    _emoji_border(img, ["1f36d", "1f36c", "1f369", "1f366", "1f36b", "1f9c1"], band, 1.1, 1.5,
                  corner="1f36d", corner_scale=1.8, seed=22)
    return img


def sports(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(20, 60, 160, 255), (230, 30, 40, 255),
                                     (20, 60, 160, 255)], 90), _ring(w, h, band, 4))
    _fill_ring(img, Image.new("RGBA", (w, h), (255, 255, 255, 255)),
               _ring(w, h, max(2, band // 6), 4, inset=band // 2))
    img = _shadowed(img)
    _emoji_border(img, ["1f3c6", "1f947", "2b50", "1f451", "1f4aa", "1f3af"], band, 1.0, 2.2,
                  corner="1f3c6", corner_scale=1.8, seed=23)
    return img


def music(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(30, 20, 60, 255), (110, 30, 130, 255),
                                     (30, 20, 60, 255)], 70), _ring(w, h, band, band // 3))
    d = ImageDraw.Draw(img)
    for k in range(5):                                   # a music staff along the top and bottom
        y = band * 0.25 + k * band * 0.12
        d.line([0, y, w, y], fill=(255, 255, 255, 110), width=1)
        d.line([0, h - y, w, h - y], fill=(255, 255, 255, 110), width=1)
    img = _shadowed(img)
    _emoji_border(img, ["1f3b5", "1f3b6", "1f3a4", "1f3a7", "1f3b8"], band, 1.0, 1.9,
                  corner="1f3b8", corner_scale=1.8, seed=24)
    return img


def ornate_blue(w, h, thickness, _color):
    band = _band_size(w, h, thickness)
    img = _canvas(w, h)
    _fill_ring(img, _gradient(w, h, [(20, 40, 110, 255), (50, 90, 190, 255), (20, 40, 110, 255)], 90),
               _ring(w, h, band, 6))
    _fill_ring(img, _gradient(w, h, [(255, 236, 160, 255), (190, 135, 30, 255), (255, 225, 120, 255)], 40),
               _ring(w, h, max(2, band // 6), 4, inset=band // 2))
    _fill_ring(img, _gradient(w, h, [(255, 236, 160, 255), (190, 135, 30, 255)], 40),
               _ring(w, h, max(2, band // 7), 5))
    _emoji_border(img, ["2728"], band, 0.7, 4.0, corner="1f48e", corner_scale=1.6, seed=25)
    return _shadowed(img, 110)


def _fixed(color):
    return lambda w, h, t, c: _neon_single(color)(w, h, t, c)


# key -> (label, group, painter)
THEMES: dict[str, tuple[str, str, Callable]] = {
    "xmas": ("Christmas", "Holiday", christmas),
    "xmas-lights": ("Christmas lights", "Holiday", christmas_lights),
    "winter": ("Winter snow", "Holiday", winter),
    "halloween": ("Halloween", "Holiday", halloween),
    "valentine": ("Valentine", "Holiday", valentine),
    "birthday": ("Birthday bunting", "Holiday", birthday),
    "new-year": ("New Year", "Holiday", new_year),
    "autumn": ("Autumn leaves", "Holiday", autumn),
    "confetti": ("Confetti", "Holiday", party_confetti),
    "sakura": ("Cherry blossom", "Nature", sakura),
    "garden": ("Flower garden", "Nature", garden),
    "tropical": ("Tropical", "Nature", tropical),
    "ocean": ("Ocean", "Nature", ocean),
    "animals": ("Cute animals", "Nature", animals),
    "space": ("Deep space", "Nature", space),
    "candy": ("Candy", "Fun", candy),
    "sports": ("Champion", "Fun", sports),
    "music": ("Music", "Fun", music),
    "comic": ("Comic book", "Fun", comic),
    "washi": ("Washi tape", "Fun", washi),
    "stamp": ("Postage stamp", "Fun", stamp),
    "cyber-hud": ("Cyber HUD", "Tech", cyber_hud),
    "gaming": ("RGB gaming", "Tech", gaming),
    "vhs": ("VHS tape", "Tech", vhs),
    "neon-trio": ("Neon pink + blue", "Tech", neon_trio),
    "neon-green": ("Neon green", "Tech", _neon_single((60, 255, 120))),
    "neon-orange": ("Neon orange", "Tech", _neon_single((255, 140, 30))),
    "neon-violet": ("Neon violet", "Tech", _neon_single((180, 90, 255))),
    "gold": ("Gold frame", "Fancy", gold),
    "wood": ("Wood frame", "Fancy", wood),
    "royal": ("Royal blue + gold", "Fancy", ornate_blue),
    "rainbow": ("Rainbow", "Fancy", rainbow),
}

GROUPS: tuple[str, ...] = tuple(dict.fromkeys(item[1] for item in THEMES.values()))


def render(style: str, width: int, height: int, thickness: int, color: str) -> Image.Image:
    _label, _group, painter = THEMES[style]
    return painter(width, height, max(1, int(thickness)), color)
