"""Drawing and annotation: strokes, the live canvas layer and the draw mode."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")
pytest.importorskip("PySide6")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from anycam import animated  # noqa: E402
from anycam.gui import PreviewWidget  # noqa: E402
from anycam.gui_many import ManyCamWindow  # noqa: E402
from anycam.model import Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("CACHE", "CONFIG", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))


def stroke(tool="pen", points=((0.2, 0.5), (0.6, 0.5)), color="#ff0000", width=0.01):
    return {"tool": tool, "color": color, "width": width,
            "points": [list(p) for p in points]}


def pixel(frame, x, y):
    return tuple(int(v) for v in frame[int(y), int(x)])


# ------------------------------------------------------------------- strokes
def test_strokes_round_trip_and_bad_data_is_ignored():
    strokes = [stroke("marker"), stroke("arrow"), stroke("box"), stroke()]
    assert animated.parse_strokes(animated.dump_strokes(strokes)) == strokes
    assert animated.parse_strokes("") == []
    assert animated.parse_strokes("{not json") == []
    assert animated.parse_strokes('{"strokes": [{"tool": "laser", "points": [[0, 0]]}]}') == []
    assert animated.parse_strokes('{"strokes": [{"tool": "pen", "points": []}]}') == []
    assert animated.parse_strokes('{"strokes": "oops"}') == []


def test_each_tool_draws_where_it_should_and_leaves_the_rest_transparent():
    canvas = animated.DrawingCanvas(400, 200, [
        stroke("pen", ((0.1, 0.2), (0.4, 0.2)), "#ff0000", 0.02),
        stroke("marker", ((0.1, 0.5), (0.4, 0.5)), "#ffff00", 0.02),
        stroke("box", ((0.55, 0.15), (0.9, 0.85)), "#00ff00", 0.01),
        stroke("arrow", ((0.55, 0.9), (0.95, 0.9)), "#0000ff", 0.01)])
    frame = canvas.frame(0.0)
    assert frame.shape == (200, 400, 4)
    r, g, b, a = pixel(frame, 100, 40)                       # the pen line
    assert (r, a) == (255, 255) and g < 30 and b < 30
    assert 60 < pixel(frame, 100, 100)[3] < 160              # the marker is translucent
    assert pixel(frame, 220, 30)[3] > 200 and pixel(frame, 220, 30)[1] > 200     # box edge (anti-aliased)
    assert pixel(frame, 300, 100)[3] == 0                    # inside the box: empty
    assert pixel(frame, 5, 5)[3] == 0                        # untouched corner
    assert pixel(frame, 370, 180)[3] > 235                   # the arrow head (just behind the tip)


def test_strokes_are_relative_to_the_box_so_they_survive_a_resize():
    strokes = [stroke("pen", ((0.25, 0.25), (0.75, 0.75)), "#ff0000", 0.02)]
    small = animated.DrawingCanvas(200, 100, strokes).frame(0)
    large = animated.DrawingCanvas(800, 400, strokes).frame(0)
    assert pixel(small, 100, 50)[3] > 235 and pixel(large, 400, 200)[3] > 235
    assert pixel(small, 10, 90)[3] == 0 and pixel(large, 40, 360)[3] == 0


def test_set_strokes_replaces_the_picture_and_the_frame_is_reused_otherwise():
    canvas = animated.DrawingCanvas(200, 100)
    empty = canvas.frame(0)
    assert empty[..., 3].max() == 0
    assert canvas.frame(1.0) is canvas.frame(2.0)            # no redraw while unchanged
    canvas.set_strokes([stroke()])
    assert canvas.frame(3.0)[..., 3].max() == 255
    canvas.set_strokes([])
    assert canvas.frame(4.0)[..., 3].max() == 0


# --------------------------------------------------------------- the live canvas
class Live:
    def __init__(self, *layers):
        self.frames: list[Image.Image] = []
        project = Project(name="draw")
        project.output = OutputConfig(canvas_w=640, canvas_h=360, fps=30)
        scene = Scene("s", "S")
        for layer in layers:
            scene.add_layer(layer)
        project.scenes, project.active_scene = [scene], "s"
        self.engine = AnyCamEngine(project, preview=True, on_frame=self._on).build()
        self.engine.start()

    def _on(self, sample):
        caps = sample.get_caps().get_structure(0)
        w, h = int(caps.get_value("width")), int(caps.get_value("height"))
        ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
        if ok:
            try:
                self.frames.append(Image.frombytes("RGBA", (w, h), bytes(info.data)[: w * h * 4]))
            finally:
                sample.get_buffer().unmap(info)

    def settle(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.engine.pump(30)

    def colour_at(self, x, y):
        return self.frames[-1].convert("RGB").getpixel((x, y))


def drawing_layer(strokes=()):
    return Layer("draw", "drawing", "Drawing", source=animated.dump_strokes(list(strokes)),
                 box=(0, 0, 1, 1), z=9)


def test_strokes_appear_on_the_canvas_and_update_live_without_a_rebuild():
    base = Layer("bg", "test", "bg", source="black", box=(0, 0, 1, 1), z=1)
    layer = drawing_layer([stroke("pen", ((0.1, 0.5), (0.9, 0.5)), "#ff0000", 0.02)])
    live = Live(base, layer)
    try:
        live.settle(0.6)
        assert live.engine.report.error == ""
        r, g, b = live.colour_at(320, 180)
        assert r > 200 and g < 40 and b < 40
        pad = live.engine._layer_pads["draw"]
        assert live.engine.set_layer_strokes("draw", [
            stroke("pen", ((0.5, 0.1), (0.5, 0.9)), "#00ff00", 0.02)])
        live.settle(0.6)
        assert live.engine._layer_pads["draw"] is pad            # same pad: no rebuild
        assert live.colour_at(100, 180) == (0, 0, 0)             # the old stroke is gone
        assert live.colour_at(320, 60)[1] > 200                  # the new one is there
        assert live.engine.set_layer_strokes("bg", []) is False   # not a drawing layer
    finally:
        live.engine.stop()


# ------------------------------------------------------------------- draw mode
@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def preview(app, layers=()):
    widget = PreviewWidget()
    widget.resize(960, 540)
    widget.image = QImage(960, 540, QImage.Format.Format_RGBA8888)
    scene = Scene("s", "S")
    for layer in layers:
        scene.add_layer(layer)
    widget.set_scene(scene)
    return widget


def test_the_pointer_draws_in_canvas_units_and_a_stroke_is_emitted(app):
    widget = preview(app)
    widget.set_edit_mode("draw")
    got = []
    widget.stroke_finished.connect(got.append)
    widget.draw_tool, widget.draw_color, widget.draw_width = "pen", "#123456", 0.01
    widget._begin_stroke(QPointF(96, 54))
    for step in range(1, 11):
        widget._extend_stroke(QPointF(96 + step * 60, 54 + step * 20))
    widget._finish_stroke()
    assert len(got) == 1
    out = got[0]
    assert (out["tool"], out["color"], out["width"]) == ("pen", "#123456", 0.01)
    assert out["points"][0] == pytest.approx([0.1, 0.1])
    assert out["points"][-1] == pytest.approx([696 / 960, 254 / 540])
    assert len(out["points"]) > 5


def test_shapes_keep_only_their_start_and_end_and_a_bare_click_draws_no_shape(app):
    widget = preview(app)
    widget.set_edit_mode("draw")
    got = []
    widget.stroke_finished.connect(got.append)
    widget.draw_tool = "box"
    widget._begin_stroke(QPointF(100, 100))
    for x in (150, 300, 480):
        widget._extend_stroke(QPointF(x, 300))
    widget._finish_stroke()
    assert len(got[0]["points"]) == 2
    widget._begin_stroke(QPointF(200, 200))
    widget._finish_stroke()
    assert len(got) == 1                                     # the click made nothing


def test_the_pointer_outside_the_picture_does_not_start_a_stroke(app):
    widget = preview(app)
    widget.resize(1200, 540)                  # leaves bars either side of the picture
    widget.set_edit_mode("draw")
    got = []
    widget.stroke_finished.connect(got.append)
    widget._begin_stroke(QPointF(5, 5))
    widget._finish_stroke()
    assert got == []


def test_a_drawing_layer_never_captures_clicks_meant_for_what_is_beneath(app):
    under = Layer("under", "test", "U", box=(0.2, 0.2, 0.4, 0.4), z=1)
    over = Layer("over", "drawing", "D", box=(0, 0, 1, 1), z=9)
    widget = preview(app, [under, over])
    assert widget.layer_at(QPointF(960 * 0.4, 540 * 0.4)).id == "under"
    assert widget.layer_at(QPointF(960 * 0.9, 540 * 0.9)) is None


def test_edit_modes_switch_cleanly(app):
    widget = preview(app)
    widget.set_edit_mode("draw")
    widget._begin_stroke(QPointF(300, 200))
    widget.set_edit_mode("move")
    assert widget._stroke is None and widget.edit_mode == "move"
    widget.set_edit_mode("nonsense")
    assert widget.edit_mode == "move"


# ------------------------------------------------------------------- the window
def make_window(app):
    scene = Scene("s", "S")
    scene.add_layer(Layer("cam", "test", "Cam", source="smpte", box=(0, 0, 1, 1), z=1))
    window = ManyCamWindow(Project(name="w", scenes=[scene], active_scene="s"))
    window.headless = True
    window._persist = False
    window.resize(1200, 800)
    window.show()
    app.processEvents()
    return window


def close(window, app):
    window.engine = None
    window._persist = False
    window.close()
    app.processEvents()


def test_draw_mode_shows_the_bar_and_done_returns_to_editing(app):
    window = make_window(app)
    try:
        assert window.draw_bar.isHidden()
        window.draw_button.click()
        assert not window.draw_bar.isHidden() and window.preview.edit_mode == "draw"
        window.toggle_draw(False)
        assert window.draw_bar.isHidden() and window.preview.edit_mode == "move"
        assert not window.draw_button.isChecked()
    finally:
        close(window, app)


def test_the_first_stroke_creates_a_drawing_layer_and_later_ones_join_it(app):
    window = make_window(app)
    try:
        window._draw_stroke_finished(stroke())
        drawings = [l for l in window.scene.layers if l.kind == "drawing"]
        assert len(drawings) == 1
        window._draw_stroke_finished(stroke("box", ((0.1, 0.1), (0.3, 0.3))))
        assert [l for l in window.scene.layers if l.kind == "drawing"] == drawings
        assert len(animated.parse_strokes(drawings[0].source)) == 2
        assert drawings[0].z > window.scene.layers[0].z          # above the picture
    finally:
        close(window, app)


def test_undo_clear_and_the_tool_settings_reach_the_stroke(app):
    window = make_window(app)
    try:
        window._draw_tool("arrow")
        window._draw_colour("#00ff88")
        window.draw_width_slider.setValue(10)
        assert (window.preview.draw_tool, window.preview.draw_color) == ("arrow", "#00ff88")
        assert window.preview.draw_width == pytest.approx(0.015)
        assert window.draw_tool_buttons["arrow"].isChecked()
        assert not window.draw_tool_buttons["pen"].isChecked()
        window._draw_stroke_finished(stroke())
        window._draw_stroke_finished(stroke("box", ((0.1, 0.1), (0.3, 0.3))))
        layer = window._drawing_layer()
        window._draw_undo()
        assert [s["tool"] for s in animated.parse_strokes(layer.source)] == ["pen"]
        window._draw_clear()
        assert animated.parse_strokes(layer.source) == []
        window._draw_undo()                                      # nothing left: no error
        assert "Nothing to undo" in window.statusBar().currentMessage()
    finally:
        close(window, app)


def test_with_a_running_canvas_strokes_go_live_and_never_restart_it(app):
    window = make_window(app)
    updates, restarts = [], []
    window.engine = SimpleNamespace(
        set_layer_strokes=lambda layer_id, strokes: updates.append((layer_id, len(strokes))) or True,
        add_layer=lambda layer, **kw: True)
    window.restart_engine = lambda: restarts.append(1)
    try:
        window._draw_stroke_finished(stroke())                   # creates the layer
        window._draw_stroke_finished(stroke())
        window._draw_stroke_finished(stroke())
        assert [count for _id, count in updates] == [2, 3]
        assert restarts == []
        window.engine = SimpleNamespace(set_layer_strokes=lambda *a: False)
        window._draw_stroke_finished(stroke())                   # layer not built: rebuild
        assert restarts == [1]
    finally:
        close(window, app)


def test_drawings_are_saved_with_the_project(app):
    window = make_window(app)
    try:
        window._draw_stroke_finished(stroke("marker", ((0.2, 0.2), (0.8, 0.3)), "#facc15"))
        loaded = Project.from_dict(window.project.to_dict())
        layer = next(l for l in loaded.scene.layers if l.kind == "drawing")
        assert animated.parse_strokes(layer.source)[0]["color"] == "#facc15"
    finally:
        close(window, app)
