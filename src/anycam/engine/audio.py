"""The audio mixer, microphone and shared AAC tee."""
from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from .. import config  # noqa: E402
from ..sources import (caps_filter, make)
from ..engine.encoders import (ENCODER_FACTORIES, ENCODER_PARSERS, ENCODER_BITRATES, ENCODER_LABELS, _NVENC_TTL_S, _nvenc_seen, encoder_bitrate, nvenc_available, _probe_nvenc, _va_encodes, va_factory, va_present, EncoderPlan, _requested_codec, _wants_va, encoder_plan, recording_codec, EncodedFeed, force_keyframe)  # noqa: E402,F401
from ..engine.report import (EngineReport)  # noqa: E402,F401
from ..engine.audio_helpers import (MIC_DENOISE_LEVELS, mic_dsp_wanted, describe_mic_dsp, mic_dsp_elements)  # noqa: E402,F401
from ..engine.canvas_helpers import (GL_MIXER_LATENCY_NS, canvas_requested, gl_canvas_problem, _empty_drawing)  # noqa: E402,F401

Gst.init(None)


class AudioMixin:
    """The audio mixer, microphone and shared AAC tee."""

    def _mixer_input(self, name: str, gain_db: float, muted: bool, meter: bool = False):
        """Convert/resample one audio source and attach it to the mixer."""
        assert self.audio_mixer is not None
        conv = make("audioconvert", f"{name}_conv")
        res = make("audioresample", f"{name}_resample")
        caps = caps_filter("audio/x-raw,format=F32LE,rate=48000,channels=2", f"{name}_caps")
        vol = make("volume", f"{name}_volume", volume=10 ** (gain_db / 20.0))
        vol.set_property("mute", bool(muted))
        self._add(conv, res, caps, vol)
        self._link([conv, res, caps, vol])
        tail = vol
        level = None
        if meter:
            level = make("level", f"{name}_level", interval=100_000_000, post_messages=True)
            self._add(level)
            self._link([vol, level])
            tail = level
        pad = self.audio_mixer.request_pad_simple("sink_%u")
        tail.get_static_pad("src").link(pad)
        return conv, vol, level

    def _build_audio(self) -> None:
        assert self.pipeline is not None
        mixer = make("audiomixer", "audio_mixer", ignore_inactive_pads=True)
        self._add(mixer)
        self.audio_mixer = mixer
        # A silent always-on input keeps audiomixer producing buffers while a
        # USB/PipeWire microphone wakes up; without it mp4mux stalls the whole
        # tee (and the compositor) waiting for the first audio buffer.
        silence = make("audiotestsrc", "audio_clock", is_live=True, wave="silence")
        conv, _, _ = self._mixer_input("silence", 0.0, False)
        self._add(silence)
        self._link([silence, conv])
        self.report.audio_inputs.append("silence")
        self._audio_eos_pads.append(silence.get_static_pad("src"))
        # config.microphone() runs ``pactl``; do not pay for that (or block on
        # a stuck sound server) when the microphone is not even enabled.
        mic_name = ""
        if self.project.audio.mic_enabled:
            mic_name = self.project.audio.mic_device or config.microphone()
        if mic_name:
            src = make("pulsesrc", "mic", device=mic_name)
            conv, vol, _level = self._mixer_input(
                "mic", self.project.audio.mic_gain_db,
                self.project.audio.mic_muted, meter=True)
            self._add(src)
            dsp = mic_dsp_elements(self.project.audio)
            if dsp:
                self._add(*dsp)
                self._link([src, *dsp, conv])
                self.report.mic_processing = describe_mic_dsp(self.project.audio)
            else:
                self._link([src, conv])
                if mic_dsp_wanted(self.project.audio):
                    self.report.mic_processing = (
                        "unavailable: GStreamer webrtcdsp is not installed")
            self.mic_source = src
            self.mic_volume = vol
            self._audio_eos_pads.append(src.get_static_pad("src"))
            self.report.audio_inputs.append(f"mic:{mic_name}")

        desktop = self.project.audio.desktop_device
        if desktop:
            # Desktop audio is a monitor source: a separate mixer input so
            # sharing what you hear does not replace the microphone.
            src = make("pulsesrc", "desktop_audio", device=desktop)
            conv, vol, _level = self._mixer_input(
                "desktop", self.project.audio.desktop_gain_db,
                self.project.audio.desktop_muted, meter=False)
            self._add(src)
            self._link([src, conv])
            self.desktop_volume = vol
            self._audio_eos_pads.append(src.get_static_pad("src"))
            self.report.audio_inputs.append(f"desktop:{desktop}")

        media = self.project.audio.media_file
        if media and not Path(media).exists():
            self.report.media_error = f"media audio file not found: {media}"
        elif media:
            uri = Path(media).resolve().as_uri()
            decode = make("uridecodebin", "media_audio", uri=uri)
            conv, vol, _ = self._mixer_input("media", self.project.audio.media_gain_db,
                                             self.project.audio.media_muted)
            self._add(decode)
            self.media_volume = vol
            self.report.audio_inputs.append(f"media:{media}")

            def on_pad(_bin, pad, target):
                caps = pad.get_current_caps() or pad.query_caps(None)
                if caps is None or caps.get_size() == 0:
                    return
                if not caps.get_structure(0).get_name().startswith("audio/"):
                    return
                sink = target.get_static_pad("sink")
                if sink is not None and not sink.is_linked():
                    pad.link(sink)
                    self._audio_eos_pads.append(pad)

            self._on(decode, "pad-added", on_pad, conv)

        # Give the shared mixer a consumer even in preview-only mode, so its
        # src pad is never unlinked and the audio tee can be tapped later by
        # the recorder/RTMP branches without rebuilding the graph.
        tee = self._audio_tee()
        if tee is not None:
            fs = make("fakesink", "audio_preview_sink", sync=False)
            fs.set_property("async", False)
            self._add(fs)
            tpad = tee.request_pad_simple("src_%u")
            if tpad is not None and tpad.link(fs.get_static_pad("sink")) == Gst.PadLinkReturn.OK:
                try:
                    tpad.set_active(True)
                except Exception:  # noqa: BLE001
                    pass

    def _audio_tee(self) -> Gst.Element | None:
        """Build the shared AAC encode chain once and return its tee.

        Both the recording and RTMP branches can pull an AAC stream from this
        tee, so enabling both at once does not try to link ``audio_mixer``'s
        single src pad twice.
        """
        if self.audio_tee is not None:
            return self.audio_tee
        if self.audio_mixer is None or not self.report.audio_inputs:
            return None
        aconv = make("audioconvert", "audio_shared_conv")
        aenc = make("avenc_aac", "audio_shared_enc", bitrate=128000)
        aparse = make("aacparse", "audio_shared_parse")
        tee = make("tee", "audio_shared_tee")
        self._add(aconv, aenc, aparse, tee)
        self._link([self.audio_mixer, aconv, aenc, aparse, tee])
        self.audio_tee = tee
        return tee

    def set_mic_gain(self, db: float) -> None:
        self.project.audio.mic_gain_db = db
        if self.mic_volume is not None:
            self.mic_volume.set_property("volume", 10 ** (db / 20.0))

    def set_mic_muted(self, muted: bool) -> None:
        self.project.audio.mic_muted = muted
        if self.mic_volume is not None:
            self.mic_volume.set_property("mute", bool(muted))
