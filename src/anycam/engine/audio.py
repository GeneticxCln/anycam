"""The audio mixer, microphone and shared AAC tee."""
from __future__ import annotations

import random
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from .. import config  # noqa: E402
from ..sources import (caps_filter, make)
from ..engine.encoders import (ENCODER_FACTORIES, ENCODER_PARSERS, ENCODER_BITRATES, ENCODER_LABELS, _NVENC_TTL_S, _nvenc_seen, encoder_bitrate, nvenc_available, _probe_nvenc, _va_encodes, va_factory, va_present, EncoderPlan, _requested_codec, _wants_va, encoder_plan, recording_codec, EncodedFeed, force_keyframe)  # noqa: E402,F401
from ..engine.report import (EngineReport)  # noqa: E402,F401
from ..engine.audio_helpers import (MIC_DENOISE_LEVELS, mic_dsp_wanted, describe_mic_dsp, mic_dsp_elements, voice_elements, delay_element, describe_voice_fx, audio_server_socket, virtual_mic_properties, VIRTUAL_MIC_NAME)  # noqa: E402,F401
from ..engine.source_pool import audio_channel  # noqa: E402  # noqa: E402,F401
from ..engine.canvas_helpers import (GL_MIXER_LATENCY_NS, canvas_requested, gl_canvas_problem, _empty_drawing)  # noqa: E402,F401

Gst.init(None)

LEVEL_STALE_S = 0.6     # the level elements report every 100 ms


class AudioMixin:
    """The audio mixer, microphone and shared AAC tee."""

    def _mixer_input(self, name: str, gain_db: float, muted: bool, meter: bool = False,
                     delay_ms: int = 0):
        """Convert/resample one audio source and attach it to the mixer.

        ``delay_ms`` holds the source back (a sync offset).  Every input but the
        silent clock can carry a level meter; ``audio_levels()`` reads them.
        """
        assert self.audio_mixer is not None
        conv = make("audioconvert", f"{name}_conv")
        res = make("audioresample", f"{name}_resample")
        caps = caps_filter("audio/x-raw,format=F32LE,rate=48000,channels=2", f"{name}_caps")
        vol = make("volume", f"{name}_volume", volume=10 ** (gain_db / 20.0))
        vol.set_property("mute", bool(muted))
        self._add(conv, res, caps, vol)
        self._link([conv, res, caps, vol])
        delay = delay_element(delay_ms, f"{name}_delay")
        if delay is not None:
            self._add(delay)
            self._link([vol, delay])
            tail = delay
        else:
            tail = vol
        level = None
        if meter:
            level = make("level", f"{name}_level", interval=100_000_000, post_messages=True)
            self._add(level)
            self._link([tail, level])
            tail = level
        pad = self.audio_mixer.request_pad_simple("sink_%u")
        tail.get_static_pad("src").link(pad)
        return conv, vol, level

    def _build_audio(self) -> None:
        assert self.pipeline is not None
        # Not ignore_inactive_pads: when the mixer's first (timed) aggregation
        # runs before any input has delivered a buffer, every pad counts as
        # inactive, the mixer answers "we're EOS" and pauses its output task for
        # good.  The input that is still waiting for it then blocks in a query
        # on the mixer for ever (no audio, and engine.stop hangs in the audio
        # EOS push).  Measured with the GPU preview: 6 of 8 starts dead with
        # True, 0 of 8 with False.  A live mixer outputs at its deadline either
        # way, so a slow microphone does not stall it.
        mixer = make("audiomixer", "audio_mixer", ignore_inactive_pads=False)
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
                self.project.audio.mic_muted, meter=True,
                delay_ms=self.project.audio.mic_delay_ms)
            self._add(src)
            dsp = [*mic_dsp_elements(self.project.audio),
                   *voice_elements(self.project.audio)]
            if dsp:
                self._add(*dsp)
                self._link([src, *dsp, conv])
                self.report.mic_processing = describe_mic_dsp(self.project.audio)
            else:
                self._link([src, conv])
                if mic_dsp_wanted(self.project.audio):
                    self.report.mic_processing = (
                        "unavailable: GStreamer webrtcdsp is not installed")
            fx = describe_voice_fx(self.project.audio)
            if fx:
                self.report.mic_processing = ", ".join(
                    part for part in (self.report.mic_processing, fx) if part)
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
                self.project.audio.desktop_muted, meter=True,
                delay_ms=self.project.audio.desktop_delay_ms)
            self._add(src)
            self._link([src, conv])
            self.desktop_volume = vol
            self._audio_eos_pads.append(src.get_static_pad("src"))
            self.report.audio_inputs.append(f"desktop:{desktop}")

        self._build_media_bed()
        for layer in self.scene.layers:
            self.ensure_layer_audio(layer)

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

    # ------------------------------------------------------ video-layer sound
    def ensure_layer_audio(self, layer) -> bool:
        """Mix a video layer's sound into the canvas audio (idempotent).

        Only file videos carry sound; the capture pipeline publishes it on an
        ``interaudiosink`` channel and this reads it through ``interaudiosrc``,
        so the clip's sound loops with its picture and several layers on one
        file share one decode.  Returns True when the layer has an audio input.
        """
        if (self.audio_mixer is None or getattr(layer, "kind", "") != "video"
                or not layer.source or Gst.ElementFactory.find("interaudiosrc") is None):
            return False
        if layer.id in self._layer_audio:
            return True
        spec = self._layer_specs.get(layer.id)
        if spec is None:
            return False
        channel = audio_channel(self.pool.channel_for(spec))
        name = f"layeraudio_{layer.id}"
        src = make("interaudiosrc", f"{name}_src", channel=channel)
        conv, vol, _level = self._mixer_input(
            name, 0.0, False, meter=True)
        self._add(src)
        self._link([src, conv])
        self._layer_audio[layer.id] = (src, conv, vol)
        self.report.audio_inputs.append(f"layer:{layer.name}")
        self.apply_layer_audio(layer)
        for element in (src, conv):
            element.sync_state_with_parent()
        return True

    def apply_layer_audio(self, layer) -> None:
        """Push a layer's volume, mute and visibility onto its audio input."""
        entry = self._layer_audio.get(layer.id)
        if entry is None:
            return
        vol = entry[2]
        volume = max(0.0, min(4.0, float(getattr(layer, "volume", 1.0))))
        vol.set_property("volume", volume)
        vol.set_property("mute", bool(getattr(layer, "muted", False)) or not layer.visible)

    def set_layer_volume(self, layer_id: str, volume: float, muted: bool) -> bool:
        layer = self._find_layer(layer_id)
        if layer is None:
            return False
        layer.volume, layer.muted = float(volume), bool(muted)
        self.apply_layer_audio(layer)
        return layer_id in self._layer_audio

    def drop_layer_audio(self, layer_id: str) -> None:
        """Take a removed layer's sound out of the mixer."""
        entry = self._layer_audio.pop(layer_id, None)
        if entry is None or self.pipeline is None:
            return
        prefix = f"layeraudio_{layer_id}_"
        members = [element for element in self.pipeline.children
                   if element.get_name().startswith(prefix)]
        # Unlink the last element from the mixer before stopping.  (Video
        # layer chains are stopped first, see _dispose_chain; this path was
        # checked with a clip that has sound: no pipeline error on removal.)
        # The mixer pad is released only after the stop (releasing first broke
        # re-add).
        mixer_pads = []
        for element in members:
            pad = element.get_static_pad("src")
            peer = pad.get_peer() if pad is not None else None
            if peer is None:
                continue
            pad.unlink(peer)
            if peer.get_parent_element() is self.audio_mixer:
                mixer_pads.append(peer)
        for element in members:
            element.set_state(Gst.State.NULL)
        for peer in mixer_pads:
            self.audio_mixer.release_request_pad(peer)
        for element in members:
            self.pipeline.remove(element)

    # ------------------------------------------------------------- media bed
    def _media_paths(self) -> list[str]:
        audio = self.project.audio
        wanted = [audio.media_file, *audio.media_playlist] if audio.media_file else \
            list(audio.media_playlist)
        found = [path for path in wanted if path and Path(path).exists()]
        missing = [path for path in wanted if path and not Path(path).exists()]
        if missing:
            self.report.media_error = f"media audio file not found: {missing[0]}"
        if audio.media_shuffle and len(found) > 1:
            random.shuffle(found)
        return found

    def _build_media_bed(self) -> None:
        """The background audio file or playlist, looping as asked."""
        paths = self._media_paths()
        self._media_items = paths
        self._media_index = 0
        self._media_eos = False
        if not paths:
            return
        audio = self.project.audio
        decode = make("uridecodebin", "media_audio", uri=Path(paths[0]).resolve().as_uri())
        conv, vol, _level = self._mixer_input("media", audio.media_gain_db, audio.media_muted,
                                              meter=True, delay_ms=audio.media_delay_ms)
        self._add(decode)
        self.media_volume = vol
        self._media_decode = decode
        self._media_sink = conv.get_static_pad("sink")
        self.report.audio_inputs.append(f"media:{paths[0]}")

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

        def on_event(_pad, info):
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.EOS:
                # The bed ended: the mixer must not see EOS (it would end the
                # whole mix); pump() starts the next item or the same one again.
                self._media_eos = True
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        self._probe(self._media_sink, Gst.PadProbeType.EVENT_DOWNSTREAM, on_event)

    def service_media(self) -> bool:
        """Start the next bed item (or loop the only one) after the last ended.

        Returns True when something was restarted.  Called from ``pump``.
        """
        if not getattr(self, "_media_eos", False) or self._stopped or self.pipeline is None:
            return False
        self._media_eos = False
        items = self._media_items
        loop = bool(self.project.audio.media_loop)
        index = self._media_index + 1
        if index >= len(items):
            if not loop:
                self.report.audio_inputs = [name for name in self.report.audio_inputs
                                            if not name.startswith("media:")] + ["media:(finished)"]
                return False
            index = 0
        self._media_index = index
        decode = self._media_decode
        clock = self.pipeline.get_clock()
        if clock is not None:
            # The new item's timestamps start at zero: shift them to "now" so the
            # mixer does not treat the whole file as late.
            self._media_sink.set_offset(max(0, clock.get_time() - self.pipeline.get_base_time()))
        decode.set_state(Gst.State.NULL)
        decode.set_property("uri", Path(items[index]).resolve().as_uri())
        decode.sync_state_with_parent()
        return True

    def audio_levels(self) -> dict[str, tuple[float, float]]:
        """Latest (peak dB, rms dB) per metered input ("mic", "desktop", "media", "master").

        A meter that stopped reporting (its source ended or was unplugged)
        reads silence, not the last value it happened to post.
        """
        now = time.monotonic()
        return {name: ((peak, loud) if now - when <= LEVEL_STALE_S else (-100.0, -100.0))
                for name, (peak, loud, when) in self._audio_levels.items()}

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
        master = make("level", "master_level", interval=100_000_000, post_messages=True)
        # Pin the mixer's output.  When the mixer aggregates before any input
        # has caps it outputs its default 44100 Hz and then switches to the
        # inputs' 48000 Hz; the AAC encoder follows with a new stream, and the
        # recorder (one audio track, one rate) ended its audio after the first
        # few buffers.  Seen with the GPU preview, whose slower start makes the
        # first aggregation early.
        mixer_caps = caps_filter("audio/x-raw,format=F32LE,rate=48000,channels=2",
                                 "audio_mixer_caps")
        raw = make("tee", "audio_raw_tee", allow_not_linked=True)
        queue = make("queue", "audio_encode_queue", max_size_time=2 * Gst.SECOND,
                     max_size_buffers=0, max_size_bytes=0)
        aconv = make("audioconvert", "audio_shared_conv")
        aenc = make("avenc_aac", "audio_shared_enc", bitrate=128000)
        aparse = make("aacparse", "audio_shared_parse")
        tee = make("tee", "audio_shared_tee")
        self._add(mixer_caps, master, raw, queue, aconv, aenc, aparse, tee)
        self._link([self.audio_mixer, mixer_caps, master, raw, queue, aconv, aenc, aparse, tee])
        self.audio_tee = tee
        self._audio_raw_tee = raw
        self._build_audio_outputs(raw)
        return tee

    def _audio_branch(self, raw: Gst.Element, name: str, sink: Gst.Element) -> bool:
        """raw tee -> queue -> convert -> resample -> ``sink``; False when it cannot link."""
        queue = make("queue", f"{name}_queue", max_size_time=Gst.SECOND,
                     max_size_buffers=0, max_size_bytes=0, leaky="downstream")
        conv = make("audioconvert", f"{name}_conv")
        res = make("audioresample", f"{name}_resample")
        caps = caps_filter("audio/x-raw,format=F32LE,rate=48000,channels=2", f"{name}_caps")
        self._add(queue, conv, res, caps, sink)
        self._link([queue, conv, res, caps, sink])
        pad = raw.request_pad_simple("src_%u")
        return pad is not None and pad.link(queue.get_static_pad("sink")) == Gst.PadLinkReturn.OK

    def _build_audio_outputs(self, raw: Gst.Element) -> None:
        """The monitor (hear the mix) and the virtual microphone (other apps record it)."""
        audio = self.project.audio
        report = self.report
        if audio.monitor_device:
            device = "" if audio.monitor_device == "default" else audio.monitor_device
            # Feedback guard: monitoring to the device whose monitor source is
            # captured as desktop audio loops (audit item 9).  PulseAudio names
            # a sink's monitor source "<sink>.monitor".
            desktop = (audio.desktop_device or "").strip()
            monitor = (audio.monitor_device or "").strip()
            desktop_sink = desktop[:-8] if desktop.endswith(".monitor") else desktop
            if desktop and monitor and monitor != "default" and (
                    monitor == desktop_sink or desktop == f"{monitor}.monitor"):
                report.audio_monitor_error = (
                    "monitor output would play into the desktop-audio capture "
                    "(feedback loop); monitor disabled")
            elif not audio_server_socket("pulse"):
                report.audio_monitor_error = "no PulseAudio/PipeWire-Pulse server is running"
            elif Gst.ElementFactory.find("pulsesink") is None:
                report.audio_monitor_error = "GStreamer pulsesink is not installed"
            else:
                sink = make("pulsesink", "audio_monitor_sink", sync=False)
                if device:
                    sink.set_property("device", device)
                if self._audio_branch(raw, "audio_monitor", sink):
                    report.audio_monitor = device or "default output"
                else:
                    report.audio_monitor_error = "could not link the monitor output"
        if audio.virtual_mic:
            if not audio_server_socket("pipewire"):
                report.virtual_mic_error = "PipeWire is not running"
            elif Gst.ElementFactory.find("pipewiresink") is None:
                report.virtual_mic_error = "GStreamer pipewiresink is not installed"
            else:
                sink = make("pipewiresink", "audio_vmic_sink", sync=False, mode="provide")
                props = Gst.Structure.from_string(virtual_mic_properties())[0]
                if props is not None:
                    sink.set_property("stream-properties", props)
                if self._audio_branch(raw, "audio_vmic", sink):
                    report.virtual_mic = VIRTUAL_MIC_NAME
                else:
                    report.virtual_mic_error = "could not link the virtual microphone"

    def set_mic_gain(self, db: float) -> None:
        self.project.audio.mic_gain_db = db
        if self.mic_volume is not None:
            self.mic_volume.set_property("volume", 10 ** (db / 20.0))

    def set_mic_muted(self, muted: bool) -> None:
        self.project.audio.mic_muted = muted
        if self.mic_volume is not None:
            self.mic_volume.set_property("mute", bool(muted))
