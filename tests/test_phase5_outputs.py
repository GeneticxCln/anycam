"""Phase 5: several stream destinations with status, reconnect and backoff; presets."""
from __future__ import annotations

import shutil
import socket
import subprocess
import time

import gi
import pytest

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam import streaming  # noqa: E402
from anycam.model import Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)

needs_stack = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or Gst.ElementFactory.find("rtmp2sink") is None
    or Gst.ElementFactory.find("flvmux") is None or Gst.ElementFactory.find("x264enc") is None,
    reason="ffmpeg RTMP listener, rtmp2sink, flvmux or x264enc unavailable")


# ------------------------------------------------------------------ presets
def test_presets_build_and_split_urls():
    url = streaming.preset_url("twitch", "live_123_abc")
    assert url == "rtmp://live.twitch.tv/app/live_123_abc"
    assert streaming.split_url(url) == ("twitch", "live_123_abc")
    assert streaming.preset_url("facebook", "KEY").startswith("rtmps://")
    assert streaming.preset_url("custom", "x") == ""
    assert streaming.preset_url("nope", "x") == ""
    assert streaming.split_url("rtmp://example.com/live/k") == ("custom", "rtmp://example.com/live/k")
    assert streaming.preset_url("youtube", "") == "rtmp://a.rtmp.youtube.com/live2"


@pytest.mark.parametrize("url, bad", [
    ("rtmp://a.rtmp.youtube.com/live2/abcd", False),
    ("rtmps://live-api-s.facebook.com:443/rtmp/KEY", False),
    ("", True), ("   ", True), ("http://example.com/live/k", True),
    ("rtmp://", True), ("rtmp:///live/k", True), ("rtmp://host", True),
    ("rtmp://host:99999/live/k", True), ("rtmp://host/live/k; rm -rf /", True),
    ("rtmp://host/live/k extra", True), ("ftp://host/live/k", True),
])
def test_stream_urls_are_validated(url, bad):
    assert bool(streaming.validate_stream_url(url)) is bad


def test_output_config_keeps_only_clean_extra_urls():
    out = OutputConfig.from_dict({"rtmp_extra_urls": [" rtmp://a/b/c ", 5, "", "rtmps://d/e/f"]})
    assert out.rtmp_extra_urls == ["rtmp://a/b/c", "rtmps://d/e/f"]
    assert OutputConfig.from_dict({"rtmp_extra_urls": "x"}).rtmp_extra_urls == []
    assert OutputConfig.from_dict(OutputConfig(rtmp_extra_urls=["rtmp://a/b/c"]).__dict__) \
        .rtmp_extra_urls == ["rtmp://a/b/c"]


# ------------------------------------------------- a real listener to publish to
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Listener:
    """``ffmpeg -listen 1`` as a one-shot RTMP ingest that records what it receives."""

    def __init__(self, tmp_path, name: str, port: int | None = None) -> None:
        self.port = port or free_port()
        self.path = tmp_path / f"{name}.flv"
        self.url = f"rtmp://127.0.0.1:{self.port}/live/key"
        self.process = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-listen", "1", "-i", self.url,
             "-c", "copy", "-f", "flv", str(self.path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Connecting to find out would use up the one connection ffmpeg accepts,
        # so look for the port being taken instead.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", self.port))
                except OSError:
                    return
            time.sleep(0.05)
        raise RuntimeError("the listener never opened its port")

    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()


def stream_project() -> Project:
    project = Project(name="stream")
    project.output.canvas_w, project.output.canvas_h, project.output.fps = 320, 180, 15
    project.output.encoder = "cpu"
    scene = Scene("scene_stream", "Stream")
    scene.add_layer(Layer("bg", "test", "BG", source="smpte", box=(0, 0, 1, 1), z=1))
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def run_until(engine: AnyCamEngine, condition, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        engine.pump(40)
        if condition():
            return True
    return False


def states(engine: AnyCamEngine) -> list[str]:
    return [row["state"] for row in engine.report.rtmp_targets]


@needs_stack
def test_two_destinations_receive_the_same_stream_with_a_status_each(tmp_path):
    first, second = Listener(tmp_path, "one"), Listener(tmp_path, "two")
    engine = AnyCamEngine(stream_project(), preview=True, audio=False,
                          on_frame=lambda _sample: None, rtmp=first.url,
                          rtmp_extra=[second.url]).build()
    engine.start()
    try:
        assert run_until(engine, lambda: states(engine) == ["live", "live"], 12), \
            engine.report.rtmp_targets
        assert run_until(engine, lambda: first.size() > 2000 and second.size() > 2000, 6)
        rows = engine.report.rtmp_targets
        assert [row["restarts"] for row in rows] == [0, 0]
        assert "127.0.0.1" in rows[0]["url"]
    finally:
        engine.stop()
        first.stop()
        second.stop()
    for listener in (first, second):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name", "-of", "csv=p=0", str(listener.path)],
            capture_output=True, text=True)
        assert probe.stdout.strip() == "h264", (listener.path, probe.stdout, probe.stderr)


@needs_stack
def test_a_dropped_destination_reconnects_with_backoff_while_the_other_keeps_going(tmp_path):
    first, second = Listener(tmp_path, "one"), Listener(tmp_path, "two")
    port = first.port
    engine = AnyCamEngine(stream_project(), preview=True, audio=False,
                          on_frame=lambda _sample: None, rtmp=second.url,
                          rtmp_extra=[first.url]).build()
    engine.start()
    replacement = None
    try:
        assert run_until(engine, lambda: states(engine) == ["live", "live"], 12), \
            engine.report.rtmp_targets
        second_before = second.size()
        first.stop()                                    # the extra destination goes away
        assert run_until(engine, lambda: states(engine)[1] in ("waiting", "failed", "connecting"), 15)
        assert states(engine)[0] == "live"
        assert run_until(engine, lambda: second.size() > second_before + 500, 6)
        replacement = Listener(tmp_path, "back", port)  # ... and comes back
        assert run_until(engine, lambda: states(engine) == ["live", "live"], 40), \
            engine.report.rtmp_targets
        assert engine.report.rtmp_targets[1]["restarts"] >= 1
        assert run_until(engine, lambda: replacement.size() > 2000, 8)
    finally:
        engine.stop()
        for listener in (first, second, replacement):
            if listener is not None:
                listener.stop()


@needs_stack
def test_the_primary_destination_reconnects_too(tmp_path):
    first = Listener(tmp_path, "one")
    port = first.port
    engine = AnyCamEngine(stream_project(), preview=True, audio=False,
                          on_frame=lambda _sample: None, rtmp=first.url).build()
    engine.start()
    replacement = None
    try:
        assert run_until(engine, lambda: states(engine) == ["live"], 12), engine.report.rtmp_targets
        first.stop()
        assert run_until(engine, lambda: states(engine) != ["live"], 15)
        assert engine.report.rtmp_error or states(engine) != ["live"]
        replacement = Listener(tmp_path, "back", port)
        assert run_until(engine, lambda: states(engine) == ["live"], 40), engine.report.rtmp_targets
        assert engine.report.rtmp_error == ""
        assert run_until(engine, lambda: replacement.size() > 2000, 8)
    finally:
        engine.stop()
        first.stop()
        if replacement is not None:
            replacement.stop()


def test_backoff_doubles_to_a_ceiling_and_resets_after_a_healthy_run():
    from anycam.engine.outputs.stream import RtmpState

    engine = AnyCamEngine(stream_project(), preview=True, on_frame=lambda _sample: None)
    state = RtmpState(url="rtmp://x/y/z")
    delays = []
    for _ in range(8):
        engine._schedule(state, 100.0)
        delays.append(state.restart_at - 100.0)
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
    state.live_since = 50.0                     # it stayed up for 50 s
    engine._schedule(state, 100.0)
    assert state.restart_at - 100.0 == 1


def test_teardown_forgets_the_publisher_but_keeps_the_encoder():
    """A stale publisher must not flip a torn-down stream back to "live", and
    a reconnect still needs the encoder for force_keyframe (items 5 and 6)."""
    from anycam.engine.outputs.stream import RtmpState, StreamMixin

    class FakePipeline:
        def __init__(self):
            self.events = []
            self.states = []

        def send_event(self, event):
            self.events.append(event)

        def set_state(self, state):
            self.states.append(state)

    engine = StreamMixin()
    encoder = object()
    engine._rtmp_pipeline = FakePipeline()
    engine._rtmp_bus = object()
    engine._rtmp_appsrc = object()
    engine._rtmp_audio_appsrc = object()
    engine._rtmp_feed = object()
    engine._rtmp_publisher = object()
    engine._rtmp_encoder = encoder
    engine._rtmp_extras = []
    engine._rtmp_state = RtmpState(url="rtmp://x/y/z", state="live", publisher=object())

    engine._teardown_rtmp()

    assert engine._rtmp_publisher is None
    assert engine._rtmp_state.publisher is None     # no stale first_data can say "live"
    assert engine._rtmp_encoder is encoder          # the canvas still owns it
    assert engine._rtmp_pipeline is None


def test_a_reconnect_forces_a_keyframe_from_the_encoder_kept_in_the_canvas(monkeypatch):
    from types import SimpleNamespace

    from anycam.engine.outputs import stream as stream_module
    from anycam.engine.outputs.stream import RtmpState, StreamMixin

    engine = StreamMixin()
    encoder = object()
    engine._rtmp_encoder = encoder
    engine.rtmp = "rtmp://127.0.0.1/live/x"
    engine._rtmp_state = RtmpState(url=engine.rtmp, state="waiting")
    engine._rtmp_extras = []
    forced = []
    monkeypatch.setattr(stream_module, "force_keyframe", lambda element: forced.append(element))
    publisher = SimpleNamespace(pipeline=object(), bus=object(), video_src=object(),
                                audio_src=None, feed=SimpleNamespace(skipping=False))
    monkeypatch.setattr(engine, "_make_publisher", lambda *args, **kwargs: publisher)
    monkeypatch.setattr(engine, "_start_async", lambda pipeline: None)

    engine._restart_primary(time.monotonic())

    assert forced == [encoder]
    assert engine._rtmp_state.restarts == 1
    assert engine._rtmp_state.state == "connecting"


# --------------------------------------------------- recording quality and guard
def test_the_disk_guard_compares_free_space_with_the_limit(monkeypatch, tmp_path):
    import shutil
    from collections import namedtuple

    from anycam import config

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10**12, 10**12 - 300 * 2**20, 300 * 2**20))
    assert config.disk_free_mb(tmp_path / "not" / "created" / "yet") == 300
    assert "300 MiB" in config.disk_guard(tmp_path, 500)
    assert config.disk_guard(tmp_path, 200) == ""
    assert config.disk_guard(tmp_path, 0) == ""          # guard off
    monkeypatch.setattr(shutil, "disk_usage", lambda path: (_ for _ in ()).throw(OSError("gone")))
    assert config.disk_guard(tmp_path, 500) == ""        # an unreadable disk never blocks


def test_bitrate_and_guard_settings_are_clamped():
    out = OutputConfig.from_dict({"record_bitrate_kbps": 5, "stream_bitrate_kbps": 10**9,
                                  "record_min_free_mb": -4})
    assert out.record_bitrate_kbps == 500 and out.stream_bitrate_kbps == 200_000
    assert out.record_min_free_mb == 0
    assert OutputConfig.from_dict({}).record_bitrate_kbps == 0


@needs_stack
def test_a_chosen_recording_bitrate_reaches_the_encoder(tmp_path):
    project = stream_project()
    project.output.record_bitrate_kbps = 3000
    engine = AnyCamEngine(project, record_path=tmp_path / "r.mp4").build()
    try:
        encoder = engine.pipeline.get_by_name("record_encoder")
        assert encoder is not None and encoder.get_property("bitrate") == 3000
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


# --------------------------------------------------------------------- the panels
@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    for name in ("CACHE", "CONFIG", "STATE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    from PySide6.QtWidgets import QApplication

    from anycam.gui_many import ManyCamWindow

    QApplication.instance() or QApplication([])
    project = Project(name="p5")
    scene = Scene("scene_a", "Alpha")
    scene.add_layer(Layer("l1", "test", "Ball", source="ball"))
    project.scenes, project.active_scene = [scene], scene.id
    win = ManyCamWindow(project)
    win.headless = True
    win._persist = False
    yield win
    win.engine = None
    win.close()


def test_the_stream_tab_builds_urls_from_a_platform_and_key(window):
    window.stream_preset.setCurrentIndex(window.stream_preset.findData("twitch"))
    window.stream_key_edit.setText("live_1")
    window.stream_key_edit.textEdited.emit("live_1")
    assert window.rtmp_edit.text() == "rtmp://live.twitch.tv/app/live_1"
    window.stream_preset.setCurrentIndex(window.stream_preset.findData("custom"))
    window.rtmp_edit.setText("rtmp://example.com/live/abc")
    window.stream_key_edit.textEdited.emit("zzz")
    assert window.rtmp_edit.text() == "rtmp://example.com/live/abc"      # custom is left alone


def test_extra_destinations_are_checked_stored_and_removed(window):
    assert "must start with" in window.add_extra_destination("http://x/y")
    assert window.add_extra_destination("rtmp://a.example/live/k1") == ""
    assert "already" in window.add_extra_destination("rtmp://a.example/live/k1")
    window.rtmp_edit.setText("rtmp://main.example/live/m")
    assert "already" in window.add_extra_destination("rtmp://main.example/live/m")
    assert window.project.output.rtmp_extra_urls == ["rtmp://a.example/live/k1"]
    window.extra_list.setCurrentRow(0)
    window._remove_extra_destination()
    assert window.project.output.rtmp_extra_urls == [] and window.extra_list.count() == 0


def test_go_live_refuses_a_bad_address(window):
    window.rtmp_edit.setText("not a url")
    window.toggle_live()
    assert window.live is False
    window.rtmp_edit.setText("rtmp://example.com/live/key")
    window.toggle_live()
    assert window.live is True


def test_the_status_lines_show_each_destination(window):
    class FakeEngine:
        class report:
            rtmp_targets = [
                {"url": "rtmp://a/live/***", "state": "live", "error": "", "restarts": 0, "retry_in_s": 0},
                {"url": "rtmp://b/live/***", "state": "waiting", "error": "Connection refused",
                 "restarts": 2, "retry_in_s": 4.0}]

    window.engine = FakeEngine()
    window.update_stream_status()
    text = window.stream_status.text()
    assert "live  rtmp://a/live/***" in text and "retry in 4 s" in text and "Connection refused" in text
    window.engine = None


def test_a_dropped_stream_that_is_reconnecting_keeps_going_live(window):
    class Report:
        rtmp_error = "Connection reset"
        rtmp_targets = [{"url": "rtmp://a/live/***", "state": "waiting", "error": "x",
                         "restarts": 1, "retry_in_s": 2.0}]

    class FakeEngine:
        report = Report()

    window.engine = FakeEngine()
    window.live = True
    window._rtmp_error_shown = ""
    window._handle_rtmp_report()
    assert window.live is True                      # reconnecting, still publishing
    Report.rtmp_targets = []
    Report.rtmp_error = "stream output unavailable: missing GStreamer element(s)"
    window._handle_rtmp_report()
    assert window.live is False                     # nothing will retry: it stopped
    window.engine = None


def test_recording_settings_are_stored_and_the_guard_stops_a_recording(window, monkeypatch, tmp_path):
    window.record_bitrate.setValue(6000)
    window.record_min_free.setValue(900)
    window.record_dir_edit.setText(str(tmp_path / "rec"))
    window.record_dir_edit.editingFinished.emit()
    out = window.project.output
    assert (out.record_bitrate_kbps, out.record_min_free_mb, out.record_dir) == (
        6000, 900, str(tmp_path / "rec"))
    from anycam import config

    stopped = []
    window.recording = True
    window.toggle_record = lambda: stopped.append(True)
    monkeypatch.setattr(config, "disk_guard", lambda path, minimum: "only 10 MiB free")
    assert window.check_disk_guard(now=100.0) is True and stopped
    assert window.check_disk_guard(now=101.0) is False        # rate limited
    window.recording = False
    assert window.check_disk_guard(now=200.0) is False


# ------------------------------------------------------------------------- SRT
def test_srt_addresses_validate_and_hide_their_passphrase():
    assert streaming.validate_stream_url("srt://203.0.113.5:9000") == ""
    assert streaming.validate_stream_url("srt://host:9000?passphrase=secret123&latency=200") == ""
    assert "port" in streaming.validate_stream_url("srt://host")
    assert streaming.validate_stream_url("rtmp://h/app/k&x") != ""
    from anycam import config

    text = config.redact_text("connect failed: passphrase secret123 rejected",
                              "srt://host:9000?passphrase=secret123")
    assert "secret123" not in text


class SrtListener(Listener):
    def __init__(self, tmp_path, name: str) -> None:
        self.port = free_port()
        self.path = tmp_path / f"{name}.ts"
        self.url = f"srt://127.0.0.1:{self.port}"
        self.process = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-i",
             f"srt://127.0.0.1:{self.port}?mode=listener", "-c", "copy", "-f", "mpegts",
             str(self.path)], stdout=subprocess.DEVNULL,
            stderr=open(tmp_path / f"{name}.log", "wb"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                try:
                    probe.bind(("127.0.0.1", self.port))
                except OSError:
                    return
            time.sleep(0.05)
        raise RuntimeError("the SRT listener never opened its port")


@pytest.mark.skipif(shutil.which("ffmpeg") is None or Gst.ElementFactory.find("srtsink") is None
                    or Gst.ElementFactory.find("mpegtsmux") is None
                    or Gst.ElementFactory.find("x264enc") is None,
                    reason="SRT stack unavailable")
def test_an_srt_destination_receives_h264_in_transport_stream(tmp_path):
    listener = SrtListener(tmp_path, "srt")
    engine = AnyCamEngine(stream_project(), preview=True, audio=True,
                          on_frame=lambda _sample: None, rtmp=listener.url).build()
    engine.start()
    try:
        assert run_until(engine, lambda: states(engine) == ["live"], 15), engine.report.rtmp_targets
        assert run_until(engine, lambda: listener.size() > 5000, 30)
    finally:
        engine.stop()
        listener.stop()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
         str(listener.path)], capture_output=True, text=True)
    codecs = set(probe.stdout.split())
    assert "h264" in codecs and "aac" in codecs, (probe.stdout, probe.stderr)
