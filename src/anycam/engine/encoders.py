"""Hardware and software video encoder selection, shared by recording and streaming."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from .. import config  # noqa: E402



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


# ------------------------------------------------------- encoder probing
#
# Whether an encoder really works is found by encoding a few frames in a child
# process (``anycam encoders --json``): a driver that crashes or hangs costs the
# probe, never the app.  The answer is cached on disk until the AnyCam version,
# GStreamer, the kernel or a graphics driver changes.  The functions the rest of
# the app already calls read that answer and never block.

PROBE_TIMEOUT_S = 10.0
PROBE_MARK = "ANYCAM-PROBE "
X264 = "x264enc"
_PROBE_FRAMES = 5
_PROBE_WAIT_S = 4

_results: dict[str, dict] | None = None
_probe_thread: threading.Thread | None = None
_probe_lock = threading.Lock()


def candidate_encoders() -> list[str]:
    """Every encoder factory the probe tries, in a stable order."""
    names = list(ENCODER_FACTORIES.values())
    for factories in VA_FACTORIES.values():
        names.extend(factories)
    names.append(X264)
    return names


def _upload_for(factory: str) -> str:
    if factory.startswith("va"):
        return ("vapostproc ! " if Gst.ElementFactory.find("vapostproc")
                else "videoconvert ! video/x-raw,format=NV12 ! ")
    return "videoconvert ! video/x-raw,format=NV12 ! "


def test_encode(factory: str) -> str:
    """Encode a few frames; "" when it worked, otherwise why not.  Run in the child."""
    if Gst.ElementFactory.find(factory) is None:
        return "plugin not installed"
    try:
        pipeline = Gst.parse_launch(
            f"videotestsrc num-buffers={_PROBE_FRAMES} ! "
            "video/x-raw,width=320,height=240,framerate=30/1 ! "
            f"{_upload_for(factory)}{factory} ! fakesink")
    except GLib.Error as exc:
        return f"could not be built: {exc.message}"
    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)
    try:
        message = bus.timed_pop_filtered(
            _PROBE_WAIT_S * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    finally:
        pipeline.set_state(Gst.State.NULL)
    if message is None:
        return "did not finish in time"
    if message.type == Gst.MessageType.ERROR:
        error, _debug = message.parse_error()
        return error.message or "encoding failed"
    return ""


def probe_main() -> int:
    """The child: one ``ANYCAM-PROBE {json}`` line per encoder, flushed as it finishes."""
    for factory in candidate_encoders():
        reason = test_encode(factory)
        print(PROBE_MARK + json.dumps({"factory": factory, "ok": not reason, "reason": reason}),
              flush=True)
    return 0


def run_probe(timeout: float = PROBE_TIMEOUT_S, command: list[str] | None = None) -> dict[str, dict]:
    """Run the child and collect its answers; what it did not answer is "probe crashed"."""
    command = command or [sys.executable, "-m", "anycam", "encoders", "--json"]
    found: dict[str, dict] = {}
    why = "the probe did not finish"
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError as exc:
        child = None
        why = f"the probe could not start ({exc.strerror or exc})"
    output = ""
    if child is not None:
        try:
            output, _ = child.communicate(timeout=timeout)
            if child.returncode != 0:
                why = f"the probe crashed (exit {child.returncode})"
        except subprocess.TimeoutExpired:
            child.kill()
            output, _ = child.communicate()
            why = f"the probe stopped after {timeout:.0f} s"
    for line in (output or "").splitlines():
        if line.startswith(PROBE_MARK):
            try:
                entry = json.loads(line[len(PROBE_MARK):])
                found[str(entry["factory"])] = {"ok": bool(entry["ok"]), "reason": str(entry.get("reason", ""))}
            except (ValueError, KeyError, TypeError):
                continue
    for factory in candidate_encoders():
        found.setdefault(factory, {"ok": False, "reason": why})
    return found


def fingerprint() -> str:
    """Changes when a driver or library the answer depends on changes."""
    parts = [config.VERSION, Gst.version_string(), platform.release(),
             os.environ.get("LIBVA_DRIVER_NAME", "")]
    try:
        parts.append(Path("/sys/module/nvidia/version").read_text().strip())
    except OSError:
        parts.append("")
    for folder in ("/usr/lib/dri", "/usr/lib64/dri", "/usr/lib/x86_64-linux-gnu/dri"):
        try:
            for entry in sorted(Path(folder).glob("*_drv_video.so")):
                parts.append(f"{entry.name}:{int(entry.stat().st_mtime)}")
        except OSError:
            continue
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def _cache_path() -> Path:
    return config.cache_dir() / "encoders.json"


def load_cache() -> dict[str, dict] | None:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        if data.get("fingerprint") == fingerprint() and isinstance(data.get("results"), dict):
            return data["results"]
    except (OSError, ValueError, AttributeError):
        pass
    return None


def save_cache(results: dict[str, dict]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps({"fingerprint": fingerprint(), "probed_at": time.time(),
                                    "results": results}, indent=2), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


def probe_results(block: bool = False, force: bool = False) -> dict[str, dict] | None:
    """The answer for every candidate, or ``None`` while it is not known yet.

    With ``block`` a missing answer is probed now (up to ``PROBE_TIMEOUT_S``).
    ``force`` ignores what is cached.
    """
    global _results
    if _results is not None and not force:
        return _results
    if not force:
        cached = load_cache()
        if cached is not None:
            _results = cached
            return _results
    if block:
        _results = run_probe()
        save_cache(_results)
        return _results
    return None


def start_background_probe(force: bool = False) -> None:
    """Probe on a thread (the child does the risky part); no-op when already known."""
    global _probe_thread
    if os.environ.get("ANYCAM_ENCODER_PROBE", "").lower() in ("off", "0", "no"):
        return
    with _probe_lock:
        if _probe_thread is not None and _probe_thread.is_alive():
            return
        if not force and probe_results() is not None:
            return
        _probe_thread = threading.Thread(target=lambda: probe_results(block=True, force=True),
                                         name="encoder-probe", daemon=True)
        _probe_thread.start()


def probe_running() -> bool:
    return _probe_thread is not None and _probe_thread.is_alive()


def forget_probe() -> None:
    """For tests and "Test again": drop the in-memory answer."""
    global _results
    _results = None


def encoder_works(factory: str) -> bool:
    answers = probe_results()
    return bool(answers and answers.get(factory, {}).get("ok"))


def encoder_problems() -> dict[str, str]:
    """``factory -> why not`` for every candidate that failed (empty until probed)."""
    answers = probe_results() or {}
    return {name: entry.get("reason", "") for name, entry in answers.items() if not entry.get("ok")}


def nvenc_available(codec: str = "h264") -> bool:
    """Whether NVENC for ``codec`` encoded a test clip (from the cached probe)."""
    return _probe_nvenc(codec)


def _probe_nvenc(codec: str) -> bool:
    factory = ENCODER_FACTORIES.get(codec, "")
    return bool(factory) and encoder_works(factory)


def _va_encodes(factory: str) -> bool:
    return encoder_works(factory)


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
    if probe_results() is None:
        note += " (encoder check not finished)"
        start_background_probe()
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
