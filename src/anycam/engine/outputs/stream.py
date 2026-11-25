"""RTMP publishing: encode in the canvas graph, publish in its own pipeline."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from ... import config  # noqa: E402
from ...streaming import is_srt  # noqa: E402
from ...sources import (caps_filter, make)
from ...engine.encoders import (ENCODER_FACTORIES, ENCODER_PARSERS, ENCODER_BITRATES, ENCODER_LABELS, encoder_bitrate, nvenc_available, _probe_nvenc, _va_encodes, va_factory, va_present, EncoderPlan, _requested_codec, _wants_va, encoder_plan, recording_codec, EncodedFeed, force_keyframe)  # noqa: E402,F401
from ...engine.report import (EngineReport)  # noqa: E402,F401
from ...engine.audio_helpers import (MIC_DENOISE_LEVELS, mic_dsp_wanted, describe_mic_dsp, mic_dsp_elements)  # noqa: E402,F401
from ...engine.canvas_helpers import (GL_MIXER_LATENCY_NS, canvas_requested, gl_canvas_problem, _empty_drawing)  # noqa: E402,F401

Gst.init(None)


# Video encoders stamp DTS/PTS a long way ahead (1000 hours) so that DTS can
# never be negative.  FLV ignores the base; an MPEG-TS muxer on a live clock would
# wait that long before sending anything, so the SRT leg takes it off again.
ENCODER_TS_OFFSET = 1000 * 3600 * Gst.SECOND


def without_encoder_offset(sample):
    """A copy of ``sample`` whose timestamps no longer carry the encoder's offset."""
    buffer = sample.get_buffer()
    if buffer is None or buffer.pts == Gst.CLOCK_TIME_NONE or buffer.pts < ENCODER_TS_OFFSET // 2:
        return sample
    copy = buffer.copy()
    copy.pts = buffer.pts - ENCODER_TS_OFFSET
    if buffer.dts != Gst.CLOCK_TIME_NONE and buffer.dts >= ENCODER_TS_OFFSET:
        copy.dts = buffer.dts - ENCODER_TS_OFFSET
    else:
        copy.dts = Gst.CLOCK_TIME_NONE
    segment = Gst.Segment()
    segment.init(Gst.Format.TIME)
    return Gst.Sample.new(copy, sample.get_caps(), segment, None)


@dataclass
class Publisher:
    """One live publish leg (see ``StreamMixin._make_publisher``)."""

    url: str
    pipeline: Gst.Pipeline
    bus: Gst.Bus
    video_src: Gst.Element
    audio_src: Gst.Element | None
    feed: EncodedFeed
    first_data: float = 0.0
    srt: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancelled: bool = False


@dataclass
class RtmpState:
    """What the window shows for one destination, and its reconnect clock."""

    url: str = ""
    state: str = "off"            # off | connecting | live | waiting | failed
    error: str = ""
    failures: int = 0
    restarts: int = 0
    restart_at: float = 0.0
    live_since: float = 0.0
    publisher: Publisher | None = None
    failed_flag: bool = False


class StreamMixin:
    """RTMP publishing: encode in the canvas graph, publish in its own pipeline."""

    @staticmethod
    def _set_leaky(element: Gst.Element) -> None:
        """Drop old buffers instead of blocking (GstAppLeakyType.DOWNSTREAM=2)."""
        try:
            element.set_property("leaky-type", 2)
        except (TypeError, ValueError):
            pass

    def _build_rtmp(self) -> None:
        """Encode in the canvas graph, publish in a separate pipeline.

        The publish leg blocks its streaming thread while it talks to the
        server.  In the single graph that back-pressure reached the tee and
        froze preview, recording and the virtual camera (audit B9/B11).  The
        appsinks below drop frames instead, so a dead server can only starve
        its own output pipeline.  ``rtmp2sink`` is preferred when available;
        the older ``rtmpsink`` remains a fallback for minimal installs.
        """
        url = self.rtmp
        if not url:
            return
        # Check before attaching the canvas branch: a missing plugin used to
        # abort the whole build and leave a recording session running with no
        # preview.  Report it and keep the other outputs alive instead.
        srt = is_srt(url)
        sink_factory = ("srtsink" if srt else
                        "rtmp2sink" if Gst.ElementFactory.find("rtmp2sink") else "rtmpsink")
        # FLV/RTMP carries H.264/AAC, not HEVC or AV1.  A saved H.265/AV1
        # recording preference therefore selects H.264 NVENC for this branch.
        stream_w, stream_h, _stream_fps = self._output_format("rtmp")
        plan = encoder_plan(self._encoder_preference(), purpose="rtmp",
                            width=stream_w, height=stream_h)
        missing = [name for name in (sink_factory, "mpegtsmux" if srt else "flvmux",
                                     plan.factory, plan.parser)
                   if Gst.ElementFactory.find(name) is None]
        if missing:
            self.report.rtmp_error = (
                "stream output unavailable: missing GStreamer element(s): "
                + ", ".join(missing)
            )
            return
        queue = make("queue", "queue_rtmp", max_size_buffers=4,
                     max_size_time=200_000_000)
        # Downstream leak: the encoder can never push back into the compositor.
        queue.set_property("leaky", 2)
        scale_path = self._output_scale_elements("rtmp", "rtmp")
        pixel_path = self._encoder_input(plan.hardware, "rtmp", plan.factory)
        enc = self._make_encoder(plan, "rtmp_encoder", purpose="rtmp")
        parse = self._make_parser(plan, "rtmp_parse")
        h264 = caps_filter("video/x-h264,stream-format=avc,alignment=au",
                           "rtmp_h264")
        video_sink = make("appsink", "rtmp_video_sink", emit_signals=True,
                          max_buffers=8, drop=False, sync=False)
        video_sink.set_property("async", False)
        video_elements = [queue, *scale_path, *pixel_path, enc, parse, h264, video_sink]
        self._on(video_sink, "new-sample", self._on_rtmp_video_sample)
        video_tee_pad = self._attach_tee_branch(self.tee, video_elements,
                                                count_branch=True)
        self.report.rtmp_encoder = plan.factory
        self.report.rtmp_encoder_note = plan.note

        audio_sink = None
        audio_elements: list[Gst.Element] = []
        audio_tee_pad = None
        tee = self._audio_tee()
        if tee is not None:
            audio_queue = make("queue", "rtmp_audio_queue",
                               max_size_buffers=2, max_size_time=100_000_000)
            audio_queue.set_property("leaky", 2)
            aac = caps_filter("audio/mpeg,mpegversion=4,stream-format=raw",
                              "rtmp_aac")
            audio_sink = make("appsink", "rtmp_audio_sink", emit_signals=True,
                              max_buffers=2, drop=True, sync=False)
            audio_sink.set_property("async", False)
            audio_elements = [audio_queue, aac, audio_sink]
            self._on(audio_sink, "new-sample", self._on_rtmp_audio_sample)
            try:
                audio_tee_pad = self._attach_tee_branch(tee, audio_elements)
            except Exception:
                self._cleanup_rtmp_canvas_branch(
                    video_elements, video_tee_pad, [], None, tee)
                raise

        self._rtmp_sink_factory = sink_factory
        self._rtmp_with_audio = audio_sink is not None
        self._rtmp_encoder = enc
        self._rtmp_video_elements = list(video_elements)
        self._rtmp_video_tee_pad = video_tee_pad
        self._rtmp_audio_elements = list(audio_elements)
        self._rtmp_audio_tee_pad = audio_tee_pad
        self._rtmp_audio_tee = tee
        try:
            publisher = self._make_publisher(url, "anycam-rtmp")
        except Exception:
            self._rtmp_encoder = None
            self._rtmp_video_elements = []
            self._rtmp_video_tee_pad = None
            self._rtmp_audio_elements = []
            self._rtmp_audio_tee_pad = None
            self._rtmp_audio_tee = None
            self._cleanup_rtmp_canvas_branch(
                video_elements, video_tee_pad, audio_elements, audio_tee_pad, tee)
            raise
        self._adopt_primary(publisher)
        self._rtmp_state.url = url
        self._rtmp_state.state = "connecting"
        self.report.rtmp_url = url
        for extra_url in self.rtmp_extra:
            self._add_extra_target(extra_url)
        self._publish_report()

    def attach_rtmp(self, url: str, extra: list[str] | tuple[str, ...] = ()) -> bool:
        """Start streaming from a running canvas without rebuilding it."""
        if (self.pipeline is None or self.tee is None
                or getattr(self, "_stopped", False) or not url):
            return False
        if getattr(self, "_rtmp_video_tee_pad", None) is not None or self._rtmp_pipeline is not None:
            self.detach_rtmp()
        self.rtmp = url
        self.rtmp_extra = [u for u in extra if u and u != url]
        self.report._rtmp_extra_urls = list(self.rtmp_extra)
        self.report.rtmp_error = ""
        self._rtmp_state = RtmpState()
        self._rtmp_extras = []
        try:
            self._build_rtmp()
        except Exception as exc:  # noqa: BLE001
            self.report.rtmp_error = config.redact_text(
                f"stream output could not start: {exc}", url, *self.rtmp_extra)
            self.rtmp = ""
            self.rtmp_extra = []
            return False
        publisher = getattr(self, "_rtmp_publisher", None)
        if publisher is None:
            self.rtmp = ""
            self.rtmp_extra = []
            return False
        clock = self.pipeline.get_clock() or Gst.SystemClock.obtain()
        if self._canvas_base is None:
            self._canvas_base = self.pipeline.get_base_time()
        publisher.feed.skipping = True
        force_keyframe(self._rtmp_encoder)
        self._apply_canvas_clock(publisher.pipeline, clock)
        self._start_async(publisher)
        with self._get_rtmp_lock():
            extras = list(self._rtmp_extras)
        for state in extras:
            if state.publisher is not None:
                state.publisher.feed.skipping = True
                self._apply_canvas_clock(state.publisher.pipeline, clock)
                self._start_async(state.publisher)
        return True

    @staticmethod
    def _unlink_tee_pad_safely(pad: Gst.Pad | None) -> None:
        """Unlink a tee src pad under a blocking probe so no buffer hits not-linked."""
        if pad is None or not pad.is_linked():
            return
        parent = pad.get_parent_element()
        playing = False
        if parent is not None:
            _ret, state, pending = parent.get_state(0)
            playing = state == Gst.State.PLAYING or pending == Gst.State.PLAYING
        if not playing:
            peer = pad.get_peer()
            if peer is not None:
                pad.unlink(peer)
            return
        done = threading.Event()

        def on_idle(target_pad, _info):
            peer = target_pad.get_peer()
            if peer is not None:
                target_pad.unlink(peer)
            done.set()
            return Gst.PadProbeReturn.REMOVE

        probe = pad.add_probe(Gst.PadProbeType.IDLE, on_idle)
        if not done.wait(0.4):
            if probe:
                try:
                    pad.remove_probe(probe)
                except Exception:  # noqa: BLE001
                    pass
            peer = pad.get_peer()
            if peer is not None:
                pad.unlink(peer)

    def detach_rtmp(self) -> bool:
        """Stop streaming and remove the RTMP encoder branch without rebuilding the canvas."""
        if self.pipeline is None:
            return False
        self._teardown_rtmp(everything=True)
        video_elements = list(getattr(self, "_rtmp_video_elements", ()))
        video_pad = getattr(self, "_rtmp_video_tee_pad", None)
        audio_elements = list(getattr(self, "_rtmp_audio_elements", ()))
        audio_pad = getattr(self, "_rtmp_audio_tee_pad", None)
        audio_tee = getattr(self, "_rtmp_audio_tee", None)
        self._rtmp_video_elements = []
        self._rtmp_video_tee_pad = None
        self._rtmp_audio_elements = []
        self._rtmp_audio_tee_pad = None
        self._rtmp_audio_tee = None
        self._rtmp_encoder = None
        self.rtmp = ""
        self.rtmp_extra = []
        self._rtmp_state = RtmpState()
        self._rtmp_extras = []
        self.report.rtmp_url = ""
        self.report.rtmp_targets = []
        self.report.rtmp_error = ""
        self.report._rtmp_extra_urls = []
        if video_pad is not None or audio_pad is not None:
            self._unlink_tee_pad_safely(video_pad)
            self._unlink_tee_pad_safely(audio_pad)
            self._cleanup_rtmp_canvas_branch(
                video_elements, video_pad, audio_elements, audio_pad, audio_tee)
        return True

    def _cleanup_rtmp_canvas_branch(
        self,
        video_elements: list[Gst.Element],
        video_tee_pad: Gst.Pad | None,
        audio_elements: list[Gst.Element],
        audio_tee_pad: Gst.Pad | None,
        audio_tee: Gst.Element | None,
    ) -> None:
        """Undo a canvas RTMP encoder branch when publisher creation fails."""
        members = set(video_elements) | set(audio_elements)
        keep_handlers = []
        for obj, handler in getattr(self, "_signal_handlers", ()):
            if obj in members:
                try:
                    obj.disconnect(handler)
                except Exception:  # noqa: BLE001
                    pass
            else:
                keep_handlers.append((obj, handler))
        if hasattr(self, "_signal_handlers"):
            self._signal_handlers = keep_handlers
        for element in (*reversed(video_elements), *reversed(audio_elements)):
            element.set_state(Gst.State.NULL)
        if video_tee_pad is not None and self.tee is not None:
            peer = video_tee_pad.get_peer()
            if peer is not None:
                video_tee_pad.unlink(peer)
            try:
                self.tee.release_request_pad(video_tee_pad)
            except Exception:  # noqa: BLE001
                pass
        if audio_tee_pad is not None and audio_tee is not None:
            peer = audio_tee_pad.get_peer()
            if peer is not None:
                audio_tee_pad.unlink(peer)
            try:
                audio_tee.release_request_pad(audio_tee_pad)
            except Exception:  # noqa: BLE001
                pass
        if self.pipeline is not None:
            for element in (*reversed(video_elements), *reversed(audio_elements)):
                if element.get_parent() is self.pipeline:
                    self.pipeline.remove(element)
        if video_tee_pad is not None:
            self._branch_count = max(0, self._branch_count - 1)

    # ------------------------------------------------------------ publishers
    def _make_publisher(self, url: str, name: str, prefix: str = "rtmp") -> "Publisher":
        """One publish leg: appsrc(s) -> flvmux -> rtmp sink, in its own pipeline."""
        output = Gst.Pipeline.new(name)
        video_src = make("appsrc", f"{prefix}_video_src", format=Gst.Format.TIME,
                         is_live=True)
        # MPEG-TS leaves the caps unset: they come, with the encoder's
        # codec_data, from the first sample (h264parse needs that to convert).
        if not is_srt(url):
            video_src.set_property("caps", Gst.Caps.from_string(
                "video/x-h264,stream-format=avc,alignment=au"))
        video_src.set_property("max-buffers", 8)
        video_src.set_property("block", False)
        srt = is_srt(url)
        if srt:
            # MPEG-TS wants Annex-B H.264 and ADTS AAC, not the FLV forms.
            # 7 TS packets = 1316 bytes: SRT's live mode drops larger messages,
            # and an I-frame is far bigger than that.
            mux = make("mpegtsmux", f"{prefix}_mux", alignment=7)
            sink_factory = "srtsink"
            sink = make(sink_factory, f"{prefix}_sink", uri=url, sync=False)
        else:
            mux = make("flvmux", f"{prefix}_mux", streamable=True)
            sink_factory = (
                self._rtmp_sink_factory
                if self._rtmp_sink_factory in ("rtmp2sink", "rtmpsink")
                else ("rtmp2sink" if Gst.ElementFactory.find("rtmp2sink") else "rtmpsink")
            )
            sink = make(sink_factory, f"{prefix}_sink", location=url, sync=False)
        output.add(video_src)
        output.add(mux)
        output.add(sink)
        if srt:
            parse = make("h264parse", f"{prefix}_ts_parse", config_interval=-1)
            annexb = caps_filter("video/x-h264,stream-format=byte-stream,alignment=au",
                                 f"{prefix}_annexb")
            output.add(parse)
            output.add(annexb)
            if not (video_src.link(parse) and parse.link(annexb) and annexb.link(mux)):
                raise RuntimeError("could not link SRT video into mpegtsmux")
        elif video_src.get_static_pad("src").link(mux.request_pad_simple("video")) != Gst.PadLinkReturn.OK:
            raise RuntimeError("could not link RTMP video into flvmux")
        audio_src = None
        if self._rtmp_with_audio:
            audio_src = make("appsrc", f"{prefix}_audio_src", format=Gst.Format.TIME,
                             is_live=True)
            if not srt:
                audio_src.set_property("caps", Gst.Caps.from_string(
                    "audio/mpeg,mpegversion=4,stream-format=raw"))
            audio_src.set_property("max-buffers", 2)
            audio_src.set_property("block", False)
            self._set_leaky(audio_src)
            output.add(audio_src)
            if srt:
                aparse = make("aacparse", f"{prefix}_ts_aparse")
                adts = caps_filter("audio/mpeg,mpegversion=4,stream-format=adts",
                                   f"{prefix}_adts")
                output.add(aparse)
                output.add(adts)
                if not (audio_src.link(aparse) and aparse.link(adts) and adts.link(mux)):
                    raise RuntimeError("could not link SRT audio into mpegtsmux")
            else:
                audio_pad = mux.request_pad_simple("audio")
                if (audio_pad is None
                        or audio_src.get_static_pad("src").link(audio_pad) != Gst.PadLinkReturn.OK):
                    raise RuntimeError("could not link RTMP audio into flvmux")
        if not mux.link(sink):
            raise RuntimeError(f"could not link mux into {sink_factory}")
        self._apply_canvas_clock(output)
        feed = EncodedFeed(video_src, lambda: force_keyframe(self._rtmp_encoder))
        publisher = Publisher(url, output, output.get_bus(), video_src, audio_src, feed, srt=srt)
        pad = sink.get_static_pad("sink")
        if pad is not None:
            def on_data(_pad, _info, publisher=publisher):
                publisher.first_data = time.monotonic()
                return Gst.PadProbeReturn.REMOVE

            pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, on_data)
        return publisher

    def _get_rtmp_lock(self) -> threading.RLock:
        lock = getattr(self, "_rtmp_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._rtmp_lock = lock
        return lock

    def _adopt_primary(self, publisher: "Publisher") -> None:
        """Expose the first target through the long-standing ``_rtmp_*`` attributes."""
        with self._get_rtmp_lock():
            self._rtmp_publisher = publisher
            self._rtmp_pipeline = publisher.pipeline
            self._rtmp_bus = publisher.bus
            self._rtmp_appsrc = publisher.video_src
            self._rtmp_audio_appsrc = publisher.audio_src
            self._rtmp_feed = publisher.feed
            self._rtmp_failed = False
            self._rtmp_state.publisher = publisher

    def _add_extra_target(self, url: str) -> None:
        state = RtmpState(url=url, state="connecting")
        with self._get_rtmp_lock():
            self._rtmp_extras.append(state)
            index = len(self._rtmp_extras)
        try:
            publisher = self._make_publisher(
                url, f"anycam-rtmp-{index}", f"rtmpx{index}")
            with self._get_rtmp_lock():
                state.publisher = publisher
        except Exception as exc:  # noqa: BLE001 - one bad target must not stop the rest
            with self._get_rtmp_lock():
                state.state = "failed"
                state.error = config.redact_text(f"could not start: {exc}", url)

    def _apply_canvas_clock(self, pipeline: Gst.Pipeline, clock=None) -> None:
        if self._canvas_base is not None:
            pipeline.use_clock(clock or Gst.SystemClock.obtain())
            pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
            pipeline.set_base_time(self._canvas_base)

    def start_extra_targets(self, clock=None) -> None:
        with self._get_rtmp_lock():
            extras = list(self._rtmp_extras)
        for state in extras:
            if state.publisher is not None:
                self._apply_canvas_clock(state.publisher.pipeline, clock)
                state.publisher.pipeline.set_state(Gst.State.PLAYING)

    def _on_rtmp_video_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        with self._get_rtmp_lock():
            primary = (self._rtmp_feed, self._rtmp_publisher) if not self._rtmp_failed else (None, None)
            extras = [state.publisher for state in self._rtmp_extras
                      if state.publisher is not None and not state.failed_flag]
        feed, publisher = primary
        if feed is not None:
            feed.push(without_encoder_offset(sample)
                      if publisher is not None and publisher.srt else sample)
        for extra_pub in extras:
            extra_pub.feed.push(
                without_encoder_offset(sample) if extra_pub.srt else sample)
        return Gst.FlowReturn.OK

    def _on_rtmp_audio_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        with self._get_rtmp_lock():
            appsrc = self._rtmp_audio_appsrc if not self._rtmp_failed else None
            extra_srcs = [state.publisher.audio_src for state in self._rtmp_extras
                          if state.publisher is not None
                          and state.publisher.audio_src is not None
                          and not state.failed_flag]
        if appsrc is not None:
            appsrc.emit("push-sample", sample)
        for extra_src in extra_srcs:
            extra_src.emit("push-sample", sample)
        return Gst.FlowReturn.OK

    def _poll_rtmp_bus(self) -> None:
        """Collect publish errors, reconnect with backoff, keep the report current."""
        now = time.monotonic()
        if self._rtmp_bus is not None:
            error = self._pop_error(self._rtmp_bus)
            if error:
                self.report.rtmp_error = config.redact_text(error, self.rtmp)
                self._rtmp_failed = True
                self._rtmp_state.error = self.report.rtmp_error
                self._teardown_rtmp()
                self._schedule(self._rtmp_state, now)
        state = self._rtmp_state
        publisher = getattr(self, "_rtmp_publisher", None)
        if publisher is not None and publisher.first_data and state.state != "live":
            state.state = "live"
            state.live_since = publisher.first_data
            self.report.rtmp_error = ""
        if state.restart_at and now >= state.restart_at and not self._stopped:
            self._restart_primary(now)
        for extra in self._rtmp_extras:
            self._poll_extra(extra, now)
        self._publish_report()

    @staticmethod
    def _pop_error(bus) -> str:
        message = ""
        while True:
            msg = bus.pop_filtered(Gst.MessageType.ERROR)
            if msg is None:
                break
            err, debug = msg.parse_error()
            message = f"{err.message} ({debug})"
        return message

    #: Reconnect delays double from RTMP_RETRY_FIRST_S up to RTMP_RETRY_MAX_S; a
    #: stream that stayed up this long starts again from the first delay.
    RTMP_RETRY_FIRST_S = 1.0
    RTMP_RETRY_MAX_S = 30.0
    RTMP_HEALTHY_S = 10.0

    def _schedule(self, state: "RtmpState", now: float) -> None:
        if state.live_since and now - state.live_since >= self.RTMP_HEALTHY_S:
            state.failures = 0
        delay = min(self.RTMP_RETRY_MAX_S, self.RTMP_RETRY_FIRST_S * (2 ** state.failures))
        state.failures += 1
        state.live_since = 0.0
        state.state = "waiting"
        state.restart_at = now + delay

    def _restart_primary(self, now: float) -> None:
        state = self._rtmp_state
        state.restart_at = 0.0
        try:
            publisher = self._make_publisher(self.rtmp, f"anycam-rtmp-r{state.restarts + 1}",
                                             f"rtmpr{state.restarts + 1}")
        except Exception as exc:  # noqa: BLE001
            state.error = config.redact_text(f"could not reconnect: {exc}", self.rtmp)
            self.report.rtmp_error = state.error
            self._schedule(state, now)
            return
        publisher.feed.skipping = True     # begin on a keyframe
        force_keyframe(self._rtmp_encoder)
        state.restarts += 1
        state.state = "connecting"
        self._adopt_primary(publisher)
        self._start_async(publisher)

    @staticmethod
    def _start_async(publisher_or_pipeline) -> None:
        """set_state(PLAYING) can sit inside a socket call: keep it off this thread."""
        if isinstance(publisher_or_pipeline, Publisher):
            publisher = publisher_or_pipeline

            def start() -> None:
                with publisher.lock:
                    if publisher.cancelled:
                        return
                    publisher.pipeline.set_state(Gst.State.PLAYING)

            threading.Thread(target=start, daemon=True,
                             name="anycam-rtmp-start").start()
            return
        threading.Thread(
            target=lambda: publisher_or_pipeline.set_state(Gst.State.PLAYING),
            daemon=True, name="anycam-rtmp-start").start()

    def _poll_extra(self, state: "RtmpState", now: float) -> None:
        publisher = state.publisher
        if publisher is not None:
            error = self._pop_error(publisher.bus)
            if error:
                state.error = config.redact_text(error, state.url)
                state.failed_flag = True
                self._discard(publisher)
                state.publisher = None
                self._schedule(state, now)
            elif publisher.first_data and state.state != "live":
                state.state = "live"
                state.live_since = publisher.first_data
                state.error = ""
        if state.restart_at and now >= state.restart_at and not self._stopped:
            state.restart_at = 0.0
            try:
                publisher = self._make_publisher(
                    state.url, f"anycam-rtmp-x{id(state) % 10000}-{state.restarts + 1}",
                    f"rtmpx{id(state) % 10000}r{state.restarts + 1}")
            except Exception as exc:  # noqa: BLE001
                state.error = config.redact_text(f"could not reconnect: {exc}", state.url)
                self._schedule(state, now)
                return
            publisher.feed.skipping = True
            force_keyframe(self._rtmp_encoder)
            state.publisher, state.failed_flag = publisher, False
            state.restarts += 1
            state.state = "connecting"
            self._start_async(publisher)

    @staticmethod
    def _discard(publisher: "Publisher") -> None:
        def shutdown() -> None:
            lock = getattr(publisher, "lock", None)
            if lock is not None:
                with lock:
                    publisher.cancelled = True
                    try:
                        publisher.pipeline.set_state(Gst.State.NULL)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                try:
                    publisher.pipeline.set_state(Gst.State.NULL)
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=shutdown, daemon=True, name="anycam-rtmp-teardown").start()

    def _publish_report(self) -> None:
        """Per-destination status for the window (URLs redacted)."""
        rows = []
        for state in (self._rtmp_state, *self._rtmp_extras):
            if not state.url:
                continue
            rows.append({
                "url": config.redact_url(state.url), "state": state.state,
                "error": config.redact_text(state.error, state.url),
                "restarts": state.restarts,
                "retry_in_s": max(0.0, round(state.restart_at - time.monotonic(), 1))
                if state.restart_at else 0.0})
        self.report.rtmp_targets = rows

    def _teardown_rtmp(self, everything: bool = False) -> None:
        """Stop publishing without letting a stuck socket block the caller.

        ``everything`` also ends the extra destinations and cancels reconnects
        (the engine is going away); otherwise only the primary leg is dropped.
        """
        with self._get_rtmp_lock():
            if everything:
                extras_to_drop = []
                for state in self._rtmp_extras:
                    if state.publisher is not None:
                        extras_to_drop.append(state.publisher)
                        state.publisher = None
                    state.restart_at = 0.0
                self._rtmp_state.restart_at = 0.0
            else:
                extras_to_drop = []
            pipeline = self._rtmp_pipeline
            publisher = getattr(self, "_rtmp_publisher", None)
            if pipeline is not None:
                self._rtmp_pipeline = None
                self._rtmp_bus = None
                self._rtmp_appsrc = None
                self._rtmp_audio_appsrc = None
                self._rtmp_feed = None
                # Keep _rtmp_encoder: it lives in the canvas graph, and _restart_primary
                # needs it for force_keyframe (audit item 6).  Clear publisher refs so
                # a stale first_data cannot flip state back to "live" (item 5).
                self._rtmp_publisher = None
                self._rtmp_state.publisher = None
        for extra_pub in extras_to_drop:
            self._discard(extra_pub)
        if pipeline is None:
            return

        def shutdown() -> None:
            lock = getattr(publisher, "lock", None)
            if lock is not None:
                with lock:
                    publisher.cancelled = True
                    try:
                        pipeline.send_event(Gst.Event.new_eos())
                    except Exception:
                        pass
                    try:
                        pipeline.set_state(Gst.State.NULL)
                    except Exception:
                        pass
                return
            try:
                pipeline.send_event(Gst.Event.new_eos())
            except Exception:
                pass
            try:
                # Can block while rtmpsink is inside a socket call; keep it
                # off the UI thread until the M1 controller owns outputs.
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass

        threading.Thread(target=shutdown, daemon=True,
                         name="anycam-rtmp-teardown").start()
