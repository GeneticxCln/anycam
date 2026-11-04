"""The virtual camera must be a session output, not an engine-owned pipeline."""
from __future__ import annotations

import subprocess
import time

import pytest

pytest.importorskip("gi")
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    HAS_COMPOSITOR = Gst.ElementFactory.find("compositor") is not None
    HAS_V4L2SINK = Gst.ElementFactory.find("v4l2sink") is not None
except Exception:  # pragma: no cover - platform without GStreamer
    HAS_COMPOSITOR = False
    HAS_V4L2SINK = False

from anycam import config  # noqa: E402
from anycam.demo import build_demo_project  # noqa: E402
from anycam.model import Layer, Project, Scene, new_id  # noqa: E402
from anycam.engine.outputs.vcam import (  # noqa: E402
    CARD_GRACE_S, LIVE_GRACE_S, VcamOutput, idle_frame,
)
from anycam.pipeline import AnyCamEngine  # noqa: E402

pytestmark = pytest.mark.skipif(not HAS_COMPOSITOR, reason="compositor unavailable")


class FakeVcam:
    active = True
    pixel_format = "YUY2"
    error = ""
    width = 320
    height = 180
    fps = 30

    def __init__(self) -> None:
        self.pushed = 0

    def push_sample(self, _sample) -> None:
        self.pushed += 1

    def poll(self) -> None:
        return None


def test_vcam_output_size_scales_a_4k_canvas_to_the_device():
    """A fixed camera mode is what keeps v4l2sink open across resizes."""
    assert config.vcam_output_size(3840, 2160) == (1920, 1080)
    assert config.vcam_output_size(1920, 1080) == (1920, 1080)
    assert config.vcam_output_size(1280, 720) == (1280, 720)


def test_missing_vcam_device_fails_without_a_pipeline():
    output = VcamOutput()
    assert output.start("/dev/anycam-missing", 320, 180, 30) is False
    assert "not available" in output.error
    assert output.active is False


def test_inactive_vcam_output_is_reported():
    """B17/B18: an output that failed to start must be visible in the report."""
    fake = FakeVcam()
    fake.active = False
    fake.error = "configured device disappeared"
    engine = AnyCamEngine(
        build_demo_project("testsrc", 320, 180, 30),
        preview=True,
        vcam=True,
        vcam_output=fake,
    ).build()
    try:
        assert "configured device disappeared" in engine.report.vcam_error
        assert engine.pipeline.get_by_name("vcam_appsink") is None
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


@pytest.mark.skipif(not HAS_V4L2SINK, reason="v4l2sink unavailable")
def test_legacy_vcam_appsrc_is_bounded(monkeypatch):
    """B20: the self-contained vcam bridge used to queue without a bound."""
    monkeypatch.setattr(config, "vcam_available", lambda device=None: True)
    monkeypatch.setattr(config, "vcam_pixel_format", lambda device: "YUY2")
    engine = AnyCamEngine(
        build_demo_project("testsrc", 320, 180, 30),
        preview=True,
        vcam=True,
    ).build()
    try:
        assert engine._vcam_pipeline is not None
        appsrc = engine._vcam_pipeline.get_by_name("vcam_appsrc")
        assert appsrc is not None
        assert appsrc.get_property("max-buffers") == 2
        assert appsrc.get_property("block") is False
    finally:
        if engine._vcam_pipeline is not None:
            engine._vcam_pipeline.set_state(Gst.State.NULL)
        engine.pipeline.set_state(Gst.State.NULL)


def test_idle_frame_matches_the_canvas_format():
    """The held-open node streams this card before the canvas exists."""
    rgba = idle_frame(160, 90)
    assert len(rgba) == 160 * 90 * 4
    assert any(rgba), "the idle card must not be a blank frame"


@pytest.mark.skipif(not HAS_V4L2SINK, reason="v4l2sink unavailable")
def test_idle_card_yields_to_an_attached_canvas():
    """A paused window share must not be replaced by the dark ready card."""
    output = VcamOutput()
    output._last_push = 0.0
    assert output._card_allowed() is True
    output._last_push = time.monotonic()
    assert output._card_allowed() is False
    output.set_canvas_attached(True)
    output._last_push = 0.0
    assert output._card_allowed() is False
    output.set_canvas_attached(False)
    # A rebuild detaches the tap for a few seconds; the last real frame must
    # stay on the device while the camera is probed, not the ready card.
    assert output._card_allowed() is False
    # And it must stay *flowing*: a silent gap is what Discord turns into a
    # black preview even though the canvas comes back.
    output._last_buffer = Gst.Buffer.new_wrapped(b"\0" * 64)
    held = output._held_canvas_buffer()
    assert held is not None
    assert held.pts != Gst.CLOCK_TIME_NONE
    output._detached_at -= CARD_GRACE_S + 1.0
    assert output._card_allowed() is True
    assert output._held_canvas_buffer() is None


def test_vcam_repeats_the_last_frame_when_the_canvas_goes_quiet():
    """A quiet attached canvas must not leave a consumer on black."""
    output = VcamOutput()
    output._canvas_live = True
    output._last_buffer = Gst.Buffer.new_wrapped(b"\0" * 64)
    output._last_push = time.monotonic() - LIVE_GRACE_S - 0.1
    held = output._quiet_canvas_buffer()
    assert held is not None
    assert held.pts != Gst.CLOCK_TIME_NONE
    output._last_push = time.monotonic()
    assert output._quiet_canvas_buffer() is None
    output._canvas_live = False
    assert output._quiet_canvas_buffer() is None


def test_vcam_retimestamps_canvas_frames_after_a_rebuild():
    """A rebuilt canvas resets PTS; the persistent vcam must not go late."""
    class RecordingAppsrc:
        def __init__(self):
            self.calls = []

        def emit(self, name, payload):
            self.calls.append((name, payload))
            return Gst.FlowReturn.OK

    caps = Gst.Caps.from_string(
        "video/x-raw,format=RGBA,width=4,height=4,framerate=30/1")
    buffer = Gst.Buffer.new_wrapped(b"\0" * 64)
    buffer.pts = 0
    sample = Gst.Sample.new(buffer, caps, None, None)
    output = VcamOutput()
    output.fps = 30
    output._epoch = time.monotonic() - 10.0
    output._last_canvas_pts = 100 * Gst.SECOND
    appsrc = RecordingAppsrc()
    output._appsrc = appsrc

    output.push_sample(sample)
    assert appsrc.calls and appsrc.calls[0][0] == "push-buffer"
    pushed = appsrc.calls[0][1]
    assert pushed.pts != 0
    assert pushed.pts >= 9 * Gst.SECOND
    assert output._last_buffer is pushed


def test_vcam_freezes_the_last_frame_when_the_canvas_goes_empty():
    """Removing the last source must not forward the black background."""
    output = VcamOutput()
    output._canvas_live = True
    output._last_buffer = Gst.Buffer.new_wrapped(b"frame" * 16)
    output._last_push = time.monotonic()
    output.freeze_last_frame()
    assert output._frozen is True
    # The idle loop repeats the last real frame instead of a black canvas.
    held = output._quiet_canvas_buffer()
    assert held is not None
    assert held.pts != Gst.CLOCK_TIME_NONE

    class RefusingAppsrc:
        def emit(self, *_args):
            raise AssertionError("a frozen output must ignore canvas samples")

    output._appsrc = RefusingAppsrc()
    output.push_sample(None)
    output._appsrc = None
    output.resume_canvas()
    assert output._frozen is False


def test_vcam_sink_streams_before_the_canvas_runs():
    """A consumer must not get EIO while the preview is stopped."""
    if not config.vcam_available():
        pytest.skip("no v4l2loopback device")
    device = config.loopback_device()
    output = VcamOutput()
    assert output.start(device, 640, 360, 30) is True, output.error
    try:
        # The idle card is the first buffer: it must carry the colorimetry the
        # driver asks for, or the sink dies before the preview starts.
        fixed = output._pipeline.get_by_name("vcam_caps")
        negotiation = fixed.get_property("caps").to_string()
        assert "colorimetry" in negotiation
        assert "2:4:7:1" in negotiation
        # One conversion: RGBA in, device format out.
        appsrc = output._pipeline.get_by_name("vcam_appsrc")
        assert "RGBA" in appsrc.get_property("caps").to_string()
        time.sleep(0.8)
        proc = subprocess.run(
            ["v4l2-ctl", "-d", device,
             "--set-fmt-video=width=640,height=360,pixelformat=YUYV",
             "--stream-mmap=2", "--stream-count=5"],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
    finally:
        output.stop()


def test_vcam_consumers_finds_the_open_device(tmp_path):
    device = tmp_path / "video10"
    device.write_text("")
    proc = tmp_path / "proc"
    holder = proc / "4242" / "fd"
    holder.mkdir(parents=True)
    (proc / "4242" / "comm").write_text("discord\n")
    (holder / "7").symlink_to(device)
    other = proc / "5151" / "fd"
    other.mkdir(parents=True)
    (proc / "5151" / "comm").write_text("firefox\n")
    assert config.vcam_consumers(str(device), proc=str(proc)) == ["discord"]


class _FreezeVcam(FakeVcam):
    def __init__(self) -> None:
        super().__init__()
        self.frozen = False
        self.resumes = 0

    def freeze_last_frame(self) -> None:
        self.frozen = True

    def resume_canvas(self) -> None:
        self.frozen = False
        self.resumes += 1


def test_hiding_the_last_layer_holds_and_resumes_the_vcam():
    """A scene with no visible source must keep the last real picture."""
    project = Project(name="freeze")
    scene = Scene(new_id("scene"), "Scene")
    first = Layer(new_id("layer"), "test", "First", source="ball",
                  box=(0.0, 0.0, 0.5, 1.0), z=1)
    second = Layer(new_id("layer"), "test", "Second", source="ball",
                   box=(0.5, 0.0, 0.5, 1.0), z=2)
    scene.add_layer(first)
    scene.add_layer(second)
    project.scenes = [scene]
    project.active_scene = scene.id
    project.output.canvas_w = 320
    project.output.canvas_h = 180
    project.output.fps = 30
    fake = _FreezeVcam()
    engine = AnyCamEngine(
        project, preview=True, vcam=True, vcam_output=fake,
    ).build()
    try:
        engine.start()
        deadline = time.monotonic() + 5.0
        while fake.pushed <= 0 and time.monotonic() < deadline:
            engine.pump(50)
            time.sleep(0.01)
        assert fake.pushed > 0

        engine.set_layer_visible(first.id, False)
        assert fake.frozen is False, "one source is still visible"
        engine.set_layer_visible(second.id, False)
        assert fake.frozen is True, "the last picture must be held, not black"
        engine.set_layer_visible(second.id, True)
        assert fake.frozen is False
        assert fake.resumes >= 1
    finally:
        engine.stop()


def test_engine_uses_a_session_vcam_tap():
    fake = FakeVcam()
    engine = AnyCamEngine(
        build_demo_project("testsrc", 320, 180, 30),
        preview=True,
        vcam=True,
        vcam_output=fake,
    ).build()
    try:
        assert engine.pipeline.get_by_name("vcam_appsink") is not None
        # The tap forwards the canvas RGBA frames; converting here as well as
        # in VcamOutput was a duplicated full-frame pass.
        tap = engine.pipeline.get_by_name("vcam_caps")
        assert "RGBA" in tap.get_property("caps").to_string()
        assert engine.pipeline.get_by_name("vcam_scale") is not None
        assert engine.pipeline.get_by_name("vcam_rate") is not None
        assert engine.pipeline.get_by_name("vcam_convert") is None
        assert engine._vcam_pipeline is None
        engine.start()
        for _ in range(20):
            engine.pump(50)
            time.sleep(0.01)
        assert fake.pushed > 0
        assert engine.report.vcam_active is True
    finally:
        engine.stop()


def test_ensure_vcam_device_is_a_noop_when_the_device_exists(tmp_path):
    device = tmp_path / "video10"
    device.write_text("")
    assert config.ensure_vcam_device(str(device)) == ""


def test_ensure_vcam_device_creates_the_loopback_device(tmp_path, monkeypatch):
    """The app brings its own device up instead of asking for a terminal."""
    device = tmp_path / "video10"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        device.write_text("")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert config.ensure_vcam_device(str(device)) == ""
    assert calls and calls[0][:3] == ["pkexec", "modprobe", "v4l2loopback"]
    assert "video_nr=10" in calls[0]
    assert "exclusive_caps=1" in calls[0]


def test_reload_vcam_module_uses_pkexec_with_chromium_caps(tmp_path,
                                                            monkeypatch):
    """One pkexec prompt unloads and reloads with the Chromium flag."""
    device = tmp_path / "video10"
    device.write_text("")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(config, "vcam_exclusive_caps", lambda path=None: True)
    assert config.reload_vcam_module(str(device)) == ""
    assert calls[0][:2] == ["pkexec", "/bin/sh"]
    command = calls[0][-1]
    assert "modprobe -r v4l2loopback" in command
    assert "video_nr=10" in command
    assert "exclusive_caps=1" in command


def test_reload_vcam_module_reports_when_the_node_is_in_use(tmp_path,
                                                            monkeypatch):
    """Discord holding the node must surface, not look like success."""
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, "",
            "modprobe: FATAL: Module v4l2loopback is in use.")

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    message = config.reload_vcam_module(str(tmp_path / "video10"))
    assert "in use" in message
    assert "sudo modprobe -r v4l2loopback" in message


def test_enable_script_loads_the_module_with_chromium_caps():
    """The module default hides the node from Discord/Chrome entirely."""
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1]
              / "scripts" / "enable-v4l2loopback.sh")
    text = script.read_text()
    assert "options='video_nr=10 card_label=AnyCam exclusive_caps=1'" in text
    assert "options v4l2loopback video_nr=10 card_label=AnyCam " \
        "exclusive_caps=1" in text


def test_vcam_exclusive_caps_reads_the_module_parameter(tmp_path):
    """Chromium only lists the node when the module announces capture-only."""
    param = tmp_path / "exclusive_caps"
    param.write_text("N,N,N\n")
    assert config.vcam_exclusive_caps(str(param)) is False
    param.write_text("Y,N,N\n")
    assert config.vcam_exclusive_caps(str(param)) is True
    assert config.vcam_exclusive_caps(str(tmp_path / "missing")) is None


def test_ensure_vcam_device_explains_when_it_cannot_prompt(tmp_path, monkeypatch):
    device = tmp_path / "video10"
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    message = config.ensure_vcam_device(str(device))
    assert "modprobe v4l2loopback" in message
    assert "video_nr=10" in message


@pytest.mark.skipif(not HAS_V4L2SINK, reason="v4l2sink unavailable")
def test_vcam_uses_gpu_conversion_when_available():
    """The GPU chain is preferred when it can deliver a frame."""
    if not config.vcam_available():
        pytest.skip("no v4l2loopback device")
    if not VcamOutput._gl_available():
        pytest.skip("GStreamer GL conversion unavailable")
    device = config.loopback_device()
    output = VcamOutput()
    assert output.start(device, 640, 360, 30) is True, output.error
    try:
        assert output._pipeline.get_by_name("vcam_gl_convert") is not None
        assert output._pipeline.get_by_name("vcam_convert") is None
        proc = subprocess.run(
            ["v4l2-ctl", "-d", device,
             "--set-fmt-video=width=640,height=360,pixelformat=YUYV",
             "--stream-mmap=2", "--stream-count=5"],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0, proc.stderr
    finally:
        output.stop()


def test_vcam_falls_back_to_cpu_when_gl_is_missing(monkeypatch):
    """No GL elements must mean the CPU chain, not a dead camera."""
    if not config.vcam_available():
        pytest.skip("no v4l2loopback device")
    monkeypatch.setattr(VcamOutput, "_gl_available", staticmethod(lambda: False))
    output = VcamOutput()
    assert output.start(config.loopback_device(), 640, 360, 30) is True, output.error
    try:
        assert output._pipeline.get_by_name("vcam_convert") is not None
        assert output._pipeline.get_by_name("vcam_gl_convert") is None
    finally:
        output.stop()


def test_vcam_falls_back_when_the_gl_probe_fails(monkeypatch):
    """A GPU pipeline that accepts PLAYING but never delivers cannot stick."""
    if not config.vcam_available():
        pytest.skip("no v4l2loopback device")
    monkeypatch.setattr(VcamOutput, "_gl_available", staticmethod(lambda: True))
    monkeypatch.setattr(VcamOutput, "_first_frame_flows", lambda self: False)
    output = VcamOutput()
    assert output.start(config.loopback_device(), 640, 360, 30) is True, output.error
    try:
        assert output._pipeline.get_by_name("vcam_convert") is not None
        assert output._pipeline.get_by_name("vcam_gl_convert") is None
    finally:
        output.stop()


def test_measure_conversion_reports_both_paths():
    """Diagnostics need a measured number per converter, not a guess."""
    from anycam.engine.outputs.vcam import measure_conversion

    result = measure_conversion(320, 180, 30, frames=12)
    assert result["width"] == 320 and result["height"] == 180
    assert result["fps"] == 30
    assert result["cpu"] is not None and result["cpu"] > 0
    assert result["chosen"] == ("gpu" if result["gpu"] is not None
                                else "cpu")
