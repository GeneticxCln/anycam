"""Hardware and software video encoder selection, shared by recording and streaming."""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402



Gst.init(None)

ENCODER_FACTORIES = {"h264": "nvh264enc", "h265": "nvh265enc",
                    "av1": "nvav1enc"}
ENCODER_PARSERS = {"h264": "h264parse", "h265": "h265parse",
                  "av1": "av1parse"}
ENCODER_BITRATES = {"h264": 12000, "h265": 10000, "av1": 8000}
ENCODER_LABELS = {"h264": "H.264", "h265": "H.265", "av1": "AV1"}



# VA-API (Intel, AMD) through the ``va`` plugin of gst-plugins-bad.  The old
# gstreamer-vaapi elements (vaapih264enc ...) are deprecated and not used.
VA_FACTORIES = {"h264": ("vah264enc", "vah264lpenc"),
                "h265": ("vah265enc", "vah265lpenc"),
                "av1": ("vaav1enc", "vaav1lpenc")}
_VA_ALIASES = {"vaapi", "va", "vaapi_h264", "va_h264"}
_VA_H265_ALIASES = {"vaapi_h265", "va_h265", "vaapi_hevc"}
_VA_AV1_ALIASES = {"vaapi_av1", "va_av1"}
_CPU_ENCODERS = {"cpu", "x264", "software", "0", "false", "off"}
_H265_ALIASES = {"h265", "hevc", "nvh265", "nvenc_h265", "nvenc_hevc",
                 "hevc_nvenc"}
_AV1_ALIASES = {"av1", "nvav1", "nvenc_av1", "av1_nvenc"}


_NVENC_TTL_S = 60.0
_nvenc_seen: dict[str, tuple[float, bool]] = {}



def encoder_bitrate(codec: str, width: int, height: int) -> int:
    """Bitrate appropriate for the canvas, in kbit/s.

    The 12 Mbit/s H.264 baseline is sized for 1080p.  Sending a 4K canvas at
    that rate is visibly soft, so scale above 1080p with a sub-linear exponent;
    4K H.264 lands at 34 Mbit/s and 1440p at 18 Mbit/s.  Recording keeps its
    established bitrates, while RTMP uses this canvas-aware value.
    """
    base = ENCODER_BITRATES.get(codec, ENCODER_BITRATES["h264"])
    pixels = max(1, int(width)) * max(1, int(height))
    scale = max(1.0, (pixels / (1920 * 1080)) ** 0.75)
    return max(1000, int(round(base * scale / 1000.0)) * 1000)


def nvenc_available(codec: str = "h264") -> bool:
    """Whether NVENC for ``codec`` can run on this machine right now.

    The GStreamer factory alone is not enough: a headless host can install
    ``nvcodec`` without a usable NVIDIA driver.  ``nvidia-smi`` is the cheap
    runtime check; selection still falls back to x264 if it cannot run.  The
    answer is kept for a minute: every recording and stream build asked, and
    each ask ran ``nvidia-smi`` (up to its 2 s timeout) on the GUI thread.
    """
    now = time.monotonic()
    seen = _nvenc_seen.get(codec)
    if seen is not None and now - seen[0] < _NVENC_TTL_S:
        return seen[1]
    answer = _probe_nvenc(codec)
    _nvenc_seen[codec] = (now, answer)
    return answer


def _probe_nvenc(codec: str) -> bool:
    factory = ENCODER_FACTORIES.get(codec, "")
    if not factory or Gst.ElementFactory.find(factory) is None:
        return False
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        probe = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0 and bool(probe.stdout.strip())


@functools.lru_cache(maxsize=None)
def _va_encodes(factory: str) -> bool:
    """A real VA-API encode of a few frames (cached).

    A present element proves nothing: a missing or broken VA driver only fails
    once frames arrive.  This runs once per encoder per session.
    """
    if Gst.ElementFactory.find(factory) is None:
        return False
    upload = "vapostproc ! " if Gst.ElementFactory.find("vapostproc") else (
        "videoconvert ! video/x-raw,format=NV12 ! ")
    try:
        pipeline = Gst.parse_launch(
            "videotestsrc num-buffers=5 ! video/x-raw,width=320,height=240,framerate=30/1 ! "
            f"{upload}{factory} ! fakesink")
    except GLib.Error:
        return False
    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)
    try:
        message = bus.timed_pop_filtered(
            5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    finally:
        pipeline.set_state(Gst.State.NULL)
    return message is not None and message.type == Gst.MessageType.EOS


def va_factory(codec: str) -> str:
    """The first VA-API encoder for ``codec`` that really encodes, or ""."""
    for factory in VA_FACTORIES.get(codec, ()):
        if _va_encodes(factory):
            return factory
    return ""


def va_present(codec: str = "h264") -> bool:
    """Cheap check (no probe): is any VA-API encoder for ``codec`` installed?"""
    return any(Gst.ElementFactory.find(name) is not None
               for name in VA_FACTORIES.get(codec, ()))


@dataclass(frozen=True)
class EncoderPlan:
    """One H.26x/AV1 decision shared by recording and RTMP."""

    codec: str
    factory: str
    hardware: bool
    parser: str
    bitrate: int
    note: str


def _requested_codec(preference: str) -> str:
    preference = (preference or "auto").strip().lower()
    if preference in _H265_ALIASES or preference in _VA_H265_ALIASES:
        return "h265"
    if preference in _AV1_ALIASES or preference in _VA_AV1_ALIASES:
        return "av1"
    return "h264"


def _wants_va(preference: str) -> bool:
    return preference in _VA_ALIASES | _VA_H265_ALIASES | _VA_AV1_ALIASES


def encoder_plan(preference: str, *, purpose: str = "record") -> EncoderPlan:
    """Choose an encoder, falling back only through formats the graph supports.

    Recording accepts H.264, H.265 and AV1 in MP4.  RTMP/FLV is H.264-only, so
    a requested H.265/AV1 stream is encoded as H.264 there.  A missing driver
    falls back to x264 rather than leaving the output without an encoder.
    """
    preference = (preference or "auto").strip().lower()
    requested = _requested_codec(preference)
    effective = "h264" if purpose == "rtmp" else requested
    if preference in _CPU_ENCODERS:
        note = "CPU x264"
        if effective != "h264":
            note += f" · {ENCODER_LABELS[effective]} needs NVENC"
        return EncoderPlan("h264", "x264enc", False, "h264parse",
                           ENCODER_BITRATES["h264"], note)

    candidates = {
        "h264": ["h264"],
        "h265": ["h265", "h264"],
        "av1": ["av1", "h265", "h264"],
    }[effective]

    def nvenc(codec: str) -> EncoderPlan | None:
        if (not nvenc_available(codec)
                or Gst.ElementFactory.find(ENCODER_PARSERS[codec]) is None):
            return None
        note = f"{ENCODER_LABELS[codec]} NVENC · "
        note += ("CUDA upload" if Gst.ElementFactory.find("cudaupload")
                 else "RGBA upload")
        return EncoderPlan(codec, ENCODER_FACTORIES[codec], True,
                           ENCODER_PARSERS[codec], ENCODER_BITRATES[codec], note)

    def vaapi(codec: str) -> EncoderPlan | None:
        if Gst.ElementFactory.find(ENCODER_PARSERS[codec]) is None:
            return None
        factory = va_factory(codec)
        if not factory:
            return None
        return EncoderPlan(codec, factory, True, ENCODER_PARSERS[codec],
                           ENCODER_BITRATES[codec],
                           f"{ENCODER_LABELS[codec]} VA-API ({factory})")

    # Auto and NVENC choices try NVENC first, then VA-API (Intel/AMD); a
    # VA-API choice tries VA-API first.  x264 is the last resort.
    families = (vaapi, nvenc) if _wants_va(preference) else (nvenc, vaapi)
    for codec in candidates:
        for family in families:
            plan = family(codec)
            if plan is None:
                continue
            note = plan.note
            if purpose == "rtmp" and requested != "h264":
                note = f"RTMP/FLV uses H.264 · {note}"
            elif codec != effective:
                note = f"{ENCODER_LABELS[effective]} unavailable · {note}"
            return EncoderPlan(plan.codec, plan.factory, True, plan.parser,
                               plan.bitrate, note)

    note = "No hardware encoder · CPU x264"
    if effective != "h264":
        note = f"{ENCODER_LABELS[effective]} unavailable · " + note
    elif purpose == "rtmp" and requested != "h264":
        note = "RTMP/FLV uses H.264 · " + note
    return EncoderPlan("h264", "x264enc", False, "h264parse",
                       ENCODER_BITRATES["h264"], note)


def recording_codec(preference: str) -> str:
    """Codec a new persistent recorder must parse for this preference.

    ``ANYCAM_ENCODER`` overrides the saved setting in the engine, so it has to
    here too, or the recorder and the encoder disagree about the codec.
    """
    return encoder_plan(
        os.environ.get("ANYCAM_ENCODER", "").strip().lower() or preference
    ).codec


class EncodedFeed:
    """Push encoded samples into an ``appsrc`` without leaking a delta frame.

    A leaky queue drops whichever frame is oldest, which for H.264 leaves the
    next frames referring to a picture the decoder never saw.  When the queue is
    full this drops on a keyframe boundary instead (skipping to the next
    keyframe) and asks the encoder for one.
    """

    def __init__(self, appsrc, request_keyframe=None) -> None:
        self.appsrc = appsrc
        self.request_keyframe = request_keyframe
        self.skipping = False

    def push(self, sample) -> None:
        buf = sample.get_buffer()
        is_key = buf is None or not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        if self.skipping:
            if not is_key:
                return
            self.skipping = False
        try:
            level = int(self.appsrc.get_property("current-level-buffers"))
            limit = int(self.appsrc.get_property("max-buffers"))
        except (AttributeError, TypeError, ValueError):
            level, limit = 0, 0
        if limit and level >= limit:
            self.skipping = True
            if self.request_keyframe is not None:
                try:
                    self.request_keyframe()
                except Exception:  # noqa: BLE001 - best effort
                    pass
            return
        self.appsrc.emit("push-sample", sample)


def force_keyframe(encoder) -> bool:
    """Ask ``encoder`` for a keyframe (with fresh headers)."""
    pad = encoder.get_static_pad("src") if encoder is not None else None
    if pad is None:
        return False
    event = GstVideo.video_event_new_upstream_force_key_unit(
        Gst.CLOCK_TIME_NONE, True, 0)
    return bool(pad.send_event(event))
