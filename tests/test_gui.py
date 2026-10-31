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

from anycam import config, portal
from anycam.demo import build_demo_project
from anycam.gui import AnyCamWindow, CameraGridDialog
from anycam.model import Layer, Project

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
        crop = window.preview.crop_rect(layer)
        left_edge = QPointF(crop.left(), crop.center().y())
        _drag(window.preview, left_edge,
              QPointF(left_edge.x() + canvas.width() * 0.04, left_edge.y()))
        assert layer.crop[0] == pytest.approx(0.14, abs=0.015)
        assert layer.crop[2] == pytest.approx(0.1, abs=0.005)

        # Panning the crop window keeps its size and moves both insets.
        crop = window.preview.crop_rect(layer)
        middle = crop.center()
        _drag(window.preview, middle,
              QPointF(middle.x() + canvas.width() * 0.03, middle.y()))
        assert layer.crop[0] > 0.14
        assert layer.crop[2] < 0.1
        assert layer.crop[0] + layer.crop[2] == pytest.approx(0.24, abs=0.01)

        # Dragging past the canvas edge clamps instead of producing a
        # negative far inset.
        crop = window.preview.crop_rect(layer)
        _drag(window.preview, crop.center(),
              QPointF(canvas.right(), crop.center().y()))
        assert layer.crop[2] >= 0.0
        assert layer.crop[0] + layer.crop[2] == pytest.approx(0.24, abs=0.01)

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


def test_layer_edit_while_recording_finalises_both_segments(app, tmp_path, monkeypatch):
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
        segments = [Path(path) for path in window.recording_segments]
        assert len(segments) == 2, segments
        for path in segments:
            assert path.exists() and path.stat().st_size > 0
            if shutil.which("ffprobe"):
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(path)],
                    capture_output=True, text=True,
                )
                assert probe.returncode == 0, probe.stderr
                assert float(probe.stdout.strip()) > 0.2
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
    cameras = [
        config.CameraInfo("/dev/video2", "Desk Cam", ["MJPG"]),
        config.CameraInfo("/dev/video3", "Wide Cam", ["MJPG"]),
        config.CameraInfo("/dev/video10", "AnyCam", ["YUYV"]),
    ]
    monkeypatch.setattr(config, "list_video_devices", lambda: cameras)
    window = _window()
    try:
        first = window.add_layer("camera", "/dev/video2")
        second = window.add_layer("camera", "/dev/video3")
        assert first is not None and second is not None
        assert first.source == "/dev/video2"
        assert second.source == "/dev/video3"
        assert first.name == "Desk Cam"
        assert second.name == "Wide Cam"
        # AnyCam's own virtual camera must never be offered as an input.
        assert all(device != "/dev/video10" for _label, device in window._camera_choices)

        window.preview.set_selection([first.id], first.id)
        assert window.camera_combo.isEnabled()
        assert window.camera_combo.currentData() == "/dev/video2"
        window.camera_width.setValue(1280)
        window.camera_height.setValue(720)
        window.camera_fps.setValue(25)
        assert (first.camera_width, first.camera_height, first.camera_fps) == (1280, 720, 25)
        window.camera_combo.setCurrentIndex(
            window.camera_combo.findData("/dev/video3"))
        assert first.source == "/dev/video3"

        window.preview.set_selection([second.id], second.id)
        assert window.camera_combo.currentData() == "/dev/video3"
        assert (window.camera_width.value(), window.camera_height.value(),
                window.camera_fps.value()) == (0, 0, 0)
        assert (second.camera_width, second.camera_height, second.camera_fps) == (0, 0, 0)
        monkeypatch.setattr(QInputDialog, "getItem",
                            lambda *args, **kwargs: ("Wide Cam · /dev/video3", True))
        assert window._choose_camera_device() == "/dev/video3"
        assert window.add_layer("camera", "/dev/video2").source == "/dev/video2"
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


def test_add_screen_layer_keeps_portal_session(app, monkeypatch):
    class FakeSession:
        def __init__(self):
            self.streams = [portal.ScreenStream(123, 640, 360)]
            self.fd = 12
            self.closed = False

        @property
        def node_id(self):
            return self.streams[0].node_id

        def close(self):
            self.closed = True

    session = FakeSession()
    monkeypatch.setattr(portal, "request_screen_cast", lambda: session)
    window = _window()
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


def test_face_overlay_follows_detector_and_can_be_disabled(app, monkeypatch):
    from anycam import face as face_module

    class FakeDetector:
        faces = [face_module.Face(0.2, 0.3, 0.1, 0.1, 0.95)]

        def detect(self, frame):
            assert frame.shape[2] == 3
            return list(type(self).faces)

    monkeypatch.setattr(face_module, "FaceDetector", FakeDetector)
    window = _window()
    try:
        camera = next(layer for layer in window.scene.layers
                      if layer.kind in ("camera", "test", "video", "screen", "network"))
        camera.effects.face_effect = "frame"
        assert window._sync_face_overlay() is True
        face_layer = window.scene.layer(window._face_layer_id)
        assert face_layer is not None and face_layer.kind == "face"
        assert face_layer.visible is False

        class FakeEngine:
            def __init__(self):
                self.boxes = {}
                self.visible = {}

            def set_layer_box(self, layer_id, box):
                self.boxes[layer_id] = box
                window.scene.layer(layer_id).box = box

            def set_layer_visible(self, layer_id, visible):
                self.visible[layer_id] = visible
                window.scene.layer(layer_id).visible = visible

        engine = FakeEngine()
        window.engine = engine
        window._frame_bytes = bytes(320 * 180 * 4)
        window._face_size = (320, 180)
        window._face_last = 0.0
        window._track_face()
        assert engine.visible[face_layer.id] is True
        box = engine.boxes[face_layer.id]
        assert box[0] < 0.2 and box[1] < 0.3
        assert box[2] > 0.1 and box[3] > 0.1

        FakeDetector.faces = []
        window._face_last = 0.0
        window._track_face()
        assert engine.visible[face_layer.id] is False

        camera.effects.face_effect = "none"
        assert window._sync_face_overlay() is True
        assert window.scene.layer(face_layer.id) is None
    finally:
        window.engine = None
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
