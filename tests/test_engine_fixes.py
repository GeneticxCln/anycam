"""Regression tests for the engine correctness and leak fixes.

Each test names the defect it pins down.  The pixel test drives a real
GStreamer pipeline with a synthetic source; the others build (but do not start)
a graph, which is enough to exercise capture bookkeeping without a camera.
"""
from __future__ import annotations

import fcntl
import gc
import os
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

from anycam import portal, sources  # noqa: E402
from anycam.model import Effects, Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402
from anycam.pool import CaptureSpec, SourcePool  # noqa: E402
from anycam.sources import SourceChain  # noqa: E402

Gst.init(None)
pytestmark = pytest.mark.skipif(
    Gst.ElementFactory.find("compositor") is None,
    reason="GStreamer compositor unavailable")


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def project_with(*layers: Layer, width: int = 640, height: int = 360) -> Project:
    project = Project(name="fixes")
    project.output = OutputConfig(canvas_w=width, canvas_h=height, fps=30)
    scene = Scene("scene_fix", "Fix")
    for layer in layers:
        scene.add_layer(layer)
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def make_test_layer(layer_id="l_test", pattern="smpte", **kwargs) -> Layer:
    return Layer(layer_id, "test", "Test", source=pattern,
                 box=kwargs.pop("box", (0.0, 0.0, 1.0, 1.0)), z=1, **kwargs)


def fd_is_open(fd: int) -> bool:
    try:
        fcntl.fcntl(fd, fcntl.F_GETFD)
        return True
    except OSError:
        return False


def open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


# ----------------------------------------------------------- item 1: distortion
def test_pad_keeps_aspect_when_the_box_changes_live():
    """A live resize must centre the picture instead of stretching it."""
    layer = make_test_layer()
    project = project_with(layer)
    frames: list[tuple[int, int, bytes]] = []

    def on_frame(sample) -> None:
        caps = sample.get_caps().get_structure(0)
        width, height = int(caps.get_value("width")), int(caps.get_value("height"))
        ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
        if ok:
            try:
                frames.append((width, height, bytes(info.data)[: width * height * 4]))
            finally:
                sample.get_buffer().unmap(info)

    engine = AnyCamEngine(project, preview=True, on_frame=on_frame).build()
    engine.start()
    try:
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            engine.pump(50)
        pad = engine._layer_pads[layer.id]
        assert pad.get_property("sizing-policy").value_nick == "keep-aspect-ratio"
        # Half the width, full height: the 16:9 picture must stay 16:9.
        engine.set_layer_box(layer.id, (0.0, 0.0, 0.5, 1.0))
        frames.clear()
        end = time.monotonic() + 1.5
        while time.monotonic() < end and len(frames) < 8:
            engine.pump(50)
        width, height, data = frames[-1]
    finally:
        engine.stop()

    def average(x0, y0, x1, y1):
        total = count = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                offset = (y * width + x) * 4
                total += data[offset] + data[offset + 1] + data[offset + 2]
                count += 1
        return total / (3 * count)

    # 320x360 pad, 320x180 picture: the top band is empty, the middle is
    # picture.  A stretched picture would fill the top band with colour bars.
    assert average(0, 0, 40, 40) < 24
    assert average(40, 150, 280, 210) > 40


# ------------------------------------------------- item 2: attach after missing
def test_attach_capture_builds_the_live_capture_not_another_placeholder(monkeypatch):
    monkeypatch.setattr(sources, "camera_modes", lambda device: [])
    camera = Layer("l_cam", "camera", "Cam", source="/dev/video99",
                   box=(0.0, 0.0, 1.0, 1.0), z=1)
    engine = AnyCamEngine(project_with(camera), preview=True,
                          skip_layers={camera.id}).build()
    try:
        assert engine.report.camera_placeholders == ["Cam"]
        assert camera.id not in engine._layer_specs
        assert engine.attach_capture(camera.id) is True
        assert camera.id in engine._layer_specs, "no live capture was built"
        assert len(engine.pool.entries()) == 1
        assert engine.report.camera_placeholders == []
        assert camera.id not in engine.skip_layers
    finally:
        engine.pool.stop()


# ----------------------------------------------------- item 3: pool bookkeeping
def test_failed_build_releases_its_captures_and_spares_a_shared_pool():
    pool = SourcePool()
    other = CaptureSpec("test", "ball", 64, 64, 30)
    pool.acquire(other)
    layer = make_test_layer(pattern="smpte")
    # No preview/vcam/record requested: build raises after the layer is added.
    engine = AnyCamEngine(project_with(layer), pool=pool, preview=False)
    with pytest.raises(RuntimeError):
        engine.build()
    try:
        assert [entry.refs for entry in pool.entries()] == [1]
        assert pool.entry(other) is not None and pool.entry(other).refs == 1
    finally:
        pool.stop()


def test_layer_failing_mid_build_releases_its_capture(monkeypatch):
    pool = SourcePool()

    def boom(*args, **kwargs):
        raise RuntimeError("tail failed")

    monkeypatch.setattr("anycam.pipeline.layer_tail", boom)
    engine = AnyCamEngine(project_with(make_test_layer()), pool=pool, preview=True)
    with pytest.raises(RuntimeError, match="tail failed"):
        engine.build()
    assert pool.entries() == []


def test_release_never_goes_negative():
    pool = SourcePool()
    spec = CaptureSpec("test", "ball", 64, 64, 30)
    entry = pool.acquire(spec)
    pool.release(spec)
    pool.pin(spec)  # keep the entry alive so a stray release can find it
    pool.acquire(spec)
    pool.release(spec)
    pool.release(spec)
    assert entry.refs == 0
    pool.stop()


def test_detaching_one_of_two_layers_on_a_shared_capture_keeps_the_other_counted():
    first = make_test_layer("l_a", "smpte")
    second = make_test_layer("l_b", "smpte")
    engine = AnyCamEngine(project_with(first, second), preview=True).build()
    try:
        assert len(engine._specs) == 2
        engine.detach_layer("l_a")
        assert len(engine._specs) == 1
        assert engine.pool.entries()[0].refs == 1
    finally:
        engine.pool.stop()


# ------------------------------------------------------------ items 4 and 5: fds
def test_portal_remote_does_not_leak_a_descriptor():
    # Engines from earlier tests are collectable now; let them go first, or
    # the gc.collect() below frees their sockets and the count drops.
    gc.collect()
    source = os.open(os.devnull, os.O_RDONLY)
    before = open_fd_count() - 1
    fd_list = Gio.UnixFDList.new()
    fd_list.append(source)
    os.close(source)

    reply = GLib.Variant("(h)", (0,))
    connection = SimpleNamespace(
        call_with_unix_fd_list_sync=lambda *args: (reply, fd_list))

    fd = portal._open_pipewire_remote(connection, "/session")
    assert fd_is_open(fd)
    os.close(fd)
    fd_list = connection = None  # drop the list's own copy of the descriptor
    gc.collect()
    assert open_fd_count() == before


def test_engine_closes_screen_fds_when_the_build_fails():
    fd = os.open(os.devnull, os.O_RDONLY)
    engine = AnyCamEngine(project_with(make_test_layer()), preview=False,
                          screen_fds={"l_screen": fd})
    with pytest.raises(RuntimeError):
        engine.build()
    assert not fd_is_open(fd)


def test_engine_closes_screen_fds_on_stop():
    fd = os.open(os.devnull, os.O_RDONLY)
    engine = AnyCamEngine(project_with(make_test_layer()), preview=True,
                          screen_fds={"l_screen": fd})
    engine.build().start()
    engine.pump(50)
    engine.stop()
    assert not fd_is_open(fd)


def test_reattaching_a_detached_screen_closes_the_unused_remote():
    layer = Layer("l_screen2", "screen", "Screen", source="42",
                  box=(0.0, 0.0, 1.0, 1.0), z=2)
    base = make_test_layer()
    engine = AnyCamEngine(project_with(base, layer), preview=True,
                          skip_layers={layer.id}).build()
    try:
        engine._detached_screens["42"] = [{
            "chain": SourceChain(), "pad": engine._layer_pads[base.id],
            "layer_id": "old", "crop": (0.0, 0.0, 0.0, 0.0),
            "effects": Effects(), "source_size": (1, 1), "frames": 0,
            "last": 0.0}]
        assert engine.can_reattach_screen(layer)
        fd = os.open(os.devnull, os.O_RDONLY)
        assert engine.add_layer(layer, screen_fd=fd) is True
        assert not fd_is_open(fd)
    finally:
        engine.pool.stop()


# --------------------------------------------------------- items 13 and 14: size
def test_a_newly_visible_source_is_not_stalled_before_its_own_grace():
    layer = make_test_layer()
    engine = AnyCamEngine(project_with(layer), preview=True).build()
    try:
        now = time.monotonic()
        engine._start_time = now - 100.0  # the canvas has been up for a while
        engine._update_health(now)
        assert engine.report.stalled_sources == []
        engine._update_health(now + engine._stall_seconds + 1.0)
        assert engine.report.stalled_sources == [layer.id]
    finally:
        engine.pool.stop()


def test_hidden_camera_tail_uses_the_real_mode_not_the_canvas_size(monkeypatch):
    from anycam import pool as pool_module

    monkeypatch.setattr(
        pool_module, "camera_modes",
        lambda device: [sources.CameraMode("MJPG", 1280, 720, 30)])
    camera = Layer("l_hidden", "camera", "Cam", source="/dev/video99",
                   box=(0.0, 0.0, 1.0, 1.0), z=1, visible=False)
    engine = AnyCamEngine(project_with(camera, width=1920, height=1080),
                          preview=True).build()
    try:
        assert engine.layer_sources[camera.id] == (1280, 720)
        assert engine.pool.entries() == []  # hidden: nothing was opened
    finally:
        engine.pool.stop()


# ------------------------------------------------------------- VA-API encoders
def _fake_elements(monkeypatch, present):
    import anycam.pipeline as pipeline_module

    real_find = pipeline_module.Gst.ElementFactory.find

    def find(name):
        if name in present:
            return object()
        if name.startswith(("va", "nvh", "nvav")):
            return None
        return real_find(name)

    monkeypatch.setattr(pipeline_module.Gst.ElementFactory, "find", staticmethod(find))
    return pipeline_module


def test_auto_uses_vaapi_on_intel_or_amd_when_nvenc_is_missing(monkeypatch):
    pipeline_module = _fake_elements(monkeypatch, {"vah264enc", "vah265enc"})
    monkeypatch.setattr(pipeline_module, "nvenc_available", lambda codec="h264": False)
    monkeypatch.setattr(pipeline_module, "_va_encodes", lambda factory: factory in ("vah264enc", "vah265enc"))
    plan = pipeline_module.encoder_plan("auto")
    assert (plan.factory, plan.codec, plan.hardware) == ("vah264enc", "h264", True)
    assert "VA-API" in plan.note
    assert pipeline_module.encoder_plan("vaapi_h265").factory == "vah265enc"
    av1 = pipeline_module.encoder_plan("vaapi_av1")
    assert av1.factory == "vah265enc" and "AV1 unavailable" in av1.note
    assert pipeline_module.encoder_plan("vaapi", purpose="rtmp").codec == "h264"


def test_nvenc_stays_first_and_a_broken_va_driver_falls_back(monkeypatch):
    pipeline_module = _fake_elements(monkeypatch, {"vah264enc", "nvh264enc"})
    monkeypatch.setattr(pipeline_module, "nvenc_available", lambda codec="h264": codec == "h264")
    monkeypatch.setattr(pipeline_module, "_va_encodes", lambda factory: True)
    assert pipeline_module.encoder_plan("auto").factory == "nvh264enc"
    assert pipeline_module.encoder_plan("vaapi").factory == "vah264enc", "an explicit choice wins"
    monkeypatch.setattr(pipeline_module, "nvenc_available", lambda codec="h264": False)
    monkeypatch.setattr(pipeline_module, "_va_encodes", lambda factory: False)  # driver broken
    plan = pipeline_module.encoder_plan("auto")
    assert plan.factory == "x264enc" and "No hardware encoder" in plan.note


def test_a_va_encoder_gets_live_settings_and_a_gpu_upload(monkeypatch):
    import anycam.pipeline as pipeline_module

    if Gst.ElementFactory.find("vah264enc") is None:
        pytest.skip("no VA-API encoder on this machine (covered by the plan tests)")
    plan = pipeline_module.EncoderPlan("h264", "vah264enc", True, "h264parse", 12000, "")
    from anycam.demo import build_demo_project

    engine = AnyCamEngine(build_demo_project("testsrc", 320, 180, 30))
    encoder = engine._make_encoder(plan, "enc")
    assert encoder.get_property("bitrate") == 12000
    names = [e.get_factory().get_name()
             for e in AnyCamEngine._encoder_input(True, "rec", "vah264enc")]
    assert names[0] in ("vapostproc", "videoconvert")


def test_the_va_input_path_without_vapostproc_converts_to_nv12(monkeypatch):
    pipeline_module = _fake_elements(monkeypatch, {"vah264enc"})
    elements = AnyCamEngine._encoder_input(True, "rec", "vah264enc")
    names = [e.get_factory().get_name() for e in elements]
    assert names == ["videoconvert", "capsfilter"]
    assert "NV12" in elements[1].get_property("caps").to_string()
    nvenc = [e.get_factory().get_name() for e in AnyCamEngine._encoder_input(True, "rec", "nvh264enc")]
    assert "videoconvert" not in nvenc, "NVENC keeps its zero-conversion path"


def test_the_real_va_probe_is_false_without_va_elements():
    import anycam.pipeline as pipeline_module

    if Gst.ElementFactory.find("vah264enc") is not None:
        pytest.skip("VA-API present: the probe result depends on the driver")
    assert pipeline_module.va_factory("h264") == ""
