"""RTMP publishing: encode in the canvas graph, publish in its own pipeline."""
from __future__ import annotations

import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from ... import config  # noqa: E402
from ...sources import (caps_filter, make)
from ...engine.encoders import (ENCODER_FACTORIES, ENCODER_PARSERS, ENCODER_BITRATES, ENCODER_LABELS, _NVENC_TTL_S, _nvenc_seen, encoder_bitrate, nvenc_available, _probe_nvenc, _va_encodes, va_factory, va_present, EncoderPlan, _requested_codec, _wants_va, encoder_plan, recording_codec, EncodedFeed, force_keyframe)  # noqa: E402,F401
from ...engine.report import (EngineReport)  # noqa: E402,F401
from ...engine.audio_helpers import (MIC_DENOISE_LEVELS, mic_dsp_wanted, describe_mic_dsp, mic_dsp_elements)  # noqa: E402,F401
from ...engine.canvas_helpers import (GL_MIXER_LATENCY_NS, canvas_requested, gl_canvas_problem, _empty_drawing)  # noqa: E402,F401

Gst.init(None)


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
        sink_factory = ("rtmp2sink" if Gst.ElementFactory.find("rtmp2sink")
                        else "rtmpsink")
        # FLV/RTMP carries H.264/AAC, not HEVC or AV1.  A saved H.265/AV1
        # recording preference therefore selects H.264 NVENC for this branch.
        plan = encoder_plan(self._encoder_preference(), purpose="rtmp")
        missing = [name for name in (sink_factory, "flvmux", plan.factory, plan.parser)
                   if Gst.ElementFactory.find(name) is None]
        if missing:
            self.report.rtmp_error = (
                "stream output unavailable: missing GStreamer element(s): "
                + ", ".join(missing)
            )
            return
        queue = self._branch("rtmp")
        # Downstream leak: the encoder can never push back into the compositor.
        queue.set_property("leaky", 2)
        pixel_path = self._encoder_input(plan.hardware, "rtmp", plan.factory)
        enc = self._make_encoder(plan, "rtmp_encoder", purpose="rtmp")
        parse = self._make_parser(plan, "rtmp_parse")
        h264 = caps_filter("video/x-h264,stream-format=avc,alignment=au",
                           "rtmp_h264")
        video_sink = make("appsink", "rtmp_video_sink", emit_signals=True,
                          max_buffers=8, drop=False, sync=False)
        self._add(*pixel_path, enc, parse, h264, video_sink)
        self._link([queue, *pixel_path, enc, parse, h264, video_sink])
        self._on(video_sink, "new-sample", self._on_rtmp_video_sample)
        self.report.rtmp_encoder = plan.factory
        self.report.rtmp_encoder_note = plan.note

        audio_sink = None
        tee = self._audio_tee()
        if tee is not None:
            audio_queue = make("queue", "rtmp_audio_queue",
                               max_size_buffers=2, max_size_time=100_000_000)
            audio_queue.set_property("leaky", 2)
            aac = caps_filter("audio/mpeg,mpegversion=4,stream-format=raw",
                              "rtmp_aac")
            audio_sink = make("appsink", "rtmp_audio_sink", emit_signals=True,
                              max_buffers=2, drop=True, sync=False)
            self._add(audio_queue, aac, audio_sink)
            self._link([audio_queue, aac, audio_sink])
            tpad = tee.request_pad_simple("src_%u")
            if tpad is None or tpad.link(audio_queue.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
                raise RuntimeError("could not tap shared audio for RTMP")
            self._on(audio_sink, "new-sample", self._on_rtmp_audio_sample)

        output = Gst.Pipeline.new("anycam-rtmp")
        video_src = make("appsrc", "rtmp_video_src", format=Gst.Format.TIME,
                         is_live=True)
        video_src.set_property(
            "caps", Gst.Caps.from_string("video/x-h264,stream-format=avc,alignment=au"))
        video_src.set_property("max-buffers", 8)
        video_src.set_property("block", False)
        mux = make("flvmux", "rtmp_mux", streamable=True)
        sink = make(sink_factory, "rtmp_sink", location=url, sync=False)
        output.add(video_src)
        output.add(mux)
        output.add(sink)
        if not video_src.link(mux):
            raise RuntimeError("could not link RTMP video into flvmux")
        self._rtmp_audio_appsrc = None
        if audio_sink is not None:
            audio_src = make("appsrc", "rtmp_audio_src", format=Gst.Format.TIME,
                             is_live=True)
            audio_src.set_property(
                "caps", Gst.Caps.from_string("audio/mpeg,mpegversion=4,stream-format=raw"))
            audio_src.set_property("max-buffers", 2)
            audio_src.set_property("block", False)
            self._set_leaky(audio_src)
            output.add(audio_src)
            if not audio_src.link(mux):
                raise RuntimeError("could not link RTMP audio into flvmux")
            self._rtmp_audio_appsrc = audio_src
        if not mux.link(sink):
            raise RuntimeError(f"could not link flvmux into {sink_factory}")
        self._rtmp_pipeline = output
        self._rtmp_bus = output.get_bus()
        self._rtmp_appsrc = video_src
        self._rtmp_encoder = enc
        self._rtmp_feed = EncodedFeed(video_src, lambda: force_keyframe(enc))
        self.report.rtmp_url = url

    def _on_rtmp_video_sample(self, sink):
        sample = sink.emit("pull-sample")
        feed = self._rtmp_feed
        if sample is not None and feed is not None and not self._rtmp_failed:
            feed.push(sample)
        return Gst.FlowReturn.OK

    def _on_rtmp_audio_sample(self, sink):
        sample = sink.emit("pull-sample")
        if (sample is not None and self._rtmp_audio_appsrc is not None
                and not self._rtmp_failed):
            self._rtmp_audio_appsrc.emit("push-sample", sample)
        return Gst.FlowReturn.OK

    def _poll_rtmp_bus(self) -> None:
        if self._rtmp_bus is None:
            return
        failed = False
        while True:
            msg = self._rtmp_bus.pop_filtered(Gst.MessageType.ERROR)
            if msg is None:
                break
            err, debug = msg.parse_error()
            self.report.rtmp_error = config.redact_text(
                f"{err.message} ({debug})", self.rtmp)
            failed = True
        if failed:
            self._rtmp_failed = True
            self._teardown_rtmp()

    def _teardown_rtmp(self) -> None:
        """Stop publishing without letting a stuck socket block the caller."""
        pipeline = self._rtmp_pipeline
        if pipeline is None:
            return
        self._rtmp_pipeline = None
        self._rtmp_bus = None
        self._rtmp_appsrc = None
        self._rtmp_audio_appsrc = None
        self._rtmp_feed = None
        self._rtmp_encoder = None

        def shutdown() -> None:
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
