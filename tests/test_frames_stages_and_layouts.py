"""Regression tests for the second 2026-11-07 audit (18 findings)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from anycam import config, overlay, procedural, sources  # noqa: E402
from anycam.model import (  # noqa: E402
    Effects, Layer, Project, Scene, empty_project, new_id, save_scene_preset,
)
from anycam.pool import SourcePool, CaptureSpec  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance
    instance.processEvents()


def _many(project, app):
    from anycam.gui_many import ManyCamWindow

    window = ManyCamWindow(project)
    window.headless = True
    window._persist = False
    app.processEvents()
    return window


def _close(window, app):
    window._persist = False
    window.close()
    app.processEvents()


def _caps_of(elements) -> list[str]:
    return [element.get_property("caps").to_string() for element in elements
            if element.get_factory().get_name() == "capsfilter"]


# 1 ---------------------------------------------------------------- frames
@pytest.mark.parametrize("style", sorted(overlay.WINDOW_FRAME_STYLES))
def test_every_frame_style_renders_at_any_size_and_thickness(style):
    for width, height in ((8, 8), (24, 14), (64, 64), (160, 90), (1920, 1080)):
        for thickness in (1, 24, 48):
            path = overlay.render_window_frame(width, height, style, thickness=thickness)
            assert Path(path).exists()


def test_a_frame_that_cannot_be_drawn_never_aborts_the_chain(monkeypatch):
    def broken(*_args, **_kwargs):
        if _args[2] != "none":
            raise ValueError("y1 must be greater than or equal to y0")
        return real(*_args, **_kwargs)

    real = overlay.render_window_frame
    monkeypatch.setattr(overlay, "render_window_frame", broken)
    layer = Layer("l", "test", "T", effects=Effects(frame_style="polaroid", frame_width=48))
    chain = sources.test_chain(64, 64, 30, layer)          # must not raise
    assert chain.effects["frame"].get_property("alpha") == 0.0


# 2 ---------------------------------------------------------- layout keys
def test_an_unknown_layout_key_opens_as_the_single_layout(app):
    project = empty_project()
    project.scenes[0].layout = "layout-from-a-newer-build"
    window = _many(project, app)
    try:
        assert window._current_layout_key == "full"
        assert window.layout_buttons["full"].isChecked()
    finally:
        _close(window, app)


# 3/4 ------------------------------------------------------ square pixels
def test_network_and_unsized_screen_captures_are_letterboxed_not_stretched():
    network = SourcePool._network(CaptureSpec("network", "rtsp://cam/x", 1920, 1080, 30))
    assert "pixel-aspect-ratio=(fraction)1/1" in _caps_of(network.elements)[-1]
    if Gst.ElementFactory.find("pipewiresrc") is not None:
        screen = SourcePool._screen(CaptureSpec("screen", "42", 1920, 1080, 30))
        assert "pixel-aspect-ratio=(fraction)1/1" in _caps_of(screen.elements)[-1]


def test_a_4_3_stream_scaled_to_the_canvas_keeps_its_shape():
    pipeline = Gst.parse_launch(
        "videotestsrc num-buffers=1 pattern=white ! video/x-raw,width=640,height=480 ! "
        "videoconvert ! videoscale ! "
        "video/x-raw,format=RGBA,width=192,height=108,pixel-aspect-ratio=1/1 ! "
        "appsink name=out")
    sink = pipeline.get_by_name("out")
    pipeline.set_state(Gst.State.PLAYING)
    sample = sink.emit("try-pull-sample", 5 * Gst.SECOND)
    pipeline.set_state(Gst.State.NULL)
    buffer = sample.get_buffer()
    ok, info = buffer.map(Gst.MapFlags.READ)
    data = bytes(info.data)
    buffer.unmap(info)
    row = 54 * 192 * 4
    assert data[row + 4 * 4] < 40, "black bar on the left (letterbox), not stretched white"
    assert data[row + 96 * 4] > 200, "the picture in the middle"


def test_network_chain_crops_the_normalised_frame():
    layer = Layer("n", "network", "N", crop=(0.1, 0.0, 0.0, 0.0))
    chain = sources.network_chain("rtsp://cam/x", 640, 360, 30, layer, source_size=(1280, 720))
    names = [element.get_factory().get_name() for element in chain.elements]
    caps = _caps_of(chain.elements)
    assert any("width=(int)1280" in text and "pixel-aspect-ratio=(fraction)1/1" in text
               for text in caps)
    assert names.index("videoscale") < names.index("videocrop"), "normalise, then crop"


# 5 ------------------------------------------------------ stage effects
def test_a_stage_keeps_its_effect_rack_when_it_has_effects(tmp_path):
    png = overlay.render_background("Studio", 160, 90)
    plain = Layer("s", "image", "Stage", source=str(png), role="backdrop")
    lean = sources.image_chain(str(png), 160, 90, 30, plain, fit=False)
    assert not lean.effects, "a default stage takes the fast path"
    styled = Layer("s", "image", "Stage", source=str(png), role="backdrop",
                   effects=Effects(brightness=0.3))
    full = sources.image_chain(str(png), 160, 90, 30, styled, fit=False)
    assert "balance" in full.effects, "effects on a stage are not silently dropped"


def test_clicks_never_pick_the_stage(app):
    from PySide6.QtGui import QImage

    from anycam.gui import PreviewWidget

    scene = Scene("s", "S")
    scene.add_layer(Layer("bg", "image", "Stage", box=(0, 0, 1, 1), z=0, role="backdrop"))
    scene.add_layer(Layer("logo", "image", "Logo", box=(0.0, 0.0, 0.2, 0.2), z=1))
    preview = PreviewWidget()
    preview.resize(320, 180)
    image = QImage(320, 180, QImage.Format.Format_RGBA8888)
    image.fill(0)
    preview.set_image(image)
    preview.scene = scene
    assert not preview.canvas_rect().isEmpty()
    logo = scene.layer("logo")
    assert preview.layer_at(preview.box_rect(logo).center()) is logo, "a real layer is picked"
    assert preview.layer_at(preview.canvas_rect().center()) is None, "the stage is not"


def test_an_effect_the_stage_cannot_apply_live_rebuilds(app):
    window = _many(empty_project(), app)
    try:
        stage = Layer("bg", "image", "Stage", box=(0, 0, 1, 1), role="backdrop",
                      effects=Effects(brightness=0.4))
        restarts = []
        window.engine = SimpleNamespace(set_layer_effects=lambda *_: False,
                                        _layer_chains={"bg": object()})
        window.restart_engine = lambda: restarts.append(True)
        window._apply_effects(stage)
        assert restarts == [True]
        animation = Layer("an", "animation", "Anim", box=(0, 0, 1, 1))
        window.engine._layer_chains["an"] = object()
        window._apply_effects(animation)
        assert restarts == [True], "no rebuild loop for layers that never have a rack"
    finally:
        window.engine = None
        _close(window, app)


# 6 ---------------------------------------------------------- slot menu
def test_right_click_on_a_plus_shows_the_slot_menu_under_particles(app):
    from PySide6.QtGui import QImage

    window = _many(empty_project(), app)
    try:
        window.preview.resize(640, 360)
        image = QImage(640, 360, QImage.Format.Format_RGBA8888)
        image.fill(0)
        window.preview.set_image(image)
        window.apply_layout("grid-2x2")
        window.scene.add_layer(Layer("fx", "particles", "Snow", source="snow",
                                     box=(0, 0, 1, 1), z=9))
        window._refresh_stage_slots()
        window._position_stage_slots(force=True)
        button = window.stage_add_buttons[0]
        opened, forwarded = [], []
        window._slot_menu = lambda index, btn: opened.append(index)
        window._preview_context_menu = lambda layer_id, pos: forwarded.append(layer_id)
        assert window.preview.layer_at(window.preview.canvas_rect().center()).kind == "particles"
        window._slot_context_menu(button, QPoint(5, 5))
        assert opened and not forwarded, "the slot menu, not the particles layer's menu"
    finally:
        _close(window, app)


# 7 ------------------------------------------------------------- insets
def test_insetting_twice_still_restores_the_original_box(app):
    window = _many(empty_project(), app)
    try:
        layer = window.add_layer("test", box=(0.0, 0.0, 1.0, 1.0))
        window._inset_capture_layers()
        window._inset_capture_layers()
        assert layer.box == pytest.approx((0.055, 0.055, 0.89, 0.89))
        window._restore_inset_layers()
        assert layer.box == (0.0, 0.0, 1.0, 1.0)
    finally:
        _close(window, app)


# 8/9 ------------------------------------------------------ camera errors
def test_an_error_about_video10_does_not_blame_the_camera_on_video1(app):
    window = _many(empty_project(), app)
    try:
        window.scene.add_layer(Layer("cam", "camera", "Cam", source="/dev/video1"))
        assert window._camera_error_layers(
            "Could not write to resource /dev/video10 (v4l2sink)") == {}
        assert window._camera_error_layers(
            "Device '/dev/video1' is busy") == {"cam": "/dev/video1"}
    finally:
        _close(window, app)


def test_an_unrelated_error_is_not_a_camera_disconnect(app):
    window = _many(empty_project(), app)
    try:
        window.scene.add_layer(Layer("cam", "camera", "Cam", source="/dev/anycam-gone"))
        stopped, rebuilt = [], []
        window.stop_engine = lambda: stopped.append(True)
        window._rebuild_without_lost_cameras = lambda message="": rebuilt.append(message)
        window._handle_pipeline_error("nvh264enc0: could not encode frame")
        assert stopped and not rebuilt and window._lost_cameras == {}
        window._handle_pipeline_error("Error reading from device: No such device")
        assert rebuilt and window._lost_cameras == {"cam": "/dev/anycam-gone"}
    finally:
        _close(window, app)


# 10 ----------------------------------------------------- vcam GPU leftovers
def test_a_failed_gpu_attempt_does_not_leave_the_vcam_dead(monkeypatch):
    from anycam.engine.outputs import vcam as vcam_module

    output = vcam_module.VcamOutput()
    monkeypatch.setattr(vcam_module.config, "vcam_available", lambda device: True)
    monkeypatch.setattr(vcam_module.config, "vcam_pixel_format", lambda device: "YUY2")
    monkeypatch.setattr(output, "_gl_available", lambda: True)
    monkeypatch.setattr(output, "_start_idle_feed", lambda: None)
    attempts = []

    def build(use_gl):
        attempts.append(use_gl)
        if use_gl:
            output.error = "virtual camera pipeline failed: could not link glupload"
            return False
        return True

    monkeypatch.setattr(output, "_build_pipeline", build)
    assert output.start("/dev/video10", 320, 180, 30)
    assert attempts == [True, False]
    assert output.error == "", "the CPU chain works; frames must not be dropped"


# 11 --------------------------------------------------------- scene presets
def test_scene_presets_are_private_and_written_atomically(tmp_path):
    scene = Scene("s", "Studio cam")
    scene.add_layer(Layer("n", "network", "Cam", source="rtsp://user:secret@cam/live"))
    path = save_scene_preset(scene, tmp_path / "presets")
    assert (path.stat().st_mode & 0o777) == 0o600
    assert json.loads(path.read_text())["layers"][0]["source"].startswith("rtsp://")
    assert not list((tmp_path / "presets").glob("*.tmp"))


# 12 ------------------------------------------------- concurrent phone offers
def test_two_offers_from_one_phone_leave_one_receiver(monkeypatch, tmp_path):
    from anycam.phone import server as phone_server
    from anycam.phone import webrtc

    made = []
    gate = threading.Barrier(2)

    class FakeReceiver:
        def __init__(self, device, on_event=None):
            self.stopped = False
            made.append(self)

        def answer(self, offer):
            gate.wait(5)            # both offers are past _drop() together
            return "v=0\r\n"

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(webrtc, "PhoneReceiver", FakeReceiver)
    service = phone_server.PhoneService(state_dir=tmp_path, cert_dir=tmp_path)
    threads = [threading.Thread(target=service.start_stream, args=("phone-a", "m=video"))
               for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    running = [receiver for receiver in made if not receiver.stopped]
    assert len(made) == 2 and len(running) == 1
    assert service.receivers["phone-a"] is running[0]
    assert list(service._sessions.values()) == ["phone-a"]


# 13 ------------------------------------------------- crashing generators
def test_a_crashing_generator_keeps_the_picture_alive_and_says_so(capfd):
    import numpy as np

    class Flaky:
        calls = 0

        def frame(self, _t):
            Flaky.calls += 1
            if Flaky.calls > 2:
                raise RuntimeError("boom")
            return np.full((8, 8, 4), 200, np.uint8)

    source = procedural.ProceduralSource("flaky", "flaky_src", 8, 8, 30,
                                         generator=Flaky(), size=(8, 8))
    pipeline = Gst.Pipeline.new()
    sink = Gst.ElementFactory.make("appsink")
    sink.set_property("sync", False)
    sink.set_property("drop", True)
    sink.set_property("max-buffers", 1)
    pipeline.add(source.appsrc)
    pipeline.add(sink)
    source.appsrc.link(sink)
    pipeline.set_state(Gst.State.PLAYING)
    try:
        time.sleep(0.6)
        assert source._thread.is_alive(), "the feeding thread survived the error"
        assert sink.emit("try-pull-sample", Gst.SECOND) is not None
    finally:
        source.stop()
        pipeline.set_state(Gst.State.NULL)
    assert "frame failed" in capfd.readouterr().err


# 14 ------------------------------------------------------------- reload
def test_reload_refreshes_the_format_and_forgets_old_state(app):
    window = _many(empty_project(), app)
    try:
        project = empty_project()
        project.output.canvas_w, project.output.canvas_h = 1280, 720
        project.audio.mic_denoise = "high"
        config.project_path().parent.mkdir(parents=True, exist_ok=True)
        project.save(config.project_path())
        window._backdrop_insets["gone"] = ((0, 0, 1, 1), (0.1, 0.1, 0.8, 0.8))
        window._lost_cameras["gone"] = "/dev/video9"
        window.reload_project()
        assert window.preview.canvas_size == (1280, 720)
        assert window._backdrop_insets == {} and window._lost_cameras == {}
        assert window.denoise_combo.currentData() == "high"
        sizes = getattr(window, "_format_sizes", None)
        if sizes is not None:
            assert sizes[window.resolution_combo.currentIndex()] == (1280, 720)
    finally:
        _close(window, app)


# 15 --------------------------------------------------- screen session leak
def test_removing_a_scene_ends_its_screen_capture(app):
    project = empty_project()
    other = Scene(new_id("scene"), "Share")
    other.add_layer(Layer("scr", "screen", "Screen", source="42"))
    project.scenes.append(other)
    window = _many(project, app)
    try:
        closed = []
        window._screen_sessions["scr"] = SimpleNamespace(close=lambda: closed.append(True))
        window.remove_scene(other.id)
        assert closed == [True] and "scr" not in window._screen_sessions
    finally:
        _close(window, app)


# 16 ---------------------------------------------------------- stage cache
def test_a_changed_painter_is_not_served_from_an_old_cache(monkeypatch):
    first = overlay.render_background("Blueprint", 160, 90)
    monkeypatch.setattr(overlay, "_stages_revision", lambda: "changed1")
    second = overlay.render_background("Blueprint", 160, 90)
    assert first != second and Path(second).exists()


# 17 ------------------------------------------------- per-device caps
def test_exclusive_caps_is_read_for_anycams_own_device(tmp_path):
    params = tmp_path / "parameters"
    params.mkdir()
    (params / "exclusive_caps").write_text("N,Y,N\n")
    (params / "video_nr").write_text("3,10,-1\n")
    path = str(params / "exclusive_caps")
    assert config.vcam_exclusive_caps(path, "/dev/video10") is True
    assert config.vcam_exclusive_caps(path, "/dev/video3") is False
    assert config.vcam_exclusive_caps(path, "/dev/video42") is False, "unlisted: first value"


# 18 ------------------------------------------------------- package data
def test_every_package_data_pattern_matches_a_file():
    import glob
    import tomllib

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text())
    for pattern in data["tool"]["setuptools"]["package-data"]["anycam"]:
        assert glob.glob(str(root / "src" / "anycam" / pattern)), f"{pattern} matches nothing"
