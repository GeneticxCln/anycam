"""GPU preview (qml6glsink): when it is used, how it falls back.

The view itself needs a real window system and OpenGL, which the offscreen
test platform does not have; these tests cover the decisions and the graph.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam import gl_preview  # noqa: E402
from anycam.model import Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)
needs_qml6glsink = pytest.mark.skipif(
    Gst.ElementFactory.find("qml6glsink") is None, reason="qml6glsink is not installed")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def fresh_reason():
    gl_preview.unavailable_reason.cache_clear()
    yield
    gl_preview.unavailable_reason.cache_clear()


def _project() -> Project:
    project = Project(name="gpu")
    project.output = OutputConfig(canvas_w=640, canvas_h=360, fps=30)
    scene = Scene("scene_gpu", "GPU")
    scene.add_layer(Layer("l_test", "test", "Test", source="smpte",
                          box=(0.0, 0.0, 1.0, 1.0), z=1))
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


# --------------------------------------------------------------- deciding
def test_the_offscreen_platform_never_uses_the_gpu_preview(app, monkeypatch):
    monkeypatch.delenv("ANYCAM_PREVIEW", raising=False)
    assert "no window system" in gl_preview.unavailable_reason()
    assert not gl_preview.available()


def test_the_image_preview_can_be_forced(app, monkeypatch):
    monkeypatch.setenv("ANYCAM_PREVIEW", "image")
    assert gl_preview.requested() == "image"
    assert gl_preview.unavailable_reason() == "ANYCAM_PREVIEW=image"


def test_an_unknown_preview_value_means_automatic(monkeypatch):
    monkeypatch.setenv("ANYCAM_PREVIEW", "fancy")
    assert gl_preview.requested() == ""


def test_attaching_needs_a_visible_view():
    """The sink takes Qt's GL context, which a hidden view never creates."""

    class HiddenView:
        video_item = None

        def isVisible(self):
            return False

    assert gl_preview.attach_sink(Gst.ElementFactory.make("fakesink"), HiddenView()) is False
    assert gl_preview.attach_sink(Gst.ElementFactory.make("fakesink"), None) is False


# ------------------------------------------------------------------ graph
def test_without_a_gpu_view_the_engine_builds_the_image_preview():
    engine = AnyCamEngine(_project(), preview=True).build()
    try:
        assert engine.preview_backend == "image"
        assert engine._appsink is not None
    finally:
        engine.stop()


@pytest.mark.parametrize("answer", [False, "raise"])
def test_a_refused_or_failing_gpu_attach_falls_back_to_the_image_preview(answer):
    def attach(_sink):
        if answer == "raise":
            raise RuntimeError("no context")
        return answer

    engine = AnyCamEngine(_project(), preview=True, preview_gl_sink=attach).build()
    try:
        assert engine.preview_backend == "image"
        assert engine._appsink is not None
    finally:
        engine.stop()


@needs_qml6glsink
def test_the_gpu_branch_uploads_without_the_udmabuf_pool():
    """identity drop-allocation keeps glupload off the dmabuf import that
    NVIDIA rejects; the sink is the one handed to the window."""
    handed = []

    def attach(sink):
        handed.append(sink)
        return True

    engine = AnyCamEngine(_project(), preview=True, preview_gl_sink=attach).build()
    try:
        assert engine.preview_backend == "gpu"
        assert engine._appsink is None
        children = {child.get_name(): child for child in engine.pipeline.children}
        assert children["preview_no_pool"].get_property("drop-allocation") is True
        assert children["preview_gl_upload"].get_factory().get_name() == "glupload"
        assert children["preview_gl_sink"] is handed[0]
        assert handed[0].get_factory().get_name() == "qml6glsink"
        assert handed[0].get_property("sync") is False
        # The GPU preview has no caps to resize: the GPU scales it.
        assert engine.set_preview_target(320, 180) in (True, False)
    finally:
        engine.stop()


# ----------------------------------------------------------------- widget
class FakeRoot:
    def __init__(self):
        self.props = {}

    def setProperty(self, name, value):
        self.props[name] = value


class FakeView:
    def __init__(self):
        self.root = FakeRoot()
        self.deleted = False

    def rootObject(self):
        return self.root

    def lower(self):
        pass

    def hide(self):
        pass

    def deleteLater(self):
        self.deleted = True


def test_live_state_switches_the_qml_view_and_the_canvas_geometry(app):
    from anycam.gui import PreviewWidget

    widget = PreviewWidget()
    widget.resize(800, 600)
    widget.canvas_size = (1920, 1080)
    widget.video_view = FakeView()
    try:
        assert widget.canvas_rect().isEmpty()  # no frame, not live
        widget.set_gpu_live(True)
        assert widget.video_view.root.props["live"] is True
        # The GPU preview has no QImage: the canvas aspect places the marks.
        rect = widget.canvas_rect()
        assert (round(rect.width()), round(rect.height())) == (800, 450)
        assert rect.top() == pytest.approx(75)
        assert widget.has_frame()
        widget.set_gpu_live(False)
        assert widget.video_view.root.props["live"] is False
        assert not widget.has_frame()
    finally:
        widget.video_view = None
        widget.close()


def test_disabling_the_gpu_view_returns_to_the_image_preview(app):
    from anycam.gui import PreviewWidget

    widget = PreviewWidget()
    view = FakeView()
    widget.video_view = view
    widget.gpu_live = True
    widget.disable_gpu()
    assert view.deleted and widget.video_view is None and not widget.gpu_live
    widget.set_gpu_live(True)  # without a view it can never become live
    assert not widget.gpu_live
    widget.close()


def test_the_overlay_draws_marks_only_for_the_gpu_view(app, monkeypatch):
    from PySide6.QtGui import QImage

    from anycam.gui import PreviewWidget, _PreviewOverlay

    widget = PreviewWidget()
    widget.resize(320, 180)
    overlay = _PreviewOverlay(widget)
    overlay.resize(320, 180)
    calls = []
    monkeypatch.setattr(widget, "_paint_marks", lambda painter: calls.append(1))
    target = QImage(320, 180, QImage.Format.Format_ARGB32_Premultiplied)
    overlay.render(target)
    assert calls == []  # the image preview paints its own marks
    widget.gpu_live = True
    overlay.render(target)
    assert calls == [1]
    widget.close()
