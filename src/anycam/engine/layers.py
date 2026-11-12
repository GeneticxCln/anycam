"""Building, adding, removing and swapping layers on the canvas."""
from __future__ import annotations

import copy
import os
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from .. import config, overlay  # noqa: E402
from .. import geometry
from . import transitions
from ..geometry import box_pixels, clamp  # noqa: E402
from ..model import Layer, Scene  # noqa: E402
from ..engine.source_pool import CAPTURE_KINDS, capture_spec  # noqa: E402
from ..sources import (LIVE_KINDS, SourceChain, apply_effects, build_chain, image_chain, layer_tail)
from ..engine.encoders import (ENCODER_FACTORIES, ENCODER_PARSERS, ENCODER_BITRATES, ENCODER_LABELS, _NVENC_TTL_S, _nvenc_seen, encoder_bitrate, nvenc_available, _probe_nvenc, _va_encodes, va_factory, va_present, EncoderPlan, _requested_codec, _wants_va, encoder_plan, recording_codec, EncodedFeed, force_keyframe)  # noqa: E402,F401
from ..engine.report import (EngineReport)  # noqa: E402,F401
from ..engine.audio_helpers import (MIC_DENOISE_LEVELS, mic_dsp_wanted, describe_mic_dsp, mic_dsp_elements)  # noqa: E402,F401
from ..engine.canvas_helpers import (GL_MIXER_LATENCY_NS, canvas_requested, gl_canvas_problem, _empty_drawing)  # noqa: E402,F401

Gst.init(None)


class LayerMixin:
    """Building, adding, removing and swapping layers on the canvas."""

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
        chains_before = len(self.chains)
        self._partial_pad = None
        try:
            self._add_layer_impl(layer, zorder)
        except Exception:
            self._rollback_layer_capture(layer.id)
            self._discard_partial_layer(layer.id, chains_before)
            raise
        self._partial_pad = None

    def _discard_partial_layer(self, layer_id: str, chains_before: int) -> None:
        """Take out what a layer that failed to build left in the canvas.

        A requested compositor pad that never got a feed makes the mixer wait
        for it for ever (the canvas freezes), and an orphan chain keeps its
        elements, probes and generator threads until the next rebuild.
        """
        pad, self._partial_pad = getattr(self, "_partial_pad", None), None
        self._layer_pads.pop(layer_id, None)
        self._layer_chains.pop(layer_id, None)
        self.layer_sources.pop(layer_id, None)
        orphans = self.chains[chains_before:]
        for chain in orphans:
            for hook in getattr(chain, "cleanup", ()):
                try:
                    hook()
                except Exception:  # noqa: BLE001 - one hook must not block the rest
                    pass
        for chain in reversed(orphans):
            try:
                self._dispose_chain(chain, pad)
            except Exception:  # noqa: BLE001 - the canvas must survive its own cleanup
                pass
            pad = None
        if pad is not None and self.compositor is not None:
            try:
                self.compositor.release_request_pad(pad)
            except Exception:  # noqa: BLE001
                pass

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
        self._partial_pad = pad
        pad.set_property("xpos", box[0])
        pad.set_property("ypos", box[1])
        pad.set_property("width", box[2])
        pad.set_property("height", box[3])
        pad.set_property("zorder", int(zorder))
        # The chain delivers the picture at its natural aspect.  The pad is the
        # layer box; centring inside it (instead of stretching to it) is what
        # lets a live move/resize or layout change skip a canvas rebuild.
        pad.set_property("sizing-policy", self._sizing_policy(layer))
        pad.set_property("alpha", float(clamp(layer.alpha)) if layer.visible else 0.0)
        if self._link_to_mixer(chain, pad, layer) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"could not link {layer.name} into the compositor")
        self._layer_pads[layer.id] = pad
        self._layer_chains[layer.id] = chain
        self.ensure_layer_audio(layer)

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

    @staticmethod
    def _sizing_policy(layer: Layer) -> str:
        """Cover and stretch fill the pad; contain is centred inside it."""
        return "none" if geometry.fit_mode(layer.fit) != "contain" else "keep-aspect-ratio"

    def set_layer_framing(self, layer_id: str) -> bool:
        """Apply a layer's zoom, pan and cover window live (no rebuild).

        False when the layer has no framing stage, i.e. it was built without a
        transform: the caller must restart the engine for it to take effect.
        """
        layer = self.scene.layer(layer_id)
        chain = self._layer_chains.get(layer_id)
        framing = getattr(chain, "effects", {}).get("framing") if chain else None
        if layer is None or framing is None:
            return False
        rect = box_pixels(layer, self.canvas_w, self.canvas_h)
        framing.apply(layer, rect.w, rect.h)
        return True

    def _box_pixels(self, layer: Layer) -> tuple[int, int, int, int]:
        rect = box_pixels(layer, self.canvas_w, self.canvas_h)
        return (rect.x, rect.y, rect.w, rect.h)

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
            from .. import animated

            name, title, color, background, accent = animated.lower_third_style(
                layer.source, layer.color, layer.background)
            control.reconfigure(name, title, color, background, accent)
        elif layer.kind in ("clock", "timer"):
            control.reconfigure(layer.source, layer.color, layer.background)
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
        layer = self._find_layer(layer_id)
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
        self.apply_layer_audio(layer)
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

    def mark_layer_lost(self, layer_id: str) -> bool:
        """Swap a failed camera for its placeholder card on the running canvas.

        The dead capture is released, the layer's pad gets the "camera
        disconnected" card, and everything else - the other cameras, a screen
        share, the recording, the virtual camera - keeps running.  A rebuild
        used to do this and stopped all of them.  ``attach_capture`` is the way
        back.  Returns False when it cannot be done live (the caller rebuilds).
        """
        if self.pipeline is None or self.compositor is None or self._stopped:
            return False
        layer = next((item for item in self.scene.layers if item.id == layer_id), None)
        if layer is None or layer.kind != "camera":
            return False
        if layer_id in self.skip_layers and layer_id in self._layer_pads:
            return True                 # already showing its card
        visible = layer.visible
        self.drop_capture(layer_id)
        self.detach_layer(layer_id)     # hides the model's layer on the way out
        layer.visible = visible
        self.skip_layers.add(layer_id)
        if not self.add_layer(layer):
            self.skip_layers.discard(layer_id)
            return False
        self.refresh_layer_order()
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
            placeholder_chain = self._layer_chains.get(layer_id)
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
                # Take the old card out of the running canvas: its elements
                # and compositor pad go, so repeated loss and return do not
                # pile up cards.
                placeholder.set_property("alpha", 0.0)
                if placeholder_chain is not None:
                    self._dispose_chain(placeholder_chain, placeholder)
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
        layer = self._find_layer(layer_id)
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
        self.drop_layer_audio(layer_id)
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

    def _find_layer(self, layer_id: str) -> Layer | None:
        """A layer of the shown scene, or of the one a transition is removing."""
        for layer in self.scene.layers:
            if layer.id == layer_id:
                return layer
        for layer in getattr(self, "_ghosts", ()):
            if layer.id == layer_id:
                return layer
        return None

    def switch_scene(self, scene: Scene, transition: str = "cut", seconds: float = 0.0) -> bool:
        """Show ``scene`` on the running canvas without rebuilding it.

        The old scene's layers are taken out (cameras stay open for a while so
        a quick switch back, or a layer both scenes use, is a relink) and the
        new scene's layers are added to the same compositor.  The preview, the
        recording, the stream and the virtual camera never see a gap or a
        timestamp reset.  Returns False when it cannot be done live (the graph
        is not running, a screen share must be set up); the caller rebuilds.

        With a ``transition`` (see :mod:`anycam.engine.transitions`) the old
        layers stay on the canvas while the new ones arrive, then go.
        """
        if self.pipeline is None or self.compositor is None or self._stopped:
            return False
        if scene is self.scene:
            return True
        self.finish_transition()        # a quick second switch settles the first
        if any(layer.kind == "screen" for layer in scene.layers):
            return False        # a portal remote is needed per share
        old = self.scene
        kind = transitions.transition_kind(transition)
        ghost_ids = {layer.id for layer in old.layers if layer.id in self._layer_pads}
        animate = (kind != "cut" and seconds > 0 and ghost_ids
                   and not ghost_ids & {layer.id for layer in scene.layers})
        # detach_layer() hides the model's layer on the way out; the old scene
        # must come back exactly as it was when the user returns to it.
        visible = {layer.id: layer.visible for layer in old.layers}
        ghosts: list[Layer] = []
        if animate:
            ghosts = [layer for layer in old.layers if layer.id in ghost_ids]
            self._ghosts = ghosts
        else:
            for layer in list(old.layers):
                self.detach_layer(layer.id)
            for layer in old.layers:
                layer.visible = visible.get(layer.id, layer.visible)
        self.scene = scene
        self.skip_layers &= {layer.id for layer in scene.layers}
        self.report.media_errors.clear()
        self.report.camera_placeholders.clear()
        self.report.camera_modes = []
        self._camera_modes.clear()
        self._pads_with_data = {layer_id for layer_id in self._pads_with_data
                                if layer_id in self._layer_pads}
        incoming: list[Layer] = []
        for layer in sorted(scene.layers, key=lambda item: item.z):
            if _empty_drawing(layer):
                continue
            if not self.add_layer(layer):
                self._ghosts = []
                return False
            incoming.append(layer)
        self.refresh_layer_order()
        self._sync_standby_label()
        if animate:
            self._begin_transition(kind, float(seconds), ghosts, incoming)
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
        # Layers a transition is still removing sit underneath the new ones.
        layers = (sorted(getattr(self, "_ghosts", ()), key=lambda l: l.z)
                  + sorted(self.scene.layers, key=lambda l: l.z))
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
        # A cover window follows the box's shape.
        self.set_layer_framing(layer_id)
