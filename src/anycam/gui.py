"""AnyCam's standalone window: entry points and the names tests and ``gui_many`` import.

The implementation lives in ``anycam.ui`` (window, panels, preview, dialogs) and the
engine in ``anycam.engine``.  This module keeps ``run_gui`` / ``run_screenshot`` and
the historical import paths."""
from __future__ import annotations


import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
)

from . import config
from .model import Project

from .engine.outputs.recorder import Recorder  # noqa: F401
from .engine.outputs.vcam import VcamOutput  # noqa: F401
from .engine.source_pool import SCREEN_IDLE_GRACE_S, SourcePool  # noqa: F401
from .pipeline import AnyCamEngine, nvenc_available, recording_codec, va_present  # noqa: F401
from .ui.effect_presets import EFFECT_PRESETS, EFFECT_STYLES  # noqa: F401
from .ui.theme import (  # noqa: F401
    STYLE,
)
from .ui.layout_history import (  # noqa: F401
    LayoutHistory,
)
from .ui.preview import (  # noqa: F401
    _PreviewOverlay,
    PreviewWidget,
)
from .ui.dialogs import (  # noqa: F401
    _slider_row,
    OverlayDialog,
    CameraGridDialog,
)
from .ui.instance import (  # noqa: F401
    INSTANCE_SERVER,
    _raise_window,
    _take_instance_lock,
    _ask_instance_to_show,
    claim_gui_instance,
    serve_instance_requests,
    _register_freeze_dump,
)
from .ui.window import (  # noqa: F401
    AnyCamWindow,
)
from .ui.cameras import _CAMERA_LOSS  # noqa: F401


def run_gui(project: Project | None = None) -> int:
    _register_freeze_dump()
    app = QApplication(sys.argv[:1])
    primary, instance_server = claim_gui_instance()
    if not primary:
        return 0
    window = AnyCamWindow(project)
    if instance_server is not None:
        instance_server.setParent(window)
        window._instance_server = instance_server
        serve_instance_requests(instance_server, window)
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    # Hold the loopback node from launch: apps that remember "AnyCam" must be
    # able to open it before the preview is started.
    if window.hold_vcam_device():
        if window.project.output.vcam_enabled:
            window.feed_held_vcam()
        window.note_vcam_consumers()
        window.ensure_vcam_visible()
    code = app.exec()
    # GStreamer 1.28's v4l2src segfaults during Python/GStreamer teardown on
    # this machine (niri/Wayland + GStreamer 1.28.8). Skip cleanup and let the
    # OS reclaim resources; the app has already written everything it needs.
    os._exit(code if code is not None else 0)


def run_screenshot(out: str, seconds: float, project: Project | None = None,
                   source: str = "") -> int:
    """Render the GUI on Qt's offscreen platform and save a PNG."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if project is None and source:
        from .demo import build_demo_project
        project = build_demo_project(source, config.CANVAS_W, config.CANVAS_H, config.CANVAS_FPS)
    app = QApplication(sys.argv[:1])
    window = AnyCamWindow(project)
    window.headless = True
    window.auto_screenshot_path = out
    window._render_seconds = max(0.1, seconds)
    window._screenshot_done = False
    window.resize(1440, 900)
    app.aboutToQuit.connect(window.shutdown)
    # Independent of the engine: a failed pipeline must still produce an exit,
    # and the PNG is still useful as a diagnostic of the stopped UI.
    QTimer.singleShot(int((window._render_seconds + 5.0) * 1000), window._screenshot_deadline)
    window.show()
    window.start_engine()
    app.exec()
    window.shutdown()
    status = 0 if (window.screenshot_ok and Path(out).exists() and Path(out).stat().st_size > 0) else 1
    # Same teardown guard as run_gui: GStreamer 1.28 can segfault during
    # Python/GStreamer interpreter shutdown, after the work is complete.
    os._exit(status)

