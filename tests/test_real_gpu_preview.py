"""The GPU preview on a real window system and a real OpenGL context.

The rest of the suite runs on the offscreen platform, which cannot create GL,
so the GPU preview path is only checked with fakes there.  This file starts the
actual studio window in a child process and drives it.

It opens a window on the live desktop, so it only runs when asked:

    ANYCAM_REAL_DISPLAY=1 pytest tests/test_real_gpu_preview.py

The child uses private XDG directories (the user's project is never read or
written), the test pattern as the only video layer, and a silent audio source
(no microphone, no camera).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("gi")

ROOT = Path(__file__).resolve().parents[1]

_enabled = os.environ.get("ANYCAM_REAL_DISPLAY") == "1"
_has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

pytestmark = pytest.mark.skipif(
    not (_enabled and _has_display),
    reason="set ANYCAM_REAL_DISPLAY=1 on a machine with a display to run this")


_DRIVER = textwrap.dedent(
    """
    import json, os, sys, tempfile, time

    tmp = tempfile.mkdtemp(prefix="anycam-realgpu-")
    for key in ("CONFIG", "STATE", "CACHE", "DATA"):
        os.environ[f"XDG_{key}_HOME"] = f"{tmp}/{key.lower()}"
    sys.path.insert(0, sys.argv[1])

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from gi.repository import Gst
    from anycam import config
    config.microphone = lambda: ""
    import anycam.gui_many as gm
    from anycam.model import Layer, empty_project, new_id

    app = QApplication(sys.argv[:1])
    project = empty_project()
    project.scene.add_layer(Layer(new_id("layer"), "test", "Test",
                                  box=(0, 0, 1, 1), z=1))
    window = gm.ManyCamWindow(project)
    window.headless = False
    window.audio_on = True
    window.show()
    out = {}
    audio = [0]

    def start():
        window.start_engine()
        engine = window.engine
        if engine is None:
            out["error"] = "no engine"
            return finish()
        if engine.audio_mixer is not None:
            def count(pad, info):
                audio[0] += 1
                return Gst.PadProbeReturn.OK
            engine.audio_mixer.get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, count)
        out["backends"] = [engine.canvas_backend, engine.preview_backend]
        out["frames0"] = engine._frame_count
        out["t0"] = time.monotonic()
        QTimer.singleShot(3000, measure)

    def measure():
        engine = window.engine
        out["fps"] = (engine._frame_count - out["frames0"]) / (
            time.monotonic() - out["t0"])
        out["audio_buffers"] = audio[0]
        layer_id = window.scene.layers[0].id
        t = time.monotonic()
        window.remove_layers([layer_id])
        out["remove_s"] = time.monotonic() - t
        t = time.monotonic()
        window.add_layer("test")
        out["add_s"] = time.monotonic() - t
        t = time.monotonic()
        window.add_scene()
        out["scene_s"] = time.monotonic() - t
        QTimer.singleShot(500, record)

    def record():
        window.project.output.record_dir = tmp
        window.toggle_record()
        QTimer.singleShot(3000, stop_record)

    def stop_record():
        window.toggle_record()
        import glob, subprocess
        files = glob.glob(tmp + "/*.mp4")
        out["recording"] = files[0] if files else ""
        if files:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
                 "-of", "json", files[0]], capture_output=True, text=True).stdout
            streams = json.loads(probe or "{}").get("streams", [])
            out["durations"] = {s["codec_type"]: float(s.get("duration") or 0)
                                for s in streams}
        QTimer.singleShot(300, finish)

    def finish():
        t = time.monotonic()
        window.shutdown()
        out["shutdown_s"] = time.monotonic() - t
        print("RESULT " + json.dumps(out), flush=True)
        app.quit()

    QTimer.singleShot(1500, start)
    app.exec()
    os._exit(0)
    """
)


def _run(canvas: str, preview: str) -> dict:
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    env["ANYCAM_CANVAS"] = canvas
    env["ANYCAM_PREVIEW"] = preview
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _DRIVER, str(ROOT / "src")],
        cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=90)
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    pytest.fail(f"no result from the child (exit {proc.returncode}):\n"
                f"{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}")


@pytest.mark.parametrize("canvas", ["gpu", "cpu"])
def test_the_gpu_preview_runs_on_a_real_display(canvas):
    result = _run(canvas, "gpu")
    assert "error" not in result, result
    assert result["backends"][1] == "gpu", result
    assert result["fps"] > 20, result
    assert result["remove_s"] < 1.0, result
    assert result["add_s"] < 1.0, result
    assert result["scene_s"] < 1.0, result
    assert result["shutdown_s"] < 3.0, result


@pytest.mark.parametrize("canvas", ["gpu", "cpu"])
def test_audio_flows_with_the_gpu_preview_on_a_real_display(canvas):
    """Regression: with the GPU preview the audio mixer used to start dead."""
    result = _run(canvas, "gpu")
    assert result.get("audio_buffers", 0) > 0, result


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is not installed")
@pytest.mark.parametrize("canvas", ["gpu", "cpu"])
def test_a_recording_made_with_the_gpu_preview_has_sound_for_its_whole_length(canvas):
    """Regression: the audio ended after ~50 ms (rate change in the mixer
    output), and the GPU canvas with an NVENC recording segfaulted in the
    preview tap about every other run."""
    result = _run(canvas, "gpu")
    durations = result.get("durations") or {}
    assert "video" in durations and "audio" in durations, result
    assert durations["video"] > 2.0, result
    assert abs(durations["video"] - durations["audio"]) < 0.5, result
