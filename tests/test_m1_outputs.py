"""M1 slice 1: outputs live in their own pipeline behind a leaky bridge.

The failure mode this pins down is audit B9/B11: a server that accepts a TCP
connection and then never speaks RTMP used to block ``rtmpsink``, which reached
the tee and froze the preview, the recorder and the virtual camera.
"""
from __future__ import annotations

import socket
import threading
import time
import wave

import pytest

pytest.importorskip("gi")

from anycam.model import Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

HAS_COMPOSITOR = False
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    HAS_COMPOSITOR = Gst.ElementFactory.find("compositor") is not None
    HAS_RTMP = Gst.ElementFactory.find("rtmpsink") is not None
    HAS_RTSP = (Gst.ElementFactory.find("rtspsrc") is not None
                and Gst.ElementFactory.find("uridecodebin") is not None)
    HAS_MP4MUX = Gst.ElementFactory.find("mp4mux") is not None
except Exception:  # pragma: no cover - platform without GStreamer
    HAS_RTMP = False
    HAS_RTSP = False
    HAS_MP4MUX = False

pytestmark = pytest.mark.skipif(
    not HAS_COMPOSITOR,
    reason="GStreamer compositor unavailable",
)


def make_project() -> Project:
    project = Project(name="rtmp")
    project.output = OutputConfig(canvas_w=320, canvas_h=180, fps=30)
    scene = Scene("scene_rtmp", "RTMP")
    scene.add_layer(Layer("layer_test", "test", "Pattern", source="ball",
                          box=(0.0, 0.0, 1.0, 1.0), z=1))
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


@pytest.mark.skipif(not HAS_RTMP, reason="rtmpsink unavailable")
def test_unresponsive_rtmp_cannot_stall_the_canvas():
    """The canvas must keep producing while the publish pipeline is stuck."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    frames = []
    engine = AnyCamEngine(
        make_project(),
        preview=True,
        rtmp=f"rtmp://127.0.0.1:{port}/live/secret",
        on_frame=lambda sample: frames.append(1),
    ).build()
    try:
        engine.start()
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            engine.pump(50)
        started = time.monotonic()
        engine.stop()
        stop_seconds = time.monotonic() - started
    finally:
        server.close()

    # Before the split the tee blocked after a handful of frames.
    assert len(frames) > 30, f"canvas stalled at {len(frames)} frames"
    assert engine.report.error == "", engine.report.error
    assert stop_seconds < 2.5, f"stop() took {stop_seconds:.2f}s"
    # The stream key must not leak into the report even when the output fails.
    summary = engine.report.as_dict()
    assert "secret" not in summary["rtmp_url"]


@pytest.mark.skipif(not HAS_RTSP, reason="rtspsrc/uridecodebin unavailable")
def test_silent_rtsp_source_cannot_stall_the_canvas():
    """B10: a network source that never answers must not hold the canvas."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    server.settimeout(0.2)
    port = server.getsockname()[1]
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            # Accept the RTSP connection but never answer it.
            accepted.append(conn)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    project = make_project()
    project.scene.add_layer(Layer(
        "layer_network", "network", "Silent RTSP",
        source=f"rtsp://127.0.0.1:{port}/live",
        box=(0.0, 0.0, 1.0, 1.0), z=2,
    ))
    frames: list[int] = []
    engine = AnyCamEngine(
        project, preview=True, on_frame=lambda _sample: frames.append(1)
    ).build()
    stop_seconds = 0.0
    try:
        engine.start()
        warmup = time.monotonic() + 2.5
        while time.monotonic() < warmup:
            engine.pump(50)
        baseline = engine._frame_count
        started = time.monotonic()
        while time.monotonic() - started < 2.0:
            engine.pump(50)
        elapsed = time.monotonic() - started
        measured = engine._frame_count - baseline
    finally:
        stop_started = time.monotonic()
        engine.stop()
        stop_seconds = time.monotonic() - stop_started
        stop.set()
        server.close()
        for conn in accepted:
            conn.close()
        thread.join(timeout=2)

    assert measured >= 0.99 * elapsed * 30, (
        f"canvas produced {measured} frames in {elapsed:.2f}s"
    )
    assert frames, "network stall left the preview without a frame"
    assert engine.report.error == "", engine.report.error
    assert stop_seconds < 1.0, f"stop() took {stop_seconds:.2f}s"


@pytest.mark.skipif(not HAS_MP4MUX, reason="mp4mux unavailable")
def test_missing_rtmp_sink_keeps_preview_and_recording(monkeypatch, tmp_path):
    """B11: a refused output must not abort the canvas or the recording."""
    from anycam import pipeline as pipeline_module

    real_make = pipeline_module.make

    def make_without_rtmp(factory, name=None, **props):
        if factory == "rtmpsink":
            raise RuntimeError("rtmpsink is not installed")
        return real_make(factory, name, **props)

    class _FakeVcam:
        active = True
        pixel_format = "YUY2"
        error = ""

        def __init__(self) -> None:
            self.samples = 0

        def push_sample(self, _sample) -> None:
            self.samples += 1

        def poll(self) -> None:
            pass

    monkeypatch.setattr(pipeline_module, "make", make_without_rtmp)
    frames: list[int] = []
    vcam = _FakeVcam()
    out = tmp_path / "still-recording.mp4"
    engine = AnyCamEngine(
        make_project(),
        preview=True,
        record_path=out,
        rtmp="rtmp://127.0.0.1:1/live/refused",
        vcam=True,
        vcam_output=vcam,
        on_frame=lambda _sample: frames.append(1),
    ).build()
    try:
        engine.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            engine.pump(50)
    finally:
        engine.stop()

    assert len(frames) > 30, f"canvas stalled at {len(frames)} frames"
    assert "rtmpsink" in engine.report.rtmp_error, engine.report.rtmp_error
    assert engine.report.error == "", engine.report.error
    assert engine.report.record_ok, engine.report.as_dict()
    assert out.exists() and out.stat().st_size > 0
    assert engine.report.vcam_active
    assert vcam.samples > 0, "virtual-camera tap stopped with a refused output"


@pytest.mark.skipif(not (HAS_RTMP and HAS_MP4MUX),
                    reason="rtmpsink or mp4mux unavailable")
def test_recording_and_streaming_have_separate_audio_queues(tmp_path):
    """B21: one muxer must not block the other through the shared audio tee."""
    if Gst.ElementFactory.find("avenc_aac") is None:
        pytest.skip("avenc_aac unavailable")
    wav = tmp_path / "bed.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\0" * 96000)
    project = make_project()
    project.audio.media_file = str(wav)
    project.audio.mic_enabled = False
    engine = AnyCamEngine(
        project,
        record_path=tmp_path / "both.mp4",
        rtmp="rtmp://127.0.0.1:1/live/both",
        audio=True,
    ).build()
    try:
        assert engine.pipeline.get_by_name("record_audio_queue") is not None
        assert engine.pipeline.get_by_name("rtmp_audio_queue") is not None
        assert engine.report.audio_inputs
    finally:
        engine.pipeline.set_state(Gst.State.NULL)
