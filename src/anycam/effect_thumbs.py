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
from . import effect_worker
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


def _is_pure_processor(effects: Effects) -> str | None:
    """Return processor name when ``effects`` is a single processor filter with no other active FX."""
    if (effects.filter == "none" or effects.filter2 != "none"
            or getattr(effects, "filter3", "none") != "none"
            or getattr(effects, "filter4", "none") != "none"
            or effects.frame_style != "none" or effects.color_effect != "none"
            or effects.chroma_enabled or effects.mirror
            or abs(effects.brightness) > 1e-4 or abs(effects.contrast - 1.0) > 1e-4
            or abs(effects.saturation - 1.0) > 1e-4 or abs(effects.hue) > 1e-4
            or abs(effects.red_gain - 1.0) > 1e-4 or abs(effects.green_gain - 1.0) > 1e-4
            or abs(effects.blue_gain - 1.0) > 1e-4):
        return None
    spec = effect_library.FILTERS.get(effects.filter)
    if spec is None or not spec.processor or spec.processor.startswith("pack:"):
        return None
    if spec.processor.startswith("autoframe"):
        return None
    return spec.processor


_SAMPLE_FACE_CACHE: dict[tuple[int, int, int], list] = {}


def _detect_sample_faces(picture: Image.Image):
    """Detect faces once per thumbnail sample image (shared across all 42 face/mask tiles)."""
    import hashlib
    import numpy as np

    from . import face as face_mod

    rgb = picture.convert("RGB")
    digest = hash(hashlib.blake2b(rgb.tobytes(), digest_size=8).digest())
    key = (rgb.width, rgb.height, digest)
    if key in _SAMPLE_FACE_CACHE:
        return _SAMPLE_FACE_CACHE[key]
    detector = face_mod.shared_detector()
    if detector is None:
        return None
    found = detector.detect(np.asarray(rgb), score_threshold=0.6, top_k=4)
    if len(_SAMPLE_FACE_CACHE) > 8:
        _SAMPLE_FACE_CACHE.clear()
    _SAMPLE_FACE_CACHE[key] = found
    return found


def render_thumbnail(effects: Effects, sample: Image.Image,
                     size: tuple[int, int] = THUMB_SIZE) -> Image.Image | None:
    """``sample`` with ``effects`` applied, or ``None`` if the chain failed."""
    import time

    width, height = size
    picture = sample.convert("RGBA").resize((width, height), Image.BILINEAR)
    if any(str(getattr(effects, attr, "none")).startswith("pack-")
           for attr in ("filter", "filter2", "filter3", "filter4")):
        # A pack's code only ever runs in the worker process; a tile is not worth starting one.
        import dataclasses

        effects = dataclasses.replace(
            effects,
            **{attr: "none" if str(getattr(effects, attr, "none")).startswith("pack-")
               else getattr(effects, attr, "none")
               for attr in ("filter", "filter2", "filter3", "filter4")})
    proc_name = _is_pure_processor(effects)
    if proc_name is not None:
        from . import processors

        proc = None
        try:
            with effect_worker.in_process():
                proc = processors.make_processor(proc_name, effects.filter_amount)
            faces_helper = getattr(proc, "faces", None)
            if faces_helper is not None:
                # Synchronous + cached detection: avoids 42 redundant YuNet runs & 42 GStreamer pipelines.
                faces_helper._job = None
                cached = _detect_sample_faces(picture)
                if cached is not None:
                    faces_helper._merge(cached, picture.size, 0.0)
                    faces_helper._last = 0.0
            mask_helper = getattr(proc, "mask", None)
            if mask_helper is not None:
                mask_helper._job = None
            out = proc.process(picture, 0.1)
            return out.convert("RGB") if out is not None else None
        except Exception:  # noqa: BLE001 - fall back to pipeline render
            pass
        finally:
            if proc is not None:
                try:
                    proc.close()
                except Exception:  # noqa: BLE001
                    pass
    layer = Layer("thumb", "test", "thumb", effects=effects)
    chain = None
    pipeline = None
    result = None
    try:
        try:
            chain = SourceChain()
            chain.add(make("videoconvert"))
            _add_effects(chain, layer)
            with effect_worker.in_process():
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
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            return None
        step = Gst.SECOND // 30
        for index in range(count):
            buffer = Gst.Buffer.new_wrapped(frames_data[index])
            buffer.pts = index * step
            buffer.duration = step
            if source.emit("push-buffer", buffer) != Gst.FlowReturn.OK:
                break
        source.emit("end-of-stream")
        bus = pipeline.get_bus()
        deadline = time.monotonic() + 5.0
        last = None
        while time.monotonic() < deadline:
            if bus is not None and bus.pop_filtered(Gst.MessageType.ERROR) is not None:
                return None
            remaining_ns = max(1, int((deadline - time.monotonic()) * Gst.SECOND))
            sample_out = sink.emit("try-pull-sample", min(remaining_ns, Gst.SECOND // 2))
            if sample_out is not None:
                last = sample_out
                continue
            if sink.get_property("eos"):
                break
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
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        # A processor filter owns a worker thread; without this every tile
        # drawn for one leaked a thread for the life of the program.
        if chain is not None:
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
        self._generation = 0

    @property
    def busy(self) -> bool:
        return self.future is not None and not self.future.done()

    def submit(self, token, category: str, entry: dict, sample: Image.Image,
               sample_is_default: bool) -> None:
        import time

        gen = self._generation

        def job():
            if gen != self._generation:
                return None
            try:
                return tile_picture(category, entry, sample, sample_is_default)
            except Exception:  # noqa: BLE001 - a tile without a preview is fine
                return None
            finally:
                if gen == self._generation:
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
        self._generation += 1
        if self.future is not None:
            self.future.cancel()
        self.future = self.token = None

    def stop(self) -> None:
        self.forget()
        self._pool.shutdown(wait=False, cancel_futures=True)
