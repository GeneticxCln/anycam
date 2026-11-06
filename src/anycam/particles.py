"""Animated 2D particle layers: falling snow, rising hearts, confetti, fireworks...

Every effect is a generator ``frame(t) -> RGBA numpy array`` like the other
overlays in :mod:`anycam.animated`.  Positions are pure functions of time, so a
frame never depends on the previous one: a stalled thread cannot desync the
animation and the effect needs no per-frame state.  Pictures come from the
bundled emoji pack plus a few drawn sprites (soft dots, bubbles, confetti).
"""
from __future__ import annotations

import colorsys
import math
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import emoji, overlay

SIZE_BOOST = 1.35       # sprites read small on a webcam-sized tile
MAX_SIDE = 960          # particles are drawn at most this wide; the chain scales up


def internal_size(width: int, height: int) -> tuple[int, int]:
    width, height = max(8, int(width)), max(8, int(height))
    if width <= MAX_SIDE:
        return width, height
    return MAX_SIDE, max(8, round(MAX_SIDE * height / width))


# --- sprites ------------------------------------------------------------------

def _soft_dot(px: int, color=(255, 255, 255)) -> Image.Image:
    px = max(3, px)
    big = Image.new("RGBA", (px * 4, px * 4), (0, 0, 0, 0))
    ImageDraw.Draw(big).ellipse([px * 0.6, px * 0.6, px * 3.4, px * 3.4], fill=color + (255,))
    return big.filter(ImageFilter.GaussianBlur(px * 0.35)).resize((px, px), Image.LANCZOS)


def _bubble(px: int) -> Image.Image:
    px = max(6, px)
    s = px * 3
    big = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.ellipse([2, 2, s - 3, s - 3], fill=(170, 220, 255, 45), outline=(220, 240, 255, 230),
              width=max(2, s // 28))
    d.ellipse([s * 0.2, s * 0.18, s * 0.42, s * 0.34], fill=(255, 255, 255, 220))
    d.arc([s * 0.16, s * 0.16, s * 0.84, s * 0.84], 20, 70, fill=(255, 255, 255, 140),
          width=max(1, s // 40))
    return big.resize((px, px), Image.LANCZOS)


def _confetti(px: int, color, angle: float) -> Image.Image:
    px = max(4, px)
    s = px * 3
    big = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(big).rectangle([s * 0.3, s * 0.38, s * 0.7, s * 0.62], fill=color + (255,))
    big = big.rotate(angle, resample=Image.BICUBIC)
    return big.resize((px, px), Image.LANCZOS)


def _spark(px: int, color=(255, 240, 170)) -> Image.Image:
    px = max(6, px)
    s = px * 3
    big = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    c = s / 2
    d.polygon([(c, 0), (c + s * 0.07, c - s * 0.07), (s, c), (c + s * 0.07, c + s * 0.07),
               (c, s), (c - s * 0.07, c + s * 0.07), (0, c), (c - s * 0.07, c - s * 0.07)],
              fill=color + (255,))
    glow = big.filter(ImageFilter.GaussianBlur(s * 0.08))
    glow.alpha_composite(big)
    return glow.resize((px, px), Image.LANCZOS)


def _streak(px: int, color=(190, 215, 255), thin: float = 0.12) -> Image.Image:
    """A line that fades toward its tail; the head is at the bottom."""
    px = max(6, px)
    width = max(2, int(px * thin))
    img = Image.new("RGBA", (width, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i in range(px):
        d.line([(0, i), (width, i)], fill=color + (int(235 * (i / px) ** 1.4),))
    return img


def _ember(px: int) -> Image.Image:
    return _soft_dot(px, (255, 150, 40))


def _make_sprite(kind: str, px: int, variant: int) -> Image.Image | None:
    if kind.startswith("emoji:"):
        pic = emoji.load(kind[6:])
        return None if pic is None else pic.resize((px, px), Image.LANCZOS)
    if kind == "dot":
        return _soft_dot(px)
    if kind == "bubble":
        return _bubble(px)
    if kind == "confetti":
        palette = [(255, 71, 87), (255, 165, 2), (46, 213, 115), (30, 144, 255),
                   (255, 107, 237), (255, 234, 77)]
        return _confetti(px, palette[variant % len(palette)], variant * 23 % 90)
    if kind == "spark":
        return _spark(px)
    if kind == "streak":
        return _streak(px)
    if kind == "ember":
        return _ember(px)
    return None


# --- the general field ----------------------------------------------------------

class ParticleField:
    """Many sprites drifting through the frame, each a function of time."""

    def __init__(self, width: int, height: int, sprites: list[str], count: int = 50,
                 size: tuple[float, float] = (0.03, 0.07), vy: tuple[float, float] = (0.08, 0.18),
                 vx: tuple[float, float] = (-0.02, 0.02), sway: float = 0.0,
                 spin: tuple[float, float] = (0.0, 0.0), twinkle: bool = False,
                 span: float = 1.0, alpha: float = 1.0, seed: int = 1,
                 lean: bool = False) -> None:
        self.width, self.height = width, height
        rnd = random.Random(seed)
        n = max(1, count)
        self.sprite_names = sprites
        self.twinkle = twinkle
        self.span = span
        self.alpha = alpha
        self.lean = lean
        margin = height * size[1] * SIZE_BOOST * 1.6
        self.margin = margin
        self.wrap_w = (width + margin * 2) * (1.0 if lean else span)
        self.wrap_h = (height + margin * 2) * span
        self.x0 = np.array([rnd.uniform(0, self.wrap_w) for _ in range(n)])
        self.y0 = np.array([rnd.uniform(0, self.wrap_h) for _ in range(n)])
        self.vx = np.array([rnd.uniform(*vx) * width for _ in range(n)])
        self.vy = np.array([rnd.uniform(*vy) * height for _ in range(n)])
        self.sway = np.array([rnd.uniform(0.5, 1.0) * sway * width for _ in range(n)])
        self.sway_rate = np.array([rnd.uniform(0.6, 1.6) for _ in range(n)])
        self.phase = np.array([rnd.uniform(0, math.tau) for _ in range(n)])
        self.px = [max(4, int(height * rnd.uniform(*size) * SIZE_BOOST)) for _ in range(n)]
        self.kind = [rnd.randrange(len(sprites)) for _ in range(n)]
        self.spin = np.array([rnd.uniform(*spin) for _ in range(n)])
        self.tw_rate = np.array([rnd.uniform(1.2, 3.0) for _ in range(n)])
        self._cache: dict[tuple[int, int, int], Image.Image] = {}

    def _sprite(self, index: int, angle: float, scale: float) -> Image.Image | None:
        px = max(4, int(self.px[index] * scale))
        step = int(round(angle / 12.0)) % 30
        key = (index if self.sprite_names[self.kind[index]].startswith("confetti") else
               self.kind[index], px, step)
        pic = self._cache.get(key)
        if pic is None:
            name = self.sprite_names[self.kind[index]]
            pic = _make_sprite(name, px, index)
            if pic is None:
                return None
            if step and not name.startswith("confetti"):
                pic = pic.rotate(step * 12.0, resample=Image.BICUBIC, expand=True)
            if len(self._cache) > 900:
                self._cache.clear()
            self._cache[key] = pic
        return pic

    def frame(self, t: float) -> np.ndarray:
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        xs = (self.x0 + self.vx * t
              + self.sway * np.sin(self.sway_rate * t + self.phase)) % self.wrap_w - self.margin
        ys = (self.y0 + self.vy * t) % self.wrap_h - self.margin
        for i in range(len(self.px)):
            scale = 1.0
            if self.twinkle:
                scale = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(t * self.tw_rate[i] + self.phase[i]))
                scale = round(scale * 6) / 6
            angle = self.spin[i] * t
            if self.lean:                                  # point along the motion
                angle = math.degrees(math.atan2(self.vx[i], self.vy[i]))
            pic = self._sprite(i, angle, scale)
            if pic is None:
                continue
            x = int(xs[i] - pic.width / 2)
            y = int(ys[i] - pic.height / 2)
            _blend(canvas, pic, x, y, self.alpha)
        return np.asarray(canvas)


def _blend(canvas: Image.Image, pic: Image.Image, x: int, y: int, alpha: float = 1.0) -> None:
    w, h = canvas.size
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + pic.width), min(h, y + pic.height)
    if x1 <= x0 or y1 <= y0:
        return
    part = pic.crop((x0 - x, y0 - y, x1 - x, y1 - y))
    if alpha < 1.0:
        part = part.copy()
        part.putalpha(part.getchannel("A").point(lambda v: int(v * alpha)))
    region = canvas.crop((x0, y0, x1, y1))
    region.alpha_composite(part)
    canvas.paste(region, (x0, y0))


# --- bespoke effects --------------------------------------------------------------

class Fireworks:
    """Rockets that climb and burst into fading, falling sparks."""

    def __init__(self, width: int, height: int, interval: float = 1.15) -> None:
        self.width, self.height, self.interval = width, height, interval
        self.sparks = 44

    def frame(self, t: float) -> np.ndarray:
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        w, h = self.width, self.height
        k_now = int(t // self.interval)
        for k in range(k_now - 2, k_now + 1):
            if k < 0:
                continue
            rnd = random.Random(k * 7919 + 3)
            born = k * self.interval
            tau = t - born
            if tau < 0:
                continue
            ox = rnd.uniform(0.15, 0.85) * w
            oy = rnd.uniform(0.15, 0.5) * h
            hue = rnd.random()
            rise = 0.55
            if tau < rise:                               # the rocket
                f = tau / rise
                x, y = ox + math.sin(f * 6) * w * 0.004, h - (h - oy) * (1 - (1 - f) ** 2)
                draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 235, 190, 255))
                for j in range(1, 6):
                    draw.ellipse([x - 1.5, y + j * 5 - 1.5, x + 1.5, y + j * 5 + 1.5],
                                 fill=(255, 190, 90, int(200 - j * 35)))
                continue
            tau -= rise
            life = 1.6
            if tau > life:
                continue
            size = max(2.0, h * 0.007)
            for s in range(self.sparks):
                ang = s / self.sparks * math.tau + rnd.uniform(-0.05, 0.05)
                speed = rnd.uniform(0.3, 0.55) * h
                drag = 1 - math.exp(-tau * 2.2)
                x = ox + math.cos(ang) * speed * drag / 1.4
                y = oy + math.sin(ang) * speed * drag / 1.4 + 0.5 * h * 0.12 * tau * tau
                fade = max(0.0, 1.0 - tau / life)
                r, g, b = colorsys.hsv_to_rgb((hue + (s % 3) * 0.04) % 1, 0.75, 1.0)
                color = (int(r * 255), int(g * 255), int(b * 255))
                for trail in range(3):
                    tt = max(0.0, tau - trail * 0.04)
                    dr = 1 - math.exp(-tt * 2.2)
                    tx = ox + math.cos(ang) * speed * dr / 1.4
                    ty = oy + math.sin(ang) * speed * dr / 1.4 + 0.5 * h * 0.12 * tt * tt
                    a = int(255 * fade * (1 - trail * 0.3))
                    rad = size * (1 - trail * 0.25) * (0.5 + fade)
                    draw.ellipse([tx - rad, ty - rad, tx + rad, ty + rad], fill=color + (a,))
                if fade > 0.7:                          # white-hot core of the burst
                    draw.ellipse([x - 1, y - 1, x + 1, y + 1], fill=(255, 255, 255, 255))
        return np.asarray(canvas)


class MatrixRain:
    """Columns of falling green glyphs with a bright head."""

    GLYPHS = "01234567890ABCDEFGHIJKLMNPQRSTUVWXYZ+=*<>$#"

    def __init__(self, width: int, height: int, size: float = 0.034) -> None:
        self.width, self.height = width, height
        self.cell = max(8, int(height * size))
        font = overlay._load_font(self.cell)
        self.sprites: list[list[Image.Image]] = []
        for tone in range(4):                           # head, bright, mid, dim
            row = []
            for ch in self.GLYPHS:
                img = Image.new("RGBA", (self.cell, self.cell), (0, 0, 0, 0))
                color = ((220, 255, 230), (60, 255, 110), (20, 190, 70), (10, 110, 45))[tone]
                ImageDraw.Draw(img).text((self.cell // 2, self.cell // 2), ch, font=font,
                                         fill=color + (255,), anchor="mm")
                row.append(img)
            self.sprites.append(row)
        self.columns = max(1, width // self.cell)
        rnd = random.Random(5)
        self.speed = [rnd.uniform(5, 14) for _ in range(self.columns)]
        self.offset = [rnd.uniform(0, 100) for _ in range(self.columns)]
        self.trail = [rnd.randint(8, 18) for _ in range(self.columns)]

    def frame(self, t: float) -> np.ndarray:
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        rows = self.height // self.cell + 1
        for c in range(self.columns):
            head = (self.offset[c] + t * self.speed[c]) % (rows + self.trail[c])
            for j in range(self.trail[c]):
                row = int(head) - j
                if not 0 <= row < rows:
                    continue
                tone = 0 if j == 0 else 1 if j < 3 else 2 if j < self.trail[c] * 0.6 else 3
                glyph = (c * 31 + row * 17 + int(t * 6 + j * 0.5 if j else t * 12)) % len(self.GLYPHS)
                _blend(canvas, self.sprites[tone][glyph], c * self.cell, row * self.cell)
        return np.asarray(canvas)


class Embers:
    """Glowing sparks drifting up from the bottom, flickering."""

    def __init__(self, width: int, height: int) -> None:
        self.field = ParticleField(width, height, ["ember", "spark"], count=60,
                                   size=(0.008, 0.026), vy=(-0.22, -0.08), vx=(-0.03, 0.03),
                                   sway=0.03, twinkle=True, seed=9)

    def frame(self, t: float) -> np.ndarray:
        return self.field.frame(t)


# --- the catalogue -------------------------------------------------------------------

def _e(*codes: str) -> list[str]:
    return [f"emoji:{c}" for c in codes]


# key -> (label, group, factory(width, height) -> generator)
def _field(sprites, **kwargs):
    return lambda w, h: ParticleField(w, h, sprites, **kwargs)


PRESETS: dict[str, tuple[str, str, object]] = {
    "snow": ("Snowfall", "Weather", _field(
        ["dot", "dot", "dot", "emoji:2744"], count=90, size=(0.012, 0.05), vy=(0.07, 0.2),
        vx=(-0.01, 0.01), sway=0.02, seed=1)),
    "snowflakes": ("Snowflakes", "Weather", _field(
        _e("2744"), count=34, size=(0.04, 0.09), vy=(0.06, 0.15), sway=0.03,
        spin=(-40, 40), seed=2)),
    "blizzard": ("Blizzard", "Weather", _field(
        ["dot"], count=180, size=(0.008, 0.03), vy=(0.25, 0.5), vx=(-0.28, -0.12),
        sway=0.01, seed=3)),
    "rain": ("Rain", "Weather", _field(
        ["streak"], count=130, size=(0.04, 0.09), vy=(0.9, 1.4), vx=(-0.12, -0.08),
        lean=True, seed=4, alpha=0.85)),
    "storm": ("Thunder storm", "Weather", _field(
        _e("26a1", "2601", "1f4a7"), count=24, size=(0.05, 0.11), vy=(0.3, 0.6),
        vx=(-0.1, -0.04), seed=5)),
    "clouds": ("Drifting clouds", "Weather", _field(
        _e("2601"), count=9, size=(0.12, 0.26), vy=(0.0, 0.0), vx=(0.01, 0.05),
        alpha=0.9, seed=6)),
    "sun-rays": ("Sunshine", "Weather", _field(
        _e("2600", "2728"), count=14, size=(0.06, 0.16), vy=(0.0, 0.0), vx=(0.0, 0.0),
        twinkle=True, seed=7)),
    "leaves": ("Autumn leaves", "Nature", _field(
        _e("1f341", "1f342", "1f343"), count=32, size=(0.05, 0.1), vy=(0.08, 0.2),
        vx=(-0.05, 0.02), sway=0.06, spin=(-80, 80), seed=8)),
    "petals": ("Cherry blossoms", "Nature", _field(
        _e("1f338"), count=36, size=(0.04, 0.09), vy=(0.06, 0.16), vx=(-0.04, 0.01),
        sway=0.05, spin=(-70, 70), seed=9)),
    "butterflies": ("Butterflies", "Nature", _field(
        _e("1f98b"), count=10, size=(0.07, 0.13), vy=(-0.07, -0.02), vx=(-0.05, 0.05),
        sway=0.08, spin=(-25, 25), seed=10)),
    "bees": ("Busy bees", "Nature", _field(
        _e("1f41d", "1f33b"), count=14, size=(0.05, 0.1), vy=(-0.05, 0.05), vx=(0.04, 0.12),
        sway=0.05, seed=11)),
    "fish": ("Swimming fish", "Nature", _field(
        _e("1f420", "1f41a", "1f419"), count=14, size=(0.06, 0.13), vy=(-0.02, 0.02),
        vx=(0.05, 0.14), sway=0.02, seed=12)),
    "bubbles": ("Bubbles", "Nature", _field(
        ["bubble"], count=26, size=(0.04, 0.13), vy=(-0.16, -0.07), vx=(-0.01, 0.01),
        sway=0.03, seed=13)),
    "fireflies": ("Fireflies", "Nature", _field(
        ["spark"], count=26, size=(0.02, 0.05), vy=(-0.02, 0.02), vx=(-0.02, 0.02),
        sway=0.05, twinkle=True, seed=14)),
    "hearts": ("Rising hearts", "Love", _field(
        _e("2764", "1f496", "1f495", "1f498"), count=30, size=(0.05, 0.12),
        vy=(-0.22, -0.09), sway=0.04, seed=15)),
    "heart-rain": ("Heart rain", "Love", _field(
        _e("2764", "1f49c", "1f499", "1f49a", "1f9e1", "1f49b"), count=30, size=(0.04, 0.09),
        vy=(0.1, 0.22), sway=0.03, seed=16)),
    "roses": ("Falling roses", "Love", _field(
        _e("1f339", "1f338", "2764"), count=22, size=(0.05, 0.1), vy=(0.07, 0.16),
        sway=0.04, spin=(-60, 60), seed=17)),
    "love-letters": ("Love letters", "Love", _field(
        _e("1f48c", "1f498", "1f496"), count=14, size=(0.07, 0.13), vy=(-0.12, -0.05),
        sway=0.05, seed=18)),
    "confetti": ("Confetti", "Party", _field(
        ["confetti"], count=110, size=(0.018, 0.04), vy=(0.1, 0.3), sway=0.03,
        vx=(-0.03, 0.03), seed=19)),
    "party": ("Party time", "Party", _field(
        _e("1f389", "1f38a", "1f388", "2728", "1f381"), count=26, size=(0.05, 0.11),
        vy=(0.07, 0.2), sway=0.04, spin=(-60, 60), seed=20)),
    "balloons": ("Rising balloons", "Party", _field(
        _e("1f388"), count=16, size=(0.1, 0.2), vy=(-0.16, -0.07), sway=0.04, seed=21)),
    "fireworks": ("Fireworks", "Party", lambda w, h: Fireworks(w, h)),
    "sparklers": ("Sparkle burst", "Party", _field(
        _e("2728", "1f31f", "2b50"), count=36, size=(0.03, 0.09), vy=(0, 0), vx=(0, 0),
        twinkle=True, seed=22)),
    "stars": ("Twinkling stars", "Sparkle", _field(
        ["spark", "emoji:2b50"], count=40, size=(0.02, 0.07), vy=(0, 0), vx=(0, 0),
        twinkle=True, seed=23)),
    "shooting-stars": ("Shooting stars", "Sparkle", _field(
        ["streak"], count=8, size=(0.12, 0.2), vy=(0.5, 0.7), vx=(-0.5, -0.4),
        span=2.0, lean=True, seed=24)),
    "glitter": ("Glitter", "Sparkle", _field(
        ["spark", "dot"], count=70, size=(0.01, 0.035), vy=(0.02, 0.07), vx=(-0.02, 0.02),
        twinkle=True, seed=25)),
    "magic": ("Magic dust", "Sparkle", _field(
        _e("2728", "1f4ab", "1f52e"), count=26, size=(0.04, 0.1), vy=(-0.1, -0.03),
        sway=0.05, twinkle=True, seed=26)),
    "embers": ("Fire embers", "Sparkle", lambda w, h: Embers(w, h)),
    "matrix": ("Matrix code", "Tech", lambda w, h: MatrixRain(w, h)),
    "xmas": ("Christmas magic", "Holiday", _field(
        _e("1f384", "2744", "1f381", "1f514", "2b50", "1f36c"), count=28, size=(0.05, 0.1),
        vy=(0.06, 0.16), sway=0.04, spin=(-40, 40), seed=27)),
    "santa": ("Santa's sleigh", "Holiday", _field(
        _e("1f385", "1f98c", "1f381"), count=6, size=(0.1, 0.17), vy=(-0.02, 0.0),
        vx=(0.14, 0.2), span=2.5, sway=0.01, seed=28)),
    "halloween": ("Halloween night", "Holiday", _field(
        _e("1f987", "1f47b", "1f383", "1f577", "1f480"), count=22, size=(0.06, 0.12),
        vy=(-0.05, 0.1), vx=(0.04, 0.12), sway=0.05, seed=29)),
    "bats": ("Bat swarm", "Holiday", _field(
        _e("1f987"), count=16, size=(0.05, 0.11), vy=(-0.05, 0.0), vx=(0.1, 0.22),
        sway=0.05, span=1.3, seed=30)),
    "ghosts": ("Floating ghosts", "Holiday", _field(
        _e("1f47b"), count=10, size=(0.08, 0.15), vy=(-0.07, -0.02), sway=0.07,
        alpha=0.85, seed=31)),
    "new-year": ("New Year", "Holiday", _field(
        _e("1f389", "1f942", "1f386", "1f387", "1f37e"), count=24, size=(0.06, 0.12),
        vy=(0.08, 0.2), sway=0.04, spin=(-50, 50), seed=32)),
    "space": ("Space trip", "Fun", _field(
        _e("1f680", "1f6f8", "1f47d", "2b50", "1f319"), count=14, size=(0.06, 0.13),
        vy=(0.02, 0.08), vx=(-0.08, 0.08), sway=0.03, spin=(-30, 30), seed=33)),
    "emoji-pizza": ("Pizza party", "Fun", _field(
        _e("1f355", "1f354", "1f35f", "1f366", "1f369", "1f36a"), count=24, size=(0.06, 0.12),
        vy=(0.1, 0.22), sway=0.03, spin=(-70, 70), seed=34)),
    "money": ("Money rain", "Fun", _field(
        _e("1f4b0", "1f48e", "1f4af"), count=26, size=(0.05, 0.1), vy=(0.14, 0.3),
        sway=0.03, spin=(-60, 60), seed=35)),
    "music": ("Music notes", "Fun", _field(
        _e("1f3b5", "1f3b6", "1f3a4", "1f3b8"), count=18, size=(0.06, 0.12),
        vy=(-0.18, -0.07), sway=0.05, spin=(-20, 20), seed=36)),
    "fire": ("Fire & flames", "Fun", _field(
        _e("1f525"), count=24, size=(0.05, 0.12), vy=(-0.14, -0.06), sway=0.03,
        twinkle=True, seed=37)),
    "animals": ("Animal parade", "Fun", _field(
        _e("1f431", "1f436", "1f430", "1f43c", "1f98a", "1f438"), count=16, size=(0.07, 0.13),
        vy=(-0.02, 0.02), vx=(0.04, 0.1), sway=0.03, seed=38)),
    "dinos": ("Dino stampede", "Fun", _field(
        _e("1f995", "1f432", "1f43e"), count=10, size=(0.08, 0.15), vy=(0.0, 0.01),
        vx=(0.08, 0.16), sway=0.01, seed=39)),
}

GROUPS: tuple[str, ...] = tuple(dict.fromkeys(item[1] for item in PRESETS.values()))


def make(source: str, width: int, height: int):
    """A generator for preset ``source`` (``emoji:<code>`` falls any emoji)."""
    width, height = internal_size(width, height)
    key = (source or "").strip()
    if key in PRESETS:
        return PRESETS[key][2](width, height), (width, height)
    if key.startswith("emoji:") and emoji.load(key[6:]) is not None:
        field = ParticleField(width, height, [key], count=34, size=(0.05, 0.11),
                              vy=(0.08, 0.2), sway=0.04, spin=(-60, 60), seed=40)
        return field, (width, height)
    return PRESETS["confetti"][2](width, height), (width, height)


def label_for(source: str) -> str:
    key = (source or "").strip()
    if key in PRESETS:
        return PRESETS[key][0]
    if key.startswith("emoji:"):
        for code, name, _cat, _words in emoji.CATALOGUE:
            if code == key[6:]:
                return f"{name} rain"
    return "Particles"


class Switcher:
    """Wraps the running generator so the animation can change without a rebuild.

    The canvas keeps one ``appsrc`` at a fixed size; picking another animation
    only replaces the generator behind it (an atomic reference swap).
    """

    def __init__(self, generator, width: int, height: int) -> None:
        self.generator = generator
        self.width, self.height = width, height

    def frame(self, t: float):
        return self.generator.frame(t)

    def set_source(self, source: str) -> None:
        self.generator, _size = make(source, self.width, self.height)
