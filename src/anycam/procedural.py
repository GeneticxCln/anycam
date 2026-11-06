"""Procedural animated backdrops.

Each generator draws one frame as a function of time with numpy, at a low
internal resolution (about 320 px wide) that the chain scales up.  That keeps a
moving backdrop cheap (a few milliseconds per frame) and gives real motion and
real perspective, unlike a test pattern run through a distortion filter.

``ProceduralSource`` feeds a generator into the canvas through an ``appsrc``.
"""
from __future__ import annotations

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


GENERATORS = {
    "tunnel": Tunnel,
    "starfield": Starfield,
    "plasma": Plasma,
    "aurora": Aurora,
    "ripples": Ripples,
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
        while not self._stop.is_set():
            next_frame += interval
            try:
                playing = self.appsrc.get_state(0)[1] == Gst.State.PLAYING
            except Exception:  # noqa: BLE001 - the element is going away
                return
            if playing:
                frame = self.generator.frame(time.monotonic() - started)
                buffer = Gst.Buffer.new_wrapped(frame.tobytes())
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
