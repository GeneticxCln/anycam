"""M1 slice 1: outputs live in their own pipeline behind a leaky bridge.

The failure mode this pins down is audit B9/B11: a server that accepts a TCP
connection and then never speaks RTMP used to block ``rtmpsink``, which reached
the tee and froze the preview, the recorder and the virtual camera.
"""
from __future__ import annotations

import socket
import time

import pytest

pytest.importorskip("gi")

from anycam.model import Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

HAS_COMPOSITOR = False
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    HAS_COMPOSITOR = Gst.ElementFactory.find("compositor") is not None
    HAS_RTMP = Gst.ElementFactory.find("rtmpsink") is not None
except Exception:  # pragma: no cover - platform without GStreamer
    HAS_RTMP = False

pytestmark = pytest.mark.skipif(
    not (HAS_COMPOSITOR and HAS_RTMP),
    reason="GStreamer compositor or rtmpsink unavailable",
)


def make_project() -> Project:
    project = Project(name="rtmp")
    project.output = OutputConfig(canvas_w=320, canvas_h=180, fps=30)
    scene = Scene("scene_rtmp", "RTMP")
    scene.add_layer(Layer("layer_test", "test", "Pattern", source="ball",
                          box=(0.0, 0.0, 1.0, 1.0), z=1))
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def test_unresponsive_rtmp_cannot_stall_the_canvas():
    """The canvas must keep producing while the publish pipeline is stuck."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    frames = []
    engine = AnyCamEngine(
        make_project(),
        preview=True,
        rtmp=f"rtmp://127.0.0.1:{port}/live/secret",
        on_frame=lambda sample: frames.append(1),
    ).build()
    try:
        engine.start()
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            engine.pump(50)
        started = time.monotonic()
        engine.stop()
        stop_seconds = time.monotonic() - started
    finally:
        server.close()

    # Before the split the tee blocked after a handful of frames.
    assert len(frames) > 30, f"canvas stalled at {len(frames)} frames"
    assert engine.report.error == "", engine.report.error
    assert stop_seconds < 2.5, f"stop() took {stop_seconds:.2f}s"
    # The stream key must not leak into the report even when the output fails.
    summary = engine.report.as_dict()
    assert "secret" not in summary["rtmp_url"]
