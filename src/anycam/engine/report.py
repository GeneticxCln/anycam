"""What an engine run reports."""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import config


@dataclass
class EngineReport:
    started: float = 0.0
    stopped: float = 0.0
    frames: int = 0
    fps: float = 0.0
    expected_frames: float = 0.0
    dropped_frames: float = 0.0
    dropped_pct: float = 0.0
    error: str = ""
    vcam_error: str = ""
    media_error: str = ""
    media_errors: list[str] = field(default_factory=list)
    camera_mode: str = ""
    camera_modes: list[str] = field(default_factory=list)
    instant_fps: float = 0.0
    source_health: dict[str, dict] = field(default_factory=dict)
    stalled_sources: list[str] = field(default_factory=list)
    camera_probe_error: str = ""
    camera_placeholders: list[str] = field(default_factory=list)
    source_errors: list[str] = field(default_factory=list)
    mic_level_db: float | None = None
    encoder: str = ""
    video_codec: str = ""
    encoder_note: str = ""
    vcam: str = ""
    vcam_active: bool = False
    screen_error: str = ""
    rtmp_error: str = ""
    rtmp_encoder: str = ""
    rtmp_encoder_note: str = ""
    record_path: str = ""
    record_bytes: int = 0
    record_ok: bool = False
    rtmp_url: str = ""
    audio_inputs: list[str] = field(default_factory=list)
    # What the microphone passes through before the mixer ("" = nothing).
    mic_processing: str = ""
    # Audio outputs: the monitor the streamer listens on and the virtual
    # microphone node; "" is fine, otherwise why one could not be made.
    audio_monitor: str = ""
    audio_monitor_error: str = ""
    virtual_mic: str = ""
    virtual_mic_error: str = ""
    samples: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "duration_s": round(max(0.0, self.stopped - self.started), 3),
            "frames": self.frames,
            "fps": round(self.fps, 3),
            "expected_frames": round(self.expected_frames, 1),
            "dropped_frames": round(self.dropped_frames, 1),
            "dropped_pct": round(self.dropped_pct, 3),
            "error": self.error,
            "vcam_error": self.vcam_error,
            "media_error": self.media_error,
            "media_errors": self.media_errors,
            "camera_mode": self.camera_mode,
            "camera_modes": self.camera_modes,
            "instant_fps": round(self.instant_fps, 3),
            "source_health": self.source_health,
            "stalled_sources": self.stalled_sources,
            "camera_probe_error": self.camera_probe_error,
            "camera_placeholders": self.camera_placeholders,
            "source_errors": self.source_errors,
            "mic_level_db": self.mic_level_db,
            "encoder": self.encoder,
            "video_codec": self.video_codec,
            "encoder_note": self.encoder_note,
            "vcam": self.vcam,
            "vcam_active": self.vcam_active,
            "screen_error": self.screen_error,
            "rtmp_error": config.redact_text(self.rtmp_error, self.rtmp_url),
            "rtmp_encoder": self.rtmp_encoder,
            "rtmp_encoder_note": self.rtmp_encoder_note,
            "record_path": self.record_path,
            "record_bytes": self.record_bytes,
            "record_ok": self.record_ok,
            # The URL carries the stream key; reports are shared, so mask it.
            "rtmp_url": config.redact_url(self.rtmp_url),
            "audio_inputs": self.audio_inputs,
            "mic_processing": self.mic_processing,
            "audio_monitor": self.audio_monitor,
            "audio_monitor_error": self.audio_monitor_error,
            "virtual_mic": self.virtual_mic,
            "virtual_mic_error": self.virtual_mic_error,
            "samples": self.samples,
        }
