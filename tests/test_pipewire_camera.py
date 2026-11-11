"""The virtual camera published as a PipeWire camera node."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam.engine.outputs import vcam  # noqa: E402
from anycam.engine.outputs.vcam import VcamOutput  # noqa: E402
from anycam.model import OutputConfig  # noqa: E402

needs_pipewiresink = pytest.mark.skipif(
    not vcam.pipewire_available(), reason="pipewiresink is not installed")


# ----------------------------------------------------------------- setting
def test_the_backend_setting_defaults_to_the_loopback_and_rejects_junk():
    assert OutputConfig().vcam_backend == "v4l2"
    assert OutputConfig.from_dict({"vcam_backend": "both"}).vcam_backend == "both"
    assert OutputConfig.from_dict({"vcam_backend": "pipewire"}).vcam_backend == "pipewire"
    assert OutputConfig.from_dict({"vcam_backend": "nonsense"}).vcam_backend == "v4l2"
    assert OutputConfig.from_dict({}).vcam_backend == "v4l2"


# ------------------------------------------------------------ node graph
@needs_pipewiresink
def test_the_node_is_announced_as_a_camera():
    """WirePlumber's portal gate matches exactly these two properties."""
    props = vcam.pipewire_stream_properties()
    assert props.get_value("media.class") == "Video/Source"
    assert props.get_value("media.role") == "Camera"
    assert props.get_value("node.name") == "anycam"
    assert props.get_value("node.description") == "AnyCam"


@needs_pipewiresink
@pytest.mark.parametrize("backend, factories", [
    ("v4l2", ["v4l2sink"]),
    ("pipewire", ["pipewiresink"]),
    ("both", ["v4l2sink", "pipewiresink"]),
])
def test_each_backend_builds_its_sinks(backend, factories):
    output = VcamOutput()
    output.backend = backend
    output.device = "/dev/video10"
    sinks = output._make_sinks()
    names = [branch[-1].get_factory().get_name() for branch in sinks]
    assert names == factories
    if "pipewiresink" in factories:
        sink = output._pipewire_sink
        assert sink.get_property("mode") == 2  # provide: AnyCam produces
        assert sink.get_property("stream-properties").get_value("media.role") == "Camera"
    # The first-frame probe watches the loopback when there is one.
    assert output._sink_for_probe.get_factory().get_name() == factories[0]


@needs_pipewiresink
def test_both_outputs_share_one_conversion_through_leaky_queues(monkeypatch):
    """One stalled reader must not hold up the other output."""
    output = VcamOutput()
    output.backend = "both"
    output.device = "/dev/video10"
    output.width, output.height, output.fps = 64, 36, 30
    output.source_caps = "video/x-raw,format=RGBA,width=64,height=36,framerate=30/1"
    output.sink_caps = ("video/x-raw,format=YUY2,width=64,height=36,"
                        "framerate=30/1,colorimetry=2:4:7:1")
    built = {}
    monkeypatch.setattr(Gst.Pipeline, "set_state",
                        lambda self, state: built.setdefault("pipeline", self)
                        and Gst.StateChangeReturn.FAILURE)
    output._build_pipeline(use_gl=False)
    pipeline = built["pipeline"]
    factories = [element.get_factory().get_name() for element in pipeline.children]
    assert factories.count("videoconvert") == 1
    assert "tee" in factories
    queues = [element for element in pipeline.children
              if element.get_factory().get_name() == "queue"]
    assert len(queues) == 2
    assert all(queue.get_property("leaky") == 2 for queue in queues)


# ------------------------------------------------------------- fallbacks
def test_pipewire_only_without_the_plugin_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(vcam, "pipewire_available", lambda: False)
    output = VcamOutput()
    assert not output.start("/dev/video10", 320, 180, 30, backend="pipewire")
    assert "pipewiresink" in output.error


def test_both_without_the_plugin_keeps_the_loopback(monkeypatch):
    monkeypatch.setattr(vcam, "pipewire_available", lambda: False)
    monkeypatch.setattr(vcam.config, "vcam_available", lambda device: True)
    monkeypatch.setattr(vcam.config, "vcam_pixel_format", lambda device: "YUY2")
    started = []
    monkeypatch.setattr(VcamOutput, "_start_chain",
                        lambda self, w, h, f: started.append(self.backend) or True)
    output = VcamOutput()
    assert output.start("/dev/video10", 320, 180, 30, backend="both")
    assert started == ["v4l2"]
    assert output.pipewire_error


def test_both_without_a_loopback_publishes_the_pipewire_node(monkeypatch):
    """No v4l2loopback (never set up, or a Flatpak): no root is needed."""
    monkeypatch.setattr(vcam, "pipewire_available", lambda: True)
    monkeypatch.setattr(vcam.config, "vcam_available", lambda device: False)
    started = []
    monkeypatch.setattr(VcamOutput, "_start_chain",
                        lambda self, w, h, f: started.append(self.backend) or True)
    output = VcamOutput()
    assert output.start("/dev/video10", 320, 180, 30, backend="both")
    assert started == ["pipewire"]
    assert output.backend == "pipewire" and output.device == ""


def test_both_falls_back_to_the_loopback_when_pipewire_refuses(monkeypatch):
    monkeypatch.setattr(vcam, "pipewire_available", lambda: True)
    monkeypatch.setattr(vcam.config, "vcam_available", lambda device: True)
    monkeypatch.setattr(vcam.config, "vcam_pixel_format", lambda device: "YUY2")
    started = []

    def chain(self, w, h, f):
        started.append(self.backend)
        if self.backend == "both":
            self.error = "pipewiresink: connection refused"
            return False
        return True

    monkeypatch.setattr(VcamOutput, "_start_chain", chain)
    output = VcamOutput()
    assert output.start("/dev/video10", 320, 180, 30, backend="both")
    assert started == ["both", "v4l2"]
    assert output.backend == "v4l2" and not output.error
    assert "connection refused" in output.pipewire_error


def test_pipewire_only_never_asks_for_the_loopback_setup(monkeypatch):
    monkeypatch.setattr(vcam, "pipewire_available", lambda: True)
    monkeypatch.setattr(vcam.config, "ensure_vcam_device",
                        lambda device: pytest.fail("no root setup for a PipeWire node"))
    monkeypatch.setattr(vcam.config, "vcam_available",
                        lambda device: pytest.fail("the loopback is not used"))
    monkeypatch.setattr(VcamOutput, "_start_chain", lambda self, w, h, f: True)
    assert VcamOutput().start("/dev/video10", 320, 180, 30, setup=True, backend="pipewire")


@pytest.mark.parametrize("running, device, backend, expected", [
    (("v4l2", "/dev/video10"), "/dev/video10", "v4l2", True),
    (("v4l2", "/dev/video10"), "/dev/video11", "v4l2", False),
    (("v4l2", "/dev/video10"), "/dev/video10", "pipewire", False),
    (("pipewire", ""), "/dev/video10", "pipewire", True),
    (("pipewire", ""), "/dev/video10", "v4l2", False),
    (("both", "/dev/video10"), "/dev/video10", "both", True),
    # "both" that fell back to one side: restarting would fail the same way.
    (("pipewire", ""), "/dev/video10", "both", True),
    (("v4l2", "/dev/video10"), "/dev/video10", "both", True),
    (("both", "/dev/video10"), "/dev/video10", "v4l2", False),
])
def test_a_running_output_is_reused_only_for_what_it_serves(running, device, backend, expected):
    output = VcamOutput()
    output.backend, output.device = running
    assert output.matches(device, backend) is expected


# ------------------------------------------------------------------ live
def _anycam_nodes() -> list[dict]:
    try:
        dump = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10)
        objects = json.loads(dump.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return [((o.get("info") or {}).get("props") or {}) for o in objects
            if ((o.get("info") or {}).get("props") or {}).get("node.name") == "anycam"]


@needs_pipewiresink
def test_a_live_pipewire_node_appears_and_streams_the_ready_card(hardware_free_environment):
    if hardware_free_environment:
        pytest.skip("hardware-free run: no PipeWire session")
    if shutil.which("pw-dump") is None:
        pytest.skip("pw-dump is not installed")
    before = len(_anycam_nodes())
    output = VcamOutput()
    if not output.start("", 320, 180, 30, backend="pipewire"):
        pytest.skip(f"no PipeWire session: {output.error}")
    try:
        deadline = time.monotonic() + 5
        nodes = []
        while time.monotonic() < deadline:
            nodes = _anycam_nodes()
            if len(nodes) > before:
                break
            time.sleep(0.1)
        assert len(nodes) > before, "the AnyCam node never appeared in PipeWire"
        props = nodes[-1]
        assert props.get("media.class") == "Video/Source"
        assert props.get("media.role") == "Camera"
        # A separate process, like a real app reading the camera.
        if shutil.which("gst-launch-1.0") is None:
            pytest.skip("gst-launch-1.0 is not installed")
        reader = subprocess.run(
            ["gst-launch-1.0", "-q", "pipewiresrc", "target-object=anycam",
             "num-buffers=2", "!", "video/x-raw", "!", "fakesink", "sync=false"],
            capture_output=True, text=True, timeout=20)
        assert reader.returncode == 0, reader.stderr[-300:]
    finally:
        report = output.stop()
    assert report["backend"] == "pipewire" and report["ok"]


# ------------------------------------------------------------------- GUI
@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    instance = QApplication.instance() or QApplication([])
    yield instance


def test_the_gui_skips_the_root_setup_for_a_pipewire_only_camera(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from anycam import gui
    from anycam.demo import build_demo_project

    project = build_demo_project("testsrc", 320, 180, 30)
    project.output.vcam_backend = "pipewire"
    window = gui.AnyCamWindow(project)
    try:
        monkeypatch.setattr(gui.config, "vcam_setup_needed",
                            lambda device: pytest.fail("no loopback check"))
        monkeypatch.setattr(gui.config, "vcam_udev_capabilities",
                            lambda device: pytest.fail("no udev refresh"))
        assert window.setup_vcam_if_needed()
        window.ensure_vcam_visible()
        assert window.vcam_backend_combo.currentData() == "pipewire"
        assert "allow-pipewire" in window.vcam_note.text()
    finally:
        window.close()


def test_choosing_a_backend_republishes_a_held_camera(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from anycam import gui
    from anycam.demo import build_demo_project

    window = gui.AnyCamWindow(build_demo_project("testsrc", 320, 180, 30))
    started = []

    class Output:
        active = True
        error = ""
        pipewire_error = ""
        device = ""

        def __init__(self):
            self.backend = ""

        def start(self, device, w, h, fps, setup=False, backend="v4l2"):
            self.backend = backend
            started.append(backend)
            return True

        @property
        def publishes_pipewire(self):
            return self.backend in ("pipewire", "both")

        @property
        def publishes_v4l2(self):
            return self.backend in ("v4l2", "both")

        def stop(self):
            started.append("stop")
            return {}

    monkeypatch.setattr("anycam.ui.outputs.VcamOutput", Output)
    monkeypatch.setattr(window, "setup_vcam_if_needed", lambda: True)
    monkeypatch.setattr(window, "ensure_vcam_visible", lambda *a: None)
    try:
        window._vcam_output = Output()
        window.vcam_backend_combo.setCurrentIndex(
            window.vcam_backend_combo.findData("pipewire"))
        assert window.project.output.vcam_backend == "pipewire"
        assert started == ["stop", "pipewire"]
        assert window._vcam_output.backend == "pipewire"
        # A reload puts the combo back to the saved choice without restarting.
        window.project.output.vcam_backend = "both"
        index = window.vcam_backend_combo.findData("both")
        window.vcam_backend_combo.blockSignals(True)
        window.vcam_backend_combo.setCurrentIndex(index)
        window.vcam_backend_combo.blockSignals(False)
        assert started == ["stop", "pipewire"]
    finally:
        window._vcam_output = None
        window.close()
