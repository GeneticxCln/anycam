"""Regression tests for the ManyCam-style layout and layer stacking order.

The window is driven on Qt's offscreen platform with temp XDG directories, so
these tests never open a camera or touch the user's project file.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF
from PySide6.QtGui import QContextMenuEvent, QImage
from PySide6.QtWidgets import QApplication

from anycam import portal
from anycam.gui import AnyCamWindow, PreviewWidget
from anycam.gui_many import ManyCamWindow
from anycam.layouts import LAYOUT_PRESETS, layout_preset
from anycam.model import (
    Layer, Project, Scene, default_project, empty_project, new_id,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


def _window(project, app) -> ManyCamWindow:
    window = ManyCamWindow(project)
    window.headless = True
    window._persist = False
    app.processEvents()
    return window


def _close(window: ManyCamWindow, app) -> None:
    window._persist = False
    window.close()
    app.processEvents()


def _two_scene_project():
    project = default_project()
    second = Scene(new_id("scene"), "Second")
    project.scenes.append(second)
    return project, second


def _camera_project(count: int, kind: str = "test") -> Project:
    project = Project(name="grid")
    scene = Scene(new_id("scene"), "Grid")
    for index in range(count):
        scene.add_layer(Layer(
            new_id("layer"), kind, f"Cam {index}", source="ball",
            box=(0.62, 0.60, 0.34, 0.34), z=index + 1))
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


class _PadEngine:
    """Records the only mutations a layout switch is allowed to make."""

    def __init__(self) -> None:
        self.boxes: dict[str, tuple] = {}
        self.visible: dict[str, bool] = {}
        self.detached: list[str] = []

    def set_layer_box(self, layer_id: str, box) -> None:
        self.boxes[layer_id] = tuple(box)

    def detach_layer(self, layer_id: str) -> None:
        self.detached.append(layer_id)
        self.visible[layer_id] = False

    def set_layer_visible(self, layer_id: str, visible: bool) -> None:
        self.visible[layer_id] = bool(visible)

    def stop(self) -> dict:
        return {}


def test_diagnostics_log_records_vcam_and_screen_events(app):
    """Rebuild/recovery events must be visible inside the app, not only stderr."""
    window = _window(empty_project(), app)
    try:
        window.log_event("screen capture reused after a rebuild (node 86)")
        window.log_event("virtual camera: recovered and re-attached to the canvas")
        text = window.diag_log.toPlainText()
        assert "screen capture reused after a rebuild" in text
        assert "virtual camera: recovered" in text
    finally:
        _close(window, app)


def test_held_vcam_follows_the_canvas_without_a_second_click(app):
    """Launch holds the node; starting the preview must replace the card."""
    window = _window(empty_project(), app)

    class Held:
        active = True
        device = "/dev/video10"

        def stop(self):
            self.active = False
            return {"device": self.device, "ok": True, "error": ""}

    try:
        assert window.feed_held_vcam() is False
        window._vcam_output = Held()
        assert window.feed_held_vcam() is True
        assert window.vcam_on is True
        assert "on" in window.vcam_button.text()
    finally:
        window._vcam_output = None
        _close(window, app)


def test_manycam_window_shows_a_held_vcam_hint(app, monkeypatch):
    """The default UI used to lack the banner, so the hint crashed it."""
    import anycam.gui_many as many

    class HeldVcam:
        active = True
        device = "/dev/video10"

    monkeypatch.setattr(many.config, "vcam_consumers",
                        lambda device=None: ["discord"])
    window = _window(empty_project(), app)
    try:
        window._vcam_output = HeldVcam()
        window.note_vcam_consumers()
        assert not window.vcam_hint.isHidden()
        assert "discord" in window.vcam_hint.text()
        window.dismiss_vcam_hint()
        assert window.vcam_hint.isHidden()
    finally:
        window._vcam_output = None
        _close(window, app)


def test_launch_hold_feeds_the_canvas_instead_of_the_idle_card(app):
    """A held node must not stay on the dark ready card once preview runs."""
    class Held:
        active = True
        device = "/dev/video10"

    window = _window(empty_project(), app)
    try:
        assert window.enable_held_vcam() is False
        window._vcam_output = Held()
        assert window.enable_held_vcam() is True
        assert window.vcam_on is True
        assert "on" in window.vcam_button.text().lower()
    finally:
        window._vcam_output = None
        _close(window, app)


def test_diagnostics_fix_button_reloads_and_reholds_the_device(app, monkeypatch):
    """The one-click fix must release the node, reload, then hold it again."""
    import anycam.gui_many as many

    class Held:
        active = True
        device = "/dev/video10"

        def stop(self):
            self.active = False
            return {"device": self.device, "ok": True, "error": ""}

    caps = {"value": False}
    monkeypatch.setattr(many.config, "vcam_exclusive_caps",
                        lambda path=None, device=None: caps["value"])
    monkeypatch.setattr(many.config, "vcam_setup_needed",
                        lambda device=None: "" if caps["value"] else "exclusive_caps=0")
    window = _window(empty_project(), app)
    try:
        window._vcam_output = Held()
        window.hold_vcam_device = lambda: True
        window._refresh_diag_state()
        assert not window.diag_fix.isHidden()

        calls = []
        caps["value"] = True
        monkeypatch.setattr(many.config, "other_virtual_cameras",
                            lambda *a, **k: [])
        monkeypatch.setattr(many.config, "setup_vcam_system",
                            lambda device=None: calls.append(device) or "")
        window.fix_vcam_visibility()
        assert window._fix_thread is not None
        window._fix_thread.join(5)
        assert not window._fix_thread.is_alive()
        window._fix_poll()

        assert calls == ["/dev/video10"]
        assert "listed" in window.diag_chromium.text()
        assert window.diag_fix.isHidden()
    finally:
        window._vcam_output = None
        _close(window, app)


def test_diagnostics_fix_button_surfaces_an_authorization_failure(app,
                                                                  monkeypatch):
    import anycam.gui_many as many

    monkeypatch.setattr(many.config, "vcam_exclusive_caps",
                        lambda path=None, device=None: False)
    window = _window(empty_project(), app)
    try:
        window.hold_vcam_device = lambda: True
        monkeypatch.setattr(many.config, "other_virtual_cameras",
                            lambda *a, **k: [])
        monkeypatch.setattr(
            many.config, "setup_vcam_system",
            lambda device=None: "setup failed: the password prompt was cancelled (authorization)")
        window._refresh_diag_state()
        window.fix_vcam_visibility()
        window._fix_thread.join(5)
        window._fix_poll()
        assert "reload failed" in window.diag_chromium.text()
        assert "authorization" in window.diag_chromium.text()
        assert not window.diag_fix.isHidden()
    finally:
        window._vcam_output = None
        _close(window, app)


def test_diagnostics_tab_shows_the_measured_conversion_cost(app, monkeypatch):
    """The settings Diagnostics tab must report the path and its real cost."""
    import anycam.engine.outputs.vcam as vcam_module

    monkeypatch.setattr(
        vcam_module, "measure_conversion",
        lambda *args, **kwargs: {"gpu": 12.5, "cpu": 20.0, "chosen": "gpu",
                                 "width": 1920, "height": 1080, "fps": 60})
    window = _window(empty_project(), app)
    try:
        window._refresh_diag_state()
        assert "stopped" in window.diag_state.text()
        window.measure_vcam_cost()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.processEvents()
            thread = window._diag_thread
            if thread is None or not thread.is_alive():
                break
            time.sleep(0.01)
        window._diag_poll()
        text = window.diag_cost.text()
        assert "GPU 12.50 ms/frame" in text and "CPU 20.00 ms/frame" in text
        assert "chosen: GPU" in text
    finally:
        _close(window, app)


def test_scene_cards_are_built_from_the_project(app):
    """The card strip used to be created after its only refresh, so it stayed empty."""
    project, second = _two_scene_project()
    window = _window(project, app)
    try:
        assert window.scene_cards.count() == 2
        labels = [window.scene_cards.itemAt(index).widget().text()
                  for index in range(window.scene_cards.count())]
        assert labels == ["1  Camera", "2  Second"]
        checked = [window.scene_cards.itemAt(index).widget().isChecked()
                   for index in range(window.scene_cards.count())]
        assert checked == [True, False]

        window._switch_scene_card(second.id)
        assert window.project.active_scene == second.id
        checked = [window.scene_cards.itemAt(index).widget().isChecked()
                   for index in range(window.scene_cards.count())]
        assert checked == [False, True], "card check states must follow the switch"

        window.add_scene()
        assert window.scene_cards.count() == 3
    finally:
        _close(window, app)


def test_preset_deck_numbers_scenes_and_supports_rename_and_remove(app, monkeypatch):
    project, second = _two_scene_project()
    window = _window(project, app)
    monkeypatch.setattr("anycam.gui.QInputDialog.getText",
                        lambda *args, **kwargs: ("Intro", True))
    try:
        window.rename_scene(second.id)
        assert second.name == "Intro"
        labels = [window.scene_cards.itemAt(index).widget().text()
                  for index in range(window.scene_cards.count())]
        assert labels == ["1  Camera", "2  Intro"]

        window.remove_scene(second.id)
        assert second not in window.project.scenes
        assert window.project.active_scene == project.scenes[0].id
        assert window.scene_cards.count() == 1
    finally:
        _close(window, app)


def test_manycam_layout_leaves_the_classic_window_alone():
    """Importing the alternative layout must not patch the base class."""
    assert AnyCamWindow._refresh_scenes.__qualname__.startswith("AnyCamWindow.")
    assert ManyCamWindow._refresh_scenes is not AnyCamWindow._refresh_scenes
    assert ManyCamWindow._scene_changed is not AnyCamWindow._scene_changed


def test_new_layer_stacks_above_a_reordered_scene(app):
    """Drag-reorder renumbers z in tens; a new layer must still land on top."""
    project = Project(name="stacking")
    scene = Scene(new_id("scene"), "Stacking")
    project.scenes = [scene]
    project.active_scene = scene.id
    for index in range(4):
        scene.add_layer(Layer(
            new_id("layer"), "test", f"Pattern {index}", source="ball",
            box=(0.05, 0.05, 0.2, 0.2), z=index + 1))
    window = _window(project, app)
    try:
        window._layers_reordered()
        assert [layer.z for layer in window.scene.layers] == [10, 20, 30, 40]

        added = window.add_layer("test")
        assert added is not None
        assert added.z > max(layer.z for layer in window.scene.layers
                             if layer is not added)
        assert window.scene.ordered()[-1] is added
    finally:
        _close(window, app)


def test_only_one_program_preview_and_layout_thumbnails(app):
    """There must be no second, tiny preview under the program monitor."""
    window = _window(default_project(), app)
    try:
        assert len(window.findChildren(PreviewWidget)) == 1
        assert not hasattr(window, "grid")
        assert list(window.layout_buttons) == [
            "full", "split-2", "three-up", "three-across", "grid-2x2", "pip"]
        assert not hasattr(window, "scene_layout_combo")
    finally:
        _close(window, app)


def test_layout_change_updates_the_running_engine_without_rebuild(app):
    """Switching layout must not tear down the source graph or vcam tap."""
    window = _window(_camera_project(2), app)
    try:
        window.start_engine()
        assert window.engine is not None
        previous = window.engine
        arranged = window.apply_layout("split-2")
        assert window.engine is previous
        assert [layer.box for layer in arranged] == list(
            layout_preset("split-2").boxes)
        assert all(layer.id in previous._layer_pads for layer in arranged)
    finally:
        window.stop_engine()
        _close(window, app)


def test_assigning_a_slot_moves_pads_without_restarting(app, monkeypatch):
    """Assigning an existing source is a pad edit, not a graph rebuild."""
    window = _window(_camera_project(2), app)
    pads = _PadEngine()
    window.engine = pads
    restarts: list[int] = []
    monkeypatch.setattr(window, "restart_engine",
                        lambda: restarts.append(1))
    try:
        window.apply_layout("split-2")
        first, second = window.scene.layers
        window._assign_slot(0, second)
        boxes = layout_preset("split-2").boxes
        assert pads.boxes[second.id] == boxes[0]
        assert pads.boxes[first.id] == boxes[1]
        assert pads.visible[second.id] is True
        assert restarts == []
    finally:
        window.engine = None
        _close(window, app)


def test_layout_switch_keeps_the_portal_session(app, monkeypatch):
    """A layout switch must not request a new screen-cast session."""
    project = Project(name="screen")
    scene = Scene(new_id("scene"), "Screen")
    screen = Layer(new_id("layer"), "screen", "Screen", source="151",
                   box=(0.0, 0.0, 1.0, 1.0), z=0)
    camera = Layer(new_id("layer"), "camera", "Cam", source="/dev/video0",
                   box=(0.65, 0.60, 0.30, 0.30), z=1)
    scene.add_layer(screen)
    scene.add_layer(camera)
    project.scenes = [scene]
    project.active_scene = scene.id
    window = _window(project, app)

    class Session:
        closed = False

        def close(self) -> None:
            self.closed = True

    session = Session()
    window._screen_sessions[screen.id] = session
    pads = _PadEngine()
    window.engine = pads
    restarts: list[int] = []
    monkeypatch.setattr(window, "restart_engine",
                        lambda: restarts.append(1))

    def refuse(*_args, **_kwargs):
        raise AssertionError("layout switch requested a new portal session")

    monkeypatch.setattr("anycam.gui.portal.request_screen_cast", refuse)
    try:
        window.apply_layout("full")
        window._assign_slot(0, camera)
        assert window._screen_sessions[screen.id] is session
        assert session.closed is False
        assert screen.id in pads.boxes
        assert camera.id in pads.boxes
        assert restarts == []
    finally:
        window.engine = None
        _close(window, app)


def test_camera_picker_rescans_after_a_device_renumber(app, monkeypatch):
    """A cached /dev/videoN that disappeared must not be added to the PiP."""
    window = _window(empty_project(), app)
    window._camera_choices = [("Old", "/dev/anycam-old-camera")]
    monkeypatch.setattr(
        window, "_scan_camera_choices",
        lambda: [("Current", "/dev/anycam-current-camera")])
    try:
        assert window._choose_camera_device() == "/dev/anycam-current-camera"
    finally:
        _close(window, app)


def test_camera_error_keeps_a_live_screen_instead_of_rebuilding(app, monkeypatch):
    """A busy camera must not tear down the live PiP screen share."""
    project = Project(name="screen")
    scene = Scene(new_id("scene"), "Screen")
    screen = Layer(new_id("layer"), "screen", "Screen", source="151",
                   box=(0.0, 0.0, 1.0, 1.0), z=1)
    camera = Layer(new_id("layer"), "camera", "Cam", source="/dev/video0",
                   box=(0.66, 0.60, 0.32, 0.38), z=2)
    scene.add_layer(screen)
    scene.add_layer(camera)
    project.scenes = [scene]
    project.active_scene = scene.id
    window = _window(project, app)

    class Engine:
        def __init__(self):
            self.dropped = []

        def drop_capture(self, layer_id):
            self.dropped.append(layer_id)
            return True

        def stop(self):
            return {}

    engine = Engine()
    window.engine = engine
    restarts = []
    monkeypatch.setattr(window, "restart_engine",
                        lambda: restarts.append(1))
    try:
        window._handle_pipeline_error(
            "Camera source error: v4l2src: Device or resource busy")
        assert engine.dropped == [camera.id]
        assert restarts == []
        assert window._lost_cameras == {camera.id: camera.source}
        assert "screen preview kept live" in window.statusBar().currentMessage()
    finally:
        window.engine = None
        _close(window, app)


def test_camera_reconnect_reattaches_capture_without_rebuilding_the_screen(
        app, monkeypatch):
    """A returned camera resumes on the existing canvas channel."""
    project = Project(name="screen")
    scene = Scene(new_id("scene"), "Screen")
    screen = Layer(new_id("layer"), "screen", "Screen", source="151",
                   box=(0.0, 0.0, 1.0, 1.0), z=1)
    camera = Layer(new_id("layer"), "camera", "Cam", source="/dev/video-cam",
                   box=(0.66, 0.60, 0.32, 0.38), z=2)
    scene.add_layer(screen)
    scene.add_layer(camera)
    project.scenes = [scene]
    project.active_scene = scene.id
    window = _window(project, app)

    class Engine:
        def __init__(self):
            self.attached = []

        def attach_capture(self, layer_id):
            self.attached.append(layer_id)
            return True

        def stop(self):
            return {}

    engine = Engine()
    window.engine = engine
    window._lost_cameras = {camera.id: camera.source}
    window._camera_watch.start()
    restarts = []
    monkeypatch.setattr(window, "restart_engine",
                        lambda: restarts.append(1))
    monkeypatch.setattr("anycam.gui.Path.exists", lambda self: True)
    try:
        window._reconnect_cameras()
        assert engine.attached == [camera.id]
        assert restarts == []
        assert window._lost_cameras == {}
        assert "Camera reconnected" in window.statusBar().currentMessage()
    finally:
        window.engine = None
        _close(window, app)


def test_removing_a_camera_does_not_restart_or_close_the_screen_session(
        app, monkeypatch):
    """Removing the webcam must leave the screen branch and vcam alive."""
    project = Project(name="screen")
    scene = Scene(new_id("scene"), "Screen")
    screen = Layer(new_id("layer"), "screen", "Screen", source="151",
                   box=(0.0, 0.0, 1.0, 1.0), z=0)
    camera = Layer(new_id("layer"), "camera", "Cam", source="/dev/video0",
                   box=(0.65, 0.60, 0.30, 0.30), z=1)
    scene.add_layer(screen)
    scene.add_layer(camera)
    project.scenes = [scene]
    project.active_scene = scene.id
    window = _window(project, app)

    class Session:
        closed = False

        def close(self) -> None:
            self.closed = True

    session = Session()
    window._screen_sessions[screen.id] = session
    pads = _PadEngine()
    window.engine = pads
    restarts: list[int] = []
    monkeypatch.setattr(window, "restart_engine",
                        lambda: restarts.append(1))
    try:
        window.remove_layers([camera.id])
        assert camera.id not in [layer.id for layer in window.scene.layers]
        assert screen.id in [layer.id for layer in window.scene.layers]
        assert pads.detached == [camera.id]
        assert pads.visible[camera.id] is False
        assert window._screen_sessions[screen.id] is session
        assert session.closed is False
        assert restarts == []
        assert window.engine is pads
    finally:
        window.engine = None
        _close(window, app)


def test_screen_remove_and_readd_reuses_the_portal_session(app, monkeypatch):
    """One picker per launch: remove/add must reuse the open portal stream."""
    class FakeSession:
        def __init__(self):
            self.fd = 7
            self.node_id = 42
            self.streams = [SimpleNamespace(width=1920, height=1080,
                                            source_type="monitor")]
            self.closed = 0

        def take_remote(self):
            fd, self.fd = self.fd, -1
            return fd

        def close(self):
            self.closed += 1

    session = FakeSession()
    requests: list[int] = []
    monkeypatch.setattr(
        portal, "request_screen_cast",
        lambda: requests.append(1) or session)
    window = _window(_camera_project(1), app)
    try:
        first = window.add_layer("screen")
        assert first is not None
        assert requests == [1]
        window.remove_layers([first.id])
        assert session.closed == 0
        assert window._last_screen_session is session

        second = window.add_layer("screen")
        assert second is not None
        assert requests == [1], "re-add asked the portal for a new session"
        assert second.source == "42"
        assert window._screen_sessions[second.id] is session
        assert session.closed == 0
    finally:
        _close(window, app)


def test_adding_a_screen_after_its_fd_was_used_reuses_the_portal_session(
        app, monkeypatch):
    """A consumed fd must not force a second portal picker."""
    class FakeSession:
        fd = -1
        streams = [SimpleNamespace(width=1920, height=1080, source_type="monitor")]

        @property
        def node_id(self):
            return 42

        def close(self):
            pass

    session = FakeSession()
    requests: list[int] = []
    monkeypatch.setattr(
        portal, "request_screen_cast",
        lambda **_kwargs: requests.append(1) or session)
    window = _window(empty_project(), app)
    window._last_screen_session = session
    try:
        layer = window.add_layer("screen")
        assert layer is not None
        assert layer.source == "42"
        assert window._screen_sessions[layer.id] is session
        assert requests == []
    finally:
        _close(window, app)


def test_adding_a_source_keeps_the_running_pipeline(app):
    window = _window(_camera_project(1), app)
    try:
        window.start_engine()
        assert window.engine is not None
        engine = window.engine
        pipeline = engine.pipeline
        layer = window.add_layer("test", source="blue",
                                 box=(0.5, 0.0, 0.5, 1.0))
        assert layer is not None
        assert window.engine is engine
        assert engine.pipeline is pipeline, "adding a source rebuilt the canvas"
        assert layer.id in engine._layer_pads
    finally:
        window.stop_engine()
        _close(window, app)


def test_removing_a_camera_keeps_the_live_canvas(app):
    """Real engine: removing one source must not replace the pipeline."""
    window = _window(_camera_project(2), app)
    try:
        window.start_engine()
        assert window.engine is not None
        previous = window.engine
        removed = window.scene.layers[0]
        window.remove_layers([removed.id])
        assert window.engine is previous
        assert window.engine.pipeline is not None
        assert removed.id not in previous._layer_pads
        assert len(window.scene.layers) == 1
    finally:
        window.stop_engine()
        _close(window, app)


def test_every_layout_thumbnail_arranges_the_expected_boxes(app):
    project = _camera_project(4)
    window = _window(project, app)
    try:
        for preset in LAYOUT_PRESETS:
            arranged = window.apply_layout(preset.key)
            expected = list(preset.boxes)[:len(arranged)]
            assert [layer.box for layer in arranged] == expected, preset.key
            assert window.layout_buttons[preset.key].isChecked()
            assert preset.label in window.statusBar().currentMessage()
    finally:
        _close(window, app)


def test_pip_shows_one_big_camera_and_one_small_corner_camera(app):
    project = _camera_project(2)
    window = _window(project, app)
    try:
        arranged = window.apply_layout("pip")
        assert len(arranged) == 2
        main, inset = arranged[0].box, arranged[1].box
        assert main == (0.0, 0.0, 1.0, 1.0)
        assert inset[0] > 0.5 and inset[1] > 0.5
        assert inset[2] < 0.5 and inset[3] < 0.5
    finally:
        _close(window, app)


def test_manycam_has_one_layout_control_and_classic_keeps_its_combo(app):
    """Layouts live in one place; the Scene tab is scenes only."""
    window = _window(_camera_project(2), app)
    try:
        assert not hasattr(window, "scene_layout_combo")
        assert len(window.layout_buttons) == len(LAYOUT_PRESETS)
    finally:
        _close(window, app)

    classic = AnyCamWindow(_camera_project(2))
    classic.headless = True
    classic._persist = False
    app.processEvents()
    try:
        assert classic.layout_combo.count() == len(LAYOUT_PRESETS)
        classic.layout_combo.setCurrentIndex(classic.layout_combo.findData("split-2"))
        arranged = classic.apply_layout()
        assert [layer.box for layer in arranged] == list(
            layout_preset("split-2").boxes)
        assert arranged[0].box[0] < arranged[1].box[0]
    finally:
        _close(classic, app)


def test_layout_leaves_title_overlays_in_place(app):
    project = default_project()
    window = _window(project, app)
    title = next(layer for layer in window.scene.layers if layer.kind == "text")
    original = title.box
    try:
        window.apply_layout("grid-2x2")
        assert title.box == original
    finally:
        _close(window, app)


def test_layout_choice_is_remembered_per_preset(app):
    project = Project(name="memory")
    first = Scene(new_id("scene"), "First")
    second = Scene(new_id("scene"), "Second")
    for scene in (first, second):
        for index in range(2):
            scene.add_layer(Layer(
                new_id("layer"), "test", f"{scene.name} {index}", source="ball",
                box=(0.62, 0.60, 0.34, 0.34), z=index + 1))
    project.scenes = [first, second]
    project.active_scene = first.id
    window = _window(project, app)
    try:
        window.apply_layout("pip")
        assert first.layout == "pip"

        window._switch_scene_card(second.id)
        assert window.scene is second
        assert second.layout == ""
        window.apply_layout("split-2")
        assert second.layout == "split-2"

        window._switch_scene_card(first.id)
        assert window.layout_buttons["pip"].isChecked()
        assert window.scene is first
    finally:
        _close(window, app)


def test_assigning_a_layer_to_a_slot_moves_it_there(app):
    project = _camera_project(3)
    window = _window(project, app)
    try:
        window.apply_layout("split-2")
        scene = window.scene
        third = scene.layers[2]
        window._assign_slot(0, third)

        preset = layout_preset("split-2")
        assert third.box == preset.boxes[0]
        assert scene.layout == "split-2"
        assert window._slot_layers(preset)[0] is third
        assert not hasattr(window, "slot_grid")
    finally:
        _close(window, app)


def test_monitor_format_controls_update_the_output(app):
    window = _window(default_project(), app)
    try:
        window.resolution_combo.setCurrentIndex(
            window.resolution_combo.findData(0))
        window.fps_combo.setCurrentIndex(window.fps_combo.findData(30))

        output = window.project.output
        assert (output.canvas_w, output.canvas_h, output.fps) == (1280, 720, 30)
        assert window.width_spin.value() == 1280
        assert window.height_spin.value() == 720
        assert window.fps_spin.value() == 30
        assert window.statusBar().currentMessage() == "Output 1280×720 · 30 fps"
    finally:
        _close(window, app)


def test_preset_deck_buttons_carry_a_composition_thumbnail(app):
    window = _window(_camera_project(2), app)
    try:
        button = window.scene_cards.itemAt(0).widget()
        assert button is not None
        assert not button.icon().isNull()
    finally:
        _close(window, app)


def test_first_run_without_a_saved_project_starts_empty(app):
    window = ManyCamWindow()
    window.headless = True
    window._persist = False
    app.processEvents()
    try:
        assert len(window.project.scenes) == 1
        assert window.scene.layers == []
        assert window.scene.layout == "full"
    finally:
        _close(window, app)


def test_untouched_starter_project_migrates_to_empty(app, tmp_path, monkeypatch):
    path = tmp_path / "project.json"
    default_project().save(path)
    monkeypatch.setattr("anycam.gui_many.config.project_path", lambda: path)

    window = ManyCamWindow()
    window.headless = True
    window._persist = False
    app.processEvents()
    try:
        assert window.scene.layers == []
        assert window.scene.layout == "full"
    finally:
        _close(window, app)


def test_empty_single_layout_shows_one_add_button(app):
    window = _window(empty_project(), app)
    try:
        assert window.scene.layers == []
        assert len(window.stage_add_buttons) == 1
        assert window.stage_add_buttons[0].text() == "+"
    finally:
        _close(window, app)


def test_every_empty_slot_gets_a_plus_button(app):
    window = _window(empty_project(), app)
    try:
        window.apply_layout("grid-2x2")
        assert window.scene.layout == "grid-2x2"
        assert window.scene.layers == []
        assert len(window.stage_add_buttons) == 4
        assert all(button.text() == "+" for button in window.stage_add_buttons)

        window.apply_layout("pip")
        assert len(window.stage_add_buttons) == 2
    finally:
        _close(window, app)


def test_sidebar_is_layouts_effects_scenes_and_settings_is_a_window(app):
    window = _window(empty_project(), app)
    try:
        names = [window.side_tabs.tabText(index)
                 for index in range(window.side_tabs.count())]
        assert names == ["Layouts", "Effects", "Stage", "Scenes"]
        assert window._settings is not None
        assert not window._settings.isVisible()

        window._open_settings()
        app.processEvents()
        assert window._settings.isVisible()
        tabs = [window.settings_tabs.tabText(index)
                for index in range(window.settings_tabs.count())]
        assert tabs == ["Sources", "Audio", "Media", "Output", "Stream",
                        "Diagnostics", "About"]
        assert not hasattr(window, "slot_grid")
    finally:
        _close(window, app)


def test_settings_keeps_one_video_output_one_mic_and_separate_media(app):
    window = _window(empty_project(), app)
    try:
        window._open_settings(1)
        app.processEvents()
        tabs = window.settings_tabs
        audio_page = tabs.widget(1)
        media_page = tabs.widget(2)
        output_page = tabs.widget(3)
        stream_page = tabs.widget(4)

        assert audio_page.isAncestorOf(window.mic_combo)
        assert not audio_page.isAncestorOf(window.desktop_combo)
        assert not audio_page.isAncestorOf(window.media_edit)

        assert media_page.isAncestorOf(window.media_edit)
        assert media_page.isAncestorOf(window.desktop_combo)
        assert output_page.isAncestorOf(window.vcam_edit)
        assert not output_page.isAncestorOf(window.rtmp_edit)
        assert stream_page.isAncestorOf(window.rtmp_edit)
    finally:
        _close(window, app)


def test_share_desktop_audio_updates_the_project(app):
    window = _window(empty_project(), app)
    try:
        window._share_desktop_audio("alsa_output.test.monitor")
        assert window.project.audio.desktop_device == "alsa_output.test.monitor"
        assert window.desktop_combo.currentData() == "alsa_output.test.monitor"

        window._share_desktop_audio("")
        assert window.project.audio.desktop_device == ""
        assert window.desktop_combo.currentData() == ""
    finally:
        _close(window, app)


def test_output_shortcuts_open_the_matching_settings_tab(app):
    window = _window(empty_project(), app)
    try:
        window._open_settings(3)
        app.processEvents()
        assert window.settings_tabs.currentIndex() == 3
        window._open_settings(1)
        assert window.settings_tabs.currentIndex() == 1
    finally:
        _close(window, app)


def test_canvas_right_click_emits_the_layer_under_the_pointer(app):
    scene = Scene(new_id("scene"), "Canvas")
    layer = Layer(new_id("layer"), "camera", "Desk cam", source="/dev/video2",
                  box=(0.0, 0.0, 1.0, 1.0))
    scene.add_layer(layer)
    preview = PreviewWidget()
    preview.resize(640, 360)
    preview.set_scene(scene)
    image = QImage(640, 360, QImage.Format.Format_RGBA8888)
    image.fill(0)
    preview.set_image(image)
    seen: list[str] = []
    preview.context_menu_requested.connect(lambda layer_id, _pos: seen.append(layer_id))
    try:
        position = QPoint(320, 180)
        QApplication.sendEvent(preview, QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, position,
            preview.mapToGlobal(position)))
        assert seen == [layer.id]
    finally:
        preview.deleteLater()


def test_plus_overlay_forwards_right_click_to_a_layer_under_it(app):
    """A floating source must still be removable through the + overlay."""
    window = _window(_camera_project(1), app)
    try:
        window.preview.resize(640, 360)
        image = QImage(640, 360, QImage.Format.Format_RGBA8888)
        image.fill(0)
        window.preview.set_image(image)
        window.apply_layout("grid-2x2")
        preset = window._active_layout()
        layer = window.scene.layers[0]
        # A source dragged across two cells: it keeps cell 1, and the free
        # cell 2's + now lies over its right half.
        layer.box = (0.3, 0.1, 0.4, 0.3)
        window._refresh_stage_slots()
        window._position_stage_slots(force=True)
        button = next(btn for btn, box in window._stage_slots if box == preset.boxes[1])
        seen: list[str] = []
        window._preview_context_menu = (
            lambda layer_id, _pos: seen.append(layer_id))

        rect = window.preview.box_rect(layer)
        target = QPointF(rect.left() + rect.width() * 0.8, rect.center().y()).toPoint()
        window._slot_context_menu(button, button.mapFrom(window.preview, target))
        assert seen == [layer.id]
    finally:
        _close(window, app)


def test_right_click_menu_removes_a_layer_from_the_layout(app):
    window = _window(_camera_project(1), app)
    try:
        window.apply_layout("full")
        layer = window.scene.layers[0]
        menu = window._preview_menu(layer.id)
        labels = [action.text() for action in menu.actions()
                  if not action.isSeparator()]
        assert labels[0].startswith(layer.name)
        assert "Input" in labels
        assert "Output" in labels
        assert "Duplicate layer" in labels
        assert f"Remove {layer.name}" in labels
        inputs = next(action.menu() for action in menu.actions()
                      if action.text() == "Input")
        input_labels = [action.text() for action in inputs.actions()]
        assert "Add video file…" in input_labels
        assert "Add native desktop share…" in input_labels
        outputs = next(action.menu() for action in menu.actions()
                       if action.text() == "Output")
        output_labels = [action.text() for action in outputs.actions()
                         if not action.isSeparator()]
        assert "Virtual camera" in output_labels
        assert "Snapshot" in output_labels

        window.remove_layers([layer.id])
        assert window.scene.layers == []
        assert window._slot_layers(layout_preset("full")) == {}
        assert len(window.stage_add_buttons) == 1
    finally:
        _close(window, app)


def test_selection_outline_auto_hides_and_returns_when_picking_a_layer(app):
    window = _window(_camera_project(1), app)
    try:
        layer = window.scene.layers[0]
        window.preview.select_layer(layer.id)
        assert window.preview._selection_visible

        window.preview._hide_selection()
        assert not window.preview._selection_visible

        window.preview.select_layer(layer.id)
        assert window.preview._selection_visible
    finally:
        _close(window, app)


def test_source_status_reports_stale_and_recovers_without_restarting(app):
    from types import SimpleNamespace

    window = _window(_camera_project(2), app)
    first, second = window.scene.layers
    report = SimpleNamespace(source_health={}, stalled_sources=[], instant_fps=30)
    window.engine = SimpleNamespace(report=report)
    try:
        window._update_source_status()
        assert window.status_chip.text() == "WAITING"
        report.source_health = {layer.id: {"frames": 30} for layer in (first, second)}
        window._update_source_status()
        assert window.status_chip.text() == "LIVE"
        report.stalled_sources = [first.id]
        window._update_source_status()
        assert window.status_chip.text() == "STALE"
        assert first.name in window.statusBar().currentMessage()
        report.stalled_sources = []
        window._update_source_status()
        assert window.status_chip.text() == "LIVE"
        first.kind = "screen"
        report.stalled_sources = [first.id]
        window._update_source_status()
        assert window.status_chip.text() == "STALE"
        assert "hidden or static" in window.statusBar().currentMessage()
        report.stalled_sources = []
        report.source_health[first.id].update(fps=2.5, slow=True)
        window._update_source_status()
        assert window.status_chip.text() == "SLOW"
        assert "2.5 fps" in window.statusBar().currentMessage()
        assert "compositor frame delivery" in window.status_chip.toolTip()
        report.source_health[first.id].update(fps=30, slow=False)
        window._update_source_status()
        assert window.status_chip.text() == "LIVE"
        report.source_health[first.id].update(fps=2.5, slow=True)
        report.stalled_sources = [first.id]
        window._update_source_status()
        assert window.status_chip.text() == "STALE"
        report.stalled_sources = []
        report.instant_fps = 0
        window._update_source_status()
        assert window.status_chip.text() == "STALLED"
        first.visible = False
        report.stalled_sources = [first.id]
        report.instant_fps = 30
        window._update_source_status()
        assert window.status_chip.text() == "LIVE"
    finally:
        window.engine = None
        _close(window, app)


def test_desktop_only_audio_enables_engine_and_recorder(app, monkeypatch, tmp_path):
    from anycam import gui

    window = _window(_camera_project(1), app)
    window.project.audio.desktop_device = "synthetic.monitor"
    window.project.audio.media_file = ""
    window.audio_on = False
    window.project.output.record_dir = str(tmp_path)
    seen = []

    class FakeEngine:
        def __init__(self, project, **kwargs):
            seen.append(("engine", kwargs["audio"]))
            raise RuntimeError("stop before opening real audio")

    class FakeRecorder:
        error = ""

        def start(self, *args, **kwargs):
            seen.append(("recorder", kwargs["audio"]))
            return False

    monkeypatch.setattr(gui, "AnyCamEngine", FakeEngine)
    monkeypatch.setattr(gui, "Recorder", FakeRecorder)
    try:
        window.start_engine()
        window._start_recorder()
        assert seen == [("engine", True), ("recorder", True)]
    finally:
        _close(window, app)


def test_multiview_negotiates_rgba_and_hands_an_owned_image_to_gui(app):
    import time
    from PySide6.QtWidgets import QLabel
    from anycam.multiview import SourcePreview
    from anycam.pool import CaptureSpec, SourcePool

    pool = SourcePool()
    entry = pool.acquire(CaptureSpec("test", "red", 322, 180, 30))
    label = QLabel()
    label.resize(322, 180)
    preview = SourcePreview(entry.channel, label)
    try:
        pool.start()
        preview.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            image = preview._last
            if image is not None and image.pixelColor(image.width() // 2, image.height() // 2).red() > 200:
                break
            time.sleep(0.02)
        assert isinstance(image, QImage)
        assert image.pixelColor(image.width() // 2, image.height() // 2).red() > 200
        sink = preview.pipeline.get_by_name(f"mv-sink-{entry.channel}")
        caps = sink.get_static_pad("sink").get_current_caps()
        assert caps.get_structure(0).get_value("format") == "RGBA"
        preview.pump()
        assert not label.pixmap().isNull()
        assert label.pixmap().toImage().pixelColor(160, 90).red() > 200
    finally:
        preview.stop()
        pool.stop()


def test_new_preset_starts_as_an_empty_single_layout(app):
    window = _window(empty_project(), app)
    try:
        window.add_scene()
        assert len(window.project.scenes) == 2
        scene = window.scene
        assert scene.layers == []
        assert scene.layout == "full"
        assert window.scene_cards.count() == 2
    finally:
        _close(window, app)


def test_launch_refreshes_udev_so_chromium_lists_anycam(app, monkeypatch):
    """The app, not the user, must repair cached udev capabilities."""
    from anycam import gui

    window = _window(empty_project(), app)
    try:
        monkeypatch.setattr(gui.config, "vcam_udev_capabilities",
                            lambda device=None: ":video_output:")
        monkeypatch.setattr(gui.config, "vcam_exclusive_caps",
                            lambda path=None, device=None: True)
        calls = []

        def fake_refresh(device=None, *, prompt=True):
            calls.append(device)
            return ""

        monkeypatch.setattr(gui.config, "refresh_vcam_udev", fake_refresh)
        window.ensure_vcam_visible("/dev/video10")
        assert window._vcam_udev_thread is not None
        window._vcam_udev_thread.join(5)
        assert not window._vcam_udev_thread.is_alive()
        window._vcam_udev_poll()
        assert calls == ["/dev/video10"]
        assert "announced to Chromium" in window.diag_log.toPlainText()
    finally:
        _close(window, app)


def test_a_stage_inset_does_not_turn_a_filled_slot_into_a_plus(app):
    """Single layout + camera, then a stage, then another stage: no "+".

    The stage insets the camera ~5% so the stage shows around it.  The slot
    match compared exact boxes, so the inset camera no longer "filled" its
    slot and the add button came back over it after the next refresh.
    """
    from anycam import overlay

    window = _window(empty_project(), app)
    try:
        layer = window.add_layer("test", box=(0.0, 0.0, 1.0, 1.0))
        assert layer is not None
        window._refresh_stage_slots()
        assert window.stage_add_buttons == []
        first, second = overlay.BACKGROUND_NAMES[:2]
        window._apply_background(first)
        assert layer.box != (0.0, 0.0, 1.0, 1.0), "the camera was inset"
        window._refresh_stage_slots()
        assert window.stage_add_buttons == []
        window._apply_background(second)
        window._refresh_stage_slots()
        assert window.stage_add_buttons == [], "swapping stages must not free the slot"
        window._clear_backdrop()
        window._refresh_stage_slots()
        assert layer.box == (0.0, 0.0, 1.0, 1.0)
        assert window.stage_add_buttons == []
    finally:
        _close(window, app)


def test_moving_or_resizing_the_only_source_does_not_bring_back_a_plus(app):
    """The live trace: single layout, camera dragged smaller into a corner.

    A slot counted as filled only when a layer sat exactly on it, so resizing
    the camera made the one slot look empty and a canvas-sized "+" covered the
    picture.  A moved layer still owns the slot it overlaps most.
    """
    window = _window(empty_project(), app)
    try:
        layer = window.add_layer("test", box=(0.0, 0.0, 1.0, 1.0))
        window._refresh_stage_slots()
        assert window.stage_add_buttons == []
        layer.box = (0.0, 0.516, 0.442, 0.374)          # resized with the mouse
        window._refresh_stage_slots()
        assert window.stage_add_buttons == []
        layer.box = (0.766, 0.673, 0.217, 0.327)        # and moved to a corner
        window._refresh_stage_slots()
        assert window.stage_add_buttons == []
    finally:
        _close(window, app)


def test_a_moved_source_keeps_its_slot_and_only_free_slots_get_a_plus(app):
    window = _window(empty_project(), app)
    try:
        window.apply_layout("grid-2x2")
        preset = window._active_layout()
        first = window.add_layer("test", box=preset.boxes[0])
        second = window.add_layer("test", box=preset.boxes[3])
        window._refresh_stage_slots()
        assert len(window.stage_add_buttons) == 2
        # Drag the second source a little off its cell: it still owns cell 4.
        x, y, w, h = preset.boxes[3]
        second.box = (x + 0.03, y + 0.02, w * 0.8, h * 0.8)
        window._refresh_stage_slots()
        assert len(window.stage_add_buttons) == 2
        slots = window._slot_layers(preset)
        assert slots[0] is first and slots[3] is second
        free = sorted(box for _button, box in window._stage_slots)
        assert free == sorted([preset.boxes[1], preset.boxes[2]])
    finally:
        _close(window, app)
