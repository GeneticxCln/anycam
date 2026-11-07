"""Offscreen GUI regressions: preview, snapshot, recording close, screenshot.

The window is driven with Qt's ``offscreen`` platform so these tests do not
touch the live desktop session.  Each test owns one window and finalises the
engine in ``finally`` so GStreamer teardown stays clean.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("gi")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox

from anycam import config, portal, sources
from anycam.demo import build_demo_project
from anycam.gui import AnyCamWindow, CameraGridDialog
from anycam.model import Layer, Project, Scene

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def _window() -> AnyCamWindow:
    window = AnyCamWindow(build_demo_project("testsrc", 320, 180, 30))
    window.resize(720, 480)
    window.show()
    return window


def _pump(app: QApplication, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def _wait_for(app: QApplication, predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_second_gui_launch_is_forwarded_not_started(app):
    """Two GUI processes would race for the single v4l2loopback writer."""
    from anycam.gui import claim_gui_instance
    from PySide6.QtNetwork import QLocalServer

    name = f"anycam-test-{os.getpid()}-{time.monotonic_ns()}"
    primary, server = claim_gui_instance(name)
    assert primary is True and server is not None
    try:
        second, second_server = claim_gui_instance(name)
        assert second is False
        assert second_server is None
    finally:
        server.close()
        QLocalServer.removeServer(name)


def test_canvas_resize_reuses_the_open_vcam_output(app):
    """A canvas resize must not reopen v4l2sink under a consumer."""
    window = _window()

    class Held:
        active = True
        width = 320
        height = 180
        fps = 30
        error = ""

        def __init__(self, device: str) -> None:
            self.device = device

        def stop(self) -> dict:
            self.active = False
            return {"error": ""}

    held = Held(config.loopback_device())
    window.project.output.vcam_device = held.device
    window._vcam_output = held
    try:
        window.project.output.canvas_w = 3840
        window.project.output.canvas_h = 2160
        assert window._start_vcam_output() is True
        assert window._vcam_output is held
        assert held.active is True
    finally:
        window.close()
        app.processEvents()


def test_preview_delivers_a_live_frame(app):
    window = _window()
    try:
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        assert _wait_for(app, lambda: window.preview.image is not None
                                   and not window.preview.image.isNull())
        assert window.engine.report.error == ""
        assert window.engine._appsink is not None
    finally:
        window.close()
        app.processEvents()


def test_restart_keeps_the_last_preview_frame_on_screen(app, monkeypatch):
    """A scene/resolution rebuild must not blank the program monitor."""
    window = _window()
    image = QImage(32, 18, QImage.Format.Format_RGBA8888)
    image.fill(QColor("red"))
    window.preview.set_image(image)

    class Engine:
        def stop(self) -> dict:
            return {}

    started: list[bool] = []
    window.engine = Engine()
    monkeypatch.setattr(window, "start_engine",
                        lambda: started.append(True))
    try:
        window.restart_engine()
        assert window.engine is None
        assert window.preview.image is image
        assert started == [True]
    finally:
        window.engine = None
        window.close()
        app.processEvents()


def test_snapshot_writes_the_live_frame(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path))
    window = _window()
    try:
        window.start_engine()
        assert window.engine is not None
        assert _wait_for(app, lambda: window.preview.image is not None)
        window.snapshot()
        files = list((tmp_path / "AnyCam").glob("anycam-snapshot-*.png"))
        assert files, "snapshot() did not write a PNG"
        assert files[0].stat().st_size > 0
        from PIL import Image
        with Image.open(files[0]) as image:
            image.verify()
    finally:
        window.close()
        app.processEvents()


def test_closing_while_recording_finalises_the_mp4(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path))
    window = _window()
    window.recording = True
    window.record_button.setText("Stop recording")
    try:
        window.start_engine()
        assert window.engine is not None
        path = Path(window.engine.record_path)
        assert _wait_for(app, lambda: window.preview.image is not None)
        _pump(app, 1.5)
        window.close()
        app.processEvents()
        assert path.exists() and path.stat().st_size > 0
        if shutil.which("ffprobe"):
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True,
            )
            assert probe.returncode == 0, probe.stderr
            assert float(probe.stdout.strip()) > 0.5
    finally:
        window.close()
        app.processEvents()


def test_screenshot_deadline_exits_without_a_frame(app, tmp_path):
    window = _window()
    out = tmp_path / "deadline.png"
    try:
        window.headless = True
        window.auto_screenshot_path = str(out)
        window._screenshot_done = False
        window.screenshot_ok = False
        window._screenshot_deadline()
        assert window._screenshot_done is True
        assert window.screenshot_ok is False
        assert out.exists() and out.stat().st_size > 0
    finally:
        window.close()
        app.processEvents()


def test_screenshot_success_marks_the_frame(app, tmp_path):
    window = _window()
    out = tmp_path / "live.png"
    try:
        window.start_engine()
        assert window.engine is not None
        assert _wait_for(app, lambda: window.preview.image is not None)
        window.auto_screenshot_path = str(out)
        window._screenshot_done = False
        window._auto_screenshot()
        assert window.screenshot_ok is True, window.statusBar().currentMessage()
        assert out.exists() and out.stat().st_size > 0
    finally:
        window.close()
        app.processEvents()


def test_failed_start_clears_recording_state(app, tmp_path, monkeypatch):
    """B11: a refused output must not leave Record stuck on."""
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path))
    window = _window()
    window.headless = True
    window._quiet_engine_errors = True
    window.recording = True
    window.record_button.setText("Stop recording")

    def refuse(_self):
        raise RuntimeError("output refused")

    monkeypatch.setattr("anycam.pipeline.AnyCamEngine.build", refuse)
    try:
        window.start_engine()
        assert window.recording is False
        assert window.record_button.text() == "Record"
        assert window._recorder is None
        assert "output refused" in window.statusBar().currentMessage()
    finally:
        window.close()
        app.processEvents()


def _live_canvas_image() -> QImage:
    image = QImage(640, 360, QImage.Format.Format_RGBA8888)
    image.fill(QColor("#203040"))
    return image


def _mouse(widget, kind, pos: QPointF) -> None:
    if kind == QEvent.Type.MouseMove:
        event = QMouseEvent(kind, pos, pos, Qt.MouseButton.NoButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    elif kind == QEvent.Type.MouseButtonPress:
        event = QMouseEvent(kind, pos, pos, Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    else:
        event = QMouseEvent(kind, pos, pos, Qt.MouseButton.LeftButton,
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


def _drag(widget, start: QPointF, end: QPointF) -> None:
    _mouse(widget, QEvent.Type.MouseButtonPress, start)
    _mouse(widget, QEvent.Type.MouseMove, end)
    _mouse(widget, QEvent.Type.MouseButtonRelease, end)


def _key(widget, key, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    QApplication.sendEvent(
        widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


def test_preview_drag_moves_the_selected_layer(app):
    window = _window()
    try:
        window.preview.set_image(_live_canvas_image())
        layer = window.scene.layer("layer_source")
        layer.box = (0.2, 0.2, 0.4, 0.4)
        window.preview.select_layer(layer.id)
        _pump(app, 0.05)
        canvas = window.preview.canvas_rect()
        start = window.preview.box_rect(layer).center()
        _drag(window.preview, start,
              QPointF(start.x() + canvas.width() * 0.1,
                      start.y() + canvas.height() * 0.1))
        assert layer.box[0] == pytest.approx(0.3, abs=0.02)
        assert layer.box[1] == pytest.approx(0.3, abs=0.02)
        assert layer.box[2] == pytest.approx(0.4, abs=0.005)
        assert layer.box[3] == pytest.approx(0.4, abs=0.005)
        assert window.preview.selected_id == layer.id
    finally:
        window.close()
        app.processEvents()


def test_preview_corner_handle_resizes_the_layer(app):
    window = _window()
    try:
        window.preview.set_image(_live_canvas_image())
        layer = window.scene.layer("layer_source")
        layer.box = (0.2, 0.2, 0.4, 0.4)
        window.preview.select_layer(layer.id)
        _pump(app, 0.05)
        canvas = window.preview.canvas_rect()
        start = window.preview.box_rect(layer).bottomRight()
        _drag(window.preview, start,
              QPointF(start.x() + canvas.width() * 0.1,
                      start.y() + canvas.height() * 0.1))
        assert layer.box[2] == pytest.approx(0.5, abs=0.02)
        assert layer.box[3] == pytest.approx(0.5, abs=0.02)
        assert layer.box[0] == pytest.approx(0.2, abs=0.005)
    finally:
        window.close()
        app.processEvents()


def test_preview_crop_handles_trim_and_pan_the_source(app):
    window = _window()
    try:
        window.preview.set_image(_live_canvas_image())
        layer = window.scene.layer("layer_source")
        layer.box = (0.2, 0.2, 0.6, 0.6)
        layer.crop = (0.1, 0.1, 0.1, 0.1)
        window.preview.select_layer(layer.id)
        window._set_edit_mode("crop")
        _pump(app, 0.05)
        canvas = window.preview.canvas_rect()
        # Crop insets are fractions of the picture, so a pointer move in canvas
        # pixels is scaled by how much of the canvas the picture covers: the
        # window now tracks the pointer instead of lagging behind it (B31).
        picture = window.preview.picture_rect(layer)
        factor = canvas.width() / picture.width()
        crop = window.preview.crop_rect(layer)
        left_edge = QPointF(crop.left(), crop.center().y())
        _drag(window.preview, left_edge,
              QPointF(left_edge.x() + canvas.width() * 0.04, left_edge.y()))
        assert layer.crop[0] == pytest.approx(0.1 + 0.04 * factor, abs=0.02)
        assert layer.crop[2] == pytest.approx(0.1, abs=0.005)
        inset_sum = layer.crop[0] + layer.crop[2]

        # Panning the crop window keeps its size and moves both insets.
        crop = window.preview.crop_rect(layer)
        middle = crop.center()
        _drag(window.preview, middle,
              QPointF(middle.x() + canvas.width() * 0.03, middle.y()))
        assert layer.crop[0] > 0.1 + 0.04 * factor
        assert layer.crop[2] < 0.1
        assert layer.crop[0] + layer.crop[2] == pytest.approx(inset_sum, abs=0.01)

        # Dragging past the canvas edge clamps instead of producing a
        # negative far inset.
        crop = window.preview.crop_rect(layer)
        _drag(window.preview, crop.center(),
              QPointF(canvas.right(), crop.center().y()))
        assert layer.crop[2] >= 0.0
        assert layer.crop[0] + layer.crop[2] == pytest.approx(inset_sum, abs=0.01)

        window._reset_crop()
        assert layer.crop == (0.0, 0.0, 0.0, 0.0)
    finally:
        window.close()
        app.processEvents()


def test_preview_arrow_keys_nudge_the_selected_layer(app):
    window = _window()
    try:
        window.preview.set_image(_live_canvas_image())
        layer = window.scene.layer("layer_source")
        layer.box = (0.2, 0.2, 0.4, 0.4)
        window.preview.select_layer(layer.id)
        _pump(app, 0.05)
        _key(window.preview, Qt.Key.Key_Right)
        assert layer.box[0] == pytest.approx(0.202, abs=1e-6)
        _key(window.preview, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
        assert layer.box[1] == pytest.approx(0.22, abs=1e-6)
        _key(window.preview, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
        assert layer.box[0] == pytest.approx(0.182, abs=1e-6)

        layer.crop = (0.1, 0.1, 0.1, 0.1)
        window._set_edit_mode("crop")
        _key(window.preview, Qt.Key.Key_Right)
        assert layer.crop[0] == pytest.approx(0.102, abs=1e-6)
        assert layer.crop[2] == pytest.approx(0.098, abs=1e-6)
    finally:
        window.close()
        app.processEvents()


def test_numeric_box_and_crop_fields_apply_and_clamp(app):
    window = _window()
    try:
        layer = window.scene.layer("layer_source")
        layer.box = (0.2, 0.2, 0.4, 0.4)
        layer.crop = (0.1, 0.1, 0.1, 0.1)
        window.preview.select_layer(layer.id)
        _pump(app, 0.05)
        assert window.box_spins["x"].value() == pytest.approx(0.2)
        assert window.crop_spins["l"].value() == pytest.approx(0.1)

        window.box_spins["x"].setValue(0.5)
        assert layer.box[0] == pytest.approx(0.5)
        window.box_spins["w"].setValue(0.9)
        assert layer.box[2] == pytest.approx(0.5)

        window.crop_spins["l"].setValue(0.3)
        assert layer.crop[0] == pytest.approx(0.3)
        window.crop_spins["r"].setValue(0.8)
        assert layer.crop[2] == pytest.approx(0.6)

        window.preview.select_layer("")
        assert not window.box_spins["x"].isEnabled()
        assert not window.opacity_slider.isEnabled()
    finally:
        window.close()
        app.processEvents()


def test_opacity_and_layer_order_controls(app):
    window = _window()
    try:
        layer = window.scene.layer("layer_source")
        window.preview.select_layer(layer.id)
        _pump(app, 0.05)
        window.opacity_slider.setValue(40)
        assert layer.alpha == pytest.approx(0.4)
        assert window.opacity_label.text() == "40%"

        before = [item.id for item in window.scene.ordered()]
        window.order_buttons[0].click()  # Raise
        after = [item.id for item in window.scene.ordered()]
        assert after.index(layer.id) == before.index(layer.id) + 1
        window.order_buttons[3].click()  # Bottom
        assert window.scene.ordered()[0].id == layer.id
    finally:
        window.close()
        app.processEvents()


def test_layer_edit_while_recording_keeps_one_continuous_file(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path))
    from PIL import Image

    plate = tmp_path / "plate.png"
    Image.new("RGB", (640, 360), (30, 120, 200)).save(plate)
    window = AnyCamWindow(build_demo_project(str(plate), 320, 180, 30))
    window.resize(720, 480)
    window.show()
    window.headless = True
    layer = window.scene.layer("layer_source")
    layer.crop = (0.1, 0.1, 0.1, 0.1)
    window.recording = True
    window.record_button.setText("Stop recording")
    try:
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        assert _wait_for(app, lambda: window.preview.image is not None, timeout=8)
        _pump(app, 1.0)
        first_engine = window.engine
        window.preview.select_layer(layer.id)
        # Opacity and stacking are live pad updates: they must not restart the
        # graph or split the recording.
        window.opacity_slider.setValue(60)
        window.order_buttons[0].click()
        _pump(app, 0.5)
        window._set_edit_mode("crop")
        _pump(app, 0.05)
        crop = window.preview.crop_rect(layer)
        start = QPointF(crop.left(), crop.center().y())
        _drag(window.preview, start, QPointF(start.x() + 40, start.y()))
        assert _wait_for(
            app,
            lambda: window.engine is not None and window.engine is not first_engine
                    and window.preview.image is not None,
            timeout=12,
        ), window.statusBar().currentMessage()
        _pump(app, 1.2)
        window.close()
        app.processEvents()
        files = [Path(path) for path in window.recording_segments]
        # The crop editor and the crop commit both rebuild the canvas.  The
        # recorder must keep the same file across both rebuilds.
        assert len(files) == 1, files
        path = files[0]
        assert path.exists() and path.stat().st_size > 0
        if shutil.which("ffprobe"):
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True,
            )
            assert probe.returncode == 0, probe.stderr
            assert float(probe.stdout.strip()) > 1.5, probe.stdout
    finally:
        window.close()
        app.processEvents()


def test_record_toggle_attaches_without_rebuilding(app, tmp_path, monkeypatch):
    """B12: starting/stopping Record is a live tap change, not a graph rebuild."""
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path))
    window = _window()
    window.headless = True
    try:
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        assert _wait_for(app, lambda: window.preview.image is not None)
        first = window.engine
        baseline = len(window.recording_segments)

        window.toggle_record()
        assert window.recording is True
        assert window.engine is first, "Record rebuilt the canvas"
        assert first._recorder_valve is not None
        assert first._recorder_valve.get_property("drop") is False
        _pump(app, 1.0)

        window.toggle_record()
        assert window.recording is False
        assert window.engine is first, "Stop rebuilt the canvas"
        assert first._recorder_valve.get_property("drop") is True
        files = [Path(path) for path in window.recording_segments]
        assert len(files) == baseline + 1, files
        assert files[-1].exists() and files[-1].stat().st_size > 0
    finally:
        window.close()
        app.processEvents()


def test_five_scene_switches_keep_one_recording(app, tmp_path, monkeypatch):
    """B12: each scene switch rebuilds the canvas; the MP4 must stay one file."""
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path))
    project = build_demo_project("testsrc", 320, 180, 30)
    first = project.scene
    for index in range(1, 5):
        scene = Scene(f"scene_{index}", f"Scene {index}")
        for layer in first.layers:
            clone = Layer.from_dict(layer.to_dict())
            clone.id = f"{layer.id}_{index}"
            scene.add_layer(clone)
        project.scenes.append(scene)
    window = AnyCamWindow(project)
    window.resize(720, 480)
    window.show()
    window.headless = True
    window.recording = True
    window.record_button.setText("Stop recording")
    try:
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        assert _wait_for(app, lambda: window.preview.image is not None, timeout=8)
        _pump(app, 1.0)
        previous = window.engine
        # Five rebuilds, including the return to the first scene.
        for index in (1, 2, 3, 4, 0):
            window.switch_scene(index)
            assert window.engine is not None, window.statusBar().currentMessage()
            assert window.engine is not previous
            previous = window.engine
            _pump(app, 0.45)
        window.close()
        app.processEvents()
        files = [Path(path) for path in window.recording_segments]
        assert len(files) == 1, files
        path = files[0]
        assert path.exists() and path.stat().st_size > 0
        if shutil.which("ffprobe"):
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True,
            )
            assert probe.returncode == 0, probe.stderr
            assert float(probe.stdout.strip()) > 2.0, probe.stdout
    finally:
        window.close()
        app.processEvents()


def test_undo_redo_drag_opacity_and_order(app):
    window = _window()
    try:
        window.preview.set_image(_live_canvas_image())
        layer = window.scene.layer("layer_source")
        layer.box = (0.2, 0.2, 0.4, 0.4)
        window.preview.select_layer(layer.id)
        _pump(app, 0.05)
        canvas = window.preview.canvas_rect()
        start = window.preview.box_rect(layer).center()
        _drag(window.preview, start,
              QPointF(start.x() + canvas.width() * 0.1, start.y()))
        assert layer.box[0] == pytest.approx(0.3, abs=0.02)
        assert window.undo_button.isEnabled()
        window.undo()
        assert layer.box[0] == pytest.approx(0.2, abs=1e-6)
        assert window.redo_button.isEnabled()
        window.redo()
        assert layer.box[0] == pytest.approx(0.3, abs=0.02)

        window.opacity_slider.setValue(40)
        assert layer.alpha == pytest.approx(0.4)
        window.undo()
        assert layer.alpha == pytest.approx(1.0)

        before = [item.id for item in window.scene.ordered()]
        window.order_buttons[0].click()
        assert [item.id for item in window.scene.ordered()] != before
        window.undo()
        assert [item.id for item in window.scene.ordered()] == before
    finally:
        window.close()
        app.processEvents()


def test_undo_redo_keyboard_nudge(app):
    window = _window()
    try:
        window.preview.set_image(_live_canvas_image())
        layer = window.scene.layer("layer_source")
        layer.box = (0.2, 0.2, 0.4, 0.4)
        window.preview.select_layer(layer.id)
        _pump(app, 0.05)
        _key(window.preview, Qt.Key.Key_Right)
        _pump(app, 0.45)  # debounce the nudge into one undo entry
        assert layer.box[0] == pytest.approx(0.202, abs=1e-6)
        window.undo()
        assert layer.box[0] == pytest.approx(0.2, abs=1e-6)
        window.redo()
        assert layer.box[0] == pytest.approx(0.202, abs=1e-6)
    finally:
        window.close()
        app.processEvents()


def _add_test_layer(window: AnyCamWindow, box=(0.75, 0.2, 0.2, 0.2), z=40) -> Layer:
    layer = Layer("layer_align", "test", "Align", box=box, z=z)
    window.scene.add_layer(layer)
    window._refresh_layers()
    return layer


def test_multi_select_drag_moves_all_selected_layers(app):
    window = _window()
    try:
        window.preview.set_image(_live_canvas_image())
        source = window.scene.layer("layer_source")
        source.box = (0.2, 0.2, 0.2, 0.2)
        other = _add_test_layer(window, box=(0.6, 0.2, 0.2, 0.2))
        window.preview.set_selection([source.id, other.id], source.id)
        _pump(app, 0.05)
        canvas = window.preview.canvas_rect()
        start = window.preview.box_rect(source).center()
        _drag(window.preview, start,
              QPointF(start.x() + canvas.width() * 0.05, start.y()))
        assert source.box[0] == pytest.approx(0.25, abs=0.01)
        assert other.box[0] == pytest.approx(0.65, abs=0.01)
        assert window.preview.selected_ids == [source.id, other.id]

        window.box_spins["x"].setValue(0.4)
        assert source.box[0] == pytest.approx(0.4, abs=0.01)
        assert other.box[0] == pytest.approx(0.8, abs=0.01)
        window.undo()
        assert source.box[0] == pytest.approx(0.25, abs=0.01)
        assert other.box[0] == pytest.approx(0.65, abs=0.01)
        window.redo()
        assert source.box[0] == pytest.approx(0.4, abs=0.01)
        assert other.box[0] == pytest.approx(0.8, abs=0.01)
    finally:
        window.close()
        app.processEvents()


def test_layer_list_multi_selection_syncs_to_preview(app):
    window = _window()
    try:
        source = window.scene.layer("layer_source")
        other = _add_test_layer(window)
        _pump(app, 0.05)
        items = {item.data(Qt.ItemDataRole.UserRole): item
                 for item in (window.layer_list.item(i)
                              for i in range(window.layer_list.count()))}
        window.layer_list.clearSelection()
        window.layer_list.setCurrentItem(items[source.id])
        items[source.id].setSelected(True)
        items[other.id].setSelected(True)
        _pump(app, 0.05)
        assert set(window.preview.selected_ids) == {source.id, other.id}
        assert window.preview.selected_id == source.id
    finally:
        window.close()
        app.processEvents()


def test_snap_toggle_and_tolerance(app):
    window = _window()
    try:
        window.preview.set_image(_live_canvas_image())
        source = window.scene.layer("layer_source")
        source.box = (0.2, 0.2, 0.2, 0.2)
        _add_test_layer(window, box=(0.75, 0.2, 0.2, 0.2))
        window.preview.select_layer(source.id)
        _pump(app, 0.05)
        canvas = window.preview.canvas_rect()
        start = window.preview.box_rect(source).center()
        end = QPointF(start.x() + canvas.width() * 0.303, start.y())

        window.snap_threshold.setValue(1)
        window.snap_check.setChecked(False)
        assert window.preview.snap_enabled is False
        assert window.preview.snap_px == 1
        _drag(window.preview, start, end)
        assert source.box[0] == pytest.approx(0.503, abs=0.002)

        window.undo()
        window.snap_threshold.setValue(6)
        window.snap_check.setChecked(True)
        assert window.preview.snap_px == 6
        _drag(window.preview, start, end)
        assert source.box[0] == pytest.approx(0.5, abs=1e-6)
    finally:
        window.close()
        app.processEvents()


def test_align_shortcuts_align_selected_layers(app):
    window = _window()
    try:
        window.preview.set_image(_live_canvas_image())
        source = window.scene.layer("layer_source")
        source.box = (0.2, 0.2, 0.2, 0.2)
        other = _add_test_layer(window, box=(0.6, 0.3, 0.1, 0.1))
        window.preview.set_selection([source.id, other.id], source.id)
        _pump(app, 0.05)

        def shortcut(sequence: str):
            return next(item for item in window.shortcuts
                        if item.key().toString() == sequence)

        shortcut("Ctrl+Alt+Left").activated.emit()
        assert source.box[0] == pytest.approx(0.0)
        assert other.box[0] == pytest.approx(0.0)
        assert source.box[2] == pytest.approx(0.2)
        assert other.box[2] == pytest.approx(0.1)
        window.undo()
        assert source.box[0] == pytest.approx(0.2)
        assert other.box[0] == pytest.approx(0.6)

        shortcut("Ctrl+Alt+Up").activated.emit()
        assert source.box[1] == pytest.approx(0.0)
        assert other.box[1] == pytest.approx(0.0)
        shortcut("Ctrl+Alt+H").activated.emit()
        assert source.box[0] == pytest.approx(0.4)
        assert other.box[0] == pytest.approx(0.45)
        shortcut("Ctrl+Alt+V").activated.emit()
        assert source.box[1] == pytest.approx(0.4)
        assert other.box[1] == pytest.approx(0.45)
    finally:
        window.close()
        app.processEvents()


def test_undo_redo_add_remove_and_duplicate_layers(app):
    window = _window()
    try:
        base_count = len(window.scene.layers)
        window.add_layer("test")
        added = window.preview.selected_id
        assert added and window.scene.layer(added) is not None
        assert len(window.scene.layers) == base_count + 1

        window.undo()
        assert window.scene.layer(added) is None
        assert len(window.scene.layers) == base_count
        window.redo()
        assert window.scene.layer(added) is not None

        window.preview.select_layer(added)
        _pump(app, 0.05)
        window.remove_layer()
        assert window.scene.layer(added) is None
        window.undo()
        restored = window.scene.layer(added)
        assert restored is not None
        assert restored.kind == "test"

        window.preview.select_layer(added)
        _pump(app, 0.05)
        before_duplicate = len(window.scene.layers)
        window.duplicate_layer()
        copy_id = window.preview.selected_id
        assert copy_id != added
        assert len(window.scene.layers) == before_duplicate + 1
        assert window.scene.layer(copy_id).kind == "test"
        assert window.scene.layer(copy_id).name.endswith("copy")

        window.undo()
        assert window.scene.layer(copy_id) is None
        assert len(window.scene.layers) == before_duplicate
        window.redo()
        assert window.scene.layer(copy_id) is not None
    finally:
        window.close()
        app.processEvents()


def test_duplicate_camera_shares_one_pooled_capture(app, tmp_path, monkeypatch):
    """B16: a duplicated camera layer must not open the device twice."""
    device = tmp_path / "fake-camera"
    device.write_bytes(b"")
    monkeypatch.setattr(
        "anycam.pool.camera_modes",
        lambda _device: [sources.CameraMode("MJPG", 320, 180, 30)],
    )
    window = AnyCamWindow(build_demo_project("testsrc", 320, 180, 30))
    camera = Layer("camera_shared", "camera", "Shared", source=str(device),
                   box=(0.0, 0.0, 1.0, 1.0), z=5)
    window.scene.add_layer(camera)
    window.resize(720, 480)
    window.show()
    window.headless = True
    window._quiet_engine_errors = True
    try:
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        window.preview.select_layer(camera.id)
        window.duplicate_layer()
        assert window.engine is not None, window.statusBar().currentMessage()
        cameras = [entry for entry in window._source_pool.entries()
                   if entry.spec.kind == "camera"]
        assert len(cameras) == 1, [entry.as_dict() for entry in cameras]
        assert cameras[0].refs == 2
    finally:
        window.close()
        app.processEvents()


def test_drag_snaps_and_shows_spacing_guides(app):
    window = _window()
    try:
        window.preview.set_image(_live_canvas_image())
        source = window.scene.layer("layer_source")
        source.box = (0.2, 0.2, 0.2, 0.2)
        _add_test_layer(window, box=(0.75, 0.2, 0.2, 0.2))
        window.preview.select_layer(source.id)
        _pump(app, 0.05)
        canvas = window.preview.canvas_rect()
        start = window.preview.box_rect(source).center()
        _mouse(window.preview, QEvent.Type.MouseButtonPress, start)
        _mouse(window.preview, QEvent.Type.MouseMove,
               QPointF(start.x() + canvas.width() * 0.303, start.y()))
        assert source.box[0] == pytest.approx(0.5, abs=1e-6)
        assert ("v", 0.5) in window.preview._guides
        assert any(guide[0] == "v" for guide in window.preview._spacing)
        _mouse(window.preview, QEvent.Type.MouseButtonRelease,
               QPointF(start.x() + canvas.width() * 0.303, start.y()))
        assert window.preview._guides == []
        assert window.preview._spacing == []
    finally:
        window.close()
        app.processEvents()


def test_cli_screenshot_returns_zero(tmp_path):
    out = tmp_path / "cli.png"
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "anycam", "gui", "--screenshot", str(out),
         "--seconds", "1", "--source", "testsrc"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.exists() and out.stat().st_size > 0
    assert "frame=yes" in proc.stdout


def test_add_image_video_and_network_source_layers(app, tmp_path):
    from PIL import Image

    plate = tmp_path / "plate.png"
    Image.new("RGB", (64, 36), (30, 120, 200)).save(plate)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    window = _window()
    try:
        image_layer = window.add_layer("image", str(plate))
        video_layer = window.add_layer("video", str(clip))
        network_layer = window.add_layer("network", "rtsp://example.test/live")
        assert image_layer is not None and image_layer.kind == "image"
        assert video_layer is not None and video_layer.kind == "video"
        assert network_layer is not None and network_layer.kind == "network"
        assert network_layer.source == "rtsp://example.test/live"
        assert video_layer.source == str(clip)

        count = len(window.scene.layers)
        assert window.add_layer("network", "ftp://example.test/live") is None
        assert len(window.scene.layers) == count
        assert window.add_layer("image", str(tmp_path / "missing.png")) is None
        assert len(window.scene.layers) == count
    finally:
        window.close()
        app.processEvents()


def test_camera_picker_keeps_a_device_per_layer(app, monkeypatch):
    # Use paths that do not exist on the host.  A real /dev/videoN would be
    # canonicalised to its /dev/v4l/by-id name and make this test depend on
    # the machine's current camera numbering.
    desk = "/dev/anycam-desk-cam"
    wide = "/dev/anycam-wide-cam"
    cameras = [
        config.CameraInfo(desk, "Desk Cam", ["MJPG"]),
        config.CameraInfo(wide, "Wide Cam", ["MJPG"]),
        config.CameraInfo("/dev/video10", "AnyCam", ["YUYV"]),
    ]
    monkeypatch.setattr(config, "list_video_devices", lambda: cameras)
    window = _window()
    try:
        first = window.add_layer("camera", desk)
        second = window.add_layer("camera", wide)
        assert first is not None and second is not None
        assert first.source == desk
        assert second.source == wide
        assert first.name == "Desk Cam"
        assert second.name == "Wide Cam"
        # AnyCam's own virtual camera must never be offered as an input.
        assert all(device != "/dev/video10" for _label, device in window._camera_choices)

        window.preview.set_selection([first.id], first.id)
        assert window.camera_combo.isEnabled()
        assert window.camera_combo.currentData() == desk
        window.camera_width.setValue(1280)
        window.camera_height.setValue(720)
        window.camera_fps.setValue(25)
        assert (first.camera_width, first.camera_height, first.camera_fps) == (1280, 720, 25)
        window.camera_combo.setCurrentIndex(
            window.camera_combo.findData(wide))
        assert first.source == wide

        window.preview.set_selection([second.id], second.id)
        assert window.camera_combo.currentData() == wide
        assert (window.camera_width.value(), window.camera_height.value(),
                window.camera_fps.value()) == (0, 0, 0)
        assert (second.camera_width, second.camera_height, second.camera_fps) == (0, 0, 0)
        monkeypatch.setattr(QInputDialog, "getItem",
                            lambda *args, **kwargs: ("Wide Cam · " + wide, True))
        assert window._choose_camera_device() == wide
        assert window.add_layer("camera", desk).source == desk
    finally:
        window.close()
        app.processEvents()


def test_two_camera_scene_preset_positions_both_frames(app, monkeypatch):
    window = _window()
    try:
        monkeypatch.setattr(window, "_scan_camera_choices", lambda: [
            ("Desk Cam · /dev/video2", "/dev/video2"),
            ("Wide Cam · /dev/video3", "/dev/video3"),
        ])
        window.add_two_camera_scene()
        cameras = [layer for layer in window.scene.layers if layer.kind == "camera"]
        assert window.scene.name == "Two cameras"
        assert [layer.source for layer in cameras] == ["/dev/video2", "/dev/video3"]
        assert cameras[0].box == (0.0, 0.0, 0.5, 1.0)
        assert cameras[1].box == (0.5, 0.0, 0.5, 1.0)
        assert "Two-camera scene" in window.statusBar().currentMessage()
    finally:
        window.close()
        app.processEvents()


def test_camera_grid_dialog_prefills_detected_cameras(app, monkeypatch):
    choices = [("Desk Cam · /dev/video2", "/dev/video2"),
               ("Wide Cam · /dev/video3", "/dev/video3"),
               ("Side Cam · /dev/video4", "/dev/video4")]
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *args, **kwargs: warnings.append(args))
    dialog = CameraGridDialog(choices)
    try:
        assert dialog.slots() == ["/dev/video2", "/dev/video3",
                                  "/dev/video4", ""]
        dialog.combos[0].setCurrentIndex(0)
        assert dialog.slots()[0] == ""
        # Duplicate devices are rejected instead of opening the same node twice.
        dialog.combos[0].setCurrentIndex(1)
        dialog.combos[1].setCurrentIndex(1)
        dialog._accept_if_valid()
        assert warnings
        assert dialog.result() != QDialog.DialogCode.Accepted
        # Two cameras are below the three-camera minimum.
        warnings.clear()
        dialog.combos[1].setCurrentIndex(0)
        dialog.combos[2].setCurrentIndex(0)
        dialog._accept_if_valid()
        assert warnings
        assert dialog.result() != QDialog.DialogCode.Accepted
    finally:
        dialog.close()


def test_camera_grid_scene_button_builds_the_selected_slots(app, monkeypatch):
    window = _window()
    monkeypatch.setattr(window, "_scan_camera_choices", lambda: [
        ("Desk Cam · /dev/video2", "/dev/video2"),
        ("Wide Cam · /dev/video3", "/dev/video3"),
        ("Side Cam · /dev/video4", "/dev/video4"),
        ("Back Cam · /dev/video5", "/dev/video5"),
    ])

    class FakeDialog:
        def __init__(self, _choices, _parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def slots(self):
            return ["/dev/video2", "/dev/video3", "", "/dev/video5"]

    monkeypatch.setattr("anycam.gui.CameraGridDialog", FakeDialog)
    try:
        before = len(window.project.scenes)
        window.add_camera_grid_scene()
        assert len(window.project.scenes) == before + 1
        scene = window.scene
        assert scene.name == "Three cameras"
        assert [layer.source for layer in scene.layers] == [
            "/dev/video2", "/dev/video3", "/dev/video5"]
        assert [layer.box for layer in scene.layers] == [
            (0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 0.5, 0.5),
            (0.5, 0.5, 0.5, 0.5)]
        assert "3-camera grid" in window.statusBar().currentMessage()
    finally:
        window.close()
        app.processEvents()


def test_camera_grid_needs_three_devices(app, monkeypatch):
    window = _window()
    monkeypatch.setattr(window, "_scan_camera_choices", lambda: [
        ("Desk Cam · /dev/video2", "/dev/video2"),
        ("Wide Cam · /dev/video3", "/dev/video3"),
    ])
    try:
        before = len(window.project.scenes)
        window.add_camera_grid_scene()
        assert len(window.project.scenes) == before
        assert "Three camera devices" in window.statusBar().currentMessage()
    finally:
        window.close()
        app.processEvents()


def test_camera_loss_drops_layer_but_keeps_preview_alive(app):
    window = _window()
    camera = Layer("layer_lost", "camera", "Lost cam",
                   source="/dev/anycam-missing")
    window.scene.add_layer(camera)
    try:
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        assert window._lost_cameras == {camera.id: camera.source}
        assert camera.id in window.engine.skip_layers
        assert camera.id in window.engine._layer_pads
        assert window.engine.report.camera_placeholders == ["Lost cam"]
        assert window._camera_watch.isActive()
        assert "waiting for camera" in window.statusBar().currentMessage()
        assert _wait_for(app, lambda: window.preview.image is not None)
        assert window.engine._frame_count > 0, "the other layers stopped compositing"
        assert window.engine.report.error == ""
        centre = window.preview.image.pixelColor(
            window.preview.image.width() // 2,
            window.preview.image.height() // 2)
        assert centre.red() + centre.green() + centre.blue() > 0, \
            "the disconnected camera box rendered black instead of a placeholder"

        window.engine.report.error = "Error reading from device"
        window._handle_pipeline_error("Pipeline error: device gone")
        assert window.engine is not None
        assert camera.id in window.engine.skip_layers
        assert camera.id in window.engine._layer_pads
        assert window.engine.report.camera_placeholders == ["Lost cam"]
        assert window._camera_watch.isActive()
        assert "placeholder" in window.statusBar().currentMessage()
    finally:
        window.close()
        app.processEvents()


def test_all_lost_cameras_still_show_placeholder_frames(app):
    window = _window()
    window.scene.layers = []
    devices = ("/dev/anycam-missing-1", "/dev/anycam-missing-2",
               "/dev/anycam-missing-3", "/dev/anycam-missing-4")
    boxes = ((0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 0.5, 0.5),
             (0.0, 0.5, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5))
    ids = []
    for index, (device, box) in enumerate(zip(devices, boxes), start=1):
        layer = Layer(f"camera_{index}", "camera", f"Camera {index}",
                      source=device, box=box, z=index)
        window.scene.add_layer(layer)
        ids.append(layer.id)
    try:
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        assert window.status_chip.text() == "WAITING"
        assert set(window.engine._layer_pads) == set(ids)
        assert window.engine.report.camera_placeholders == [
            "Camera 1", "Camera 2", "Camera 3", "Camera 4"]
        assert _wait_for(app, lambda: window.preview.image is not None)
    finally:
        window.close()
        app.processEvents()


def test_camera_reconnects_when_its_device_returns(app, monkeypatch):
    window = _window()
    back = Layer("layer_back", "camera", "Back", source="/dev/anycam-back")
    gone = Layer("layer_gone", "camera", "Gone", source="/dev/anycam-gone")
    window.scene.add_layer(back)
    window.scene.add_layer(gone)
    window._lost_cameras = {back.id: back.source, gone.id: gone.source}

    class FakeEngine:
        def stop(self):
            return {}

    window.engine = FakeEngine()
    window._camera_watch.start()
    seen = {}

    def fake_restart():
        # The restored camera must no longer be skipped when the graph rebuilds.
        seen["lost"] = set(window._lost_cameras)
        window.engine = FakeEngine()

    monkeypatch.setattr(window, "restart_engine", fake_restart)
    monkeypatch.setattr("anycam.gui.Path.exists",
                        lambda self: str(self) == "/dev/anycam-back")
    try:
        window._reconnect_cameras()
        assert seen["lost"] == {gone.id}
        assert window._lost_cameras == {gone.id: "/dev/anycam-gone"}
        assert window._camera_watch.isActive()
        assert "Camera reconnected: /dev/anycam-back" in window.statusBar().currentMessage()
    finally:
        window.engine = None
        window.close()
        app.processEvents()


def test_camera_reconnect_retries_with_backoff_when_restart_fails(app, monkeypatch):
    """A present-but-wedged UVC device must not stop the reconnect watch."""
    window = _window()
    camera = Layer("layer_retry", "camera", "Retry", source="/dev/anycam-retry")
    window.scene.add_layer(camera)
    window._lost_cameras = {camera.id: camera.source}
    window.engine = object()
    calls = []

    def failing_restart():
        calls.append(1)
        window.engine = None

    monkeypatch.setattr(window, "restart_engine", failing_restart)
    monkeypatch.setattr("anycam.gui.Path.exists", lambda self: True)
    try:
        window._reconnect_cameras()
        assert calls == [1]
        assert window._lost_cameras == {camera.id: camera.source}
        assert window._camera_watch.isActive()
        assert window._camera_watch.interval() > 1200
        assert "retrying" in window.statusBar().currentMessage().lower()
    finally:
        window.engine = None
        window.close()
        app.processEvents()


def test_camera_source_error_quarantines_an_existing_device(app):
    """v4l2 errors after too many opens are treated as temporary loss."""
    window = _window()
    camera = Layer("layer_wedged", "camera", "Wedged", source="/dev/anycam-wedged")
    window.scene.add_layer(camera)
    try:
        assert window._camera_error_layers(
            "v4l2src: Device or resource busy") == {
                camera.id: "/dev/anycam-wedged"}
        assert window._camera_error_layers("compositor: not-linked") == {}
    finally:
        window.engine = None
        window.close()
        app.processEvents()


def test_held_vcam_hint_names_the_app_that_opened_first(app, monkeypatch):
    class HeldVcam:
        active = True
        error = ""
        pixel_format = "YUY2"
        device = "/dev/video10"

    monkeypatch.setattr(config, "vcam_consumers", lambda device=None: ["discord"])
    window = _window()
    try:
        window._vcam_output = HeldVcam()
        window.note_vcam_consumers()
        assert not window.vcam_hint.isHidden()
        text = window.vcam_hint.text()
        assert "discord" in text
        assert "re-select" in text
        window.dismiss_vcam_hint()
        assert window.vcam_hint.isHidden()
        assert window.vcam_dismiss.isHidden()
    finally:
        window._vcam_output = None
        window.close()
        app.processEvents()


def test_vcam_toggle_starts_the_preview_it_needs(app, monkeypatch):
    """The button must not leave a loopback device with nothing to push."""

    class FakeVcam:
        def __init__(self):
            self.active = False
            self.error = ""
            self.pixel_format = "YUY2"

        def start(self, device, width, height, fps, setup=False, backend="v4l2"):
            self.device = device
            self.active = True
            return True

        def push_sample(self, _sample):
            return None

        def poll(self):
            return None

        def stop(self):
            self.active = False
            return {"device": self.device, "ok": True, "error": ""}

    monkeypatch.setattr("anycam.gui.VcamOutput", FakeVcam)
    window = _window()
    try:
        assert window.engine is None
        window.toggle_vcam()
        assert window.vcam_on is True
        # No canvas means no frames: a consumer reading /dev/video10 fails
        # with EIO instead of showing a camera.
        assert window.engine is not None, window.statusBar().currentMessage()
        assert window.engine.report.vcam_active is True
    finally:
        window.close()
        app.processEvents()


def test_vcam_output_survives_engine_restarts(app, monkeypatch):
    created = []

    class FakeVcam:
        def __init__(self):
            self.active = False
            self.error = ""
            self.pixel_format = "YUY2"
            self.stopped = False
            created.append(self)

        def start(self, device, width, height, fps, setup=False, backend="v4l2"):
            self.device = device
            self.width, self.height, self.fps = width, height, fps
            self.setup = setup
            self.active = True
            return True

        def push_sample(self, _sample):
            return None

        def poll(self):
            return None

        def stop(self):
            self.active = False
            self.stopped = True
            return {"device": self.device, "ok": True, "error": ""}

    monkeypatch.setattr("anycam.gui.VcamOutput", FakeVcam)
    window = _window()
    try:
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        first = window.engine
        window.toggle_vcam()
        assert window.vcam_on is True
        assert window.engine is first, "toggling vcam rebuilt the canvas"
        assert len(created) == 1
        output = created[0]
        assert output.setup is True, "the button must also create the device"
        window.restart_engine()
        assert len(created) == 1, "a rebuild created a second v4l2sink"
        assert window._vcam_output is output
        assert window.engine is not first
        window.toggle_vcam()
        assert window.vcam_on is False
        # The node stays held (idle card) so the next consumer can select it;
        # only the live canvas tap is detached.
        assert output.stopped is False
        assert window._vcam_output is output
    finally:
        window.close()
        app.processEvents()


def test_vcam_sink_error_is_rebuilt_not_turned_off(app, monkeypatch):
    """One dead v4l2sink must not disable the user's camera for the session.

    A latched sink error made every later rebuild build without the tap, so
    the held node stayed on the dark ready card while the canvas kept running.
    """
    created = []

    class FakeVcam:
        def __init__(self):
            self.active = False
            self.error = ""
            self.pixel_format = "YUY2"
            created.append(self)

        def start(self, device, width, height, fps, setup=False, backend="v4l2"):
            self.device = device
            self.width, self.height, self.fps = width, height, fps
            if len(created) == 1:
                self.error = "Error in IOCTL: Invalid argument"
                return False
            self.active = True
            return True

        def push_sample(self, _sample):
            return None

        def poll(self):
            return None

        def stop(self):
            self.active = False
            return {"device": self.device, "ok": True, "error": ""}

        def set_canvas_attached(self, _attached):
            return None

    monkeypatch.setattr("anycam.gui.VcamOutput", FakeVcam)
    window = _window()
    try:
        window.vcam_on = True
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        # The first output failed; the request survives and the retry built a
        # second output and attached it without a button press.
        assert window.vcam_on is True
        assert len(created) == 2
        assert window._vcam_output is created[-1]
        assert window.engine.report.vcam_active is True
        assert window.vcam_button.text() == "Virtual camera: on"
    finally:
        window.close()
        app.processEvents()


def test_screen_source_fps_reports_the_capture_not_the_canvas(app):
    """The status line must show captured frames, not just the 60 fps canvas."""
    from types import SimpleNamespace

    window = _window()
    try:
        layer = Layer("screen_fps", "screen", "Screen", source="1")
        window.scene.add_layer(layer)
        health = {layer.id: {"frames": 100}}
        window.engine = SimpleNamespace(
            report=SimpleNamespace(source_health=health))
        assert window._screen_source_fps() is None  # no previous sample yet
        health[layer.id]["frames"] = 160
        window._screen_fps_prev = (100, time.monotonic() - 1.0)
        assert window._screen_source_fps() == pytest.approx(60, rel=0.2)
    finally:
        window.engine = None
        window.close()
        app.processEvents()


def test_add_screen_layer_keeps_portal_session(app, monkeypatch):
    class FakeSession:
        def __init__(self):
            self.streams = [portal.ScreenStream(123, 640, 360)]
            self.fd = 12
            self.closed = False

        @property
        def node_id(self):
            return self.streams[0].node_id

        def take_remote(self):
            fd, self.fd = self.fd, -1
            return fd

        def close(self):
            self.closed = True

    session = FakeSession()
    monkeypatch.setattr(portal, "request_screen_cast", lambda **_kwargs: session)
    window = _window()
    window.headless = True
    try:
        layer = window.add_layer("screen")
        assert layer is not None and layer.kind == "screen"
        assert layer.source == "123"
        assert window._screen_sessions[layer.id] is session
        assert window._screen_fds() == {layer.id: 12}
        window._close_screen_session(layer.id)
        assert session.closed is True
        assert window._screen_fds() == {}
    finally:
        window.close()
        app.processEvents()


def test_screen_fds_are_renewed_for_each_canvas_build(app):
    """A screen rebuild must not reuse the fd pipewiresrc already consumed."""
    class FakeSession:
        def __init__(self):
            self.fd = 12
            self.next_fd = 20
            self.calls = 0

        def take_remote(self):
            self.calls += 1
            if self.fd >= 0:
                fd, self.fd = self.fd, -1
                return fd
            fd = self.next_fd
            self.next_fd += 1
            return fd

        def close(self):
            pass

    window = _window()
    try:
        layer = Layer("screen_build", "screen", "Screen", source="42")
        window.scene.add_layer(layer)
        session = FakeSession()
        window._screen_sessions[layer.id] = session
        assert window._screen_fds() == {layer.id: 12}
        assert window._screen_fds() == {layer.id: 20}
        assert session.calls == 2
    finally:
        window.close()
        app.processEvents()


def test_playlist_plays_once_and_loops(app):
    class FakeEngine:
        def __init__(self):
            self.visible = {}

        def set_layer_visible(self, layer_id, visible):
            self.visible[layer_id] = visible

        def stop(self):
            return {}

    window = _window()
    try:
        first = window.scene.layers[0].id
        second = window.scene.layers[1].id
        window.playlist_ids = [first, second]
        window.playlist_index = -1
        window.playlist_interval.setValue(1.0)
        window.playlist_enabled.setChecked(False)
        window.playlist_last = 0.0
        window.engine = FakeEngine()

        window._advance_playlist(10.0)
        assert window.playlist_index == 0
        window._advance_playlist(11.0)
        assert window.playlist_index == 1
        window._advance_playlist(12.0)
        assert window.playlist_index == 1, "loop-off playlist must stop on the last item"

        window.playlist_enabled.setChecked(True)
        window.playlist_last = 12.0
        window._advance_playlist(13.0)
        assert window.playlist_index == 0
        assert window.engine.visible[first] is True
        assert window.engine.visible[second] is False
    finally:
        window.engine = None
        window.close()
        app.processEvents()


def test_playlist_snap_and_autosave_persist(app, tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    plate = tmp_path / "plate.png"
    Image.new("RGB", (64, 36), (30, 120, 200)).save(plate)

    window = AnyCamWindow()
    window.show()
    try:
        layer = window.add_layer("image", str(plate))
        assert layer is not None
        window.playlist_ids = [layer.id]
        window.playlist_index = 0
        window.playlist_interval.setValue(2.5)
        window.playlist_enabled.setChecked(False)
        window._store_playlist_state()
        window.snap_check.setChecked(False)
        window.snap_threshold.setValue(11)
        window._autosave()

        window.add_scene()
        assert window.playlist_ids == []
        window.switch_scene(0)
        assert window.playlist_ids == [layer.id]

        loaded = Project.load(config.project_path())
        assert loaded.editor.snap_enabled is False
        assert loaded.editor.snap_px == 11.0
        assert loaded.scene.playlist.layer_ids == [layer.id]
        assert loaded.scene.playlist.interval == 2.5
        assert loaded.scene.playlist.loop is False
        assert loaded.scene.playlist.index == 0
    finally:
        window.close()
        app.processEvents()

    restored = AnyCamWindow()
    try:
        assert restored.preview.snap_enabled is False
        assert restored.preview.snap_px == 11.0
        assert restored.playlist_ids == [layer.id]
        assert restored.playlist_interval.value() == 2.5
        assert restored.playlist_enabled.isChecked() is False
        assert restored.playlist_index == 0
    finally:
        restored.close()
        app.processEvents()


def test_effects_follow_selection_and_update_without_a_rebuild(app):
    window = _window()

    class FakeEngine:
        def __init__(self):
            self.calls = []

        def set_layer_effects(self, layer_id, effects):
            self.calls.append((layer_id, effects))
            return True

    camera = next(layer for layer in window.scene.layers if layer.kind == "test")
    selected = next(layer for layer in window.scene.layers if layer.kind == "text")
    fake = FakeEngine()
    window.engine = fake
    try:
        window.preview.set_selection([selected.id], selected.id)
        app.processEvents()
        assert window.effects_target.text().startswith(selected.name)

        window._set_effect("brightness", 25)
        assert selected.effects.brightness == pytest.approx(0.25)
        assert camera.effects.brightness == 0.0
        assert fake.calls[-1][0] == selected.id

        window.effect_preset.setCurrentIndex(
            window.effect_preset.findData("Mono"))
        assert selected.effects.saturation == 0.0
        assert fake.calls[-1][0] == selected.id
    finally:
        window.engine = None
        window.close()
        app.processEvents()


def test_window_frame_controls_update_the_selected_layer(app):
    window = _window()

    class FakeEngine:
        def __init__(self):
            self.calls = []

        def set_layer_effects(self, layer_id, effects):
            self.calls.append((layer_id, effects))
            return True

    selected = next(layer for layer in window.scene.layers
                    if layer.kind == "test")
    fake = FakeEngine()
    window.engine = fake
    try:
        window.preview.set_selection([selected.id], selected.id)
        app.processEvents()
        window.frame_style.setCurrentIndex(
            window.frame_style.findData("neon"))
        app.processEvents()
        assert selected.effects.frame_style == "neon"
        assert fake.calls[-1][0] == selected.id
        assert window.frame_color.isEnabled()

        window._set_effect("frame_width", 12)
        assert selected.effects.frame_width == 12
        assert fake.calls[-1][1] is selected.effects

        window.frame_style.setCurrentIndex(
            window.frame_style.findData("none"))
        app.processEvents()
        assert selected.effects.frame_style == "none"
        assert not window.frame_color.isEnabled()
    finally:
        window.engine = None
        window.close()
        app.processEvents()


def test_output_tab_saves_the_encoder_choice(app):
    window = _window()
    try:
        assert window.encoder_combo.findData("nvenc_h265") >= 0
        assert window.encoder_combo.findData("nvenc_av1") >= 0
        index = window.encoder_combo.findData("cpu")
        assert index >= 0
        window.encoder_combo.setCurrentIndex(index)
        app.processEvents()
        assert window.project.output.encoder == "cpu"
        assert window.encoder_combo.currentData() == "cpu"
    finally:
        window.close()
        app.processEvents()


def test_backdrop_tab_replaces_static_and_3d_backdrops(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    window = _window()
    try:
        window._apply_background("Aurora")
        static = [layer for layer in window._active_backdrops()
                  if layer.visible]
        assert len(static) == 1
        assert static[0].kind == "image"
        assert static[0].role == "backdrop"

        window._apply_animation("tunnel")
        motion = [layer for layer in window._active_backdrops()
                  if layer.visible]
        assert len(motion) == 1
        assert motion[0].kind == "animation"
        assert motion[0].source == "tunnel"
        # The replaced backdrop is deleted, not left behind hidden.
        assert static[0] not in window.scene.layers
        assert len(window._active_backdrops()) == 1

        window._clear_backdrop()
        assert window._active_backdrops() == []
    finally:
        window.close()
        app.processEvents()


def test_unsaved_layers_are_saved_on_close(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    window = AnyCamWindow()
    window.show()
    layer = window.add_layer("network", "http://example.test/stream")
    assert layer is not None
    window.close()
    app.processEvents()

    loaded = Project.load(config.project_path())
    assert any(item.id == layer.id and item.kind == "network"
               for item in loaded.scene.layers)


# ----------------------------------------------- 2026-11-07 audit: window fixes
def test_changing_the_canvas_while_recording_starts_a_new_file(app, tmp_path):
    """One MP4 has one picture size; the old file silently rejected every frame."""
    window = _window()
    window.project.output.record_dir = str(tmp_path)
    try:
        window.toggle_record()
        assert window.recording and window._recorder is not None
        first = window._recorder
        assert first.requested_size == (320, 180)
        window.width_spin.setValue(640)
        window.height_spin.setValue(360)
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        assert window._recorder is not first, "the recorder kept the old size"
        assert window._recorder.requested_size == (640, 360)
        assert window._segment_index == 1
        assert Path(first.path) in tmp_path.iterdir() or Path(first.path).exists()
    finally:
        window.recording = False
        window.stop_engine()
        window._finish_recorder()
        window.close()


def test_the_output_tab_accepts_the_rates_and_sizes_the_project_can_hold(app):
    window = _window()
    try:
        assert window.fps_spin.maximum() >= 120
        assert window.width_spin.maximum() >= 4096
    finally:
        window.close()


def test_scene_shortcut_runs_the_same_bookkeeping_as_the_scene_picker(app):
    window = _window()
    try:
        window.add_scene()
        assert window.project.active_scene == window.project.scenes[1].id
        synced = []
        original = window._sync_layout_combo
        window._sync_layout_combo = lambda: (synced.append(True), original())[1]
        window.switch_scene(0)
        assert window.project.active_scene == window.project.scenes[0].id
        assert synced, "the layout picker was left showing the previous scene"
    finally:
        window.close()


def test_first_playlist_item_is_not_shown_for_two_intervals(app):
    window = _window()
    try:
        first, second = window.scene.layers[0], Layer(
            "second", "test", "Second", source="smpte", box=(0, 0, 1, 1), z=2)
        window.scene.add_layer(second)
        first.visible, second.visible = True, False
        window.playlist_ids = [first.id, second.id]
        window.playlist_index = -1
        window.start_engine()
        assert window.engine is not None, window.statusBar().currentMessage()
        assert window.playlist_index == 0, "item 0 is already on screen"
        window.playlist_interval.setValue(1.0)
        window._advance_playlist(window.playlist_last + 1.5)
        assert window.playlist_index == 1
    finally:
        window.stop_engine()
        window.close()


def test_a_codec_change_restarts_the_canvas_instead_of_a_broken_attach(app, tmp_path, monkeypatch):
    monkeypatch.delenv("ANYCAM_ENCODER", raising=False)
    window = _window()
    window.project.output.record_dir = str(tmp_path)
    try:
        window.project.output.encoder = "cpu"
        window.start_engine()
        first = window.engine
        assert first is not None
        # Build the recording tap with H.264, then ask for another codec.
        window.toggle_record()
        window.toggle_record()                    # stop; the tap stays in place
        assert first.record_codec() == "h264"
        monkeypatch.setattr("anycam.gui.recording_codec", lambda _pref: "h265")
        window.toggle_record()
        assert window.engine is not first, "the stale H.264 tap was reused"
    finally:
        window.recording = False
        window.stop_engine()
        window._finish_recorder()
        window.close()


# --------------------------------------------- 2026-11-07 live-check fixes
def test_add_layer_puts_an_image_into_the_box_it_was_given(app, tmp_path):
    """A layout slot's "Add image…" landed in the corner: the box was overwritten."""
    from PIL import Image

    picture = tmp_path / "pic.png"
    Image.new("RGB", (64, 64), (200, 0, 0)).save(picture)
    clip = ROOT / "tests" / "data" / "armstrong.jpg"
    window = _window()
    try:
        image = window.add_layer("image", source=str(picture), box=(0.5, 0.0, 0.5, 1.0))
        assert tuple(image.box) == (0.5, 0.0, 0.5, 1.0)
        logo = window.add_layer("logo", source=str(clip))
        assert tuple(logo.box) == (0.04, 0.04, 0.18, 0.18), "the default spot is unchanged"
    finally:
        window.close()


def test_the_default_window_offers_backdrops(app):
    from anycam.gui_many import ManyCamWindow
    from anycam.model import empty_project

    window = ManyCamWindow(empty_project())
    try:
        assert hasattr(window, "inset_on_backdrop"), "the Backdrops tab was never built"
        window.add_layer("test")
        window._apply_background("Aurora")
        assert len(window._active_backdrops()) == 1
        window._clear_backdrop()
        assert not window._active_backdrops()
    finally:
        window.close()


def test_changing_camera_width_height_and_rate_rebuilds_once(app, monkeypatch):
    from types import SimpleNamespace

    window = _window()
    try:
        camera = Layer("cam", "camera", "Cam", source="/dev/video0", box=(0, 0, 1, 1))
        window.scene.add_layer(camera)
        window._refresh_layers()
        window.preview.set_selection([camera.id], camera.id)
        rebuilds = []
        window.engine = SimpleNamespace(stop=lambda: {})
        monkeypatch.setattr(window, "restart_engine", lambda: rebuilds.append(1))
        window.camera_width.setValue(1280)
        window.camera_height.setValue(720)
        window.camera_fps.setValue(30)
        assert (camera.camera_width, camera.camera_height, camera.camera_fps) == (1280, 720, 30)
        assert not rebuilds, "nothing reopens the camera per field"
        _wait_for(app, lambda: bool(rebuilds), 2.0)
        _pump(app, 0.5)
        assert len(rebuilds) == 1
    finally:
        window.engine = None
        window.close()


# ------------------------------------------------------------ phone camera
@pytest.fixture
def phone_link(monkeypatch, tmp_path):
    """The window's phone link on loopback with throwaway pairing state."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    from anycam.phone import server

    def local_service(on_event=None):
        return server.PhoneService(state_dir=tmp_path / "state",
                                   cert_dir=tmp_path / "cert", host="127.0.0.1",
                                   port=0, on_event=on_event)

    monkeypatch.setattr(server, "default_service", local_service)
    return local_service


def test_add_phone_camera_shows_a_qr_and_adds_the_phone_that_streams(app, phone_link):
    from PySide6.QtCore import QTimer

    from anycam.phone_dialog import PhonePairDialog

    window = _window()
    seen: list[PhonePairDialog] = []
    logged: list[str] = []
    window.log_event = lambda message: logged.append(message)
    try:
        def phone_arrives():
            dialog = window._phone_dialog
            assert dialog is not None, "the QR dialog is open"
            seen.append(dialog)
            service = window._phone_service
            token = dialog.service.pairing_url().split("#pair=", 1)[1]
            device, _secret = service.pairing.pair(token, "Pixel 8")
            # Events come from network threads in real use.
            service.emit("paired", device)
            service.emit("connecting", device)
            service.emit("streaming", device)

        QTimer.singleShot(200, phone_arrives)
        before = len(window.scene.layers)
        layer = window.add_layer("phone")
        assert seen and seen[0].qr.pixmap() is not None or "QR" in seen[0].qr.text()
        assert layer is not None and layer.kind == "phone"
        assert layer.name == "Pixel 8"
        assert layer.source.startswith("phone-")
        assert len(window.scene.layers) == before + 1
        assert any("Pixel 8 is live" in line for line in logged)
        # The same phone again (it reconnected) is not added twice.
        assert window.add_layer("phone", layer.source) is layer
        assert len(window.scene.layers) == before + 1
    finally:
        window.close()
        app.processEvents()
    assert window._phone_service is None, "closing the window stops the phone link"


def test_cancelling_the_phone_dialog_adds_nothing(app, phone_link):
    from PySide6.QtCore import QTimer

    window = _window()
    try:
        QTimer.singleShot(200, lambda: window._phone_dialog.reject())
        before = len(window.scene.layers)
        assert window.add_layer("phone") is None
        assert len(window.scene.layers) == before
        assert window._phone_service is not None and window._phone_service.running
    finally:
        window.close()
        app.processEvents()


def test_a_running_canvas_takes_a_phone_layer(app, phone_link):
    window = _window()
    try:
        window.start_engine()
        assert window.engine is not None
        layer = window.add_layer("phone", "phone-canvas1")
        assert layer is not None
        assert _wait_for(app, lambda: any(
            entry.spec.kind == "phone" and entry.spec.source == "phone-canvas1"
            for entry in window._source_pool.entries()), 5.0)
        assert window._phone_service is not None, \
            "a scene with a phone keeps the link listening for it"
    finally:
        window.close()
        app.processEvents()


def test_microphone_noise_suppression_controls(app):
    window = _window()
    try:
        assert window.denoise_combo.currentData() == window.project.audio.mic_denoise
        restarts = []
        window.restart_engine = lambda: restarts.append(True)
        window.engine = object()  # a running canvas
        window.project.audio.mic_enabled = True
        window.denoise_combo.setCurrentIndex(window.denoise_combo.findData("very-high"))
        assert window.project.audio.mic_denoise == "very-high"
        window.auto_gain_check.setChecked(True)
        assert window.project.audio.mic_auto_gain is True
        assert len(restarts) == 2, "webrtcdsp reads its settings at start: rebuild"
        window.project.audio.mic_enabled = False
        window.denoise_combo.setCurrentIndex(window.denoise_combo.findData("off"))
        assert window.project.audio.mic_denoise == "off"
        assert len(restarts) == 2, "no rebuild while the microphone is off"
    finally:
        window.engine = None
        window.close()
        app.processEvents()


def test_ai_effects_device_setting(app):
    from anycam import inference

    window = _window()
    try:
        assert window.effects_device_combo.currentData() == "cpu"
        window.effects_device_combo.setCurrentIndex(
            window.effects_device_combo.findData("gpu"))
        assert window.project.output.effects_device == "gpu"
        assert inference.preference() == "gpu"
        assert "running on" in window.effects_device_note.text()
    finally:
        inference.set_preference("cpu")
        window.close()
        app.processEvents()


def test_person_cut_out_model_choice_and_download(app, monkeypatch, tmp_path):
    from anycam import models as model_catalog
    from anycam import segment

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    window = _window()
    try:
        assert window.segmentation_combo.currentData() == "mediapipe"
        window.segmentation_combo.setCurrentIndex(window.segmentation_combo.findData("rvm"))
        assert window.project.output.segmentation_model == "rvm"
        assert not window.model_download_button.isHidden(), "not downloaded yet"
        assert segment.active_model() == "mediapipe"

        fetched = []

        def fake_download(key, progress=None, timeout=30.0):
            progress(5, 10)
            fetched.append(key)
            return tmp_path / "m.onnx"

        monkeypatch.setattr(model_catalog, "download", fake_download)
        window._download_rvm()
        assert _wait_for(app, lambda: window._model_download is None, 5.0)
        assert fetched == ["rvm"]
        assert "downloaded" in window.statusBar().currentMessage().lower()
    finally:
        segment.set_model("mediapipe")
        window.close()
        app.processEvents()


def test_allow_phones_uses_firewalld_over_dbus(app, monkeypatch):
    from anycam.phone import firewall
    from anycam.phone_dialog import PhonePairDialog

    state = {"open": False}
    monkeypatch.setattr(firewall, "backend", lambda: "firewalld")
    monkeypatch.setattr(firewall, "blocks", lambda port, host="": not state["open"])
    monkeypatch.setattr(firewall, "firewalld_blocks", lambda port, host="": not state["open"])
    calls = []

    def allow(port, host=""):
        calls.append((port, host))
        state["open"] = True
        return ""

    monkeypatch.setattr(firewall, "firewalld_allow", allow)
    service = type("Service", (), {"port": 8443, "host": "192.168.1.20", "fingerprint": "AA",
                                   "pairing_url": lambda self: "https://192.168.1.20:8443/#pair=x",
                                   "page_url": lambda self: "https://192.168.1.20:8443/"})()
    dialog = PhonePairDialog(service)
    try:
        assert not dialog.firewall_bar.isHidden()
        dialog.allow_through_firewall()
        assert _wait_for(app, lambda: dialog.firewall_bar.isHidden(), 5.0)
        assert calls == [(8443, "192.168.1.20")]
    finally:
        dialog.close()
