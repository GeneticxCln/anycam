"""Regression tests for the 2026-10-31 audit, milestone M0.

Each test is named after the bug it pins down.  The pixel tests drive a real
GStreamer pipeline (synthetic sources only) so they fail if the picture changes
even when every model field still looks right.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("gi")

from anycam import config, overlay, sources  # noqa: E402
from anycam.geometry import fit_contain  # noqa: E402
from anycam.model import (  # noqa: E402
    AudioConfig, Layer, OutputConfig, Project, Scene, save_scene_preset,
)
from anycam.pipeline import AnyCamEngine  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

HAS_COMPOSITOR = False
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    HAS_COMPOSITOR = Gst.ElementFactory.find("compositor") is not None
except Exception:  # pragma: no cover - platform without GStreamer
    Gst = None

pytestmark = pytest.mark.skipif(not HAS_COMPOSITOR, reason="GStreamer compositor unavailable")


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Keep rendered overlays out of the developer's real cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def make_project(width: int = 640, height: int = 360, fps: int = 30) -> Project:
    project = Project(name="audit")
    project.output = OutputConfig(canvas_w=width, canvas_h=height, fps=fps)
    scene = Scene("scene_audit", "Audit")
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def _add(project: Project, layer: Layer) -> Layer:
    project.scene.add_layer(layer)
    return layer


# --------------------------------------------------------------------- B22
def test_two_camera_boxes_show_their_own_half(tmp_path):
    """The two-camera preset must read red | blue, not squeezed halves."""
    from helpers.frames import grab_frame

    rect = tmp_path / "left.png"
    from PIL import Image

    Image.new("RGB", (320, 240), (255, 0, 0)).save(rect)
    other = tmp_path / "right.png"
    Image.new("RGB", (320, 240), (0, 0, 255)).save(other)
    project = make_project(640, 360, 30)
    _add(project, Layer("l_left", "image", "Left", source=str(rect),
                        box=(0.0, 0.0, 0.5, 1.0), z=1))
    _add(project, Layer("l_right", "image", "Right", source=str(other),
                        box=(0.5, 0.0, 0.5, 1.0), z=2))
    frame = grab_frame(project)
    left = frame.average(80, 170, 240, 190)
    right = frame.average(400, 170, 560, 190)
    detail = (frame.width, frame.height, frame.pixel(160, 180), frame.pixel(480, 180))
    assert left[0] > 200 and left[2] < 30, (left, detail)
    assert right[2] > 200 and right[0] < 30, (right, detail)
    # 4:3 content in a portrait box is letterboxed, not stretched.
    assert frame.corner_is_black((80, 20, 240, 50)), frame.average(80, 20, 240, 50)


def test_camera_chain_letterboxes_a_1080p_mode(monkeypatch):
    """B22: a 16:9 camera in a narrow box gets bars instead of a squeeze."""
    monkeypatch.setattr(
        sources, "camera_modes",
        lambda device: [sources.CameraMode("MJPG", 1920, 1080, 60)])
    layer = Layer("l_cam", "camera", "Cam", source="/dev/video0",
                  box=(0.0, 0.0, 0.5, 1.0))
    chain = sources.camera_chain("/dev/video0", 960, 1080, 60, layer)
    boxes = [element for element in chain.elements
             if element.get_factory().get_name() == "videobox"]
    assert len(boxes) == 1, "a 16:9 frame in a 0.89 box must be letterboxed"
    assert boxes[0].get_property("top") == -270
    assert boxes[0].get_property("bottom") == -270
    assert boxes[0].get_property("left") == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
def test_video_layer_keeps_its_aspect(tmp_path):
    """B22: a 4:3 clip in a 16:9 box is pillarboxed, not stretched."""
    from helpers.frames import grab_frame

    clip = tmp_path / "red.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "color=c=red:s=320x240:d=2:r=25", "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-preset", "ultrafast", str(clip)],
        check=True, timeout=30,
    )
    project = make_project(640, 360, 25)
    _add(project, Layer("l_video", "video", "Clip", source=str(clip),
                        box=(0.0, 0.0, 1.0, 1.0), z=1))
    frame = grab_frame(project, timeout=8.0)
    assert frame.average(160, 160, 480, 200)[0] > 150
    # Pillarbox bands: a stretched 4:3 frame would be red at x=20.
    assert frame.corner_is_black((5, 160, 60, 200)), frame.average(5, 160, 60, 200)


# --------------------------------------------------------------------- B23
def test_crop_is_a_fraction_of_the_source_image(tmp_path):
    """B23: crop percentages used to be applied to the box, not the photo."""
    from helpers.frames import grab_frame
    from PIL import Image

    plate = tmp_path / "split.png"
    image = Image.new("RGB", (200, 100), (255, 0, 0))
    image.paste((0, 0, 255), (100, 0, 200, 100))
    image.save(plate)
    # A square box separates the two rules: a source-fraction crop keeps the
    # blue half, while cropping the box would keep the red one.
    project = make_project(320, 320, 30)
    _add(project, Layer("l_crop", "image", "Split", source=str(plate),
                        box=(0.0, 0.0, 1.0, 1.0), crop=(0.5, 0.0, 0.0, 0.0), z=1))
    frame = grab_frame(project)
    centre = frame.average(120, 120, 200, 200)
    assert centre[2] > 200 and centre[0] < 30, centre
    assert frame.average(10, 10, 60, 60)[2] > 200


# --------------------------------------------------------------------- B24
def test_chroma_tolerance_is_not_scaled_to_bytes():
    """B24: noise-level is 0..64, so multiplying by 255 broke every value."""
    from anycam.model import Effects

    fx = Effects(chroma_enabled=True, chroma_similarity=0.35,
                 chroma_smoothness=0.25)
    layer = Layer("l", "camera", "Cam", effects=fx)
    elements = sources._effects_chain(layer)
    alpha = elements[0]
    assert alpha.get_property("noise-level") == pytest.approx(0.35)
    assert alpha.get_property("black-sensitivity") == 32
    assert alpha.get_property("white-sensitivity") == 32

    fx.chroma_similarity = 0.9
    assert sources._effects_chain(layer)[0].get_property("noise-level") == pytest.approx(0.9)


# --------------------------------------------------------------------- B29
def test_bad_chroma_colour_cannot_break_the_graph():
    """B29: '#fff' was ignored and a malformed colour raised mid-build."""
    assert sources.parse_chroma_color("#fff") == (255, 255, 255)
    assert sources.parse_chroma_color("#00ff00") == (0, 255, 0)
    assert sources.parse_chroma_color("nonsense") == (0, 255, 0)
    assert sources.parse_chroma_color("#12zz34") == (0, 255, 0)
    layer = Layer("l", "camera", "Cam",
                  effects=_effects(fx_color="#fff"))
    assert sources._effects_chain(layer)[0].get_property("target-r") == 255


def _effects(fx_color: str = "#00ff00"):
    from anycam.model import Effects

    return Effects(chroma_enabled=True, chroma_key=fx_color)


# --------------------------------------------------------------------- B26
def test_two_photos_with_the_same_name_do_not_collide(tmp_path):
    """B26: the cache key was the file stem plus the box size."""
    from PIL import Image

    first_dir, second_dir = tmp_path / "a", tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "photo.png"
    second = second_dir / "photo.png"
    Image.new("RGB", (120, 120), (255, 0, 0)).save(first)
    Image.new("RGB", (120, 120), (0, 0, 255)).save(second)

    red_png = overlay.prepare_rgba(first, 100, 100)
    blue_png = overlay.prepare_rgba(second, 100, 100)
    assert red_png != blue_png
    assert Image.open(red_png).getpixel((50, 50))[:3] == (255, 0, 0)
    assert Image.open(blue_png).getpixel((50, 50))[:3] == (0, 0, 255)


# --------------------------------------------------------------------- B49
def test_photo_exif_rotation_is_applied(tmp_path):
    """B49: a portrait phone photo used to land sideways."""
    from PIL import Image

    source = tmp_path / "portrait.jpg"
    image = Image.new("RGB", (200, 100), (0, 200, 0))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise
    image.save(source, exif=exif)

    out = overlay.prepare_rgba(source, 100, 100)
    with Image.open(out) as rendered:
        # Rotated content is tall: it fills the height and leaves side bands.
        assert rendered.getpixel((50, 5))[3] > 200
        assert rendered.getpixel((5, 50))[3] == 0


# --------------------------------------------------------------------- B27
def test_camera_mode_never_picks_a_tiny_first_mode():
    """B27: no exact match used to select the first listed mode (320x240)."""
    modes = [sources.CameraMode("MJPG", 320, 240, 30),
             sources.CameraMode("MJPG", 1920, 1080, 30),
             sources.CameraMode("MJPG", 640, 480, 30)]
    assert sources.choose_from_modes(modes, 1280, 720, 30).width == 1920
    # A small request still prefers the smallest size that covers it.
    assert sources.choose_from_modes(modes, 400, 300, 30).width == 640


# --------------------------------------------------------------------- B38
def test_pump_waits_in_nanoseconds():
    """B38: a 100 ms wait returned in ~1 ms, so stop() spun on the UI thread."""
    engine = AnyCamEngine(make_project(320, 180, 30), preview=True).build()
    try:
        started = time.monotonic()
        engine.pump(100)
        elapsed = time.monotonic() - started
        assert 0.06 <= elapsed <= 0.6, elapsed
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


# ---------------------------------------------------------------------- B3
def test_saved_rtmp_url_never_publishes_on_preview():
    """B3: opening preview used to start broadcasting a saved URL."""
    project = make_project(320, 180, 30)
    project.output.rtmp_url = "rtmp://127.0.0.1:1/live/secret-key"
    engine = AnyCamEngine(project, preview=True).build()
    try:
        assert engine.pipeline.get_by_name("rtmp_sink") is None
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


def test_report_redacts_the_stream_key():
    """B5: reports and logs must not carry the key in clear text."""
    project = make_project(320, 180, 30)
    engine = AnyCamEngine(project, preview=True,
                          rtmp="rtmp://example.test/live/super-secret").build()
    try:
        summary = engine.report.as_dict()
        assert "super-secret" not in json.dumps(summary)
        assert summary["rtmp_url"].endswith("/***")
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


# ---------------------------------------------------------------------- B4
def test_microphone_is_absent_unless_enabled(monkeypatch, tmp_path):
    """B4: a media bed used to drag the microphone into the graph."""
    monkeypatch.setattr(config, "microphone", lambda: "fake-mic")
    project = make_project(320, 180, 30)
    project.audio = AudioConfig(mic_enabled=False, mic_device="fake-mic")
    # The audio graph only exists when something consumes it.
    engine = AnyCamEngine(project, preview=True, audio=True,
                          record_path=tmp_path / "audio.mp4").build()
    try:
        assert engine.pipeline.get_by_name("mic") is None
        assert engine.report.audio_inputs == []
    finally:
        engine.pipeline.set_state(Gst.State.NULL)

    project.audio.mic_enabled = True
    engine = AnyCamEngine(project, preview=True, audio=True,
                          record_path=tmp_path / "audio2.mp4").build()
    try:
        assert engine.pipeline.get_by_name("mic") is not None
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


# ---------------------------------------------------------------------- B6
def test_recording_is_fragmented(tmp_path):
    """B6: faststart buffered the whole file in /tmp and left 0 bytes on disk."""
    engine = AnyCamEngine(make_project(320, 180, 30),
                          record_path=tmp_path / "clip.mp4").build()
    try:
        mux = engine.pipeline.get_by_name("record_mux")
        assert mux.get_property("fragment-duration") >= 250
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe missing")
def test_kill_9_leaves_a_playable_recording(tmp_path):
    """B6/B7: a crash used to lose the whole recording."""
    output = tmp_path / "crash.mp4"
    code = (
        "import sys, time\n"
        "sys.path.insert(0, 'src')\n"
        "from anycam.model import Layer, Project, Scene\n"
        "from anycam.pipeline import AnyCamEngine\n"
        "project = Project()\n"
        "project.output.canvas_w, project.output.canvas_h, project.output.fps = 320, 180, 30\n"
        "scene = Scene('s', 'S')\n"
        "scene.add_layer(Layer('l', 'test', 'Pattern', source='ball', box=(0, 0, 1, 1), z=1))\n"
        "project.scenes = [scene]\n"
        "project.active_scene = 's'\n"
        "engine = AnyCamEngine(project, record_path=sys.argv[1]).build()\n"
        "engine.start()\n"
        "time.sleep(30)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    process = subprocess.Popen([sys.executable, "-c", code, str(output)],
                               cwd=ROOT, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if output.exists() and output.stat().st_size > 4096:
                break
            time.sleep(0.1)
        assert output.exists() and output.stat().st_size > 4096, "recording never started"
        time.sleep(1.5)
    finally:
        process.kill()
        process.wait(timeout=10)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(output)],
        capture_output=True, text=True, timeout=20,
    )
    assert probe.returncode == 0, probe.stderr
    assert float(probe.stdout.strip()) > 0.5, probe.stdout


# ---------------------------------------------------------------------- B8
def test_project_save_is_atomic_private_and_keeps_a_backup(tmp_path):
    path = tmp_path / "project.json"
    project = Project(name="first", output=OutputConfig(rtmp_url="rtmp://x/live/key"))
    project.save(path)
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not (tmp_path / "project.json.tmp").exists()

    project.name = "second"
    project.save(path)
    backup = tmp_path / "project.json.bak"
    assert backup.exists()
    assert json.loads(backup.read_text())["name"] == "first"
    assert json.loads(path.read_text())["name"] == "second"


def test_corrupt_project_is_preserved_on_load(tmp_path, monkeypatch):
    """B8: defaults used to overwrite the only copy of a broken project."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    path = config.project_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"scenes": [ this is not json')

    from PySide6.QtWidgets import QApplication

    from anycam.gui import AnyCamWindow

    app = QApplication.instance() or QApplication([])
    window = AnyCamWindow()
    try:
        assert window._load_warning
        preserved = path.with_name("project.json.corrupt")
        assert preserved.exists()
        assert "not json" in preserved.read_text()
    finally:
        window._persist = False
        window.close()
        app.processEvents()


# --------------------------------------------------------------------- B39
def test_record_dir_setting_is_used(tmp_path):
    """B39: record_dir was serialised but every recording ignored it."""
    target = tmp_path / "my-recordings"
    from anycam import config as config_module

    assert config_module.recordings_dir(str(target)) == target
    assert target.is_dir()


def test_xdg_video_dir_comes_from_user_dirs(tmp_path, monkeypatch):
    """B39: XDG_VIDEOS_DIR is not a normal environment variable to read."""
    monkeypatch.delenv("XDG_VIDEOS_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config_file = tmp_path / "config" / "user-dirs.dirs"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(f'XDG_VIDEOS_DIR="{tmp_path / "clips"}"\n')
    assert config.recordings_dir() == tmp_path / "clips" / "AnyCam"


# --------------------------------------------------------------------- B50
def test_scene_preset_filename_cannot_escape(tmp_path):
    scene = Scene("s", "../../etc/passwd")
    path = save_scene_preset(scene, tmp_path)
    assert path.parent == tmp_path
    assert "/" not in path.name and ".." not in path.name


# --------------------------------------------------------------------- B51
def test_test_pattern_crop_is_applied():
    """B51: test layers ignored crop entirely."""
    layer = Layer("l", "test", "Pattern", box=(0.0, 0.0, 1.0, 1.0),
                  crop=(0.5, 0.0, 0.0, 0.0))
    chain = sources.test_chain(640, 360, 30, layer)
    crops = [element for element in chain.elements
             if element.get_factory().get_name() == "videocrop"]
    assert len(crops) == 1
    assert crops[0].get_property("left") == 320


# ---------------------------------------------------------------------- B1
@pytest.mark.skipif(Gst.ElementFactory.find("pipewiresrc") is None,
                    reason="pipewiresrc missing")
def test_screen_chain_connects_with_the_node_id():
    """B1: target-object is a name/serial, so the numeric id picked a webcam."""
    chain = sources.screen_chain(123, 640, 360, 30, expected_size=(2560, 1440))
    source = chain.check_element
    assert source.get_property("path") == "123"
    assert chain.expected_size == (2560, 1440)


# --------------------------------------------------------------------- B28
def test_engine_rejects_an_odd_canvas():
    """B28: odd sizes were accepted and then killed x264."""
    project = make_project(641, 361, 30)
    with pytest.raises(ValueError, match="even"):
        AnyCamEngine(project, preview=True).build()


# --------------------------------------------------------------------- B22 helper
def test_fit_contain_is_the_shared_geometry():
    """The compositor and the tests must agree on the fit rule."""
    rect = fit_contain(1920, 1080, 960, 1080)
    assert (rect.x, rect.y, rect.w, rect.h) == (0, 270, 960, 540)
