"""The engine controller: build, start, pump, stop."""
from __future__ import annotations

import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable

import gi
import psutil

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from .. import config  # noqa: E402
from ..model import Project, Scene  # noqa: E402
from ..engine.outputs.recorder import Recorder  # noqa: E402
from ..engine.outputs.vcam import VcamOutput  # noqa: E402
from ..engine.source_pool import CaptureSpec, SourcePool  # noqa: E402
from ..sources import (SourceChain, caps_filter, caps_string, make)
from ..engine.encoders import (ENCODER_FACTORIES, ENCODER_PARSERS, ENCODER_BITRATES, ENCODER_LABELS, _NVENC_TTL_S, _nvenc_seen, encoder_bitrate, nvenc_available, _probe_nvenc, _va_encodes, va_factory, va_present, EncoderPlan, _requested_codec, _wants_va, encoder_plan, recording_codec, EncodedFeed, force_keyframe)  # noqa: E402,F401
from ..engine.report import (EngineReport)  # noqa: E402,F401
from ..engine.audio_helpers import (MIC_DENOISE_LEVELS, mic_dsp_wanted, describe_mic_dsp, mic_dsp_elements)  # noqa: E402,F401
from ..engine.canvas_helpers import (GL_MIXER_LATENCY_NS, canvas_requested, gl_canvas_problem, _empty_drawing)  # noqa: E402,F401

Gst.init(None)
from .audio import AudioMixin  # noqa: E402
from .canvas import CanvasMixin  # noqa: E402
from .health import HealthMixin  # noqa: E402
from .layers import LayerMixin  # noqa: E402
from .outputs.record_tap import RecordTapMixin  # noqa: E402
from .outputs.stream import StreamMixin  # noqa: E402
from .outputs.taps import VcamTapMixin  # noqa: E402
from .transitions import TransitionMixin  # noqa: E402


class AnyCamEngine(LayerMixin, TransitionMixin, CanvasMixin, AudioMixin, VcamTapMixin, RecordTapMixin,
                   StreamMixin, HealthMixin):
    """Build and run one scene as a GStreamer compositor graph.

The engine is intentionally UI-free.  The GUI supplies a frame callback and
drives ``pump`` from a timer; the CLI drives it from a small loop."""

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
        self._ghosts: list = []        # the outgoing scene's layers during a transition
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
        self.poll_transition()
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

    def stop(self) -> dict:
        # A second stop used to re-run the whole shutdown and overwrite the
        # report (0 frames, 100% dropped).  Stopping is final.
        if self.pipeline is None or self._stopped:
            return self.report.as_dict()
        run = self._transition
        if run is not None:
            run.stop.set()                 # no pad is touched once teardown starts
            self._transition = None
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
