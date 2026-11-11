"""Regression tests for the low-severity audit fixes (phase 4)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from anycam import config, overlay, pipeline, portal, sources  # noqa: E402
from anycam.engine.outputs.recorder import Recorder  # noqa: E402
from anycam.gui import PreviewWidget  # noqa: E402
from anycam.gui_many import ManyCamWindow  # noqa: E402
from anycam.model import Layer, Project, Scene, new_id  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("CACHE", "CONFIG", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))


def make_project(*layers: Layer, width: int = 320, height: int = 180) -> Project:
    project = Project(name="cleanups")
    project.output.canvas_w, project.output.canvas_h, project.output.fps = width, height, 30
    scene = Scene(new_id("scene"), "Scene")
    for layer in layers:
        scene.add_layer(layer)
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def pattern_layer(**kwargs) -> Layer:
    return Layer(new_id("layer"), "test", "Pattern", source="ball",
                 box=kwargs.pop("box", (0.0, 0.0, 1.0, 1.0)), **kwargs)


# ------------------------------------------------- item 18: portal restore token
def test_select_sources_asks_the_portal_to_persist_the_choice():
    options = portal.select_sources_options("tok", 3)
    assert options["persist_mode"].unpack() == 2
    assert "restore_token" not in options
    restored = portal.select_sources_options("tok", 3, "saved-token")
    assert restored["restore_token"].unpack() == "saved-token"


def test_restore_token_is_stored_privately_and_never_blanked():
    assert config.load_screen_restore_token() == ""
    config.save_screen_restore_token("abc123")
    assert config.load_screen_restore_token() == "abc123"
    mode = stat.S_IMODE(config.screen_token_path().stat().st_mode)
    assert mode == 0o600
    config.save_screen_restore_token("")  # a portal without tokens must not wipe it
    assert config.load_screen_restore_token() == "abc123"


class TokenSession:
    def __init__(self, node: int, token: str):
        self.node, self.restore_token = node, token
        self.fd = 20 + node
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


def test_saved_screen_layers_restore_with_the_token_but_new_shares_do_not(app, monkeypatch):
    config.save_screen_restore_token("tok1")
    calls: list[dict] = []
    sessions = iter([TokenSession(42, "tok2"), TokenSession(43, "tok3")])

    def fake_request(**kwargs):
        calls.append(kwargs)
        return next(sessions)

    monkeypatch.setattr(portal, "request_screen_cast", fake_request)
    saved = Layer(new_id("layer"), "screen", "Screen", source="9", z=1)
    window = ManyCamWindow(make_project(saved))
    window.headless = True
    window._persist = False
    try:
        assert window._ensure_screen_sessions() is True
        assert calls == [{"restore_token": "tok1"}]
        assert config.load_screen_restore_token() == "tok2"
        # The user is choosing now: the picker must open, with no token.
        other = window.add_layer("screen-new")
        assert other is not None and calls[-1] == {}
    finally:
        window.engine = None
        window._persist = False
        window.close()
        app.processEvents()


# ------------------------------------------------- item 19: resolution changes
def h264_sample(width: int, height: int, pts_ms: int) -> Gst.Sample:
    buffer = Gst.Buffer.new_allocate(None, 32, None)
    buffer.pts = pts_ms * Gst.MSECOND
    caps = Gst.Caps.from_string(
        f"video/x-h264,width={width},height={height},stream-format=byte-stream")
    return Gst.Sample.new(buffer, caps, None, None)


class FakeAppsrc:
    def __init__(self):
        self.pushed = 0

    def emit(self, _signal, _sample):
        self.pushed += 1
        return Gst.FlowReturn.OK


def test_recorder_drops_frames_of_another_size_instead_of_corrupting_the_file():
    recorder = Recorder()
    recorder._pipeline = object()
    recorder._video_src = FakeAppsrc()
    recorder.push_video(h264_sample(640, 360, 0))
    recorder.push_video(h264_sample(640, 360, 33))
    recorder.push_video(h264_sample(1280, 720, 66))
    assert recorder.size == (640, 360)
    assert recorder._video_src.pushed == 2
    assert recorder.rejected_frames == 1
    assert recorder.error == ""


class FakeRecorder:
    def __init__(self):
        self.path = ""
        self.stopped = False
        self.error = ""

    def start(self, path, *_args, **_kwargs):
        self.path = str(path)
        return True

    def stop(self):
        self.stopped = True
        return {"path": self.path, "ok": True, "error": ""}


def test_changing_the_canvas_size_while_recording_starts_a_new_file(app, monkeypatch):
    monkeypatch.setattr("anycam.gui.Recorder", FakeRecorder)
    window = ManyCamWindow(make_project(pattern_layer(), width=640, height=360))
    window.headless = True
    window._persist = False
    try:
        window.recording = True
        assert window._start_recorder()
        first = window._recorder
        first_path = Path(first.path)
        window.set_output_format(640, 360, 60)       # rate only: same file
        assert window._recorder is first and not first.stopped
        window.set_output_format(1280, 720, 60)      # new size: new file
        assert first.stopped and window._recorder is None
        assert window._segment_index == 1
        assert window._record_path().name == first_path.stem + "-2.mp4"
        assert str(first_path) in window.recording_segments
    finally:
        window.engine = None
        window._persist = False
        window.close()
        app.processEvents()


# ------------------------------------------------------- item 20: keyframe
@pytest.mark.skipif(Gst.ElementFactory.find("x264enc") is None,
                    reason="x264enc missing")
def test_reattaching_the_recorder_starts_on_a_keyframe(monkeypatch):
    monkeypatch.setenv("ANYCAM_ENCODER", "cpu")
    samples: list[Gst.Sample] = []
    recorder = SimpleNamespace(
        path="/tmp/unused.mp4", push_video=samples.append,
        push_audio=lambda _sample: None)
    engine = AnyCamEngine(make_project(pattern_layer()), recorder=recorder,
                          preview=True).build()
    engine.start()
    try:
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            engine.pump(50)
        assert samples, "the tap never delivered an encoded frame"
        engine.detach_recorder()
        end = time.monotonic() + 0.7   # the encoder is now mid-GOP, idle
        while time.monotonic() < end:
            engine.pump(50)
        samples.clear()
        assert engine.attach_recorder(recorder)
        end = time.monotonic() + 1.5
        while time.monotonic() < end and not samples:
            engine.pump(50)
        assert samples, "no frame after re-attaching"
        flags = samples[0].get_buffer().get_flags()
        assert not flags & Gst.BufferFlags.DELTA_UNIT, "first frame is a delta frame"
    finally:
        engine.stop()


# --------------------------------------------------------- item 22: microphone
def test_microphone_is_not_looked_up_when_it_is_disabled(monkeypatch):
    def boom():
        raise AssertionError("pactl was queried although the mic is off")

    monkeypatch.setattr(config, "microphone", boom)
    project = make_project(pattern_layer())
    project.audio.mic_enabled = False
    engine = AnyCamEngine(project, audio=True, preview=True).build()
    try:
        assert engine.report.audio_inputs == ["silence"]
    finally:
        engine.pool.stop()


# ------------------------------------------------------------ item 25: robustness
def test_frame_rate_parsing_survives_streams_without_one():
    assert pipeline._parse_frame_rate("60/1") == 60.0
    assert pipeline._parse_frame_rate("0/0") == 0.0
    assert pipeline._parse_frame_rate("30000/1001") == pytest.approx(29.97, abs=0.01)
    assert pipeline._parse_frame_rate(None) == 0.0
    assert pipeline._parse_frame_rate("junk") == 0.0


def test_verify_mp4_reports_a_stream_with_unknown_rate_instead_of_crashing(monkeypatch):
    monkeypatch.setattr("anycam.engine.verify.ffprobe", lambda path: {
        "format": {"duration": "N/A"},
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080,
                     "r_frame_rate": "0/0", "nb_frames": "N/A"}]})
    monkeypatch.setattr("anycam.engine.verify._count_packets", lambda path: 0)
    ok, info = pipeline.verify_mp4("x.mp4")
    assert ok is False and info["rate"] == 0.0


def test_a_failed_start_inside_run_leaves_nothing_running():
    engine = AnyCamEngine(make_project(pattern_layer()), preview=True).build()

    def fail():
        raise RuntimeError("start failed")

    engine.start = fail
    with pytest.raises(RuntimeError, match="start failed"):
        engine.run(0.5)
    assert engine.pipeline is None
    assert engine.pool.entries() == []


# ------------------------------------------------------- item 26: overlay cache
def test_overlay_cache_drops_the_oldest_files_beyond_the_limit(tmp_path):
    directory = tmp_path / "overlays"
    directory.mkdir()
    paths = []
    for index in range(5):
        path = directory / f"f{index}.png"
        path.write_bytes(b"x" * 1000)
        os.utime(path, (1000 + index, 1000 + index))
        paths.append(path)
    removed = overlay.prune_overlay_cache(directory, max_bytes=2500, max_files=100)
    assert removed == 3
    assert [path.exists() for path in paths] == [False, False, False, True, True]


def test_overlay_cache_never_removes_the_file_just_written(tmp_path):
    directory = tmp_path / "overlays"
    directory.mkdir()
    oldest = directory / "keep.png"
    oldest.write_bytes(b"x" * 1000)
    os.utime(oldest, (1, 1))
    newer = directory / "new.png"
    newer.write_bytes(b"x" * 1000)
    overlay.prune_overlay_cache(directory, max_bytes=0, max_files=0, keep=oldest)
    assert oldest.exists() and not newer.exists()


def test_rendering_prunes_the_cache_but_not_on_every_call(tmp_path, monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(overlay, "prune_overlay_cache",
                        lambda *args, **kwargs: calls.append(kwargs.get("keep")) or 0)
    monkeypatch.setattr(overlay, "_last_prune", 0.0)
    source = tmp_path / "pic.png"
    Image.new("RGB", (40, 20), (200, 30, 30)).save(source)
    overlay.prepare_rgba(source, 64, 36)
    overlay.prepare_rgba(source, 64, 36, crop=(0.1, 0.0, 0.0, 0.0))
    assert len(calls) == 1


# -------------------------------------------------------------- item 27: preview
def preview_with(*layers: Layer) -> PreviewWidget:
    widget = PreviewWidget()
    widget.resize(960, 540)
    widget.image = QImage(960, 540, QImage.Format.Format_RGBA8888)
    scene = Scene("s", "S")
    for layer in layers:
        scene.add_layer(layer)
    widget.set_scene(scene)
    widget.set_snap_enabled(False)
    return widget


def test_a_group_stops_together_at_the_canvas_edge(app):
    first = Layer("a", "test", "A", box=(0.0, 0.1, 0.2, 0.2), z=1)
    second = Layer("b", "test", "B", box=(0.5, 0.1, 0.2, 0.2), z=2)
    widget = preview_with(first, second)
    widget.set_selection(["a", "b"], "b")
    widget._begin_drag(second, QPointF(100, 100), "move", "")
    widget._apply_drag(QPointF(100 + 0.4 * 960, 100))   # asks for dx = 0.4
    # B can only travel 0.3 before it touches the edge; A must travel the same.
    assert second.box[0] == pytest.approx(0.8)
    assert first.box[0] == pytest.approx(0.3)


def test_spacing_guides_report_canvas_pixels_not_preview_pixels(app):
    other = Layer("o", "test", "O", box=(0.0, 0.4, 0.2, 0.2), z=1)
    widget = preview_with(other)            # the preview image is 960 wide
    widget.canvas_size = (1920, 1080)
    guides = widget._spacing_guides(0.5, 0.4, 0.2, 0.2, set())
    horizontal = [guide for guide in guides if guide[0] == "v"]
    assert horizontal and horizontal[0][4] == 576      # 0.3 of 1920, not 288


# -------------------------------------------------------------- item 28: packaging
def test_gui_without_pyside6_explains_what_to_install():
    code = ("import sys; sys.modules['PySide6'] = None; "
            "from anycam.__main__ import main; sys.exit(main(['gui']))")
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=60)
    assert result.returncode == 2
    assert "PySide6" in result.stderr and "Traceback" not in result.stderr


def test_pyproject_declares_a_gui_extra():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'gui = ["PySide6' in text


# ------------------------------------------------------ item 29: raw image chains
@pytest.fixture
def wide_png(tmp_path):
    path = tmp_path / "wide.png"
    Image.new("RGB", (200, 100), (10, 200, 10)).save(path)
    return path


def test_a_raw_image_is_fitted_by_its_own_aspect_not_stretched(wide_png):
    chain = sources.image_chain(str(wide_png), 400, 400, 30)
    caps = chain.tail.get_property("caps").get_structure(0)
    assert (caps.get_value("width"), caps.get_value("height")) == (400, 200)
    assert chain.source_size == (200, 100)


def test_a_raw_image_crop_uses_the_images_own_dimensions(wide_png):
    layer = Layer("img", "image", "Img", source=str(wide_png),
                  crop=(0.5, 0.0, 0.0, 0.0))
    chain = sources.image_chain(str(wide_png), 400, 400, 30, layer)
    crops = [element for element in chain.elements
             if element.get_factory().get_name() == "videocrop"]
    assert len(crops) == 1
    assert crops[0].get_property("left") == 100        # half of 200, not of 400


# ------------------------------------------------------------- small config fixes
def test_unreadable_exclusive_caps_is_unknown_not_false():
    assert config.vcam_exclusive_caps("") is None
    assert config.vcam_exclusive_caps(None) is None


def test_default_camera_scans_the_devices_once(monkeypatch):
    scans: list[int] = []
    devices = [config.CameraInfo(f"/dev/video{n}", f"Cam {n}", ["MJPG"])
               for n in range(3)]
    monkeypatch.setattr(config, "list_video_devices",
                        lambda: scans.append(1) or devices)
    monkeypatch.delenv("ANYCAM_CAMERA", raising=False)
    monkeypatch.setenv("ANYCAM_VCAM", "/dev/video2")
    assert config.camera_device() == "/dev/video0"
    assert len(scans) == 1
