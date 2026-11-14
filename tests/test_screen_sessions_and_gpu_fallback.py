"""Fixes for six bugs found in review (2026-11-08)."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam import pipeline, portal  # noqa: E402
from anycam.model import Layer  # noqa: E402

Gst.init(None)


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _window(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from anycam.demo import build_demo_project
    from anycam.gui import AnyCamWindow

    window = AnyCamWindow(build_demo_project("testsrc", 320, 180, 30))
    window.headless = True
    return window


# ------------------------------------------- 1. a removed scene's screen share
def test_a_closed_screen_session_no_longer_looks_reusable():
    session = portal.ScreenSession("/session/1", -1,
                                   [portal.ScreenStream(42, 1920, 1080, 1)])
    session.close()
    assert session.session_handle == "" and session.streams == []
    with pytest.raises(portal.PortalError):
        session.take_remote()


def test_closing_the_last_screen_session_forgets_it(app, monkeypatch, tmp_path):
    window = _window(app, monkeypatch, tmp_path)
    try:
        session = portal.ScreenSession("/session/1", -1,
                                       [portal.ScreenStream(42, 1920, 1080, 1)])
        window._screen_sessions["l_screen"] = session
        window._last_screen_session = session
        window._close_screen_session("l_screen")
        assert window._last_screen_session is None
    finally:
        window.close()


def test_a_screen_layer_whose_capture_cannot_open_is_taken_out(app, monkeypatch, tmp_path):
    """Left in the scene, every later start failed on it."""
    window = _window(app, monkeypatch, tmp_path)

    class DeadSession:
        streams = [portal.ScreenStream(42, 1920, 1080, 1)]

        def take_remote(self):
            raise portal.PortalError("screen-cast session is no longer available")

    class Engine:
        def can_reattach_screen(self, layer):
            return False

        def add_layer(self, *a, **k):
            pytest.fail("a layer without a capture must not reach the canvas")

    dead = DeadSession()
    layer = Layer("l_screen", "screen", "Screen", source="42")
    try:
        window.engine = Engine()
        window._screen_sessions[layer.id] = dead
        window._last_screen_session = dead
        window._insert_layer(layer)
        assert window.scene.layer(layer.id) is None
        assert layer.id not in window._screen_sessions
        assert window._last_screen_session is None
    finally:
        window.engine = None
        window.close()


# ------------------------------- 2. the CPU fallback also covers build failures
def test_a_gpu_canvas_that_fails_while_building_falls_back(app, monkeypatch, tmp_path):
    monkeypatch.delenv("ANYCAM_CANVAS", raising=False)
    window = _window(app, monkeypatch, tmp_path)
    from anycam import gui

    real = gui.AnyCamEngine
    asked = []

    class BreaksWhileBuilding(real):
        def build(self):
            asked.append(self.canvas)
            if self.canvas == "gpu":
                self.canvas_backend = "gpu"
                raise RuntimeError("could not upload a layer to the GPU canvas")
            return super().build()

    monkeypatch.setattr("anycam.ui.engine_control.AnyCamEngine", BreaksWhileBuilding)
    try:
        window.start_engine()
        assert asked == ["gpu", "cpu"]
        assert window.engine is not None and window.engine.canvas_backend == "cpu"
    finally:
        window.shutdown()
        window.close()


# ---------------------------- 3. a rebuild while hidden keeps the GPU preview
class HiddenView:
    """A GPU view in a window hidden to the tray."""

    def __init__(self):
        self.deleted = False
        self.root = type("Root", (), {"setProperty": lambda *a: None})()

    def isVisible(self):
        return False

    def rootObject(self):
        return self.root

    def lower(self):
        pass

    def hide(self):
        pass

    def deleteLater(self):
        self.deleted = True


def test_a_rebuild_while_hidden_does_not_drop_the_gpu_preview(app, monkeypatch, tmp_path):
    window = _window(app, monkeypatch, tmp_path)
    view = HiddenView()
    try:
        window.preview.video_view = view
        window.start_engine()
        assert window.engine is not None
        assert window.engine.preview_backend == "image"     # no GL context while hidden
        assert window.preview.video_view is view and not view.deleted
        assert window._gpu_preview_pending
        restarted = []
        monkeypatch.setattr(window, "restart_engine", lambda: restarted.append(1))
        view.isVisible = lambda: True
        window._resume_gpu_preview()
        assert restarted == [1]
    finally:
        window.preview.video_view = None
        window.shutdown()
        window.close()


# --------------------------- 4. mirror plus an asymmetric crop on the GPU canvas
def test_without_a_gpu_mirror_the_cpu_mirror_does_it():
    from anycam.model import Effects
    from anycam.sources import _effects_chain, apply_effects, gl_rack_elements

    rack = _effects_chain(Layer("l", "camera", "L"))
    gl = gl_rack_elements(mirror_on_gpu=False)
    if gl is None:
        pytest.skip("no glshader here")
    assert "gl_flip" not in gl
    rack.update(gl)
    apply_effects(rack, Effects(mirror=True))
    assert rack["videoflip"].get_property("video-direction").value_nick == "horiz"


@pytest.mark.skipif(bool(pipeline.gl_canvas_problem()), reason="no working GL canvas here")
@pytest.mark.parametrize("crop, gpu_mirror", [((0.2, 0.0, 0.0, 0.0), False),
                                              ((0.1, 0.0, 0.1, 0.0), True),
                                              ((0.0, 0.0, 0.0, 0.0), True)])
def test_the_gpu_mirror_is_used_only_for_a_symmetric_crop(crop, gpu_mirror):
    from anycam.model import OutputConfig, Project, Scene

    layer = Layer("cam", "test", "Cam", source="smpte", box=(0, 0, 1, 1), z=1, crop=crop)
    layer.effects.mirror = True
    project = Project(name="crop")
    project.output = OutputConfig(canvas_w=640, canvas_h=360, fps=30)
    scene = Scene("s", "S")
    scene.add_layer(layer)
    project.scenes = [scene]
    project.active_scene = "s"
    engine = pipeline.AnyCamEngine(project, preview=True, canvas="gpu").build()
    try:
        rack = engine._layer_chains["cam"].effects
        assert ("gl_flip" in rack) is gpu_mirror
        cpu = rack["videoflip"].get_property("video-direction").value_nick
        assert cpu == ("identity" if gpu_mirror else "horiz")
    finally:
        engine.stop()


# ---------------------------------- 5. the QML scene lives in the user's cache
def test_the_qml_scene_is_written_to_the_cache_and_never_through_a_symlink(monkeypatch, tmp_path):
    from anycam import gl_preview

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    folder = tmp_path / "cache" / "anycam"
    folder.mkdir(parents=True)
    (folder / "gpu-preview.qml").symlink_to(victim)
    path = gl_preview._qml_file()
    assert path.parent == folder and not str(path).startswith("/tmp/anycam")
    assert not path.is_symlink()
    assert path.read_text() == gl_preview.QML
    assert victim.read_text() == "keep me"


def test_a_qml_scene_that_cannot_be_written_only_skips_the_gpu_preview(app, monkeypatch):
    from anycam import gl_preview

    def refuse():
        raise PermissionError("not yours")

    monkeypatch.setattr(gl_preview, "_qml_file", refuse)
    assert gl_preview.make_video_view(None) is None


# -------------------------------- 6. a live source slower than the canvas
@pytest.mark.parametrize("source_fps, expected", [(30, 30), (60, 60), (120, 60)])
def test_a_live_chain_accepts_a_slower_source(source_fps, expected):
    """drop-only videorate cannot raise 30 fps to a fixed 60/1: it failed to link."""
    from anycam.sources import (SourceChain, _add_effects, _finish_chain, _fit_letterbox,
                                caps_filter, make)

    layer = Layer("cam", "camera", "Cam")
    chain = SourceChain()
    _add_effects(chain, layer)
    fitted = _fit_letterbox(chain, 1280, 720, 640, 360, live=True, fps=60)
    _finish_chain(chain, fitted.w, fitted.h, 60, layer=layer, pin_rate=False)
    test_pipeline = Gst.Pipeline.new("slow")
    elements = [make("videotestsrc", is_live=True),
                caps_filter(f"video/x-raw,format=RGBA,width=1280,height=720,framerate={source_fps}/1"),
                *chain.elements, make("fakesink", sync=False)]
    for element in elements:
        test_pipeline.add(element)
    assert all(a.link(b) for a, b in zip(elements, elements[1:]))
    frames = [0]
    elements[-1].get_static_pad("sink").add_probe(
        Gst.PadProbeType.BUFFER, lambda *_: frames.__setitem__(0, frames[0] + 1) or Gst.PadProbeReturn.OK)
    test_pipeline.set_state(Gst.State.PLAYING)
    try:
        time.sleep(1.2)
        assert test_pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR) is None
        rate = frames[0] / 1.2
        assert expected * 0.75 <= rate <= expected * 1.25, rate
    finally:
        test_pipeline.set_state(Gst.State.NULL)
        for cleanup in chain.cleanup:
            cleanup()


# ------------------- 7. the frame overlay no longer goes through gdk-pixbuf's loader
def _overlay_output(set_picture, style="rounded"):
    from anycam import overlay

    path = overlay.render_window_frame(320, 180, style, "#ff4d7a", 8)
    test_pipeline = Gst.parse_launch(
        "videotestsrc num-buffers=1 pattern=black ! video/x-raw,format=RGBA,width=320,height=180 "
        "! gdkpixbufoverlay name=frame overlay-width=320 overlay-height=180 ! appsink name=out")
    set_picture(test_pipeline.get_by_name("frame"), path)
    test_pipeline.set_state(Gst.State.PLAYING)
    try:
        sample = test_pipeline.get_by_name("out").emit("try-pull-sample", 5 * Gst.SECOND)
        buffer = sample.get_buffer()
        return buffer.extract_dup(0, buffer.get_size())
    finally:
        test_pipeline.set_state(Gst.State.NULL)


def test_the_frame_picture_matches_the_file_loader_and_starts_no_loader_process():
    import subprocess

    from anycam.sources import _set_frame_picture

    expected = _overlay_output(lambda frame, path: frame.set_property("location", str(path)))
    before = subprocess.run(["ps", "--ppid", str(os.getpid()), "-o", "pid=,args="],
                            capture_output=True, text=True).stdout
    got = _overlay_output(_set_frame_picture)
    after = subprocess.run(["ps", "--ppid", str(os.getpid()), "-o", "pid=,args="],
                           capture_output=True, text=True).stdout
    assert got == expected
    new_children = set(after.splitlines()) - set(before.splitlines())
    assert not [line for line in new_children if "glycin" in line or "bwrap" in line]


def _has_gdkpixbuf_bindings() -> bool:
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


@pytest.mark.skipif(not _has_gdkpixbuf_bindings(),
                    reason="no GdkPixbuf bindings (gir1.2-gdkpixbuf-2.0); the file loader is used")
def test_an_unchanged_frame_picture_is_not_handed_over_again():
    from anycam import overlay
    from anycam.sources import _set_frame_picture

    frame = Gst.ElementFactory.make("gdkpixbufoverlay")
    path = overlay.render_window_frame(320, 180, "none")
    _set_frame_picture(frame, path)
    first = frame.get_property("pixbuf")
    _set_frame_picture(frame, path)
    assert frame.get_property("pixbuf") is first or frame.get_property("pixbuf") == first
    assert first is not None


def test_the_gui_can_dump_every_thread_on_sigusr1():
    import faulthandler
    import signal

    from anycam.gui import _register_freeze_dump

    _register_freeze_dump()
    try:
        assert faulthandler.unregister(signal.SIGUSR1)
    finally:
        pass
