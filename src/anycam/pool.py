"""Compatibility shim: the capture pool lives in ``anycam.engine.source_pool``."""
from .engine.source_pool import *  # noqa: F401,F403
from .engine.source_pool import (  # noqa: F401
    CAPTURE_KINDS, RETAIN_GRACE_S, SCREEN_IDLE_GRACE_S, CaptureSpec, Entry,
    SourcePool, capture_spec,
)
