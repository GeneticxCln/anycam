"""M1 slice 2: capture pipelines behind intervideosrc, ref-counted per source.

The point of the split is that a capture lives outside the canvas graph: two
layers on one device share it, the last release gives the hardware back, and a
device that dies cannot stop the compositor.
"""
from __future__ import annotations

import time

import pytest

pytest.importorskip("gi")

from anycam.model import Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402
from anycam.pool import CaptureSpec, SourcePool  # noqa: E402
from helpers.frames import grab_frame  # noqa: E402

HAS_INTER = False
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    HAS_INTER = bool(
        Gst.ElementFactory.find("intervideosrc")
        and Gst.ElementFactory.find("intervideosink")
        and Gst.ElementFactory.find("compositor")
    )
except Exception:  # pragma: no cover - platform without GStreamer
    HAS_INTER = False

pytestmark = pytest.mark.skipif(not HAS_INTER, reason="inter elements unavailable")


def two_patterns() -> Project:
    project = Project(name="pool")
    project.output = OutputConfig(canvas_w=320, canvas_h=180, fps=30)
    scene = Scene("scene_pool", "Pool")
    scene.add_layer(Layer("left", "test", "Left", source="red",
                          box=(0.0, 0.0, 0.5, 1.0), z=1))
    scene.add_layer(Layer("right", "test", "Right", source="blue",
                          box=(0.5, 0.0, 0.5, 1.0), z=2))
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def test_layers_on_the_same_source_share_one_capture():
    pool = SourcePool()
    spec = CaptureSpec("test", "ball", 320, 180, 30)
    first = pool.acquire(spec)
    second = pool.acquire(spec)
    assert first is second
    assert first.refs == 2
    assert len(pool.entries()) == 1

    pool.release(spec)
    assert pool.entry(spec) is first and first.refs == 1

    pool.release(spec)
    assert pool.entry(spec) is None
    assert pool.entries() == []


def test_canvas_renders_through_the_pool():
    """Both layers arrive through intervideosrc with their own pattern."""
    frame = grab_frame(two_patterns(), timeout=8.0)
    left = frame.average(40, 60, 120, 120)
    right = frame.average(200, 60, 280, 120)
    assert left[0] > 200 and left[1] < 60 and left[2] < 60, left
    assert right[2] > 200 and right[0] < 60, right


def test_a_dead_capture_does_not_stop_the_canvas():
    engine = AnyCamEngine(two_patterns(), preview=True).build()
    try:
        engine.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            engine.pump(50)
        before = engine._frame_count
        assert before > 10, f"canvas never started ({before} frames)"

        # The device dies: the capture pipeline stops without sending EOS.
        engine.pool.entries()[0].pipeline.set_state(Gst.State.NULL)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            engine.pump(50)

        assert engine._frame_count - before > 10, "canvas stalled with a dead capture"
        assert engine.report.error == "", engine.report.error
    finally:
        engine.stop()


def test_stop_releases_every_capture():
    engine = AnyCamEngine(two_patterns(), preview=True).build()
    assert len(engine.pool.entries()) == 2
    engine.stop()
    assert engine.pool.entries() == []


def test_health_flags_a_capture_that_stops():
    """B41: one stopped source is visible even while the canvas continues."""
    engine = AnyCamEngine(two_patterns(), preview=True).build()
    try:
        engine.start()
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            engine.pump(50)
        record = engine.sample(force=True)
        assert record is not None
        assert record["instant_fps"] > 0
        assert engine.report.source_health
        assert engine.report.stalled_sources == []

        red = next(entry for entry in engine.pool.entries()
                   if entry.spec.source == "red")
        red.pipeline.set_state(Gst.State.NULL)
        time.sleep(2.2)
        engine.sample(force=True)
        assert "left" in engine.report.stalled_sources, (
            engine.report.source_health, engine.report.stalled_sources
        )
    finally:
        engine.stop()


def test_report_lists_every_camera_mode(monkeypatch, tmp_path):
    """B48: two cameras used to leave only the last mode in the report."""
    from anycam import pool as pool_module
    from anycam.sources import CameraMode

    def modes(device):
        width = 640 if device.endswith("a") else 1280
        return [CameraMode("MJPG", width, 480, 30)]

    monkeypatch.setattr(pool_module, "camera_modes", modes)
    project = Project(name="modes")
    project.output = OutputConfig(canvas_w=320, canvas_h=180, fps=30)
    scene = Scene("scene_modes", "Modes")
    scene.add_layer(Layer("cam_a", "camera", "A", source=str(tmp_path / "cam-a"),
                          box=(0.0, 0.0, 0.5, 1.0), z=1))
    scene.add_layer(Layer("cam_b", "camera", "B", source=str(tmp_path / "cam-b"),
                          box=(0.5, 0.0, 0.5, 1.0), z=2))
    project.scenes = [scene]
    project.active_scene = scene.id
    engine = AnyCamEngine(project, preview=True).build()
    try:
        assert len(engine.report.camera_modes) == 2, engine.report.camera_modes
        assert any("640x480" in mode for mode in engine.report.camera_modes)
        assert any("1280x480" in mode for mode in engine.report.camera_modes)
    finally:
        engine.stop()


def test_hidden_capture_is_acquired_only_while_visible():
    """B15: a hidden layer must not hold a device or run a capture."""
    project = two_patterns()
    project.scene.layer("right").visible = False
    engine = AnyCamEngine(project, preview=True).build()
    try:
        assert len(engine.pool.entries()) == 1
        engine.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            engine.pump(50)
        before = engine._frame_count

        engine.set_layer_visible("right", True)
        assert len(engine.pool.entries()) == 2
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            engine.pump(50)
        assert engine._frame_count > before

        engine.set_layer_visible("right", False)
        assert len(engine.pool.entries()) == 1
        assert engine._layer_acquired == {"left"}
    finally:
        engine.stop()
