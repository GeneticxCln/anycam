"""Real-time spectral and transient (keyboard click / desk tap) microphone denoiser.

Runs directly on 48 kHz mono F32LE audio buffers via a zero-copy in-place
GStreamer pad probe (no extra thread, no appsink/appsrc latency).  Combines:

1. **STFT spectral floor tracking** (Hann-windowed 10 ms frames, 50% overlap)
   with smooth Wiener-style gain suppression for non-stationary room noise.
2. **Impulsive transient suppressor** that detects sharp broadband spikes
   (mechanical keyboard clicks, mouse clicks, desk knocks) via instantaneous
   onset-to-envelope ratio and attenuates the impulse without muddying voice.
"""
from __future__ import annotations

import numpy as np

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .sources import caps_filter, make


class TransientSpectralDenoiser:
    """Streaming 48 kHz mono overlap-add spectral + transient denoiser."""

    def __init__(
        self,
        sample_rate: int = 48000,
        frame_size: int = 512,
        strength: float = 0.75,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.frame_size = int(frame_size)
        self.hop = self.frame_size // 2
        self.strength = float(max(0.0, min(1.0, strength)))
        # Analysis/synthesis sqrt-Hann window satisfies perfect-reconstruction COLA at 50% overlap.
        n = np.arange(self.frame_size, dtype=np.float32)
        self._win = np.sqrt(0.5 - 0.5 * np.cos(2.0 * np.pi * n / self.frame_size)).astype(np.float32)
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._overlap = np.zeros(self.hop, dtype=np.float32)
        self._out_buf = np.zeros(0, dtype=np.float32)
        bins = self.frame_size // 2 + 1
        self._noise_psd = np.full(bins, 1e-6, dtype=np.float32)
        self._prev_gain = np.ones(bins, dtype=np.float32)
        self._env = 1e-4
        self._frames_seen = 0
        # High-frequency transient band (> 2.5 kHz, where keyboard clicks concentrate energy).
        freqs = np.fft.rfftfreq(self.frame_size, d=1.0 / self.sample_rate)
        self._hf_mask = freqs >= 2500.0
        self._voice_mask = (freqs >= 120.0) & (freqs <= 2400.0)

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        windowed = frame * self._win
        spec = np.fft.rfft(windowed)
        mag2 = (spec.real * spec.real + spec.imag * spec.imag).astype(np.float32)
        self._frames_seen += 1

        # 1. Adaptive minimum-statistics noise floor estimate.
        if self._frames_seen <= 4:
            self._noise_psd = np.maximum(self._noise_psd, mag2 * 0.5)
        else:
            # Track downward quickly, upward slowly so speech never raises the noise floor.
            lower = mag2 < self._noise_psd
            self._noise_psd = np.where(
                lower,
                0.85 * self._noise_psd + 0.15 * mag2,
                0.992 * self._noise_psd + 0.008 * mag2,
            )
            self._noise_psd = np.maximum(self._noise_psd, 1e-9)

        # 2. Wiener-like spectral gain.
        snr = mag2 / (self._noise_psd + 1e-9)
        over = 1.0 + 1.8 * self.strength
        raw_gain = np.clip((snr - over) / (snr + 1e-6), 0.08, 1.0)
        gain = 0.65 * self._prev_gain + 0.35 * raw_gain
        self._prev_gain = gain

        # 3. Impulsive transient (keyboard/mouse click) detector:
        #    short sharp burst with high HF-to-voice ratio and high crest factor.
        rms = float(np.sqrt(np.mean(frame * frame) + 1e-12))
        peak = float(np.max(np.abs(frame)))
        crest = peak / (rms + 1e-6)
        hf_energy = float(np.mean(mag2[self._hf_mask])) if np.any(self._hf_mask) else 0.0
        voice_energy = float(np.mean(mag2[self._voice_mask])) if np.any(self._voice_mask) else 1e-9
        hf_ratio = hf_energy / (voice_energy + 1e-9)
        is_click = (
            self._frames_seen > 4
            and rms > self._env * 2.2
            and crest > 3.6
            and hf_ratio > 0.35
        )
        if is_click:
            att = max(0.05, 1.0 - 0.90 * self.strength)
            gain = np.where(self._hf_mask, gain * att, gain * max(0.25, att))
        else:
            self._env = 0.92 * self._env + 0.08 * rms

        cleaned = np.fft.irfft(spec * gain, n=self.frame_size).astype(np.float32)
        return cleaned * self._win

    def process_chunk(self, samples: np.ndarray) -> np.ndarray:
        """Process a 1-D float32 mono array and return same-length cleaned audio."""
        n_in = len(samples)
        if n_in == 0 or self.strength <= 1e-4:
            return samples
        self._in_buf = np.concatenate((self._in_buf, samples.astype(np.float32, copy=False)))
        produced: list[np.ndarray] = []
        while len(self._in_buf) >= self.frame_size:
            frame = self._in_buf[: self.frame_size]
            self._in_buf = self._in_buf[self.hop :]
            out_frame = self._process_frame(frame)
            chunk = self._overlap + out_frame[: self.hop]
            self._overlap = out_frame[self.hop :].copy()
            produced.append(chunk)
        if produced:
            self._out_buf = np.concatenate((self._out_buf, *produced))
        if len(self._out_buf) >= n_in:
            result = self._out_buf[:n_in]
            self._out_buf = self._out_buf[n_in:]
            return np.clip(result, -1.0, 1.0)
        # Initial priming hop (< 10 ms): pad smoothly with zeros on the very first buffer.
        pad = np.zeros(n_in - len(self._out_buf), dtype=np.float32)
        result = np.concatenate((pad, self._out_buf))
        self._out_buf = np.zeros(0, dtype=np.float32)
        return np.clip(result, -1.0, 1.0)


def denoise_elements(prefix: str = "mic_transient", strength: float = 0.75) -> list[Gst.Element]:
    """GStreamer elements running :class:`TransientSpectralDenoiser` in-place."""
    conv = make("audioconvert", f"{prefix}_conv")
    res = make("audioresample", f"{prefix}_resample")
    caps = caps_filter(
        "audio/x-raw,format=F32LE,layout=interleaved,rate=48000,channels=1",
        f"{prefix}_caps",
    )
    stage = make("identity", f"{prefix}_stage")
    denoiser = TransientSpectralDenoiser(sample_rate=48000, strength=strength)

    def on_buffer(_pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        ok, mapped = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        try:
            n_samples = len(mapped.data) // 4
            if n_samples <= 0:
                return Gst.PadProbeReturn.OK
            arr = np.frombuffer(mapped.data, dtype=np.float32, count=n_samples).copy()
        finally:
            buf.unmap(mapped)
        cleaned = denoiser.process_chunk(arr)
        payload = cleaned.tobytes()
        if buf.mini_object.is_writable() and buf.is_all_memory_writable():
            buf.fill(0, payload)
        else:
            setter = getattr(info, "set_buffer", None)
            if setter is not None:
                out = buf.copy_deep()
                out.fill(0, payload)
                setter(out)
        return Gst.PadProbeReturn.OK

    src_pad = stage.get_static_pad("src")
    if src_pad is not None:
        src_pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
    return [conv, res, caps, stage]
