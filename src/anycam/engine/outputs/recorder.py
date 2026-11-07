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
# GstVideoEncoder stamps its output 1000 hours ahead so DTS can never go
# negative; audio is not.  Handing both to one muxer put video 1000 hours
# "later" than audio, so the muxer kept waiting for audio to catch up and
# wrote a handful of video frames.  The bias is removed before muxing.
ENCODER_PTS_BIAS = 3600 * 1000 * Gst.SECOND
VIDEO_PARSERS = {"h264": "h264parse", "h265": "h265parse",
                 "av1": "av1parse"}


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
        self.codec = "h264"
        self._video_step = Gst.SECOND // 30
        self._audio_step = 1024 * Gst.SECOND // 48000
        self._last_in: dict[str, int | None] = {"video": None, "audio": None}
        self._last_out: dict[str, int | None] = {"video": None, "audio": None}
        self._offset: dict[str, int] = {"video": 0, "audio": 0}
        self._eos = False
        # Set while stop() finishes the file: a frame still in flight from the
        # canvas must not be pushed after end-of-stream (or count as a failure).
        self._closing = False
        # Frame size of the first video sample.  mp4mux writes one sample
        # description, so frames of another size (a canvas resized while the
        # file is open) cannot be added to it; they are counted and dropped,
        # and the owner starts a new file instead.
        self.size: tuple[int, int] | None = None
        self.rejected_frames = 0
        # Size the file was started for (the owner compares it with the canvas).
        self.requested_size: tuple[int, int] | None = None
        # Set by the engine: asks the encoder for a keyframe after the
        # recorder had to drop an encoded frame.
        self.request_keyframe = None
        # Dropping a delta frame corrupts the picture until the next keyframe,
        # so after a drop everything is skipped up to the next keyframe.
        self._skipping = False
        # Timeline of the canvas that is feeding us now (see begin_epoch).
        self._epoch_base: int | None = None

    @property
    def active(self) -> bool:
        return self._pipeline is not None

    @property
    def has_audio(self) -> bool:
        """Whether the file has an audio track the canvas must keep feeding.

        A muxer with an audio pad that stops receiving data stalls the video
        too, so a canvas rebuilt without audio would freeze the recording.
        """
        return self._audio_src is not None

    # ------------------------------------------------------------------ start
    def start(self, path: str, width: int, height: int, fps: int, *,
              audio: bool = False, codec: str = "h264") -> bool:
        """Start a session.  Requested pads exist before the first sample."""
        if self.active:
            self.stop()
        self.path = str(path)
        self.error = ""
        self.codec = codec if codec in VIDEO_PARSERS else "h264"
        self._eos = False
        self._closing = False
        self._last_in = {"video": None, "audio": None}
        self._last_out = {"video": None, "audio": None}
        self._offset = {"video": 0, "audio": 0}
        self.size = None
        self.requested_size = (int(width), int(height))
        self.rejected_frames = 0
        self._skipping = False
        self._epoch_base = None
        self._video_step = max(1, Gst.SECOND // max(1, int(fps)))
        self._audio_step = 1024 * Gst.SECOND // 48000
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        pipeline = Gst.Pipeline.new("anycam-recorder")
        video = _make("appsrc", "record_video_src")
        video.set_property("format", Gst.Format.TIME)
        video.set_property("is-live", True)
        video.set_property("max-buffers", 8)
        video.set_property("block", False)
        parse_factory = VIDEO_PARSERS[self.codec]
        parse_props = {"config_interval": -1} if parse_factory != "av1parse" else {}
        parse = _make(parse_factory, "record_video_parse", **parse_props)
        mux = _make("mp4mux", "record_mux", fragment_duration=500)
        sink = _make("filesink", "record_sink", location=self.path, sync=False)
        for element in (video, parse, mux, sink):
            pipeline.add(element)
        if not video.link(parse):
            self.error = f"could not link recorder video into {parse_factory}"
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
    def begin_epoch(self) -> None:
        """A new canvas is about to feed this file (its timestamps restart).

        Video and audio must continue from the same point.  Shifting each
        stream by its own last timestamp erased any stretch in which one of
        them was absent (microphone toggled off, say), so the audio that came
        back played early for the rest of the file.  Both streams now share
        one offset, taken from the end of whichever stream ran longest.
        """
        ends = [self._last_out[stream] + step
                for stream, step in (("video", self._video_step),
                                     ("audio", self._audio_step))
                if self._last_out[stream] is not None]
        # The first canvas starts the file at 0 as well.  Attaching to a canvas
        # that had been running for seconds kept its running time, so the two
        # streams entered the muxer at different points: it waited on video,
        # the leaky audio input dropped frames, and the file lost ~2 s of sound.
        self._epoch_base = max(ends) if ends else 0

    def push_video(self, sample) -> None:
        self._push("video", sample, self._video_step)

    def push_audio(self, sample) -> None:
        if self._audio_src is not None:
            self._push("audio", sample, self._audio_step)

    def _push(self, stream: str, sample, default_step: int) -> None:
        if self._pipeline is None or self.error or self._closing:
            return
        source = self._video_src if stream == "video" else self._audio_src
        if source is None:
            return
        buf = sample.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return
        if stream == "video" and not self._accept_size(sample):
            return
        if stream == "video" and not self._admit_video(source, buf):
            return
        bias = int(buf.pts) // ENCODER_PTS_BIAS * ENCODER_PTS_BIAS
        pts = int(buf.pts) - bias
        if self._epoch_base is not None:
            # First sample of a new canvas: one offset for both streams.
            shared = self._epoch_base - pts
            self._offset = {"video": shared, "audio": shared}
            self._last_in = {"video": None, "audio": None}
            self._epoch_base = None
        dts = (int(buf.dts) - bias
               if buf.dts != Gst.CLOCK_TIME_NONE and int(buf.dts) >= bias
               else None)
        duration = (int(buf.duration) if buf.duration not in (0, Gst.CLOCK_TIME_NONE)
                    else default_step)
        last_in = self._last_in[stream]
        if last_in is not None and pts < last_in:
            # A rebuilt canvas restarts its encoder at PTS 0.  Continue from the
            # last written sample instead of letting mp4mux reject the stream.
            base = (self._last_out[stream] or 0) + duration
            self._offset[stream] = base - pts
        out_pts = pts + self._offset[stream]
        if out_pts < 0:
            # This stream's first frame is a little older than the one that
            # set the shared offset; never hand the muxer a negative time.
            self._offset[stream] -= out_pts
            out_pts = 0
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
            # A plain time segment: the encoder's own one describes the biased
            # timeline this buffer no longer has.
            segment = Gst.Segment()
            segment.init(Gst.Format.TIME)
            new_sample = Gst.Sample.new(new, caps, segment, None)
            result = source.emit("push-sample", new_sample)
            if self._closing and result == Gst.FlowReturn.EOS:
                return      # lost the race with stop(): the file is complete
            if result not in (Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING, None):
                self.error = f"{stream} push failed: {result}"
        except Exception as exc:  # noqa: BLE001
            self.error = f"{stream} push failed: {exc}"

    def _admit_video(self, source, buf) -> bool:
        """Never let the appsrc leak an encoded frame by itself.

        Leaking a delta frame leaves later ones referring to a picture that is
        not in the file.  When the queue is full, drop on a keyframe boundary
        instead and ask the encoder for a fresh keyframe.
        """
        is_key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        if self._skipping:
            if not is_key:
                return False
            self._skipping = False
        try:
            level = int(source.get_property("current-level-buffers"))
            limit = int(source.get_property("max-buffers"))
        except (AttributeError, TypeError, ValueError):
            return True     # a source without a readable queue level
        if limit and level >= limit:
            self._skipping = True
            if self.request_keyframe is not None:
                try:
                    self.request_keyframe()
                except Exception:  # noqa: BLE001 - best effort
                    pass
            return False
        return True

    def _accept_size(self, sample) -> bool:
        """Whether this frame has the size the file was started with."""
        caps = sample.get_caps()
        structure = caps.get_structure(0) if caps is not None and caps.get_size() else None
        if structure is None or not (structure.has_field("width")
                                     and structure.has_field("height")):
            return True
        size = (int(structure.get_value("width")), int(structure.get_value("height")))
        if self.size is None:
            self.size = size
            return True
        if size != self.size:
            self.rejected_frames += 1
            return False
        return True

    # ------------------------------------------------------------------- stop
    def stop(self) -> dict:
        pipeline = self._pipeline
        if pipeline is None:
            return {"path": self.path, "bytes": 0, "ok": False,
                    "error": self.error or "recorder is not running"}
        self._closing = True
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
                break   # an errored pipeline never reaches EOS
            if msg.type == Gst.MessageType.EOS:
                self._eos = True
        pipeline.set_state(Gst.State.NULL)
        file = Path(self.path)
        size = file.stat().st_size if file.exists() else 0
        ok = bool(size > 0 and not self.error
                  and self._last_in.get("video") is not None)
        result = {"path": self.path, "bytes": size, "ok": ok,
                  "error": self.error, "rejected_frames": self.rejected_frames}
        self._clear_runtime()
        return result

    def _clear_runtime(self) -> None:
        self._pipeline = None
        self._mux = None
        self._video_src = None
        self._audio_src = None
        self._bus = None
        self._eos = False
        self._skipping = False
        self._closing = False
