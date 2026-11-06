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
from anycam.pool import CaptureSpec, Entry, SourcePool, capture_spec  # noqa: E402
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


def test_screen_capture_uploads_dmabuf_frames():
    """niri's DMA-BUF-only stream needs GL upload; videoconvert alone fails."""
    if Gst.ElementFactory.find("pipewiresrc") is None:
        pytest.skip("pipewiresrc unavailable")
    spec = CaptureSpec("screen", "86", 320, 180, 30, expected=(320, 180))
    built = SourcePool._screen(spec)
    source = built.elements[0]
    names = [element.get_factory().get_name() for element in built.elements]
    assert names[0] == "pipewiresrc"
    # One slot, leaking downstream: the newest frame always wins and stale
    # frames are dropped instead of back-pressuring the screencast (which would
    # empty the producer's pool after a focus stall and stop the capture).
    assert names[1] == "queue"
    queue = built.elements[1]
    assert queue.get_property("leaky") == 2
    assert queue.get_property("max-size-buffers") == 1
    if all(Gst.ElementFactory.find(name) is not None
           for name in ("glupload", "glcolorconvert", "gldownload")):
        assert names[2:5] == ["glupload", "glcolorconvert", "gldownload"]
        assert source.get_property("always-copy") is False
    else:
        assert source.get_property("always-copy") is True
    assert "videoflip" in names
    assert "videoscale" in names
    assert "videorate" not in names
    # The format is pinned right after videoconvert, before any consumer can
    # read the buffer with the wrong stride and paint it black.
    assert names.index("capsfilter") == names.index("videoconvert") + 1
    caps = next(element for element in built.elements
                if element.get_factory().get_name() == "capsfilter")
    assert caps.get_property("caps").get_structure(0).get_value("format") == "RGBA"
    all_caps = [element.get_property("caps").to_string()
                for element in built.elements
                if element.get_factory().get_name() == "capsfilter"]
    assert all("framerate" not in text for text in all_caps)


def test_invalid_portal_size_is_scaled_to_the_canvas():
    """A 1x1 portal size used to become a not-negotiated pipewiresrc."""
    layer = Layer("screen_layer", "screen", "Screen 1x1", source="86")
    spec = capture_spec(layer, 1920, 1080, 60, screen_size=(1, 1))
    # Unknown, not the canvas: _screen still scales to the canvas, but the
    # size probe must stay off so a real 3831x2100 window is not reported as
    # a "portal stream" mismatch.
    assert spec.expected is None
    built = SourcePool._screen(spec)
    assert built.size == (1920, 1080)
    assert built.check_size is None


def test_released_screen_capture_survives_a_canvas_rebuild(monkeypatch):
    """A rebuild must reuse the running screencast, not re-open a dark one.

    KWin's window share is damage-driven: a fresh pipewiresrc has no frame
    until the window repaints, so the rebuilt canvas went black.  The retained
    intervideosink hands its last frame to the new canvas instead.
    """

    class FakePipeline:
        def __init__(self):
            self.states = []

        def set_state(self, state):
            self.states.append(state)
            return True

    pool = SourcePool()
    events: list[str] = []
    pool.log_event = events.append
    spec = CaptureSpec("screen", "86", 320, 180, 30, expected=(320, 180))
    pipeline = FakePipeline()
    entry = Entry(spec=spec, channel="probe", pipeline=pipeline)
    monkeypatch.setattr(pool, "_build", lambda _spec: entry)

    first = pool.acquire(spec)
    pool.release(spec)
    assert pool.entry(spec) is first and first.refs == 0
    assert any("kept alive" in line for line in events), events

    second = pool.acquire(spec)
    assert second is first, "the rebuild re-opened the portal capture"
    assert first.refs == 1
    assert any("reused after a rebuild" in line for line in events), events

    pool.release(spec)
    entry.idle_since -= 10.0
    pool.sweep()
    assert pool.entry(spec) is None
    assert pipeline.states, "the idle capture was never torn down"
    assert any("idle screen capture released" in line for line in events), events


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


def test_removed_source_is_reattached_to_its_running_capture():
    """Removing then re-adding a layer must not reopen its capture."""
    project = two_patterns()
    engine = AnyCamEngine(project, preview=True).build()
    try:
        engine.start()
        pipeline = engine.pipeline
        spec = engine._layer_specs["right"]
        capture = engine.pool.entry(spec)
        assert capture is not None

        engine.detach_layer("right")
        project.scene.remove_layer("right")
        assert engine.pool.entry(spec) is capture
        assert capture.refs == 0 and capture.retained

        added = Layer("right2", "test", "Right again", source="blue",
                      box=(0.5, 0.0, 0.5, 1.0), z=2)
        project.scene.add_layer(added)
        assert engine.add_layer(added) is True
        assert engine.pipeline is pipeline, "re-add rebuilt the canvas"
        assert engine.pool.entry(engine._layer_specs[added.id]) is capture
        assert capture.refs == 1 and not capture.retained

        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            engine.pump(50)
        assert added.id in engine._layer_pads
    finally:
        engine.stop()


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
