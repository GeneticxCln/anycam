"""The AnyCam GStreamer engine: sources -> compositor -> preview/vcam/record.

This module is a facade: the implementation lives in ``anycam.engine`` (controller,
layers, canvas, audio, health, encoders and the output taps).  Existing imports
(``from anycam.pipeline import AnyCamEngine``) keep working.
"""
from __future__ import annotations

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402,F401

from .engine.controller import AnyCamEngine  # noqa: F401
from .engine.encoders import (  # noqa: F401
    ENCODER_FACTORIES,
    ENCODER_PARSERS,
    ENCODER_BITRATES,
    ENCODER_LABELS,
    _NVENC_TTL_S,
    _nvenc_seen,
    encoder_bitrate,
    nvenc_available,
    _probe_nvenc,
    _va_encodes,
    va_factory,
    va_present,
    EncoderPlan,
    _requested_codec,
    _wants_va,
    encoder_plan,
    recording_codec,
    EncodedFeed,
    force_keyframe,
)
from .engine.report import (  # noqa: F401
    EngineReport,
)
from .engine.audio_helpers import (  # noqa: F401
    MIC_DENOISE_LEVELS,
    mic_dsp_wanted,
    describe_mic_dsp,
    mic_dsp_elements,
)
from .engine.canvas_helpers import (  # noqa: F401
    GL_MIXER_LATENCY_NS,
    canvas_requested,
    gl_canvas_problem,
    _empty_drawing,
)
from .engine.verify import (  # noqa: F401
    ffprobe,
    _count_packets,
    _parse_frame_rate,
    verify_mp4,
)
from .sources import build_chain, make  # noqa: F401
from .engine.source_pool import CAPTURE_KINDS, CaptureSpec, SourcePool, capture_spec  # noqa: F401

__all__ = [name for name in dir() if not name.startswith('__')]
