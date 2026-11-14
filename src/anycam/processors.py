"""Per-frame processors: portrait, face and framing effects.

GStreamer's stock elements cannot tell a person from a background or find a
face, so these effects run Python on each frame.  A ``Processor`` takes and
returns a PIL RGBA picture; ``build_processor_bin`` wraps one in a small bin
(``appsink`` -> worker thread -> ``appsrc``) that sits in a layer's filter slot
and is swapped in live like any other filter.

Heavy frames are handled at reduced size where it does not show (blur, the
mask) and the worker drops frames rather than stall the canvas when it falls
behind.  The slow, cached work (face detection, person segmentation, the
blurred background) runs on a :class:`SlowJob` thread beside the frame
thread: onnxruntime and Pillow release the GIL, so it overlaps for real and a
frame never waits for a 20 ms detection.
"""
from __future__ import annotations

import concurrent.futures
import math
import threading
import time

import numpy as np

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps  # noqa: E402

from . import face as face_module  # noqa: E402
from . import emoji as emoji_module  # noqa: E402
from . import masks, procedural, segment  # noqa: E402

Gst.init(None)


# --------------------------------------------------------------- slow jobs
class SlowJob:
    """One slow job at a time on a worker thread; the frame thread polls it.

    ``submit`` returns False while a job is still running, so a slow machine
    simply refreshes less often instead of queueing work.  The owner keeps
    using its newest finished result until the next one arrives.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._future: concurrent.futures.Future | None = None

    @property
    def busy(self) -> bool:
        return self._future is not None and not self._future.done()

    def submit(self, function, *args) -> bool:
        if self._future is not None:
            return False
        if self._pool is None:
            self._pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"anycam-{self.name}")
        self._future = self._pool.submit(function, *args)
        return True

    def poll(self):
        """The finished job's result, or None (still running, none, failed)."""
        future = self._future
        if future is None or not future.done():
            return None
        self._future = None
        try:
            return future.result()
        except Exception:  # noqa: BLE001 - a failed refresh keeps the old result
            return None

    def wait(self, timeout: float = 2.0):
        """Block for the running job (tests and shutdown)."""
        future = self._future
        if future is None:
            return None
        concurrent.futures.wait([future], timeout=timeout)
        return self.poll()

    def close(self) -> None:
        pool, self._pool, self._future = self._pool, None, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def _reduced(image: Image.Image, longest: int) -> Image.Image:
    """A cheap box-filtered copy whose longer side is about ``longest``.

    ``Image.reduce`` is an integer box filter, several times cheaper than a
    bilinear resize of the full frame, and it gives the worker thread its own
    picture so the frame thread may keep drawing on the original.
    """
    factor = max(1, math.ceil(max(image.size) / float(longest)))
    return image.reduce(factor) if factor > 1 else image.copy()


# ------------------------------------------------------------------ tracking
class TrackedFace:
    """A face smoothed over time: box and landmarks in pixels."""

    __slots__ = ("box", "points", "seen")

    def __init__(self, box, points, seen):
        self.box, self.points, self.seen = box, points, seen

    @property
    def centre(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return x + w / 2.0, y + h / 2.0


class FaceTrack:
    """Detects faces a few times a second and smooths them between frames.

    Detection (YuNet, ~20 ms at 1080p) runs on a :class:`SlowJob`; a frame
    only merges the newest finished result.  The first detection, and one
    after the results went stale (a paused source), run immediately so a
    single frame (a thumbnail) or a jump still gets the face.
    """

    DETECT_SIZE = 384

    def __init__(self, interval: float = 0.1, hold: float = 0.7,
                 smoothing: float = 0.55, asynchronous: bool = True) -> None:
        self.interval, self.hold, self.smoothing = interval, hold, smoothing
        self.faces: list[TrackedFace] = []
        self._last = -1e9
        self._result_at = -1e9
        self._job = SlowJob("faces") if asynchronous else None

    def update(self, image: Image.Image, now: float) -> list[TrackedFace]:
        if self._job is not None:
            done = self._job.poll()
            if done is not None:
                self._merge(*done)
        if now - self._last >= self.interval:
            stale = now - self._result_at > self.hold
            if self._job is None or stale:
                self._last = now
                self._detect(image, now)
            elif not self._job.busy:
                sample, size = _reduced(image, self.DETECT_SIZE), image.size
                if self._job.submit(lambda: (self._find(sample), size, now)):
                    self._last = now
        self.faces = [f for f in self.faces if now - f.seen <= self.hold]
        return self.faces

    def close(self) -> None:
        if self._job is not None:
            self._job.close()

    def _detect(self, image: Image.Image, now: float) -> None:
        """Detect on this frame, now (blocking)."""
        self._merge(self._find(_reduced(image, self.DETECT_SIZE)), image.size, now)

    @staticmethod
    def _find(sample: Image.Image) -> list | None:
        detector = face_module.shared_detector()
        if detector is None:
            return None
        return detector.detect(np.asarray(sample.convert("RGB")),
                               score_threshold=0.6, top_k=4)

    def _merge(self, found, size: tuple[int, int], now: float) -> None:
        if found is None:
            return
        self._result_at = max(self._result_at, now)
        width, height = size
        previous = list(self.faces)
        updated: list[TrackedFace] = []
        for item in found:
            box = (item.x * width, item.y * height, item.w * width, item.h * height)
            points = tuple((lx * width, ly * height)
                           for lx, ly in (item.landmarks or ()))
            match = min(previous, key=lambda f: _distance(f.centre, (
                box[0] + box[2] / 2, box[1] + box[3] / 2)), default=None)
            if match is not None and _distance(match.centre, (
                    box[0] + box[2] / 2, box[1] + box[3] / 2)) < max(box[2], box[3]):
                previous.remove(match)
                a = self.smoothing
                box = tuple(m * (1 - a) + n * a for m, n in zip(match.box, box, strict=True))
                if match.points and points:
                    points = tuple((mx * (1 - a) + nx * a, my * (1 - a) + ny * a)
                                   for (mx, my), (nx, ny) in zip(match.points, points, strict=True))
            updated.append(TrackedFace(box, points, now))
        # A face that missed one detection is kept (``hold``) instead of flickering.
        updated.extend(f for f in previous if now - f.seen <= self.hold)
        self.faces = updated


def _distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class PersonMask:
    """A soft person mask at frame size, smoothed over time.

    Uses the segmentation model; without one it falls back to an ellipse built
    from the tracked faces (head and shoulders), and to "everyone" when no
    face is found either, so an effect never blanks the picture.

    With the model, the refresh (segmentation, smoothing and the full-size
    mask, ~18 ms at 1080p) runs on a :class:`SlowJob`; frames reuse the newest
    mask.  The first mask, and one after a pause, are computed immediately.
    """

    STALE_S = 0.7

    def __init__(self, interval: float = 0.07, asynchronous: bool = True) -> None:
        self.interval = interval
        self.faces = FaceTrack()
        self._last = -1e9
        self._small: np.ndarray | None = None
        self._full: Image.Image | None = None
        self._result_at = -1e9
        self._job = SlowJob("segment") if asynchronous else None
        self._segmenter: segment.Segmenter | None = None
        self._generation = -1

    def _seg(self) -> segment.Segmenter:
        """This mask's segmenter, rebuilt when the model or device changes
        (RVM is recurrent: each mask keeps its own state)."""
        if self._segmenter is None or self._generation != segment.generation():
            self._generation = segment.generation()
            self._segmenter = segment.segmenter()
        return self._segmenter

    def compute(self, image: Image.Image, now: float) -> Image.Image:
        """The mask at frame size, refreshed ~14 times a second."""
        size = image.size
        if self._job is not None:
            done = self._job.poll()
            if done is not None:
                small, full, stamp = done
                self._small, self._result_at = small, stamp
                self._full = full if full.size == size else None
        if now - self._last >= self.interval or self._small is None:
            background = (self._job is not None and segment.available()
                          and self._small is not None
                          and now - self._result_at <= self.STALE_S)
            if not background:
                self._last = now
                self._refresh(image, now)
                self._result_at = now
                self._full = None
            elif not self._job.busy:
                seg = self._seg()
                sample = _reduced(image, seg.sample_longest)
                previous = self._small
                if self._job.submit(self._work, seg, sample, previous, size, now):
                    self._last = now
        if self._full is None or self._full.size != size:
            self._full = self._full_size(self._small, size)
        return self._full

    def close(self) -> None:
        self.faces.close()
        if self._job is not None:
            self._job.close()

    @staticmethod
    def _full_size(small: np.ndarray, size: tuple[int, int]) -> Image.Image:
        mask = Image.fromarray((small * 255.0).astype(np.uint8), "L")
        return mask.resize(size, Image.BILINEAR)

    @staticmethod
    def _smoothed(alpha: np.ndarray, previous: np.ndarray | None,
                  sharpen: bool = True) -> np.ndarray:
        if sharpen:
            # A probability (MediaPipe): sharpen the soft edge a little, then
            # smooth over time.
            alpha = np.clip((alpha - 0.3) / 0.4, 0.0, 1.0)
            weight = 0.55
        else:
            # A real matte (RVM) is already stable between frames.
            weight = 0.75
        if previous is None or previous.shape != alpha.shape:
            return alpha
        return weight * alpha + (1.0 - weight) * previous

    def _work(self, seg: segment.Segmenter, sample: Image.Image, previous, size, stamp):
        alpha = seg.alpha(sample)
        if alpha is None:
            raise RuntimeError("segmentation failed")
        small = self._smoothed(alpha, previous, seg.sharpen)
        return small, self._full_size(small, size), stamp

    def _refresh(self, image: Image.Image, now: float) -> None:
        alpha = None
        seg = None
        if segment.available():
            seg = self._seg()
            alpha = seg.alpha(_reduced(image, seg.sample_longest))
        if alpha is None:
            alpha = self._from_faces(image, now)
            same = self._small is not None and self._small.shape == alpha.shape
            self._small = 0.55 * alpha + 0.45 * self._small if same else alpha
        else:
            self._small = self._smoothed(alpha, self._small, seg.sharpen)

    def _from_faces(self, image: Image.Image, now: float) -> np.ndarray:
        faces = self.faces.update(image, now)
        width, height = segment.INPUT_SIZE
        if not faces:
            return np.ones((height, width), dtype=np.float32)
        scale_x, scale_y = width / image.width, height / image.height
        canvas = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(canvas)
        for tracked in faces:
            x, y, w, h = (tracked.box[0] * scale_x, tracked.box[1] * scale_y,
                          tracked.box[2] * scale_x, tracked.box[3] * scale_y)
            cx = x + w / 2
            draw.ellipse([cx - w * 0.85, y - h * 0.35, cx + w * 0.85, y + h * 1.2], fill=255)
            draw.ellipse([cx - w * 2.0, y + h * 0.95, cx + w * 2.0, y + h * 4.2], fill=255)
        canvas = canvas.filter(ImageFilter.GaussianBlur(max(1.0, width / 64)))
        return np.asarray(canvas, dtype=np.float32) / 255.0


# --------------------------------------------------------------- base class
class Processor:
    """Takes an RGBA picture, returns an RGBA picture of the same size."""

    def __init__(self, amount: float = 0.5) -> None:
        self.amount = float(amount)

    def set_amount(self, amount: float) -> None:
        self.amount = max(0.0, min(1.0, float(amount)))

    def process(self, image: Image.Image, now: float) -> Image.Image:
        return image

    def close(self) -> None:
        # Stop the worker threads of the shared helpers (face tracking, mask).
        for name in ("faces", "mask"):
            part = getattr(self, name, None)
            if part is not None and hasattr(part, "close"):
                part.close()


# ------------------------------------------------------------------ portrait
def _blurred(image: Image.Image, radius: float) -> Image.Image:
    """Blur cheaply: shrink, blur the small picture, enlarge."""
    width, height = image.size
    factor = max(1, int(width // 360))
    small = image.reduce(factor) if factor > 1 else image
    small = small.filter(ImageFilter.GaussianBlur(max(0.5, radius / factor)))
    return small.resize((width, height), Image.BILINEAR) if factor > 1 else small


class Background(Processor):
    """Keep the person, change everything behind them.

    Per frame this is one RGBA composite (~5 ms at 1080p).  The person mask,
    the shaded (blurred, dimmed, grey) copy and animated backdrops are made on
    :class:`SlowJob` threads and reused until the next one is ready; the shade
    is computed on the reduced picture and enlarged once.
    """

    def __init__(self, mode: str, amount: float = 0.5, backdrop: str = "",
                 asynchronous: bool = True) -> None:
        super().__init__(amount)
        self.mode, self.backdrop = mode, backdrop
        self.mask = PersonMask(asynchronous=asynchronous)
        self._generator = None
        self._cache: tuple | None = None      # (size, RGBA picture, time) of the backdrop
        self._shaded: tuple | None = None     # (size, RGBA picture, time) of the shaded copy
        self._shade_job = SlowJob("shade") if asynchronous else None
        self._backdrop_job = SlowJob("backdrop") if asynchronous else None

    BACKDROP_FPS = 15.0   # the animated backdrops need no more than this
    SHADE_FPS = 12.0      # a blurred background changes too slowly to need more
    STALE_S = 0.7         # older than this (a paused source): redo it now

    def _render_backdrop(self, size, now):
        frame = Image.fromarray(self._generator.frame(now), "RGBA")
        return size, frame.resize(size, Image.BILINEAR), now

    def _backdrop_picture(self, size, now) -> Image.Image:
        """The replacement backdrop as an RGBA picture of ``size``."""
        width, height = size
        if self.backdrop in procedural.GENERATORS:
            if self._generator is None:
                inner = procedural.internal_size(width, height)
                self._generator = procedural.GENERATORS[self.backdrop](*inner)
            job = self._backdrop_job
            if job is not None:
                done = job.poll()
                if done is not None and done[0] == size:
                    self._cache = done
            due = (self._cache is None or self._cache[0] != size
                   or now - self._cache[2] >= 1.0 / self.BACKDROP_FPS)
            if due:
                if (job is None or self._cache is None or self._cache[0] != size
                        or now - self._cache[2] > self.STALE_S):
                    self._cache = self._render_backdrop(size, now)
                elif not job.busy:
                    job.submit(self._render_backdrop, size, now)
            return self._cache[1]
        if self._cache is None or self._cache[0] != size:
            from . import overlay

            path = overlay.render_background(self.backdrop or "Studio", width, height)
            with Image.open(path) as opened:
                picture = opened.convert("RGBA")
            # render_background never makes a picture under 64 px; the
            # composite must still come out at the frame's size.
            if picture.size != size:
                picture = picture.resize(size, Image.BILINEAR)
            self._cache = (size, picture, now)
        return self._cache[1]

    def process(self, image: Image.Image, now: float) -> Image.Image:
        rgba = image if image.mode == "RGBA" else image.convert("RGBA")
        mask = self.mask.compute(rgba, now)
        if self.mode == "replace":
            background = self._backdrop_picture(rgba.size, now)
        else:
            background = self._shaded_picture(rgba, now)
        # Straight in RGBA: converting to RGB and back cost ~4 ms a frame.
        return Image.composite(rgba, background, mask)

    def _shaded_picture(self, image: Image.Image, now: float) -> Image.Image:
        size = image.size
        job = self._shade_job
        if job is not None:
            done = job.poll()
            if done is not None and done[0] == size:
                self._shaded = done
        stale = (self._shaded is None or self._shaded[0] != size
                 or now - self._shaded[2] >= 1.0 / self.SHADE_FPS)
        if stale:
            if (job is None or self._shaded is None or self._shaded[0] != size
                    or now - self._shaded[2] > self.STALE_S):
                self._shaded = self._shade(self._shade_sample(image), size, now)
            elif not job.busy:
                job.submit(self._shade, self._shade_sample(image), size, now)
        return self._shaded[1]

    @staticmethod
    def _shade_sample(image: Image.Image) -> Image.Image:
        factor = max(1, int(image.width // 360))
        return image.reduce(factor) if factor > 1 else image.copy()

    def _shade(self, small: Image.Image, size, stamp):
        """Blur/dim/grey the reduced picture, then enlarge it once."""
        factor = max(1.0, size[0] / max(1, small.width))
        rgb = small.convert("RGB")

        def blur(radius: float) -> Image.Image:
            return rgb.filter(ImageFilter.GaussianBlur(max(0.5, radius / factor)))

        if self.mode == "blur":
            out = blur(3.0 + 28.0 * self.amount)
        elif self.mode == "dim":
            out = ImageEnhance.Brightness(blur(2.0)).enhance(max(0.05, 1.0 - 0.9 * self.amount))
        else:
            gray = ImageOps.grayscale(blur(3.0)).convert("RGB")
            out = ImageEnhance.Brightness(gray).enhance(1.0 - 0.45 * self.amount)
        return size, out.resize(size, Image.BILINEAR).convert("RGBA"), stamp

    def set_amount(self, amount: float) -> None:
        super().set_amount(amount)
        self._shaded = None      # the strength changed: shade again next frame

    def close(self) -> None:
        super().close()
        for job in (self._shade_job, self._backdrop_job):
            if job is not None:
                job.close()


class Spotlight(Processor):
    """Darken everything except the person and a soft pool of light on them."""

    def __init__(self, amount: float = 0.6, asynchronous: bool = True) -> None:
        super().__init__(amount)
        self.mask = PersonMask(asynchronous=asynchronous)
        self._table: tuple[float, list[int]] | None = None

    def process(self, image: Image.Image, now: float) -> Image.Image:
        rgba = image if image.mode == "RGBA" else image.convert("RGBA")
        mask = self.mask.compute(rgba, now)
        factor = max(0.04, 0.85 - 0.8 * self.amount)
        if self._table is None or self._table[0] != factor:
            scaled = [min(255, int(i * factor)) for i in range(256)]
            # Darken colour, keep alpha: a lookup is ~3x cheaper than an
            # ImageEnhance pass on the full frame.
            self._table = (factor, scaled * 3 + list(range(256)))
        dark = rgba.point(self._table[1])
        return Image.composite(rgba, dark, mask)


# --------------------------------------------------------------- face drawing
_SS = 3  # supersampling for sprites


def _sprite(size, painter) -> Image.Image:
    """Draw ``painter(draw, w, h)`` supersampled so edges are smooth."""
    width, height = size
    canvas = Image.new("RGBA", (width * _SS, height * _SS), (0, 0, 0, 0))
    painter(ImageDraw.Draw(canvas), width * _SS, height * _SS)
    return canvas.resize(size, Image.LANCZOS)


def _paint_sunglasses(d, w, h):
    lens = (22, 22, 28, 255)
    for x0, x1 in ((0.04, 0.47), (0.53, 0.96)):
        d.rounded_rectangle([w * x0, h * 0.18, w * x1, h * 0.88], radius=h * 0.32, fill=lens)
        d.ellipse([w * (x0 + 0.05), h * 0.26, w * (x0 + 0.17), h * 0.42], fill=(255, 255, 255, 90))
    d.rectangle([w * 0.46, h * 0.28, w * 0.54, h * 0.40], fill=lens)
    d.line([w * 0.04, h * 0.30, 0, h * 0.22], fill=lens, width=int(h * 0.1))
    d.line([w * 0.96, h * 0.30, w, h * 0.22], fill=lens, width=int(h * 0.1))


def _paint_party_hat(d, w, h):
    d.polygon([(w * 0.5, 0.05 * h), (w * 0.04, h * 0.94), (w * 0.96, h * 0.94)],
              fill=(236, 72, 153, 255))
    for index, colour in enumerate(((250, 204, 21, 255), (56, 189, 248, 255))):
        top = 0.28 + 0.26 * index
        d.polygon([(w * (0.5 - 0.17 * (top + 0.1)), h * (top + 0.14)),
                   (w * (0.5 + 0.17 * (top + 0.1)), h * (top + 0.14)),
                   (w * (0.5 + 0.22 * (top + 0.16)), h * (top + 0.2)),
                   (w * (0.5 - 0.22 * (top + 0.16)), h * (top + 0.2))], fill=colour)
    d.ellipse([w * 0.43, 0, w * 0.57, h * 0.13], fill=(255, 255, 255, 255))
    d.rectangle([w * 0.04, h * 0.9, w * 0.96, h], fill=(250, 204, 21, 255))


def _paint_crown(d, w, h):
    gold, dark = (252, 196, 25, 255), (180, 120, 10, 255)
    d.polygon([(w * 0.04, h * 0.95), (w * 0.04, h * 0.25), (w * 0.27, h * 0.6),
               (w * 0.5, h * 0.05), (w * 0.73, h * 0.6), (w * 0.96, h * 0.25),
               (w * 0.96, h * 0.95)], fill=gold, outline=dark, width=int(h * 0.03))
    d.rectangle([w * 0.04, h * 0.78, w * 0.96, h * 0.95], fill=dark)
    for cx, colour in ((0.2, (220, 38, 38, 255)), (0.5, (37, 99, 235, 255)), (0.8, (22, 163, 74, 255))):
        d.ellipse([w * (cx - 0.05), h * 0.68, w * (cx + 0.05), h * 0.9], fill=colour)


def _paint_mustache(d, w, h):
    ink = (36, 24, 20, 255)
    top = [(0.5, 0.42), (0.40, 0.30), (0.28, 0.26), (0.16, 0.34), (0.06, 0.52),
           (0.02, 0.74), (0.10, 0.62), (0.2, 0.58), (0.32, 0.64), (0.44, 0.72), (0.5, 0.66)]
    d.polygon([(w * x, h * y) for x, y in top], fill=ink)
    d.polygon([(w * (1 - x), h * y) for x, y in top], fill=ink)


def _paint_clown_nose(d, w, h):
    d.ellipse([0, 0, w - 1, h - 1], fill=(220, 38, 38, 255))
    d.ellipse([w * 0.2, h * 0.14, w * 0.46, h * 0.4], fill=(255, 255, 255, 140))


def _paint_cat_ears(d, w, h):
    for x0, x1 in ((0.04, 0.38), (0.62, 0.96)):
        tip = (x0 + x1) / 2 + (0.06 if x0 < 0.5 else -0.06)
        d.polygon([(w * x0, h * 0.95), (w * tip, h * 0.02), (w * x1, h * 0.95)],
                  fill=(60, 52, 58, 255))
        d.polygon([(w * (x0 + 0.06), h * 0.9), (w * tip, h * 0.24), (w * (x1 - 0.06), h * 0.9)],
                  fill=(244, 164, 190, 255))


def _paint_cat_nose(d, w, h):
    d.polygon([(w * 0.1, h * 0.15), (w * 0.9, h * 0.15), (w * 0.5, h * 0.9)],
              fill=(244, 114, 150, 255))


def _paint_whiskers(d, w, h):
    ink = (245, 245, 245, 235)
    for side in (0, 1):
        for index, dy in enumerate((-0.28, 0.0, 0.28)):
            x0, x1 = (0.02, 0.36) if side == 0 else (0.98, 0.64)
            d.line([w * x0, h * (0.5 + dy * 0.9), w * x1, h * (0.5 + dy * 0.2)],
                   fill=ink, width=max(2, int(h * 0.05)))


def _paint_halo(d, w, h):
    d.ellipse([w * 0.03, h * 0.1, w * 0.97, h * 0.9], outline=(253, 224, 71, 255),
              width=int(h * 0.2))
    d.ellipse([w * 0.03, h * 0.1, w * 0.97, h * 0.9], outline=(255, 247, 190, 120),
              width=int(h * 0.06))


_SPRITES = {
    "sunglasses": ((500, 190), _paint_sunglasses),
    "party-hat": ((300, 380), _paint_party_hat),
    "crown": ((400, 250), _paint_crown),
    "mustache": ((420, 150), _paint_mustache),
    "clown-nose": ((160, 160), _paint_clown_nose),
    "cat-ears": ((420, 200), _paint_cat_ears),
    "cat-nose": ((120, 90), _paint_cat_nose),
    "whiskers": ((560, 130), _paint_whiskers),
    "halo": ((420, 110), _paint_halo),
}
_sprite_cache: dict[str, Image.Image] = {}


def sprite(name: str) -> Image.Image:
    if name not in _sprite_cache:
        size, painter = _SPRITES[name]
        _sprite_cache[name] = _sprite(size, painter)
    return _sprite_cache[name]


# Resized + rotated sprites, keyed by (picture, size, whole degrees).  A head
# turns slowly, so most frames reuse one; rotating a sprite every frame was
# ~6 ms each (three per face for the cat).
_PLACED_LIMIT = 160
_placed: dict[tuple, Image.Image] = {}
_placed_lock = threading.Lock()


def _placed_picture(key: str, base: Image.Image, width: int, height: int,
                    angle: float) -> Image.Image:
    width, height = max(4, width // 2 * 2), max(4, height // 2 * 2)
    degrees = int(round(angle)) if abs(angle) > 0.5 else 0
    cache_key = (key, width, height, degrees)
    with _placed_lock:
        picture = _placed.pop(cache_key, None)
        if picture is not None:
            _placed[cache_key] = picture          # most recent last
            return picture
    picture = base.resize((width, height), Image.BILINEAR)
    if degrees:
        picture = picture.rotate(degrees, resample=Image.BICUBIC, expand=True)
    with _placed_lock:
        _placed[cache_key] = picture
        while len(_placed) > _PLACED_LIMIT:
            _placed.pop(next(iter(_placed)))
    return picture


def _emoji_at(canvas: Image.Image, code: str, centre, size: float, angle: float) -> None:
    pic = emoji_module.load(code)
    size = int(size)
    if pic is None or size < 4:
        return
    pic = _placed_picture(f"emoji:{code}", pic, size, size, angle)
    canvas.alpha_composite(pic, (int(centre[0] - pic.width / 2), int(centre[1] - pic.height / 2)))


def _stamp(canvas: Image.Image, name: str, centre, width: float, angle: float) -> None:
    """Paste sprite ``name`` centred at ``centre``, ``width`` px wide, rotated."""
    base = sprite(name)
    width = max(4, int(width))
    height = max(4, int(width * base.height / base.width))
    picture = _placed_picture(f"sprite:{name}", base, width, height, angle)
    canvas.alpha_composite(
        picture, (int(centre[0] - picture.width / 2), int(centre[1] - picture.height / 2)))


class FaceSticker(Processor):
    """Props that follow the face: glasses, hats, mustaches and so on."""

    def __init__(self, kind: str, amount: float = 0.5) -> None:
        super().__init__(amount)
        self.kind = kind
        self.faces = FaceTrack()

    EYE_EMOJI = {"heart-eyes": "2764", "star-eyes": "2b50", "fire-eyes": "1f525",
                 "sparkle-eyes": "2728", "money-eyes": "1f4b0", "dizzy-eyes": "1f4ab"}
    ORBITS = {"orbit-hearts": ("2764", "1f496", "1f495"), "orbit-stars": ("2b50", "1f31f", "2728"),
              "orbit-butterflies": ("1f98b",), "orbit-flowers": ("1f338", "1f33c", "1f33a"),
              "orbit-snow": ("2744",), "orbit-music": ("1f3b5", "1f3b6"),
              "orbit-party": ("1f389", "1f388", "1f381", "1f38a"),
              "orbit-bats": ("1f987",)}

    def process(self, image: Image.Image, now: float) -> Image.Image:
        self._now = now
        faces = self.faces.update(image, now)
        if not faces:
            return image
        size = 0.7 + 0.7 * self.amount
        out = image.convert("RGBA") if image.mode != "RGBA" else image.copy()
        for tracked in faces:
            self._draw(out, tracked, size)
        return out

    def _draw(self, out: Image.Image, tracked: TrackedFace, size: float) -> None:
        x, y, w, h = tracked.box
        if len(tracked.points) < 5:
            return
        (lx, ly), (rx, ry), nose, mouth_a, mouth_b = tracked.points[:5]
        eyes = ((lx + rx) / 2, (ly + ry) / 2)
        eye_gap = _distance((lx, ly), (rx, ry))
        roll = -math.degrees(math.atan2(ry - ly, rx - lx))
        mouth = ((mouth_a[0] + mouth_b[0]) / 2, (mouth_a[1] + mouth_b[1]) / 2)
        mouth_width = _distance(mouth_a, mouth_b)
        up = (math.sin(math.radians(roll)) * -1.0, -math.cos(math.radians(roll)))

        def above(distance: float) -> tuple[float, float]:
            return (eyes[0] + up[0] * distance, eyes[1] + up[1] * distance)

        kind = self.kind
        if kind in self.EYE_EMOJI:
            pulse = 1.0 + 0.12 * math.sin(self._now * 6.0)
            for eye in ((lx, ly), (rx, ry)):
                _emoji_at(out, self.EYE_EMOJI[kind], eye, eye_gap * 0.95 * size * pulse, roll)
        elif kind in self.ORBITS:
            codes = self.ORBITS[kind]
            centre = (x + w / 2, y + h * 0.42)
            for index in range(7):
                angle = self._now * 1.3 + index * math.tau / 7
                px = centre[0] + math.cos(angle) * w * 0.82 * size
                py = centre[1] + math.sin(angle) * h * 0.34 * size - h * 0.42
                _emoji_at(out, codes[index % len(codes)], (px, py),
                          w * (0.26 + 0.08 * math.sin(angle)) * size, 0.0)
        elif kind == "sunglasses":
            _stamp(out, kind, eyes, eye_gap * 2.55 * size, roll)
        elif kind == "party-hat":
            width = w * 0.95 * size
            _stamp(out, kind, above(h * 0.55 + width * 0.62), width, roll)
        elif kind == "crown":
            width = w * 1.0 * size
            _stamp(out, kind, above(h * 0.5 + width * 0.3), width, roll)
        elif kind == "mustache":
            centre = (nose[0] * 0.55 + mouth[0] * 0.45, nose[1] * 0.58 + mouth[1] * 0.42)
            _stamp(out, kind, centre, max(mouth_width * 1.7, w * 0.45) * size, roll)
        elif kind == "clown-nose":
            _stamp(out, kind, nose, w * 0.26 * size, 0)
        elif kind == "cat":
            _stamp(out, "cat-ears", above(h * 0.55 + w * 0.12), w * 1.35 * size, roll)
            _stamp(out, "cat-nose", (nose[0], nose[1] - h * 0.02), w * 0.2 * size, roll)
            _stamp(out, "whiskers", (nose[0], nose[1] + h * 0.1), w * 1.9 * size, roll)
        elif kind == "halo":
            _stamp(out, kind, above(h * 0.6 + w * 0.12), w * 1.1 * size, roll)
        elif kind == "face-frame":
            self._frame(out, x, y, w, h, size)

    @staticmethod
    def _frame(out, x, y, w, h, size) -> None:
        draw = ImageDraw.Draw(out)
        pad = w * 0.18 * size
        left, top, right, bottom = x - pad, y - pad * 1.3, x + w + pad, y + h + pad * 0.6
        tick = max(10, w * 0.28)
        thick = max(2, int(w * 0.035))
        colour = (94, 234, 208, 255)
        for cx, cy, dx, dy in ((left, top, 1, 1), (right, top, -1, 1),
                               (left, bottom, 1, -1), (right, bottom, -1, -1)):
            draw.line([cx, cy, cx + dx * tick, cy], fill=colour, width=thick)
            draw.line([cx, cy, cx, cy + dy * tick], fill=colour, width=thick)


# --------------------------------------------------------------------- masks
class FaceMask(Processor):
    """A full-face mask or face paint that follows the head (see ``masks``)."""

    def __init__(self, key: str, amount: float = 0.85) -> None:
        super().__init__(amount)
        if key not in masks.MASKS:
            raise KeyError(key)
        self.key = key
        self.faces = FaceTrack(interval=0.07, smoothing=0.6)
        self._prerender = SlowJob("mask-anim")

    def process(self, image: Image.Image, now: float) -> Image.Image:
        upcoming = masks.next_template_time(self.key, now)
        if upcoming is not None and not self._prerender.busy:
            self._prerender.poll()
            self._prerender.submit(masks.template, self.key, upcoming)
        faces = self.faces.update(image, now)
        faces = [f for f in faces if len(f.points) >= 5]
        if not faces:
            return image
        out = image.convert("RGBA") if image.mode != "RGBA" else image.copy()
        opacity = 0.35 + 0.65 * self.amount
        for tracked in faces:
            left, right, _nose, mouth_a, mouth_b = tracked.points[:5]
            mouth = ((mouth_a[0] + mouth_b[0]) / 2, (mouth_a[1] + mouth_b[1]) / 2)
            masks.place(out, self.key, now, left, right, mouth, opacity)
        return out

    def close(self) -> None:
        super().close()
        self._prerender.close()


# ------------------------------------------------------------------- privacy
class FacePrivacy(Processor):
    """Pixelate or blur every face."""

    def __init__(self, mode: str, amount: float = 0.5) -> None:
        super().__init__(amount)
        self.mode = mode
        self.faces = FaceTrack(interval=0.07)

    def process(self, image: Image.Image, now: float) -> Image.Image:
        faces = self.faces.update(image, now)
        if not faces:
            return image
        out = image.copy()
        for tracked in faces:
            x, y, w, h = tracked.box
            pad = w * 0.2
            box = (max(0, int(x - pad)), max(0, int(y - pad * 1.2)),
                   min(image.width, int(x + w + pad)), min(image.height, int(y + h + pad)))
            if box[2] - box[0] < 4 or box[3] - box[1] < 4:
                continue
            region = out.crop(box)
            if self.mode == "pixelate":
                cells = max(3, int(14 - 10 * self.amount))
                small = region.resize((cells, max(2, int(cells * region.height / region.width))),
                                      Image.BILINEAR)
                region = small.resize(region.size, Image.NEAREST)
            else:
                region = region.filter(ImageFilter.GaussianBlur(region.width / 10 * (0.6 + self.amount)))
            out.paste(region, box)
        return out


# ------------------------------------------------------------------- touch-up
class TouchUp(Processor):
    """Practical picture improvements for a webcam, kept cheap.

    Each mode does the least work that gives the look: skin smoothing touches
    only the face crops, the glow layer is recomputed ~12 times a second, and
    there is deliberately no sharpen (Pillow's kernels cost 46 ms at 1080p).
    """

    GLOW_FPS = 12.0

    def __init__(self, mode: str, amount: float = 0.5) -> None:
        super().__init__(amount)
        self.mode = mode
        self.faces = FaceTrack(interval=0.1)
        self._lut_amount = None
        self._lut: list[int] | None = None
        self._glow: tuple | None = None          # (size, picture, time)
        self._feather: dict[tuple, Image.Image] = {}

    def process(self, image: Image.Image, now: float) -> Image.Image:
        if self.mode == "smooth":
            return self._smooth(image, now)
        rgb = image.convert("RGB")
        if self.mode == "glow":
            out = self._bloom(rgb, now)
        else:  # night: lift shadows with a gamma curve, keep highlights
            if self._lut is None or self._lut_amount != self.amount:
                gamma = 1.0 + 1.1 * self.amount
                table = [int(255 * ((i / 255.0) ** (1.0 / gamma))) for i in range(256)]
                self._lut, self._lut_amount = table * 3, self.amount
            out = rgb.point(self._lut)
        return out.convert("RGBA")

    def _bloom(self, rgb: Image.Image, now: float) -> Image.Image:
        """Add a blurred, dimmed copy with ``screen``; the copy is reused ~80 ms."""
        if (self._glow is None or self._glow[0] != rgb.size
                or now - self._glow[2] >= 1.0 / self.GLOW_FPS):
            glow = _blurred(rgb, 10.0 + 18.0 * self.amount)
            glow = ImageEnhance.Brightness(glow).enhance(0.35 + 0.5 * self.amount)
            self._glow = (rgb.size, glow, now)
        return ImageChops.screen(rgb, self._glow[1])

    def _feather_mask(self, size: tuple[int, int]) -> Image.Image:
        """A soft-edged ellipse the size of a face crop (cached per size)."""
        if size not in self._feather:
            width, height = size
            mask = Image.new("L", (width, height), 0)
            ImageDraw.Draw(mask).ellipse([width * 0.04, height * 0.02, width * 0.96, height * 0.98], fill=255)
            self._feather[size] = mask.filter(ImageFilter.GaussianBlur(max(1.5, width / 14)))
            if len(self._feather) > 12:
                self._feather.pop(next(iter(self._feather)))
        return self._feather[size]

    def _smooth(self, image: Image.Image, now: float) -> Image.Image:
        faces = self.faces.update(image, now)
        if not faces:
            return image
        out = image.copy()
        for tracked in faces:
            x, y, w, h = tracked.box
            box = (max(0, int(x - w * 0.12)), max(0, int(y - h * 0.22)),
                   min(image.width, int(x + w * 1.12)), min(image.height, int(y + h * 1.12)))
            if box[2] - box[0] < 16 or box[3] - box[1] < 16:
                continue
            crop = image.crop(box).convert("RGB")
            soft = _blurred(crop, 2.5 + 5.0 * self.amount) if crop.width > 360 else \
                crop.filter(ImageFilter.GaussianBlur(2.5 + 5.0 * self.amount))
            soft = Image.blend(crop, soft, 0.4 + 0.5 * self.amount)
            mask = self._feather_mask(crop.size)
            out.paste(Image.composite(soft, crop, mask).convert("RGBA"), box[:2])
        return out


# ------------------------------------------------------------------- framing
class Zoom(Processor):
    """Digital zoom into the middle of the picture."""

    def process(self, image: Image.Image, now: float) -> Image.Image:
        factor = 1.0 + 2.0 * self.amount
        width, height = image.size
        crop_w, crop_h = width / factor, height / factor
        left, top = (width - crop_w) / 2, (height - crop_h) / 2
        return image.convert("RGB").resize(
            image.size, Image.BILINEAR,
            box=(left, top, left + crop_w, top + crop_h)).convert("RGBA")


class AutoFrame(Processor):
    """Keep the face centred and fairly large, moving smoothly."""

    def __init__(self, amount: float = 0.5) -> None:
        super().__init__(amount)
        self.faces = FaceTrack(interval=0.08)
        self._centre: tuple[float, float] | None = None
        self._zoom = 1.0

    def process(self, image: Image.Image, now: float) -> Image.Image:
        width, height = image.size
        faces = self.faces.update(image, now)
        target_zoom, target = 1.0, (width / 2.0, height / 2.0)
        if faces:
            main = max(faces, key=lambda f: f.box[2] * f.box[3])
            wanted = 0.30 + 0.20 * self.amount          # face height / frame height
            target_zoom = max(1.0, min(3.0, wanted * height / max(1.0, main.box[3])))
            cx, cy = main.centre
            target = (cx, cy + main.box[3] * 0.25)      # leave room for shoulders
        if self._centre is None:
            self._centre = target
        self._centre = (self._centre[0] * 0.9 + target[0] * 0.1,
                        self._centre[1] * 0.9 + target[1] * 0.1)
        self._zoom = self._zoom * 0.93 + target_zoom * 0.07
        crop_w, crop_h = width / self._zoom, height / self._zoom
        left = min(max(self._centre[0] - crop_w / 2, 0), width - crop_w)
        top = min(max(self._centre[1] - crop_h / 2, 0), height - crop_h)
        return image.convert("RGB").resize(
            image.size, Image.BILINEAR,
            box=(left, top, left + crop_w, top + crop_h)).convert("RGBA")


class SelfTest(Processor):
    """Processors for testing the worker process (never offered in the UI)."""

    def __init__(self, mode: str, amount: float = 0.5) -> None:
        super().__init__(amount)
        self.mode, self.count = mode, 0

    def process(self, image: Image.Image, now: float) -> Image.Image:
        self.count += 1
        if self.mode == "crash" and self.count >= 3:
            import os

            os._exit(7)
        if self.mode == "hang" and self.count >= 3:
            time.sleep(60)
        if self.mode == "raise":
            raise RuntimeError("selftest failure")
        return ImageOps.invert(image.convert("RGB")).convert("RGBA")


class RoundCorners(Processor):
    """Round the picture's corners by clearing the alpha outside them.

    ``amount`` 0..1 is the radius as a share of half the short side, so 1.0 on a
    square is a circle.  The mask is built once per size and radius.
    """

    def __init__(self, amount: float = 0.25) -> None:
        super().__init__(amount)
        self._mask: np.ndarray | None = None
        self._key: tuple[int, int, int] | None = None

    @staticmethod
    def radius(width: int, height: int, amount: float) -> int:
        return int(round(max(0.0, min(1.0, amount)) * min(width, height) / 2.0))

    def mask(self, width: int, height: int) -> np.ndarray | None:
        radius = self.radius(width, height, self.amount)
        if radius <= 0:
            return None
        key = (width, height, radius)
        if key != self._key:
            # 4x supersampled for smooth edges.
            big = Image.new("L", (width * 4, height * 4), 0)
            ImageDraw.Draw(big).rounded_rectangle(
                (0, 0, width * 4 - 1, height * 4 - 1), radius=radius * 4, fill=255)
            self._mask = np.asarray(big.resize((width, height), Image.LANCZOS))
            self._key = key
        return self._mask

    def process(self, image: Image.Image, now: float) -> Image.Image:
        mask = self.mask(*image.size)
        if mask is None:
            return image
        pixels = np.array(image.convert("RGBA"))
        np.minimum(pixels[..., 3], mask, out=pixels[..., 3])
        return Image.fromarray(pixels, "RGBA")


# ------------------------------------------------------------------ registry
def make_processor(name: str, amount: float) -> Processor:
    """Build the processor registered under ``name`` (see ``effects``)."""
    kind, _, variant = name.partition(":")
    if kind == "background":
        if variant in ("blur", "dim", "mono"):
            return Background(variant, amount)
        return Background("replace", amount, backdrop=variant)
    if kind == "spotlight":
        return Spotlight(amount)
    if kind == "sticker":
        return FaceSticker(variant, amount)
    if kind == "mask":
        return FaceMask(variant, amount)
    if kind == "privacy":
        return FacePrivacy(variant, amount)
    if kind == "touchup":
        return TouchUp(variant, amount)
    if kind == "corners":
        return RoundCorners(amount)
    if kind == "selftest":
        return SelfTest(variant, amount)
    if kind == "pack":
        from . import packs

        return packs.load_effect(variant, amount)
    if kind == "zoom":
        return Zoom(amount)
    if kind == "autoframe":
        return AutoFrame(amount)
    raise KeyError(name)


# ------------------------------------------------------------ the live slot bin
class ProcessorRunner:
    """Worker thread between an ``appsink`` and an ``appsrc``."""

    def __init__(self, processor: Processor, sink, source, name: str,
                 fps: int = 30) -> None:
        self.processor, self.sink, self.source = processor, sink, source
        # A source that is not paced (a frozen image) would otherwise be
        # processed as fast as the CPU allows; keep to the canvas rate.
        self._interval = 0.9 / max(1, int(fps))
        self._last_frame = 0.0
        self._stop = threading.Event()
        self._eos = threading.Event()
        # The sink reports end-of-stream through this signal.  Its ``eos``
        # property is also true before the sink has started, which made the
        # worker (started before the pipeline plays) quit immediately.
        sink.connect("eos", lambda _sink: self._eos.set())
        self._started = time.monotonic()
        self.frames = 0
        self.errors = 0
        self._thread = threading.Thread(target=self._run, name=f"anycam-fx-{name}",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self.sink.emit("try-pull-sample", 100 * Gst.MSECOND)
            except Exception:  # noqa: BLE001 - the element is being torn down
                return
            if sample is None:
                if self._eos.is_set():
                    self.source.emit("end-of-stream")
                    return
                continue
            # Pace by waiting, not dropping: the upstream then feels back-pressure
            # and runs at the canvas rate.  Dropping let an unpaced source (a
            # frozen image) spin at tens of thousands of frames a second.
            wait = self._interval - (time.monotonic() - self._last_frame)
            if wait > 0 and self._stop.wait(wait):
                return
            self._last_frame = time.monotonic()
            try:
                self._handle(sample)
            except Exception:  # noqa: BLE001 - one bad frame must not kill the stream
                self.errors += 1
                self._push_raw(sample)

    def _handle(self, sample) -> None:
        caps = sample.get_caps().get_structure(0)
        width, height = int(caps.get_value("width")), int(caps.get_value("height"))
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return
        try:
            # One copy out of the mapped buffer (bytes() then a slice made two).
            image = Image.frombytes("RGBA", (width, height),
                                    info.data[: width * height * 4])
        finally:
            buffer.unmap(info)
        result = self.processor.process(image, time.monotonic() - self._started)
        if result.size != (width, height):
            result = result.resize((width, height), Image.BILINEAR)
        if result.mode != "RGBA":
            result = result.convert("RGBA")
        out = Gst.Buffer.new_wrapped(result.tobytes())
        if buffer.duration != Gst.CLOCK_TIME_NONE:
            out.duration = buffer.duration
        self.frames += 1
        self.source.emit("push-buffer", out)

    def _push_raw(self, sample) -> None:
        buffer = sample.get_buffer()
        if buffer is not None:
            self.source.emit("push-buffer", buffer.copy())

    def stop(self) -> None:
        self._stop.set()
        self.processor.close()


# ------------------------------------------------- native framing (live)
# Pillow cannot enlarge a 1080p crop fast enough (a bilinear resize alone is
# ~21 ms), so the live Zoom and Auto-frame slots crop and scale with
# GStreamer's videocrop/videoscale.  Python only decides the rectangle.  The
# Zoom and AutoFrame classes above still draw thumbnails and still pictures.
def _crop_rect(width: int, height: int, zoom: float,
               centre: tuple[float, float]) -> tuple[int, int, int, int]:
    """(left, top, crop_w, crop_h) of a ``zoom`` x crop around ``centre``."""
    zoom = max(1.0, zoom)
    crop_w, crop_h = width / zoom, height / zoom
    left = min(max(centre[0] - crop_w / 2, 0.0), width - crop_w)
    top = min(max(centre[1] - crop_h / 2, 0.0), height - crop_h)
    return int(round(left)), int(round(top)), int(round(crop_w)), int(round(crop_h))


class _CropControl:
    """Sets a videocrop's four margins, skipping sub-pixel jitter."""

    def __init__(self, crop: Gst.Element, width: int, height: int) -> None:
        self.crop, self.width, self.height = crop, width, height
        self.margins: tuple[int, int, int, int] | None = None

    def apply(self, left: int, top: int, crop_w: int, crop_h: int) -> None:
        crop_w = max(2, min(self.width, crop_w))
        crop_h = max(2, min(self.height, crop_h))
        left = max(0, min(self.width - crop_w, left))
        top = max(0, min(self.height - crop_h, top))
        margins = (left, self.width - left - crop_w, top, self.height - top - crop_h)
        if self.margins is not None and max(
                abs(a - b) for a, b in zip(margins, self.margins, strict=True)) < 2:
            return
        self.margins = margins
        # Each change renegotiates videocrop -> videoscale; measured at 1080p60
        # it still holds the full frame rate.
        for prop, value in zip(("left", "right", "top", "bottom"), margins, strict=True):
            self.crop.set_property(prop, int(value))


class NativeZoom:
    """Live Zoom: a fixed centre crop that only changes with the slider."""

    def __init__(self, control: _CropControl, amount: float) -> None:
        self.processor = self
        self.control = control
        self.set_amount(amount)

    def set_amount(self, amount: float) -> None:
        self.amount = max(0.0, min(1.0, float(amount)))
        width, height = self.control.width, self.control.height
        self.control.apply(*_crop_rect(width, height, 1.0 + 2.0 * self.amount,
                                       (width / 2.0, height / 2.0)))

    def stop(self) -> None:
        pass


class NativeAutoFrame:
    """Live Auto-frame: faces found on a small tap steer a videocrop.

    The picture itself never passes through Python; a control thread reads
    the newest 15 fps thumbnail, tracks the face and eases the crop at the
    canvas rate, with the same targets and smoothing as :class:`AutoFrame`.
    """

    def __init__(self, control: _CropControl, tap, amount: float, fps: int) -> None:
        self.processor = self
        self.control, self.tap = control, tap
        self.amount = max(0.0, min(1.0, float(amount)))
        self.faces = FaceTrack(interval=0.08, asynchronous=False)
        self._interval = 1.0 / max(1, int(fps))
        self._centre: tuple[float, float] | None = None
        self.zoom = 1.0
        self._latest: Image.Image | None = None
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="anycam-fx-autoframe",
                                        daemon=True)
        self._thread.start()

    def set_amount(self, amount: float) -> None:
        self.amount = max(0.0, min(1.0, float(amount)))

    def _pull(self) -> None:
        try:
            sample = self.tap.emit("try-pull-sample", 0)
        except Exception:  # noqa: BLE001 - the element is being torn down
            return
        if sample is None:
            return
        caps = sample.get_caps().get_structure(0)
        width, height = int(caps.get_value("width")), int(caps.get_value("height"))
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return
        try:
            self._latest = Image.frombytes("RGBA", (width, height),
                                           info.data[: width * height * 4])
        finally:
            buffer.unmap(info)

    def step(self, now: float) -> None:
        """One control tick: track, ease, move the crop."""
        self._pull()
        width, height = self.control.width, self.control.height
        target_zoom, target = 1.0, (width / 2.0, height / 2.0)
        if self._latest is not None:
            faces = self.faces.update(self._latest, now)
            if faces:
                scale = width / self._latest.width
                main = max(faces, key=lambda f: f.box[2] * f.box[3])
                face_h = main.box[3] * scale
                wanted = 0.30 + 0.20 * self.amount      # face height / frame height
                target_zoom = max(1.0, min(3.0, wanted * height / max(1.0, face_h)))
                cx, cy = main.centre
                target = (cx * scale, cy * scale + face_h * 0.25)
        if self._centre is None:
            self._centre = target
        self._centre = (self._centre[0] * 0.9 + target[0] * 0.1,
                        self._centre[1] * 0.9 + target[1] * 0.1)
        self.zoom = self.zoom * 0.93 + target_zoom * 0.07
        self.control.apply(*_crop_rect(width, height, self.zoom, self._centre))

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.step(started - self._started)
            except Exception:  # noqa: BLE001 - framing must never stop the video
                pass
            self._stop.wait(max(0.0, self._interval - (time.monotonic() - started)))

    def stop(self) -> None:
        self._stop.set()
        self.faces.close()


def build_framing_bin(name: str, amount: float, bin_name: str, width: int,
                      height: int, fps: int):
    """``(bin, runner)`` for a live Zoom or Auto-frame slot (GStreamer scaling)."""
    def make(factory, element_name=None, **props):
        element = Gst.ElementFactory.make(factory, element_name)
        if element is None:
            raise RuntimeError(f"GStreamer element '{factory}' is not installed")
        for key, value in props.items():
            element.set_property(key.replace("_", "-"), value)
        return element

    def caps(text):
        return make("capsfilter", caps=Gst.Caps.from_string(text))

    width, height = max(2, int(width)), max(2, int(height))
    out_caps = (f"video/x-raw,format=RGBA,width={width},height={height},"
                "pixel-aspect-ratio=1/1")
    bin_ = Gst.Bin.new(bin_name)
    crop = make("videocrop", "fx")
    chain = [crop, make("videoscale"), caps(out_caps)]
    kind = name.partition(":")[0]
    if kind == "zoom":
        elements = chain
        for element in elements:
            bin_.add(element)
        head = crop
    else:
        tee = make("tee")
        main_queue = make("queue", max_size_buffers=2, max_size_bytes=0, max_size_time=0)
        tap_w = 384
        tap_h = max(2, int(round(tap_w * height / width)) // 2 * 2)
        # The tap only needs a few frames a second: drop before scaling down.
        tap = [make("queue", leaky=2, max_size_buffers=1, max_size_bytes=0,
                    max_size_time=0),
               make("videorate", drop_only=True, max_rate=15),
               make("videoscale"),
               caps(f"video/x-raw,format=RGBA,width={tap_w},height={tap_h}"),
               make("appsink", sync=False, drop=True, max_buffers=1)]
        # Not async: like the processor sink, it sits mid-canvas and must not
        # hold the pipeline in PAUSED waiting to preroll.
        tap[-1].set_property("async", False)
        elements = [tee, main_queue, *chain, *tap]
        for element in elements:
            bin_.add(element)
        tee.link(main_queue)
        tee.link(tap[0])
        for first, second in zip(tap, tap[1:]):
            if not first.link(second):
                raise RuntimeError("could not link the auto-frame tap")
        main_queue.link(crop)
        head = tee
    for first, second in zip(chain, chain[1:]):
        if not first.link(second):
            raise RuntimeError("could not link the framing chain")
    bin_.add_pad(Gst.GhostPad.new("sink", head.get_static_pad("sink")))
    bin_.add_pad(Gst.GhostPad.new("src", chain[-1].get_static_pad("src")))
    control = _CropControl(crop, width, height)
    if kind == "zoom":
        runner = NativeZoom(control, amount)
    else:
        runner = NativeAutoFrame(control, elements[-1], amount, fps)
    return bin_, runner


NATIVE_FRAMING = ("zoom", "autoframe")


def build_processor_bin(name: str, amount: float, bin_name: str, width: int,
                        height: int, fps: int, live: bool = False):
    """A bin that runs processor ``name``; returns ``(bin, runner)``."""
    if name.partition(":")[0] in NATIVE_FRAMING:
        return build_framing_bin(name, amount, bin_name, width, height, fps)
    from . import effect_worker, safemode

    if safemode.active() and name.partition(":")[0] in effect_worker.ISOLATED_KINDS:
        processor = Processor(amount)    # safe mode: AI effects pass the picture through
    else:
        processor = (effect_worker.RemoteProcessor(name, amount)
                     if effect_worker.wants_isolation(name) else make_processor(name, amount))

    def make(factory, element_name=None):
        element = Gst.ElementFactory.make(factory, element_name)
        if element is None:
            raise RuntimeError(f"GStreamer element '{factory}' is not installed")
        return element

    bin_ = Gst.Bin.new(bin_name)
    queue = make("queue")
    # An unpaced source (a frozen image) must feel back-pressure or it runs
    # away (see ProcessorRunner._run), so the queue is not leaky for it.  A live
    # source paces itself, and back-pressure there blocked the camera thread
    # whenever the worker fell behind: camera frames were dropped upstream,
    # videorate filled the gaps with duplicates, the worker spent its time on
    # those, and a mask showed 7-19 different pictures a second.  For a live
    # source the worker takes the newest frame instead.
    queue.set_property("max-size-buffers", 2)
    queue.set_property("max-size-bytes", 0)
    queue.set_property("max-size-time", 0)
    if live:
        queue.set_property("leaky", 2)
    sink = make("appsink")
    sink.set_property("sync", False)
    sink.set_property("max-buffers", 1)
    sink.set_property("drop", bool(live))
    sink.set_property("emit-signals", True)   # for the eos signal only
    # Not async: this sink sits in the middle of the canvas.  Waiting to preroll
    # it kept a pipeline that was *built* with a processor in PAUSED forever,
    # because the data that would preroll it only flows once it is playing.
    sink.set_property("async", False)
    source = make("appsrc")
    source.set_property("caps", Gst.Caps.from_string(
        f"video/x-raw,format=RGBA,width={int(width)},height={int(height)},"
        f"framerate={max(1, int(fps))}/1"))
    source.set_property("format", Gst.Format.TIME)
    source.set_property("is-live", True)
    source.set_property("do-timestamp", True)
    source.set_property("block", False)
    source.set_property("max-buffers", 2)
    source.set_property("leaky-type", 2)   # downstream is the canvas: never stall it
    for element in (queue, sink, source):
        bin_.add(element)
    if not queue.link(sink):
        raise RuntimeError("could not link the processor queue")
    bin_.add_pad(Gst.GhostPad.new("sink", queue.get_static_pad("sink")))
    bin_.add_pad(Gst.GhostPad.new("src", source.get_static_pad("src")))
    runner = ProcessorRunner(processor, sink, source, name.replace(":", "-"), fps)
    return bin_, runner
