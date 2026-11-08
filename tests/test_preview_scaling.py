"""Preview frames follow the widget size (at most 960x540) and are painted
without a per-frame QImage rescale or copy on the GUI thread."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam.model import Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402


def _project(width=1920, height=1080) -> Project:
    project = Project(name="preview")
    project.output = OutputConfig(canvas_w=width, canvas_h=height, fps=30)
    scene = Scene("scene_preview", "Preview")
    scene.add_layer(Layer("l_test", "test", "Test", source="smpte",
                          box=(0.0, 0.0, 1.0, 1.0), z=1))
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def _engine(target=None, width=1920, height=1080) -> AnyCamEngine:
    return AnyCamEngine(_project(width, height), preview=True, preview_target=target)


# ------------------------------------------------------------------ sizing
@pytest.mark.parametrize("target, canvas, expected", [
    (None, (1920, 1080), (960, 540)),            # headless default
    ((1236, 695), (1920, 1080), (960, 540)),     # panel larger than the cap
    ((1920, 1080), (1920, 1080), (960, 540)),    # full screen: still capped
    ((640, 360), (1920, 1080), (640, 360)),      # small panel: smaller frame
    ((641, 361), (1920, 1080), (640, 360)),      # even sizes
    ((800, 800), (640, 360), (640, 360)),        # never above the canvas
    ((600, 600), (1920, 1080), (600, 336)),      # letterboxed: width limits
    ((1000, 300), (1920, 1080), (532, 300)),     # pillarboxed: height limits
    ((1, 1), (1920, 1080), (2, 2)),              # degenerate widget
])
def test_the_preview_size_follows_the_widget(target, canvas, expected):
    assert _engine(target, *canvas)._preview_size() == expected


def test_a_resize_changes_the_preview_caps_in_place():
    engine = _engine((960, 540))
    engine._preview_caps = Gst.ElementFactory.make("capsfilter")
    assert engine.set_preview_target(640, 360) is True
    caps = engine._preview_caps.get_property("caps").get_structure(0)
    assert (caps.get_value("width"), caps.get_value("height")) == (640, 360)
    # The same frame size again: nothing to renegotiate.
    assert engine.set_preview_target(641, 361) is False
    # Growing past the cap only returns to the cap.
    assert engine.set_preview_target(1920, 1080) is True
    assert engine.set_preview_target(3840, 2160) is False


def test_a_running_preview_switches_size_without_a_rebuild():
    sizes: list[tuple[int, int]] = []

    def on_frame(sample) -> None:
        caps = sample.get_caps().get_structure(0)
        sizes.append((int(caps.get_value("width")), int(caps.get_value("height"))))

    project = _project(640, 360)
    engine = AnyCamEngine(project, preview=True, preview_target=(320, 180),
                          on_frame=on_frame).build()
    pipeline = engine.pipeline
    engine.start()
    try:
        end = time.monotonic() + 3
        while time.monotonic() < end and not sizes:
            engine.pump(50)
        assert sizes and sizes[-1] == (320, 180)
        engine.set_preview_target(480, 270)
        sizes.clear()
        end = time.monotonic() + 3
        while time.monotonic() < end and (480, 270) not in sizes:
            engine.pump(50)
        assert (480, 270) in sizes, sizes[-5:]
        assert engine.pipeline is pipeline, "a resize must not rebuild the graph"
        assert not engine.report.error
    finally:
        engine.stop()


# --------------------------------------------------------------- painting
@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def test_the_widget_paints_without_a_per_frame_rescale_or_copy(app, monkeypatch):
    import gc

    from PySide6.QtGui import QColor, QImage

    from anycam.gui import PreviewWidget

    widget = PreviewWidget()
    widget.canvas_size = (1920, 1080)
    reported = []
    widget.display_size_changed.connect(lambda w, h: reported.append((w, h)))
    widget.resize(1000, 700)
    widget.show()
    app.processEvents()
    scaled_calls = []
    original = QImage.scaled
    monkeypatch.setattr(QImage, "scaled", lambda self, *a, **k: (
        scaled_calls.append(a), original(self, *a, **k))[1])
    try:
        assert reported and reported[-1] == widget.display_size()
        width, height = widget.display_size()
        assert abs(width / height - 16 / 9) < 0.01
        target = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
        # An exact-size frame and a capped (smaller) frame both fill the
        # canvas area, and neither goes through QImage.scaled.
        for size, colour in (((width // 2 * 2, height // 2 * 2), (255, 0, 0)),
                             ((960, 540), (0, 255, 0))):
            raw = bytes((*colour, 255)) * (size[0] * size[1])
            widget.set_image(QImage(raw, size[0], size[1], size[0] * 4,
                                    QImage.Format.Format_RGBA8888), owner=raw)
            del raw
            gc.collect()
            widget.render(target)
            for x, y in ((widget.width() // 2, widget.height() // 2),
                         (int(widget.canvas_rect().right()) - 2,
                          int(widget.canvas_rect().bottom()) - 2)):
                pixel = target.pixelColor(x, y)
                assert (pixel.red(), pixel.green(), pixel.blue()) == colour
        assert scaled_calls == []
    finally:
        widget.close()


def test_the_window_passes_the_shown_size_to_the_engine(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from anycam.demo import build_demo_project
    from anycam.gui import AnyCamWindow

    window = AnyCamWindow(build_demo_project("testsrc", 320, 180, 30))
    calls = []

    class Engine:
        def set_preview_target(self, width, height):
            calls.append((width, height))

    try:
        window.engine = Engine()
        window.preview.resize(800, 450)
        window._apply_preview_size()
        assert calls == [window.preview.display_size()]
    finally:
        window.engine = None
        window.close()
