import shutil
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
except Exception:  # pragma: no cover - platform without GStreamer
    HAS_COMPOSITOR = False

from anycam import config
from anycam.model import Layer, Project, Scene
from anycam.pipeline import AnyCamEngine, encoder_bitrate, verify_mp4

pytestmark = pytest.mark.skipif(not HAS_COMPOSITOR, reason="GStreamer compositor unavailable")


def make_project(width=640, height=360, fps=30):
    project = Project(name="test")
    project.output.canvas_w = width
    project.output.canvas_h = height
    project.output.fps = fps
    scene = Scene("scene_test", "Test")
    scene.add_layer(Layer("layer_test", "test", "Pattern", box=(0, 0, 1, 1), z=1))
    scene.add_layer(Layer("layer_text", "text", "Caption", source="AnyCam",
                          box=(0.05, 0.05, 0.35, 0.12), z=10, font_size=0.06))
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def test_screen_flip_method_override(monkeypatch):
    monkeypatch.delenv("ANYCAM_SCREEN_FLIP", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("ANYCAM_SCREEN_FLIP", "vertical")
    assert config.screen_flip_method() == "vertical-flip"
    monkeypatch.setenv("ANYCAM_SCREEN_FLIP", "180")
    assert config.screen_flip_method() == "rotate-180"
    monkeypatch.setenv("ANYCAM_SCREEN_FLIP", "not-a-method")
    assert config.screen_flip_method() == "automatic"
    # Automatic must not force a KWin rotation: that rotates the source itself,
    # so the preview and every vcam consumer are upside down too.
    monkeypatch.delenv("ANYCAM_SCREEN_FLIP")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert config.screen_flip_method() == "automatic"
    assert config.screen_flip_method("rotate-180") == "rotate-180"


def test_build_chain_uses_the_project_screen_flip():
    from anycam.sources import build_chain

    layer = Layer("screen_flip_layer", "screen", "Screen", source="123")
    chain = build_chain(layer, 320, 180, 30, screen_flip="vertical-flip")
    flip = next(element for element in chain.elements
                if element.get_factory().get_name() == "videoflip")
    assert flip.get_property("method").value_nick == "vertical-flip"


def test_screen_capture_uploads_pipewire_dmabuf_frames():
    if Gst.ElementFactory.find("pipewiresrc") is None:
        pytest.skip("pipewiresrc unavailable")
    from anycam.sources import screen_chain

    chain = screen_chain(1, 320, 180, 30)
    names = [element.get_factory().get_name() for element in chain.elements
             if element.get_factory() is not None]
    sources = [element for element in chain.elements
               if element.get_factory() is not None
               and element.get_factory().get_name() == "pipewiresrc"]
    assert sources
    # The canvas must own the clock.  Letting pipewiresrc provide the pipeline
    # clock ties the whole compositor to the share's damage cadence, which is
    # what turns a static/off-screen window share into a ~1 fps preview.
    assert sources[0].get_property("provide-clock") is False
    assert "videoflip" in names
    flip = next(element for element in chain.elements
                if element.get_factory().get_name() == "videoflip")
    assert flip.get_property("method").value_nick == config.screen_flip_method()
    if all(Gst.ElementFactory.find(name) is not None
           for name in ("glupload", "glcolorconvert", "gldownload")):
        assert "glupload" in names
        assert sources[0].get_property("always-copy") is False
    else:
        assert sources[0].get_property("always-copy") is True
    # One-slot queue leaking downstream: stale frames are dropped rather than
    # back-pressuring the screencast when the window gains focus.
    queues = [element for element in chain.elements
              if element.get_factory().get_name() == "queue"]
    assert len(queues) == 1
    assert queues[0].get_property("leaky") == 2
    assert queues[0].get_property("max-size-buffers") == 1
    elements = [element for element in chain.elements
                if element.get_factory() is not None]
    convert = next(index for index, element in enumerate(elements)
                   if element.get_factory().get_name() == "videoconvert")
    assert elements[convert + 1].get_factory().get_name() == "capsfilter"
    format_caps = elements[convert + 1].get_property("caps").get_structure(0)
    assert format_caps.get_value("format") == "RGBA"
    caps = [element for element in chain.elements
            if element.get_factory().get_name() == "capsfilter"]
    assert caps
    assert all("framerate" not in element.get_property("caps").to_string()
               for element in caps)


@pytest.mark.parametrize("sequences, expected_frames", [
    ([None, None, None, None], 2),
    ([1, 2, 3, 4], 4),
    ([1, 1, 1, 1], 1),
])
def test_screen_health_uses_sequence_then_timestamp(monkeypatch, sequences, expected_frames):
    from anycam import pipeline as pipeline_module
    from anycam.sources import SourceChain, make, caps_filter

    src = make("appsrc", is_live=True, format=Gst.Format.TIME)
    caps = Gst.Caps.from_string("video/x-raw,format=RGBA,width=32,height=18,framerate=0/1")
    src.set_property("caps", caps)
    tail = caps_filter(caps.to_string())
    chain = SourceChain(elements=[src, tail], tail=tail, source_size=(32, 18))
    chain.check_element = src
    monkeypatch.setattr(pipeline_module, "build_chain", lambda *args, **kwargs: chain)
    project = make_project(32, 18, 30)
    project.scene.layers = [Layer("screen", "screen", "Screen", source="123")]
    engine = AnyCamEngine(project, preview=True).build()
    engine.start()
    try:
        for pts, sequence in zip((0, 0, 0, Gst.SECOND), sequences):
            buf = Gst.Buffer.new_allocate(None, 32 * 18 * 4, None)
            buf.pts = pts
            if sequence is not None:
                buf.offset = sequence
            src.emit("push-buffer", buf)
            time.sleep(0.05)
        deadline = time.monotonic() + 2
        while engine._layer_frames.get("screen", 0) < expected_frames and time.monotonic() < deadline:
            time.sleep(0.01)
        assert engine._layer_frames["screen"] == expected_frames
        engine._update_health(engine._layer_last["screen"] + 3)
        assert engine.report.stalled_sources == ["screen"]
    finally:
        engine.stop()


@pytest.mark.parametrize("has_sequence", [False, True])
def test_two_screens_with_repeated_pts_keep_changing_pixels(monkeypatch, has_sequence):
    """Fresh source counters must not hide a frozen compositor picture."""
    from anycam import pipeline as pipeline_module
    from anycam.sources import SourceChain, make, caps_filter
    from helpers.frames import Frame

    sources = []
    timestamps = [[], []]

    def build_screen(_layer, width, height, *_args, **_kwargs):
        src = make("appsrc", is_live=True, format=Gst.Format.TIME)
        caps = f"video/x-raw,format=RGBA,width={width},height={height},framerate=0/1"
        src.set_property("caps", Gst.Caps.from_string(caps))
        tail = caps_filter(caps)
        chain = SourceChain(elements=[src, tail], tail=tail,
                            source_size=(width, height))
        chain.check_element = src
        index = len(sources)
        sources.append(src)

        def inspect_timestamp(_pad, info):
            timestamps[index].append(info.get_buffer().pts)
            return Gst.PadProbeReturn.OK

        tail.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, inspect_timestamp)
        return chain

    monkeypatch.setattr(pipeline_module, "build_chain", build_screen)
    project = make_project(64, 36, 30)
    project.scene.layers = [
        Layer("left", "screen", "Left", source="123", box=(0, 0, 0.5, 1)),
        Layer("right", "screen", "Right", source="124", box=(0.5, 0, 0.5, 1)),
    ]
    seen = [[], []]

    def on_frame(sample):
        buf = sample.get_buffer()
        ok, mapped = buf.map(Gst.MapFlags.READ)
        assert ok
        try:
            frame = Frame(64, 36, bytes(mapped.data))
        finally:
            buf.unmap(mapped)
        for i, x in enumerate((16, 48)):
            pixel = frame.pixel(x, 18)
            if not seen[i] or seen[i][-1] != pixel:
                seen[i].append(pixel)

    engine = AnyCamEngine(project, preview=True, on_frame=on_frame).build().start()
    palettes = [((255, 0, 0, 255), (0, 0, 255, 255)),
                ((0, 255, 0, 255), (255, 255, 0, 255))]
    try:
        start = time.monotonic()
        sequence = 0
        while time.monotonic() - start < 2.5:
            elapsed = time.monotonic() - start
            sequence += 1
            for i, src in enumerate(sources):
                color = palettes[i][int(elapsed * 5) % 2]
                buf = Gst.Buffer.new_allocate(None, 32 * 36 * 4, None)
                buf.fill(0, bytes(color) * (32 * 36))
                buf.pts = 0  # niri's producer timestamp does not advance.
                if has_sequence:
                    buf.offset = sequence
                assert src.emit("push-buffer", buf) == Gst.FlowReturn.OK
            engine.pump(20)
            time.sleep(0.01)
        engine.pump(50)
        assert not engine.report.error, engine.report.error
        for i in range(2):
            assert set(palettes[i]).issubset(seen[i]), seen
            assert len(seen[i]) >= 6, seen
            assert len(set(timestamps[i])) > 10, timestamps
            assert timestamps[i] == sorted(timestamps[i])
        # Retiming must not make repeated identities look fresh.
        engine._update_health(time.monotonic())
        if has_sequence:
            assert engine.report.stalled_sources == []
        else:
            assert engine.report.stalled_sources == ["left", "right"]
    finally:
        engine.stop()


def test_screen_health_measures_bursty_input_and_recovers():
    project = make_project()
    project.scene.layers = [Layer("screen", "screen", "Screen", source="123")]
    engine = AnyCamEngine(project, preview=True)
    engine._layer_pads["screen"] = object()
    engine._start_time = 100.0
    # Three frames in a short burst once per second must stay SLOW between
    # bursts even though the canvas keeps rendering at its configured rate.
    for step in range(1, 17):
        now = 100 + step * 0.25
        engine._layer_frames["screen"] = (step // 4) * 3
        engine._layer_last["screen"] = 100 + step // 4
        engine._update_health(now)
        health = engine.report.source_health["screen"]
        if step < 8:
            assert health["fps"] is None
            assert not health["slow"]
        else:
            assert health["fps"] < 4
            assert health["slow"]
        assert not engine.report.stalled_sources
    # A resumed 30 fps input clears the rolling low-rate state.
    for step in range(1, 17):
        now = 104 + step * 0.25
        engine._layer_frames["screen"] = 12 + round(step * 7.5)
        engine._layer_last["screen"] = now
        engine._update_health(now)
    assert engine.report.source_health["screen"]["fps"] == 30
    assert not engine.report.source_health["screen"]["slow"]
    engine._update_health(now + 3)
    assert engine.report.stalled_sources == ["screen"]
    assert len(engine._source_rate_history["screen"]) <= 16


def test_screen_health_uses_elapsed_time_and_resets_on_restart():
    project = make_project(fps=5)
    project.scene.layers = [Layer("screen", "screen", "Screen", source="123")]
    engine = AnyCamEngine(project, preview=True)
    engine._layer_pads["screen"] = object()
    engine._start_time = 10
    engine._layer_frames["screen"] = 30
    engine._layer_last["screen"] = 13
    engine._update_health(13)
    assert engine.report.source_health["screen"]["fps"] == 10
    assert not engine.report.source_health["screen"]["slow"]
    engine._layer_frames["screen"] = 0
    engine._layer_last["screen"] = 14
    engine._update_health(14)
    assert engine.report.source_health["screen"]["fps"] is None
    assert not engine.report.source_health["screen"]["slow"]


def test_two_screen_layers_have_unique_elements():
    if Gst.ElementFactory.find("pipewiresrc") is None:
        pytest.skip("pipewiresrc unavailable")
    project = make_project(320, 180, 30)
    project.scene.layers = [
        Layer("screen_a", "screen", "A", source="123", box=(0, 0, 0.5, 1)),
        Layer("screen_b", "screen", "B", source="124", box=(0.5, 0, 0.5, 1)),
    ]
    engine = AnyCamEngine(project, preview=True).build()
    try:
        assert set(engine._layer_pads) == {"screen_a", "screen_b"}
        names = [element.get_name() for chain in engine.chains for element in chain.elements]
        assert len(names) == len(set(names))
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_empty_layout_shows_the_anycam_standby_background():
    project = make_project(320, 180, 30)
    project.scenes[0].layers = []
    engine = AnyCamEngine(project, preview=True).build()
    try:
        assert engine.scene.layers == []
        assert engine._layer_pads == {}
        pad = engine._background_pad
        assert pad is not None
        assert pad.get_property("zorder") == 0
        assert pad.get_property("alpha") == 1.0
        assert pad.get_property("width") == 320
        assert pad.get_property("height") == 180

        chain = engine._background_chain
        assert chain is not None
        titles = [element.get_property("text") for element in chain.elements
                  if element.get_factory() is not None
                  and element.get_factory().get_name() == "textoverlay"]
        assert "AnyCam" in titles
        sources = [element for element in chain.elements
                   if element.get_factory() is not None
                   and element.get_factory().get_name() == "videotestsrc"]
        assert sources and sources[0].get_property("is-live") is True
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_removing_the_last_source_leaves_the_live_standby():
    """The compositor must never end up with zero pads after a removal."""
    project = make_project(320, 180, 30)
    project.scenes[0].layers = [layer for layer in project.scenes[0].layers
                                if layer.id == "layer_test"]
    engine = AnyCamEngine(project, preview=True).build()
    try:
        engine.start()
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            engine.pump(50)
        assert engine._frame_count > 5

        engine.detach_layer("layer_test")
        project.scenes[0].remove_layer("layer_test")
        assert engine._background_pad is not None
        assert engine._layer_pads == {}

        settled = engine._frame_count
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            engine.pump(50)
        assert engine._frame_count > settled, "the standby stopped producing"
        assert engine.report.error == "", engine.report.error
    finally:
        engine.stop()


def test_engine_composites_and_records(tmp_path):
    output = tmp_path / "composite.mp4"
    engine = AnyCamEngine(make_project(), record_path=output).build()
    report = engine.run(2.0)
    assert report["error"] == "", report["error"]
    assert report["frames"] > 20
    assert report["record_ok"] is True
    assert output.exists() and output.stat().st_size > 1000
    ok, info = verify_mp4(str(output), width=640, height=360, fps=30, min_seconds=1.0)
    assert ok, info
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "h264"


def test_encoder_auto_prefers_nvenc_and_cuda_input(monkeypatch):
    if Gst.ElementFactory.find("nvh264enc") is None:
        pytest.skip("nvh264enc unavailable")
    from anycam import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "nvenc_available",
                        lambda *_args, **_kwargs: True)
    project = make_project(320, 180, 30)
    project.output.encoder = "auto"
    engine = AnyCamEngine(project, preview=True)
    enc, hardware = engine._make_record_encoder()
    assert hardware is True
    assert enc.get_factory().get_name() == "nvh264enc"
    assert engine.report.encoder == "nvh264enc"
    assert "NVENC" in engine.report.encoder_note


def test_cpu_encoder_keeps_i420_conversion(monkeypatch):
    from anycam import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "nvenc_available",
                        lambda *_args, **_kwargs: True)
    project = make_project(320, 180, 30)
    project.output.encoder = "cpu"
    engine = AnyCamEngine(project, preview=True)
    enc, hardware = engine._make_record_encoder()
    assert hardware is False
    assert enc.get_factory().get_name() == "x264enc"
    path = engine._encoder_input(False, "test")
    assert [element.get_factory().get_name() for element in path] == [
        "videoconvert", "capsfilter"]
    assert path[1].get_property("caps").get_structure(0).get_value("format") == "I420"


def test_hardware_recording_uses_cuda_upload(monkeypatch, tmp_path):
    if Gst.ElementFactory.find("nvh264enc") is None:
        pytest.skip("nvh264enc unavailable")
    if Gst.ElementFactory.find("cudaupload") is None:
        pytest.skip("cudaupload unavailable")
    from anycam import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "nvenc_available",
                        lambda *_args, **_kwargs: True)
    project = make_project(320, 180, 30)
    project.output.encoder = "nvenc"
    engine = AnyCamEngine(project, preview=True,
                          record_path=tmp_path / "hw.mp4").build()
    try:
        assert engine.report.encoder == "nvh264enc"
        assert engine.pipeline.get_by_name("record_cuda_upload") is not None
        assert engine.pipeline.get_by_name("record_raw") is None
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_rtmp_bitrate_scales_above_1080p():
    """A 4K stream must not still be encoded at the 1080p bitrate."""
    assert encoder_bitrate("h264", 1920, 1080) == 12000
    assert encoder_bitrate("h264", 2560, 1440) == 18000
    assert encoder_bitrate("h264", 3840, 2160) == 34000
    assert encoder_bitrate("h265", 3840, 2160) == 28000


def test_encoder_plan_falls_back_to_h264_for_rtmp(monkeypatch):
    from anycam import pipeline as pipeline_module

    monkeypatch.setattr(
        pipeline_module, "nvenc_available",
        lambda codec="h264": codec in {"h264", "h265", "av1"})
    plan = pipeline_module.encoder_plan("nvenc_av1")
    assert plan.codec == "av1"
    assert "AV1 NVENC" in plan.note
    rtmp = pipeline_module.encoder_plan("nvenc_av1", purpose="rtmp")
    assert rtmp.codec == "h264"
    assert "RTMP/FLV uses H.264" in rtmp.note

    monkeypatch.setattr(
        pipeline_module, "nvenc_available",
        lambda codec="h264": codec == "h265")
    fallback = pipeline_module.encoder_plan("nvenc_av1")
    assert fallback.codec == "h265"
    assert "AV1 unavailable" in fallback.note

    monkeypatch.setattr(pipeline_module, "nvenc_available",
                        lambda _codec="h264": False)
    assert pipeline_module.encoder_plan("nvenc_h265").codec == "h264"


@pytest.mark.parametrize(("preference", "factory", "codec", "probe_codec"), [
    ("nvenc_h265", "nvh265enc", "h265", "hevc"),
    ("nvenc_av1", "nvav1enc", "av1", "av1"),
])
def test_hardware_hevc_and_av1_record_to_mp4(monkeypatch, tmp_path,
                                             preference, factory, codec,
                                             probe_codec):
    from anycam import pipeline as pipeline_module

    if Gst.ElementFactory.find(factory) is None:
        pytest.skip(f"{factory} unavailable")
    if Gst.ElementFactory.find(pipeline_module.ENCODER_PARSERS[codec]) is None:
        pytest.skip(f"{codec} parser unavailable")
    monkeypatch.setattr(pipeline_module, "nvenc_available",
                        lambda *_args, **_kwargs: True)
    project = make_project(320, 180, 30)
    project.output.encoder = preference
    out = tmp_path / f"{codec}.mp4"
    engine = AnyCamEngine(project, record_path=out).build()
    report = engine.run(1.0)
    assert report["error"] == "", report["error"]
    assert report["video_codec"] == codec
    assert report["encoder"] == factory
    ok, info = verify_mp4(str(out), width=320, height=180, fps=30,
                          min_seconds=0.5)
    assert ok, info
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == probe_codec


def test_engine_delivers_preview_frames():
    frames = []
    engine = AnyCamEngine(make_project(320, 180, 30), preview=True,
                          on_frame=frames.append).build()
    report = engine.run(1.0)
    assert report["error"] == "", report["error"]
    assert frames, "preview appsink never produced a sample"


class _PreviewSample:
    """The slice of ``Gst.Sample`` the appsink callback uses."""

    def __init__(self, buffer, caps):
        self._buffer = buffer
        self._caps = caps

    def get_buffer(self):
        return self._buffer

    def get_caps(self):
        return self._caps


class _PreviewSink:
    def __init__(self, sample):
        self._sample = sample

    def emit(self, _name):
        return self._sample


def test_preview_drops_a_frame_without_dimensions_and_logs(capsys):
    """Caps without width/height are rejected instead of painting black."""
    engine = AnyCamEngine(make_project(320, 180, 30))
    caps = Gst.Caps.from_string("video/x-raw,format=RGBA")
    buffer = Gst.Buffer.new_wrapped(b"\x00" * 8)
    result = engine._on_preview_sample(
        _PreviewSink(_PreviewSample(buffer, caps)))

    assert result == Gst.FlowReturn.OK
    assert engine._pending_sample is None
    assert engine._preview_problem_count == 1
    err = capsys.readouterr().err
    assert "[preview] dropped frame:" in err
    assert "caps without dimensions" in err


def test_preview_drops_a_frame_with_the_wrong_format_and_logs(capsys):
    """A non-RGBA frame would be read with the wrong stride, so it is dropped."""
    engine = AnyCamEngine(make_project(320, 180, 30))
    caps = Gst.Caps.from_string("video/x-raw,format=RGBx,width=4,height=4")
    buffer = Gst.Buffer.new_wrapped(b"\x00" * 64)
    result = engine._on_preview_sample(
        _PreviewSink(_PreviewSample(buffer, caps)))

    assert result == Gst.FlowReturn.OK
    assert engine._pending_sample is None
    assert engine._preview_problem_count == 1
    err = capsys.readouterr().err
    assert "unexpected preview format" in err


def test_preview_branch_keeps_only_the_newest_frame():
    """A stalled GUI must drop stale frames instead of blocking the compositor."""
    engine = AnyCamEngine(make_project(320, 180, 30), preview=True).build()
    try:
        queue = engine.pipeline.get_by_name("queue_preview")
        assert queue is not None
        assert queue.get_property("max-size-buffers") == 1
        assert queue.get_property("max-size-time") == 0
        assert queue.get_property("leaky") == 2  # downstream: drop stale
        format_caps = engine.pipeline.get_by_name("preview_format")
        assert format_caps is not None
        assert format_caps.get_property("caps").get_structure(0).get_value("format") == "RGBA"
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_set_layer_box_moves_the_compositor_pad():
    engine = AnyCamEngine(make_project(320, 180, 30), preview=True).build()
    try:
        engine.set_layer_box("layer_test", (0.25, 0.1, 0.5, 0.4))
        pad = engine._layer_pads["layer_test"]
        assert pad.get_property("xpos") == 80
        assert pad.get_property("ypos") == 18
        assert pad.get_property("width") == 160
        assert pad.get_property("height") == 72
        assert engine.scene.layer("layer_test").box == (0.25, 0.1, 0.5, 0.4)
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_layer_opacity_and_order_are_applied_live():
    engine = AnyCamEngine(make_project(320, 180, 30), preview=True).build()
    try:
        engine.set_layer_alpha("layer_text", 0.4)
        text_pad = engine._layer_pads["layer_text"]
        assert text_pad.get_property("alpha") == pytest.approx(0.4)
        assert engine.scene.layer("layer_text").alpha == pytest.approx(0.4)

        engine.scene.layer("layer_test").z = 20
        engine.refresh_layer_order()
        assert engine._background_pad.get_property("zorder") == 0
        assert engine._layer_pads["layer_test"].get_property("zorder") == 2
        assert text_pad.get_property("zorder") == 1
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_face_overlay_is_a_compositor_pad(tmp_path):
    from anycam import overlay

    png = overlay.render_face_frame(out=tmp_path / "face.png")
    project = make_project(320, 180, 30)
    project.scene.add_layer(Layer(
        "layer_face", "face", "Face overlay", source=str(png),
        box=(0.3, 0.2, 0.4, 0.5), z=20, visible=False,
    ))
    engine = AnyCamEngine(project, preview=True).build()
    try:
        pad = engine._layer_pads["layer_face"]
        assert pad.get_property("width") == 128
        assert pad.get_property("height") == 90
        assert pad.get_property("alpha") == 0.0
        engine.set_layer_visible("layer_face", True)
        assert pad.get_property("alpha") == 1.0
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


class _FakePad:
    def __init__(self):
        self.props = {"xpos": 0, "ypos": 0, "width": 0, "height": 0,
                      "zorder": 0, "alpha": 0.0}

    def set_property(self, name, value):
        self.props[name] = value

    def get_property(self, name):
        return self.props.get(name)


def test_removed_screen_layer_reattaches_the_same_source():
    """The portal stream is bound to pipewiresrc: keep it, relink its pad."""
    project = make_project(320, 180, 30)
    engine = AnyCamEngine(project, preview=True).build()
    try:
        screen = Layer("screen_live", "screen", "Screen", source="86",
                       box=(0.0, 0.0, 1.0, 1.0), z=5)
        pad = _FakePad()
        chain = object()
        engine.scene.add_layer(screen)
        engine._layer_pads[screen.id] = pad
        engine._layer_chains[screen.id] = chain
        engine.layer_sources[screen.id] = (1920, 1080)
        pipeline = engine.pipeline

        engine.detach_layer(screen.id)
        assert pad.get_property("alpha") == 0.0
        assert screen.id not in engine._layer_pads
        assert engine._detached_screens["86"][0]["chain"] is chain
        project.scene.remove_layer(screen.id)

        readded = Layer("screen_again", "screen", "Screen", source="86",
                        box=(0.0, 0.0, 1.0, 1.0), z=5)
        project.scene.add_layer(readded)
        assert engine.add_layer(readded) is True
        assert engine.pipeline is pipeline, "re-add rebuilt the canvas"
        assert engine._layer_pads[readded.id] is pad
        assert engine._layer_chains[readded.id] is chain
        assert pad.get_property("alpha") == 1.0
        assert "86" not in engine._detached_screens
    finally:
        engine.stop()


def test_build_without_output_branch_is_rejected():
    with pytest.raises(RuntimeError, match="no output branch"):
        AnyCamEngine(make_project(), preview=False).build()


def test_skipped_camera_builds_a_placeholder_pad():
    project = make_project(320, 180, 30)
    project.scene.add_layer(Layer(
        "layer_lost", "camera", "Lost cam", source="/dev/anycam-missing",
        box=(0.5, 0.5, 0.5, 0.5), z=20,
    ))
    engine = AnyCamEngine(project, preview=True,
                          skip_layers={"layer_lost"}).build()
    try:
        pad = engine._layer_pads["layer_lost"]
        assert pad.get_property("width") == 160
        assert pad.get_property("height") == 90
        assert engine.report.camera_placeholders == ["Lost cam"]
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_all_skipped_cameras_still_build_placeholders():
    project = Project(name="grid")
    project.output.canvas_w = 320
    project.output.canvas_h = 180
    project.output.fps = 30
    scene = Scene("scene_grid", "Grid")
    scene.add_layer(Layer("camera_a", "camera", "Camera A",
                          source="/dev/anycam-missing-a",
                          box=(0.0, 0.0, 0.5, 1.0), z=1))
    scene.add_layer(Layer("camera_b", "camera", "Camera B",
                          source="/dev/anycam-missing-b",
                          box=(0.5, 0.0, 0.5, 1.0), z=2))
    project.scenes = [scene]
    project.active_scene = scene.id
    engine = AnyCamEngine(project, preview=True,
                          skip_layers={"camera_a", "camera_b"}).build()
    try:
        assert set(engine._layer_pads) == {"camera_a", "camera_b"}
        assert engine.report.camera_placeholders == ["Camera A", "Camera B"]
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe missing")
def test_engine_reports_dropped_frame_ratio(tmp_path):
    engine = AnyCamEngine(make_project(), record_path=tmp_path / "x.mp4").build()
    report = engine.run(1.5)
    assert report["dropped_pct"] < 10.0
    assert report["fps"] > 15.0


def test_missing_media_layers_render_cards_without_stopping_scene(tmp_path):
    """B13: one moved image/video must not refuse the rest of the scene."""
    project = make_project(320, 180, 30)
    project.scene.add_layer(Layer(
        "missing_image", "image", "Gone image",
        source=str(tmp_path / "gone.png"),
        box=(0.0, 0.0, 0.5, 0.5), z=20,
    ))
    project.scene.add_layer(Layer(
        "missing_video", "video", "Gone clip",
        source=str(tmp_path / "gone.mp4"),
        box=(0.5, 0.0, 0.5, 0.5), z=21,
    ))
    frames: list[int] = []
    engine = AnyCamEngine(
        project, preview=True, on_frame=lambda _sample: frames.append(1)
    ).build()
    try:
        engine.start()
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            engine.pump(50)
        assert len(engine.report.media_errors) == 2, engine.report.media_errors
        assert engine.report.error == "", engine.report.error
        assert frames, "missing media left the preview without a frame"
        assert {"missing_image", "missing_video"}.issubset(engine._layer_pads)
    finally:
        engine.stop()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
def test_video_layer_loops_without_a_black_frame(tmp_path):
    """B14: a clip ending used to freeze or blank the layer."""
    clip = tmp_path / "loop.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "color=c=red:s=320x180:d=1:r=25", "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-preset", "ultrafast", str(clip)],
        check=True, timeout=30,
    )
    project = Project(name="loop")
    project.output.canvas_w = 320
    project.output.canvas_h = 180
    project.output.fps = 25
    scene = Scene("scene_loop", "Loop")
    scene.add_layer(Layer("clip", "video", "Clip", source=str(clip),
                          box=(0.0, 0.0, 1.0, 1.0), z=1))
    project.scenes = [scene]
    project.active_scene = scene.id

    latest = {}

    def on_frame(sample) -> None:
        buffer = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        width = int(caps.get_value("width"))
        height = int(caps.get_value("height"))
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return
        try:
            latest["pixels"] = (width, height, bytes(info.data)[:width * height * 4])
        finally:
            buffer.unmap(info)

    frames: list[int] = []
    engine = AnyCamEngine(
        project, preview=True, on_frame=on_frame
    ).build()
    try:
        engine.start()
        deadline = time.monotonic() + 2.6
        while time.monotonic() < deadline:
            engine.pump(50)
            frames.append(1)
        video_sources = [entry for entry in engine.pool.entries()
                         if entry.spec.kind == "video"]
        assert video_sources and video_sources[0].loops >= 1, [
            entry.as_dict() for entry in engine.pool.entries()
        ]
        assert len(frames) > 40
        width, height, pixels = latest["pixels"]
        offset = ((height // 2) * width + width // 2) * 4
        red, green, blue = pixels[offset:offset + 3]
        assert red > 150 and green < 80 and blue < 80, (red, green, blue)
        assert engine.report.error == "", engine.report.error
    finally:
        engine.stop()
