"""Phase 7: PDF slides, video-site links, MJPEG cameras, motion, region blur, overlay panel."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

import gi
import numpy as np
import pytest
from PIL import Image

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam import content  # noqa: E402
from anycam.model import Layer, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("CACHE", "CONFIG", "STATE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))


# ------------------------------------------------------------------------- PDF
@pytest.fixture
def pdf(tmp_path):
    if not content.pdf_available():
        pytest.skip("pdftoppm unavailable")
    pages = []
    for index, colour in enumerate(((200, 30, 30), (30, 200, 30), (30, 30, 200))):
        page = tmp_path / f"p{index}.png"
        Image.new("RGB", (400, 300), colour).save(page)
        pages.append(page)
    path = tmp_path / "slides.pdf"
    Image.open(pages[0]).save(path, "PDF", save_all=True,
                              append_images=[Image.open(p) for p in pages[1:]])
    return path


def test_a_pdf_becomes_one_picture_per_page_and_is_cached(pdf):
    pages, error = content.pdf_pages(str(pdf), dpi=72)
    assert error == "" and len(pages) == 3
    reds = [np.asarray(Image.open(page).convert("RGB"))[10, 10] for page in pages]
    assert reds[0][0] > 150 and reds[1][1] > 150 and reds[2][2] > 150      # page order kept
    stamp = pages[0].stat().st_mtime_ns
    again, _ = content.pdf_pages(str(pdf), dpi=72)
    assert again == pages and again[0].stat().st_mtime_ns == stamp          # reused, not redone


def test_pdf_problems_are_explained(tmp_path, monkeypatch):
    assert "not found" in content.pdf_pages(str(tmp_path / "no.pdf"))[1]
    broken = tmp_path / "broken.pdf"
    broken.write_text("this is not a pdf")
    if content.pdf_available():
        assert "could not read" in content.pdf_pages(str(broken))[1]
    monkeypatch.setattr(content.shutil, "which", lambda name: None)
    assert "poppler" in content.pdf_pages(str(broken))[1]


def test_a_timed_out_pdf_render_is_never_cached(tmp_path, monkeypatch):
    """A partial render used to be served from the cache forever (item 11)."""
    from pathlib import Path

    from anycam import config

    slides = tmp_path / "slides.pdf"
    slides.write_bytes(b"%PDF-1.4\n% the fake renderer never reads it\n")
    attempts = []

    def timed_out(command, **_kwargs):
        attempts.append(command)
        folder = Path(command[-1]).parent
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "page-1.png").write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, 180)

    monkeypatch.setattr(content, "pdf_available", lambda: True)
    monkeypatch.setattr(content.subprocess, "run", timed_out)
    pages, error = content.pdf_pages(str(slides), dpi=72)
    assert pages == [] and "timed out" in error
    cache = config.cache_dir() / "pdf"
    assert list(cache.rglob("page-*.png")) == []       # partial pages removed
    assert not list(cache.rglob(".complete"))          # nothing says "cached"

    def rendered(command, **_kwargs):
        attempts.append(command)
        folder = Path(command[-1]).parent
        (folder / "page-1.png").write_bytes(b"done")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(content.subprocess, "run", rendered)
    pages, error = content.pdf_pages(str(slides), dpi=72)
    assert error == "" and len(pages) == 1
    assert len(attempts) == 2                          # the failure left nothing to reuse


# ----------------------------------------------------------------------- yt-dlp
def fake_ytdlp(tmp_path, monkeypatch, body: str) -> None:
    script = tmp_path / "bin" / "yt-dlp"
    script.parent.mkdir(exist_ok=True)
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")


def test_video_site_links_are_recognised():
    assert content.needs_resolving("https://www.youtube.com/watch?v=abc")
    assert content.needs_resolving("https://youtu.be/abc")
    assert content.needs_resolving("https://clips.twitch.tv/x")
    assert not content.needs_resolving("rtsp://camera.local/stream")
    assert not content.needs_resolving("http://192.168.1.5:8080/video")
    assert not content.needs_resolving("https://evilyoutube.com.example.org/x")


def test_a_link_is_resolved_to_a_stream_through_yt_dlp(tmp_path, monkeypatch):
    fake_ytdlp(tmp_path, monkeypatch, 'echo "https://cdn.example/stream.mp4"\n')
    assert content.resolve_stream("https://youtu.be/abc") == ("https://cdn.example/stream.mp4", "")


def test_resolving_failures_say_why(tmp_path, monkeypatch):
    fake_ytdlp(tmp_path, monkeypatch, 'echo "ERROR: Video unavailable" >&2\nexit 1\n')
    stream, error = content.resolve_stream("https://youtu.be/abc")
    assert stream == "" and "unavailable" in error
    fake_ytdlp(tmp_path, monkeypatch, 'echo "file:///etc/passwd"\n')
    assert content.resolve_stream("https://youtu.be/abc")[0] == ""        # only http(s) streams
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert "needs yt-dlp" in content.resolve_stream("https://youtu.be/abc")[1]


# ----------------------------------------------------------------- MJPEG camera
class MjpegHandler(BaseHTTPRequestHandler):
    colour = (230, 20, 20)

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        buffer = BytesIO()
        Image.new("RGB", (320, 240), self.colour).save(buffer, "JPEG", quality=85)
        jpeg = buffer.getvalue()
        try:
            for _ in range(600):
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 + f"Content-Length: {len(jpeg)}\r\n\r\n".encode() + jpeg + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.mark.skipif(Gst.ElementFactory.find("souphttpsrc") is None
                    or Gst.ElementFactory.find("multipartdemux") is None
                    or Gst.ElementFactory.find("jpegdec") is None,
                    reason="HTTP/MJPEG GStreamer elements unavailable")
def test_an_ip_camera_serving_mjpeg_shows_up_on_the_canvas():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MjpegHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    project = Project(name="mjpeg")
    project.output.canvas_w, project.output.canvas_h, project.output.fps = 320, 180, 15
    scene = Scene("scene_m", "M")
    scene.add_layer(Layer("cam", "network", "IP camera",
                          source=f"http://127.0.0.1:{server.server_address[1]}/video",
                          box=(0, 0, 1, 1), z=1))
    project.scenes, project.active_scene = [scene], scene.id
    frames = {}

    def on_frame(sample) -> None:
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
        if ok:
            frames["last"] = np.frombuffer(info.data, np.uint8)[: width * height * 4].reshape(
                height, width, 4).copy()
            sample.get_buffer().unmap(info)

    engine = AnyCamEngine(project, preview=True, on_frame=on_frame).build()
    engine.start()
    try:
        end = time.monotonic() + 12
        red = False
        while time.monotonic() < end and not red:
            engine.pump(50)
            last = frames.get("last")
            if last is not None:
                centre = last[90, 160]
                red = centre[0] > 180 and centre[1] < 80 and centre[2] < 80
        assert red, (frames.get("last") is not None and frames["last"][90, 160],
                     engine.report.source_errors, engine.report.error)
    finally:
        engine.stop()
        server.shutdown()


# ----------------------------------------------------------------------- motion
from anycam import motion  # noqa: E402


def frame(value: int, box=None, size=(180, 320)) -> np.ndarray:
    picture = np.full((*size, 4), value, dtype=np.uint8)
    if box:
        x0, y0, x1, y1 = box
        picture[y0:y1, x0:x1, :3] = 255
    return picture


def test_a_still_picture_has_no_motion_and_a_changed_patch_is_found():
    a = motion.to_grey(frame(40))
    share, region = motion.detect(a, motion.to_grey(frame(40)))
    assert share == 0.0 and region is None
    moved = motion.to_grey(frame(40, box=(160, 90, 240, 135)))
    share, region = motion.detect(a, moved)
    assert 0.05 < share < 0.2
    x0, y0, x1, y1 = region
    assert 0.45 < x0 < 0.55 and 0.45 < y0 < 0.55 and 0.7 < x1 < 0.8 and 0.7 < y1 < 0.8
    assert motion.detect(a, a[:10])[0] == 0.0                     # sizes differ: ignored


def test_the_monitor_waits_for_motion_to_last_then_stays_quiet():
    monitor = motion.MotionMonitor(sensitivity=0.8, hold_s=1.0, cooldown_s=10.0)
    fired = []
    t = 0.0
    monitor.update(frame(40), t)
    for step in range(1, 40):                                     # 4 s of a flickering patch
        t += 0.1
        patch = frame(40, box=(100, 50, 200, 120)) if step % 2 else frame(40)
        if monitor.update(patch, t):
            fired.append(round(t, 1))
    assert len(fired) == 1 and 1.0 <= fired[0] <= 1.3, fired      # once, after the hold
    assert monitor.level > 0


def test_brief_motion_does_not_count_and_sensitivity_matters():
    monitor = motion.MotionMonitor(sensitivity=0.8, hold_s=1.0, cooldown_s=0.0)
    monitor.update(frame(40), 0.0)
    assert not monitor.update(frame(40, box=(0, 0, 100, 60)), 0.1)
    assert not monitor.update(frame(40), 0.2)                     # it stopped: the clock resets
    low = motion.MotionMonitor(sensitivity=0.0)
    high = motion.MotionMonitor(sensitivity=1.0)
    assert low.min_share > high.min_share and low.threshold > high.threshold


def test_motion_settings_round_trip_and_are_clamped():
    from anycam.model import MotionConfig

    config_ = MotionConfig.from_dict({"enabled": True, "sensitivity": 9, "hold_s": -3,
                                      "cooldown_s": 1e9, "scene_id": "s2"})
    assert config_.sensitivity == 1.0 and config_.hold_s == 0.0 and config_.cooldown_s == 3600.0
    project = Project(name="m")
    project.motion = config_
    again = Project.from_dict(project.to_dict())
    assert again.motion.enabled and again.motion.scene_id == "s2"
    assert Project.from_dict({"scenes": []}).motion.enabled is False


# --------------------------------------------------------------------- the window
@pytest.fixture
def window(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from anycam.gui_many import ManyCamWindow

    QApplication.instance() or QApplication([])
    project = Project(name="p7")
    first, second = Scene("scene_a", "Alpha"), Scene("scene_b", "Beta")
    first.add_layer(Layer("l1", "test", "Ball", source="ball"))
    second.add_layer(Layer("l2", "test", "Bars", source="smpte"))
    project.scenes, project.active_scene = [first, second], first.id
    win = ManyCamWindow(project)
    win.headless = True
    win._persist = False
    yield win
    win.engine = None
    win.close()


def test_motion_switches_the_scene_and_notifies(window, monkeypatch):
    window.motion_check.setChecked(True)
    window.motion_scene.setCurrentIndex(window.motion_scene.findData("scene_b"))
    window.motion_hold.setValue(0.0)
    window.motion_cooldown.setValue(0.0)
    window.motion_sensitivity.setValue(90)
    sent = []
    monkeypatch.setattr(type(window), "desktop_notify", staticmethod(lambda t, b: sent.append((t, b))))
    assert window.feed_motion(frame(40), 0.0) is False
    assert window.feed_motion(frame(40, box=(0, 0, 200, 120)), 0.1) is True
    assert window.project.active_scene == "scene_b"
    assert sent and "Beta" in sent[0][1]
    assert "Movement now" in window.motion_level_label.text()
    window.motion_check.setChecked(False)
    assert window.feed_motion(frame(40), 1.0) is False            # switched off


def test_notifications_need_notify_send(monkeypatch):
    from anycam.ui import general_panel

    monkeypatch.setattr(general_panel.shutil, "which", lambda name: None)
    assert general_panel.GeneralPanelMixin.desktop_notify("t", "b") is False


def test_a_loaded_project_refreshes_the_newer_settings_widgets(window):
    project = Project(name="loaded")
    project.output.rtmp_url = "rtmp://live.twitch.tv/app/key123"
    project.output.rtmp_extra_urls = ["rtmp://x.example/live/k"]
    project.output.record_bitrate_kbps = 7000
    project.audio.mic_dynamics, project.audio.media_playlist = "gate", ["/a.wav"]
    project.motion.enabled = True
    project.scenes = [Scene("only", "Only")]
    project.active_scene = "only"
    window.project = project
    window._load_extended_widgets()
    assert window.stream_preset.currentData() == "twitch" and window.stream_key_edit.text() == "key123"
    assert window.extra_list.count() == 1 and window.record_bitrate.value() == 7000
    assert window.dynamics_combo.currentData() == "gate" and window.music_list.count() == 1
    assert window.motion_check.isChecked() and window._motion_monitor is not None


@pytest.mark.skipif(not content.pdf_available(), reason="pdftoppm unavailable")
def test_a_pdf_adds_paused_slides_to_the_playlist(window, pdf):
    assert window.add_pdf_slides(str(pdf)) == 3
    assert len(window.playlist_ids) == 3 and window.playlist_pause.isChecked()
    assert [layer.kind for layer in window.scene.layers[-3:]] == ["image"] * 3
    visible = [layer.visible for layer in window.scene.layers[-3:]]
    assert visible.count(True) <= 1                               # one slide shows at a time
    assert window.add_pdf_slides(str(pdf) + ".missing") == 0


def test_the_floating_panel_mirrors_and_drives_the_window(window, monkeypatch):
    from PySide6.QtGui import QImage

    window.set_overlay_panel(True)
    panel = window._overlay_panel
    assert panel.isVisible() and panel.scene_label.text() == "Alpha"
    calls = []
    monkeypatch.setattr(type(window), "toggle_record", lambda self: calls.append("record"))
    monkeypatch.setattr(type(window), "toggle_mic_mute", lambda self: calls.append("mute"))
    panel.record_button.click()
    panel.mute_button.click()
    panel.next_button.click()
    assert calls == ["record", "mute"] and window.project.active_scene == "scene_b"
    panel.refresh()
    assert panel.scene_label.text() == "Beta"
    image = QImage(320, 180, QImage.Format.Format_RGBA8888)
    image.fill(0xFFFF0000)
    panel.set_frame(image)
    assert not panel.picture.pixmap().isNull()
    corner = panel.picture.pixmap().toImage().pixelColor(0, 0)
    assert corner.alpha() == 0                                   # the corners of the circle are clear
    window.set_overlay_panel(False)
    assert not panel.isVisible()
    window.set_overlay_panel(True)
    assert window.panel_check.isChecked()
    window.set_overlay_panel(False)
