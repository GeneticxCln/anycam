"""Session-owned virtual camera.

The canvas rebuilds for scene/layer edits.  Keeping ``v4l2sink`` in a separate
pipeline means a consumer reading the loopback device does not see the sink
stop and start with every rebuild.  The engine only owns a small appsink tap
into this pipeline.

A loopback node only starts streaming once its writer has pushed a buffer, and
a node with no active stream fails a consumer's open with ``EIO``.  AnyCam
therefore opens this pipeline at launch and repeats a static "ready" card until
the canvas feeds real frames (and again whenever it stops feeding them).
"""
from __future__ import annotations

import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from ... import config, overlay  # noqa: E402

Gst.init(None)

# The card only has to prove the device is alive.  A low rate keeps a held-open
# node nearly free while no application is watching it.
IDLE_FPS = 10
# A canvas sample this recent means the tap is live and the card is not needed.
LIVE_GRACE_S = 0.25
# A canvas rebuild releases the tap for a few seconds while sources are probed
# and the new graph is built.  Keep the last real frame on the device through
# that gap instead of dropping straight back to the near-black ready card.
CARD_GRACE_S = 6.0
# GPU colour conversion, when the build ships it and the first frame proves it.
GL_ELEMENTS = ("glupload", "glcolorconvert", "gldownload")
# A broken GL context can still accept PLAYING, so the probe waits for one
# buffer to reach the sink before the GPU path is trusted.
GL_PROBE_S = 2.0


def _make(factory: str, name: str, **props) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer element '{factory}' is not installed")
    for key, value in props.items():
        element.set_property(key.replace("_", "-"), value)
    return element


def idle_frame(width: int, height: int) -> bytes:
    """Render the "virtual camera ready" card as one raw RGBA frame.

    RGBA matches the canvas format, so the card travels the same single
    conversion as live frames instead of needing its own YUY2/NV12 encoder.
    Rendered once per session: the idle feed repeats these bytes, so this is
    not on the per-frame path.
    """
    from PIL import Image

    width = max(2, int(width) // 2 * 2)
    height = max(2, int(height) // 2 * 2)
    path = overlay.render_camera_placeholder(
        "Start the preview", "", width, height, title="Virtual camera ready")
    with Image.open(path) as card:
        return card.convert("RGBA").tobytes()


def _measure_chain(use_gl: bool, width: int, height: int, fps: int,
                   pixel_format: str, frames: int) -> float | None:
    """Milliseconds of pipeline time per frame for one conversion chain."""
    if use_gl and not all(Gst.ElementFactory.find(name) is not None
                          for name in GL_ELEMENTS):
        return None
    source_caps = (f"video/x-raw,format=RGBA,width={width},height={height},"
                   f"framerate={fps}/1")
    sink_caps = (f"video/x-raw,format={pixel_format},width={width},"
                 f"height={height},framerate={fps}/1,colorimetry=2:4:7:1")
    convert = ("glupload ! glcolorconvert ! gldownload" if use_gl
               else "videoconvert")
    # block=true (not a leaky bound): a fast push loop must wait for the
    # converter rather than drop frames, so every pushed frame is timed.
    description = (f'appsrc name=diag_appsrc caps="{source_caps}" format=time '
                   f'max-buffers=8 block=true leaky-type=0 ! {convert} ! '
                   f'capsfilter caps="{sink_caps}" ! '
                   'fakesink name=diag_sink sync=false async=false')
    try:
        pipeline = Gst.parse_launch(description)
    except Exception:  # noqa: BLE001 - reported as "path unavailable"
        return None
    appsrc = pipeline.get_by_name("diag_appsrc")
    sink = pipeline.get_by_name("diag_sink")
    if appsrc is None or sink is None:
        return None
    seen = [0]

    def on_buffer(_pad, _info):
        seen[0] += 1
        return Gst.PadProbeReturn.OK

    pad = sink.get_static_pad("sink")
    probe = pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
    data = bytes(max(4, width * height * 4))

    def push(start: int, count: int) -> None:
        for offset in range(count):
            index = start + offset
            buffer = Gst.Buffer.new_wrapped(data)
            buffer.pts = index * Gst.SECOND // fps
            buffer.duration = int(Gst.SECOND / fps)
            appsrc.emit("push-buffer", buffer)

    def wait_for(target: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if seen[0] >= target:
                return True
            if pipeline.get_bus().pop_filtered(
                    Gst.MessageType.ERROR) is not None:
                return False
            time.sleep(0.005)
        return False

    warmup = 4
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            return None
        # Buffers pushed during the async start can be dropped by the appsrc's
        # leaky bound, so wait until the pipeline reports PLAYING.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            _result, state, _pending = pipeline.get_state(0)
            if state == Gst.State.PLAYING:
                break
            if pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR) is not None:
                return None
            time.sleep(0.005)
        else:
            return None
        push(0, warmup)
        if not wait_for(1, 3.0):
            return None
        start = seen[0]
        started = time.monotonic()
        push(warmup, frames)
        target = start + frames
        deadline = started + 3.0
        while time.monotonic() < deadline and seen[0] < target:
            if pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR) is not None:
                return None
            time.sleep(0.005)
        elapsed = time.monotonic() - started
        if seen[0] - start < frames:
            return None
    except Exception:  # noqa: BLE001 - a failed probe is "unavailable"
        return None
    finally:
        pad.remove_probe(probe)
        pipeline.set_state(Gst.State.NULL)
    processed = min(frames, seen[0] - start)
    if processed <= 0:
        return None
    # Milliseconds of pipeline time per frame: the real conversion cost,
    # whether the work lands on the CPU or the GPU.
    return elapsed / processed * 1000


def measure_conversion(width: int = 1920, height: int = 1080, fps: int = 60,
                       pixel_format: str = "YUY2", frames: int = 24) -> dict:
    """Measure both conversion chains on synthetic frames.

    No camera and no loopback device are opened: this is the same
    appsrc -> converter -> capsfilter work the virtual camera does.  The cost
    is milliseconds of pipeline time per frame, which is meaningful for both
    the CPU and the GPU (an element's workload does not stay on one thread and
    may not be CPU time at all).  ``chosen`` follows the start policy: the GPU
    is used whenever it can deliver a frame.
    """
    width = max(2, int(width) // 2 * 2)
    height = max(2, int(height) // 2 * 2)
    fps = max(1, int(fps))
    frames = max(1, int(frames))
    gpu = _measure_chain(True, width, height, fps, pixel_format, frames)
    cpu = _measure_chain(False, width, height, fps, pixel_format, frames)
    return {"gpu": gpu, "cpu": cpu,
            "chosen": "gpu" if gpu is not None else "cpu",
            "width": width, "height": height, "fps": fps,
            "frames": frames}


class VcamOutput:
    """Own the ``v4l2sink`` pipeline for the window's lifetime."""

    def __init__(self) -> None:
        self.device = ""
        self.pixel_format = "YUY2"
        self.width = 0
        self.height = 0
        self.fps = 30
        self.error = ""
        # True when ``error`` came from a failed push (which can clear itself
        # on the next good frame), False for a sink error (the v4l2sink
        # pipeline is dead and only a rebuild brings it back).
        self._error_from_push = False
        # "gpu" or "cpu": which conversion chain the running pipeline uses.
        self.conversion = ""
        self._pipeline: Gst.Pipeline | None = None
        self._appsrc: Gst.Element | None = None
        self._bus: Gst.Bus | None = None
        self._sink: Gst.Element | None = None
        self._idle_bytes = b""
        self._idle_thread: threading.Thread | None = None
        self._idle_stop = threading.Event()
        self._last_push = 0.0
        # Last canvas buffer.  A rebuild releases the tap for seconds; keep
        # this frame flowing so consumers do not sit on a black preview.
        self._last_buffer: Gst.Buffer | None = None
        self._epoch = 0.0
        self._live_logged = False
        self._quiet_logged = False
        # True while an engine's tap is attached.  The canvas then owns the
        # feed even during a pause, so the ready card cannot overwrite it.
        self._canvas_live = False
        # A scene with no sources still makes the compositor emit its black
        # background.  While frozen, canvas samples are ignored and the last
        # real frame is repeated until a source attaches again.
        self._frozen = False
        # Last timestamp seen from the canvas.  A canvas rebuild restarts PTS
        # at zero; the persistent vcam pipeline must not treat that as a late
        # buffer and stop on the last black frame.
        self._last_canvas_pts: int | None = None
        # When the canvas detached.  The card waits CARD_GRACE_S after this,
        # so a rebuild does not flash the ready card at a consumer.
        self._detached_at = 0.0

    @property
    def active(self) -> bool:
        return self._pipeline is not None

    def start(self, device: str, width: int, height: int, fps: int,
              setup: bool = False) -> bool:
        if self.active:
            self.stop()
        # One fixed camera mode, like OBS's v4l2sink output.  The canvas may
        # resize or switch layouts while this node is open; the tap scales
        # into this mode rather than reopening (and killing) the device.
        width, height = config.vcam_output_size(width, height)
        self.device = device
        self.error = ""
        self._error_from_push = False
        if setup:
            # The user just asked for the virtual camera: bring the loopback
            # device up (pkexec/polkit prompt) instead of telling them to run
            # modprobe in a terminal.
            problem = config.ensure_vcam_device(device)
            if problem:
                self.error = problem
                return False
        if not config.vcam_available(device):
            self.error = (
                f"virtual camera {device} is not available; "
                f"load v4l2loopback with 'sudo modprobe v4l2loopback "
                f"video_nr=10 card_label=AnyCam'"
            )
            return False
        self.pixel_format = config.vcam_pixel_format(device)
        self.width, self.height, self.fps = int(width), int(height), int(fps)
        # The canvas hands us RGBA, so convert exactly once -- here -- into the
        # format the driver advertises.  Converting in the canvas tap as well
        # was a second full-frame pass at 1080p60 (measured ~34% of a core).
        self.source_caps = (f"video/x-raw,format=RGBA,width={self.width},"
                            f"height={self.height},framerate={self.fps}/1")
        # v4l2loopback advertises SRGB (full-range BT.709) and refuses a YUY2
        # stream with a different colorimetry.  A bare YUY2 buffer makes
        # videoconvert emit BT.601 (2:3:7:1), so the very first buffer -- the
        # idle card -- killed the sink with "does not support 2:3:7:1".  Pin
        # what the driver asks for.
        sink_caps = (f"video/x-raw,format={self.pixel_format},"
                     f"width={self.width},height={self.height},"
                     f"framerate={self.fps}/1,colorimetry=2:4:7:1")
        self.sink_caps = sink_caps
        try:
            self._idle_bytes = idle_frame(self.width, self.height)
        except Exception as exc:  # noqa: BLE001 - a card must not break the device
            print(f"[vcam] idle card unavailable: {exc}", flush=True)
            self._idle_bytes = b""
        # Convert on the GPU when it is available: measured at 1080p60 the GL
        # chain costs ~21% of a core against ~31% for videoconvert.  A GPU can
        # accept the pipeline and still fail on the first frame (no context,
        # unsupported format), so the GL attempt must prove a frame reaches the
        # sink; otherwise the CPU chain is built instead of leaving the camera
        # broken.  The sink needs a buffer before it streams, and the card is
        # what keeps a consumer's open from failing with EIO.
        if self._gl_available() and self._build_pipeline(use_gl=True):
            self._start_idle_feed()
            return True
        if self._build_pipeline(use_gl=False):
            self._start_idle_feed()
            return True
        return False

    @staticmethod
    def _gl_available() -> bool:
        """True when this GStreamer build ships the GL conversion trio."""
        return all(Gst.ElementFactory.find(name) is not None
                   for name in GL_ELEMENTS)

    def _build_pipeline(self, use_gl: bool) -> bool:
        """Build and start one conversion chain, GL or CPU."""
        pipeline = Gst.Pipeline.new("anycam-vcam")
        try:
            appsrc = _make("appsrc", "vcam_appsrc")
            appsrc.set_property("caps", Gst.Caps.from_string(self.source_caps))
            appsrc.set_property("format", Gst.Format.TIME)
            appsrc.set_property("is-live", True)
            appsrc.set_property("max-buffers", 2)
            appsrc.set_property("block", False)
            appsrc.set_property("leaky-type", 2)
            elements: list[Gst.Element] = [appsrc]
            if use_gl:
                elements.extend([_make("glupload", "vcam_gl_upload"),
                                 _make("glcolorconvert", "vcam_gl_convert"),
                                 _make("gldownload", "vcam_gl_download")])
            else:
                elements.append(_make("videoconvert", "vcam_convert"))
            fixed = _make("capsfilter", "vcam_caps")
            fixed.set_property("caps", Gst.Caps.from_string(self.sink_caps))
            sink = _make("v4l2sink", "vcam_sink", device=self.device,
                         sync=False)
            elements.extend([fixed, sink])
            for element in elements:
                pipeline.add(element)
            for first, second in zip(elements, elements[1:]):
                if not first.link(second):
                    raise RuntimeError(
                        f"could not link {first.get_name()} into "
                        f"{second.get_name()}")
        except Exception as exc:  # noqa: BLE001
            pipeline.set_state(Gst.State.NULL)
            self.error = f"virtual camera pipeline failed: {exc}"
            return False
        self._pipeline = pipeline
        self._appsrc = appsrc
        self._bus = pipeline.get_bus()
        self._sink = sink
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.error = "virtual camera pipeline failed to start"
            self._teardown_pipeline()
            return False
        if not use_gl:
            self.conversion = "cpu"
            return True
        if self._first_frame_flows():
            self.conversion = "gpu"
            return True
        print("[vcam] GPU conversion unavailable; using CPU", file=sys.stderr,
              flush=True)
        self._teardown_pipeline()
        self.error = ""
        return False

    def _first_frame_flows(self) -> bool:
        """Push one card frame and verify the GPU chain reaches the sink."""
        reached = threading.Event()

        def on_buffer(_pad, _info):
            reached.set()
            return Gst.PadProbeReturn.OK

        appsrc, sink = self._appsrc, self._sink
        if appsrc is None or sink is None:
            return False
        pad = sink.get_static_pad("sink")
        probe = pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
        data = self._idle_bytes or bytes(self.width * self.height * 4)
        buffer = Gst.Buffer.new_wrapped(data)
        buffer.pts = 0
        buffer.duration = int(Gst.SECOND / max(1, self.fps))
        try:
            appsrc.emit("push-buffer", buffer)
        except Exception:  # noqa: BLE001 - treated as a failed probe
            pass
        deadline = time.monotonic() + GL_PROBE_S
        flowed = False
        while time.monotonic() < deadline:
            if reached.wait(0.02):
                flowed = True
                break
            if (self._bus is not None
                    and self._bus.pop_filtered(Gst.MessageType.ERROR) is not None):
                break
        if flowed:
            # A sink pad accepts a buffer before the driver write happens; give
            # the sink a moment to reject the frame, then confirm it did not.
            time.sleep(0.15)
            flowed = (self._bus is None
                      or self._bus.pop_filtered(Gst.MessageType.ERROR) is None)
        pad.remove_probe(probe)
        return flowed

    def _teardown_pipeline(self) -> None:
        pipeline = self._pipeline
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        self._clear()

    def _card_allowed(self) -> bool:
        """Whether the ready card may push right now."""
        if self._canvas_live:
            return False
        # A rebuild releases the tap, probes the camera and rebuilds the
        # canvas.  Hold the last canvas frame through that gap: the card is
        # near-black, and a consumer shows it as a black camera.
        if (self._detached_at
                and time.monotonic() - self._detached_at < CARD_GRACE_S):
            return False
        return time.monotonic() - self._last_push >= LIVE_GRACE_S

    def set_canvas_attached(self, attached: bool) -> None:
        """Give the feed to the canvas, or take it back for the ready card.

        A screencast is damage-driven and can go seconds without a buffer.
        Treating that gap as "no canvas" overwrote the last real picture with
        the dark ready card, which a consumer shows as a black camera.  A
        detached canvas also keeps its last frame for CARD_GRACE_S so a
        rebuild does not flash the card while sources are probed.
        """
        attached = bool(attached)
        if attached:
            self._detached_at = 0.0
            # A new canvas (or a re-attached one) owns the feed again.
            self._frozen = False
        elif self._canvas_live:
            self._detached_at = time.monotonic()
        self._canvas_live = attached

    def freeze_last_frame(self) -> None:
        """Hold the last canvas frame while the scene has no sources.

        Removing or hiding the last visible layer leaves the compositor with
        only its black background, which it keeps emitting at the canvas rate.
        Forwarding that turns a source edit into a black camera for every
        consumer.  Freeze on the last real frame instead; the idle loop already
        repeats that buffer at the idle rate.
        """
        if self._last_buffer is None or self._frozen:
            return
        self._frozen = True
        # The idle loop only repeats a quiet canvas after LIVE_GRACE_S.  Make
        # the hold take effect on its next tick instead of a silent gap.
        self._last_push = 0.0
        print("[vcam] canvas empty; holding the last canvas frame",
              file=sys.stderr, flush=True)

    def resume_canvas(self) -> None:
        """Accept live canvas samples again after a source is shown."""
        self._frozen = False

    def _held_canvas_buffer(self) -> Gst.Buffer | None:
        """The last canvas frame, repeated while a rebuild is underway.

        A rebuild detaches the tap for seconds.  v4l2loopback consumers only
        advance when the writer pushes a buffer, so a silent gap can leave an
        application (Discord) on a black preview even after the canvas comes
        back.  Repeat the last real frame instead of going quiet or showing
        the ready card during that window.
        """
        if self._last_buffer is None or not self._detached_at:
            return None
        if time.monotonic() - self._detached_at >= CARD_GRACE_S:
            return None
        return self._copy_last_buffer()

    def _copy_last_buffer(self) -> Gst.Buffer | None:
        """A fresh-timestamped copy of the last real canvas frame."""
        if self._last_buffer is None:
            return None
        buffer = self._last_buffer.copy()
        buffer.pts = int((time.monotonic() - self._epoch) * Gst.SECOND)
        buffer.duration = int(Gst.SECOND / max(1, self.fps))
        return buffer

    def _quiet_canvas_buffer(self) -> Gst.Buffer | None:
        """Keep a consumer fed when the attached canvas has gone quiet.

        A damage-driven screencast can stop producing frames after a layout
        change even though the canvas and portal session are alive.  Silence
        on v4l2loopback is shown as a black camera, so repeat the last real
        frame at the idle rate until the canvas produces a new one.
        """
        if not self._canvas_live or self._last_buffer is None:
            return None
        if time.monotonic() - self._last_push < LIVE_GRACE_S:
            return None
        return self._copy_last_buffer()

    def _start_idle_feed(self) -> None:
        self._epoch = time.monotonic()
        self._last_push = 0.0
        self._idle_stop.clear()
        self._idle_thread = threading.Thread(
            target=self._idle_loop, name="anycam-vcam-idle", daemon=True)
        self._idle_thread.start()

    def _stop_idle_feed(self) -> None:
        self._idle_stop.set()
        thread, self._idle_thread = self._idle_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self._idle_bytes = b""

    def _idle_loop(self) -> None:
        interval = 1.0 / IDLE_FPS
        while not self._idle_stop.wait(interval):
            appsrc = self._appsrc
            if appsrc is None:
                return
            if self._canvas_live:
                # The canvas owns the device.  If it is temporarily quiet (a
                # static/damage-driven screencast after a pad change), repeat
                # its last real frame instead of leaving Discord on black.
                buffer = self._quiet_canvas_buffer()
                if buffer is None:
                    continue
                if not self._quiet_logged:
                    self._quiet_logged = True
                    print("[vcam] canvas quiet; repeating last canvas frame",
                          file=sys.stderr, flush=True)
            else:
                buffer = self._held_canvas_buffer()
                if buffer is None:
                    if not self._idle_bytes:
                        return
                    buffer = Gst.Buffer.new_wrapped(self._idle_bytes)
                    buffer.pts = int((time.monotonic() - self._epoch) * Gst.SECOND)
                    buffer.duration = int(Gst.SECOND / max(1, self.fps))
            try:
                appsrc.emit("push-buffer", buffer)
            except Exception:  # noqa: BLE001 - the pipeline is going away
                return

    def _push_failed(self, message: str) -> None:
        """Record a push failure; log it once, not once per frame."""
        if message != self.error:
            print(f"[vcam] {message}", file=sys.stderr, flush=True)
        self.error = message
        self._error_from_push = True

    def push_sample(self, sample) -> None:
        if self._appsrc is None:
            return
        if self.error and not self._error_from_push:
            # A dead sink cannot accept frames; the owner rebuilds the output.
            return
        if self._frozen:
            # The canvas is empty; a black compositor frame must not replace
            # the last real picture being repeated to consumers.
            return
        source_buffer = sample.get_buffer()
        out_buffer = None
        if source_buffer is not None:
            previous = self._last_canvas_pts
            if (previous is not None
                    and source_buffer.pts != Gst.CLOCK_TIME_NONE
                    and source_buffer.pts < previous):
                print(
                    "[vcam] canvas timestamps reset; retimestamping on the "
                    "vcam clock", file=sys.stderr, flush=True)
            self._last_canvas_pts = source_buffer.pts
            # The vcam pipeline outlives canvas rebuilds, which restart the
            # canvas PTS at zero.  A live appsrc treats those as late buffers
            # and can leave the device on the last black frame.  Copy only the
            # metadata and stamp on the vcam's own continuous timeline.
            out_buffer = source_buffer.copy()
            out_buffer.pts = int((time.monotonic() - self._epoch) * Gst.SECOND)
            out_buffer.dts = Gst.CLOCK_TIME_NONE
            out_buffer.duration = int(Gst.SECOND / max(1, self.fps))
            method, payload = "push-buffer", out_buffer
        else:
            method, payload = "push-sample", sample
        try:
            result = self._appsrc.emit(method, payload)
        except Exception as exc:  # noqa: BLE001
            self._push_failed(f"virtual camera push failed: {exc}")
            return
        if result == Gst.FlowReturn.FLUSHING:
            # The pipeline is shutting down or restarting; not a fault.
            return
        if result != Gst.FlowReturn.OK:
            # The appsrc caps are fixed at start; a canvas that negotiated a
            # different size is rejected here.  Silently ignoring this left
            # the idle card frozen on screen while the preview looked fine.
            caps = sample.get_caps()
            message = ("virtual camera rejected the canvas frame ("
                       f"{result.value_nick})")
            if message != self.error:
                print(
                    f"[vcam] {message}; frame="
                    f"{caps.to_string() if caps else 'no caps'}; appsrc="
                    f"{self._appsrc.get_property('caps').to_string()}",
                    file=sys.stderr, flush=True)
            self._push_failed(message)
            return
        if self._error_from_push:
            # A good frame after a failed push: the output works again.
            print("[vcam] canvas frames accepted again", file=sys.stderr,
                  flush=True)
            self.error = ""
            self._error_from_push = False
        if not self._live_logged:
            self._live_logged = True
            caps = sample.get_caps()
            print(f"[vcam] first live canvas frame "
                  f"{caps.to_string() if caps else ''}",
                  file=sys.stderr, flush=True)
        self._last_buffer = out_buffer or self._last_buffer
        self._last_push = time.monotonic()
        self._quiet_logged = False

    def poll(self) -> None:
        if self._bus is None:
            return
        while True:
            msg = self._bus.pop_filtered(Gst.MessageType.ERROR)
            if msg is None:
                return
            err, debug = msg.parse_error()
            error = f"{err.message} ({debug})"
            if error != self.error:
                # Log it: a latched sink error used to be invisible until the
                # camera silently stayed on the ready card.
                print(f"[vcam] sink error: {error}", file=sys.stderr, flush=True)
            # The sink itself failed: frames cannot recover without a rebuild.
            self.error = error
            self._error_from_push = False

    def stop(self) -> dict:
        pipeline = self._pipeline
        if pipeline is None:
            return {"device": self.device, "ok": False,
                    "error": self.error or "virtual camera is not running"}
        self._stop_idle_feed()
        try:
            if self._appsrc is not None:
                self._appsrc.emit("end-of-stream")
        except Exception:
            pass
        pipeline.set_state(Gst.State.NULL)
        result = {"device": self.device, "ok": not self.error,
                  "error": self.error}
        self._clear()
        return result

    def _clear(self) -> None:
        self._canvas_live = False
        self._frozen = False
        self._last_canvas_pts = None
        self._detached_at = 0.0
        self._last_buffer = None
        self.conversion = ""
        self._pipeline = None
        self._appsrc = None
        self._bus = None
        self._sink = None
