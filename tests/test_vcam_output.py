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


def test_vcam_output_size_follows_the_canvas_up_to_4k():
    """A fixed camera mode is what keeps v4l2sink open across resizes."""
    assert config.vcam_output_size(3840, 2160) == (3840, 2160)
    assert config.vcam_output_size(2560, 1440) == (2560, 1440)
    assert config.vcam_output_size(5120, 2880) == (3840, 2160)
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


def test_hiding_the_last_layer_keeps_the_standby_live():
    """The permanent standby keeps the canvas live with no visible source."""
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
        before = fake.pushed
        engine.set_layer_visible(second.id, False)
        assert fake.frozen is False, "the standby must not freeze the vcam"
        deadline = time.monotonic() + 2.0
        while fake.pushed <= before and time.monotonic() < deadline:
            engine.pump(50)
            time.sleep(0.01)
        assert fake.pushed > before, "the standby must keep the canvas live"
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
    monkeypatch.setattr(config, "other_virtual_cameras", lambda *a, **k: [])
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
    monkeypatch.setattr(config, "other_virtual_cameras", lambda *a, **k: [])
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


def test_vcam_udev_capabilities_reads_the_property_udev_announces(monkeypatch):
    """Chromium reads ID_V4L_CAPABILITIES, not the module parameter."""
    calls = []

    def fake_run(cmd, timeout=5.0):
        calls.append(cmd)
        return 0, ("DEVPATH=/devices/virtual/video4linux/video10\n"
                   "E: ID_V4L_CAPABILITIES=:capture:\n")

    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(config, "run", fake_run)
    assert config.vcam_udev_capabilities("/dev/video10") == ":capture:"
    assert calls[0][:3] == ["udevadm", "info", "--query=property"]


def test_refresh_vcam_udev_triggers_a_change_after_anycam_starts(monkeypatch):
    """A capture-only module still needs udev re-read while the feed runs."""
    caps = {"value": ":video_output:"}
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(config, "vcam_udev_capabilities",
                        lambda device=None: caps["value"])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        caps["value"] = ":capture:"
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    assert config.refresh_vcam_udev("/dev/video10") == ""
    assert calls[0][:2] == ["pkexec", "udevadm"]
    assert "--action=change" in calls[0]
    assert "--sysname-match" in calls[0]
    assert "video10" in calls[0]


def test_refresh_vcam_udev_skips_a_node_chromium_already_lists(monkeypatch):
    """A correct cached value must not trigger a password prompt."""
    monkeypatch.setattr(config, "vcam_udev_capabilities",
                        lambda device=None: ":capture:")
    calls = []
    monkeypatch.setattr(config.subprocess, "run",
                        lambda *args, **kwargs: calls.append(args))
    assert config.refresh_vcam_udev("/dev/video10") == ""
    assert calls == []


def test_reload_vcam_module_refuses_to_unload_other_virtual_cameras(monkeypatch):
    """The reload removes every loopback node, not only AnyCam's."""
    calls = []
    monkeypatch.setattr(config.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(config, "other_virtual_cameras",
                        lambda *a, **k: ["/dev/video11 (OBS Virtual Camera)"])
    message = config.reload_vcam_module("/dev/video10")
    assert "OBS Virtual Camera" in message
    assert calls == [], "pkexec must not run when another camera would be lost"


def test_other_virtual_cameras_ignores_physical_nodes_and_its_own(tmp_path):
    virtual = tmp_path / "devices" / "virtual" / "video4linux"
    physical = tmp_path / "devices" / "pci" / "usb"
    sysfs = tmp_path / "class"
    for base, name, label in ((virtual, "video10", "AnyCam"),
                              (virtual, "video11", "OBS Virtual Camera"),
                              (physical, "video0", "StreamCam")):
        node = base / name
        node.mkdir(parents=True)
        (node / "name").write_text(label + "\n")
        (sysfs).mkdir(exist_ok=True)
        (sysfs / name).symlink_to(node)
    assert config.other_virtual_cameras("/dev/video10", str(sysfs)) == [
        "/dev/video11 (OBS Virtual Camera)"]


# ------------------------------------------- one-time virtual camera setup
def test_setup_is_needed_until_the_driver_is_right_and_permanent(tmp_path, monkeypatch):
    device = tmp_path / "video10"
    device.write_text("")
    state = {"caps": False, "permanent": False, "writable": False}
    monkeypatch.setattr(config, "vcam_exclusive_caps", lambda path=None: state["caps"])
    monkeypatch.setattr(config, "_vcam_configured_permanently",
                        lambda *a, **k: state["permanent"])
    monkeypatch.setattr(config.os, "access", lambda path, mode: state["writable"])
    assert "exclusive_caps=1" in config.vcam_setup_needed(str(device))
    state["caps"] = True
    assert "permanent" in config.vcam_setup_needed(str(device))
    state["permanent"] = True
    assert "cannot tell apps" in config.vcam_setup_needed(str(device))
    state["writable"] = True
    assert config.vcam_setup_needed(str(device)) == ""
    monkeypatch.setattr(config, "vcam_exclusive_caps", lambda path=None: None)
    assert "not loaded" in config.vcam_setup_needed(str(device))


def test_the_effective_exclusive_caps_is_the_last_one_modprobe_reads():
    eff = config._effective_exclusive_caps
    assert eff("options snd_hda_intel power_save=0\n") is None
    assert eff("# options v4l2loopback exclusive_caps=1\n") is None
    assert eff("options v4l2loopback video_nr=10 exclusive_caps=1\n") is True
    # The stale file that broke the first setup: read after AnyCam's, so 0 won.
    assert eff("options v4l2loopback video_nr=10 card_label=AnyCam exclusive_caps=1\n"
               "options v4l2loopback video_nr=10 card_label=AnyCam exclusive_caps=0\n") is False
    assert eff("options v4l2loopback exclusive_caps=Y,Y\n") is True


def test_overriding_files_are_named(tmp_path, monkeypatch):
    (tmp_path / "zz-other.conf").write_text("options v4l2loopback exclusive_caps=0\n")
    (tmp_path / "fine.conf").write_text("options v4l2loopback exclusive_caps=1\n")
    monkeypatch.setattr(config, "VCAM_MODPROBE_CONF", tmp_path / "anycam.conf")
    (tmp_path / "anycam.conf").write_text("options v4l2loopback exclusive_caps=0\n")
    assert config.vcam_caps_overrides((str(tmp_path),)) == [str(tmp_path / "zz-other.conf")]


def test_setup_writes_permanent_settings_with_one_prompt(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(config, "vcam_exclusive_caps", lambda path=None: False)
    monkeypatch.setattr(config, "other_virtual_cameras", lambda *a, **k: [])
    monkeypatch.setattr(config, "vcam_setup_needed", lambda device=None: "")
    assert config.setup_vcam_system("/dev/video10") == ""
    assert len(calls) == 1 and calls[0][:3] == ["pkexec", "/bin/sh", "-c"]
    script = calls[0][3]
    assert "options v4l2loopback video_nr=10 card_label=AnyCam exclusive_caps=1" in script
    assert str(config.VCAM_MODULES_LOAD) in script
    assert 'ATTR{name}=="AnyCam"' in script and "chmod 0664" in script
    assert "modprobe -r v4l2loopback" in script
    assert "cat > /etc/modprobe.d/anycam.conf" in script, "AnyCam's own file is replaced"
    assert subprocess.run(["sh", "-n", "-c", script]).returncode == 0


def test_setup_reports_a_camera_still_in_use(monkeypatch):
    monkeypatch.setattr(config.subprocess, "run", lambda cmd, **kw:
                        subprocess.CompletedProcess(cmd, config.VCAM_SETUP_BUSY, "", ""))
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(config, "vcam_exclusive_caps", lambda path=None: False)
    monkeypatch.setattr(config, "other_virtual_cameras", lambda *a, **k: [])
    message = config.setup_vcam_system("/dev/video10")
    assert "has the virtual camera open" in message


def test_setup_refuses_to_remove_another_apps_virtual_camera(monkeypatch):
    monkeypatch.setattr(config, "vcam_exclusive_caps", lambda path=None: False)
    monkeypatch.setattr(config, "other_virtual_cameras",
                        lambda *a, **k: ["/dev/video11 (OBS Virtual Camera)"])
    monkeypatch.setattr(config.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
    assert "OBS Virtual Camera" in config.setup_vcam_system("/dev/video10")


def test_a_missing_password_agent_is_started_and_asked_again(monkeypatch):
    from anycam import privileged

    results = [subprocess.CompletedProcess(
        [], 127, "", "Error creating textual authentication agent: no tty"),
        subprocess.CompletedProcess([], 0, "", "")]
    started = []
    monkeypatch.setattr(privileged.subprocess, "run", lambda cmd, **kw: results.pop(0))
    monkeypatch.setattr(privileged.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(privileged, "installed_agent", lambda: "/usr/lib/agent")
    monkeypatch.setattr(privileged, "start_agent", started.append)
    monkeypatch.setattr(privileged, "AGENT_START_S", 0)
    assert privileged.run_as_root(["true"]).returncode == 0
    assert started == ["/usr/lib/agent"]


def test_refresh_uses_the_writable_uevent_before_asking_for_root(monkeypatch):
    caps = iter([":video_output:", ":capture:"])
    monkeypatch.setattr(config, "vcam_udev_capabilities", lambda dev=None: next(caps))
    monkeypatch.setattr(config, "trigger_vcam_uevent", lambda dev=None: True)
    monkeypatch.setattr(config.subprocess, "run", lambda *a, **k: pytest.fail("asked for root"))
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert config.refresh_vcam_udev("/dev/video10") == ""
