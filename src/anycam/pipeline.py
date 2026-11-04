"""The AnyCam GStreamer engine: sources -> compositor -> preview/vcam/record."""
from __future__ import annotations

import copy
from collections import deque
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import gi
import psutil

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from . import config, overlay
from .geometry import box_pixels, clamp
from .model import Layer, Project, Scene
from .engine.outputs.recorder import Recorder
from .engine.outputs.vcam import VcamOutput
from .pool import CAPTURE_KINDS, CaptureSpec, SourcePool, capture_spec
from .sources import (SourceChain, background_chain, build_chain,
                      caps_filter, caps_string, image_chain, layer_tail, make)

Gst.init(None)


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
    vcam: str = ""
    vcam_active: bool = False
    screen_error: str = ""
    rtmp_error: str = ""
    record_path: str = ""
    record_bytes: int = 0
    record_ok: bool = False
    rtmp_url: str = ""
    audio_inputs: list[str] = field(default_factory=list)
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
            "vcam": self.vcam,
            "vcam_active": self.vcam_active,
            "screen_error": self.screen_error,
            "rtmp_error": self.rtmp_error,
            "record_path": self.record_path,
            "record_bytes": self.record_bytes,
            "record_ok": self.record_ok,
            # The URL carries the stream key; reports are shared, so mask it.
            "rtmp_url": config.redact_url(self.rtmp_url),
            "audio_inputs": self.audio_inputs,
            "samples": self.samples,
        }


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
        self._last_sample_frames = 0
        self._last_sample_time = 0.0
        self._stall_seconds = 2.0
        # The crop editor shows this layer uncropped while the model keeps its
        # real crop, so the window is drawn over the picture it edits.
        self.preview_crop_layer = preview_crop_layer
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
        self._vcam_valve: Gst.Element | None = None
        self._probe_id = 0
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
        # Source size per layer, for the crop editor's picture rectangle.
        self.layer_sources: dict[str, tuple[int, int]] = {}
        self._record_sink = None
        self._appsink = None
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
        self.mic_level_db: float | None = None
        self._audio_eos_pads: list[Gst.Pad] = []

    # ------------------------------------------------------------------ build
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
        if chain.output_pad.link(pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError("could not link the AnyCam standby background")
        self._background_pad = pad
        print(
            f"[background] live AnyCam standby "
            f"{self.canvas_w}x{self.canvas_h}@{self.fps} zorder=0",
            file=sys.stderr, flush=True)

    def _add(self, *elements: Gst.Element) -> None:
        assert self.pipeline is not None
        for element in elements:
            if not self.pipeline.add(element):
                raise RuntimeError(
                    f"could not add {element.get_name()} to the canvas pipeline"
                )
            try:
                element.sync_state_with_parent()
            except Exception:  # noqa: BLE001
                pass

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

        decode.connect("pad-added", on_pad_added, target)

    def _add_layer(self, layer: Layer, zorder: int) -> None:
        assert self.compositor is not None
        box = self._box_pixels(layer)
        crop = ((0.0, 0.0, 0.0, 0.0) if layer.id == self.preview_crop_layer
                else layer.crop)
        source_layer = layer
        if layer.kind == "camera" and not layer.source:
            source_layer = copy.copy(layer)
            source_layer.source = config.camera_device()
        missing_media = ""
        if layer.kind in ("image", "logo", "face", "video") and layer.source:
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
                source_size = (spec.expected or
                               (max(2, spec.width), max(2, spec.height)))
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
        self._add(*chain.elements)
        self._link(chain.elements, skip_from=getattr(chain, "dynamic_from", None))
        self._link_dynamic(chain)

        screen_identity = [None]

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
                    clock = self.pipeline.get_clock()
                    if clock is not None:
                        buffer.pts = max(0, clock.get_time() - self.pipeline.get_base_time())
                        buffer.dts = Gst.CLOCK_TIME_NONE
                    if identity is not None:
                        if identity == screen_identity[0]:
                            return Gst.PadProbeReturn.OK
                        screen_identity[0] = identity
            self._layer_frames[layer_id] = self._layer_frames.get(layer_id, 0) + 1
            self._layer_last[layer_id] = time.monotonic()
            return Gst.PadProbeReturn.OK

        # Screen health follows PipeWire arrivals, not a downstream element
        # that may repeat cached frames at the canvas rate.
        health_pad = (chain.check_element.get_static_pad("src")
                      if layer.kind == "screen" else chain.output_pad)
        health_pad.add_probe(Gst.PadProbeType.BUFFER, on_source_buffer)
        pad = self.compositor.request_pad_simple("sink_%u")
        if pad is None:
            raise RuntimeError("compositor refused a request pad")
        pad.set_property("xpos", box[0])
        pad.set_property("ypos", box[1])
        pad.set_property("width", box[2])
        pad.set_property("height", box[3])
        pad.set_property("zorder", int(zorder))
        pad.set_property("alpha", float(clamp(layer.alpha)) if layer.visible else 0.0)
        if chain.output_pad.link(pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"could not link {layer.name} into the compositor")
        self._layer_pads[layer.id] = pad
        self._layer_chains[layer.id] = chain

        def on_first_buffer(_pad, _info, layer_id=layer.id):
            self._pads_with_data.add(layer_id)
            return Gst.PadProbeReturn.OK

        pad.add_probe(Gst.PadProbeType.BUFFER, on_first_buffer)

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

        pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)

    def _box_pixels(self, layer: Layer) -> tuple[int, int, int, int]:
        rect = box_pixels(layer, self.canvas_w, self.canvas_h)
        return (rect.x, rect.y, rect.w, rect.h)

    def build(self) -> "AnyCamEngine":
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
        self.compositor = make("compositor", "compositor", background="black", ignore_inactive_pads=False)
        self.tee = make("tee", "output")
        # A canvas-wide rate element keeps the composited timestamps monotonic
        # for h264parse/qtmux (two inter sources can start at the same PTS).
        # ``skip-to-first`` stops it emitting black buffers before the first
        # composited frame has arrived, which otherwise flash on the preview.
        rate = make("videorate", "canvas_rate", skip_to_first=True)
        canvas_caps = caps_filter(
            caps_string(self.canvas_w, self.canvas_h, self.fps), "canvas_caps")
        self._add(self.compositor, rate, canvas_caps, self.tee)
        self._link([self.compositor, rate, canvas_caps, self.tee])
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
        try:
            # zorder 0 belongs to the permanent standby; scene layers start
            # at 1 so hiding/removing them can never expose a padless graph.
            for zorder, layer in enumerate(layers, start=1):
                self._add_layer(layer, zorder)
        except Exception:
            # A half-built graph must not leave capture pipelines running.
            self.pool.stop()
            self._specs.clear()
            raise

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
                self.report.rtmp_error = f"stream output could not start: {exc}"

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
        self._probe_id = self.compositor.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._on_compositor_buffer
        )
        return self

    def _on_compositor_buffer(self, _pad, _info):
        self._frame_count += 1
        return Gst.PadProbeReturn.OK

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

    def _preview_size(self) -> tuple[int, int]:
        """A size the GUI can paint without copying the whole canvas.

        The preview widget is far smaller than the canvas, and every frame is
        copied into a QImage on the GUI thread.  The panel is ~1236 px wide at
        the default window size, so 960x540 is already panel resolution and
        costs ~14% less CPU than 1280x720 at 60 fps.  Only the preview is
        scaled down: canvas, recording and the virtual camera keep the project
        resolution.
        """
        limit_w, limit_h = 960, 540
        scale = min(1.0, limit_w / max(1, self.canvas_w),
                    limit_h / max(1, self.canvas_h))
        width = max(2, int(self.canvas_w * scale) // 2 * 2)
        height = max(2, int(self.canvas_h * scale) // 2 * 2)
        return width, height

    def _build_preview(self) -> None:
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
        sink = make("appsink", "preview_sink", emit_signals=True,
                    max_buffers=2, drop=True, sync=False)
        self._add(convert, format_caps, scale, caps, sink)
        self._link([queue, convert, format_caps, scale, caps, sink])
        # The streaming thread only stores the newest sample.  Delivery to
        # ``on_frame`` happens from pump() on the caller's thread, which keeps
        # Qt calls on the main thread and works for the headless demo too.
        sink.connect("new-sample", self._on_preview_sample)
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
        mic_name = self.project.audio.mic_device or config.microphone()
        if mic_name and self.project.audio.mic_enabled:
            src = make("pulsesrc", "mic", device=mic_name)
            conv, vol, _level = self._mixer_input(
                "mic", self.project.audio.mic_gain_db,
                self.project.audio.mic_muted, meter=True)
            self._add(src)
            self._link([src, conv])
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

            decode.connect("pad-added", on_pad, conv)

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
            scale = make("videoscale", "vcam_scale")
            rate = make("videorate", "vcam_rate")
            caps = caps_filter(caps_text, "vcam_caps")
            appsink = make("appsink", "vcam_appsink", emit_signals=True,
                           max_buffers=2, drop=True, sync=False)
            self._add(valve, scale, rate, caps, appsink)
            self._link([queue, valve, scale, rate, caps, appsink])
            appsink.connect("new-sample", self._on_vcam_sample)
            self._notify_canvas_attached(True)
            self.report.vcam_active = True
            return

        if not config.vcam_available(device):
            self.report.vcam_error = (
                f"virtual camera {device} is not available; load v4l2loopback with "
                f"'sudo modprobe v4l2loopback video_nr=10 card_label=AnyCam'"
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
        appsink.connect("new-sample", self._on_vcam_sample)

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
            # A recovered output may still be frozen from an empty scene.
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

    def _make_record_encoder(self) -> Gst.Element:
        """Shared H.264 encoder selection for the recording branches."""
        encoder = os.environ.get("ANYCAM_ENCODER", "auto").lower()
        if encoder in ("nv", "nvh264", "nvenc") and Gst.ElementFactory.find("nvh264enc"):
            enc = make("nvh264enc", "record_encoder", bitrate=12000,
                       zerolatency=True, preset="p4")
            self.report.encoder = "nvh264enc"
        else:
            enc = make("x264enc", "record_encoder", speed_preset="veryfast",
                       tune="zerolatency", bitrate=12000, key_int_max=60)
            self.report.encoder = "x264enc"
        return enc

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
        convert = make("videoconvert")
        i420 = caps_filter("video/x-raw,format=I420", "record_raw")
        enc = self._make_record_encoder()
        parse = make("h264parse")
        sink = make("appsink", "record_tap_sink", emit_signals=True,
                    max_buffers=4, drop=True, sync=False)
        self._add(valve, convert, i420, enc, parse, sink)
        self._link([queue, valve, convert, i420, enc, parse, sink])
        sink.connect("new-sample", self._on_record_sample)

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
            audio_sink.connect("new-sample", self._on_record_audio_sample)

    def attach_recorder(self, recorder: Recorder) -> bool:
        """Start feeding the existing canvas into ``recorder`` without a rebuild."""
        self.recorder = recorder
        self.record_path = str(recorder.path)
        if self._recorder_valve is None:
            self._build_recorder_tap()
        else:
            self._recorder_valve.set_property("drop", False)
        if self._recorder_valve is not None:
            return True
        self.recorder = None
        return False

    def detach_recorder(self) -> None:
        """Stop feeding encoded samples to the recorder, leaving the file open."""
        if self._recorder_valve is not None:
            self._recorder_valve.set_property("drop", True)
        self.recorder = None

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
        convert = make("videoconvert")
        i420 = caps_filter("video/x-raw,format=I420", "record_raw")
        enc = self._make_record_encoder()
        parse = make("h264parse")
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
        self._add(convert, i420, enc, parse, buffer, mux, sink)
        self._link([queue, convert, i420, enc, parse, buffer])
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
        missing = [name for name in (sink_factory, "flvmux", "x264enc", "h264parse")
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
        convert = make("videoconvert")
        i420 = caps_filter("video/x-raw,format=I420", "rtmp_raw")
        enc = make("x264enc", "rtmp_encoder", speed_preset="veryfast",
                   tune="zerolatency", bitrate=6000, key_int_max=60)
        parse = make("h264parse", "rtmp_parse", config_interval=-1)
        h264 = caps_filter("video/x-h264,stream-format=avc,alignment=au",
                           "rtmp_h264")
        video_sink = make("appsink", "rtmp_video_sink", emit_signals=True,
                          max_buffers=1, drop=True, sync=False)
        self._add(convert, i420, enc, parse, h264, video_sink)
        self._link([queue, convert, i420, enc, parse, h264, video_sink])
        video_sink.connect("new-sample", self._on_rtmp_video_sample)

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
            audio_sink.connect("new-sample", self._on_rtmp_audio_sample)

        output = Gst.Pipeline.new("anycam-rtmp")
        video_src = make("appsrc", "rtmp_video_src", format=Gst.Format.TIME,
                         is_live=True)
        video_src.set_property(
            "caps", Gst.Caps.from_string("video/x-h264,stream-format=avc,alignment=au"))
        video_src.set_property("max-buffers", 2)
        video_src.set_property("block", False)
        self._set_leaky(video_src)
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
        self.report.rtmp_url = url

    def _on_rtmp_video_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is not None and self._rtmp_appsrc is not None and not self._rtmp_failed:
            self._rtmp_appsrc.emit("push-sample", sample)
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
            self.report.rtmp_error = f"{err.message} ({debug})"
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

    def source_sizes(self) -> dict[str, tuple[int, int]]:
        """Source size per layer id (crop editor picture geometry)."""
        return dict(self.layer_sources)

    def set_layer_visible(self, layer_id: str, visible: bool) -> None:
        """Show/hide an already-built layer without rebuilding the graph."""
        layer = next((l for l in self.scene.layers if l.id == layer_id), None)
        pad = self._layer_pads.get(layer_id)
        if layer is None or pad is None:
            return
        layer.visible = bool(visible)
        pad.set_property("alpha", float(clamp(layer.alpha)) if visible else 0.0)
        spec = self._layer_specs.get(layer_id)
        if spec is not None:
            if visible and layer_id not in self._layer_acquired:
                entry = self.pool.acquire(spec)
                self._record_camera_mode(layer_id, entry)
                self._specs.append(spec)
                self._layer_acquired.add(layer_id)
            elif not visible and layer_id in self._layer_acquired:
                self.pool.release(spec)
                if spec in self._specs:
                    self._specs.remove(spec)
                self._layer_acquired.discard(layer_id)
        # The permanent standby keeps the canvas alive when every scene layer
        # is hidden, so the vcam must never be left frozen on an old frame.
        self.resume_vcam()

    def freeze_vcam(self) -> None:
        """Hold the last canvas frame on the vcam while the scene is empty."""
        output = self.vcam_output
        if output is None:
            return
        freezer = getattr(output, "freeze_last_frame", None)
        if freezer is not None:
            freezer()
        if self._vcam_valve is not None:
            self._vcam_valve.set_property("drop", True)

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
        self.set_layer_visible(layer_id, False)
        spec = self._layer_specs.pop(layer_id, None)
        if spec is not None and spec in self._specs:
            self._specs.remove(spec)
        self._layer_acquired.discard(layer_id)
        pad = self._layer_pads.pop(layer_id, None)
        if pad is not None:
            # Keep the pad linked but invisible.  Releasing a requested pad
            # while the compositor is PLAYING can make that branch post
            # not-linked; the next explicit rebuild drops it for real.
            pad.set_property("alpha", 0.0)
        self._layer_chains.pop(layer_id, None)
        self._layer_frames.pop(layer_id, None)
        self._layer_last.pop(layer_id, None)
        self._pads_with_data.discard(layer_id)
        self.layer_sources.pop(layer_id, None)
        self._camera_modes.pop(layer_id, None)

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

    def add_layer(self, layer: Layer, *, screen_fd: int = -1,
                  screen_size: tuple[int, int] | None = None) -> bool:
        """Attach a new layer to the running canvas instead of rebuilding.

        Returns False when the graph is not running (the caller then builds
        one), or when attaching failed.  A previously removed screen layer is
        reattached to its still-PLAYING pipewiresrc and compositor pad, so no
        portal picker appears and the virtual camera never stops.
        """
        if self.pipeline is None or self.compositor is None:
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
                check = getattr(chain, "check_element", None)
                health_pad = (check.get_static_pad("src")
                              if check is not None else None)
                if health_pad is not None:
                    def on_buffer(_pad, _info, layer_id=layer.id):
                        self._layer_frames[layer_id] = (
                            self._layer_frames.get(layer_id, 0) + 1)
                        self._layer_last[layer_id] = time.monotonic()
                        return Gst.PadProbeReturn.OK

                    health_pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
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
            if self._vcam_pipeline is not None:
                self._vcam_pipeline.set_state(Gst.State.NULL)
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
                self.report.error = f"{err.message} ({debug})"
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
        self._poll_rtmp_bus()
        self.pool.poll_errors()
        errors = self.pool.errors()
        if errors:
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
                continue
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
                if now - self._start_time > self._stall_seconds:
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
        # The tap died with this canvas.  Release the held device back to its
        # idle card so the next engine can attach, instead of leaving the
        # output thinking it still owns the feed.
        if self.vcam_output is not None:
            setter = getattr(self.vcam_output, "set_canvas_attached", None)
            if setter is not None:
                setter(False)
        for spec in self._specs:
            self.pool.release(spec)
        self._specs.clear()
        if self._owns_pool:
            self.pool.stop()
        if self._vcam_pipeline is not None:
            self._vcam_pipeline.send_event(Gst.Event.new_eos())
            self._vcam_pipeline.set_state(Gst.State.NULL)
            self._vcam_bus = None
        self._teardown_rtmp()
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
        self.start()
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


def verify_mp4(path: str, *, width: int = 1920, height: int = 1080, fps: int = 60,
               min_seconds: float = 1.0) -> tuple[bool, dict]:
    info = ffprobe(path)
    if "error" in info:
        return False, info
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        return False, {"error": "no video stream", **info}
    duration = float(info["format"].get("duration") or 0)
    num, den = (video.get("r_frame_rate") or "0/1").split("/")
    rate = float(num) / float(den or 1)
    frames = int(video.get("nb_frames") or 0)
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
