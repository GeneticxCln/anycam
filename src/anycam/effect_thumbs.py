"""Thumbnails for the effects browser.

A thumbnail is made by running a small picture through the *same* chain a layer
uses (colour, film look, filter slot, frame), so what the tile shows is what the
effect does.  The rendering is a short GStreamer pipeline per tile.
"""
from __future__ import annotations

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from PIL import Image  # noqa: E402

from . import effects as effect_library  # noqa: E402
from .model import Effects, Layer  # noqa: E402
from .sources import SourceChain, _add_effects, _finish_chain, make  # noqa: E402

Gst.init(None)

# Elements that only show what *moves*.  A still sample renders them as a blown-
# out or empty tile, so their thumbnails are fed a small back-and-forth shift.
MOTION_ELEMENTS = frozenset({"radioactv", "optv", "quarktv", "streaktv",
                             "vertigotv", "rippletv"})
MOTION_FRAMES = 9

THUMB_SIZE = (160, 90)
# Effects with memory (trails, motion glow) need a few frames to show.
THUMB_FRAMES = 5


def default_sample(size: tuple[int, int] = THUMB_SIZE) -> Image.Image:
    """A real portrait for previews when no camera frame is available.

    Face and portrait effects need a face to show anything, so the sample is a
    16:9 crop around the face of the bundled public-domain NASA portrait.
    """
    from . import config

    path = config.assets_dir() / "sample-portrait.jpg"
    try:
        with Image.open(path) as opened:
            picture = opened.convert("RGB")
        width, height = picture.size                     # 480 x 600
        crop_h = int(width * 9 / 16)
        top = max(0, min(height - crop_h, int(height * 0.115)))
        return picture.crop((0, top, width, top + crop_h)).resize(size, Image.LANCZOS)
    except OSError:
        return Image.new("RGB", size, (60, 90, 140))


def render_thumbnail(effects: Effects, sample: Image.Image,
                     size: tuple[int, int] = THUMB_SIZE) -> Image.Image | None:
    """``sample`` with ``effects`` applied, or ``None`` if the chain failed."""
    width, height = size
    picture = sample.convert("RGBA").resize((width, height), Image.BILINEAR)
    layer = Layer("thumb", "test", "thumb", effects=effects)
    try:
        chain = SourceChain()
        chain.add(make("videoconvert"))
        _add_effects(chain, layer)
        _finish_chain(chain, width, height, 30, layer=layer)
        pipeline = Gst.Pipeline.new("anycam-thumb")
        source = make("appsrc", format=Gst.Format.TIME, is_live=False,
                      block=True)
        source.set_property("caps", Gst.Caps.from_string(
            f"video/x-raw,format=RGBA,width={width},height={height},"
            "framerate=30/1"))
        sink = make("appsink", sync=False, max_buffers=MOTION_FRAMES + 2,
                    drop=False)
        elements = [source, *chain.elements, sink]
        for element in elements:
            pipeline.add(element)
        for first, second in zip(elements, elements[1:], strict=False):
            if not first.link(second):
                return None
    except Exception:  # noqa: BLE001 - a missing element means "no preview"
        return None
    from PIL import ImageChops

    moving = any(
        getattr(effect_library.FILTERS.get(key), "element", "") in MOTION_ELEMENTS
        for key in (effects.filter, effects.filter2))
    count = MOTION_FRAMES if moving else THUMB_FRAMES
    # Shift sideways and return, so the last frame sits where the sample does.
    offsets = ([0, 6, 12, 6, 0, -6, -12, -6, 0][:count] if moving else [0] * count)
    frames_data = [(picture if dx == 0 else ImageChops.offset(picture, dx, 0)).tobytes()
                   for dx in offsets]
    result = None
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            return None
        step = Gst.SECOND // 30
        for index in range(count):
            buffer = Gst.Buffer.new_wrapped(frames_data[index])
            buffer.pts = index * step
            buffer.duration = step
            source.emit("push-buffer", buffer)
        source.emit("end-of-stream")
        last = None
        while True:
            sample_out = sink.emit("pull-sample")
            if sample_out is None:
                break
            last = sample_out
        if last is not None:
            buffer = last.get_buffer()
            ok, info = buffer.map(Gst.MapFlags.READ)
            if ok:
                try:
                    result = Image.frombytes(
                        "RGBA", (width, height), bytes(info.data)[: width * height * 4])
                finally:
                    buffer.unmap(info)
    finally:
        pipeline.set_state(Gst.State.NULL)
        # A processor filter owns a worker thread; without this every tile
        # drawn for one leaked a thread for the life of the program.
        for hook in chain.cleanup:
            try:
                hook()
            except Exception:  # noqa: BLE001 - cleanup must not hide the result
                pass
    return result.convert("RGB") if result is not None else None


def swatch_thumbnail(effects: Effects, size: tuple[int, int] = THUMB_SIZE
                     ) -> Image.Image | None:
    """Thumbnail from the built-in sample (used when no frame is available)."""
    return render_thumbnail(effects, default_sample(size), size)


def particle_thumbnail(source: str, sample: Image.Image,
                       size: tuple[int, int] = THUMB_SIZE) -> Image.Image | None:
    """``sample`` with one frame of a particle animation drawn over it."""
    import numpy as np

    from . import particles

    width, height = size
    generator, _size = particles.make(source, width * 4, height * 4)
    frame = None
    for step in range(4):                  # a few frames in, so rockets have burst
        frame = generator.frame(2.4 + step * 0.05)
    layer = Image.fromarray(np.ascontiguousarray(frame), "RGBA").resize(size, Image.LANCZOS)
    picture = sample.convert("RGBA").resize(size, Image.BILINEAR)
    picture.alpha_composite(layer)
    return picture.convert("RGB")


# --------------------------------------------------------------- tile rendering
# A tile costs up to ~220 ms (a mask: face detection plus drawing, through a
# whole GStreamer pipeline).  Rendered two per 15 ms tick on the GUI thread,
# opening the Masks tab blocked the window and, through Python's GIL, the
# effect threads too: the camera froze for ~0.5 s at a time for seconds.
# Tiles are now drawn on one background thread with a pause between them,
# and tiles of the built-in sample are kept on disk across sessions.
TILE_PAUSE_S = 0.03
_TILE_CACHE_VERSION = 1


def _tile_cache_path(category: str, entry: dict):
    import hashlib
    import json

    from . import __version__, config

    key = json.dumps({
        "v": _TILE_CACHE_VERSION, "app": __version__, "size": THUMB_SIZE,
        "frames": [THUMB_FRAMES, MOTION_FRAMES], "category": category,
        "label": entry.get("label"), "preview": entry.get("preview"),
        "particles": entry.get("particles"),
    }, sort_keys=True, default=str)
    folder = config.cache_dir() / "effect-thumbs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{hashlib.sha1(key.encode()).hexdigest()}.png"


def tile_picture(category: str, entry: dict, sample: Image.Image,
                 sample_is_default: bool) -> Image.Image | None:
    """The tile's picture; from the disk cache when the sample is the built-in one."""
    path = _tile_cache_path(category, entry) if sample_is_default else None
    if path is not None and path.is_file():
        try:
            with Image.open(path) as cached:
                return cached.convert("RGB")
        except OSError:
            pass
    if entry.get("particles"):
        picture = particle_thumbnail(entry["particles"], sample)
    else:
        picture = render_thumbnail(Effects(**entry["preview"]), sample)
    if picture is not None and path is not None:
        try:
            temporary = path.with_suffix(".tmp")
            picture.convert("RGB").save(temporary, "PNG")
            temporary.replace(path)
        except OSError:
            pass
    return picture


class TileRenderer:
    """One background thread drawing tiles, one at a time."""

    def __init__(self) -> None:
        import concurrent.futures

        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="anycam-thumbs")
        self.future = None
        self.token = None

    @property
    def busy(self) -> bool:
        return self.future is not None and not self.future.done()

    def submit(self, token, category: str, entry: dict, sample: Image.Image,
               sample_is_default: bool) -> None:
        import time

        def job():
            try:
                return tile_picture(category, entry, sample, sample_is_default)
            except Exception:  # noqa: BLE001 - a tile without a preview is fine
                return None
            finally:
                # Leave the GIL to the live effect threads between tiles.
                time.sleep(TILE_PAUSE_S)

        self.token = token
        self.future = self._pool.submit(job)

    def take(self):
        """``(token, picture)`` once the current tile is drawn, else None."""
        if self.future is None or not self.future.done():
            return None
        future, token = self.future, self.token
        self.future = self.token = None
        try:
            return token, future.result()
        except Exception:  # noqa: BLE001
            return token, None

    def forget(self) -> None:
        """Drop the tile in flight (its grid was repopulated)."""
        self.future = self.token = None

    def stop(self) -> None:
        self.forget()
        self._pool.shutdown(wait=False, cancel_futures=True)
