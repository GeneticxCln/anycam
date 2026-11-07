"""Regressions for the 2026-11-07 full-project audit (engine, recorder, pool, GUI)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam import effects, overlay, pipeline, pool as pool_module  # noqa: E402
from anycam.engine.outputs.recorder import ENCODER_PTS_BIAS, Recorder  # noqa: E402
from anycam.model import Layer, Project, Scene, new_id  # noqa: E402
from anycam.pipeline import AnyCamEngine, EncodedFeed  # noqa: E402
from anycam.pool import CaptureSpec, SourcePool  # noqa: E402

Gst.init(None)


def make_project(*layers: Layer, width: int = 320, height: int = 180) -> Project:
    project = Project(name="audit")
    project.output.canvas_w, project.output.canvas_h, project.output.fps = width, height, 30
    scene = Scene(new_id("scene"), "Scene")
    for layer in layers:
        scene.add_layer(layer)
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def pattern(layer_id: str = "pat", source: str = "ball") -> Layer:
    return Layer(layer_id, "test", "Pattern", source=source, box=(0, 0, 1, 1))


def run_for(engine: AnyCamEngine, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        engine.pump(50)


class FakeVcam:
    active = True
    error = ""
    width, height, fps = 320, 180, 30

    def push_sample(self, _sample): ...
    def set_canvas_attached(self, _attached): ...
    def resume_canvas(self): ...
    def poll(self): ...


# ------------------------------------------------ stop() after detaching outputs
def test_stop_is_fast_after_the_recorder_was_detached():
    """A closed valve drops EOS, so stop() used to wait out its 4 s timeout."""
    recorder = SimpleNamespace(path="/tmp/unused.mp4",
                               push_video=lambda _s: None,
                               push_audio=lambda _s: None)
    engine = AnyCamEngine(make_project(pattern()), recorder=recorder,
                          preview=True).build()
    engine.start()
    run_for(engine, 0.6)
    engine.detach_recorder()
    run_for(engine, 0.3)
    started = time.monotonic()
    report = engine.stop()
    assert time.monotonic() - started < 2.0
    assert report["error"] == ""


def test_detached_recorder_is_not_reported_as_this_engines_recording():
    recorder = SimpleNamespace(path="/tmp/unused.mp4",
                               push_video=lambda _s: None,
                               push_audio=lambda _s: None)
    engine = AnyCamEngine(make_project(pattern()), recorder=recorder,
                          preview=True).build()
    engine.start()
    run_for(engine, 0.4)
    engine.detach_recorder()
    report = engine.stop()
    assert report["record_path"] == ""
    assert report["error"] == ""


def test_stop_is_fast_after_the_vcam_was_detached():
    engine = AnyCamEngine(make_project(pattern()), preview=True, vcam=True,
                          vcam_output=FakeVcam()).build()
    engine.start()
    run_for(engine, 0.6)
    engine.detach_vcam()
    started = time.monotonic()
    engine.stop()
    assert time.monotonic() - started < 2.0


def test_a_stopped_engine_refuses_to_start_or_attach():
    engine = AnyCamEngine(make_project(pattern()), preview=True).build()
    engine.start()
    engine.stop()
    assert engine.add_layer(pattern("late")) is False
    with pytest.raises(RuntimeError, match="stopped"):
        engine.start()


# ------------------------------------------------------- recorder timelines
class FakeSource:
    def __init__(self, limit: int = 8):
        self.samples: list[Gst.Sample] = []
        self.level = 0
        self.limit = limit

    def emit(self, name, sample):
        assert name == "push-sample"
        self.samples.append(sample)
        return Gst.FlowReturn.OK

    def get_property(self, name):
        return {"current-level-buffers": self.level,
                "max-buffers": self.limit}[name]


def sample_at(pts: int, *, delta: bool = False, width: int = 64) -> Gst.Sample:
    buf = Gst.Buffer.new_wrapped(b"\0" * 8)
    buf.pts = buf.dts = pts
    if delta:
        buf.set_flags(Gst.BufferFlags.DELTA_UNIT)
    caps = Gst.Caps.from_string(
        f"video/x-h264,stream-format=avc,alignment=au,width={width},height=36")
    return Gst.Sample.new(buf, caps, None, None)


def audio_sample_at(pts: int) -> Gst.Sample:
    buf = Gst.Buffer.new_wrapped(b"\0" * 8)
    buf.pts = pts
    caps = Gst.Caps.from_string("audio/mpeg,mpegversion=4,stream-format=raw")
    return Gst.Sample.new(buf, caps, None, None)


def fake_recorder() -> tuple[Recorder, FakeSource, FakeSource]:
    recorder = Recorder()
    recorder._pipeline = object()
    recorder._video_src, recorder._audio_src = FakeSource(), FakeSource()
    recorder._video_step = Gst.SECOND // 30
    return recorder, recorder._video_src, recorder._audio_src


def out_pts(source: FakeSource) -> list[int]:
    return [s.get_buffer().pts for s in source.samples]


def test_recorder_removes_the_encoder_pts_bias():
    """Video 1000 h ahead of audio made the muxer wait and drop video."""
    recorder, video, _audio = fake_recorder()
    recorder.push_video(sample_at(ENCODER_PTS_BIAS + 5 * Gst.SECOND // 30))
    assert out_pts(video)[0] < Gst.SECOND


def test_streams_share_one_offset_after_a_canvas_rebuild():
    """Audio missing from one canvas must not shift it earlier in the file."""
    recorder, video, audio = fake_recorder()
    second = Gst.SECOND
    # Canvas 1: video and audio for two seconds.
    for i in range(60):
        recorder.push_video(sample_at(ENCODER_PTS_BIAS + i * second // 30))
    for i in range(94):
        recorder.push_audio(audio_sample_at(i * 1024 * second // 48000))
    # Canvas 2: three seconds of video only.
    recorder.begin_epoch()
    for i in range(90):
        recorder.push_video(sample_at(ENCODER_PTS_BIAS + i * second // 30))
    video_end = out_pts(video)[-1]
    # Canvas 3: audio and video again, both restarting at 0.
    recorder.begin_epoch()
    recorder.push_audio(audio_sample_at(0))
    recorder.push_video(sample_at(ENCODER_PTS_BIAS))
    first_audio, first_video = out_pts(audio)[-1], out_pts(video)[-1]
    assert first_audio >= video_end, "audio was pulled back into the gap"
    assert abs(first_audio - first_video) < second // 10


def test_full_appsrc_drops_on_a_keyframe_boundary_and_asks_for_one():
    recorder, video, _audio = fake_recorder()
    asked = []
    recorder.request_keyframe = lambda: asked.append(True)
    recorder.push_video(sample_at(ENCODER_PTS_BIAS))
    video.level = 8                                   # consumer is stuck
    recorder.push_video(sample_at(ENCODER_PTS_BIAS + 33_000_000, delta=True))
    assert asked and len(video.samples) == 1
    video.level = 0
    recorder.push_video(sample_at(ENCODER_PTS_BIAS + 66_000_000, delta=True))
    assert len(video.samples) == 1, "a delta frame after a gap corrupts the picture"
    recorder.push_video(sample_at(ENCODER_PTS_BIAS + 99_000_000))
    assert len(video.samples) == 2


def test_encoded_feed_never_leaks_a_delta_frame():
    source = FakeSource(limit=2)
    asked = []
    feed = EncodedFeed(source, lambda: asked.append(True))
    feed.push(sample_at(0))
    source.level = 2
    feed.push(sample_at(1, delta=True))
    source.level = 0
    feed.push(sample_at(2, delta=True))
    assert len(source.samples) == 1 and asked
    feed.push(sample_at(3))
    assert len(source.samples) == 2


def test_recorder_stop_returns_promptly_after_an_error(tmp_path):
    from gi.repository import GLib

    recorder = Recorder()
    assert recorder.start(str(tmp_path / "a.mp4"), 64, 36, 30)
    recorder._bus.post(Gst.Message.new_error(
        recorder._pipeline, GLib.Error("boom", "gst-core-error-quark", 1), "debug"))
    started = time.monotonic()
    result = recorder.stop()
    assert time.monotonic() - started < 2.0
    assert "boom" in result["error"]


def test_recording_codec_follows_the_environment_override(monkeypatch):
    monkeypatch.delenv("ANYCAM_ENCODER", raising=False)
    saved = pipeline.recording_codec("cpu")
    assert saved == "h264"
    monkeypatch.setenv("ANYCAM_ENCODER", "cpu")
    assert pipeline.recording_codec("nvenc_h265") == "h264"


# ------------------------------------------------------------------- pool
def test_a_removed_capture_is_released_after_the_retention_window(monkeypatch):
    shared = SourcePool()
    spec = CaptureSpec("test", "ball", 64, 64, 30)
    shared.acquire(spec)
    shared.pin(spec)
    shared.release(spec)
    shared.sweep()
    assert shared.entry(spec) is not None, "must stay for a quick re-add"
    monkeypatch.setattr(pool_module, "RETAIN_GRACE_S", 0.0)
    time.sleep(0.01)
    shared.sweep()
    assert shared.entry(spec) is None, "the device was held for the whole session"
    shared.stop()


def test_a_removed_screen_capture_is_never_expired(monkeypatch):
    shared = SourcePool()
    spec = CaptureSpec("screen", "42", 64, 64, 30)
    entry = pool_module.Entry(spec=spec, channel="c", pipeline=Gst.Pipeline.new("x"),
                              refs=0, retained=True, retained_since=1.0)
    shared._entries[spec] = entry
    monkeypatch.setattr(pool_module, "RETAIN_GRACE_S", 0.0)
    shared.sweep()
    assert shared.entry(spec) is entry, "a new screen capture means a new picker"


def test_two_modes_of_one_camera_share_one_capture(monkeypatch):
    monkeypatch.setattr("anycam.pool.camera_modes", lambda device: [])
    monkeypatch.setattr("anycam.pool.camera_probe_error", lambda device: "")
    shared = SourcePool()
    big = CaptureSpec("camera", "/dev/video77", 1920, 1080, 30)
    small = CaptureSpec("camera", "/dev/video77", 640, 360, 15)
    try:
        first = shared.acquire(big)
        second = shared.acquire(small)
        assert second is first and first.refs == 2
        assert len(shared.entries()) == 1
        assert shared.channel_for(big) == shared.channel_for(small)
        shared.release(small)
        shared.release(big)
        assert first.refs == 0
    finally:
        shared.stop()


# --------------------------------------------------------- engine visibility
def test_showing_a_layer_whose_source_cannot_open_leaves_it_hidden(monkeypatch):
    layer = pattern("hidden")
    layer.visible = False
    engine = AnyCamEngine(make_project(layer, pattern("other", "smpte")),
                          preview=True).build()
    try:
        def refuse(_spec):
            raise RuntimeError("device busy")

        monkeypatch.setattr(engine.pool, "acquire", refuse)
        assert engine.set_layer_visible("hidden", True) is False
        assert layer.visible is False
        assert engine._layer_pads["hidden"].get_property("alpha") == 0.0
        assert "device busy" in engine.report.media_errors[-1]
    finally:
        engine.pool.stop()


def test_showing_and_hiding_a_layer_still_works():
    layer = pattern("toggled")
    engine = AnyCamEngine(make_project(layer), preview=True).build()
    try:
        assert engine.set_layer_visible("toggled", False) is True
        assert engine._layer_pads["toggled"].get_property("alpha") == 0.0
        assert engine.set_layer_visible("toggled", True) is True
        assert engine._layer_pads["toggled"].get_property("alpha") == 1.0
    finally:
        engine.pool.stop()


# ------------------------------------------------------------ filter swap
def test_a_filter_swap_completes_without_any_buffer_flowing(monkeypatch):
    monkeypatch.setattr(effects, "SWAP_IDLE_AFTER_S", 0.6)
    pipe = Gst.Pipeline.new("swap")
    src = Gst.ElementFactory.make("appsrc", "s")
    src.set_property("is-live", True)
    src.set_property("caps", Gst.Caps.from_string(
        "video/x-raw,format=RGBA,width=64,height=64,framerate=30/1"))
    sink = Gst.ElementFactory.make("fakesink")
    sink.set_property("sync", False)
    slot = effects.FilterSlot("none", 0.5, None, size=(64, 64), fps=30)
    for element in (src, slot.element, sink):
        pipe.add(element)
    src.link(slot.element)
    slot.element.link(sink)
    pipe.set_state(Gst.State.PLAYING)
    for i in range(3):
        buf = Gst.Buffer.new_wrapped(bytes(64 * 64 * 4))
        buf.pts, buf.duration = i * 33_333_333, 33_333_333
        src.emit("push-buffer", buf)
    try:
        assert pipe.get_state(3 * Gst.SECOND)[1] == Gst.State.PLAYING
        slot.apply("fisheye", 0.5)          # nothing more will be pushed
        assert slot.idle.wait(6), "the swap waited for a buffer that never came"
        assert slot.key == "fisheye"
    finally:
        pipe.set_state(Gst.State.NULL)
        slot.close()


# --------------------------------------------------------- crop and mirror
def test_a_mirrored_picture_is_cropped_from_the_edge_the_user_drew(monkeypatch, tmp_path):
    from PIL import Image

    source = tmp_path / "pic.png"
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(source)
    seen = []
    monkeypatch.setattr(overlay, "prepare_rgba",
                        lambda path, w, h, crop: seen.append(crop) or Path(path))
    layer = Layer("img", "image", "Pic", source=str(source),
                  crop=(0.2, 0.1, 0.0, 0.3))
    overlay.render_layer(layer, 320, 180)
    assert seen[-1] == (0.2, 0.1, 0.0, 0.3)
    layer.effects.mirror = True
    overlay.render_layer(layer, 320, 180)
    assert seen[-1] == (0.0, 0.1, 0.2, 0.3)


# ------------------------------------------------------------------ messages
def test_setup_messages_include_exclusive_caps():
    from anycam.engine.outputs import vcam

    root = Path(pipeline.__file__).resolve().parent
    for path in (root / "pipeline.py", root / "engine/outputs/vcam.py"):
        text = path.read_text()
        assert "video_nr=10 card_label=AnyCam'" not in text, path.name
    assert vcam  # imported to be sure the module still loads


# ================================================ found by the live checks
def test_a_late_frame_after_stop_is_not_a_failed_recording():
    """Quick record start/stop marked good files as failed ("push failed: -3")."""
    recorder, video, _audio = fake_recorder()
    video.emit = lambda name, sample: Gst.FlowReturn.EOS
    recorder._closing = True
    recorder.push_video(sample_at(ENCODER_PTS_BIAS))
    assert recorder.error == ""
    recorder._closing = False
    recorder.push_video(sample_at(ENCODER_PTS_BIAS + 33_000_000))
    assert recorder.error, "a real EOS outside stop() is still reported"


def test_recording_a_running_canvas_starts_both_streams_at_zero():
    """Record on a running preview lost ~2 s of sound: the first segment kept
    the canvas running time while later ones were normalised."""
    recorder, video, audio = fake_recorder()
    recorder.begin_epoch()                       # what attach_recorder does
    second = Gst.SECOND
    recorder.push_audio(audio_sample_at(4 * second))
    recorder.push_video(sample_at(ENCODER_PTS_BIAS + 4 * second - 20_000_000))
    first_audio, first_video = out_pts(audio)[0], out_pts(video)[0]
    assert first_audio == 0
    assert 0 <= first_video < second // 10, "never negative, never far apart"


def test_the_canvas_and_encoder_colorimetry_are_pinned(monkeypatch):
    """A camera joining a running canvas changed the H.264 headers mid-file."""
    monkeypatch.setenv("ANYCAM_ENCODER", "cpu")
    recorder = SimpleNamespace(path="/tmp/unused.mp4", push_video=lambda _s: None,
                               push_audio=lambda _s: None)
    engine = AnyCamEngine(make_project(pattern()), recorder=recorder,
                          preview=True).build()
    try:
        canvas = engine.pipeline.get_by_name("canvas_caps").get_property("caps").to_string()
        raw = engine.pipeline.get_by_name("record_raw").get_property("caps").to_string()
        assert "colorimetry=(string)sRGB" in canvas
        assert "colorimetry=(string)bt709" in raw
    finally:
        engine.pool.stop()


def test_a_stopped_engine_is_freed_with_its_pipeline():
    """Signal handlers and probes kept every old engine alive: a whole graph
    (memory, sockets, descriptors) leaked on each rebuild."""
    import gc
    import weakref

    engine = AnyCamEngine(make_project(pattern()), preview=True).build()
    engine.start()
    run_for(engine, 0.4)
    engine.stop()
    assert not engine._signal_handlers and not engine._pad_probes
    ref, pipe = weakref.ref(engine), weakref.ref(engine.pipeline)
    del engine
    gc.collect()
    assert ref() is None and pipe() is None


def test_a_torn_down_capture_drops_its_probes():
    shared = SourcePool()
    spec = CaptureSpec("test", "ball", 64, 64, 30)
    entry = shared.acquire(spec)
    assert entry.probes, "the frame counter probe is tracked"
    shared.release(spec)
    assert not entry.probes
    shared.stop()


def test_captured_layers_buffer_behind_intervideosrc():
    """intervideosrc reports max latency < min; the compositor's latency query
    failed for every captured layer until a queue sat behind it."""
    from anycam.sources import layer_tail

    chain = layer_tail(pattern(), (64, 36), 64, 36, 30, "anycam-src-test")
    names = [element.get_factory().get_name() for element in chain.elements[:3]]
    assert names[:2] == ["intervideosrc", "queue"]
    assert chain.elements[1].get_property("leaky").value_nick == "downstream"


def test_a_removed_layer_leaves_the_running_canvas():
    """Removed layers stayed linked to an invisible pad until the next rebuild:
    ~23 MB per removed animation, 8 GB over the effect catalogue."""
    project = make_project(pattern("bg", "smpte"))
    engine = AnyCamEngine(project, preview=True).build()
    engine.start()
    try:
        run_for(engine, 0.5)
        baseline = engine.pipeline.get_children_count()
        for index in range(5):
            layer = Layer(new_id("layer"), "particles", "Snow", source="snow",
                          box=(0, 0, 1, 1))
            project.scene.add_layer(layer)
            assert engine.add_layer(layer)
            run_for(engine, 0.2)
            engine.detach_layer(layer.id)
            project.scene.remove_layer(layer.id)
        run_for(engine, 0.4)
        assert engine.pipeline.get_children_count() == baseline
        assert not engine.report.error
        frames = engine._frame_count
        run_for(engine, 0.5)
        assert engine._frame_count > frames, "the canvas keeps running"
    finally:
        engine.stop()


# ------------------------------------------- camera reuse across rebuilds
def _fake_camera(monkeypatch):
    from anycam.sources import CameraMode

    modes = [CameraMode("MJPG", 1920, 1080, 30), CameraMode("MJPG", 1280, 720, 30)]
    monkeypatch.setattr("anycam.pool.camera_modes", lambda device: modes)
    monkeypatch.setattr("anycam.pool.camera_probe_error", lambda device: "")


def test_a_camera_capture_is_keyed_by_the_mode_it_really_uses(monkeypatch):
    """A 1080p camera runs the same mode under 1080p and 4K canvases; keying by
    the request reopened the device on every resize."""
    from anycam.pool import capture_spec

    _fake_camera(monkeypatch)
    layer = Layer("cam", "camera", "Cam", source="/dev/video77")
    assert capture_spec(layer, 1920, 1080, 30) == capture_spec(layer, 3840, 2160, 30)


def test_a_rebuild_takes_the_running_camera_over(monkeypatch):
    _fake_camera(monkeypatch)
    shared = SourcePool()
    spec = CaptureSpec("camera", "/dev/video77", 1920, 1080, 30)
    try:
        first = shared.acquire(spec)
        shared.release(spec, linger=True)             # what engine.stop() does
        assert shared.entry(spec) is first and first.idle_since
        assert shared.acquire(spec) is first, "the device was reopened"
        shared.release(spec)                          # hiding a layer
        assert shared.entry(spec) is None, "hiding must still free the device"
    finally:
        shared.stop()


def test_a_different_mode_or_a_failed_camera_is_reopened(monkeypatch):
    _fake_camera(monkeypatch)
    shared = SourcePool()
    big = CaptureSpec("camera", "/dev/video77", 1920, 1080, 30)
    small = CaptureSpec("camera", "/dev/video77", 1280, 720, 30)
    try:
        first = shared.acquire(big)
        shared.release(big, linger=True)
        second = shared.acquire(small)
        assert second is not first and big not in shared._entries, \
            "a deliberate mode change must take effect"
        assert not first.probes, "the old capture was torn down"
        second.error = "device gone"
        shared.release(small, linger=True)
        assert shared.entry(small) is None, "a failed capture is not kept"
    finally:
        shared.stop()


def test_a_large_canvas_never_picks_a_slideshow_camera_mode():
    """The StreamCam lists 2304x1296 only as YUYV at 5 fps; a 1440p or 4K
    canvas picked it (largest size) and ran the camera at 5 fps."""
    from anycam.sources import CameraMode, choose_from_modes

    modes = [CameraMode(fmt, w, h, rate)
             for fmt in ("MJPG", "YUYV") for (w, h) in ((1280, 720), (1920, 1080))
             for rate in (5, 15, 30, 60) if not (fmt == "YUYV" and rate == 60)]
    modes.append(CameraMode("YUYV", 2304, 1296, 5))
    for canvas, rate in (((3840, 2160), 30), ((2560, 1440), 60), ((3840, 2160), 60)):
        chosen = choose_from_modes(modes, canvas[0], canvas[1], rate)
        assert (chosen.width, chosen.height, chosen.fps) == (1920, 1080, rate), chosen
        assert chosen.pixel_format == "MJPG"
    # Asking for the big mode at its own rate still gets it.
    assert choose_from_modes(modes, 2304, 1296, 5).width == 2304
    # Resolution still beats rate when 60 is asked of a 1080p30 camera.
    slow = [CameraMode("MJPG", 1920, 1080, 30), CameraMode("MJPG", 1280, 720, 60)]
    assert choose_from_modes(slow, 1920, 1080, 60).height == 1080
