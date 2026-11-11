"""The canvas tap that feeds the session-owned virtual camera."""
from __future__ import annotations

import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from ... import config  # noqa: E402
from ...engine.outputs.vcam import VcamOutput  # noqa: E402
from ...sources import (caps_filter, caps_string, make)
from ...engine.encoders import (ENCODER_FACTORIES, ENCODER_PARSERS, ENCODER_BITRATES, ENCODER_LABELS, _NVENC_TTL_S, _nvenc_seen, encoder_bitrate, nvenc_available, _probe_nvenc, _va_encodes, va_factory, va_present, EncoderPlan, _requested_codec, _wants_va, encoder_plan, recording_codec, EncodedFeed, force_keyframe)  # noqa: E402,F401
from ...engine.report import (EngineReport)  # noqa: E402,F401
from ...engine.audio_helpers import (MIC_DENOISE_LEVELS, mic_dsp_wanted, describe_mic_dsp, mic_dsp_elements)  # noqa: E402,F401
from ...engine.canvas_helpers import (GL_MIXER_LATENCY_NS, canvas_requested, gl_canvas_problem, _empty_drawing)  # noqa: E402,F401

Gst.init(None)


class VcamTapMixin:
    """The canvas tap that feeds the session-owned virtual camera."""

    def _build_vcam(self) -> None:
        """Feed the virtual camera through an appsink tap.

        A session-owned ``VcamOutput`` keeps ``v4l2sink`` alive across canvas
        rebuilds.  The engine only builds the tap; CLI users without a shared
        output still get the legacy self-contained bridge.
        """
        device = self.project.output.vcam_device or config.loopback_device()
        self.report.vcam = device
        if self._vcam_valve is not None:
            self._vcam_valve.set_property("drop", False)
            self.report.vcam_active = True
            return
        if self.vcam_output is not None:
            if not self.vcam_output.active:
                self.report.vcam_error = (self.vcam_output.error or
                                          f"virtual camera {device} is not running")
                return
            # The virtual camera has one fixed output mode, like OBS's Linux
            # v4l2sink output.  The canvas may resize or change layout while
            # the device stays open, so scale/rate the tap into that mode
            # instead of reopening v4l2sink (which kills consumers' handles).
            out_w = int(getattr(self.vcam_output, "width", self.canvas_w))
            out_h = int(getattr(self.vcam_output, "height", self.canvas_h))
            out_fps = int(getattr(self.vcam_output, "fps", self.fps))
            caps_text = caps_string(out_w, out_h, out_fps, "RGBA")
            queue = self._branch("vcam")
            queue.set_property("leaky", 2)  # downstream: drop old buffers
            valve = make("valve", "vcam_valve", drop=False)
            self._vcam_valve = valve
            # 4-tap scaling keeps a downscaled 4K canvas crisp when the
            # existing vcam mode is smaller than the current canvas.
            scale = make("videoscale", "vcam_scale", method=2)
            rate = make("videorate", "vcam_rate")
            caps = caps_filter(caps_text, "vcam_caps")
            appsink = make("appsink", "vcam_appsink", emit_signals=True,
                           max_buffers=2, drop=True, sync=False)
            self._add(valve, scale, rate, caps, appsink)
            self._link([queue, valve, scale, rate, caps, appsink])
            self._on(appsink, "new-sample", self._on_vcam_sample)
            self._notify_canvas_attached(True)
            self.report.vcam_active = True
            return

        if not config.vcam_available(device):
            self.report.vcam_error = (
                f"virtual camera {device} is not available; load v4l2loopback with "
                "'sudo modprobe v4l2loopback video_nr=10 card_label=AnyCam "
                "exclusive_caps=1'"
            )
            return
        fmt = config.vcam_pixel_format(device)
        caps_text = caps_string(self.canvas_w, self.canvas_h, self.fps, fmt)

        queue = self._branch("vcam")
        queue.set_property("leaky", 2)  # downstream: drop old buffers
        valve = make("valve", "vcam_valve", drop=False)
        self._vcam_valve = valve
        convert = make("videoconvert")
        caps = caps_filter(caps_text, "vcam_caps")
        appsink = make("appsink", "vcam_appsink", emit_signals=True,
                       max_buffers=2, drop=True, sync=False)
        self._add(valve, convert, caps, appsink)
        self._link([queue, valve, convert, caps, appsink])
        self._on(appsink, "new-sample", self._on_vcam_sample)

        vcam = Gst.Pipeline.new("anycam-vcam")
        appsrc = make("appsrc", "vcam_appsrc",
                      caps=Gst.Caps.from_string(caps_text),
                      format=Gst.Format.TIME, is_live=False,
                      max_buffers=2, block=False, leaky_type=2)
        # A converter here absorbs the extended caps fields (colorimetry,
        # interlace-mode...) that v4l2sink refuses to negotiate directly.
        vcam_convert = make("videoconvert")
        sink = make("v4l2sink", "vcam_sink", device=device, sync=False)
        vcam.add(appsrc)
        vcam.add(vcam_convert)
        vcam.add(sink)
        if not appsrc.link(vcam_convert) or not vcam_convert.link(sink):
            raise RuntimeError("could not link appsrc into v4l2sink")
        self._vcam_pipeline = vcam
        self._vcam_appsrc = appsrc
        self._vcam_bus = vcam.get_bus()
        self._vcam_sink = sink
        self.report.vcam_active = True

    def attach_vcam(self, output: VcamOutput) -> bool:
        """Resume the vcam tap for ``output`` without rebuilding the canvas."""
        if not output.active:
            self.report.vcam_error = output.error or "virtual camera is not running"
            return False
        self.vcam_output = output
        self.want_vcam = True
        self.report.vcam_error = ""
        if self._vcam_valve is None:
            self._build_vcam()
        else:
            self._vcam_valve.set_property("drop", False)
            self.report.vcam_active = True
        self._notify_canvas_attached(self._vcam_valve is not None)
        if self._vcam_valve is not None:
            # A recovered output may still be holding an old frame.
            self.resume_vcam()
        return self._vcam_valve is not None

    def detach_vcam(self) -> None:
        """Stop feeding the virtual camera without touching the canvas."""
        if self._vcam_valve is not None:
            self._vcam_valve.set_property("drop", True)
        output, self.vcam_output = self.vcam_output, None
        self.want_vcam = False
        self.report.vcam_active = False
        if output is not None:
            setter = getattr(output, "set_canvas_attached", None)
            if setter is not None:
                setter(False)

    def _notify_canvas_attached(self, attached: bool) -> None:
        """Tell the session sink whether the canvas currently owns it."""
        setter = getattr(self.vcam_output, "set_canvas_attached", None)
        if setter is not None:
            setter(attached)

    def _on_vcam_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is not None:
            if not getattr(self, "_vcam_sample_logged", False):
                self._vcam_sample_logged = True
                caps = sample.get_caps()
                print(f"[vcam-tap] first canvas sample "
                      f"{caps.to_string() if caps else 'no caps'}",
                      file=sys.stderr, flush=True)
            if self.vcam_output is not None:
                self.vcam_output.push_sample(sample)
            elif self._vcam_appsrc is not None:
                # Buffers are refcounted; handing the sample to appsrc is zero-copy.
                self._vcam_appsrc.emit("push-sample", sample)
        return Gst.FlowReturn.OK

    def resume_vcam(self) -> None:
        """Resume the canvas feed after a source becomes visible again."""
        output = self.vcam_output
        if output is None:
            return
        if self._vcam_valve is not None:
            self._vcam_valve.set_property("drop", False)
        resume = getattr(output, "resume_canvas", None)
        if resume is not None:
            resume()

    def _poll_vcam_bus(self) -> None:
        if self._vcam_bus is None:
            return
        while True:
            msg = self._vcam_bus.pop_filtered(Gst.MessageType.ERROR)
            if msg is None:
                return
            err, debug = msg.parse_error()
            self.report.vcam_error = f"{err.message} ({debug})"
            self.report.vcam_active = False
