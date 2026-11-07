"""Ref-counted capture pipelines for the M1 source split.

Every capture source owns a pipeline of its own and publishes into the canvas
through ``intervideosink``/``intervideosrc``.  Two layers that watch the same
device share one capture, and the capture is torn down when the last layer
releases it, so a hidden or deleted layer gives the hardware back.

The canvas only ever sees a normalised RGBA frame at the source's native size;
per-layer crop, effects and letterboxing stay in the canvas (``sources.layer_tail``).
That is what makes the capture shareable in the first place.

Important: ``intervideosrc`` negotiates a 30/1 source pad by default and
silently drops every second frame of a 60 fps producer.  ``layer_tail`` pins the
canvas rate for exactly this reason.
"""
from __future__ import annotations

import itertools
import sys
import time
from dataclasses import dataclass, field

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from . import config
from .phone import PHONE_SIZE, phone_channel
from .sources import (
    TEST_PATTERNS,
    _gst_raw_format,
    _loopback_mode,
    camera_capture_request,
    camera_modes,
    camera_probe_error,
    caps_filter,
    choose_from_modes,
    jpeg_decoder,
    make,
    make_optional,
    video_size,
)

CAPTURE_KINDS = ("camera", "test", "video", "network", "screen", "phone")
# A canvas rebuild releases every capture and immediately acquires it again.
# Re-creating a screencast's pipewiresrc on a static window gives the rebuilt
# canvas no frames until the window repaints, which is a black screen.  Keep a
# released screen capture alive for a moment so the rebuild reuses the running
# source (and the frame it already has).
SCREEN_IDLE_GRACE_S = 5.0
# A removed camera/video/network layer keeps its capture for a while so a quick
# undo or re-add is a relink.  After this the device is given back; holding a
# webcam open (LED on, busy for other apps) for the whole session was a leak.
# Portal screen captures are exempt: a new one means a new picker prompt.
RETAIN_GRACE_S = 30.0


@dataclass(frozen=True)
class CaptureSpec:
    """What to capture, before any per-layer crop/effect/box is applied."""

    kind: str
    source: str = ""
    width: int = 0
    height: int = 0
    fps: int = 30
    fd: int = -1
    expected: tuple[int, int] | None = None


@dataclass
class Entry:
    """One live capture pipeline."""

    spec: CaptureSpec
    channel: str
    pipeline: Gst.Pipeline
    refs: int = 0
    source_size: tuple[int, int] = (0, 0)
    camera_mode: str = ""
    probe_error: str = ""
    screen_error: str = ""
    error: str = ""
    loops: int = 0
    frames: int = 0
    last_frame: float = 0.0
    # Set when the last ref was released; a screen capture stays alive until
    # the grace expires so a rebuild can take it over instead of re-opening
    # the damage-driven portal stream.
    idle_since: float = 0.0
    # A removed layer retains its capture for the whole app lifetime.  The
    # device/stream must stay open so re-adding the source is a relink, not a
    # new capture (on Wayland a new pipewiresrc means a new portal prompt).
    retained: bool = False
    # When the last reference was released while retained (expiry clock).
    retained_since: float = 0.0
    # Pad probes on this pipeline; their closures hold the entry, so they are
    # removed at teardown or the dead pipeline is never freed.
    probes: list = field(default_factory=list)

    def teardown(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)
        for pad, probe in self.probes:
            try:
                pad.remove_probe(probe)
            except Exception:  # noqa: BLE001
                pass
        self.probes.clear()

    def as_dict(self) -> dict:
        return {
            "kind": self.spec.kind,
            "channel": self.channel,
            "refs": self.refs,
            "source_size": list(self.source_size),
            "loops": self.loops,
            "frames": self.frames,
            "error": self.error,
        }


@dataclass
class _Built:
    elements: list[Gst.Element]
    size: tuple[int, int]
    camera_mode: str = ""
    probe_error: str = ""
    check: Gst.Element | None = None
    check_size: tuple[int, int] | None = None
    dynamic_from: Gst.Element | None = None


def _link_all(elements: list[Gst.Element], dynamic_from: Gst.Element | None) -> None:
    for first, second in zip(elements, elements[1:]):
        if first is dynamic_from:
            continue
        if not first.link(second):
            raise RuntimeError(
                f"could not link {first.get_name()} -> {second.get_name()} in a capture pipeline"
            )


def _on_pad_added(_bin, pad, target) -> None:
    """Link the first video (or audio) pad a decodebin exposes."""
    caps = pad.get_current_caps() or pad.query_caps(None)
    if caps is None or caps.get_size() == 0:
        return
    if not caps.get_structure(0).get_name().startswith(("video/", "audio/")):
        return
    sink = target.get_static_pad("sink")
    if sink is not None and not sink.is_linked():
        pad.link(sink)


def _gl_screen_available() -> bool:
    """Whether the screen path can upload DMA-BUF frames through OpenGL."""
    return all(
        Gst.ElementFactory.find(name) is not None
        for name in ("glupload", "glcolorconvert", "gldownload")
    )


def capture_spec(layer, canvas_w: int, canvas_h: int, fps: int, *,
                 screen_fd: int = -1,
                 screen_size: tuple[int, int] | None = None) -> CaptureSpec:
    """Describe the capture a layer needs, independent of its box and crop."""
    kind = layer.kind
    if kind == "camera":
        width, height, rate = camera_capture_request(layer, canvas_w, canvas_h, fps)
        device = layer.source or config.camera_device()
        # Key the capture by the mode the camera will really use, not by the
        # request: a 1080p camera runs the same mode under a 1080p and a 4K
        # canvas, and a different key reopened the device on every resize.
        modes = camera_modes(device)
        if modes:
            mode = choose_from_modes(modes, width, height, rate)
            width, height, rate = mode.width, mode.height, mode.fps
        return CaptureSpec("camera", device, width, height, rate)
    if kind == "screen":
        node = None
        if layer.source:
            try:
                node = int(layer.source)
            except ValueError:
                node = None
        if node is None:
            raise RuntimeError("screen layer requires a PipeWire node id from the portal")
        # Some portal/compositor combinations report 1x1 (or 0x0) for a valid
        # stream.  Asking pipewiresrc for that size makes it fail; leaving it
        # unknown lets _screen scale to the canvas instead -- and, importantly,
        # stops the size probe from comparing a real 3831x2100 window against
        # the canvas fallback and reporting a bogus "portal stream" mismatch.
        expected = screen_size
        if expected and (expected[0] <= 1 or expected[1] <= 1):
            expected = None
        return CaptureSpec("screen", str(node), canvas_w, canvas_h, fps,
                           fd=screen_fd, expected=expected)
    if kind == "video":
        return CaptureSpec("video", layer.source, canvas_w, canvas_h, fps)
    if kind == "network":
        return CaptureSpec("network", layer.source, canvas_w, canvas_h, fps)
    if kind == "phone":
        # A paired phone always arrives letterboxed into PHONE_SIZE, so the
        # layer's crop and fit hold across rotations and reconnects.
        return CaptureSpec("phone", layer.source, PHONE_SIZE[0], PHONE_SIZE[1], fps)
    pattern = str(getattr(layer, "source", "") or "").strip().lower()
    if pattern not in TEST_PATTERNS:
        pattern = "ball"
    return CaptureSpec("test", pattern, canvas_w, canvas_h, fps)


class SourcePool:
    """Owns one capture pipeline per distinct :class:`CaptureSpec`."""

    def __init__(self) -> None:
        self._entries: dict[CaptureSpec, Entry] = {}
        self._counter = itertools.count(1)
        # Keep the channel assignment after an entry is released.  A hidden
        # layer keeps its intervideosrc alive and reconnects to the same
        # channel when it is shown again.
        self._channels: dict[CaptureSpec, str] = {}
        self._started = False
        # Optional sink for session events (the GUI routes these to the
        # Diagnostics log).  Kept as a callback so the pool stays UI-free.
        self.log_event = None

    # ------------------------------------------------------------------ public
    def _resolve(self, spec: CaptureSpec, *, claim: bool = False) -> CaptureSpec:
        """The spec whose entry serves ``spec``.

        A camera device can only be opened once.  Two layers on the same node
        with different requested modes (per-layer size or rate) therefore share
        the first capture; opening a second pipeline on the busy device failed.
        When acquiring (``claim``), an *unused* capture of another mode on the
        device (kept alive for a rebuild, or retained for a re-add) is closed
        instead, so a deliberate mode change takes effect.
        """
        if spec.kind != "camera" or spec in self._entries:
            return spec
        for other, entry in list(self._entries.items()):
            if other.kind != "camera" or other.source != spec.source:
                continue
            if not claim or entry.refs > 0:
                return other
            entry.teardown()
            del self._entries[other]
        return spec

    def _note(self, message: str) -> None:
        callback = self.log_event
        if callback is None:
            return
        try:
            callback(message)
        except Exception:  # noqa: BLE001 - logging must not break a capture
            pass

    def acquire(self, spec: CaptureSpec) -> Entry:
        self.sweep()
        spec = self._resolve(spec, claim=True)
        entry = self._entries.get(spec)
        if entry is not None and entry.error and entry.refs == 0:
            # A capture that failed is never handed out again (a camera that
            # errored must be reopened, not reused from the rebuild grace).
            entry.teardown()
            del self._entries[spec]
            entry = None
        if entry is None:
            entry = self._build(spec)
            self._entries[spec] = entry
            if self._started:
                entry.pipeline.set_state(Gst.State.PLAYING)
        elif entry.refs == 0 and entry.idle_since:
            self._note(f"{spec.kind} capture reused after a rebuild ({entry.channel})")
        elif entry.refs == 0 and entry.retained:
            self._note(f"capture reused after re-add ({entry.channel})")
        entry.idle_since = 0.0
        entry.retained = False
        entry.refs += 1
        return entry

    def pin(self, spec: CaptureSpec) -> None:
        """Keep a capture alive when its last visible layer is removed.

        Removal is not teardown: the source element and its device/portal
        stream must stay alive so adding the source back only relinks it.
        ``stop()`` still releases every retained capture at app exit.
        """
        entry = self._entries.get(self._resolve(spec))
        if entry is None:
            return
        entry.retained = True
        entry.idle_since = 0.0
        entry.retained_since = time.monotonic()
        self._note(f"capture retained for re-add ({entry.channel})")

    def release(self, spec: CaptureSpec, *, linger: bool = False) -> None:
        """Drop one reference.  ``linger`` (a canvas rebuild) keeps an unused
        camera running for a moment so the rebuilt canvas takes it over
        instead of closing and reopening the device."""
        spec = self._resolve(spec)
        entry = self._entries.get(spec)
        if entry is None or entry.refs <= 0:
            # An unmatched release must not drive the count negative: the next
            # acquire would then start at zero and be torn down under its user.
            return
        entry.refs -= 1
        if entry.refs > 0:
            return
        if entry.retained:
            # The layer was removed, not hidden.  Keep the pipeline PLAYING
            # (and the device/portal stream open) for the re-add window.
            entry.idle_since = 0.0
            entry.retained_since = time.monotonic()
            return
        if entry.spec.kind == "screen" or (
                linger and entry.spec.kind == "camera" and not entry.error):
            entry.idle_since = time.monotonic()
            self._note(f"{entry.spec.kind} capture kept alive for the next rebuild "
                       f"({entry.channel})")
            return
        entry.teardown()
        del self._entries[spec]

    def discard(self, spec: CaptureSpec) -> None:
        """Drop a released capture immediately (a removed layer's source).

        Removal is final, unlike a canvas rebuild: do not keep an idle screen
        capture around for a layer that no longer exists.  A capture shared
        with another layer stays alive for its remaining references.
        """
        spec = self._resolve(spec)
        entry = self._entries.get(spec)
        if entry is None or entry.refs > 0 or entry.retained:
            return
        entry.teardown()
        del self._entries[spec]
        self._note(f"capture released on removal ({entry.channel})")

    def sweep(self) -> None:
        """Tear down screen captures whose rebuild grace has expired."""
        now = time.monotonic()
        for spec, entry in list(self._entries.items()):
            if entry.refs > 0:
                continue
            if entry.retained:
                if (entry.spec.kind == "screen" or not entry.retained_since
                        or now - entry.retained_since < RETAIN_GRACE_S):
                    continue
                entry.teardown()
                del self._entries[spec]
                self._note(
                    f"removed capture released ({entry.channel})")
                continue
            if not entry.idle_since:
                continue
            if now - entry.idle_since < SCREEN_IDLE_GRACE_S:
                continue
            entry.teardown()
            del self._entries[spec]
            self._note(f"idle {entry.spec.kind} capture released ({entry.channel})")

    def probe_size(self, spec: CaptureSpec) -> tuple[int, int]:
        """The native frame size a capture for ``spec`` will have.

        A hidden layer builds its canvas tail before its capture exists.  It
        must still use the real source size: crop fractions and the fit are
        computed from it, and the canvas-sized request is only right when the
        camera happens to deliver exactly that mode.
        """
        entry = self._entries.get(self._resolve(spec))
        if entry is not None:
            return entry.source_size
        fallback = spec.expected or (max(2, spec.width), max(2, spec.height))
        try:
            if spec.kind == "camera":
                device = spec.source or config.camera_device()
                modes = camera_modes(device)
                mode = choose_from_modes(modes, spec.width, spec.height,
                                         spec.fps)
                if not modes:
                    mode = _loopback_mode(device, spec.width, spec.height,
                                          spec.fps) or mode
                return (mode.width, mode.height)
            if spec.kind == "video":
                return video_size(spec.source) or fallback
        except Exception:  # noqa: BLE001 - a probe must never block a build
            pass
        return fallback

    def channel_for(self, spec: CaptureSpec) -> str:
        """Stable channel for a spec, even while its capture is released."""
        # Every camera spec on one device shares a channel, so a layer built
        # before another layer's capture existed still finds the shared feed.
        key = (CaptureSpec("camera", spec.source) if spec.kind == "camera"
               else spec)
        channel = self._channels.get(key)
        if channel is None:
            channel = f"anycam-src-{next(self._counter)}"
            self._channels[key] = channel
        return channel

    def start(self) -> None:
        self._started = True
        for entry in self._entries.values():
            entry.pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        self._started = False
        for entry in list(self._entries.values()):
            entry.teardown()
        self._entries.clear()

    def entry(self, spec: CaptureSpec) -> Entry | None:
        return self._entries.get(self._resolve(spec))

    def entries(self) -> list[Entry]:
        return list(self._entries.values())

    def poll_errors(self) -> None:
        self.sweep()
        for entry in self._entries.values():
            bus = entry.pipeline.get_bus()
            if bus is None:
                continue
            while True:
                msg = bus.pop_filtered(
                    Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if msg is None:
                    break
                if msg.type == Gst.MessageType.ERROR:
                    err, debug = msg.parse_error()
                    # A network source's URL (camera password included) is
                    # often quoted by the failing element.
                    entry.error = config.redact_text(
                        f"{err.message} ({debug})", entry.spec.source)
                    print(f"[capture-error] {entry.channel} {entry.error}",
                          file=sys.stderr, flush=True)
                elif entry.spec.kind == "video" and not entry.error:
                    # File video is the only repeatable capture.  Seek the
                    # capture pipeline itself, so the canvas keeps the last
                    # frame and never sees the end of the clip as black.
                    if entry.pipeline.seek_simple(
                        Gst.Format.TIME,
                        Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                        0,
                    ):
                        entry.loops += 1
                    else:
                        entry.error = "video loop could not seek to the start"

    def errors(self) -> dict[str, str]:
        return {entry.channel: entry.error
                for entry in self._entries.values() if entry.error}

    # ------------------------------------------------------------------ build
    def _build(self, spec: CaptureSpec) -> Entry:
        channel = self.channel_for(spec)
        built = self._elements(spec)
        sink = make("intervideosink", f"{channel}-sink", channel=channel)
        if spec.kind == "screen":
            # A capture pipeline is a producer for the canvas, not a renderer.
            # With the default sync=true the sink waits for the PipeWire clock
            # that pipewiresrc provides; when the screencast has no damage that
            # clock stands still, the sink holds its buffer, PipeWire runs out
            # of buffers and the screen layer goes black.
            sink.set_property("sync", False)
        built.elements.append(sink)
        pipeline = Gst.Pipeline.new(channel)
        for element in built.elements:
            pipeline.add(element)
        _link_all(built.elements, built.dynamic_from)
        if built.dynamic_from is not None:
            index = built.elements.index(built.dynamic_from)
            built.dynamic_from.connect("pad-added", _on_pad_added,
                                       built.elements[index + 1])
        entry = Entry(spec=spec, channel=channel, pipeline=pipeline,
                      source_size=built.size, camera_mode=built.camera_mode,
                      probe_error=built.probe_error)
        sink_pad = sink.get_static_pad("sink")
        if sink_pad is not None:
            def on_buffer(_pad, _info, entry=entry):
                # This is the producer side.  Once the capture stops, the
                # canvas-side intervideosrc may repeat its last frame, so it
                # cannot be used to detect a stalled source.
                entry.frames += 1
                entry.last_frame = time.monotonic()
                return Gst.PadProbeReturn.OK

            entry.probes.append(
                (sink_pad, sink_pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)))
        if built.check is not None and built.check_size is not None:
            self._watch_size(built.check, built.check_size, entry)
        if self._started:
            pipeline.set_state(Gst.State.PLAYING)
        return entry

    def _watch_size(self, element: Gst.Element, expected: tuple[int, int],
                    entry: Entry) -> None:  # probe is tracked on the entry
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
            # Compare the shape, not the exact size: the portal reports logical
            # pixels while the buffer is physical (HiDPI scale).
            got_ratio = got[0] / got[1] if got[1] else 0.0
            want_ratio = expected[0] / expected[1] if expected[1] else 0.0
            if want_ratio and abs(got_ratio - want_ratio) > 0.02 * want_ratio:
                entry.screen_error = (
                    f"screen layer captured {got[0]}x{got[1]} but the portal "
                    f"stream is {expected[0]}x{expected[1]}"
                )
            return Gst.PadProbeReturn.OK

        entry.probes.append((pad, pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)))

    def _elements(self, spec: CaptureSpec) -> _Built:
        if spec.kind == "camera":
            return self._camera(spec)
        if spec.kind == "test":
            return self._test(spec)
        if spec.kind == "video":
            return self._file(spec)
        if spec.kind == "network":
            return self._network(spec)
        if spec.kind == "screen":
            return self._screen(spec)
        if spec.kind == "phone":
            return self._phone(spec)
        raise RuntimeError(f"no capture pipeline for layer kind '{spec.kind}'")

    @staticmethod
    def _camera(spec: CaptureSpec) -> _Built:
        device = spec.source or config.camera_device()
        modes = camera_modes(device)
        mode = choose_from_modes(modes, spec.width, spec.height, spec.fps)
        if not modes:
            mode = _loopback_mode(device, spec.width, spec.height, spec.fps) or mode
        elements: list[Gst.Element] = [
            make("v4l2src", device=device, do_timestamp=True, io_mode="mmap")
        ]
        if mode.pixel_format in ("MJPG", "JPEG"):
            elements.append(caps_filter(
                f"image/jpeg,width={mode.width},height={mode.height},framerate={mode.fps}/1"))
            elements.append(jpeg_decoder())
        elif mode.pixel_format == "H264":
            elements.append(caps_filter(
                f"video/x-h264,width={mode.width},height={mode.height},framerate={mode.fps}/1"))
            elements.extend([make("h264parse"), make("avdec_h264")])
        else:
            fmt = _gst_raw_format(mode.pixel_format)
            elements.append(caps_filter(
                f"video/x-raw,format={fmt},width={mode.width},height={mode.height},"
                f"framerate={mode.fps}/1"))
        elements.append(make("videoconvert"))
        # The capture frame is normalised here; crop/fit belong to the layer.
        elements.append(caps_filter(
            f"video/x-raw,format=RGBA,width={mode.width},height={mode.height},"
            f"framerate={mode.fps}/1"))
        return _Built(elements, (mode.width, mode.height),
                      camera_mode=f"{mode.pixel_format} {mode.width}x{mode.height}@{mode.fps}",
                      probe_error=camera_probe_error(device))

    @staticmethod
    def _test(spec: CaptureSpec) -> _Built:
        width = max(2, spec.width)
        height = max(2, spec.height)
        pattern = spec.source if spec.source in TEST_PATTERNS else "ball"
        elements: list[Gst.Element] = [
            make("videotestsrc", is_live=True, pattern=pattern),
            make("videoconvert"),
            caps_filter(f"video/x-raw,format=RGBA,width={width},height={height},"
                        f"framerate={spec.fps}/1"),
        ]
        return _Built(elements, (width, height))

    @staticmethod
    def _file(spec: CaptureSpec) -> _Built:
        width, height = video_size(spec.source) or (max(2, spec.width), max(2, spec.height))
        decode = make("decodebin")
        elements: list[Gst.Element] = [
            make("filesrc", location=str(spec.source)),
            decode,
            make("videoconvert"),
            make("videoscale"),
            make("videorate"),
            caps_filter(f"video/x-raw,format=RGBA,width={width},height={height},"
                        f"framerate={spec.fps}/1"),
        ]
        return _Built(elements, (width, height), dynamic_from=decode)

    @staticmethod
    def _network(spec: CaptureSpec) -> _Built:
        width = max(2, spec.width)
        height = max(2, spec.height)
        decode = make("uridecodebin", uri=spec.source)
        elements: list[Gst.Element] = [
            decode,
            make("videoconvert"),
            make("videoscale"),
            make("videorate"),
            caps_filter(f"video/x-raw,format=RGBA,width={width},height={height},"
                        f"framerate={spec.fps}/1"),
        ]
        return _Built(elements, (width, height), dynamic_from=decode)

    @staticmethod
    def _phone(spec: CaptureSpec) -> _Built:
        """Read a paired phone's channel (fed by ``anycam.phone.webrtc``).

        Until the phone connects, and a second after it goes away, the channel
        gives black frames; the canvas never waits on the network.
        """
        width, height = spec.width or PHONE_SIZE[0], spec.height or PHONE_SIZE[1]
        elements: list[Gst.Element] = [
            make("intervideosrc", channel=phone_channel(spec.source), timeout=Gst.SECOND),
            caps_filter(f"video/x-raw,format=I420,width={width},height={height},"
                        f"framerate={spec.fps}/1"),
            make("videoconvert"),
            caps_filter(f"video/x-raw,format=RGBA,width={width},height={height},"
                        f"framerate={spec.fps}/1"),
        ]
        return _Built(elements, (width, height))

    @staticmethod
    def _screen(spec: CaptureSpec) -> _Built:
        node = int(spec.source)
        # niri 26.04 exports the screencast as DMA-BUF only.  Asking
        # pipewiresrc for the system-memory caps that videoconvert requires
        # fails with "no more input formats" (a black or dead screen layer).
        # Upload the DMA-BUF to GL and download it before videoconvert.
        # Do not let pipewiresrc impose its PipeWire clock on the capture
        # pipeline: the canvas owns presentation timing, and a clock that only
        # advances on screencast damage stalls the sink and starves the stream.
        props = {"do_timestamp": True, "path": str(node),
                 "provide_clock": False, "max_buffers": 3, "min_buffers": 1}
        if spec.fd >= 0:
            props["fd"] = int(spec.fd)
        print(f"[screen-chain] node={node} fd={spec.fd} expected={spec.expected}",
              file=sys.stderr, flush=True)
        src = make_optional("pipewiresrc", **props)
        expected = spec.expected or (max(2, spec.width), max(2, spec.height))
        elements: list[Gst.Element] = [src]
        # Same reason as sources.screen_chain: keep the newest frame and always
        # return the rest to PipeWire, so a slow consumer cannot empty the
        # producer's pool and stall the capture.  ``leaky=2`` (downstream)
        # drops the oldest queued frame, which is what keeps the picture live
        # after the window gains focus and the consumer briefly stalls.
        elements.append(make("queue", "screen_queue", leaky=2,
                             max_size_buffers=1, max_size_bytes=0,
                             max_size_time=0, silent=True))
        if _gl_screen_available():
            elements.extend([make("glupload"), make("glcolorconvert"),
                             make("gldownload")])
        else:
            # Compositors that only expose system-memory buffers still need
            # the deprecated copy path.
            src.set_property("always-copy", True)
        if Gst.ElementFactory.find("videoflip") is not None:
            elements.append(make("videoflip",
                                 method=config.screen_flip_method()))
        elements.extend([
            make("videoconvert"),
            caps_filter("video/x-raw,format=RGBA"),
            # The portal can report a size that does not match the negotiated
            # PipeWire stream (or 1x1).  Scale instead of failing negotiation.
            make("videoscale"),
            # Do not pin a framerate here.  On niri's DMA-BUF stream a final
            # framerate cap makes the capture emit one frame and then stall;
            # the canvas layer_tail already owns the output rate.
            caps_filter(f"video/x-raw,format=RGBA,width={expected[0]},height={expected[1]}"),
        ])
        # Only probe the size when the portal actually reported one.  With an
        # unknown (0x0) size, the stream is legitimately larger than the
        # canvas and is scaled here; comparing it would raise a false alarm.
        check_size = tuple(spec.expected) if spec.expected else None
        return _Built(elements, tuple(expected), check=src,
                      check_size=check_size)
