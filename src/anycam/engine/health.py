"""Per-source health and the one-second performance sample."""
from __future__ import annotations

import time
from collections import deque

import gi
import psutil

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst  # noqa: E402

from ..engine.encoders import (ENCODER_FACTORIES, ENCODER_PARSERS, ENCODER_BITRATES, ENCODER_LABELS, encoder_bitrate, nvenc_available, _probe_nvenc, _va_encodes, va_factory, va_present, EncoderPlan, _requested_codec, _wants_va, encoder_plan, recording_codec, EncodedFeed, force_keyframe)  # noqa: E402,F401
from ..engine.report import (EngineReport)  # noqa: E402,F401
from ..engine.audio_helpers import (MIC_DENOISE_LEVELS, mic_dsp_wanted, describe_mic_dsp, mic_dsp_elements)  # noqa: E402,F401
from ..engine.canvas_helpers import (GL_MIXER_LATENCY_NS, canvas_requested, gl_canvas_problem, _empty_drawing)  # noqa: E402,F401

Gst.init(None)


class HealthMixin:
    """Per-source health and the one-second performance sample."""

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
