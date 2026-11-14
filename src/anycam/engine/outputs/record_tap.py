"""The canvas tap that feeds the persistent recorder (and one-shot recordings)."""
from __future__ import annotations

import os
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from ...engine.outputs.recorder import Recorder  # noqa: E402
from ...sources import (caps_filter, make)
from ...engine.encoders import (ENCODER_FACTORIES, ENCODER_PARSERS, ENCODER_BITRATES, ENCODER_LABELS, encoder_bitrate, nvenc_available, _probe_nvenc, _va_encodes, va_factory, va_present, EncoderPlan, _requested_codec, _wants_va, encoder_plan, recording_codec, EncodedFeed, force_keyframe)  # noqa: E402,F401
from ...engine.report import (EngineReport)  # noqa: E402,F401
from ...engine.audio_helpers import (MIC_DENOISE_LEVELS, mic_dsp_wanted, describe_mic_dsp, mic_dsp_elements)  # noqa: E402,F401
from ...engine.canvas_helpers import (GL_MIXER_LATENCY_NS, canvas_requested, gl_canvas_problem, _empty_drawing)  # noqa: E402,F401

Gst.init(None)


class RecordTapMixin:
    """The canvas tap that feeds the persistent recorder (and one-shot recordings)."""

    def _encoder_preference(self) -> str:
        """Environment override first, then the saved project setting."""
        env = os.environ.get("ANYCAM_ENCODER", "").strip().lower()
        if env:
            return env
        return str(getattr(self.project.output, "encoder", "auto")
                   or "auto").strip().lower()

    def _make_encoder(self, plan: EncoderPlan, name: str, *,
                      purpose: str = "record") -> Gst.Element:
        """Instantiate the selected encoder without leaking codec details."""
        bitrate = plan.bitrate
        if purpose == "rtmp":
            bitrate = encoder_bitrate(plan.codec, self.canvas_w, self.canvas_h)
        chosen = int(getattr(self.project.output,
                             "stream_bitrate_kbps" if purpose == "rtmp" else "record_bitrate_kbps", 0))
        if chosen > 0:
            bitrate = chosen              # kbit/s, like the codec defaults
        if plan.hardware and plan.factory.startswith("va"):
            # VA-API encoders: constant bitrate, a keyframe every 2 s, and the
            # fastest target usage (live, like NVENC's zerolatency/p4).
            encoder = make(plan.factory, name)
            for prop, value in (("bitrate", bitrate), ("rate-control", "cbr"),
                                ("key-int-max", 60), ("target-usage", 7)):
                if encoder.find_property(prop) is not None:
                    try:
                        encoder.set_property(prop, value)
                    except (TypeError, ValueError):
                        pass
            return encoder
        if plan.hardware:
            return make(plan.factory, name, bitrate=bitrate,
                        zerolatency=True, preset="p4")
        return make(plan.factory, name, speed_preset="veryfast",
                    tune="zerolatency", bitrate=bitrate, key_int_max=60)

    @staticmethod
    def _make_parser(plan: EncoderPlan, name: str) -> Gst.Element:
        """Create the bitstream parser mp4mux needs for the chosen codec."""
        if plan.parser == "av1parse":
            return make(plan.parser, name)
        return make(plan.parser, name, config_interval=-1)

    def _make_record_encoder(self) -> tuple[Gst.Element, bool]:
        """Return the recording encoder and whether its input is GPU memory."""
        plan = encoder_plan(self._encoder_preference())
        self._record_plan = plan
        self.report.encoder = plan.factory
        self.report.video_codec = plan.codec
        self.report.encoder_note = plan.note
        return self._make_encoder(plan, "record_encoder"), plan.hardware

    @staticmethod
    def _encoder_input(hardware: bool, prefix: str, factory: str = "") -> list[Gst.Element]:
        """Keep compositor RGBA out of the CPU format-conversion path.

        ``nvh264enc`` accepts system RGBA directly.  When CUDA is present,
        ``cudaupload`` moves that frame into CUDA memory once and the encoder
        consumes it there, avoiding ``videoconvert`` and an I420 copy.  x264
        still needs the explicit CPU I420 conversion.
        """
        if hardware and factory.startswith("va"):
            # VA-API: vapostproc uploads and converts on the GPU; without it,
            # convert to NV12 on the CPU (what the encoders accept).
            if Gst.ElementFactory.find("vapostproc") is not None:
                return [make("vapostproc", f"{prefix}_va_upload")]
            return [make("videoconvert", f"{prefix}_convert"),
                    caps_filter("video/x-raw,format=NV12", f"{prefix}_nv12")]
        if hardware:
            if Gst.ElementFactory.find("cudaupload") is not None:
                return [make("cudaupload", f"{prefix}_cuda_upload")]
            return []
        return [make("videoconvert", f"{prefix}_convert"),
                # Fixed colorimetry: constant SPS for the whole recording.
                caps_filter("video/x-raw,format=I420,colorimetry=bt709", f"{prefix}_raw")]

    def _build_recorder_tap(self) -> None:
        """Feed encoded samples to the persistent recorder.

        The recorder owns mp4mux and the file for the whole session, so this
        branch can be destroyed and recreated with the canvas without
        finalising the MP4.
        """
        if self.recorder is None:
            return
        if self._recorder_valve is not None:
            self._recorder_valve.set_property("drop", False)
            return
        queue = self._branch("record")
        queue.set_property("leaky", 2)
        valve = make("valve", "record_valve", drop=False)
        self._recorder_valve = valve
        enc, hardware = self._make_record_encoder()
        self._record_encoder = enc
        plan = self._record_plan or encoder_plan(self._encoder_preference())
        pixel_path = self._encoder_input(hardware, "record", plan.factory)
        parse = self._make_parser(plan, "record_parse_tap")
        # Encoded frames are never dropped here (the leaky queue in front of
        # the encoder is where this graph sheds load).
        sink = make("appsink", "record_tap_sink", emit_signals=True,
                    max_buffers=16, drop=False, sync=False)
        self._add(valve, *pixel_path, enc, parse, sink)
        self._link([queue, valve, *pixel_path, enc, parse, sink])
        self._on(sink, "new-sample", self._on_record_sample)

        tee = self._audio_tee()
        if tee is not None:
            audio_queue = make("queue", "record_audio_queue",
                               max_size_buffers=8, max_size_time=1 * Gst.SECOND)
            audio_queue.set_property("leaky", 2)
            audio_sink = make("appsink", "record_audio_sink", emit_signals=True,
                              max_buffers=4, drop=True, sync=False)
            audio_sink.set_property("async", False)
            self._add(audio_queue, audio_sink)
            self._link([audio_queue, audio_sink])
            tpad = tee.request_pad_simple("src_%u")
            if (tpad is None or
                    tpad.link(audio_queue.get_static_pad("sink")) != Gst.PadLinkReturn.OK):
                raise RuntimeError("could not tap shared audio for recording")
            try:
                tpad.set_active(True)
            except Exception:  # noqa: BLE001
                pass
            audio_queue.set_state(Gst.State.PLAYING)
            audio_sink.set_state(Gst.State.PLAYING)
            self._on(audio_sink, "new-sample", self._on_record_audio_sample)

    def attach_recorder(self, recorder: Recorder) -> bool:
        """Start feeding the existing canvas into ``recorder`` without a rebuild."""
        self.recorder = recorder
        self.record_path = str(recorder.path)
        self._bind_recorder()
        if self._recorder_valve is None:
            self._build_recorder_tap()
        else:
            self._recorder_valve.set_property("drop", False)
            # The encoder kept its state while the valve was shut, so its next
            # frame is a delta frame that refers to pictures a new file does
            # not contain.  Ask for a keyframe so the file starts decodable.
            self._request_record_keyframe()
        if self._recorder_valve is not None:
            return True
        self.recorder = None
        return False

    def _request_record_keyframe(self) -> bool:
        """Ask the recording encoder for a keyframe (with fresh headers)."""
        return force_keyframe(self._record_encoder)

    def _bind_recorder(self) -> None:
        """Tell the recorder this canvas is about to feed it.

        A new canvas restarts its timestamps, and the recorder needs to know
        so video and audio continue from the same point of the file.
        """
        recorder = self.recorder
        if recorder is None:
            return
        try:
            recorder.request_keyframe = self._request_record_keyframe
        except AttributeError:
            pass
        begin = getattr(recorder, "begin_epoch", None)
        if begin is not None:
            begin()

    def record_codec(self) -> str | None:
        """Codec of the recording tap this canvas was built with, if any."""
        if self._recorder_valve is None or self._record_plan is None:
            return None
        return self._record_plan.codec

    def detach_recorder(self) -> None:
        """Stop feeding encoded samples to the recorder, leaving the file open."""
        if self._recorder_valve is not None:
            self._recorder_valve.set_property("drop", True)
        self.recorder = None
        # The file now belongs to whoever owns the recorder.  A stale path made
        # stop() report it as this engine's own (unfinalised) recording.
        self.record_path = ""

    def _on_record_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is not None and self.recorder is not None:
            self.recorder.push_video(sample)
        return Gst.FlowReturn.OK

    def _on_record_audio_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is not None and self.recorder is not None:
            self.recorder.push_audio(sample)
        return Gst.FlowReturn.OK

    def _build_recording(self) -> None:
        queue = self._branch("record")
        enc, hardware = self._make_record_encoder()
        plan = self._record_plan or encoder_plan(self._encoder_preference())
        pixel_path = self._encoder_input(hardware, "record", plan.factory)
        parse = self._make_parser(plan, "record_parse")
        # Buffer encoded video while mux waits for the first audio buffer, so a
        # slow microphone wake-up cannot block the tee and starve the preview.
        buffer = make("queue", "record_buffer", max_size_buffers=0,
                      max_size_bytes=0, max_size_time=5 * Gst.SECOND)
        # Fragmented MP4 is playable up to the last fragment if the process
        # dies.  faststart wrote the whole file to /tmp first, so a crash left
        # a 0-byte recording and the data stayed in tmpfs until reboot.
        mux = make("mp4mux", "record_mux", fragment_duration=500)
        Path(self.record_path).parent.mkdir(parents=True, exist_ok=True)
        sink = make("filesink", "record_sink", location=self.record_path)
        self._add(*pixel_path, enc, parse, buffer, mux, sink)
        self._link([queue, *pixel_path, enc, parse, buffer])
        video_pad = mux.request_pad_simple("video_%u")
        buffer.get_static_pad("src").link(video_pad)
        mux.link(sink)
        self._record_sink = sink
        tee = self._audio_tee()
        if tee is not None:
            # Keep the muxer behind its own queue.  Without this, a slow
            # mp4mux waits on the shared audio tee and can stall the separate
            # RTMP muxer (and the recording) during simultaneous output.
            audio_queue = make("queue", "record_audio_queue",
                               max_size_buffers=8, max_size_time=1 * Gst.SECOND)
            self._add(audio_queue)
            tpad = tee.request_pad_simple("src_%u")
            apad = mux.request_pad_simple("audio_%u")
            if (tpad is None or apad is None
                    or tpad.link(audio_queue.get_static_pad("sink"))
                    != Gst.PadLinkReturn.OK
                    or audio_queue.get_static_pad("src").link(apad)
                    != Gst.PadLinkReturn.OK):
                raise RuntimeError("could not link shared audio into mp4mux")
