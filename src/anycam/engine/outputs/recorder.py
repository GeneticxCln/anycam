"""Persistent MP4 recorder.

The canvas is rebuilt for scene/layer changes, but a recording session must stay
one continuous file.  This pipeline owns ``mp4mux`` and the file sink for the
whole session; an engine attaches an appsink tap, pushes encoded samples, and
can be torn down without finalising the file.

Each rebuilt canvas restarts its encoder timestamps at zero.  The recorder
detects that backward jump and offsets the new stream onto the session
timeline, so ``mp4mux`` still sees one monotonic stream.
"""
from __future__ import annotations

import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

VIDEO_CAPS = "video/x-h264,stream-format=avc,alignment=au"
AUDIO_CAPS = "audio/mpeg,mpegversion=4,stream-format=raw"


def _make(factory: str, name: str, **props) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer element '{factory}' is not installed")
    for key, value in props.items():
        element.set_property(key.replace("_", "-"), value)
    return element


class Recorder:
    """Own one MP4 muxer/filesink across any number of canvas rebuilds."""

    def __init__(self) -> None:
        self.path = ""
        self.error = ""
        self._pipeline: Gst.Pipeline | None = None
        self._mux: Gst.Element | None = None
        self._video_src: Gst.Element | None = None
        self._audio_src: Gst.Element | None = None
        self._bus: Gst.Bus | None = None
        self._video_step = Gst.SECOND // 30
        self._audio_step = 1024 * Gst.SECOND // 48000
        self._last_in: dict[str, int | None] = {"video": None, "audio": None}
        self._last_out: dict[str, int | None] = {"video": None, "audio": None}
        self._offset: dict[str, int] = {"video": 0, "audio": 0}
        self._eos = False

    @property
    def active(self) -> bool:
        return self._pipeline is not None

    # ------------------------------------------------------------------ start
    def start(self, path: str, width: int, height: int, fps: int, *,
              audio: bool = False) -> bool:
        """Start a session.  Requested pads exist before the first sample."""
        if self.active:
            self.stop()
        self.path = str(path)
        self.error = ""
        self._eos = False
        self._last_in = {"video": None, "audio": None}
        self._last_out = {"video": None, "audio": None}
        self._offset = {"video": 0, "audio": 0}
        self._video_step = max(1, Gst.SECOND // max(1, int(fps)))
        self._audio_step = 1024 * Gst.SECOND // 48000
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        pipeline = Gst.Pipeline.new("anycam-recorder")
        video = _make("appsrc", "record_video_src")
        video.set_property("format", Gst.Format.TIME)
        video.set_property("is-live", True)
        video.set_property("max-buffers", 8)
        video.set_property("block", False)
        video.set_property("leaky-type", 2)
        parse = _make("h264parse", "record_h264_parse", config_interval=-1)
        mux = _make("mp4mux", "record_mux", fragment_duration=500)
        sink = _make("filesink", "record_sink", location=self.path, sync=False)
        for element in (video, parse, mux, sink):
            pipeline.add(element)
        if not video.link(parse):
            self.error = "could not link recorder video into h264parse"
            pipeline.set_state(Gst.State.NULL)
            return False
        video_pad = mux.request_pad_simple("video_%u")
        if video_pad is None or parse.get_static_pad("src").link(video_pad) != Gst.PadLinkReturn.OK:
            self.error = "could not link recorder video into mp4mux"
            pipeline.set_state(Gst.State.NULL)
            return False
        if not mux.link(sink):
            self.error = "could not link recorder mp4mux into filesink"
            pipeline.set_state(Gst.State.NULL)
            return False

        audio_src = None
        if audio:
            try:
                audio_src = _make("appsrc", "record_audio_src")
                audio_src.set_property("format", Gst.Format.TIME)
                audio_src.set_property("is-live", True)
                audio_src.set_property("max-buffers", 8)
                audio_src.set_property("block", False)
                audio_src.set_property("leaky-type", 2)
                audio_parse = _make("aacparse", "record_aac_parse")
                pipeline.add(audio_src)
                pipeline.add(audio_parse)
                if not audio_src.link(audio_parse):
                    raise RuntimeError("could not link recorder audio into aacparse")
                audio_pad = mux.request_pad_simple("audio_%u")
                if (audio_pad is None or
                        audio_parse.get_static_pad("src").link(audio_pad)
                        != Gst.PadLinkReturn.OK):
                    raise RuntimeError("could not link recorder audio into mp4mux")
            except Exception as exc:  # noqa: BLE001
                self.error = f"recorder audio setup failed: {exc}"
                pipeline.set_state(Gst.State.NULL)
                return False

        self._pipeline = pipeline
        self._mux = mux
        self._video_src = video
        self._audio_src = audio_src
        self._bus = pipeline.get_bus()
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.error = "recorder pipeline failed to start"
            pipeline.set_state(Gst.State.NULL)
            self._clear_runtime()
            return False
        return True

    # ------------------------------------------------------------------- push
    def push_video(self, sample) -> None:
        self._push("video", sample, self._video_step)

    def push_audio(self, sample) -> None:
        if self._audio_src is not None:
            self._push("audio", sample, self._audio_step)

    def _push(self, stream: str, sample, default_step: int) -> None:
        if self._pipeline is None or self.error:
            return
        source = self._video_src if stream == "video" else self._audio_src
        if source is None:
            return
        buf = sample.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return
        pts = int(buf.pts)
        dts = int(buf.dts) if buf.dts != Gst.CLOCK_TIME_NONE else None
        duration = (int(buf.duration) if buf.duration not in (0, Gst.CLOCK_TIME_NONE)
                    else default_step)
        last_in = self._last_in[stream]
        if last_in is not None and pts < last_in:
            # A rebuilt canvas restarts its encoder at PTS 0.  Continue from the
            # last written sample instead of letting mp4mux reject the stream.
            base = (self._last_out[stream] or 0) + duration
            self._offset[stream] = base - pts
        out_pts = pts + self._offset[stream]
        last_out = self._last_out[stream]
        if last_out is not None and out_pts <= last_out:
            self._offset[stream] += last_out + duration - out_pts
            out_pts = pts + self._offset[stream]
        new = buf.copy_deep()
        new.pts = out_pts
        if dts is not None:
            new.dts = dts + self._offset[stream]
        new.duration = duration
        self._last_in[stream] = pts
        self._last_out[stream] = out_pts
        try:
            # Push the sample, including the encoder's exact caps.  A hardcoded
            # appsrc caps string missed fields (parsed/profile) and made
            # h264parse reject the stream with not-negotiated.
            caps = sample.get_caps()
            if caps is None:
                self.error = f"{stream} sample has no caps"
                return
            new_sample = Gst.Sample.new(
                new, caps, sample.get_segment(), None)
            result = source.emit("push-sample", new_sample)
            if result not in (Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING, None):
                self.error = f"{stream} push failed: {result}"
        except Exception as exc:  # noqa: BLE001
            self.error = f"{stream} push failed: {exc}"

    # ------------------------------------------------------------------- stop
    def stop(self) -> dict:
        pipeline = self._pipeline
        if pipeline is None:
            return {"path": self.path, "bytes": 0, "ok": False,
                    "error": self.error or "recorder is not running"}
        for source in (self._video_src, self._audio_src):
            if source is None:
                continue
            try:
                source.emit("end-of-stream")
            except Exception:
                pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self._eos:
            if self._bus is None:
                break
            msg = self._bus.timed_pop_filtered(
                100 * Gst.MSECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if msg is None:
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                self.error = f"{err.message} ({debug})"
            elif msg.type == Gst.MessageType.EOS:
                self._eos = True
        pipeline.set_state(Gst.State.NULL)
        file = Path(self.path)
        size = file.stat().st_size if file.exists() else 0
        ok = bool(size > 0 and not self.error)
        result = {"path": self.path, "bytes": size, "ok": ok,
                  "error": self.error}
        self._clear_runtime()
        return result

    def _clear_runtime(self) -> None:
        self._pipeline = None
        self._mux = None
        self._video_src = None
        self._audio_src = None
        self._bus = None
        self._eos = False
