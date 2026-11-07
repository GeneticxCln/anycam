"""Procedural animated backdrops.

Each generator draws one frame as a function of time with numpy, at a low
internal resolution (about 320 px wide) that the chain scales up.  That keeps a
moving backdrop cheap (a few milliseconds per frame) and gives real motion and
real perspective, unlike a test pattern run through a distortion filter.

``ProceduralSource`` feeds a generator into the canvas through an ``appsrc``.
"""
from __future__ import annotations

import sys
import threading
import time

import numpy as np

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

INTERNAL_WIDTH = 320
TWO_PI = 2.0 * np.pi


def internal_size(width: int, height: int) -> tuple[int, int]:
    """Internal resolution with the box's aspect ratio (so it fills the box)."""
    width, height = max(2, int(width)), max(2, int(height))
    inner_w = min(INTERNAL_WIDTH, width)
    inner_h = max(2, int(round(inner_w * height / width)))
    return inner_w, inner_h


def _grid(width: int, height: int):
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    return xs, ys


def _palette(x, offset=(0.0, 0.33, 0.67), freq=1.0):
    """Smooth cosine colour ramp: ``x`` (any shape) -> float RGB 0..1."""
    x = np.asarray(x, dtype=np.float32)[..., None]
    phase = np.asarray(offset, dtype=np.float32)
    return 0.5 + 0.5 * np.cos(TWO_PI * (x * freq + phase))


def _to_rgba(rgb) -> np.ndarray:
    height, width = rgb.shape[:2]
    out = np.empty((height, width, 4), dtype=np.uint8)
    out[..., :3] = np.clip(rgb * 255.0, 0, 255).astype(np.uint8)
    out[..., 3] = 255
    return out


class Tunnel:
    """A perspective tunnel: every pixel maps to (angle, depth) on the wall."""

    label = "3D Tunnel"

    def __init__(self, width: int, height: int) -> None:
        xs, ys = _grid(width, height)
        dx = (xs - width / 2.0) / height
        dy = (ys - height / 2.0) / height
        radius = np.sqrt(dx * dx + dy * dy) + 1e-3
        self.angle = np.arctan2(dy, dx) / TWO_PI * 10.0     # 10 wall segments
        self.depth = 0.32 / radius                            # perspective
        self.shade = np.clip(radius * 2.6, 0.0, 1.0) ** 0.8   # dark far away

    def frame(self, t: float) -> np.ndarray:
        cell = (np.floor(self.angle + t * 0.12)
                + np.floor(self.depth + t * 1.6)).astype(np.int32) & 1
        colour = _palette(self.depth * 0.08 + t * 0.05, (0.0, 0.18, 0.5))
        tile = 0.28 + 0.72 * cell[..., None]
        glow = np.exp(-self.depth * 0.05)[..., None]
        return _to_rgba(colour * tile * self.shade[..., None] * (0.55 + 0.45 * glow))


class Starfield:
    """Stars flying past the camera with a short streak."""

    label = "3D Starfield"

    def __init__(self, width: int, height: int, count: int = 650) -> None:
        self.width, self.height = width, height
        rng = np.random.default_rng(7)
        self.x = rng.uniform(-1.0, 1.0, count).astype(np.float32)
        self.y = rng.uniform(-1.0, 1.0, count).astype(np.float32)
        self.z = rng.uniform(0.08, 1.0, count).astype(np.float32)
        self.tint = rng.uniform(0.0, 1.0, count).astype(np.float32)
        self.last = None
        ys = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
        self.backdrop = np.stack([0.02 + 0.03 * ys, 0.02 + 0.04 * ys,
                                  0.08 + 0.10 * ys], axis=-1) * np.ones(
                                      (1, width, 1), dtype=np.float32)

    def frame(self, t: float) -> np.ndarray:
        now = t
        dt = 0.0 if self.last is None else max(0.0, min(0.1, now - self.last))
        self.last = now
        self.z -= dt * 0.45
        behind = self.z <= 0.04
        if behind.any():
            count = int(behind.sum())
            rng = np.random.default_rng(int(now * 1000) & 0xFFFF)
            self.x[behind] = rng.uniform(-1.0, 1.0, count)
            self.y[behind] = rng.uniform(-1.0, 1.0, count)
            self.z[behind] = 1.0
        image = self.backdrop.copy()
        scale = self.height * 0.55
        colours = _palette(self.tint, (0.55, 0.62, 0.7))
        for streak in range(5):
            z = np.maximum(self.z + streak * 0.014, 0.04)
            px = (self.width / 2.0 + self.x / z * scale * 0.5).astype(np.int32)
            py = (self.height / 2.0 + self.y / z * scale * 0.5).astype(np.int32)
            inside = (px >= 1) & (px < self.width - 1) & (py >= 1) & (py < self.height - 1)
            if not inside.any():
                continue
            near_ness = np.clip(1.0 - z[inside], 0.0, 1.0)
            fade = ((0.35 + 0.65 * near_ness) * (1.0 - streak * 0.18))[:, None]
            image[py[inside], px[inside]] = np.maximum(
                image[py[inside], px[inside]], np.clip(colours[inside] * 0.6 + 0.5, 0, 1) * fade)
            if streak == 0:
                near = inside & (self.z < 0.5)
                for ox, oy in ((1, 0), (0, 1), (1, 1)):
                    image[py[near] + oy, px[near] + ox] = np.maximum(
                        image[py[near] + oy, px[near] + ox],
                        np.clip(colours[near] * 0.5 + 0.45, 0, 1)
                        * np.clip(1.0 - self.z[near], 0.0, 1.0)[:, None])
        return _to_rgba(image)


class Plasma:
    """The classic sum-of-sines plasma."""

    label = "Plasma"

    def __init__(self, width: int, height: int) -> None:
        xs, ys = _grid(width, height)
        self.x = xs / height * 9.0
        self.y = ys / height * 9.0
        self.radial = np.sqrt((self.x - 4.5 * width / height) ** 2
                              + (self.y - 4.5) ** 2)

    def frame(self, t: float) -> np.ndarray:
        value = (np.sin(self.x * 1.1 + t * 0.9)
                 + np.sin(self.y * 1.3 - t * 1.1)
                 + np.sin((self.x + self.y) * 0.7 + t * 0.6)
                 + np.sin(self.radial * 1.4 - t * 1.3)) * 0.25
        return _to_rgba(_palette(value * 0.5 + t * 0.03, (0.0, 0.25, 0.55)) * 0.9)


class Aurora:
    """Slow green-to-violet curtains over a night sky."""

    label = "Aurora"

    def __init__(self, width: int, height: int) -> None:
        xs, ys = _grid(width, height)
        self.u = xs / width
        self.v = ys / height
        self.sky = np.stack([0.01 + 0.04 * self.v, 0.02 + 0.07 * self.v,
                             0.08 + 0.14 * self.v], axis=-1)
        rng = np.random.default_rng(3)
        self.stars = (rng.random((height, width)) > 0.997).astype(np.float32)

    def frame(self, t: float) -> np.ndarray:
        image = self.sky.copy()
        for index, (speed, base) in enumerate(((0.35, 0.40), (0.23, 0.55))):
            centre = (base + 0.10 * np.sin(self.u * 5.0 + t * speed + index)
                      + 0.06 * np.sin(self.u * 11.0 - t * speed * 1.7))
            band = np.exp(-((self.v - centre) / (0.10 + 0.04 * index)) ** 2)
            ray = 0.65 + 0.35 * np.sin(self.u * 60.0 + t * 0.8 + index * 2.0)
            # green at the base of a curtain, cyan, then violet at the top
            height = np.clip((self.v - centre) / 0.35 + 0.5, 0.0, 1.0)[..., None]
            green = np.array([0.10, 1.00, 0.45], dtype=np.float32)
            cyan = np.array([0.15, 0.80, 0.95], dtype=np.float32)
            violet = np.array([0.65, 0.30, 1.00], dtype=np.float32)
            colour = np.where(height > 0.5, cyan + (green - cyan) * (height - 0.5) * 2.0,
                              violet + (cyan - violet) * height * 2.0)
            image += (band * ray * (0.95 - 0.2 * index))[..., None] * colour
        image += self.stars[..., None] * (0.5 + 0.5 * np.sin(t * 3.0 + self.u * 40.0))[..., None]
        return _to_rgba(np.clip(image, 0.0, 1.0))


class Ripples:
    """Water surface with ripples spreading from drifting points."""

    label = "Water ripples"

    def __init__(self, width: int, height: int) -> None:
        xs, ys = _grid(width, height)
        self.x = xs / height
        self.y = ys / height
        self.aspect = width / height

    def frame(self, t: float) -> np.ndarray:
        surface = np.zeros_like(self.x)
        for index in range(3):
            cx = self.aspect * (0.5 + 0.35 * np.sin(t * 0.21 + index * 2.1))
            cy = 0.5 + 0.30 * np.cos(t * 0.17 + index * 1.3)
            distance = np.sqrt((self.x - cx) ** 2 + (self.y - cy) ** 2)
            surface += np.sin(distance * 26.0 - t * 2.4 + index) / (1.0 + distance * 4.0)
        gradient_y, gradient_x = np.gradient(surface)
        light = np.clip(0.5 + (gradient_x + gradient_y) * 3.0, 0.0, 1.0)
        base = np.stack([0.05 + 0.10 * light, 0.30 + 0.30 * light,
                         0.52 + 0.38 * light], axis=-1)
        sparkle = (np.clip(light - 0.78, 0.0, 1.0) * 4.0)[..., None]
        return _to_rgba(np.clip(base * (0.75 + 0.25 * surface[..., None]) + sparkle, 0, 1))


# --------------------------------------------------------------- more stages
def _stops(value, stops) -> np.ndarray:
    """Map ``value`` (0..1, any shape) through colour ``stops`` -> float RGB."""
    value = np.clip(np.asarray(value, dtype=np.float32), 0.0, 1.0)
    positions = np.linspace(0.0, 1.0, len(stops), dtype=np.float32)
    colours = np.asarray(stops, dtype=np.float32)
    return np.stack([np.interp(value, positions, colours[:, c]) for c in range(3)],
                    axis=-1).astype(np.float32)


def _soft_dot(radius: int) -> np.ndarray:
    """A soft round sprite (0..1) of ``radius`` px, for orbs and glows."""
    size = radius * 2 + 1
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32) - radius
    distance = np.sqrt(xs * xs + ys * ys) / max(1.0, radius)
    return np.clip(1.0 - distance, 0.0, 1.0) ** 1.6


def _stamp(image: np.ndarray, sprite: np.ndarray, cx: float, cy: float,
           colour) -> None:
    """Add ``sprite`` * ``colour`` onto ``image`` centred at (cx, cy), clipped."""
    height, width = image.shape[:2]
    radius = sprite.shape[0] // 2
    x0, y0 = int(cx) - radius, int(cy) - radius
    x1, y1 = x0 + sprite.shape[1], y0 + sprite.shape[0]
    sx0, sy0 = max(0, -x0), max(0, -y0)
    sx1 = sprite.shape[1] - max(0, x1 - width)
    sy1 = sprite.shape[0] - max(0, y1 - height)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    patch = sprite[sy0:sy1, sx0:sx1, None] * np.asarray(colour, dtype=np.float32)
    image[max(0, y0):max(0, y0) + (sy1 - sy0), max(0, x0):max(0, x0) + (sx1 - sx0)] += patch


class MeshGlow:
    """Soft blobs of colour drifting and blending into each other."""

    label = "Mesh glow"
    COLOURS = ((0.98, 0.36, 0.58), (0.36, 0.46, 1.00), (0.20, 0.88, 0.80),
               (1.00, 0.72, 0.30), (0.62, 0.32, 0.96))

    def __init__(self, width: int, height: int) -> None:
        # Pure gradients: draw at half size and double the pixels.
        self.width, self.height = width, height
        half_w, half_h = max(2, (width + 1) // 2), max(2, (height + 1) // 2)
        xs, ys = _grid(half_w, half_h)
        self.x, self.y, self.aspect = xs / half_h, ys / half_h, half_w / half_h
        rng = np.random.default_rng(11)
        self.phase = rng.uniform(0.0, TWO_PI, (len(self.COLOURS), 2))
        self.speed = rng.uniform(0.06, 0.15, (len(self.COLOURS), 2))

    def frame(self, t: float) -> np.ndarray:
        total = np.zeros(self.x.shape + (3,), np.float32)
        weight = np.full(self.x.shape, 1e-3, np.float32)
        for index, colour in enumerate(self.COLOURS):
            cx = self.aspect * (0.5 + 0.45 * np.sin(t * self.speed[index, 0] + self.phase[index, 0]))
            cy = 0.5 + 0.45 * np.sin(t * self.speed[index, 1] * 1.3 + self.phase[index, 1])
            blob = np.exp(-((self.x - cx) ** 2 + (self.y - cy) ** 2) / 0.10)
            total += blob[..., None] * np.asarray(colour, np.float32)
            weight += blob
        mixed = total / weight[..., None]
        light = np.clip(weight, 0.0, 1.0) ** 0.5
        small = mixed * (0.45 + 0.5 * light[..., None])
        full = small.repeat(2, axis=0).repeat(2, axis=1)[:self.height, :self.width]
        return _to_rgba(full)


class LiquidChrome:
    """A slow silk/mercury surface with shifting reflections."""

    label = "Liquid chrome"

    def __init__(self, width: int, height: int) -> None:
        xs, ys = _grid(width, height)
        self.x, self.y = xs / height * 3.0, ys / height * 3.0

    def frame(self, t: float) -> np.ndarray:
        u = self.x + 0.35 * np.sin(self.y * 2.2 + t * 0.35)
        v = self.y + 0.35 * np.sin(self.x * 1.7 - t * 0.28)
        surface = np.sin(u * 2.6 + np.sin(v * 2.1 + t * 0.4) * 1.6 + t * 0.15)
        gradient_y, gradient_x = np.gradient(surface)
        light = np.clip(0.5 + (gradient_x * 0.7 - gradient_y) * 6.0, 0.0, 1.0)
        base = 0.30 + 0.28 * surface[..., None] * np.asarray((0.85, 0.9, 1.0), np.float32)
        shine = (light ** 6)[..., None] * np.asarray((0.95, 0.97, 1.0), np.float32)
        tint = np.asarray((0.62, 0.68, 0.80), np.float32)
        return _to_rgba(np.clip(base * tint + shine * 0.9 + 0.08, 0.0, 1.0))


class Thermal:
    """Heat-map contours shifting from cool blues into warm oranges."""

    label = "Thermal"
    STOPS = ((0.05, 0.05, 0.30), (0.30, 0.10, 0.60), (0.85, 0.15, 0.45),
             (1.00, 0.50, 0.15), (1.00, 0.88, 0.40))

    def __init__(self, width: int, height: int) -> None:
        xs, ys = _grid(width, height)
        self.x, self.y, self.aspect = xs / height, ys / height, width / height

    def frame(self, t: float) -> np.ndarray:
        field = np.zeros_like(self.x)
        for index in range(4):
            cx = self.aspect * (0.5 + 0.4 * np.sin(t * (0.11 + index * 0.03) + index * 1.9))
            cy = 0.5 + 0.38 * np.cos(t * (0.09 + index * 0.025) + index * 2.7)
            field += np.exp(-((self.x - cx) ** 2 + (self.y - cy) ** 2) / (0.05 + 0.03 * index))
        field += 0.15 * np.sin(self.x * 4.0 + t * 0.3) * np.sin(self.y * 3.0 - t * 0.2)
        level = np.clip(field / 1.6, 0.0, 1.0)
        bands = np.floor(level * 9.0) / 9.0
        fraction = level * 9.0 - np.floor(level * 9.0)
        lines = np.clip(1.0 - np.minimum(fraction, 1.0 - fraction) * 14.0, 0.0, 1.0)
        colour = _stops(0.15 + 0.85 * bands, self.STOPS)
        return _to_rgba(np.clip(colour * (0.85 + 0.15 * lines[..., None]) + lines[..., None] * 0.12, 0, 1))


class BokehDrift:
    """Out-of-focus light orbs drifting on three parallax layers."""

    label = "Bokeh drift"

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        rng = np.random.default_rng(21)
        count = 34
        self.layer = rng.integers(0, 3, count)
        self.x0 = rng.uniform(0.0, width, count)
        self.y0 = rng.uniform(0.0, height, count)
        self.phase = rng.uniform(0.0, TWO_PI, count)
        self.hue = rng.uniform(0.0, 1.0, count)
        self.sprites = {layer: _soft_dot(max(3, int(height * (0.05 + 0.05 * layer))))
                        for layer in range(3)}
        ys = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
        self.backdrop = (np.asarray((0.07, 0.04, 0.12), np.float32) * (1.0 - ys)
                         + np.asarray((0.14, 0.06, 0.16), np.float32) * ys) * np.ones((1, width, 1), np.float32)

    def frame(self, t: float) -> np.ndarray:
        image = self.backdrop.copy()
        colours = _palette(self.hue * 0.35 + 0.05, (0.0, 0.12, 0.3))
        for index in range(len(self.x0)):
            layer = int(self.layer[index])
            speed = 4.0 + 6.0 * layer
            x = (self.x0[index] + t * speed) % (self.width + 40) - 20
            y = self.y0[index] + np.sin(t * 0.3 + self.phase[index]) * 8.0 * (1 + layer)
            pulse = 0.55 + 0.25 * np.sin(t * 0.8 + self.phase[index])
            _stamp(image, self.sprites[layer], x, y,
                   colours[index] * pulse * (0.35 + 0.15 * layer))
        return _to_rgba(np.clip(image, 0.0, 1.0))


class Cosmos:
    """Deep space: twinkling stars, a drifting nebula, the odd shooting star."""

    label = "Cosmos"

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        xs, ys = _grid(width, height)
        x, y = xs / height, ys / height
        rng = np.random.default_rng(5)
        cloud = np.zeros_like(x)
        for _ in range(9):
            cx, cy = rng.uniform(0, width / height), rng.uniform(0, 1)
            cloud += rng.uniform(0.3, 1.0) * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / rng.uniform(0.02, 0.09))
        cloud *= 0.55 + 0.45 * np.sin(x * 7.0 + np.sin(y * 5.0) * 2.0)
        nebula = (cloud[..., None] * np.asarray((0.45, 0.16, 0.55), np.float32)
                  + (cloud ** 2)[..., None] * np.asarray((0.10, 0.25, 0.45), np.float32))
        self.nebula = np.clip(nebula * 0.6, 0, 1) + np.asarray((0.03, 0.03, 0.07), np.float32)
        count = max(120, width * height // 260)
        self.sx = rng.integers(0, width, count)
        self.sy = rng.integers(0, height, count)
        self.twinkle = rng.uniform(0.0, TWO_PI, count)
        self.bright = rng.uniform(0.35, 1.0, count)

    def frame(self, t: float) -> np.ndarray:
        shift = int(t * 2.0) % self.width
        image = np.roll(self.nebula, shift, axis=1).copy()
        glow = self.bright * (0.55 + 0.45 * np.sin(t * 2.3 + self.twinkle))
        image[self.sy, self.sx] = np.maximum(image[self.sy, self.sx], glow[:, None])
        cycle, period = divmod(t, 6.0)
        if period < 0.8:                       # a shooting star every 6 s
            rng = np.random.default_rng(int(cycle))
            x0, y0 = rng.uniform(0.2, 0.8) * self.width, rng.uniform(0.05, 0.4) * self.height
            progress = period / 0.8
            for step in range(14):
                along = progress * 90.0 - step * 3.0
                if along < 0:
                    continue
                px, py = int(x0 + along), int(y0 + along * 0.45)
                if 0 <= px < self.width and 0 <= py < self.height:
                    fade = (1.0 - step / 14.0) * (1.0 - progress * 0.6)
                    image[py, px] = np.maximum(image[py, px], fade)
        return _to_rgba(np.clip(image, 0.0, 1.0))


class Fireflies:
    """A night forest with glowing specks drifting upward."""

    label = "Fireflies"

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        xs, ys = _grid(width, height)
        v = ys / height
        sky = np.stack([0.03 + 0.05 * v, 0.07 + 0.09 * v, 0.07 + 0.05 * v], axis=-1)
        rng = np.random.default_rng(13)
        trees = np.zeros((height, width), np.float32)
        for _ in range(9):
            cx, half = rng.uniform(0, width), rng.uniform(width * 0.01, width * 0.03)
            depth = rng.uniform(0.4, 1.0)
            trees = np.maximum(trees, (np.abs(xs - cx) < half) * depth)
        self.backdrop = sky * (1.0 - 0.6 * trees[..., None]) + 0.04
        count = 42
        self.x0 = rng.uniform(0.0, width, count)
        self.speed = rng.uniform(4.0, 12.0, count)
        self.offset = rng.uniform(0.0, height, count)
        self.phase = rng.uniform(0.0, TWO_PI, count)
        self.sprite = _soft_dot(max(2, height // 40))

    def frame(self, t: float) -> np.ndarray:
        image = self.backdrop.copy()
        for index in range(len(self.x0)):
            y = self.height - ((self.offset[index] + t * self.speed[index]) % (self.height + 20))
            x = self.x0[index] + np.sin(t * 0.7 + self.phase[index]) * 10.0
            pulse = max(0.0, np.sin(t * 1.6 + self.phase[index] * 3.0)) ** 2
            _stamp(image, self.sprite, x, y,
                   np.asarray((0.85, 1.0, 0.35), np.float32) * (0.2 + 0.8 * pulse))
        return _to_rgba(np.clip(image, 0.0, 1.0))


class PixelGrid:
    """A clean grid whose cells breathe along shifting gradient paths."""

    label = "Pixel grid"

    def __init__(self, width: int, height: int) -> None:
        xs, ys = _grid(width, height)
        cell = max(6, height // 14)
        self.cx, self.cy = np.floor(xs / cell), np.floor(ys / cell)
        local_x, local_y = xs % cell, ys % cell
        gap = max(1, cell // 7)
        self.mask = ((local_x >= gap) & (local_y >= gap)).astype(np.float32)
        rng = np.random.default_rng(17)
        rows, cols = int(self.cy.max()) + 1, int(self.cx.max()) + 1
        noise = rng.uniform(0.0, TWO_PI, (rows, cols))
        # Everything per cell, so a frame only works on the cell table.
        gy, gx = np.mgrid[0:rows, 0:cols].astype(np.float32)
        self.cell_phase = (gx * 0.45 + gy * 0.3) - noise * 0.35
        self.cell_hue = gx * 0.015 + gy * 0.02
        self.index = (self.cy.astype(np.int32), self.cx.astype(np.int32))

    def frame(self, t: float) -> np.ndarray:
        wave = 0.5 + 0.5 * np.sin(t * 1.4 - self.cell_phase)
        colour = _palette(self.cell_hue + t * 0.04, (0.55, 0.65, 0.85))
        cells = colour * (0.12 + 0.75 * wave ** 3)[..., None]
        image = cells[self.index] * self.mask[..., None] + 0.05
        return _to_rgba(np.clip(image, 0.0, 1.0))


class SineWaves:
    """Elegant lines flowing continuously across the screen."""

    label = "Sine waves"

    def __init__(self, width: int, height: int) -> None:
        self.x = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
        self.y = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
        self.scale = float(height) * 1.2
        self.backdrop = np.stack([0.04 + 0.04 * self.y, 0.05 + 0.03 * self.y,
                                  0.10 + 0.08 * self.y], axis=-1) * np.ones((1, width, 1), np.float32)

    def frame(self, t: float) -> np.ndarray:
        image = self.backdrop.copy()
        for index in range(8):
            # A line's height depends on x only: compute one row, broadcast.
            centre = 0.5 + (0.32 * np.sin(self.x * (3.0 + index * 0.4) * np.pi
                                          + t * (0.5 + index * 0.07) + index * 0.8)
                            * (0.4 + 0.6 * np.sin(t * 0.13 + index)))
            # Only the rows the line (and its glow) can reach.
            reach = 40.0 / self.scale
            top = max(0, int((float(centre.min()) - reach) * (len(self.y) - 1)))
            bottom = min(len(self.y), int((float(centre.max()) + reach) * (len(self.y) - 1)) + 2)
            distance = np.abs(self.y[top:bottom] - centre) * self.scale
            light = np.exp(-distance * distance * 0.5) + 0.18 * np.exp(-distance * 0.08)
            colour = _palette(index / 8.0 + t * 0.02, (0.5, 0.62, 0.85)).reshape(3)
            image[top:bottom] += light[..., None] * (colour * 0.55)
        return _to_rgba(np.clip(image, 0.0, 1.0))


class Wireframe:
    """A wireframe torus and cube turning slowly, in low contrast."""

    label = "Wireframe"

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        rings, sides = 18, 9
        theta = np.linspace(0.0, TWO_PI, rings, endpoint=False)
        phi = np.linspace(0.0, TWO_PI, sides, endpoint=False)
        tt, pp = np.meshgrid(theta, phi, indexing="ij")
        self.torus = np.stack([(1.0 + 0.38 * np.cos(pp)) * np.cos(tt),
                               (1.0 + 0.38 * np.cos(pp)) * np.sin(tt),
                               0.38 * np.sin(pp)], axis=-1).reshape(-1, 3)
        edges = []
        for i in range(rings):
            for j in range(sides):
                here = i * sides + j
                edges.append((here, ((i + 1) % rings) * sides + j))
                edges.append((here, i * sides + (j + 1) % sides))
        self.torus_edges = edges
        corners = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)],
                           np.float32) * 0.45
        self.cube = corners
        self.cube_edges = [(a, b) for a in range(8) for b in range(a + 1, 8)
                           if np.sum(np.abs(corners[a] - corners[b]) > 0) == 1]
        ys = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
        self.backdrop = ((np.asarray((0.07, 0.08, 0.12), np.float32) * (1 - ys)
                          + np.asarray((0.11, 0.10, 0.16), np.float32) * ys)
                         * np.ones((1, width, 1), np.float32))

    @staticmethod
    def _rotate(points, ax, ay):
        cx, sx, cy, sy = np.cos(ax), np.sin(ax), np.cos(ay), np.sin(ay)
        x, y, z = points[:, 0], points[:, 1], points[:, 2]
        y, z = y * cx - z * sx, y * sx + z * cx
        x, z = x * cy + z * sy, -x * sy + z * cy
        return np.stack([x, y, z], axis=-1)

    def _draw(self, draw, points, edges, centre, scale, colour):
        projected = points[:, :2] / (3.2 + points[:, 2:3]) * scale + centre
        for a, b in edges:
            draw.line([tuple(projected[a]), tuple(projected[b])], fill=colour, width=1)

    def frame(self, t: float) -> np.ndarray:
        from PIL import Image as PILImage, ImageDraw

        canvas = PILImage.new("RGB", (self.width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        scale = self.height * 0.85
        self._draw(draw, self._rotate(self.torus, t * 0.31, t * 0.23), self.torus_edges,
                   np.array([self.width * 0.36, self.height * 0.5]), scale, (110, 150, 210))
        self._draw(draw, self._rotate(self.cube, t * 0.4 + 1.0, t * 0.27), self.cube_edges,
                   np.array([self.width * 0.78, self.height * 0.45]), scale * 1.1, (200, 130, 220))
        lines = np.asarray(canvas, np.float32) / 255.0
        return _to_rgba(np.clip(self.backdrop + lines * 0.55, 0.0, 1.0))


class Halftone:
    """A risograph halftone whose dots swell and shrink in sweeping waves."""

    label = "Halftone"

    def __init__(self, width: int, height: int) -> None:
        xs, ys = _grid(width, height)
        cell = max(5, height // 22)
        self.x, self.y = xs / height, ys / height
        self.cell = cell
        # Distance to the nearest dot centre, for two offset ink layers.
        self.distance = []
        for offset in (0.0, 0.5):
            lx = (xs + offset * cell) % cell - cell / 2.0
            ly = (ys + offset * cell) % cell - cell / 2.0
            self.distance.append(np.sqrt(lx * lx + ly * ly) / cell)

    def frame(self, t: float) -> np.ndarray:
        paper = np.asarray((0.96, 0.92, 0.84), np.float32)
        inks = (np.asarray((0.93, 0.25, 0.48), np.float32),
                np.asarray((0.15, 0.35, 0.85), np.float32))
        image = np.ones(self.x.shape + (3,), np.float32) * paper
        for index, (distance, ink) in enumerate(zip(self.distance, inks)):
            wave = 0.5 + 0.5 * np.sin(self.x * (5.0 + index) - t * (0.9 + 0.3 * index)
                                      + np.sin(self.y * 4.0 + t * 0.4 + index) * 1.5)
            dot = np.clip((wave * 0.62 - distance) * 8.0, 0.0, 1.0)[..., None]
            image = image * (1.0 - dot) + image * ink * dot     # multiply like ink
        return _to_rgba(image)


class VHS:
    """TV static with scanlines, a rolling bright band and small glitches."""

    label = "VHS static"

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        xs, ys = _grid(width, height)
        self.v = ys / height
        self.scan = (0.82 + 0.18 * (np.arange(height) % 2 == 0)).astype(np.float32)[:, None, None]
        self.base = np.stack([0.10 + 0.05 * self.v, 0.16 + 0.06 * self.v,
                              0.20 + 0.04 * self.v], axis=-1)
        self.rng = np.random.default_rng(9)

    def frame(self, t: float) -> np.ndarray:
        noise = self.rng.random((self.height, self.width), dtype=np.float32)
        band_centre = (t * 0.18) % 1.3 - 0.15
        band = np.exp(-((self.v - band_centre) / 0.05) ** 2)[..., None]
        image = self.base + (noise[..., None] - 0.5) * 0.22 + band * 0.18
        image = image * self.scan
        if (t * 7.0) % 5.0 < 0.6:                 # a short horizontal glitch
            rows = slice(int(self.height * ((t * 0.37) % 0.8)),
                         int(self.height * ((t * 0.37) % 0.8)) + max(2, self.height // 12))
            image[rows] = np.roll(image[rows], int(6 + 10 * np.sin(t * 9.0)), axis=1)
        image[..., 0] = np.roll(image[..., 0], 1, axis=1)   # slight chroma offset
        return _to_rgba(np.clip(image, 0.0, 1.0))


class DigitalRain:
    """Vertical trails of falling glyph light on a dark background."""

    label = "Digital rain"

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.glyph_w = max(3, width // 64)
        self.glyph_h = max(4, int(self.glyph_w * 1.4))
        self.columns = width // self.glyph_w + 1
        self.rows = height // self.glyph_h + 1
        rng = np.random.default_rng(23)
        self.speed = rng.uniform(0.35, 1.0, self.columns).astype(np.float32)
        self.offset = rng.uniform(0.0, 3.0, self.columns).astype(np.float32)
        self.length = rng.uniform(0.25, 0.6, self.columns).astype(np.float32)
        self.glyphs = rng.random((self.rows, self.columns)) > 0.35
        self.rng = rng
        xs, ys = _grid(width, height)
        self.col = (xs // self.glyph_w).astype(np.int32)
        self.row = (ys // self.glyph_h).astype(np.int32)
        inner_x, inner_y = xs % self.glyph_w, ys % self.glyph_h
        self.pixel = ((inner_x > 0) & (inner_y > 0)).astype(np.float32)
        self.v = (self.row / self.rows).astype(np.float32)

    def frame(self, t: float) -> np.ndarray:
        flips = self.rng.random(self.glyphs.shape) < 0.02
        self.glyphs ^= flips
        head = (t * self.speed * 0.45 + self.offset) % 1.6 - 0.3
        behind = head[self.col] - self.v
        trail = np.where(behind >= 0.0, np.exp(-behind / self.length[self.col] * 3.0), 0.0)
        lit = trail * self.glyphs[self.row, self.col] * self.pixel
        tip = np.clip(1.0 - np.abs(behind) * 40.0, 0.0, 1.0) * self.pixel
        green = np.asarray((0.15, 0.95, 0.45), np.float32)
        image = lit[..., None] * green + tip[..., None] * 0.8 + np.asarray((0.02, 0.06, 0.04), np.float32)
        return _to_rgba(np.clip(image, 0.0, 1.0))


GENERATORS = {
    "tunnel": Tunnel,
    "starfield": Starfield,
    "plasma": Plasma,
    "aurora": Aurora,
    "ripples": Ripples,
    "mesh": MeshGlow,
    "chrome": LiquidChrome,
    "thermal": Thermal,
    "bokeh": BokehDrift,
    "cosmos": Cosmos,
    "fireflies": Fireflies,
    "pixelgrid": PixelGrid,
    "sinewaves": SineWaves,
    "wireframe": Wireframe,
    "halftone": Halftone,
    "vhs": VHS,
    "rain": DigitalRain,
}
# Names saved by earlier builds, which drew test patterns with a distortion.
ALIASES = {"orbit": "starfield", "pulse": "ripples", "warp": "plasma"}


def generator_key(name: str) -> str:
    name = str(name or "").strip().lower()
    name = ALIASES.get(name, name)
    return name if name in GENERATORS else "tunnel"


class ProceduralSource:
    """An ``appsrc`` that is fed frames from a generator on its own thread."""

    def __init__(self, name: str, element_name: str, width: int, height: int,
                 fps: int, generator=None, size: tuple[int, int] | None = None) -> None:
        if generator is not None:
            # A prebuilt generator (ticker, lower third, GIF) at its own size;
            # it may return frames with an alpha channel.
            self.key = name
            self.width, self.height = size
            self.generator = generator
        else:
            self.key = generator_key(name)
            self.width, self.height = internal_size(width, height)
            self.generator = GENERATORS[self.key](self.width, self.height)
        self.fps = max(1, min(30, int(fps)))
        appsrc = Gst.ElementFactory.make("appsrc", element_name)
        if appsrc is None:
            raise RuntimeError("GStreamer element 'appsrc' is not installed")
        appsrc.set_property("caps", Gst.Caps.from_string(
            f"video/x-raw,format=RGBA,width={self.width},height={self.height},"
            f"framerate={self.fps}/1"))
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("is-live", True)
        appsrc.set_property("do-timestamp", True)
        appsrc.set_property("block", False)
        appsrc.set_property("max-buffers", 2)
        appsrc.set_property("leaky-type", 2)
        self.appsrc = appsrc
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"anycam-backdrop-{self.key}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        started = time.monotonic()
        interval = 1.0 / self.fps
        next_frame = started
        duration = int(Gst.SECOND / self.fps)
        last: bytes | None = None
        failures = 0
        while not self._stop.is_set():
            next_frame += interval
            try:
                playing = self.appsrc.get_state(0)[1] == Gst.State.PLAYING
            except Exception:  # noqa: BLE001 - the element is going away
                return
            if playing:
                try:
                    last = self.generator.frame(time.monotonic() - started).tobytes()
                except Exception as exc:  # noqa: BLE001 - keep the picture alive
                    # An uncaught error used to end this thread: the stage or
                    # animation froze and nothing said why.  Log it and keep
                    # showing the last good frame.
                    failures += 1
                    if failures == 1 or failures % 300 == 0:
                        print(f"[procedural] {self.key} frame failed ({failures}x): {exc!r}",
                              file=sys.stderr, flush=True)
                if last is not None:
                    buffer = Gst.Buffer.new_wrapped(last)
                    buffer.duration = duration
                    self.appsrc.emit("push-buffer", buffer)
            delay = next_frame - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_frame = time.monotonic()  # fell behind: do not burst

    def stop(self) -> None:
        """Stop feeding frames and finish the stream.

        Stopping the thread alone leaves the compositor waiting for this input
        forever when the layer is removed from a running canvas, which froze
        the whole picture.  End-of-stream tells it this pad is done.
        """
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)
        try:
            self.appsrc.emit("end-of-stream")
        except Exception:  # noqa: BLE001 - the element may already be gone
            pass
