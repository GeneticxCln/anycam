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
import time
from dataclasses import dataclass, field

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from . import config
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
    video_size,
)

CAPTURE_KINDS = ("camera", "test", "video", "network", "screen")


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


def capture_spec(layer, canvas_w: int, canvas_h: int, fps: int, *,
                 screen_fd: int = -1,
                 screen_size: tuple[int, int] | None = None) -> CaptureSpec:
    """Describe the capture a layer needs, independent of its box and crop."""
    kind = layer.kind
    if kind == "camera":
        width, height, rate = camera_capture_request(layer, canvas_w, canvas_h, fps)
        return CaptureSpec("camera", layer.source or config.camera_device(),
                           width, height, rate)
    if kind == "screen":
        node = None
        if layer.source:
            try:
                node = int(layer.source)
            except ValueError:
                node = None
        if node is None:
            raise RuntimeError("screen layer requires a PipeWire node id from the portal")
        return CaptureSpec("screen", str(node), canvas_w, canvas_h, fps,
                           fd=screen_fd, expected=screen_size)
    if kind == "video":
        return CaptureSpec("video", layer.source, canvas_w, canvas_h, fps)
    if kind == "network":
        return CaptureSpec("network", layer.source, canvas_w, canvas_h, fps)
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

    # ------------------------------------------------------------------ public
    def acquire(self, spec: CaptureSpec) -> Entry:
        entry = self._entries.get(spec)
        if entry is None:
            entry = self._build(spec)
            self._entries[spec] = entry
            if self._started:
                entry.pipeline.set_state(Gst.State.PLAYING)
        entry.refs += 1
        return entry

    def release(self, spec: CaptureSpec) -> None:
        entry = self._entries.get(spec)
        if entry is None:
            return
        entry.refs -= 1
        if entry.refs <= 0:
            entry.pipeline.set_state(Gst.State.NULL)
            del self._entries[spec]

    def channel_for(self, spec: CaptureSpec) -> str:
        """Stable channel for a spec, even while its capture is released."""
        channel = self._channels.get(spec)
        if channel is None:
            channel = f"anycam-src-{next(self._counter)}"
            self._channels[spec] = channel
        return channel

    def start(self) -> None:
        self._started = True
        for entry in self._entries.values():
            entry.pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        self._started = False
        for entry in list(self._entries.values()):
            entry.pipeline.set_state(Gst.State.NULL)
        self._entries.clear()

    def entry(self, spec: CaptureSpec) -> Entry | None:
        return self._entries.get(spec)

    def entries(self) -> list[Entry]:
        return list(self._entries.values())

    def poll_errors(self) -> None:
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
                    entry.error = f"{err.message} ({debug})"
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

            sink_pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
        if built.check is not None and built.check_size is not None:
            self._watch_size(built.check, built.check_size, entry)
        if self._started:
            pipeline.set_state(Gst.State.PLAYING)
        return entry

    def _watch_size(self, element: Gst.Element, expected: tuple[int, int],
                    entry: Entry) -> None:
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
            if got != tuple(expected):
                entry.screen_error = (
                    f"screen layer captured {got[0]}x{got[1]} but the portal "
                    f"stream is {expected[0]}x{expected[1]}"
                )
            return Gst.PadProbeReturn.OK

        pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)

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
            caps_filter(f"video/x-raw,format=RGBA,width={width},height={height},"
                        f"framerate={spec.fps}/1"),
        ]
        return _Built(elements, (width, height), dynamic_from=decode)

    @staticmethod
    def _screen(spec: CaptureSpec) -> _Built:
        node = int(spec.source)
        props = {"do_timestamp": True, "path": str(node)}
        if spec.fd >= 0:
            props["fd"] = int(spec.fd)
        src = make("pipewiresrc", **props)
        expected = spec.expected or (max(2, spec.width), max(2, spec.height))
        elements: list[Gst.Element] = [
            src,
            make("videoconvert"),
            caps_filter(f"video/x-raw,format=RGBA,width={expected[0]},height={expected[1]},"
                        f"framerate={spec.fps}/1"),
        ]
        return _Built(elements, tuple(expected), check=src, check_size=tuple(expected))
