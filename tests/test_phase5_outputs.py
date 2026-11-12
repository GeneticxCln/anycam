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
