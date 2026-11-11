"""Microphone voice-processing helpers."""
from __future__ import annotations


import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from ..sources import (caps_filter, make)


MIC_DENOISE_LEVELS = ("low", "moderate", "high", "very-high")


def mic_dsp_wanted(audio) -> bool:
    return (str(getattr(audio, "mic_denoise", "off")) in MIC_DENOISE_LEVELS
            or bool(getattr(audio, "mic_auto_gain", False)))


def describe_mic_dsp(audio) -> str:
    parts = []
    level = str(getattr(audio, "mic_denoise", "off"))
    if level in MIC_DENOISE_LEVELS:
        parts.append(f"noise suppression {level}")
    if getattr(audio, "mic_auto_gain", False):
        parts.append("auto gain")
    return ", ".join(parts)


def mic_dsp_elements(audio, prefix: str = "mic_dsp") -> list[Gst.Element]:
    """Voice processing between the microphone and the mixer, or [].

    ``webrtcdsp`` only accepts interleaved S16 at 8/16/32/48 kHz, and reads
    its settings when it starts (a change needs a rebuild).  Echo cancelling
    is off: it needs a ``webrtcechoprobe`` on the playback side, which AnyCam
    does not own.  Mono is enough for a voice and halves the work; the mixer
    input spreads it to both channels.
    """
    if not mic_dsp_wanted(audio):
        return []
    if Gst.ElementFactory.find("webrtcdsp") is None:
        return []
    level = str(getattr(audio, "mic_denoise", "off"))
    dsp = make("webrtcdsp", prefix, echo_cancel=False,
               gain_control=bool(getattr(audio, "mic_auto_gain", False)),
               noise_suppression=level in MIC_DENOISE_LEVELS,
               high_pass_filter=True)
    if level in MIC_DENOISE_LEVELS:
        # GstWebrtcNoiseSuppressionLevel: low=0 moderate=1 high=2 very-high=3.
        dsp.set_property("noise-suppression-level", MIC_DENOISE_LEVELS.index(level))
    return [
        make("audioconvert", f"{prefix}_conv"),
        make("audioresample", f"{prefix}_resample"),
        caps_filter("audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=1",
                    f"{prefix}_caps"),
        dsp,
    ]
