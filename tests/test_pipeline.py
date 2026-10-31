import shutil

import pytest

pytest.importorskip("gi")
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    HAS_COMPOSITOR = Gst.ElementFactory.find("compositor") is not None
except Exception:  # pragma: no cover - platform without GStreamer
    HAS_COMPOSITOR = False

from anycam.model import Layer, Project, Scene
from anycam.pipeline import AnyCamEngine, verify_mp4

pytestmark = pytest.mark.skipif(not HAS_COMPOSITOR, reason="GStreamer compositor unavailable")


def make_project(width=640, height=360, fps=30):
    project = Project(name="test")
    project.output.canvas_w = width
    project.output.canvas_h = height
    project.output.fps = fps
    scene = Scene("scene_test", "Test")
    scene.add_layer(Layer("layer_test", "test", "Pattern", box=(0, 0, 1, 1), z=1))
    scene.add_layer(Layer("layer_text", "text", "Caption", source="AnyCam",
                          box=(0.05, 0.05, 0.35, 0.12), z=10, font_size=0.06))
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def test_engine_composites_and_records(tmp_path):
    output = tmp_path / "composite.mp4"
    engine = AnyCamEngine(make_project(), record_path=output).build()
    report = engine.run(2.0)
    assert report["error"] == "", report["error"]
    assert report["frames"] > 20
    assert report["record_ok"] is True
    assert output.exists() and output.stat().st_size > 1000
    ok, info = verify_mp4(str(output), width=640, height=360, fps=30, min_seconds=1.0)
    assert ok, info
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "h264"


def test_engine_delivers_preview_frames():
    frames = []
    engine = AnyCamEngine(make_project(320, 180, 30), preview=True,
                          on_frame=frames.append).build()
    report = engine.run(1.0)
    assert report["error"] == "", report["error"]
    assert frames, "preview appsink never produced a sample"


def test_set_layer_box_moves_the_compositor_pad():
    engine = AnyCamEngine(make_project(320, 180, 30), preview=True).build()
    try:
        engine.set_layer_box("layer_test", (0.25, 0.1, 0.5, 0.4))
        pad = engine._layer_pads["layer_test"]
        assert pad.get_property("xpos") == 80
        assert pad.get_property("ypos") == 18
        assert pad.get_property("width") == 160
        assert pad.get_property("height") == 72
        assert engine.scene.layer("layer_test").box == (0.25, 0.1, 0.5, 0.4)
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_layer_opacity_and_order_are_applied_live():
    engine = AnyCamEngine(make_project(320, 180, 30), preview=True).build()
    try:
        engine.set_layer_alpha("layer_text", 0.4)
        text_pad = engine._layer_pads["layer_text"]
        assert text_pad.get_property("alpha") == pytest.approx(0.4)
        assert engine.scene.layer("layer_text").alpha == pytest.approx(0.4)

        engine.scene.layer("layer_test").z = 20
        engine.refresh_layer_order()
        assert engine._layer_pads["layer_test"].get_property("zorder") == 1
        assert text_pad.get_property("zorder") == 0
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_face_overlay_is_a_compositor_pad(tmp_path):
    from anycam import overlay

    png = overlay.render_face_frame(out=tmp_path / "face.png")
    project = make_project(320, 180, 30)
    project.scene.add_layer(Layer(
        "layer_face", "face", "Face overlay", source=str(png),
        box=(0.3, 0.2, 0.4, 0.5), z=20, visible=False,
    ))
    engine = AnyCamEngine(project, preview=True).build()
    try:
        pad = engine._layer_pads["layer_face"]
        assert pad.get_property("width") == 128
        assert pad.get_property("height") == 90
        assert pad.get_property("alpha") == 0.0
        engine.set_layer_visible("layer_face", True)
        assert pad.get_property("alpha") == 1.0
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_build_without_output_branch_is_rejected():
    with pytest.raises(RuntimeError, match="no output branch"):
        AnyCamEngine(make_project(), preview=False).build()


def test_skipped_camera_builds_a_placeholder_pad():
    project = make_project(320, 180, 30)
    project.scene.add_layer(Layer(
        "layer_lost", "camera", "Lost cam", source="/dev/anycam-missing",
        box=(0.5, 0.5, 0.5, 0.5), z=20,
    ))
    engine = AnyCamEngine(project, preview=True,
                          skip_layers={"layer_lost"}).build()
    try:
        pad = engine._layer_pads["layer_lost"]
        assert pad.get_property("width") == 160
        assert pad.get_property("height") == 90
        assert engine.report.camera_placeholders == ["Lost cam"]
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_all_skipped_cameras_still_build_placeholders():
    project = Project(name="grid")
    project.output.canvas_w = 320
    project.output.canvas_h = 180
    project.output.fps = 30
    scene = Scene("scene_grid", "Grid")
    scene.add_layer(Layer("camera_a", "camera", "Camera A",
                          source="/dev/anycam-missing-a",
                          box=(0.0, 0.0, 0.5, 1.0), z=1))
    scene.add_layer(Layer("camera_b", "camera", "Camera B",
                          source="/dev/anycam-missing-b",
                          box=(0.5, 0.0, 0.5, 1.0), z=2))
    project.scenes = [scene]
    project.active_scene = scene.id
    engine = AnyCamEngine(project, preview=True,
                          skip_layers={"camera_a", "camera_b"}).build()
    try:
        assert set(engine._layer_pads) == {"camera_a", "camera_b"}
        assert engine.report.camera_placeholders == ["Camera A", "Camera B"]
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe missing")
def test_engine_reports_dropped_frame_ratio(tmp_path):
    engine = AnyCamEngine(make_project(), record_path=tmp_path / "x.mp4").build()
    report = engine.run(1.5)
    assert report["dropped_pct"] < 10.0
    assert report["fps"] > 15.0
