"""The AnyCam GStreamer engine: sources -> compositor -> preview/vcam/record."""
from __future__ import annotations

import copy
from collections import deque
import os
import functools
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import gi
import psutil

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from . import config, overlay
from .geometry import box_pixels, clamp
from .model import Effects, Layer, Project, Scene
from .engine.outputs.recorder import Recorder
from .engine.outputs.vcam import VcamOutput
from .pool import CAPTURE_KINDS, CaptureSpec, SourcePool, capture_spec
from .sources import (LIVE_KINDS, SourceChain, apply_effects, background_chain,
                      build_chain, caps_filter, caps_string, gl_rack_elements,
                      image_chain, layer_tail, make)

Gst.init(None)


ENCODER_FACTORIES = {"h264": "nvh264enc", "h265": "nvh265enc",
                    "av1": "nvav1enc"}
ENCODER_PARSERS = {"h264": "h264parse", "h265": "h265parse",
                  "av1": "av1parse"}
ENCODER_BITRATES = {"h264": 12000, "h265": 10000, "av1": 8000}
ENCODER_LABELS = {"h264": "H.264", "h265": "H.265", "av1": "AV1"}


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


def nvenc_available(codec: str = "h264") -> bool:
    """Whether NVENC for ``codec`` can run on this machine right now.

    The GStreamer factory alone is not enough: a headless host can install
    ``nvcodec`` without a usable NVIDIA driver.  ``nvidia-smi`` is the cheap
    runtime check; selection still falls back to x264 if it cannot run.
    """
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


@dataclass
class EngineReport:
    started: float = 0.0
    stopped: float = 0.0
    frames: int = 0
    fps: float = 0.0
    expected_frames: float = 0.0
    dropped_frames: float = 0.0
    dropped_pct: float = 0.0
    error: str = ""
    vcam_error: str = ""
    media_error: str = ""
    media_errors: list[str] = field(default_factory=list)
    camera_mode: str = ""
    camera_modes: list[str] = field(default_factory=list)
    instant_fps: float = 0.0
    source_health: dict[str, dict] = field(default_factory=dict)
    stalled_sources: list[str] = field(default_factory=list)
    camera_probe_error: str = ""
    camera_placeholders: list[str] = field(default_factory=list)
    source_errors: list[str] = field(default_factory=list)
    mic_level_db: float | None = None
    encoder: str = ""
    video_codec: str = ""
    encoder_note: str = ""
    vcam: str = ""
    vcam_active: bool = False
    screen_error: str = ""
    rtmp_error: str = ""
    rtmp_encoder: str = ""
    rtmp_encoder_note: str = ""
    record_path: str = ""
    record_bytes: int = 0
    record_ok: bool = False
    rtmp_url: str = ""
    audio_inputs: list[str] = field(default_factory=list)
    # What the microphone passes through before the mixer ("" = nothing).
    mic_processing: str = ""
    samples: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "duration_s": round(max(0.0, self.stopped - self.started), 3),
            "frames": self.frames,
            "fps": round(self.fps, 3),
            "expected_frames": round(self.expected_frames, 1),
            "dropped_frames": round(self.dropped_frames, 1),
            "dropped_pct": round(self.dropped_pct, 3),
            "error": self.error,
            "vcam_error": self.vcam_error,
            "media_error": self.media_error,
            "media_errors": self.media_errors,
            "camera_mode": self.camera_mode,
            "camera_modes": self.camera_modes,
            "instant_fps": round(self.instant_fps, 3),
            "source_health": self.source_health,
            "stalled_sources": self.stalled_sources,
            "camera_probe_error": self.camera_probe_error,
            "camera_placeholders": self.camera_placeholders,
            "source_errors": self.source_errors,
            "mic_level_db": self.mic_level_db,
            "encoder": self.encoder,
            "video_codec": self.video_codec,
            "encoder_note": self.encoder_note,
            "vcam": self.vcam,
            "vcam_active": self.vcam_active,
            "screen_error": self.screen_error,
            "rtmp_error": config.redact_text(self.rtmp_error, self.rtmp_url),
            "rtmp_encoder": self.rtmp_encoder,
            "rtmp_encoder_note": self.rtmp_encoder_note,
            "record_path": self.record_path,
            "record_bytes": self.record_bytes,
            "record_ok": self.record_ok,
            # The URL carries the stream key; reports are shared, so mask it.
            "rtmp_url": config.redact_url(self.rtmp_url),
            "audio_inputs": self.audio_inputs,
            "mic_processing": self.mic_processing,
            "samples": self.samples,
        }


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


# Extra aggregation latency of the GPU canvas (see where it is set).
GL_MIXER_LATENCY_NS = 33_000_000


def canvas_requested() -> str:
    """``ANYCAM_CANVAS``: "cpu", "gpu" or "" (no override)."""
    value = os.environ.get("ANYCAM_CANVAS", "").strip().lower()
    return value if value in ("cpu", "gpu") else ""


@functools.lru_cache(maxsize=1)
def gl_canvas_problem() -> str:
    """Why the GL canvas cannot run here; empty when a real frame made it.

    The elements being installed is not enough: a GL context can fail, and on
    NVIDIA an upload can silently produce black textures.  So one small
    frame goes through upload -> glvideomixer -> download and its colour is
    checked.
    """
    for factory in ("glvideomixer", "glupload", "gldownload"):
        if Gst.ElementFactory.find(factory) is None:
            return f"{factory} is not installed"
    try:
        pipeline = Gst.parse_launch(
            "videotestsrc num-buffers=4 pattern=red ! "
            "video/x-raw,format=RGBA,width=64,height=64,framerate=30/1 ! "
            "identity drop-allocation=true ! glupload ! glvideomixer ! "
            "gldownload ! video/x-raw,format=RGBA,width=64,height=64 ! "
            "appsink name=probe sync=false")
    except GLib.Error as exc:
        return f"GL canvas probe could not be built: {exc.message}"
    sink = pipeline.get_by_name("probe")
    pipeline.set_state(Gst.State.PLAYING)
    sample = last = None
    try:
        for _ in range(4):
            sample = sink.emit("try-pull-sample", 3 * Gst.SECOND)
            if sample is None:
                break
            last = sample
        if last is None:
            message = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            return ("GL canvas probe produced no frame"
                    + (f": {message.parse_error()[0].message}" if message else ""))
        buf = last.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return "GL canvas probe frame could not be read"
        try:
            centre = bytes(info.data)[(32 * 64 + 32) * 4:(32 * 64 + 32) * 4 + 3]
        finally:
            buf.unmap(info)
        if not (centre[0] > 200 and centre[1] < 60 and centre[2] < 60):
            return f"GL canvas probe frame came out wrong (centre {tuple(centre)})"
        return ""
    finally:
        pipeline.set_state(Gst.State.NULL)


def _empty_drawing(layer: Layer) -> bool:
    """A drawing layer with no strokes (nothing to composite)."""
    if layer.kind != "drawing":
        return False
    from . import animated

    try:
        return not animated.parse_strokes(layer.source)
    except Exception:  # noqa: BLE001 - unreadable strokes: keep the layer
        return False


class AnyCamEngine:
    """Build and run one scene as a GStreamer compositor graph.

    The engine is intentionally UI-free.  The GUI supplies a frame callback and
    drives ``pump`` from a timer; the CLI drives it from a small loop.
    """

    def __init__(
        self,
        project: Project,
        *,
        scene: Scene | None = None,
        record_path: str | os.PathLike | None = None,
        recorder: Recorder | None = None,
        vcam: bool = False,
        vcam_output: VcamOutput | None = None,
        preview: bool = False,
        audio: bool = False,
        rtmp: str | None = None,
        screen_node: int | None = None,
        screen_fds: dict[str, int] | None = None,
        screen_sizes: dict[str, tuple[int, int]] | None = None,
        preview_crop_layer: str = "",
        preview_target: tuple[int, int] | None = None,
        preview_gl_sink: Callable[[Gst.Element], bool] | None = None,
        canvas: str | None = None,
        skip_layers: set[str] | None = None,
        pool: SourcePool | None = None,
        on_frame: Callable | None = None,
    ) -> None:
        self.project = project
        self.scene = scene or project.scene
        # Layer ids whose source is temporarily unavailable (a camera that was
        # unplugged).  A skipped camera is rendered as a placeholder card so
        # its box and z-order survive; other skipped kinds are left out.  The
        # GUI rebuilds with the real source when the device returns.
        self.skip_layers = set(skip_layers or ())
        # Capture lives in its own pipeline per source (audit M1 slice 2).
        # The GUI may pass a pool it keeps across rebuilds; otherwise the
        # engine owns one and releases every capture on stop().
        self.pool = pool or SourcePool()
        self._owns_pool = pool is None
        self._specs: list[CaptureSpec] = []
        # Capture specs by layer id and the subset currently holding a pool
        # reference.  Hidden layers keep their tail but release the source.
        self._layer_specs: dict[str, CaptureSpec] = {}
        self._layer_acquired: set[str] = set()
        self._camera_modes: dict[str, str] = {}
        # Per-layer pad counters.  A lifetime compositor FPS hides a single
        # source that stopped while the others keep the canvas alive.
        self._layer_frames: dict[str, int] = {}
        self._layer_last: dict[str, float] = {}
        self._source_rate_history: dict[str, deque] = {}
        # When each visible layer was first seen by the health check.  A source
        # that has not delivered yet is only "stalled" after its own grace, not
        # measured from the canvas start (a camera shown later is still opening).
        self._health_since: dict[str, float] = {}
        self._last_sample_frames = 0
        self._last_sample_time = 0.0
        self._stall_seconds = 2.0
        # The crop editor shows this layer uncropped while the model keeps its
        # real crop, so the window is drawn over the picture it edits.
        self.preview_crop_layer = preview_crop_layer
        # The device-pixel size the preview widget shows the canvas at.  The
        # preview branch scales to exactly that, so the GUI paints 1:1.
        self.preview_target = preview_target
        # Set by a window with a GPU preview: called with the new qml6glsink
        # so it can be pointed at the window's video item.  False from it (or
        # no qml6glsink) falls back to the appsink preview.
        self.preview_gl_sink = preview_gl_sink
        # "gpu": layers are uploaded and mixed by glvideomixer, one download
        # feeds every output.  "cpu": the compositor element.  None follows
        # ANYCAM_CANVAS (default CPU, so direct engine users keep the
        # established path).  ``canvas_backend`` is what was actually built.
        requested = canvas if canvas in ("cpu", "gpu") else (canvas_requested() or "cpu")
        self.canvas = requested
        self.canvas_backend = ""
        # "gpu" (qml6glsink) or "image" (appsink -> QImage) once built.
        self.preview_backend = ""
        self.canvas_w = int(project.output.canvas_w)
        self.canvas_h = int(project.output.canvas_h)
        self.fps = int(project.output.fps)
        self.recorder = recorder
        if recorder is not None:
            self.record_path = str(recorder.path)
        else:
            self.record_path = str(record_path) if record_path else ""
        self.want_vcam = vcam
        self.vcam_output = vcam_output
        self.preview = preview
        self.audio = audio
        self.rtmp = rtmp or ""
        self.screen_node = screen_node
        self.screen_fds = dict(screen_fds or {})
        # Portal stream sizes, keyed by layer id: used to crop a screen layer
        # against the real stream and to detect the wrong node being captured.
        self.screen_sizes = dict(screen_sizes or {})
        self.on_frame = on_frame
        self.report = EngineReport()
        self.pipeline: Gst.Pipeline | None = None
        self.compositor: Gst.Element | None = None
        self.tee: Gst.Element | None = None
        self.bus: Gst.Bus | None = None
        self.chains: list[SourceChain] = []
        self._layer_pads: dict[str, Gst.Pad] = {}
        # Permanent zorder=0 standby.  It is deliberately not a scene layer:
        # no remove/add/layout operation can ever detach this pad.
        self._background_pad: Gst.Pad | None = None
        self._background_chain: SourceChain | None = None
        # A screen layer's pipewiresrc owns the portal stream: keep the chain
        # addressable so deleting the layer can hide its pad without tearing
        # the source down.  Re-adding then relinks that same source.
        self._layer_chains: dict[str, SourceChain] = {}
        self._detached_screens: dict[str, list[dict]] = {}
        # Preview delivery waits until every compositor pad has produced a
        # buffer, so the first shown frame is not the bare background.
        self._pads_with_data: set[str] = set()
        self._preview_gate_open = False
        self._preview_gate_deadline = 0.0
        self.media_volume: Gst.Element | None = None
        self.audio_tee: Gst.Element | None = None
        # Live output taps.  A valve lets the GUI stop feeding the recorder or
        # vcam without rebuilding (and therefore interrupting) the canvas.
        self._recorder_valve: Gst.Element | None = None
        self._record_encoder: Gst.Element | None = None
        self._record_plan: EncoderPlan | None = None
        self._vcam_valve: Gst.Element | None = None
        self._probe_id = 0
        self._frame_counter = None
        self._frame_count = 0
        self._start_time = 0.0
        self._proc = psutil.Process()
        self._vcam_sink = None
        self._vcam_pipeline: Gst.Pipeline | None = None
        self._vcam_appsrc: Gst.Element | None = None
        self._vcam_bus: Gst.Bus | None = None
        # Streaming lives in its own pipeline (audit M1, slice 1).
        self._rtmp_pipeline: Gst.Pipeline | None = None
        self._rtmp_bus: Gst.Bus | None = None
        self._rtmp_appsrc: Gst.Element | None = None
        self._rtmp_audio_appsrc: Gst.Element | None = None
        self._rtmp_failed = False
        self._rtmp_encoder: Gst.Element | None = None
        self._rtmp_feed: EncodedFeed | None = None
        self._stopped = False
        # Every signal handler and pad probe this engine installs.  They point
        # back at the engine, so until they are removed neither the engine nor
        # its pipeline can be freed: each rebuild leaked a whole graph.
        self._signal_handlers: list[tuple[object, int]] = []
        self._pad_probes: list[tuple[Gst.Pad, int]] = []
        # Source size per layer, for the crop editor's picture rectangle.
        self.layer_sources: dict[str, tuple[int, int]] = {}
        self._record_sink = None
        self._appsink = None
        self._preview_caps = None
        self._pending_sample = None
        # Rate limit for preview frames that cannot be handed to the GUI.
        self._preview_problem_at = 0.0
        self._preview_problem_count = 0
        self._branch_count = 0
        self._last_sample = 0.0
        self._eos_seen = False
        self._error_seen = False
        self.audio_mixer: Gst.Element | None = None
        self.mic_source: Gst.Element | None = None
        self.mic_volume: Gst.Element | None = None
        self.desktop_volume: Gst.Element | None = None
        self._vcam_error_from_output = False
        self.mic_level_db: float | None = None
        self._audio_eos_pads: list[Gst.Pad] = []

    # ------------------------------------------------------------------ build
    def _link_to_mixer(self, chain: SourceChain, pad: Gst.Pad, layer: Layer | None = None):
        """Link a chain's output into a mixer pad, uploading it for the GPU canvas.

        The upload elements join the chain, so state sync, disposal and probe
        cleanup treat them like the rest of the layer.  identity drops the
        allocation query: glupload's udmabuf pool is imported as EGLImages,
        which NVIDIA rejects (the texture stays black).
        """
        if self.canvas_backend == "gpu":
            no_pool = make("identity", None, drop_allocation=True)
            upload = make("glupload")
            self._add(no_pool, upload)
            chain.add(no_pool, upload)
            if not (chain.tail.link(no_pool) and no_pool.link(upload)):
                raise RuntimeError("could not upload a layer to the GPU canvas")
            chain.tail = upload
            gl_rack = gl_rack_elements() if chain.effects.get("balance") is not None else None
            if gl_rack:
                # The colour step and mirror as shaders (see apply_effects).
                shader, flip = gl_rack["gl_balance"], gl_rack["gl_flip"]
                self._add(shader, flip)
                chain.add(shader, flip)
                if not (upload.link(shader) and shader.link(flip)):
                    raise RuntimeError("could not add the GPU colour step")
                chain.tail = flip
                chain.effects.update(gl_rack)
                apply_effects(chain.effects, getattr(layer, "effects", None) or Effects())
        return chain.output_pad.link(pad)

    def _build_background(self) -> None:
        """Create the one compositor pad that is never removed."""
        assert self.compositor is not None
        chain = background_chain(self.canvas_w, self.canvas_h, self.fps)
        self._background_chain = chain
        self.chains.append(chain)
        self._add(*chain.elements)
        self._link(chain.elements)
        pad = self.compositor.request_pad_simple("sink_%u")
        if pad is None:
            raise RuntimeError("compositor refused the background pad")
        pad.set_property("xpos", 0)
        pad.set_property("ypos", 0)
        pad.set_property("width", self.canvas_w)
        pad.set_property("height", self.canvas_h)
        pad.set_property("zorder", 0)
        pad.set_property("alpha", 1.0)
        if self._link_to_mixer(chain, pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("could not link the AnyCam standby background")
        self._background_pad = pad
        print(
            f"[background] live AnyCam standby "
            f"{self.canvas_w}x{self.canvas_h}@{self.fps} zorder=0",
            file=sys.stderr, flush=True)

    def _sync_standby_label(self) -> None:
        """Show the standby "AnyCam" caption only while nothing is on the canvas.

        The standby slate sits behind every layer.  Its caption used to show
        through any transparent area (a chroma-keyed region, the bands around a
        fitted picture), so it is hidden as soon as a visible source exists.
        """
        chain = self._background_chain
        label = None
        if chain is not None:
            label = next((element for element in chain.elements
                          if element.get_name() == "background_label"), None)
        if label is None:
            return
        occupied = any(layer.visible and layer.id in self._layer_pads
                       for layer in self.scene.layers)
        label.set_property("silent", occupied)

    def _on(self, obj, signal: str, callback, *args) -> int:
        """``obj.connect`` that is undone when the engine stops."""
        handler = obj.connect(signal, callback, *args)
        self._signal_handlers.append((obj, handler))
        return handler

    def _probe(self, pad: Gst.Pad, mask, callback) -> int:
        """``pad.add_probe`` that is undone when the engine stops."""
        probe = pad.add_probe(mask, callback)
        if probe:
            self._pad_probes.append((pad, probe))
        return probe

    def _release_graph(self) -> None:
        """Drop every hook that keeps this engine and its pipeline alive."""
        for obj, handler in self._signal_handlers:
            try:
                obj.disconnect(handler)
            except Exception:  # noqa: BLE001 - the object may already be gone
                pass
        self._signal_handlers.clear()
        for pad, probe in self._pad_probes:
            try:
                pad.remove_probe(probe)
            except Exception:  # noqa: BLE001
                pass
        self._pad_probes.clear()

    def _add(self, *elements: Gst.Element, sync: bool = True) -> None:
        assert self.pipeline is not None
        for element in elements:
            if not self.pipeline.add(element):
                raise RuntimeError(
                    f"could not add {element.get_name()} to the canvas pipeline"
                )
            if sync:
                self._sync_state(element)

    @staticmethod
    def _sync_state(element: Gst.Element) -> None:
        try:
            element.sync_state_with_parent()
        except Exception:  # noqa: BLE001
            pass

    def _sync_chain(self, chain: SourceChain) -> None:
        """Start a chain that was added to the pipeline without running.

        On a running canvas an element that starts before it is linked pushes
        into nothing and posts ``not-linked``, which the GUI treats as a fatal
        pipeline error (a camera re-attached beside a screen share then looped
        through rebuilds).  So a live-added chain is linked first and started
        last, downstream elements before the ones that feed them.
        """
        for element in reversed(chain.elements):
            self._sync_state(element)

    def _link(self, elements: list[Gst.Element], skip_from=None) -> None:
        for a, b in zip(elements, elements[1:]):
            if a is skip_from:
                continue
            if not a.link(b):
                raise RuntimeError(f"could not link {a.get_name()} -> {b.get_name()}")

    def _link_dynamic(self, chain: SourceChain) -> None:
        decode = getattr(chain, "dynamic_from", None)
        if decode is None:
            return
        # The element immediately after decodebin is the video converter.
        index = chain.elements.index(decode)
        target = chain.elements[index + 1]

        def on_pad_added(_bin, pad, _target):
            caps = pad.get_current_caps() or pad.query_caps(None)
            if caps is None or caps.get_size() == 0:
                return
            name = caps.get_structure(0).get_name()
            if not name.startswith("video/"):
                return
            sink = _target.get_static_pad("sink")
            if sink is None or sink.is_linked():
                return
            pad.link(sink)

        self._on(decode, "pad-added", on_pad_added, target)

    def _add_layer(self, layer: Layer, zorder: int) -> None:
        """Build one layer into the canvas; leave no pool reference on failure."""
        try:
            self._add_layer_impl(layer, zorder)
        except Exception:
            self._rollback_layer_capture(layer.id)
            raise

    def _rollback_layer_capture(self, layer_id: str) -> None:
        """Give back the capture a half-built layer acquired."""
        spec = self._layer_specs.pop(layer_id, None)
        if layer_id in self._layer_acquired:
            self._layer_acquired.discard(layer_id)
            if spec is not None:
                self.pool.release(spec)
                if spec in self._specs:
                    self._specs.remove(spec)

    def _add_layer_impl(self, layer: Layer, zorder: int) -> None:
        assert self.compositor is not None
        box = self._box_pixels(layer)
        crop = ((0.0, 0.0, 0.0, 0.0) if layer.id == self.preview_crop_layer
                else layer.crop)
        source_layer = layer
        if layer.kind == "camera" and not layer.source:
            source_layer = copy.copy(layer)
            source_layer.source = config.camera_device()
        missing_media = ""
        if layer.kind in ("image", "logo", "face", "video", "gif") and layer.source:
            source_path = Path(layer.source).expanduser()
            if not source_path.exists():
                missing_media = str(source_path)
        if missing_media:
            # One unplugged/moved file must not refuse the whole scene.  Keep
            # the layer's box as an opaque card and let the other layers run.
            png = overlay.render_media_error_card(
                layer.name, missing_media, box[2], box[3])
            self.report.media_errors.append(
                f"{layer.name}: missing {missing_media}")
            source_layer = Layer(
                id=layer.id, kind="image", name=layer.name, source=str(png),
                box=layer.box, alpha=layer.alpha, z=layer.z,
                visible=layer.visible,
            )
            chain = image_chain(str(png), box[2], box[3], self.fps, source_layer,
                                fit=False)
        elif layer.kind == "camera" and layer.id in self.skip_layers:
            # The device is temporarily gone.  Keep the layer's box and z-order
            # in the graph as a placeholder card instead of collapsing the
            # slot; the GUI rebuilds with the real camera on reconnect.
            png = overlay.render_camera_placeholder(
                layer.name, layer.source, box[2], box[3])
            self.report.camera_placeholders.append(layer.name)
            source_layer = Layer(
                id=layer.id, kind="image", name=layer.name, source=str(png),
                box=layer.box, alpha=layer.alpha, z=layer.z,
                visible=layer.visible,
            )
            chain = image_chain(str(png), box[2], box[3], self.fps, source_layer)
        elif (layer.kind in CAPTURE_KINDS and layer.kind != "screen"
              and layer.id not in self.skip_layers):
            # Capture runs in its own pipeline, so a dead device cannot stop
            # the compositor and two layers on one camera share one capture.
            # Screen layers are the exception: their PipeWire consumer cannot
            # be handed to a second pipeline through intervideosrc (the canvas
            # then only ever sees the black fallback frames), so they are
            # built directly into the canvas by the else branch below.
            if crop != layer.crop:
                layer = copy.copy(layer)
                layer.crop = crop
            spec = capture_spec(layer, self.canvas_w, self.canvas_h, self.fps,
                                screen_fd=self.screen_fds.get(layer.id, -1),
                                screen_size=self.screen_sizes.get(layer.id))
            self._layer_specs[layer.id] = spec
            entry = None
            if layer.visible:
                entry = self.pool.acquire(spec)
                self._specs.append(spec)
                self._layer_acquired.add(layer.id)
                source_size = entry.source_size
                channel = entry.channel
            else:
                # A hidden layer must not open its device or run a capture
                # pipeline.  Its intervideosrc waits on a stable channel and
                # set_layer_visible() acquires the source when it is shown.
                # Probe the size the capture will really have, so the crop and
                # fit built now stay right when the layer is shown later.
                source_size = self.pool.probe_size(spec)
                channel = self.pool.channel_for(spec)
            chain = layer_tail(layer, source_size, box[2], box[3],
                               self.fps, channel)
            self.layer_sources[layer.id] = source_size
            if entry is not None:
                self._record_camera_mode(layer.id, entry)
                if entry.probe_error:
                    self.report.camera_probe_error = entry.probe_error
                if entry.screen_error:
                    self.report.screen_error = entry.screen_error
        elif layer.kind in ("text", "logo", "image", "face"):
            if layer.kind == "face":
                png = Path(layer.source).expanduser()
                if not png.exists():
                    raise FileNotFoundError(f"face overlay asset not found: {png}")
            else:
                png = overlay.render_layer(layer, self.canvas_w, self.canvas_h,
                                           crop=crop)
            # render_layer already applied the layer's crop while rendering,
            # so the chain must not crop the box-sized PNG again.  Effects are
            # carried over: they used to be dropped on image/logo/text layers.
            source_layer = Layer(
                id=layer.id, kind="image", name=layer.name, source=str(png),
                box=layer.box, crop=(0.0, 0.0, 0.0, 0.0), alpha=layer.alpha,
                z=layer.z, visible=layer.visible, effects=layer.effects,
                role=layer.role,
            )
            # The PNG already encodes the intended aspect (transparent padding
            # included); fitting it again would add black bars over the video.
            chain = image_chain(str(png), box[2], box[3], self.fps, source_layer,
                                fit=False)
        else:
            if crop != layer.crop:
                source_layer = copy.copy(layer)
                source_layer.crop = crop
            chain = build_chain(
                source_layer, box[2], box[3], self.fps, self.screen_node,
                screen_fd=self.screen_fds.get(layer.id),
                canvas=(self.canvas_w, self.canvas_h),
                screen_size=self.screen_sizes.get(layer.id),
                screen_flip=self.project.output.screen_flip,
            )
            self._watch_source_size(chain, layer)
            mode = getattr(chain, "camera_mode", None)
            if mode is not None:
                requested_fps = getattr(chain, "capture_request", (0, 0, self.fps))[2]
                self.report.camera_mode = f"{mode.pixel_format} {mode.width}x{mode.height}@{mode.fps}"
                if mode.fps < requested_fps:
                    self.report.camera_mode += f" (camera fallback from {requested_fps} fps)"
            probe_error = getattr(chain, "camera_probe_error", "")
            if probe_error:
                self.report.camera_probe_error = probe_error
        source_size = getattr(chain, "source_size", None)
        if layer.kind in ("image", "logo") and not missing_media:
            source_size = overlay.image_size(layer) or source_size
        self.layer_sources[layer.id] = (tuple(source_size) if source_size
                                        else (box[2], box[3]))
        self.chains.append(chain)
        self._add(*chain.elements, sync=False)
        self._link(chain.elements, skip_from=getattr(chain, "dynamic_from", None))
        self._link_dynamic(chain)

        screen_identity = [None]

        retime = self.canvas_backend == "gpu" and layer.kind in LIVE_KINDS

        def on_source_buffer(_pad, _info, layer_id=layer.id):
            # PipeWire's SPA header sequence is carried in buffer.offset.
            # Some producers send PTS=0 for every frame, so timestamps alone
            # incorrectly label a changing stream as stale. Prefer sequence
            # numbers, falling back to PTS when the source has no sequence.
            if layer.kind == "screen":
                buffer = _info.get_buffer()
                if buffer is not None:
                    identity = None
                    if buffer.offset != Gst.BUFFER_OFFSET_NONE:
                        identity = ("sequence", buffer.offset)
                    elif buffer.pts != Gst.CLOCK_TIME_NONE:
                        identity = ("pts", buffer.pts)
                    # A portal stream is live, but its producer's PTS need
                    # not advance (niri supplies zero). do-timestamp on
                    # pipewiresrc does not replace an existing PTS. With two
                    # such streams the compositor can keep presenting the
                    # first frame even as their sequence numbers advance.
                    # Put each arrival on the canvas timeline, before any
                    # queue/GL processing. Preserve the original identity
                    # above so retiming a cached frame cannot fake freshness.
                    pipeline = self.pipeline   # None once the engine stopped
                    clock = pipeline.get_clock() if pipeline is not None else None
                    if clock is not None:
                        buffer.pts = max(0, clock.get_time() - pipeline.get_base_time())
                        buffer.dts = Gst.CLOCK_TIME_NONE
                    if identity is not None:
                        if identity == screen_identity[0]:
                            return Gst.PadProbeReturn.OK
                        screen_identity[0] = identity
            elif retime:
                # GPU canvas: the frame leaves the CPU chain now.  Its capture
                # timestamp is older by however long the effects took, and
                # glvideomixer drops a frame it considers late and repeats its
                # last one: a Distort filter (tens of ms per 1080p frame) froze
                # the camera completely.  Stamped with the time it is ready, it
                # is never late.  The compositor shows late frames, so the CPU
                # canvas keeps the capture time.
                buffer = _info.get_buffer()
                pipeline = self.pipeline
                clock = pipeline.get_clock() if pipeline is not None else None
                if buffer is not None and clock is not None:
                    buffer.pts = max(0, clock.get_time() - pipeline.get_base_time())
                    buffer.dts = Gst.CLOCK_TIME_NONE
            self._layer_frames[layer_id] = self._layer_frames.get(layer_id, 0) + 1
            self._layer_last[layer_id] = time.monotonic()
            return Gst.PadProbeReturn.OK

        # Screen health follows PipeWire arrivals, not a downstream element
        # that may repeat cached frames at the canvas rate.
        health_pad = (chain.check_element.get_static_pad("src")
                      if layer.kind == "screen" else chain.output_pad)
        self._probe(health_pad, Gst.PadProbeType.BUFFER, on_source_buffer)
        pad = self.compositor.request_pad_simple("sink_%u")
        if pad is None:
            raise RuntimeError("compositor refused a request pad")
        pad.set_property("xpos", box[0])
        pad.set_property("ypos", box[1])
        pad.set_property("width", box[2])
        pad.set_property("height", box[3])
        pad.set_property("zorder", int(zorder))
        # The chain delivers the picture at its natural aspect.  The pad is the
        # layer box; centring inside it (instead of stretching to it) is what
        # lets a live move/resize or layout change skip a canvas rebuild.
        pad.set_property("sizing-policy", "keep-aspect-ratio")
        pad.set_property("alpha", float(clamp(layer.alpha)) if layer.visible else 0.0)
        if self._link_to_mixer(chain, pad, layer) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"could not link {layer.name} into the compositor")
        self._layer_pads[layer.id] = pad
        self._layer_chains[layer.id] = chain

        def on_first_buffer(_pad, _info, layer_id=layer.id):
            self._pads_with_data.add(layer_id)
            # Only the first buffer matters; staying installed made every
            # frame of every layer take Python's GIL for nothing.
            return Gst.PadProbeReturn.REMOVE

        # Not tracked in _pad_probes: it removes itself after one buffer, and
        # removing it again at shutdown warned "has no probe with id".
        pad.add_probe(Gst.PadProbeType.BUFFER, on_first_buffer)
        # Linked all the way into the compositor: now it may start.
        self._sync_chain(chain)

    def _record_camera_mode(self, layer_id: str, entry) -> None:
        """Keep one negotiated mode per camera layer, not just the last one."""
        if entry.camera_mode:
            self._camera_modes[layer_id] = entry.camera_mode
            self.report.camera_mode = entry.camera_mode
            self.report.camera_modes = list(self._camera_modes.values())

    def _watch_source_size(self, chain: SourceChain, layer: Layer) -> None:
        """Report a screen layer whose frames are not the portal's stream.

        ``pipewiresrc`` happily connects to a different node (a webcam, for
        example) and the picture looks plausible in a small preview.  Compare
        the negotiated frame size with the size the portal reported once.
        """
        expected = getattr(chain, "expected_size", None)
        element = getattr(chain, "check_element", None)
        if not expected or element is None:
            return
        pad = element.get_static_pad("src")
        if pad is None:
            return
        seen: list[bool] = []

        def on_buffer(probe_pad, _info):
            if seen:
                return Gst.PadProbeReturn.OK
            caps = probe_pad.get_current_caps()
            if caps is None or caps.get_size() == 0:
                return Gst.PadProbeReturn.OK
            seen.append(True)
            struct = caps.get_structure(0)
            got = (int(struct.get_value("width")), int(struct.get_value("height")))
            # The portal reports the logical size while the buffer is in
            # physical pixels (a 1.5x scale turns 2560x1440 into 3840x2160),
            # so compare the shape, not the exact size: an exact check warned
            # about a perfectly good HiDPI share.
            got_ratio = got[0] / got[1] if got[1] else 0.0
            want_ratio = expected[0] / expected[1] if expected[1] else 0.0
            if want_ratio and abs(got_ratio - want_ratio) > 0.02 * want_ratio:
                self.report.screen_error = (
                    f"screen layer '{layer.name}' captured {got[0]}x{got[1]} "
                    f"but the portal stream is {expected[0]}x{expected[1]}"
                )
            return Gst.PadProbeReturn.OK

        self._probe(pad, Gst.PadProbeType.BUFFER, on_buffer)

    def _box_pixels(self, layer: Layer) -> tuple[int, int, int, int]:
        rect = box_pixels(layer, self.canvas_w, self.canvas_h)
        return (rect.x, rect.y, rect.w, rect.h)

    def build(self) -> "AnyCamEngine":
        """Build the graph, or leave nothing behind when any step fails."""
        try:
            return self._build()
        except Exception:
            self._abort_build()
            raise

    def _run_chain_cleanup(self) -> None:
        for chain in self.chains:
            for hook in getattr(chain, "cleanup", ()):
                try:
                    hook()
                except Exception:  # noqa: BLE001 - one hook must not block the rest
                    pass

    def _abort_build(self) -> None:
        """Undo a failed build without disturbing a pool other engines share."""
        self._run_chain_cleanup()
        for spec in list(self._specs):
            self.pool.release(spec)
        self._specs.clear()
        self._layer_acquired.clear()
        self._layer_specs.clear()
        if self._vcam_valve is not None:
            self._notify_canvas_attached(False)
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        if self._vcam_pipeline is not None:
            self._vcam_pipeline.set_state(Gst.State.NULL)
        self._teardown_rtmp()
        if self._owns_pool:
            self.pool.stop()
        self._close_screen_fds()
        self._release_graph()
        self.pipeline = None

    def _close_screen_fds(self) -> None:
        """Close the PipeWire remotes this engine was given.

        ``pipewiresrc`` duplicates the fd when it connects and never closes the
        original (measured: still open after the element reaches NULL), so the
        engine, which was handed ownership, must close it once the graph is
        down.  Without this every rebuild leaked one descriptor per screen.
        """
        for layer_id, fd in list(self.screen_fds.items()):
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self.screen_fds[layer_id] = -1

    def _build(self) -> "AnyCamEngine":
        # H.264 4:2:0 needs even dimensions; an odd canvas used to be accepted
        # and then killed the encoder after the graph was already running.
        if self.canvas_w % 2 or self.canvas_h % 2:
            raise ValueError(
                f"canvas size must be even for H.264 (got {self.canvas_w}x{self.canvas_h})"
            )
        self.pipeline = Gst.Pipeline.new("anycam")
        # A pad fed by ``intervideosrc`` is ignored from the first frame when
        # this is true, even when its capture is already PLAYING (measured:
        # 0 preview frames with True against 49 with False at 320x180), so the
        # canvas would never start.  It is not needed for a dead capture
        # either: the aggregator stops waiting for the silent pad on its own
        # and the remaining pads keep rendering.
        use_gpu = self.canvas == "gpu" and not gl_canvas_problem()
        if self.canvas == "gpu" and not use_gpu:
            print(f"[canvas] GPU canvas unavailable ({gl_canvas_problem()}); using the CPU compositor",
                  file=sys.stderr, flush=True)
        self.canvas_backend = "gpu" if use_gpu else "cpu"
        if use_gpu:
            # Same pad properties as compositor (xpos/ypos/width/height/alpha/
            # zorder/sizing-policy); mixing ~120-140% of a core -> ~35-40%
            # at 1080p60 including the uploads and the one download.
            self.compositor = make("glvideomixer", "compositor", background="black")
            # Room for a layer whose effects add processing time.  With none,
            # a colour look (~14 ms per 1080p frame in videobalance) made the
            # camera's frames late, glvideomixer dropped every one of them and
            # kept showing its last frame: frames flowed at 60/s but the camera
            # froze.  10 ms was not enough, 20 ms was; 33 ms leaves headroom.
            self.compositor.set_property("latency", GL_MIXER_LATENCY_NS)
        else:
            self.compositor = make("compositor", "compositor", background="black", ignore_inactive_pads=False)
        self.tee = make("tee", "output")
        # A canvas-wide rate element keeps the composited timestamps monotonic
        # for h264parse/qtmux (two inter sources can start at the same PTS).
        # ``skip-to-first`` stops it emitting black buffers before the first
        # composited frame has arrived, which otherwise flash on the preview.
        rate = make("videorate", "canvas_rate", skip_to_first=True)
        # The colour description is pinned.  Left open, it was renegotiated when
        # a camera joined a running canvas, the encoder then emitted new SPS
        # headers mid-stream, and the recorder's MP4 came out unreadable.
        canvas_caps = caps_filter(
            caps_string(self.canvas_w, self.canvas_h, self.fps) + ",colorimetry=sRGB",
            "canvas_caps")
        mixed = [self.compositor]
        self._gl_tee = None
        if use_gpu:
            # One download: recorder, virtual camera, RTMP and an image preview
            # take system-memory RGBA from the tee as before.  A GPU preview
            # takes the mixed texture from the GL tee before the download.
            self._gl_tee = make("tee", "canvas_gl_tee")
            gl_queue = make("queue", "canvas_download_queue", max_size_buffers=2,
                            max_size_time=0, max_size_bytes=0)
            mixed += [caps_filter(
                f"video/x-raw(memory:GLMemory),format=RGBA,width={self.canvas_w},"
                f"height={self.canvas_h},framerate={self.fps}/1", "canvas_gl_caps"),
                self._gl_tee, gl_queue, make("gldownload", "canvas_download")]
        self._add(*mixed, rate, canvas_caps, self.tee)
        self._link([*mixed, rate, canvas_caps, self.tee])
        # The compositor must never reach zero pads.  This one is live for the
        # whole canvas lifetime and sits behind every scene layer.
        self._build_background()

        # Build hidden layers too: the playlist toggles their pad alpha live,
        # which is impossible if they were never added to the compositor.
        layers = sorted(self.scene.layers, key=lambda l: l.z)
        # Skipped camera layers still build a placeholder card; skipped
        # non-camera layers are left out of the graph.
        layers = [layer for layer in layers
                  if layer.kind == "camera" or layer.id not in self.skip_layers]
        # An empty drawing is a full-canvas transparent layer blended every
        # frame for nothing (measured ~35% of a core at 1080p60).  The first
        # stroke cannot reach a layer that is not built, and the GUI already
        # rebuilds when set_layer_strokes() reports that.
        layers = [layer for layer in layers if not _empty_drawing(layer)]
        # zorder 0 belongs to the permanent standby; scene layers start at 1
        # so hiding/removing them can never expose a padless graph.  A failure
        # here is cleaned up by build(), which releases only this engine's
        # captures (the pool may be shared with the GUI).
        for zorder, layer in enumerate(layers, start=1):
            self._add_layer(layer, zorder)
        self._sync_standby_label()

        if self.preview:
            self._build_preview()
        media = self.project.audio.media_file
        if media and not Path(media).exists():
            self.report.media_error = f"media audio file not found: {media}"
        if self.audio:
            # Build audio whenever it is requested, even in preview-only mode.
            # The mixer's src is given a fakesink consumer inside _build_audio
            # so it never stays unlinked.
            self._build_audio()
        if self.want_vcam:
            self._build_vcam()
        if self.recorder is not None:
            self._bind_recorder()
            self._build_recorder_tap()
        elif self.record_path:
            self._build_recording()
        # Publishing is explicit: a URL saved in the project must never start
        # streaming just because preview was opened.  A missing/broken stream
        # element must not abort the preview or the recording; the failure is
        # reported on the output instead.
        if self.rtmp:
            try:
                self._build_rtmp()
            except Exception as exc:  # noqa: BLE001
                self.report.rtmp_error = config.redact_text(
                    f"stream output could not start: {exc}", self.rtmp)

        if self._branch_count == 0:
            # Without a tee branch the compositor's output pad stays unlinked
            # and GStreamer fails asynchronously with "not-linked" instead of
            # reporting the real mistake (nothing was asked to consume frames).
            raise RuntimeError(
                self.report.vcam_error
                or self.report.rtmp_error
                or "no output branch requested: enable preview, recording, vcam or RTMP"
            )

        self.bus = self.pipeline.get_bus()
        self._build_frame_counter()
        return self

    def _build_frame_counter(self) -> None:
        """Count canvas frames without entering Python per frame.

        A pad probe here made every canvas frame take Python's GIL; a gdb
        sample caught the compositor thread waiting for it while the GUI held
        it.  A fakesink straight on the output tee (no queue, no thread)
        counts in C; ``_frame_count`` reads its ``stats`` when sampled.
        """
        assert self.tee is not None
        sink = make("fakesink", "frame_counter", sync=False,
                    enable_last_sample=False, silent=True)
        sink.set_property("async", False)
        self._add(sink)
        tee_pad = self.tee.request_pad_simple("src_%u")
        if tee_pad is None or tee_pad.link(sink.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            raise RuntimeError("could not attach the canvas frame counter")
        self._frame_counter = sink

    @staticmethod
    def _rendered(sink) -> int:
        if sink is None:
            return 0
        try:
            return int(sink.get_property("stats").get_value("rendered") or 0)
        except Exception:  # noqa: BLE001 - a disposed sink counts nothing
            return 0

    @property
    def _frame_count(self) -> int:
        """Canvas frames since the last reset (start)."""
        return self._frame_base + self._rendered(self._frame_counter) - self._frame_zero

    @_frame_count.setter
    def _frame_count(self, value: int) -> None:
        self._frame_zero = self._rendered(getattr(self, "_frame_counter", None))
        self._frame_base = int(value)

    def _branch(self, name: str, max_buffers: int = 4,
                max_time_ns: int = 200_000_000) -> Gst.Element:
        assert self.tee is not None
        queue = make("queue", f"queue_{name}", max_size_buffers=max_buffers,
                     max_size_time=max_time_ns)
        self._add(queue)
        tee_pad = self.tee.request_pad_simple("src_%u")
        if tee_pad is None or tee_pad.link(queue.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"could not create {name} branch")
        try:
            tee_pad.set_active(True)
        except Exception:  # noqa: BLE001
            pass
        self._branch_count += 1
        return queue

    # The largest preview frame.  Every frame is copied once into Python
    # (PyGObject maps a buffer into new ``bytes``), so its size is paid per
    # frame on the GUI thread; above this the widget upscales it on paint,
    # which costs less than the bigger copy.
    PREVIEW_MAX = (960, 540)

    def _preview_size(self) -> tuple[int, int]:
        """The preview frame size: the widget's shown size, at most PREVIEW_MAX.

        A smaller widget gets a smaller frame (a cheaper copy and paint).
        Only the preview is scaled: canvas, recording and the virtual camera
        keep the project resolution.  Never larger than the canvas.
        """
        limit_w, limit_h = self.preview_target or self.PREVIEW_MAX
        limit_w = max(2, min(int(limit_w), self.PREVIEW_MAX[0]))
        limit_h = max(2, min(int(limit_h), self.PREVIEW_MAX[1]))
        scale = min(1.0, limit_w / max(1, self.canvas_w),
                    limit_h / max(1, self.canvas_h))
        width = max(2, int(self.canvas_w * scale) // 2 * 2)
        height = max(2, int(self.canvas_h * scale) // 2 * 2)
        return width, height

    def set_preview_target(self, width: int, height: int) -> bool:
        """Follow a resized preview widget without rebuilding the graph.

        The capsfilter after ``videoscale`` is changed in place; videoscale
        renegotiates on the next buffer.  True when the size changed.
        """
        previous = self._preview_size()
        self.preview_target = (int(width), int(height))
        size = self._preview_size()
        if size == previous:
            return False
        caps = self._preview_caps
        if caps is not None:
            caps.set_property("caps", Gst.Caps.from_string(
                caps_string(size[0], size[1], self.fps)))
        return True

    def _build_preview(self) -> None:
        if self.preview_gl_sink is not None and self._build_gl_preview():
            self.preview_backend = "gpu"
            return
        self._build_image_preview()
        self.preview_backend = "image"

    def _build_gl_preview(self) -> bool:
        """Canvas -> texture -> the window's Qt Quick video item.

        No frame enters Python and the GUI thread only composites.  The
        identity drops the allocation query: otherwise glupload proposes a
        udmabuf pool upstream and imports those buffers as EGLImages, which
        NVIDIA rejects ("<image> and <target> are incompatible"), leaving the
        item transparent.  Without the pool glupload uses a plain texture
        upload.
        """
        if Gst.ElementFactory.find("qml6glsink") is None:
            return False
        sink = Gst.ElementFactory.make("qml6glsink", "preview_gl_sink")
        if sink is None:
            return False
        sink.set_property("sync", False)
        try:
            attached = bool(self.preview_gl_sink(sink))
        except Exception as exc:  # noqa: BLE001 - the image preview takes over
            print(f"[preview] GPU preview unavailable: {exc}", file=sys.stderr, flush=True)
            attached = False
        if not attached:
            return False
        if getattr(self, "_gl_tee", None) is not None:
            # GPU canvas: the mixed texture goes straight to the preview, with
            # no download and no second upload.  Sinks start first, so the
            # mixer's GL context is created shared with Qt's.
            queue = make("queue", "queue_preview", max_size_buffers=1,
                         max_size_time=0, max_size_bytes=0)
            queue.set_property("leaky", 2)
            self._add(queue, sink)
            tee_pad = self._gl_tee.request_pad_simple("src_%u")
            if tee_pad is None or tee_pad.link(queue.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
                raise RuntimeError("could not branch the GPU preview off the GL canvas")
            self._link([queue, sink])
            self._branch_count += 1
            self._preview_gl_sink_element = sink
            self._appsink = None
            self._pending_sample = None
            return True
        queue = self._branch("preview", max_buffers=1, max_time_ns=0)
        queue.set_property("leaky", 2)
        no_pool = make("identity", "preview_no_pool", drop_allocation=True)
        upload = make("glupload", "preview_gl_upload")
        convert = make("glcolorconvert", "preview_gl_convert")
        self._add(no_pool, upload, convert, sink)
        self._link([queue, no_pool, upload, convert, sink])
        self._preview_gl_sink_element = sink
        self._appsink = None
        self._pending_sample = None
        return True

    @property
    def preview_gl_frames(self) -> int:
        """Frames the GPU preview's sink rendered (read from its stats)."""
        return self._rendered(getattr(self, "_preview_gl_sink_element", None))

    def _build_image_preview(self) -> None:
        # One buffered frame, leaking downstream instead of blocking: when the
        # window gains focus or is resized the GUI thread can stall for a
        # moment, and a blocking queue would then lock the compositor up.
        # ``leaky=2`` drops the oldest queued frame, so the preview always
        # shows the newest one instead of replaying stale frames.
        queue = self._branch("preview", max_buffers=1, max_time_ns=0)
        queue.set_property("leaky", 2)

        convert = make("videoconvert")
        format_caps = caps_filter("video/x-raw,format=RGBA", "preview_format")
        scale = make("videoscale")
        preview_w, preview_h = self._preview_size()
        caps = caps_filter(caps_string(preview_w, preview_h, self.fps), "preview_caps")
        self._preview_caps = caps
        sink = make("appsink", "preview_sink", emit_signals=True,
                    max_buffers=2, drop=True, sync=False)
        self._add(convert, format_caps, scale, caps, sink)
        self._link([queue, convert, format_caps, scale, caps, sink])
        # The streaming thread only stores the newest sample.  Delivery to
        # ``on_frame`` happens from pump() on the caller's thread, which keeps
        # Qt calls on the main thread and works for the headless demo too.
        self._on(sink, "new-sample", self._on_preview_sample)
        self._appsink = sink
        self._pending_sample = None

    def _log_preview_problem(self, message: str) -> None:
        """Report a frame the preview cannot paint instead of showing black.

        The streaming thread can hit one bad buffer per frame, so keep the log
        readable by printing at most one message a second.
        """
        self._preview_problem_count += 1
        now = time.monotonic()
        if now - self._preview_problem_at < 1.0:
            return
        self._preview_problem_at = now
        print(f"[preview] dropped frame: {message}", file=sys.stderr, flush=True)

    def _on_preview_sample(self, sink):
        """Keep the newest sample, rejecting the obviously malformed ones.

        Deliberately no ``map()`` here: mapping the composited buffer in the
        appsink callback (and again in the GUI to actually read it) doubles the
        per-frame work on the streaming thread and adds seconds of lag.  The
        GUI maps once in ``_on_frame`` and reports mapping/short-frame errors.
        """
        sample = sink.emit("pull-sample")
        if sample is None:
            self._log_preview_problem("appsink returned no sample")
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps()
        if buf is None or caps is None or caps.get_size() == 0:
            self._log_preview_problem("sample without buffer or caps")
            return Gst.FlowReturn.OK
        structure = caps.get_structure(0)
        if not (structure.has_field("width") and structure.has_field("height")):
            self._log_preview_problem(
                f"caps without dimensions: {structure.to_string()}")
            return Gst.FlowReturn.OK
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        if width <= 0 or height <= 0:
            self._log_preview_problem(f"invalid preview size {width}x{height}")
            return Gst.FlowReturn.OK
        if str(structure.get_value("format")) not in ("RGBA", "RGB"):
            self._log_preview_problem(
                f"unexpected preview format {structure.get_value('format')!r}")
            return Gst.FlowReturn.OK
        self._pending_sample = sample  # newest frame wins
        return Gst.FlowReturn.OK

    def deliver_frame(self) -> bool:
        """Hand the newest preview sample to ``on_frame`` (caller's thread)."""
        sample = self._pending_sample
        if sample is None:
            return False
        self._pending_sample = None
        if not self._preview_gate_open:
            if (len(self._pads_with_data) < len(self._layer_pads)
                    and time.monotonic() < self._preview_gate_deadline):
                # The compositor can emit background frames before every layer
                # has produced its first buffer; showing one flashes the
                # preview black.  Hold delivery until all pads are live
                # (bounded by the deadline so a stalled source cannot freeze
                # the preview).
                return False
            self._preview_gate_open = True
            # The pending sample was composited while some pads were blank;
            # drop it so the first shown frame contains every layer.
            return False
        if self.on_frame is not None:
            try:
                self.on_frame(sample)
            except Exception as exc:  # a bad consumer must not kill streaming
                self.report.error = f"frame callback failed: {exc}"
                return False
        return True

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

    def layers_for_channel(self, channel: str) -> set[str]:
        """Ids of the layers fed by capture ``channel`` (as named in errors)."""
        return {layer_id for layer_id, spec in self._layer_specs.items()
                if self.pool.channel_for(spec) == channel}

    def update_overlay(self, layer: Layer) -> bool:
        """Apply a ticker or lower third's edited text and colours live."""
        control = getattr(self._layer_chains.get(layer.id), "control", None)
        if layer.kind == "particles":
            if control is None or not hasattr(control, "set_source"):
                return False
            control.set_source(layer.source)
            return True
        if control is None or not hasattr(control, "reconfigure"):
            return False
        if layer.kind == "ticker":
            control.reconfigure(layer.source, layer.color, layer.background)
        elif layer.kind == "lower_third":
            name, _sep, title = str(layer.source or "").partition("|")
            control.reconfigure(name.strip(), title.strip(), layer.color, layer.background)
        else:
            return False
        return True

    def set_layer_strokes(self, layer_id: str, strokes: list[dict]) -> bool:
        """Replace a drawing layer's strokes on the running canvas (no rebuild)."""
        control = getattr(self._layer_chains.get(layer_id), "control", None)
        if control is None or not hasattr(control, "set_strokes"):
            return False
        control.set_strokes(strokes)
        return True

    def source_sizes(self) -> dict[str, tuple[int, int]]:
        """Source size per layer id (crop editor picture geometry)."""
        return dict(self.layer_sources)

    def set_layer_visible(self, layer_id: str, visible: bool) -> bool:
        """Show/hide an already-built layer without rebuilding the graph.

        Returns False when the layer could not be shown (its device could not
        be opened).  The source is acquired *before* the layer is made visible,
        so a failure leaves it cleanly hidden instead of visible with no feed.
        """
        layer = next((l for l in self.scene.layers if l.id == layer_id), None)
        pad = self._layer_pads.get(layer_id)
        if layer is None or pad is None:
            return False
        visible = bool(visible)
        spec = self._layer_specs.get(layer_id)
        if spec is not None and visible and layer_id not in self._layer_acquired:
            try:
                entry = self.pool.acquire(spec)
            except Exception as exc:  # noqa: BLE001 - keep the canvas running
                layer.visible = False
                pad.set_property("alpha", 0.0)
                self.report.media_errors.append(
                    f"{layer.name}: could not open source ({exc})")
                print(f"[capture] could not show layer={layer_id}: {exc}",
                      file=sys.stderr, flush=True)
                return False
            self._record_camera_mode(layer_id, entry)
            self._specs.append(spec)
            self._layer_acquired.add(layer_id)
            self.layer_sources[layer_id] = entry.source_size
        layer.visible = visible
        pad.set_property("alpha", float(clamp(layer.alpha)) if visible else 0.0)
        control = getattr(self._layer_chains.get(layer_id), "control", None)
        if visible and control is not None and hasattr(control, "restart"):
            control.restart()   # a lower third slides in again each time it is shown
        if spec is not None and not visible and layer_id in self._layer_acquired:
            self.pool.release(spec)
            if spec in self._specs:
                self._specs.remove(spec)
            self._layer_acquired.discard(layer_id)
        # The permanent standby keeps the canvas alive when every scene layer
        # is hidden, so the vcam must never be left frozen on an old frame.
        self._sync_standby_label()
        self.resume_vcam()
        return True

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

    def drop_capture(self, layer_id: str) -> bool:
        """Stop one layer's capture without touching the canvas or its pad.

        A camera can fail while the rest of the scene (notably a portal screen
        share) is healthy.  Rebuilding the canvas to replace the camera would
        destroy the live screen pipewiresrc and force another portal remote;
        instead the dead capture is released and the canvas keeps its pad
        until :meth:`attach_capture` rebuilds the capture on the same channel.
        """
        spec = self._layer_specs.get(layer_id)
        if spec is None or layer_id not in self._layer_acquired:
            return False
        self.pool.release(spec)
        self._layer_acquired.discard(layer_id)
        if spec in self._specs:
            self._specs.remove(spec)
        print(
            f"[capture] dropped layer={layer_id} without rebuilding the canvas",
            file=sys.stderr, flush=True)
        return True

    def attach_capture(self, layer_id: str) -> bool:
        """Restart a dropped capture on its existing canvas channel."""
        layer = next((item for item in self.scene.layers
                      if item.id == layer_id), None)
        if layer is None or not layer.visible:
            return False
        if layer_id in self._layer_acquired:
            return True
        spec = self._layer_specs.get(layer_id)
        if spec is None:
            # The camera was disconnected when this canvas was built, so it
            # has no capture spec or pad yet.  Build both now without touching
            # the screen branch.
            ordered = sorted(self.scene.layers, key=lambda item: item.z)
            try:
                zorder = max(1, ordered.index(layer) + 1)
            except ValueError:
                zorder = max(1, len(ordered))
            # The layer is only in skip_layers because its camera was missing
            # when this canvas was built.  Leaving it there would build
            # another placeholder card instead of the live capture.
            was_skipped = layer_id in self.skip_layers
            self.skip_layers.discard(layer_id)
            placeholder = self._layer_pads.get(layer_id)
            try:
                self._add_layer(layer, zorder)
                self.refresh_layer_order()
            except Exception as exc:  # noqa: BLE001 - keep the live screen
                if was_skipped:
                    self.skip_layers.add(layer_id)
                print(
                    f"[capture] could not build layer={layer_id}: {exc}",
                    file=sys.stderr, flush=True)
                return False
            if placeholder is not None and placeholder is not self._layer_pads.get(layer_id):
                # Hide the old card.  A requested pad is not released from a
                # PLAYING compositor (see detach_layer); the next rebuild
                # drops it for real.
                placeholder.set_property("alpha", 0.0)
            if layer.name in self.report.camera_placeholders:
                self.report.camera_placeholders.remove(layer.name)
            self._sync_standby_label()
            print(
                f"[capture] built layer={layer_id} live on the canvas",
                file=sys.stderr, flush=True)
            return True
        try:
            entry = self.pool.acquire(spec)
        except Exception as exc:  # noqa: BLE001 - keep the live screen alive
            print(
                f"[capture] could not reattach layer={layer_id}: {exc}",
                file=sys.stderr, flush=True)
            return False
        self._specs.append(spec)
        self._layer_acquired.add(layer_id)
        self.layer_sources[layer_id] = entry.source_size
        self._record_camera_mode(layer_id, entry)
        if entry.probe_error:
            self.report.camera_probe_error = entry.probe_error
        if entry.screen_error:
            self.report.screen_error = entry.screen_error
        print(
            f"[capture] reattached layer={layer_id} channel={entry.channel}",
            file=sys.stderr, flush=True)
        return True

    def detach_layer(self, layer_id: str) -> None:
        """Remove one layer's contribution without rebuilding the canvas.

        Removal must not destroy the source.  A screen layer's pipewiresrc
        owns the portal session, so it stays PLAYING, linked to an alpha=0
        compositor pad, and is reattached when the source is added back.  A
        pooled capture (camera/video/network/test) is pinned for the same
        reason; its device is not reopened on re-add.
        """
        layer = next((l for l in self.scene.layers if l.id == layer_id), None)
        if layer is None:
            return
        if layer.kind == "screen":
            self._detach_screen_layer(layer)
            self.resume_vcam()
            return
        spec = self._layer_specs.get(layer_id)
        if spec is not None:
            # Pin before set_layer_visible releases this layer's reference:
            # the capture pipeline keeps running for the app lifetime.
            self.pool.pin(spec)
        # set_layer_visible released this layer's own reference and dropped
        # exactly one entry from _specs.  Removing another here would take a
        # different layer's reference off the books when a capture is shared.
        self.set_layer_visible(layer_id, False)
        self._layer_specs.pop(layer_id, None)
        self._layer_acquired.discard(layer_id)
        pad = self._layer_pads.pop(layer_id, None)
        if pad is not None:
            pad.set_property("alpha", 0.0)
        removed = self._layer_chains.pop(layer_id, None)
        # A generated layer (animation, ticker, GIF, drawing) owns a feeder
        # thread.  Without this it kept drawing frames into an invisible pad
        # for the rest of the session, so every removed animation leaked a
        # running source and repeated add/remove slowly starved the canvas.
        for hook in getattr(removed, "cleanup", ()):
            try:
                hook()
            except Exception:  # noqa: BLE001 - one hook must not block removal
                pass
        if removed is not None:
            self._dispose_chain(removed, pad)
        self._layer_frames.pop(layer_id, None)
        self._layer_last.pop(layer_id, None)
        self._pads_with_data.discard(layer_id)
        self.layer_sources.pop(layer_id, None)
        self._camera_modes.pop(layer_id, None)
        self._sync_standby_label()

    def _dispose_chain(self, chain: SourceChain, pad: Gst.Pad | None) -> None:
        """Take a removed layer's elements out of the running canvas.

        They used to stay linked to an invisible pad until the next rebuild.
        Each one holds full-size frame buffers, so toggling animations or
        adding and removing sources grew memory by ~23 MB a time (the effect
        catalogue reached 8 GB).  The branch is stopped first, so nothing can
        push into the pad once it is released (that is what posted
        not-linked), then unlinked, released and removed.
        """
        if self.pipeline is None:
            return
        elements = list(chain.elements)
        members = set(elements)
        keep_probes = []
        for probe_pad, probe in self._pad_probes:
            owner = probe_pad.get_parent_element()
            if probe_pad is pad or owner in members:
                try:
                    probe_pad.remove_probe(probe)
                except Exception:  # noqa: BLE001
                    pass
            else:
                keep_probes.append((probe_pad, probe))
        self._pad_probes = keep_probes
        keep_handlers = []
        for obj, handler in self._signal_handlers:
            if obj in members:
                try:
                    obj.disconnect(handler)
                except Exception:  # noqa: BLE001
                    pass
            else:
                keep_handlers.append((obj, handler))
        self._signal_handlers = keep_handlers
        for element in elements:
            element.set_state(Gst.State.NULL)
        if pad is not None and self.compositor is not None:
            output = chain.tail.get_static_pad("src") if chain.tail else None
            if output is not None and output.get_peer() is pad:
                output.unlink(pad)
            try:
                self.compositor.release_request_pad(pad)
            except Exception:  # noqa: BLE001
                pass
        for element in elements:
            if element.get_parent() is self.pipeline:
                self.pipeline.remove(element)
        if chain in self.chains:
            self.chains.remove(chain)

    def _detach_screen_layer(self, layer: Layer) -> None:
        """Hide a screen layer while leaving its portal source untouched."""
        layer_id = layer.id
        pad = self._layer_pads.get(layer_id)
        chain = self._layer_chains.get(layer_id)
        if pad is not None:
            pad.set_property("alpha", 0.0)
        if pad is not None and chain is not None:
            self._detached_screens.setdefault(str(layer.source), []).append({
                "chain": chain,
                "pad": pad,
                "layer_id": layer_id,
                "crop": tuple(layer.crop),
                "effects": copy.deepcopy(layer.effects),
                "source_size": self.layer_sources.get(layer_id, (0, 0)),
                "frames": self._layer_frames.get(layer_id, 0),
                "last": self._layer_last.get(layer_id, 0.0),
            })
            print(
                f"[screen-session] detached layer={layer_id} "
                f"node={layer.source}; source stays PLAYING for re-add",
                file=sys.stderr, flush=True)
        layer.visible = False
        self._layer_pads.pop(layer_id, None)
        self._layer_chains.pop(layer_id, None)
        self._layer_frames.pop(layer_id, None)
        self._layer_last.pop(layer_id, None)
        self._pads_with_data.discard(layer_id)
        self.layer_sources.pop(layer_id, None)
        self._layer_specs.pop(layer_id, None)
        self._layer_acquired.discard(layer_id)
        self._camera_modes.pop(layer_id, None)
        self._sync_standby_label()

    def can_reattach_screen(self, layer: Layer) -> bool:
        """Whether adding ``layer`` reuses a still-PLAYING detached screen.

        The caller then has no need to open a new PipeWire remote for it.
        """
        if layer.kind != "screen":
            return False
        return any(tuple(layer.crop) == entry["crop"]
                   and layer.effects == entry["effects"]
                   for entry in self._detached_screens.get(str(layer.source), []))

    def add_layer(self, layer: Layer, *, screen_fd: int = -1,
                  screen_size: tuple[int, int] | None = None) -> bool:
        """Attach a new layer to the running canvas instead of rebuilding.

        Returns False when the graph is not running (the caller then builds
        one), or when attaching failed.  A previously removed screen layer is
        reattached to its still-PLAYING pipewiresrc and compositor pad, so no
        portal picker appears and the virtual camera never stops.
        """
        if self.pipeline is None or self.compositor is None or self._stopped:
            return False
        if layer.kind == "screen":
            detached = self._detached_screens.get(str(layer.source), [])
            for index, entry in enumerate(detached):
                if (tuple(layer.crop) != entry["crop"]
                        or layer.effects != entry["effects"]):
                    continue
                detached.pop(index)
                if not detached:
                    self._detached_screens.pop(str(layer.source), None)
                if screen_fd is not None and screen_fd >= 0:
                    # The still-PLAYING source needs no new remote; this one
                    # would otherwise leak.
                    try:
                        os.close(screen_fd)
                    except OSError:
                        pass
                pad = entry["pad"]
                chain = entry["chain"]
                self._layer_pads[layer.id] = pad
                self._layer_chains[layer.id] = chain
                self.layer_sources[layer.id] = entry["source_size"]
                self._layer_frames[layer.id] = entry["frames"]
                self._layer_last[layer.id] = entry["last"]
                self._pads_with_data.add(layer.id)
                pad.set_property(
                    "alpha",
                    float(clamp(layer.alpha)) if layer.visible else 0.0)
                self.set_layer_box(layer.id, layer.box)
                self.refresh_layer_order()
                self._sync_standby_label()
                check = getattr(chain, "check_element", None)
                health_pad = (check.get_static_pad("src")
                              if check is not None else None)
                if health_pad is not None:
                    def on_buffer(_pad, _info, layer_id=layer.id):
                        self._layer_frames[layer_id] = (
                            self._layer_frames.get(layer_id, 0) + 1)
                        self._layer_last[layer_id] = time.monotonic()
                        return Gst.PadProbeReturn.OK

                    self._probe(health_pad, Gst.PadProbeType.BUFFER, on_buffer)
                print(
                    f"[screen-session] reattached layer={layer.id} "
                    f"node={layer.source} without a portal request",
                    file=sys.stderr, flush=True)
                self.resume_vcam()
                return True
        if layer.kind == "screen":
            self.screen_fds[layer.id] = int(screen_fd)
            if screen_size is not None:
                self.screen_sizes[layer.id] = screen_size
        ordered = sorted(self.scene.layers, key=lambda item: item.z)
        try:
            zorder = max(1, ordered.index(layer) + 1)
        except ValueError:
            zorder = max(1, len(ordered))
        try:
            self._add_layer(layer, zorder)
        except Exception as exc:  # noqa: BLE001 - the caller rebuilds on failure
            print(
                f"[engine] could not attach {layer.name} live: {exc}",
                file=sys.stderr, flush=True)
            return False
        self.refresh_layer_order()
        self._sync_standby_label()
        self.resume_vcam()
        return True

    def set_layer_alpha(self, layer_id: str, alpha: float) -> None:
        """Change a layer's opacity without rebuilding the compositor."""
        layer = next((l for l in self.scene.layers if l.id == layer_id), None)
        if layer is None:
            return
        layer.alpha = float(clamp(alpha))
        pad = self._layer_pads.get(layer_id)
        if pad is not None and layer.visible:
            pad.set_property("alpha", layer.alpha)

    def set_layer_effects(self, layer_id: str, effects) -> bool:
        """Apply colour/detail/chroma changes to a running layer.

        The effect rack is built into every source chain up front, so this only
        writes element properties.  It never rebuilds the canvas, recreates a
        capture, or interrupts the virtual camera and recorder.
        """
        chain = self._layer_chains.get(layer_id)
        if chain is None:
            return False
        elements = getattr(chain, "effects", None)
        if not elements:
            return False
        apply_effects(elements, effects)
        return True

    def refresh_layer_order(self) -> None:
        """Push the model's z-order onto every existing compositor pad.

        zorder 0 is reserved for the permanent standby background, so scene
        layers always start at 1 and can never cover the graph's only pad if
        the scene becomes empty.
        """
        layers = sorted(self.scene.layers, key=lambda l: l.z)
        for zorder, layer in enumerate(layers, start=1):
            pad = self._layer_pads.get(layer.id)
            if pad is not None:
                pad.set_property("zorder", int(zorder))

    def set_layer_box(self, layer_id: str, box: tuple[float, float, float, float]) -> None:
        """Move/resize an existing layer pad without rebuilding the graph.

        Source chains negotiate a fixed frame size at build time; the
        compositor scales that frame into the pad, so changing the pad keeps
        the preview in sync with the model during a drag.
        """
        layer = next((l for l in self.scene.layers if l.id == layer_id), None)
        if layer is None:
            return
        layer.box = tuple(box)
        pad = self._layer_pads.get(layer_id)
        if pad is None:
            return
        rect = box_pixels(layer, self.canvas_w, self.canvas_h)
        # Do not write a pad property that already has this value.  A layout
        # click often re-applies the same full-frame box; an unnecessary
        # compositor pad update can reconfigure the sink and leave a
        # damage-driven screen source with no buffer to present.
        for name, value in (("xpos", int(rect.x)), ("ypos", int(rect.y)),
                            ("width", int(rect.w)), ("height", int(rect.h))):
            if int(pad.get_property(name)) != value:
                pad.set_property(name, value)

    # ------------------------------------------------------------------- run
    def start(self) -> "AnyCamEngine":
        if self._stopped:
            raise RuntimeError(
                "this engine was stopped; create a new AnyCamEngine")
        if self.pipeline is None:
            self.build()
        assert self.pipeline is not None
        self._start_time = time.monotonic()
        self.report.started = self._start_time
        self._pads_with_data.clear()
        self._preview_gate_open = False
        self._preview_gate_deadline = self._start_time + 2.0
        self._frame_count = 0
        self._last_sample = 0.0
        self._last_sample_frames = 0
        self._last_sample_time = self._start_time
        self._layer_frames.clear()
        self._layer_last.clear()
        self._source_rate_history.clear()
        self._health_since.clear()
        self.report.instant_fps = 0.0
        self.report.source_health = {}
        self.report.stalled_sources = []
        # Prefer the system clock: a USB/PipeWire microphone's clock can start
        # late and would delay the first composited frame (and the recording).
        self.pipeline.use_clock(Gst.SystemClock.obtain())
        # Captures must be live before the canvas prerolls, or the compositor
        # sees no active pad and the first preview frame is late.
        self.pool.start()
        if self._vcam_pipeline is not None:
            self._vcam_pipeline.set_state(Gst.State.PLAYING)
        if self._rtmp_pipeline is not None:
            self._rtmp_pipeline.set_state(Gst.State.PLAYING)
        result = self.pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            self.report.error = "pipeline failed to start"
            self._abort_build()
            self._vcam_bus = None
            raise RuntimeError(self.report.error)
        return self

    def pump(self, timeout_ms: int = 50) -> list[Gst.Message]:
        if self.bus is None:
            return []
        messages: list[Gst.Message] = []
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            left = max(0, int((deadline - time.monotonic()) * 1000))
            msg = self.bus.timed_pop_filtered(
                # Gst.Bus.timed_pop_filtered takes a Gst.ClockTime in
                # nanoseconds.  Passing milliseconds made a 100 ms wait return
                # after ~1 ms, so stop() spun on the UI thread.
                left * Gst.MSECOND,
                Gst.MessageType.ERROR | Gst.MessageType.EOS
                | Gst.MessageType.WARNING | Gst.MessageType.ELEMENT,
            )
            if msg is None:
                break
            messages.append(msg)
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                self.report.error = config.redact_text(
                    f"{err.message} ({debug})", self.rtmp,
                    *(spec.source for spec in self._layer_specs.values()
                      if spec.kind == "network"))
                self._error_seen = True
                self._eos_seen = True
            elif msg.type == Gst.MessageType.EOS:
                self._eos_seen = True
            elif msg.type == Gst.MessageType.ELEMENT:
                struct = msg.get_structure()
                if struct is not None and struct.get_name() == "level":
                    peaks = struct.get_value("peak")
                    if peaks:
                        try:
                            self.mic_level_db = float(peaks[0])
                            self.report.mic_level_db = self.mic_level_db
                        except (TypeError, ValueError):
                            pass
            if time.monotonic() >= deadline:
                break
        self._poll_vcam_bus()
        if self.vcam_output is not None:
            self.vcam_output.poll()
            if self.vcam_output.error:
                self.report.vcam_error = self.vcam_output.error
                self.report.vcam_active = False
                self._vcam_error_from_output = True
            elif self._vcam_error_from_output:
                # The output cleared its own (push) error; stop reporting it.
                self._vcam_error_from_output = False
                self.report.vcam_error = ""
                self.report.vcam_active = self._vcam_valve is not None
        self._poll_rtmp_bus()
        self.pool.poll_errors()
        errors = self.pool.errors()
        # Clear stale errors too: a dropped capture is no longer failing, and
        # keeping its old message makes the GUI rebuild the canvas on every
        # tick and lose a live screen share.
        self.report.source_errors = [f"{channel}: {message}"
                                     for channel, message in errors.items()]
        self.deliver_frame()
        return messages

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

    def sample(self, force: bool = False) -> dict | None:
        now = time.monotonic()
        if not force and now - self._last_sample < 1.0:
            return None
        self._last_sample = now
        elapsed = max(1e-6, now - self._start_time)
        delta_frames = self._frame_count - self._last_sample_frames
        delta_time = now - self._last_sample_time
        instant_fps = (delta_frames / delta_time) if delta_time > 0 else 0.0
        self._update_health(now)
        self._last_sample_frames = self._frame_count
        self._last_sample_time = now
        try:
            cpu = self._proc.cpu_percent(None)
            rss = self._proc.memory_info().rss / 2 ** 20
        except psutil.Error:
            cpu, rss = None, None
        rec = {
            "t": round(elapsed, 2),
            "frames": self._frame_count,
            "fps": round(self._frame_count / elapsed, 2),
            "instant_fps": round(instant_fps, 2),
            "stalled_sources": list(self.report.stalled_sources),
            "cpu_pct": cpu,
            "rss_mib": round(rss, 1) if rss else None,
        }
        self.report.samples.append(rec)
        return rec

    def _update_health(self, now: float) -> None:
        """Snapshot visible sources and flag one that stopped delivering."""
        health: dict[str, dict] = {}
        stalled: list[str] = []
        for layer in self.scene.layers:
            if not layer.visible or layer.id not in self._layer_pads:
                self._health_since.pop(layer.id, None)
                continue
            since = self._health_since.setdefault(layer.id, now)
            spec = self._layer_specs.get(layer.id)
            entry = self.pool.entry(spec) if spec is not None else None
            if entry is not None:
                frames = entry.frames
            else:
                frames = self._layer_frames.get(layer.id, 0)
            last = (entry.last_frame or None) if entry is not None else self._layer_last.get(layer.id)
            age = None if last is None else max(0.0, now - last)
            # Measure the input, not the compositor's repeated output. Keep
            # a bounded three-second window so bursty ~1 Hz screen updates
            # cannot alternate between LIVE and SLOW on every short burst.
            history = self._source_rate_history.setdefault(
                layer.id, deque([(self._start_time, 0)], maxlen=16))
            if now < history[-1][0] or frames < history[-1][1]:
                history.clear()
                history.append((now, frames))
            if now - history[-1][0] >= 0.25:
                history.append((now, frames))
            while len(history) > 2 and history[1][0] <= now - 3.0:
                history.popleft()
            interval = now - history[0][0]
            source_fps = ((frames - history[0][1]) / interval
                          if interval >= 2.0 else None)
            health[layer.id] = {
                "frames": frames,
                "last_frame_age_s": None if age is None else round(age, 2),
                "fps": None if source_fps is None else round(source_fps, 2),
                # Static damage-driven shares may also be low-rate: report
                # what is measured without claiming the source is broken.
                "slow": (layer.kind == "screen" and source_fps is not None
                         and source_fps < min(10.0, self.fps / 2)),
            }
            if last is None:
                if now - max(self._start_time, since) > self._stall_seconds:
                    stalled.append(layer.id)
            elif age is not None and age > self._stall_seconds:
                stalled.append(layer.id)
        self.report.instant_fps = (
            (self._frame_count - self._last_sample_frames)
            / max(1e-6, now - self._last_sample_time)
        )
        self.report.source_health = health
        self.report.stalled_sources = sorted(stalled)

    def set_mic_gain(self, db: float) -> None:
        self.project.audio.mic_gain_db = db
        if self.mic_volume is not None:
            self.mic_volume.set_property("volume", 10 ** (db / 20.0))

    def set_mic_muted(self, muted: bool) -> None:
        self.project.audio.mic_muted = muted
        if self.mic_volume is not None:
            self.mic_volume.set_property("mute", bool(muted))

    def stop(self) -> dict:
        if self.pipeline is None:
            return self.report.as_dict()
        self.report.stopped = time.monotonic()
        elapsed = self.report.stopped - self._start_time
        stop_frames = self._frame_count
        # Stop upstream sources first so no buffer arrives after EOS.
        for chain in self.chains:
            for element in chain.pause_elements:
                element.set_state(Gst.State.PAUSED)
        time.sleep(0.12)
        # End the output branch, not the individual source pads.  EOS pushed
        # into an ``intervideosrc`` tail deadlocks against the compositor's
        # streaming thread, and EOS never crosses the inter channel from the
        # capture pipeline.  One event on the compositor's src pad reaches
        # every branch and finalises mp4mux/flvmux.
        # A closed valve drops events too, EOS included, so a detached recorder
        # or vcam tap made the wait below run to its full timeout on every
        # later rebuild.  Nothing consumes those taps any more; let EOS through.
        for valve in (self._recorder_valve, self._vcam_valve):
            if valve is not None:
                valve.set_property("drop", False)
        output_pad = self.compositor.get_static_pad("src") if self.compositor else None
        if output_pad is not None:
            try:
                output_pad.push_event(Gst.Event.new_eos())
            except Exception:
                pass
        # mp4mux/flvmux only finalise after every pad sees EOS.  Video alone is
        # not enough once audio is mixed in, so end the audio inputs too.
        for pad in self._audio_eos_pads:
            try:
                pad.push_event(Gst.Event.new_eos())
            except Exception:
                pass
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not self._eos_seen:
            self.pump(100)
        self.pipeline.set_state(Gst.State.NULL)
        self._run_chain_cleanup()
        # The tap died with this canvas.  Release the held device back to its
        # idle card so the next engine can attach, instead of leaving the
        # output thinking it still owns the feed.
        if self.vcam_output is not None:
            setter = getattr(self.vcam_output, "set_canvas_attached", None)
            if setter is not None:
                setter(False)
        for spec in self._specs:
            # A rebuild follows: let the next canvas take the camera over.
            self.pool.release(spec, linger=True)
        self._specs.clear()
        self._layer_acquired.clear()
        if self._owns_pool:
            self.pool.stop()
        self._close_screen_fds()
        if self._vcam_pipeline is not None:
            self._vcam_pipeline.send_event(Gst.Event.new_eos())
            self._vcam_pipeline.set_state(Gst.State.NULL)
            self._vcam_bus = None
        self._teardown_rtmp()
        # The graph is gone: a late add_layer()/start() must see that.
        self._stopped = True
        self._release_graph()
        self.report.frames = stop_frames
        self.report.fps = stop_frames / elapsed if elapsed > 0 else 0.0
        self.report.expected_frames = elapsed * self.fps
        self.report.dropped_frames = max(0.0, self.report.expected_frames - stop_frames)
        self.report.dropped_pct = 100.0 * self.report.dropped_frames / max(self.report.expected_frames, 1.0)
        finalised = self._eos_seen and not self._error_seen
        if self.recorder is None and self.record_path:
            path = Path(self.record_path)
            if path.exists():
                self.report.record_path = self.record_path
                self.report.record_bytes = path.stat().st_size
            self.report.record_ok = (
                finalised and self.report.record_bytes > 0
            )
            if not self.report.record_ok and not self.report.error:
                self.report.error = (
                    "recording did not finalise (no EOS before shutdown)"
                    if self.report.record_bytes
                    else "recording file was not written"
                )
        return self.report.as_dict()

    def run(self, seconds: float, sample_cb: Callable[[dict], None] | None = None,
            tick_cb: Callable[[float], None] | None = None) -> dict:
        try:
            self.start()
        except Exception:
            # A failed start must not leave captures or output pipelines up.
            self._abort_build()
            raise
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end and not self._eos_seen:
                self.pump(50)
                if tick_cb is not None:
                    tick_cb(time.monotonic() - self._start_time)
                rec = self.sample()
                if rec is not None and sample_cb:
                    sample_cb(rec)
                time.sleep(0.01)
        finally:
            report = self.stop()
        return report


def ffprobe(path: str) -> dict:
    """Return a compact ffprobe summary used by tests and the CLI."""
    import json
    import subprocess

    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()}
    data = json.loads(proc.stdout or "{}")
    streams = []
    for stream in data.get("streams", []):
        streams.append({
            "codec_type": stream.get("codec_type"),
            "codec_name": stream.get("codec_name"),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "r_frame_rate": stream.get("r_frame_rate"),
            "nb_frames": stream.get("nb_frames"),
            "duration": stream.get("duration"),
        })
    return {"format": data.get("format", {}), "streams": streams}


def _count_packets(path: str) -> int:
    """Count encoded video packets in a file.

    Fragmented MP4 has no ``nb_frames`` in the moov atom (sample tables live in
    each fragment), so ffprobe reports 0 and a healthy 10-minute recording was
    scored as 100% dropped frames.  Packet counting is accurate for both
    fragmented and faststart files.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-count_packets", "-show_entries", "stream=nb_read_packets",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    for line in (proc.stdout or "").splitlines():
        line = line.strip().rstrip(",")
        if line.isdigit():
            return int(line)
    return 0


def _parse_frame_rate(text) -> float:
    """``"60/1"`` -> 60.0; ``"0/0"`` (no frame rate) and junk -> 0.0."""
    try:
        num, _, den = str(text or "0/1").partition("/")
        denominator = float(den) if den else 1.0
        return float(num) / denominator if denominator else 0.0
    except ValueError:
        return 0.0


def verify_mp4(path: str, *, width: int = 1920, height: int = 1080, fps: int = 60,
               min_seconds: float = 1.0) -> tuple[bool, dict]:
    info = ffprobe(path)
    if "error" in info:
        return False, info
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        return False, {"error": "no video stream", **info}
    try:
        duration = float(info["format"].get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    rate = _parse_frame_rate(video.get("r_frame_rate"))
    try:
        frames = int(video.get("nb_frames") or 0)
    except (TypeError, ValueError):  # "N/A"
        frames = 0
    if frames <= 0:
        frames = _count_packets(path)
    recorded_fps = frames / duration if duration > 0 else 0.0
    ok = (
        video.get("width") == width
        and video.get("height") == height
        and abs(rate - fps) < 0.5
        and duration >= min_seconds
        and frames > 0
    )
    return ok, {**info, "duration": duration, "rate": rate,
                "frames": frames, "recorded_fps": round(recorded_fps, 3), "ok": ok}
