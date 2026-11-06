"""Regression tests for the GUI and output state fixes (audit phase 3)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from anycam import portal  # noqa: E402
from anycam.engine.outputs.vcam import VcamOutput  # noqa: E402
from anycam.gui import AnyCamWindow, claim_gui_instance  # noqa: E402
from anycam.gui_many import ManyCamWindow  # noqa: E402
from anycam.model import Layer, Project, Scene, new_id  # noqa: E402
from anycam.multiview import MultiViewDialog, source_label  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402
from anycam.pool import CaptureSpec, SourcePool  # noqa: E402

Gst.init(None)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))


def make_project(*layers: Layer) -> Project:
    project = Project(name="state")
    scene = Scene(new_id("scene"), "State")
    for layer in layers:
        scene.add_layer(layer)
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def window_for(project: Project, app, cls=ManyCamWindow):
    window = cls(project)
    window.headless = True
    window._persist = False
    app.processEvents()
    return window


def close(window, app) -> None:
    window.engine = None
    window._persist = False
    window.close()
    app.processEvents()


def pattern_layer(name="Pattern", **kwargs) -> Layer:
    return Layer(new_id("layer"), "test", name, source="ball",
                 box=kwargs.pop("box", (0.0, 0.0, 1.0, 1.0)), **kwargs)


class FakeSession:
    def __init__(self, node: int):
        self.node = node
        self.fd = 10 + node
        self.streams = [SimpleNamespace(width=1920, height=1080,
                                        source_type="monitor")]

    @property
    def node_id(self) -> int:
        return self.node

    def take_remote(self) -> int:
        fd, self.fd = self.fd, -1
        return fd

    def close(self) -> None:
        pass


# ---------------------------------------------------- item 11: second screen share
def test_a_second_screen_asks_the_portal_while_the_first_is_showing(app, monkeypatch):
    first_session, second_session = FakeSession(42), FakeSession(43)
    sessions = iter([first_session, second_session])
    requests: list[int] = []
    monkeypatch.setattr(
        portal, "request_screen_cast",
        lambda **_kwargs: requests.append(1) or next(sessions))
    window = window_for(make_project(), app)
    try:
        first = window.add_layer("screen")
        second = window.add_layer("screen")
        assert requests == [1, 1], "the second share must open the picker"
        assert (first.source, second.source) == ("42", "43")
        assert window._screen_sessions[second.id] is second_session
    finally:
        close(window, app)


def test_a_removed_screen_is_put_back_without_a_picker(app, monkeypatch):
    session = FakeSession(42)
    requests: list[int] = []
    monkeypatch.setattr(
        portal, "request_screen_cast",
        lambda **_kwargs: requests.append(1) or session)
    window = window_for(make_project(), app)
    try:
        first = window.add_layer("screen")
        window.remove_layers([first.id])
        again = window.add_layer("screen")
        assert requests == [1] and again.source == "42"
    finally:
        close(window, app)


def test_add_a_different_share_always_opens_the_picker(app, monkeypatch):
    sessions = iter([FakeSession(42), FakeSession(43)])
    requests: list[int] = []
    monkeypatch.setattr(
        portal, "request_screen_cast",
        lambda **_kwargs: requests.append(1) or next(sessions))
    window = window_for(make_project(), app)
    try:
        first = window.add_layer("screen")
        window.remove_layers([first.id])      # 42 is free to reuse ...
        other = window.add_layer("screen-new")  # ... but the user wants another
        assert requests == [1, 1] and other.source == "43"
    finally:
        close(window, app)


def test_a_cancelled_picker_adds_nothing(app, monkeypatch):
    window = window_for(make_project(), app)

    def refuse(**_kwargs):
        raise portal.PortalError("screen-cast request was cancelled or denied")

    monkeypatch.setattr(portal, "request_screen_cast", refuse)
    try:
        assert window.add_layer("screen") is None
        assert window.scene.layers == []
    finally:
        close(window, app)


# ------------------------------------------------------------- item 9: backdrops
def test_backdrops_are_replaced_and_cleared_and_cameras_restored(app):
    camera = pattern_layer("Cam")
    window = window_for(make_project(camera), app, AnyCamWindow)
    original = tuple(camera.box)
    try:
        window._apply_background("Aurora")
        inset = tuple(camera.box)
        assert inset != original
        window._apply_background("Sunset")
        window._apply_animation("tunnel")
        # One backdrop at a time, and the camera was only inset once.
        assert len(window._active_backdrops()) == 1
        assert [layer for layer in window.scene.layers
                if layer.role == "backdrop"] == window._active_backdrops()
        assert tuple(camera.box) == inset
        window._clear_backdrop()
        assert window._active_backdrops() == []
        assert tuple(camera.box) == pytest.approx(original)
    finally:
        close(window, app)


def test_clearing_a_backdrop_keeps_a_camera_the_user_moved(app):
    camera = pattern_layer("Cam")
    window = window_for(make_project(camera), app, AnyCamWindow)
    try:
        window._apply_background("Aurora")
        camera.box = (0.1, 0.1, 0.5, 0.5)
        window._clear_backdrop()
        assert camera.box == (0.1, 0.1, 0.5, 0.5)
    finally:
        close(window, app)


# ------------------------------------------------------------ item 10: multi-view
def multiview_pool():
    pool = SourcePool()
    live = CaptureSpec("test", "ball", 64, 64, 30)
    removed = CaptureSpec("test", "smpte", 64, 64, 30)
    pool.acquire(live)
    pool.acquire(removed)
    pool.pin(removed)
    pool.release(removed)  # retained for a quick re-add, but on no layer
    return pool


def test_multiview_lists_only_sources_a_layer_is_using(app):
    pool = multiview_pool()
    dialog = MultiViewDialog(pool)
    try:
        assert len(dialog.previews) == 1
    finally:
        dialog.reject()
        pool.stop()


def test_multiview_stops_its_pipelines_however_it_is_closed(app):
    pool = multiview_pool()
    dialog = MultiViewDialog(pool)
    dialog.show()
    preview = dialog.previews[0]
    try:
        assert preview.pipeline.get_state(0)[1] != Gst.State.NULL
        dialog.reject()  # what Esc does: no close event
        assert preview.pipeline.get_state(0)[1] == Gst.State.NULL
    finally:
        pool.stop()


def test_multiview_button_reopens_after_the_dialog_was_closed(app):
    window = window_for(make_project(pattern_layer()), app)
    window._source_pool.acquire(CaptureSpec("test", "ball", 64, 64, 30))
    window.engine = SimpleNamespace()  # "preview is running" is all it checks
    try:
        window.toggle_multiview()
        first = window._multiview
        assert first is not None
        first.reject()
        app.processEvents()
        assert window._multiview is None
        window.toggle_multiview()
        assert window._multiview is not None and window._multiview is not first
        window._multiview.reject()
    finally:
        close(window, app)
        window._source_pool.stop()


def test_multiview_caption_hides_camera_credentials():
    spec = CaptureSpec("network", "rtsp://admin:hunter2@192.168.1.5/live", 1, 1, 30)
    assert "hunter2" not in source_label(spec)
    assert "192.168.1.5" in source_label(spec)


# ----------------------------------------------------------- item 12: vcam errors
class ScriptedAppsrc:
    def __init__(self, *results):
        self.results = list(results)

    def emit(self, _method, _payload):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_property(self, _name):
        return Gst.Caps.from_string("video/x-raw")


def a_sample():
    return Gst.Sample.new(Gst.Buffer.new_allocate(None, 16, None),
                          Gst.Caps.from_string("video/x-raw"), None, None)


def test_a_failed_push_clears_itself_once_frames_are_accepted_again():
    output = VcamOutput()
    output._appsrc = ScriptedAppsrc(Gst.FlowReturn.NOT_NEGOTIATED,
                                    Gst.FlowReturn.OK)
    output.push_sample(a_sample())
    assert "rejected the canvas frame" in output.error
    output.push_sample(a_sample())
    assert output.error == ""


def test_flushing_is_not_a_fault():
    output = VcamOutput()
    output._appsrc = ScriptedAppsrc(Gst.FlowReturn.FLUSHING)
    output.push_sample(a_sample())
    assert output.error == ""


def test_a_dead_sink_is_never_pushed_to_and_stays_failed():
    output = VcamOutput()
    bus = Gst.Bus.new()
    bus.post(Gst.Message.new_error(
        None, GLib.Error.new_literal(Gst.ResourceError.quark(), "sink died",
                                     Gst.ResourceError.WRITE), "debug"))
    output._bus = bus
    output._appsrc = ScriptedAppsrc()  # any emit would raise IndexError
    output.poll()
    assert "sink died" in output.error
    output.push_sample(a_sample())
    assert "sink died" in output.error


def test_engine_stops_reporting_a_vcam_error_the_output_cleared():
    engine = AnyCamEngine(make_project(pattern_layer()), preview=True).build()
    output = SimpleNamespace(error="rejected", poll=lambda: None,
                             set_canvas_attached=lambda _attached: None)
    try:
        engine.vcam_output = output
        engine.pump(0)
        assert engine.report.vcam_error == "rejected"
        output.error = ""
        engine.pump(0)
        assert engine.report.vcam_error == ""
    finally:
        engine.pool.stop()


# ------------------------------------------------------------- item 15: reordering
def test_dragging_layers_reorders_without_a_rebuild_and_can_be_undone(app):
    layers = [pattern_layer(f"L{i}", z=i + 1) for i in range(3)]
    window = window_for(make_project(*layers), app)
    refreshed: list[int] = []
    restarts: list[int] = []
    window.engine = SimpleNamespace(
        refresh_layer_order=lambda: refreshed.append(1))
    window.restart_engine = lambda: restarts.append(1)
    try:
        before = [layer.id for layer in sorted(window.scene.layers,
                                               key=lambda item: item.z)]
        item = window.layer_list.takeItem(0)          # drag the bottom to the top
        window.layer_list.addItem(item)
        window._layers_reordered()
        after = [layer.id for layer in sorted(window.scene.layers,
                                              key=lambda item: item.z)]
        assert after == before[1:] + before[:1]
        assert refreshed == [1] and restarts == []
        assert window.history.can_undo
        window.engine = None
        window.undo()
        restored = [layer.id for layer in sorted(window.scene.layers,
                                                 key=lambda item: item.z)]
        assert restored == before
    finally:
        close(window, app)


# ------------------------------------------------ item 16: camera error detection
def camera_window(app, count=2):
    cameras = [Layer(new_id("layer"), "camera", f"Cam{i}",
                     source=f"/dev/anycam-cam{i}", z=i + 1)
               for i in range(count)]
    pattern = pattern_layer("Pattern", z=10)
    return window_for(make_project(*cameras, pattern), app), cameras, pattern


def test_disk_or_gpu_errors_do_not_quarantine_the_cameras(app):
    window, _cameras, _pattern = camera_window(app)
    try:
        assert window._camera_error_layers("filesink: No space left on device") == {}
        assert window._camera_error_layers(
            "glupload: Failed to allocate required memory") == {}
        assert window._camera_error_layers("v4l2sink: write error") == {}
        assert len(window._camera_error_layers(
            "Internal data stream error. (gstv4l2src.c)")) == 2
    finally:
        close(window, app)


def test_a_capture_error_blames_only_the_cameras_on_that_capture(app):
    window, cameras, pattern = camera_window(app)
    window.engine = SimpleNamespace(layers_for_channel=lambda channel: (
        {cameras[1].id} if channel == "anycam-src-2" else {pattern.id}))
    try:
        blamed = window._camera_error_layers(
            "anycam-src-2: Device or resource busy (v4l2src)")
        assert blamed == {cameras[1].id: cameras[1].source}
        # A test-pattern capture failing is not a camera problem.
        assert window._camera_error_layers("anycam-src-9: failure") == {}
    finally:
        close(window, app)


# ---------------------------------------------------- item 24: single instance
HOLDER = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('locked', flush=True)\n"
    "time.sleep(60)\n"
)


def test_a_busy_first_instance_keeps_its_socket(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    name = f"anycam-guard-{os.getpid()}-{time.monotonic_ns()}"
    primary, server = claim_gui_instance(name)
    assert primary and server is not None
    try:
        # The first window is "busy": its event loop never runs.
        second, second_server = claim_gui_instance(name)
        assert second is False and second_server is None
        assert server.isListening()
        probe = QLocalSocket()
        probe.connectToServer(name)
        assert probe.waitForConnected(1000), "the first instance lost its socket"
        probe.disconnectFromServer()
    finally:
        server.close()
        QLocalServer.removeServer(name)


def test_a_lock_held_by_another_process_blocks_a_second_gui(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    name = f"anycam-guard-{os.getpid()}-{time.monotonic_ns()}"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path / f"{name}.lock")],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        started, server = claim_gui_instance(name)
        assert started is False and server is None
    finally:
        holder.kill()
        holder.wait()
    # A crashed owner releases the lock; the next launch takes over cleanly.
    primary, server = claim_gui_instance(name)
    try:
        assert primary is True and server is not None
    finally:
        if server is not None:
            server.close()
        QLocalServer.removeServer(name)
        QCoreApplication.processEvents()
