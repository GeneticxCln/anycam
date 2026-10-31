"""Session-owned virtual camera.

The canvas rebuilds for scene/layer edits.  Keeping ``v4l2sink`` in a separate
pipeline means a consumer reading the loopback device does not see the sink
stop and start with every rebuild.  The engine only owns a small appsink tap
into this pipeline.
"""
from __future__ import annotations

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from ... import config  # noqa: E402

Gst.init(None)


def _make(factory: str, name: str, **props) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer element '{factory}' is not installed")
    for key, value in props.items():
        element.set_property(key.replace("_", "-"), value)
    return element


class VcamOutput:
    """Own the ``v4l2sink`` pipeline for the window's lifetime."""

    def __init__(self) -> None:
        self.device = ""
        self.pixel_format = "YUY2"
        self.error = ""
        self._pipeline: Gst.Pipeline | None = None
        self._appsrc: Gst.Element | None = None
        self._bus: Gst.Bus | None = None

    @property
    def active(self) -> bool:
        return self._pipeline is not None

    def start(self, device: str, width: int, height: int, fps: int) -> bool:
        if self.active:
            self.stop()
        self.device = device
        self.error = ""
        if not config.vcam_available(device):
            self.error = (
                f"virtual camera {device} is not available; load v4l2loopback with "
                f"'sudo modprobe v4l2loopback video_nr=10 card_label=AnyCam'"
            )
            return False
        self.pixel_format = config.vcam_pixel_format(device)
        caps = (f"video/x-raw,format={self.pixel_format},width={width},"
                f"height={height},framerate={fps}/1")
        pipeline = Gst.Pipeline.new("anycam-vcam")
        try:
            appsrc = _make("appsrc", "vcam_appsrc")
            appsrc.set_property("caps", Gst.Caps.from_string(caps))
            appsrc.set_property("format", Gst.Format.TIME)
            appsrc.set_property("is-live", True)
            appsrc.set_property("max-buffers", 2)
            appsrc.set_property("block", False)
            appsrc.set_property("leaky-type", 2)
            convert = _make("videoconvert", "vcam_convert")
            sink = _make("v4l2sink", "vcam_sink", device=device, sync=False)
            for element in (appsrc, convert, sink):
                pipeline.add(element)
            if not appsrc.link(convert) or not convert.link(sink):
                raise RuntimeError("could not link appsrc into v4l2sink")
        except Exception as exc:  # noqa: BLE001
            self.error = f"virtual camera pipeline failed: {exc}"
            pipeline.set_state(Gst.State.NULL)
            return False
        self._pipeline = pipeline
        self._appsrc = appsrc
        self._bus = pipeline.get_bus()
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.error = "virtual camera pipeline failed to start"
            pipeline.set_state(Gst.State.NULL)
            self._clear()
            return False
        return True

    def push_sample(self, sample) -> None:
        if self._appsrc is None or self.error:
            return
        try:
            self._appsrc.emit("push-sample", sample)
        except Exception as exc:  # noqa: BLE001
            self.error = f"virtual camera push failed: {exc}"

    def poll(self) -> None:
        if self._bus is None:
            return
        while True:
            msg = self._bus.pop_filtered(Gst.MessageType.ERROR)
            if msg is None:
                return
            err, debug = msg.parse_error()
            self.error = f"{err.message} ({debug})"

    def stop(self) -> dict:
        pipeline = self._pipeline
        if pipeline is None:
            return {"device": self.device, "ok": False,
                    "error": self.error or "virtual camera is not running"}
        try:
            if self._appsrc is not None:
                self._appsrc.emit("end-of-stream")
        except Exception:
            pass
        pipeline.set_state(Gst.State.NULL)
        result = {"device": self.device, "ok": not self.error,
                  "error": self.error}
        self._clear()
        return result

    def _clear(self) -> None:
        self._pipeline = None
        self._appsrc = None
        self._bus = None
