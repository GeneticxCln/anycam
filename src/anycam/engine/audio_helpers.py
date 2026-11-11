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


# --------------------------------------------------------- voice presets
# equalizer-10bands: 29, 59, 119, 237, 474, 947, 1889, 3770, 7523, 15011 Hz (dB).
EQ_PRESETS: dict[str, tuple[float, ...]] = {
    "flat": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "voice": (-24, -12, -3, 0, 0, 1, 2, 3, 1, -2),
    "warm": (0, 2, 3, 2, 1, 0, -1, -2, -3, -4),
    "bright": (-6, -3, 0, 0, 0, 1, 3, 4, 4, 3),
    "bass-cut": (-24, -18, -9, -3, 0, 0, 0, 0, 0, 0),
}
EQ_LABELS = {"flat": "Flat", "voice": "Voice", "warm": "Warm", "bright": "Bright",
             "bass-cut": "Bass cut"}

# audiodynamic settings: (mode, characteristics, threshold 0..1 of full scale, ratio).
# An expander below its threshold is the noise gate; a compressor above it levels peaks.
DYNAMICS_PRESETS: dict[str, tuple[tuple[str, str, float, float], ...]] = {
    "off": (),
    "gate": (("expander", "hard-knee", 0.02, 4.0),),
    "compressor": (("compressor", "hard-knee", 0.25, 0.4),),
    "broadcast": (("expander", "hard-knee", 0.015, 3.0),
                  ("compressor", "hard-knee", 0.25, 0.4)),
}
DYNAMICS_LABELS = {"off": "Off", "gate": "Noise gate", "compressor": "Compressor",
                   "broadcast": "Gate + compressor"}
MAX_DELAY_MS = 2000


def preset_name(value, table, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in table else default


def delay_ms(value) -> int:
    """A sync offset clamped to 0..MAX_DELAY_MS (anything else is 0)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number != number:
        return 0
    return int(max(0, min(MAX_DELAY_MS, round(number))))


def voice_elements(audio, prefix: str = "mic_fx") -> list[Gst.Element]:
    """Gate/compressor and equaliser for the microphone, in that order, or []."""
    elements: list[Gst.Element] = []
    dynamics = preset_name(getattr(audio, "mic_dynamics", "off"), DYNAMICS_PRESETS, "off")
    if Gst.ElementFactory.find("audiodynamic") is not None:
        for index, (mode, knee, threshold, ratio) in enumerate(DYNAMICS_PRESETS[dynamics]):
            elements.append(make("audiodynamic", f"{prefix}_dyn{index}", mode=mode,
                                 characteristics=knee, threshold=threshold, ratio=ratio))
    eq = preset_name(getattr(audio, "mic_eq", "flat"), EQ_PRESETS, "flat")
    if eq != "flat" and Gst.ElementFactory.find("equalizer-10bands") is not None:
        equaliser = make("equalizer-10bands", f"{prefix}_eq")
        for band, gain in enumerate(EQ_PRESETS[eq]):
            equaliser.set_property(f"band{band}", float(gain))
        elements.append(equaliser)
    if elements:
        # audiodynamic wants an explicit float format; give each stage one.
        elements.insert(0, make("audioconvert", f"{prefix}_conv"))
    return elements


def delay_element(milliseconds, name: str):
    """A queue that holds audio back by ``milliseconds`` (a sync offset), or None."""
    milliseconds = delay_ms(milliseconds)
    if milliseconds <= 0:
        return None
    return make("queue", name, min_threshold_time=milliseconds * Gst.MSECOND,
                max_size_time=(milliseconds + 500) * Gst.MSECOND,
                max_size_buffers=0, max_size_bytes=0)


def describe_voice_fx(audio) -> str:
    parts = []
    dynamics = preset_name(getattr(audio, "mic_dynamics", "off"), DYNAMICS_PRESETS, "off")
    if dynamics != "off":
        parts.append(DYNAMICS_LABELS[dynamics].lower())
    eq = preset_name(getattr(audio, "mic_eq", "flat"), EQ_PRESETS, "flat")
    if eq != "flat":
        parts.append(f"{EQ_LABELS[eq].lower()} equaliser")
    return ", ".join(parts)


# ------------------------------------------------------------ audio outputs
VIRTUAL_MIC_NAME = "AnyCam Microphone"


def audio_server_socket(kind: str) -> str:
    """Path of the running PipeWire/PulseAudio socket, or "" when there is none."""
    import os
    from pathlib import Path

    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime:
        return ""
    names = {"pipewire": "pipewire-0", "pulse": "pulse/native"}
    candidate = Path(runtime) / names[kind]
    return str(candidate) if candidate.exists() else ""


def virtual_mic_properties(name: str = VIRTUAL_MIC_NAME) -> str:
    """The stream properties that make ``pipewiresink`` a recordable microphone."""
    safe = "".join(ch for ch in name if ch.isalnum() or ch in " -_") or "AnyCam"
    return (f"props,media.class=Audio/Source,media.role=Communication,"
            f"node.name=anycam-microphone,node.description=\"{safe}\"")
