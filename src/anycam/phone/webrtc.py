"""Receive one phone's camera over WebRTC and publish it on its channel.

``webrtcbin`` answers the phone's offer (no trickle ICE: both SDPs carry
their host candidates, so one HTTPS round trip sets the call up), decodes the
video and letterboxes it into :data:`PHONE_SIZE` on ``intervideosink``.  The
capture pool reads that channel like any other source; when the phone goes
away the channel simply stops and the canvas keeps running.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC  # noqa: E402

from . import PHONE_SIZE, phone_channel  # noqa: E402

Gst.init(None)

ICE_GATHER_TIMEOUT_S = 5.0
# Decoders that need a GPU context.  A phone's 1080p stream decodes cheaply on
# the CPU, and keeping it there leaves the GPU to the canvas and encoders.
_GPU_DECODER_PREFIXES = ("nv", "va", "vulkan", "v4l2sl", "qsv", "d3d")
_AUTOPLUG_SKIP = 2  # GstAutoplugSelectResult.SKIP


def prepare_offer(sdp: str) -> str:
    """Drop the video-orientation header extension from a phone's offer.

    With it negotiated, Android sends unrotated frames plus a rotation tag
    that the receive pipeline does not apply (sideways video).  Without it the
    phone rotates the pixels itself.
    """
    lines = [line for line in sdp.replace("\r\n", "\n").split("\n")
             if "urn:3gpp:video-orientation" not in line]
    return "\r\n".join(lines).rstrip("\r\n") + "\r\n"


class PhoneReceiver:
    """One WebRTC call from one paired phone."""

    def __init__(self, device_id: str, *,
                 on_event: Callable[[str, str], None] | None = None,
                 on_closed: Callable[[], None] | None = None) -> None:
        self.device = device_id
        self.channel = phone_channel(device_id)
        self.on_event = on_event
        self.on_closed = on_closed
        self.pipeline: Gst.Pipeline | None = None
        self.webrtc: Gst.Element | None = None
        self.frames = 0
        self.last_frame = 0.0
        self.error = ""
        self.closed = False
        self._video_linked = False
        self._gathered = threading.Event()
        self._lock = threading.Lock()
        self._handlers: list[tuple[Gst.Object, int]] = []
        self._probes: list[tuple[Gst.Pad, int]] = []

    # ------------------------------------------------------------ signalling
    def answer(self, offer_sdp: str, timeout: float = 10.0) -> str:
        """Set the phone's offer and return the complete SDP answer."""
        result, message = GstSdp.SDPMessage.new_from_text(prepare_offer(offer_sdp))
        if result != GstSdp.SDPResult.OK:
            raise ValueError("the phone sent an unreadable SDP offer")
        pipeline = Gst.Pipeline.new(f"phone-{self.device}")
        webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")
        if webrtc is None:
            raise RuntimeError("GStreamer webrtcbin is not installed")
        webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        webrtc.set_property("latency", 120)
        # Do not read webrtcbin's "ice-agent" from Python to set a port
        # range: PyGObject drops a reference it never owned and the agent is
        # freed under webrtcbin (GStreamer criticals, then a crash).
        pipeline.add(webrtc)
        with self._lock:
            if self.closed:
                raise RuntimeError("the phone receiver was stopped")
            self.pipeline, self.webrtc = pipeline, webrtc
        self._connect(webrtc, "pad-added", self._on_webrtc_pad)
        self._connect(webrtc, "notify::ice-gathering-state", self._on_gathering)
        self._connect(webrtc, "notify::connection-state", self._on_connection)
        bus = pipeline.get_bus()
        if bus is not None:
            bus.set_sync_handler(self._on_bus)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("the phone receive pipeline did not start")

        offer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.OFFER, message)
        self._call(webrtc, "set-remote-description", offer, timeout)
        answer = self._call(webrtc, "create-answer", None, timeout, "answer")
        self._call(webrtc, "set-local-description", answer, timeout)
        if webrtc.get_property("ice-gathering-state") == \
                GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self._gathered.set()
        if not self._gathered.wait(ICE_GATHER_TIMEOUT_S):
            print(f"[phone] {self.device}: ICE gathering still running, "
                  "answering with the candidates found so far",
                  file=sys.stderr, flush=True)
        with self._lock:
            if self.closed:
                raise RuntimeError("the phone receiver was stopped")
        local = webrtc.get_property("local-description")
        if local is None:
            raise RuntimeError("webrtcbin has no local description")
        return local.sdp.as_text()

    @staticmethod
    def _call(webrtc: Gst.Element, signal: str, argument, timeout: float,
              field: str | None = None):
        """Emit a promise signal and wait; return ``field`` of the reply.

        The reply structure belongs to the promise and dies with it, so the
        field is read here, while the promise is still alive.  Returning the
        structure itself handed callers freed memory (a segfault).
        """
        # Promise.wait() has no timeout; webrtcbin answers from its own
        # thread, so wait on the change callback instead of risking a request
        # that never returns.
        done = threading.Event()
        promise = Gst.Promise.new_with_change_func(lambda *_args: done.set())
        webrtc.emit(signal, argument, promise)
        if not done.wait(timeout):
            promise.interrupt()
            raise TimeoutError(f"webrtcbin did not finish {signal}")
        if promise.wait() != Gst.PromiseResult.REPLIED:
            raise RuntimeError(f"{signal} did not produce a reply")
        reply = promise.get_reply()
        if reply is not None and reply.has_field("error"):
            raise RuntimeError(f"{signal} failed: {PhoneReceiver._reply_error(reply)}")
        if field is None:
            return None
        if reply is None or not reply.has_field(field):
            raise RuntimeError(f"{signal} returned no {field}")
        # get_value() wraps the boxed value without owning it; take an owned
        # copy while the promise (and its reply) is still alive.
        value = reply.get_value(field)
        if hasattr(value, "copy"):
            value = value.copy()
        del reply
        promise = None  # noqa: F841 - the reply may die now
        return value

    @staticmethod
    def _reply_error(reply) -> str:
        if reply is None or not reply.has_field("error"):
            return "no reply"
        error = reply.get_value("error")
        return getattr(error, "message", None) or str(error)

    # ------------------------------------------------------------- callbacks
    def _connect(self, obj: Gst.Object, signal: str, handler) -> None:
        handler_id = obj.connect(signal, handler)
        with self._lock:
            if self.closed:
                should_disconnect = True
            else:
                self._handlers.append((obj, handler_id))
                should_disconnect = False
        if should_disconnect:
            try:
                obj.disconnect(handler_id)
            except Exception:  # noqa: BLE001
                pass

    def _emit(self, event: str, detail: str = "") -> None:
        with self._lock:
            closed = self.closed
        if self.on_event is not None and not closed:
            try:
                self.on_event(event, detail)
            except Exception as exc:  # noqa: BLE001 - never kill a GStreamer thread
                print(f"[phone] event handler failed: {exc}", file=sys.stderr, flush=True)

    def _trigger_close(self) -> None:
        callback = self.on_closed
        if callback is not None:
            try:
                callback()
            except Exception as exc:  # noqa: BLE001
                print(f"[phone] close handler failed: {exc}", file=sys.stderr, flush=True)
        else:
            threading.Thread(target=self.stop, name=f"anycam-phone-stop-{self.device}",
                             daemon=True).start()

    def _on_gathering(self, webrtc, _pspec) -> None:
        state = webrtc.get_property("ice-gathering-state")
        if state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self._gathered.set()

    def _on_connection(self, webrtc, _pspec) -> None:
        state = webrtc.get_property("connection-state")
        if state == GstWebRTC.WebRTCPeerConnectionState.CONNECTED:
            self._emit("connected")
        elif state == GstWebRTC.WebRTCPeerConnectionState.FAILED:
            self.error = self.error or "the network connection to the phone failed"
            self._emit("failed", self.error)
            self._trigger_close()
        elif state == GstWebRTC.WebRTCPeerConnectionState.DISCONNECTED:
            self._emit("disconnected")
        elif state == GstWebRTC.WebRTCPeerConnectionState.CLOSED:
            self._emit("disconnected")
            self._trigger_close()

    def _on_bus(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            self.error = f"{err.message} ({debug})"
            print(f"[phone] {self.device}: {self.error}", file=sys.stderr, flush=True)
            self._emit("failed", err.message)
        return Gst.BusSyncReply.DROP

    def _on_webrtc_pad(self, _webrtc, pad) -> None:
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        with self._lock:
            if self.closed or self.pipeline is None:
                return
            pipeline = self.pipeline
        decode = Gst.ElementFactory.make("decodebin")
        if decode is None:
            self.error = "GStreamer decodebin is not installed"
            self._emit("failed", self.error)
            return
        self._connect(decode, "autoplug-select", self._autoplug_select)
        self._connect(decode, "pad-added", self._on_decoded_pad)
        pipeline.add(decode)
        decode.sync_state_with_parent()
        sink_pad = decode.get_static_pad("sink")
        if sink_pad is None or pad.link(sink_pad) != Gst.PadLinkReturn.OK:
            self.error = "could not link incoming WebRTC stream to decoder"
            print(f"[phone] {self.device}: {self.error}", file=sys.stderr, flush=True)
            self._emit("failed", self.error)

    @staticmethod
    def _autoplug_select(_bin, _pad, _caps, factory) -> int:
        name = factory.get_name()
        klass = factory.get_metadata("klass") or ""
        if "Decoder" in klass and name.startswith(_GPU_DECODER_PREFIXES):
            return _AUTOPLUG_SKIP
        return 0  # TRY

    def _link_fakesink(self, pipeline: Gst.Pipeline, pad: Gst.Pad) -> None:
        sink = Gst.ElementFactory.make("fakesink")
        if sink is None:
            return
        sink.set_property("sync", False)
        pipeline.add(sink)
        sink.sync_state_with_parent()
        sink_pad = sink.get_static_pad("sink")
        if sink_pad is not None:
            pad.link(sink_pad)

    def _on_decoded_pad(self, _decode, pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
        with self._lock:
            if self.closed or self.pipeline is None:
                return
            pipeline = self.pipeline
            route_video = name.startswith("video/") and not self._video_linked
            if route_video:
                self._video_linked = True
        if not route_video:
            self._link_fakesink(pipeline, pad)
            return
        width, height = PHONE_SIZE
        factories = ("queue", "videoconvert", "videoscale", "capsfilter", "intervideosink")
        elements = [Gst.ElementFactory.make(factory) for factory in factories]
        if any(element is None for element in elements):
            missing = [f for f, e in zip(factories, elements) if e is None]
            with self._lock:
                self._video_linked = False
            self.error = f"missing GStreamer element(s): {', '.join(missing)}"
            self._emit("failed", self.error)
            self._link_fakesink(pipeline, pad)
            return
        queue, _convert, scale, caps_filter, sink = elements
        queue.set_property("leaky", 2)
        queue.set_property("max-size-buffers", 2)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", 0)
        # Letterbox an upright phone instead of stretching it.
        scale.set_property("add-borders", True)
        caps_filter.set_property("caps", Gst.Caps.from_string(
            f"video/x-raw,format=I420,width={width},height={height},"
            "pixel-aspect-ratio=1/1"))
        sink.set_property("channel", self.channel)
        # The canvas samples the newest frame at its own rate; waiting on the
        # clock here only adds latency.
        sink.set_property("sync", False)
        for element in elements:
            pipeline.add(element)
        linked = True
        for upstream, downstream in zip(elements, elements[1:]):
            if not upstream.link(downstream):
                linked = False
                break
        if linked:
            for element in elements:
                element.sync_state_with_parent()
            sink_pad = sink.get_static_pad("sink")
            if sink_pad is not None:
                probe_id = sink_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_frame)
                with self._lock:
                    if self.closed:
                        should_remove = True
                    else:
                        self._probes.append((sink_pad, probe_id))
                        should_remove = False
                if should_remove:
                    sink_pad.remove_probe(probe_id)
                    return
            queue_sink = queue.get_static_pad("sink")
            if queue_sink is None or pad.link(queue_sink) != Gst.PadLinkReturn.OK:
                linked = False
        if not linked:
            for element in elements:
                element.set_state(Gst.State.NULL)
                pipeline.remove(element)
            with self._lock:
                self._video_linked = False
            self.error = "could not link decoded phone video"
            print(f"[phone] {self.device}: {self.error}", file=sys.stderr, flush=True)
            self._emit("failed", self.error)
            self._link_fakesink(pipeline, pad)

    def _on_frame(self, _pad, _info):
        self.frames += 1
        self.last_frame = time.monotonic()
        if self.frames == 1:
            self._emit("streaming")
        return Gst.PadProbeReturn.OK

    # -------------------------------------------------------------- teardown
    def stop(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            pipeline, self.pipeline = self.pipeline, None
            self.webrtc = None
            handlers = list(self._handlers)
            probes = list(self._probes)
            self._handlers.clear()
            self._probes.clear()
        self._gathered.set()
        for obj, handler in handlers:
            try:
                obj.disconnect(handler)
            except Exception:  # noqa: BLE001
                pass
        for pad, probe in probes:
            try:
                pad.remove_probe(probe)
            except Exception:  # noqa: BLE001
                pass
        if pipeline is not None:
            bus = pipeline.get_bus()
            if bus is not None:
                bus.set_sync_handler(None)
            pipeline.set_state(Gst.State.NULL)
