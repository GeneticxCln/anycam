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
    assert gl_preview.attach_sink(Gst.ElementFactory.make("fakesink"),
                                  _ReadyView(visible=False)) is False


# ---------------------------------------------------------------- readiness
class _QuickWindow:
    """A QQuickWidget's internal window: an offscreen render target."""

    def __init__(self, initialized: bool, visible: bool = False) -> None:
        self._initialized = initialized
        self._visible = visible

    def isSceneGraphInitialized(self) -> bool:
        return self._initialized

    def isVisible(self) -> bool:
        return self._visible


class _ReadyView:
    def __init__(self, visible: bool = True, scene_graph: bool = True) -> None:
        self._visible = visible
        self._window = _QuickWindow(scene_graph)

    def isVisible(self) -> bool:
        return self._visible

    def quickWindow(self):
        return self._window


def test_a_quick_widget_is_ready_once_its_scene_graph_exists(app):
    """Regression: the gate required the Quick window to be visible and
    exposed, but a QQuickWidget renders into an offscreen window that is
    never either.  Every start therefore fell back to the image preview and
    the give-up left the view's "Preview stopped" card over the live
    picture, which read as the camera having stopped."""
    assert gl_preview.view_ready(_ReadyView()) is True
    assert gl_preview.view_ready(_ReadyView(visible=False)) is False
    assert gl_preview.view_ready(_ReadyView(scene_graph=False)) is False


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

    engine = AnyCamEngine(_project(), preview=True, preview_gl_sink=attach,
                          canvas="cpu").build()
    try:
        assert engine.preview_backend == "gpu"
        # A small CPU tap feeds motion detection / the floating panel.
        assert engine._appsink is not None
        children = {child.get_name(): child for child in engine.pipeline.children}
        assert children["preview_no_pool"].get_property("drop-allocation") is True
        assert children["preview_gl_upload"].get_factory().get_name() == "glupload"
        assert children["preview_gl_sink"] is handed[0]
        assert handed[0].get_factory().get_name() == "qml6glsink"
        assert handed[0].get_property("sync") is False
        # The CPU tap follows the window; the GPU sink scales its own view.
        assert engine.set_preview_target(320, 180) is True
        tap_caps = children["preview_caps"].get_property("caps").to_string()
        assert "width=(int)320" in tap_caps and "height=(int)180" in tap_caps
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


def test_enable_gpu_imports_the_gl_preview_module_and_builds_the_view(app, monkeypatch):
    """Regression: enable_gpu used ``from . import gl_preview`` (no module
    ``anycam.ui.gl_preview``), so the app crashed with ImportError whenever
    the GPU preview was available.  The test patches the real
    ``anycam.gl_preview`` module's builders, so reaching them proves the
    import resolves (offscreen has no GL to build a real view with)."""
    from PySide6.QtWidgets import QWidget

    from anycam.gui import PreviewWidget

    calls = []
    monkeypatch.setattr(gl_preview, "use_opengl_scene_graph", lambda: calls.append("sg"))
    monkeypatch.setattr(gl_preview, "make_video_view", lambda parent: QWidget(parent))
    widget = PreviewWidget()
    try:
        assert widget.enable_gpu() is True
        assert calls == ["sg"]
        assert widget.video_view is not None
        assert widget.video_view.parent() is widget
        assert widget._overlay is not None
        # Already enabled: reports the existing view without rebuilding.
        assert widget.enable_gpu() is True
    finally:
        widget.close()


def test_giving_up_on_the_gpu_preview_removes_the_idle_card(app, monkeypatch):
    """The retry loop must not end with the Quick view left on top: its QML
    shows "Preview stopped" over the image preview that is actually live."""
    from anycam.gui import PreviewWidget
    from anycam.ui.engine_control import EngineMixin

    widget = PreviewWidget()
    view = FakeView()
    view.isVisible = lambda: True
    widget.video_view = view
    events: list[str] = []
    restarted: list[int] = []

    class FakeWindow:
        preview = widget
        engine = object()
        _gpu_preview_tries = 4

        @staticmethod
        def restart_engine() -> None:
            restarted.append(1)

        @staticmethod
        def log_event(message: str) -> None:
            events.append(message)

    monkeypatch.setattr(gl_preview, "view_ready", lambda _view: False)
    EngineMixin._resume_gpu_preview(FakeWindow())
    assert view.deleted and widget.video_view is None
    assert restarted == []
    assert any("never became ready" in message for message in events)
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


@needs_qml6glsink
def test_on_the_gpu_canvas_the_preview_takes_the_mixed_texture_directly():
    """No download and second upload: the preview hangs off the GL tee."""
    from anycam import pipeline

    if pipeline.gl_canvas_problem():
        pytest.skip("no working GL canvas here")
    handed = []
    engine = AnyCamEngine(_project(), preview=True, canvas="gpu",
                          preview_gl_sink=lambda sink: handed.append(sink) or True).build()
    try:
        assert engine.canvas_backend == "gpu" and engine.preview_backend == "gpu"
        children = {child.get_name(): child for child in engine.pipeline.children}
        assert "preview_gl_upload" not in children and "preview_no_pool" not in children
        queue = children["queue_preview"]
        upstream = queue.get_static_pad("sink").get_peer().get_parent_element()
        assert upstream is children["canvas_gl_tee"]
        assert queue.get_static_pad("src").get_peer().get_parent_element() is handed[0]
    finally:
        engine.stop()


def test_the_preview_sinks_do_not_wait_for_preroll():
    """Regression: a sink that waits for preroll behind a queue on the canvas
    tee deadlocked startup (0 frames, then GUI hangs on scene switch and
    shutdown).  Both the GPU sink and the CPU tap must be async=false."""
    handed = []
    engine = AnyCamEngine(_project(), preview=True, canvas="gpu",
                          preview_gl_sink=lambda sink: handed.append(sink) or True).build()
    try:
        children = {child.get_name(): child for child in engine.pipeline.children}
        assert handed[0].get_property("async") is False
        assert children["preview_cpu_sink"].get_property("async") is False
    finally:
        engine.stop()


@needs_qml6glsink
def test_on_the_gpu_canvas_the_cpu_tap_scales_on_the_gpu_before_it_downloads():
    """Regression: scaling the canvas's full-size downloaded frames on the CPU
    segfaulted inside the scaler about every other run once an NVENC recording
    was active.  The tap now scales on the GPU and downloads a small texture."""
    from anycam.engine.canvas_helpers import gl_canvas_problem

    problem = gl_canvas_problem()
    if problem:
        pytest.skip(f"the GPU canvas is not available here: {problem}")
    engine = AnyCamEngine(_project(), preview=True, preview_gl_sink=lambda sink: True,
                          canvas="gpu").build()
    try:
        if engine.canvas_backend != "gpu":
            pytest.skip("the GPU canvas did not start here")
        children = {child.get_name(): child for child in engine.pipeline.children}
        assert children["preview_cpu_glscale"].get_factory().get_name() == "glcolorscale"
        caps = children["preview_caps"].get_property("caps").to_string()
        assert "memory:GLMemory" in caps
        # Resizing keeps the GL memory feature on the tap's caps.
        engine.set_preview_target(320, 180)
        caps = children["preview_caps"].get_property("caps").to_string()
        assert "memory:GLMemory" in caps and "width=(int)320" in caps
        assert engine._appsink is not None
    finally:
        engine.stop()


def test_a_layer_is_stopped_before_it_is_unlinked():
    """An appsrc layer that is unlinked while PLAYING pushes into nothing and
    posts not-linked, which stops the whole canvas.  Stop first, then unlink."""
    from types import SimpleNamespace

    order = []

    class Element:
        def set_state(self, _state) -> None:
            order.append("stop")

        def get_parent(self):
            return None

    class Pad:
        def get_peer(self):
            return pad

    class Output:
        def get_peer(self):
            return pad

        def unlink(self, _pad) -> None:
            order.append("unlink")

    class Tail:
        def get_static_pad(self, _name):
            return Output()

    pad = object()
    engine = object.__new__(AnyCamEngine)
    engine.pipeline = SimpleNamespace(remove=lambda _element: None)
    engine.compositor = SimpleNamespace(release_request_pad=lambda _pad: order.append("release"))
    engine._pad_probes = []
    engine._signal_handlers = []
    engine.chains = []
    engine._dispose_chain(SimpleNamespace(elements=[Element()], tail=Tail()), pad)
    assert order == ["stop", "unlink", "release"]


def test_an_image_preview_still_stops_layers_before_returning():
    """The CPU path must keep its synchronous guarantee (chain gone on return)."""
    from types import SimpleNamespace

    class Element:
        def __init__(self) -> None:
            self.stopped = False

        def set_state(self, _state) -> None:
            self.stopped = True

        def get_parent(self):
            return None

    engine = object.__new__(AnyCamEngine)
    engine.pipeline = SimpleNamespace(remove=lambda _element: None)
    engine.compositor = None
    engine._pad_probes = []
    engine._signal_handlers = []
    engine.chains = []
    element = Element()
    engine._dispose_chain(SimpleNamespace(elements=[element], tail=None), None)
    assert element.stopped


def test_the_audio_mixer_does_not_ignore_inactive_pads():
    """Regression: with ignore_inactive_pads=True the mixer's first timed
    aggregation (before any input had a buffer) answered "we're EOS" and
    paused its output task for good; the inputs then blocked in a query on
    it, no audio flowed and engine.stop hung in the audio EOS push."""
    project = _project()
    project.audio.mic_enabled = False
    engine = AnyCamEngine(project, preview=True, audio=True, canvas="cpu").build()
    try:
        assert engine.audio_mixer is not None
        assert engine.audio_mixer.get_property("ignore-inactive-pads") is False
    finally:
        engine.stop()


def test_the_audio_mixer_output_is_pinned_to_48_khz():
    """Regression: an early first aggregation made the mixer output 44100 Hz
    before its inputs set 48000 Hz; the rate change ended the recorder's audio
    after a few buffers (a 4 s clip had 0.05 s of sound)."""
    project = _project()
    project.audio.mic_enabled = False
    engine = AnyCamEngine(project, preview=True, audio=True, canvas="cpu").build()
    try:
        caps = engine.pipeline.get_by_name("audio_mixer_caps")
        assert caps is not None
        text = caps.get_property("caps").to_string()
        assert "rate=(int)48000" in text and "channels=(int)2" in text
        peer = engine.audio_mixer.get_static_pad("src").get_peer()
        assert peer.get_parent_element() is caps
    finally:
        engine.stop()
