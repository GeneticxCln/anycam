"""GPU canvas probing and small canvas predicates."""
from __future__ import annotations

import functools
import os

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from ..model import Layer  # noqa: E402


# Extra aggregation latency of the GPU canvas (see where it is set).
GL_MIXER_LATENCY_NS = 33_000_000


def canvas_requested() -> str:
    """``ANYCAM_CANVAS``: "cpu", "gpu" or "" (no override)."""
    value = os.environ.get("ANYCAM_CANVAS", "").strip().lower()
    return value if value in ("cpu", "gpu") else ""


@functools.lru_cache(maxsize=1)
def gl_canvas_problem() -> str:
    """Why the GL canvas cannot run here; empty when a real frame made it.

    The elements being installed is not enough: a GL context can fail, and on
    NVIDIA an upload can silently produce black textures.  So one small
    frame goes through upload -> glvideomixer -> download and its colour is
    checked.
    """
    for factory in ("glvideomixer", "glupload", "gldownload"):
        if Gst.ElementFactory.find(factory) is None:
            return f"{factory} is not installed"
    try:
        pipeline = Gst.parse_launch(
            "videotestsrc num-buffers=4 pattern=red ! "
            "video/x-raw,format=RGBA,width=64,height=64,framerate=30/1 ! "
            "identity drop-allocation=true ! glupload ! glvideomixer ! "
            "gldownload ! video/x-raw,format=RGBA,width=64,height=64 ! "
            "appsink name=probe sync=false")
    except GLib.Error as exc:
        return f"GL canvas probe could not be built: {exc.message}"
    sink = pipeline.get_by_name("probe")
    pipeline.set_state(Gst.State.PLAYING)
    sample = last = None
    try:
        for _ in range(4):
            sample = sink.emit("try-pull-sample", 3 * Gst.SECOND)
            if sample is None:
                break
            last = sample
        if last is None:
            message = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            return ("GL canvas probe produced no frame"
                    + (f": {message.parse_error()[0].message}" if message else ""))
        buf = last.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return "GL canvas probe frame could not be read"
        try:
            centre = bytes(info.data)[(32 * 64 + 32) * 4:(32 * 64 + 32) * 4 + 3]
        finally:
            buf.unmap(info)
        if not (centre[0] > 200 and centre[1] < 60 and centre[2] < 60):
            return f"GL canvas probe frame came out wrong (centre {tuple(centre)})"
        return ""
    finally:
        pipeline.set_state(Gst.State.NULL)


def _empty_drawing(layer: Layer) -> bool:
    """A drawing layer with no strokes (nothing to composite)."""
    if layer.kind != "drawing":
        return False
    from .. import animated

    try:
        return not animated.parse_strokes(layer.source)
    except Exception:  # noqa: BLE001 - unreadable strokes: keep the layer
        return False
