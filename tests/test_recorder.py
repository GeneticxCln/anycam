"""The persistent recorder must survive canvas rebuilds and keep audio."""
from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import time
import wave
from pathlib import Path

import pytest

pytest.importorskip("gi")
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    HAS_RECORDER = Gst.ElementFactory.find("mp4mux") is not None
except Exception:  # pragma: no cover - platform without GStreamer
    HAS_RECORDER = False

from anycam.demo import build_demo_project  # noqa: E402
from anycam.engine.outputs.recorder import Recorder  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_RECORDER or shutil.which("ffprobe") is None,
    reason="GStreamer mp4mux and ffprobe are required",
)


def _probe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_two_engines_write_one_continuous_file(tmp_path):
    """A rebuild resets encoder PTS; the recorder must remap it, not restart."""
    out = tmp_path / "continuous.mp4"
    recorder = Recorder()
    assert recorder.start(str(out), 320, 180, 30)
    for _ in range(2):
        engine = AnyCamEngine(
            build_demo_project("testsrc", 320, 180, 30),
            recorder=recorder,
            preview=True,
        ).build()
        engine.start()
        try:
            for _ in range(60):
                engine.pump(50)
                time.sleep(0.01)
        finally:
            engine.stop()
    result = recorder.stop()
    assert result["ok"], result
    assert out.stat().st_size > 1000
    info = _probe(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert float(video["duration"]) > 1.5, info


def test_recorder_muxes_audio_and_video(tmp_path):
    wav = tmp_path / "tone.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        frames = b"".join(
            struct.pack("<hh", int(8000 * math.sin(2 * math.pi * 440 * t / 48000)),
                        int(8000 * math.sin(2 * math.pi * 440 * t / 48000)))
            for t in range(48000 * 2)
        )
        handle.writeframes(frames)

    out = tmp_path / "audio.mp4"
    project = build_demo_project("testsrc", 320, 180, 30)
    project.audio.media_file = str(wav)
    recorder = Recorder()
    assert recorder.start(str(out), 320, 180, 30, audio=True)
    engine = AnyCamEngine(project, recorder=recorder, preview=True, audio=True).build()
    engine.start()
    try:
        for _ in range(80):
            engine.pump(50)
            time.sleep(0.01)
    finally:
        engine.stop()
    result = recorder.stop()
    assert result["ok"], result
    info = _probe(out)
    codecs = {stream["codec_type"]: stream["codec_name"]
              for stream in info["streams"]}
    assert codecs.get("video") == "h264", info
    assert codecs.get("audio") == "aac", info
