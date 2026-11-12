"""The compositor graph, its frame counter and the preview branches."""
from __future__ import annotations

import sys
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from ..model import Effects, Layer  # noqa: E402
from ..sources import (SourceChain, apply_effects, background_chain,  # noqa: E402
                         caps_filter, caps_string, gl_rack_elements,
                         make)
from ..engine.encoders import (ENCODER_FACTORIES, ENCODER_PARSERS, ENCODER_BITRATES, ENCODER_LABELS, _NVENC_TTL_S, _nvenc_seen, encoder_bitrate, nvenc_available, _probe_nvenc, _va_encodes, va_factory, va_present, EncoderPlan, _requested_codec, _wants_va, encoder_plan, recording_codec, EncodedFeed, force_keyframe)  # noqa: E402,F401
from ..engine.report import (EngineReport)  # noqa: E402,F401
from ..engine.audio_helpers import (MIC_DENOISE_LEVELS, mic_dsp_wanted, describe_mic_dsp, mic_dsp_elements)  # noqa: E402,F401
from ..engine.canvas_helpers import (GL_MIXER_LATENCY_NS, canvas_requested, gl_canvas_problem, _empty_drawing)  # noqa: E402,F401

Gst.init(None)


class CanvasMixin:
    """The compositor graph, its frame counter and the preview branches."""

    # The largest preview frame.  Every frame is copied once into Python
    # (PyGObject maps a buffer into new ``bytes``), so its size is paid per
    # frame on the GUI thread; above this the widget upscales it on paint,
    # which costs less than the bigger copy.
    PREVIEW_MAX = (960, 540)

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
            crop = tuple(getattr(layer, "crop", None) or (0.0, 0.0, 0.0, 0.0))
            symmetric = len(crop) < 4 or abs(float(crop[0]) - float(crop[2])) < 1e-6
            gl_rack = (gl_rack_elements(mirror_on_gpu=symmetric)
                       if chain.effects.get("balance") is not None else None)
            if gl_rack:
                # The colour step (and mirror) as shaders (see apply_effects).
                gl_elements = [gl_rack["gl_balance"]] + (
                    [gl_rack["gl_flip"]] if "gl_flip" in gl_rack else [])
                self._add(*gl_elements)
                chain.add(*gl_elements)
                previous = upload
                for element in gl_elements:
                    if not previous.link(element):
                        raise RuntimeError("could not add the GPU colour step")
                    previous = element
                chain.tail = gl_elements[-1]
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
