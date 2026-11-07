#!/usr/bin/env python3
"""Live end-to-end check of the real AnyCam window.

Opens the default window (``ManyCamWindow``) on the current desktop with the
real webcam and the real v4l2loopback node, drives it the way a user would and
reports what works and what does not.  Nothing is mocked except the polkit
refresh (so no password prompt appears) and modal dialogs (recorded, not shown).

    ./scripts/live-app-check.py                # everything
    ./scripts/live-app-check.py --no-mic       # do not open the microphone
    ./scripts/live-app-check.py --screen       # also test a portal screen share
                                               # (you must pick one in the picker)
    ./scripts/live-app-check.py --offscreen    # no visible window

It never writes your saved project (the window is given its own project), and
its recordings go to a temporary directory that is deleted unless --keep is
given.  The report is printed and written to --report-dir (Markdown + JSON,
plus the GStreamer warnings log).  Exit status is 1 when any check FAILed.
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("--offscreen", action="store_true")
parser.add_argument("--no-mic", action="store_true", help="skip every step that opens the microphone")
parser.add_argument("--screen", action="store_true", help="(default now) test a portal screen share")
parser.add_argument("--ui", choices=("manycam", "classic"), default="manycam",
                    help="which window to test (the default one, or --ui classic)")
parser.add_argument("--no-screen", action="store_true", help="skip the portal screen share steps")
parser.add_argument("--gpu-heavy", action="store_true",
                    help="also run NVENC encoders, GPU colour conversion and 2.5K/4K formats "
                         "(off by default: they once coincided with a compositor crash)")
parser.add_argument("--quick", action="store_true", help="sample the effect catalogue (every 7th tile)")
parser.add_argument("--keep", action="store_true", help="keep the recordings and work files")
parser.add_argument("--report-dir", default="")
parser.add_argument("--only", default="", help="comma separated step names to run")
ARGS = parser.parse_args()

STAMP = time.strftime("%Y%m%d-%H%M%S")
REPORT_DIR = Path(ARGS.report_dir or Path.home() / ".cache" / "anycam-live-reports" / STAMP)
WORK = Path(tempfile.mkdtemp(prefix="anycam-live-"))
REPORT_DIR.mkdir(parents=True, exist_ok=True)
HANG_FILE = open(REPORT_DIR / "hang-stacks.txt", "w", buffering=1)
if ARGS.offscreen:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
GPU_SAFE = not ARGS.gpu_heavy
if GPU_SAFE:
    os.environ["ANYCAM_ENCODER"] = "cpu"        # x264, no NVENC
os.environ.setdefault("GST_DEBUG", "2")
os.environ["GST_DEBUG_NO_COLOR"] = "1"
os.environ["GST_DEBUG_FILE"] = str(REPORT_DIR / "gstreamer.log")
sys.path.insert(0, str(ROOT / "src"))

# --------------------------------------------------------------------- report
T0 = time.monotonic()


class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.events: list[dict] = []
        self.step = "startup"
        self.gaps: dict[str, float] = {}

    def now(self) -> float:
        return round(time.monotonic() - T0, 1)

    def add(self, name: str, status: str, detail: str = "") -> None:
        arm_watchdog()
        self.rows.append({"step": self.step, "check": name, "status": status,
                          "detail": detail, "t": self.now()})
        mark = {"PASS": "  ok ", "FAIL": " FAIL", "WARN": " warn", "SKIP": " skip",
                "INFO": " info"}[status]
        print(f"[{self.now():6.1f}s]{mark}  {name}" + (f"  — {detail}" if detail else ""),
              flush=True)

    def check(self, name: str, ok: bool, detail: str = "", *, warn: bool = False) -> bool:
        self.add(name, "PASS" if ok else ("WARN" if warn else "FAIL"), detail)
        return ok

    def info(self, name: str, detail: str = "") -> None:
        self.add(name, "INFO", detail)

    def skip(self, name: str, detail: str = "") -> None:
        self.add(name, "SKIP", detail)

    def event(self, kind: str, text: str) -> None:
        self.events.append({"t": self.now(), "step": self.step, "kind": kind,
                            "text": str(text)})


R = Report()


def arm_watchdog(seconds: float = 45.0) -> None:
    """If nothing is recorded for ``seconds``, dump every thread's stack (a hang)."""
    if HANG_FILE is not None:
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(seconds, repeat=False, file=HANG_FILE)


class Tee:
    """Capture everything the app prints to stderr (and keep it in a file)."""

    def __init__(self, path: Path) -> None:
        self.real = sys.__stderr__
        self.file = open(path, "w", buffering=1)
        self.partial = ""

    def write(self, text: str) -> int:
        self.file.write(text)
        self.partial += text
        while "\n" in self.partial:
            line, self.partial = self.partial.split("\n", 1)
            if line.strip():
                R.event("stderr", line)
        return len(text)

    def flush(self) -> None:
        self.file.flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self.real.fileno()


sys.stderr = Tee(REPORT_DIR / "app-stderr.log")

from PySide6.QtCore import QTimer, qInstallMessageHandler  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import psutil  # noqa: E402
from anycam import config, layouts, overlay, particles, pool as pool_module  # noqa: E402
from anycam.gui_many import ManyCamWindow  # noqa: E402
from anycam.model import empty_project  # noqa: E402

if GPU_SAFE:
    from anycam.engine.outputs import vcam as _vcam_module

    _vcam_module.VcamOutput._gl_available = staticmethod(lambda: False)


def gpu_state() -> tuple[int, int, int] | None:
    """(utilisation %, memory used MiB, memory total MiB) from nvidia-smi."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        util, used, total = (int(v.strip()) for v in out.stdout.splitlines()[0].split(","))
        return util, used, total
    except Exception:  # noqa: BLE001 - no NVIDIA GPU or tool
        return None


def gpu_ok_to_continue() -> bool:
    """Wait for a busy GPU to settle; False when it will not (stop the run)."""
    for _ in range(12):
        state = gpu_state()
        if state is None:
            return True
        util, used, total = state
        if util < 80 and used < 0.85 * total:
            return True
        pump(2.0)
    return False


# ----------------------------------------------------------------- helpers
APP = QApplication(sys.argv[:1])
W: ManyCamWindow
FR = {"n": 0}
COUNTS = Counter()
LAST_TICK = [time.monotonic()]


def pump(seconds: float, until=None) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        APP.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.004)
    return bool(until()) if until is not None else True


def frames_per_second(seconds: float = 1.5) -> float:
    n0, t0 = FR["n"], time.monotonic()
    pump(seconds)
    return (FR["n"] - n0) / max(1e-6, time.monotonic() - t0)


def alive(label: str, seconds: float = 0.8) -> bool:
    """The canvas exists, reports no error, and keeps delivering frames."""
    if W.engine is None:
        R.check(f"{label}: canvas running", False, "the preview engine is gone")
        return False
    n0 = FR["n"]
    pump(seconds)
    error = W.engine.report.error if W.engine is not None else "engine gone"
    ok = W.engine is not None and not error and FR["n"] > n0
    R.check(f"{label}: canvas alive", ok,
            f"error={error!r}" if error else
            ("no new preview frames" if FR["n"] <= n0 else ""))
    return ok


def region_stats(image, box) -> tuple[float, float]:
    """Mean and spread of the pixels inside a layer's box (a black feed has neither)."""
    if image is None or image.isNull():
        return -1.0, -1.0
    x, y, w, h = box
    part = image.copy(int(x * image.width()), int(y * image.height()),
                      max(2, int(w * image.width())), max(2, int(h * image.height())))
    small = part.scaled(48, 27).convertToFormat(QImage.Format.Format_RGBA8888) \
        if False else part.scaled(48, 27)
    total, count, values = 0.0, 0, []
    for yy in range(27):
        for xx in range(48):
            c = small.pixelColor(xx, yy)
            v = (c.red() + c.green() + c.blue()) / 3
            values.append(v)
    mean = sum(values) / len(values)
    spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
    return mean, spread


def image_mean(image) -> float:
    if image is None or image.isNull():
        return -1.0
    small = image.scaled(32, 18)
    total = 0
    for y in range(18):
        for x in range(32):
            colour = small.pixelColor(x, y)
            total += colour.red() + colour.green() + colour.blue()
    return total / (32 * 18 * 3)


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_packets", "-show_streams", "-show_format",
         "-of", "json", path], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        return {"error": out.stderr.strip() or "ffprobe failed"}
    data = json.loads(out.stdout or "{}")
    info: dict = {"duration": float(data.get("format", {}).get("duration") or 0)}
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        info[kind] = {
            "codec": stream.get("codec_name"),
            "w": stream.get("width"), "h": stream.get("height"),
            "packets": int(stream.get("nb_read_packets") or 0),
            "duration": float(stream.get("duration") or 0),
        }
    return info


def device_fds(device: str) -> int:
    target = os.path.realpath(device)
    count = 0
    try:
        for fd in os.listdir("/proc/self/fd"):
            try:
                if os.path.realpath(os.readlink(f"/proc/self/fd/{fd}")) == target:
                    count += 1
            except OSError:
                continue
    except OSError:
        pass
    return count


def timed(function, *args, **kwargs):
    start = time.monotonic()
    result = function(*args, **kwargs)
    return result, time.monotonic() - start


def camera_health(layer_id: str) -> dict:
    if W.engine is None:
        return {}
    return W.engine.report.source_health.get(layer_id, {})


def consumer_frames(seconds: float = 3.0, snapshot: Path | None = None) -> tuple[int, str]:
    """Read the loopback device like Discord would, while the app keeps running."""
    log = WORK / "ffmpeg-vcam.log"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-f", "v4l2", "-i",
               W.project.output.vcam_device or "/dev/video10", "-t", str(seconds)]
    if snapshot is not None:
        command += ["-map", "0:v", "-frames:v", "1", str(snapshot)]
    else:
        command += ["-f", "null", "-"]
    with open(log, "w") as handle:
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
    pump(seconds + 2.5, until=lambda: process.poll() is not None)
    if process.poll() is None:
        process.kill()
    text = log.read_text(errors="replace")
    counts = re.findall(r"frame=\s*(\d+)", text)
    tail = " | ".join(line.strip() for line in text.strip().splitlines()[-2:])
    return (int(counts[-1]) if counts else 0), tail


def make_pngs() -> Path:
    from PIL import Image

    path = WORK / "gradient.png"
    image = Image.new("RGB", (800, 400))
    for x in range(800):
        for y in range(0, 400, 4):
            image.paste((x * 255 // 800, 120, 255 - x * 255 // 800), (x, y, x + 1, y + 4))
    image.save(path)
    return path


# ------------------------------------------------------------------- steps
CTX: dict = {}


def step_startup() -> None:
    R.check("window is visible", W.isVisible())
    held, spent = timed(W.hold_vcam_device)
    CTX["vcam_ok"] = bool(held)
    R.check("virtual camera node opened at launch", bool(held),
            f"{W.project.output.vcam_device} ({spent:.1f}s)" if held else
            (W._vcam_output.error if W._vcam_output else "could not open"), warn=True)
    if held:
        W.feed_held_vcam()
        W.note_vcam_consumers()
    R.check("vcam is on from launch (as in the real app)", W.vcam_on is bool(held))
    R.check("no scenes crash on an empty project", len(W.project.scenes) == 1)
    R.info("empty-project hint", f"layers={len(W.scene.layers)}, engine={'on' if W.engine else 'off'}")


def step_test_source() -> None:
    layer = W.add_layer("test")
    R.check("test pattern layer added", layer is not None)
    if layer is not None:
        layer.source = "smpte"      # colour bars: a picture that is clearly not black
    CTX["test_layer"] = layer
    if W.engine is None:
        W.toggle_engine()
    R.check("preview engine started", W.engine is not None,
            W.statusBar().currentMessage())
    got = pump(8, until=lambda: FR["n"] >= 20)
    R.check("preview frames arrive", got, f"{FR['n']} frames")
    if W.engine is None:
        return
    fps = frames_per_second(3)
    R.check("preview rate", fps >= 0.8 * W.project.output.fps,
            f"{fps:.1f} fps delivered to the window (canvas {W.project.output.fps})", warn=True)
    R.check("preview shows a picture (not black)", image_mean(W.preview.image) > 5,
            f"mean level {image_mean(W.preview.image):.1f}")
    R.check("engine reports no error", not W.engine.report.error, W.engine.report.error)
    R.check("virtual camera tap attached on launch",
            (not CTX.get("vcam_ok")) or W.engine.report.vcam_active,
            W.engine.report.vcam_error, warn=True)


def step_camera() -> None:
    device = config.camera_device()
    if not Path(device).exists():
        R.skip("webcam", f"{device} does not exist")
        CTX["cam"] = None
        return
    before = COUNTS["start_engine"]
    layer = W.add_layer("camera", source=device)
    CTX["cam"] = layer
    R.check("camera layer added", layer is not None, device)
    if layer is None:
        return
    R.info("camera attach", "live on the running canvas" if COUNTS["start_engine"] == before
           else f"canvas rebuilt {COUNTS['start_engine'] - before}x")
    got = pump(12, until=lambda: camera_health(layer.id).get("frames", 0) > 5)
    health = camera_health(layer.id)
    R.check("camera delivers frames", got, f"{health.get('frames')} frames; "
            f"modes={W.engine.report.camera_modes if W.engine else None}; "
            f"status={W.statusBar().currentMessage()!r}")
    pump(3)
    health = camera_health(layer.id)
    fps = health.get("fps") or 0
    R.check("camera frame rate", fps >= 25, f"{fps} fps from the device", warn=True)
    R.check("camera not stalled", layer.id not in (W.engine.report.stalled_sources if W.engine else []),
            str(W.engine.report.stalled_sources if W.engine else ""))
    R.check("camera not marked lost", layer.id not in W._lost_cameras)
    R.check("no engine error with the camera", W.engine is not None and not W.engine.report.error,
            W.engine.report.error if W.engine else "engine gone")
    mean, spread = region_stats(W.preview.image, layer.box)
    R.check("camera picture is live (not a black or flat frame)", mean > 8 or spread > 2.0,
            f"inside the camera box: mean {mean:.1f}, spread {spread:.1f}")
    CTX["cam_fds"] = device_fds(config.camera_device())


def step_vcam() -> None:
    if not CTX.get("vcam_ok") or W.engine is None:
        R.skip("virtual camera", "no loopback device or no running canvas")
        return
    R.check("vcam attached to the canvas", W.engine.report.vcam_active,
            W.engine.report.vcam_error)
    frames, tail = consumer_frames(3.0)
    R.check("an app reading /dev/video10 gets frames", frames >= 20, f"{frames} frames; {tail}")
    shot = WORK / "vcam.png"
    consumer_frames(1.5, snapshot=shot)
    if shot.exists():
        grabbed = QImage(str(shot))
        mean, spread = region_stats(grabbed, CTX["cam"].box if CTX.get("cam") else (0, 0, 1, 1))
        R.check("frame seen by that app shows the camera (not black)", mean > 8 or spread > 2.0,
                f"inside the camera box: mean {mean:.1f}, spread {spread:.1f}")
        shot.unlink()
    else:
        R.check("frame seen by that app is not black", False, "could not grab a frame")
    caps = config.vcam_udev_capabilities()
    R.check("udev advertises capture (Discord/Chrome can list it)",
            caps is None or "capture" in caps, f"ID_V4L_CAPABILITIES={caps!r}", warn=True)
    R.info("exclusive_caps", str(config.vcam_exclusive_caps()))
    # off / on
    _, took = timed(W.toggle_vcam)
    R.check("vcam off: button responds", took < 2.5, f"{took:.1f}s")
    R.check("vcam off: tap detached", W.engine is not None and not W.engine.report.vcam_active)
    _, took = timed(W.toggle_vcam)
    R.check("vcam on again: button responds", took < 3.0, f"{took:.1f}s")
    pump(1.5)
    R.check("vcam on again: tap attached", W.engine is not None and W.engine.report.vcam_active,
            W.engine.report.vcam_error if W.engine else "engine gone")
    frames, tail = consumer_frames(2.5)
    R.check("frames flow again after toggling", frames >= 15, f"{frames} frames; {tail}")


def _record(seconds: float, between=None) -> tuple[list[str], float]:
    """Record for ``seconds`` (calling ``between`` midway), return segments + stop time."""
    W.toggle_record()
    R.check("recording started", W.recording and W._recorder is not None,
            W.statusBar().currentMessage())
    half = seconds / 2
    pump(half)
    if between is not None:
        between()
    pump(half)
    _, took = timed(W.toggle_record)
    pump(0.5)
    return list(W.recording_segments), took


def _check_recording(segments: list[str], label: str, seconds: float, *, audio: bool = False,
                     sizes: list[tuple[int, int]] | None = None) -> None:
    R.check(f"{label}: file saved", bool(segments), str(segments))
    R.check(f"{label}: listed once in the session", len(segments) == len(set(segments)),
            str(segments))
    total_v = 0
    for index, path in enumerate(segments):
        info = probe(path)
        if "error" in info:
            R.check(f"{label}: file {index + 1} plays", False, info["error"])
            continue
        video = info.get("video") or {}
        total_v += video.get("packets", 0)
        R.check(f"{label}: file {index + 1} has video", bool(video),
                f"{video.get('w')}x{video.get('h')} {video.get('codec')} "
                f"{video.get('packets')} packets {info['duration']:.1f}s")
        if sizes and index < len(sizes):
            R.check(f"{label}: file {index + 1} size", (video.get("w"), video.get("h")) == sizes[index],
                    f"{video.get('w')}x{video.get('h')} expected {sizes[index]}")
        if audio and index == 0:
            sound = info.get("audio") or {}
            R.check(f"{label}: has an audio track", bool(sound))
            if sound and video:
                skew = abs(video["duration"] - sound["duration"])
                R.check(f"{label}: audio and video the same length", skew < 0.6,
                        f"video {video['duration']:.2f}s, audio {sound['duration']:.2f}s")
    rate = W.project.output.fps
    expected = seconds * rate
    R.check(f"{label}: video is not missing frames", total_v >= 0.7 * expected,
            f"{total_v} video packets, about {expected:.0f} expected at {rate} fps", warn=True)


def step_record() -> None:
    if W.engine is None:
        R.skip("recording", "no running canvas")
        return
    W.project.output.record_dir = str(WORK / "rec")
    segments, took = _record(6.0)
    R.check("stop recording returns promptly", took < 3.0, f"{took:.1f}s")
    _check_recording(segments, "video-only recording", 6.0)
    alive("after recording")


def step_record_audio() -> None:
    if ARGS.no_mic:
        R.skip("recording with audio", "--no-mic")
        return
    mics = [s for s in config.list_audio_sources() if not s.is_monitor]
    if not mics:
        R.skip("recording with audio", "no microphone source found")
        return
    W._toggle_audio(True)
    pump(2)
    R.check("canvas back after enabling the mic", W.engine is not None, W.statusBar().currentMessage())
    state = {"mic": True}

    def toggle_mic() -> None:
        # a rebuild in the middle of the recording, with audio dropping out
        _, took = timed(W._toggle_audio, False)
        R.check("mic off during recording: UI responds", took < 3.0, f"{took:.1f}s")
        pump(2.5)
        W._toggle_audio(True)
        state["mic"] = True

    segments, took = _record(10.0, between=toggle_mic)
    R.check("stop recording returns promptly (audio)", took < 3.0, f"{took:.1f}s")
    _check_recording(segments, "recording with audio + mic toggle", 10.0, audio=True)
    W._toggle_audio(False)
    pump(1.5)


def step_record_resize() -> None:
    if W.engine is None:
        R.skip("resize while recording", "no running canvas")
        return
    original = (W.project.output.canvas_w, W.project.output.canvas_h, W.project.output.fps)
    W.toggle_record()
    pump(2.5)
    _, took = timed(W.set_output_format, 1280, 720, 30)
    R.check("changing the canvas while recording: UI responds", took < 4.0, f"{took:.1f}s")
    pump(3)
    _, took = timed(W.toggle_record)
    segments = list(W.recording_segments)
    R.check("a canvas change starts a second file", len(segments) == 2, str(segments))
    for index, (path, size) in enumerate(zip(segments, [(original[0], original[1]), (1280, 720)])):
        info = probe(path)
        video = info.get("video") or {}
        R.check(f"resize recording: file {index + 1} is {size[0]}x{size[1]} and plays",
                (video.get("w"), video.get("h")) == size and video.get("packets", 0) > 10,
                f"{video.get('w')}x{video.get('h')} {video.get('packets')} packets"
                if video else str(info))
    W.set_output_format(*original)
    pump(3)
    alive("after restoring the canvas size")


def step_scenes() -> None:
    cam = CTX.get("cam")
    W.add_scene()
    pump(1.5)
    R.check("second scene added", len(W.project.scenes) == 2)
    durations = []
    for cycle in range(5):
        for index in (0, 1):
            _, took = timed(W.switch_scene, index)
            durations.append(took)
            pump(0.8)
    worst = max(durations)
    R.check("scene switches never freeze the window", worst < 2.5,
            f"slowest {worst:.1f}s, average {sum(durations) / len(durations):.1f}s over {len(durations)}")
    W.switch_scene(0)
    if cam is not None:
        got = pump(10, until=lambda: camera_health(cam.id).get("frames", 0) > 3 and FR["n"] > 0)
        R.check("camera is back after switching scenes", got,
                f"health={camera_health(cam.id)}")
    pump(3)
    chip = W.status_chip.text()
    R.check("status chip says LIVE after switching", chip == "LIVE", f"chip={chip!r}", warn=True)
    alive("after scene switching")


def step_layouts() -> None:
    if W.engine is None:
        R.skip("layouts", "no canvas")
        return
    engine = W.engine
    for preset in layouts.LAYOUT_PRESETS:
        W.apply_layout(preset.key)
        pump(0.7)
        R.check(f"layout {preset.key}: applied live",
                W.engine is engine and not W.engine.report.error,
                "canvas was rebuilt" if W.engine is not engine else W.engine.report.error)
        engine = W.engine
        if engine is None:
            return
    W.apply_layout("pip")
    alive("after layouts")


def step_effects() -> None:
    cam = CTX.get("cam")
    if cam is None or W.engine is None:
        R.skip("effects", "no camera layer")
        return
    W.preview.select_layer(cam.id)
    pump(0.3)
    slot_failures = []
    for key in ("bulge", "tilt", "blur", "aging", "kaleidoscope", "fisheye", "bg-blur",
                "bg-dim", "spotlight", "sunglasses", "cat", "heart-eyes", "orbit-hearts", "none"):
        W._set_effect("filter", key)
        chain = W.engine._layer_chains.get(cam.id) if W.engine else None
        slot = chain.effects.get("slot") if chain else None
        if slot is None:
            R.check("filter slot exists", False, "no live filter slot on the camera layer")
            return
        done = slot.idle.wait(8)
        pump(0.35)
        if not done or slot.key != key or (W.engine and W.engine.report.error):
            slot_failures.append(f"{key}: done={done} key={slot.key!r} "
                                 f"error={W.engine.report.error if W.engine else 'gone'!r}")
    R.check("14 live filters swap in without errors", not slot_failures, "; ".join(slot_failures))
    W._set_effect("filter2", "pinch")
    chain = W.engine._layer_chains.get(cam.id) if W.engine else None
    slot2 = chain.effects.get("slot2") if chain else None
    done = bool(slot2 and slot2.idle.wait(8))
    R.check("second filter slot stacks", done and slot2.key == "pinch",
            f"key={slot2.key if slot2 else None!r}")
    W._set_effect("filter2", "none")
    errors = []
    for attr, value in (("color_effect", "sepia"), ("brightness", 40), ("contrast", 150),
                        ("saturation", 20), ("hue", 90), ("mirror", True), ("mirror", False),
                        ("chroma_enabled", True), ("chroma_enabled", False),
                        ("frame_style", "neon"), ("frame_style", "circle"),
                        ("frame_style", "heart"), ("frame_style", "polaroid"),
                        ("frame_style", "tv"), ("frame_style", "film"),
                        ("frame_style", "vignette"), ("frame_style", "none"),
                        ("color_effect", "none"), ("brightness", 0), ("contrast", 100),
                        ("saturation", 100), ("hue", 0)):
        W._set_effect(attr, value)
        pump(0.2)
        if W.engine is None or W.engine.report.error:
            errors.append(f"{attr}={value}: {W.engine.report.error if W.engine else 'engine gone'}")
            break
    R.check("colour, mirror, chroma and frame styles apply live", not errors, "; ".join(errors))
    alive("after effects")


def step_overlays() -> None:
    if W.engine is None:
        R.skip("overlays", "no canvas")
        return
    added = []

    def attempt(label: str, make) -> None:
        before = COUNTS["start_engine"]
        try:
            layer = make()
        except Exception as exc:  # noqa: BLE001
            R.check(f"{label}", False, f"{type(exc).__name__}: {exc}")
            return
        if layer is not None:
            added.append(layer.id)
        pump(1.2)
        rebuilt = COUNTS["start_engine"] - before
        ok = W.engine is not None and not W.engine.report.error
        R.check(f"{label}: added",
                ok, (W.engine.report.error if W.engine else "engine gone")
                or (f"canvas rebuilt {rebuilt}x" if rebuilt else "live, no rebuild"))

    attempt("text caption", lambda: W.add_layer("text"))
    attempt("ticker", lambda: W.add_layer("ticker", source="Live check ticker"))
    attempt("lower third", lambda: W.add_layer("lower_third", source="Name|Title"))
    keys = list(particles.PRESETS)[:2]
    for key in keys:
        W._set_animation(key)
        pump(1.0)
        R.check(f"animation {key}", W.engine is not None and not W.engine.report.error,
                W.engine.report.error if W.engine else "engine gone")
    W._set_animation(keys[-1])          # clicking the active one removes it
    pump(0.8)
    R.check("clicking the active animation removes it",
            not W._animation_layers(), f"{len(W._animation_layers())} left")
    picture = make_pngs()

    def image_layer():
        layer = W.add_layer("image", source=str(picture))
        if layer is not None:
            layer.crop = (0.1, 0.0, 0.1, 0.0)
            layer.effects.mirror = True
            W.restart_engine()
        return layer

    attempt("image with crop + mirror", image_layer)
    clip = WORK / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=2:size=320x180:rate=30", "-pix_fmt", "yuv420p", str(clip)],
                   check=False)
    if clip.exists():
        attempt("video file layer", lambda: W.add_layer("video", source=str(clip)))
        pump(4)
        loops = [e.loops for e in W._source_pool.entries() if e.spec.kind == "video"]
        R.check("video layer loops without going black", bool(loops) and max(loops) >= 1,
                f"loops={loops}", warn=True)
    from PIL import Image

    gif = WORK / "spin.gif"
    frames = [Image.new("RGBA", (64, 64), (i * 40 % 255, 80, 200, 255)) for i in range(6)]
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=100, loop=0)
    attempt("animated gif layer", lambda: W.add_layer("gif", source=str(gif)))
    if added:
        _, took = timed(W.remove_layers, added)
        R.check("removing the overlays", took < 2.5 and W.engine is not None
                and not W.engine.report.error, f"{took:.1f}s")
    alive("after overlays")


def step_release_camera() -> None:
    cam = CTX.get("cam")
    if cam is None or W.engine is None:
        R.skip("releasing the webcam", "no camera layer")
        return
    device = config.camera_device()
    before = device_fds(device)
    R.info("camera descriptors held", str(before))
    saved = pool_module.RETAIN_GRACE_S
    pool_module.RETAIN_GRACE_S = 2.0
    try:
        W.remove_layers([cam.id])
        pump(1.0)
        R.check("a removed camera is kept for a quick re-add",
                device_fds(device) > 0, f"{device_fds(device)} descriptors right after removal",
                warn=True)
        pump(6.0)
        held = device_fds(device)
        R.check("a removed camera is released after the retention window", held == 0,
                f"{held} descriptors still open on {device}")
    finally:
        pool_module.RETAIN_GRACE_S = saved
    layer = W.add_layer("camera", source=device)
    CTX["cam"] = layer
    got = pump(12, until=lambda: layer is not None and camera_health(layer.id).get("frames", 0) > 3)
    R.check("camera comes back after being released", got,
            f"health={camera_health(layer.id) if layer else None}")


def step_playlist() -> None:
    W.switch_scene(1)
    pump(1.0)
    first = W.add_layer("test")
    second = W.add_layer("test")
    if first is None or second is None:
        R.skip("playlist", "could not add test layers")
        return
    for layer in (first, second):
        layer.box = (0.0, 0.0, 1.0, 1.0)
    second.source = "smpte"
    first.visible, second.visible = True, False
    W.playlist_ids = [first.id, second.id]
    W.playlist_index = -1
    W.playlist_interval.setValue(1.0)
    W.playlist_enabled.setChecked(True)
    W._store_playlist_state()
    W.restart_engine()
    changes = []
    last = None
    start = W._started_at
    end = time.monotonic() + 6.5
    while time.monotonic() < end:
        APP.processEvents()
        shown = (first.visible, second.visible)
        if shown != last:
            changes.append((round(time.monotonic() - start, 2), shown))
            last = shown
        time.sleep(0.02)
    switches = [t for t, _ in changes[1:]]
    R.info("playlist visibility changes (s since start)", str(changes))
    R.check("playlist advances", len(switches) >= 3, f"{len(switches)} switches in 6.5 s")
    R.check("first playlist item shows for one interval, not two",
            bool(switches) and switches[0] < 1.7,
            f"first switch at {switches[0] if switches else None}s (interval 1.0s)")
    W.playlist_ids = []
    W.playlist_index = -1
    W._store_playlist_state()
    W.switch_scene(0)
    pump(2)


def _recovery(label: str, with_screen: bool, prefix: str = "Pipeline error") -> None:
    cam = CTX.get("cam")
    if cam is None or W.engine is None:
        R.skip(label, "no camera layer")
        return
    device = cam.source
    COUNTS["start_engine"] = 0
    if with_screen:
        W._has_screen_layer = lambda: True
    try:
        W._handle_pipeline_error(f"{prefix}: simulated failure of {device}")
        settled = pump(25, until=lambda: (not W._lost_cameras and W.engine is not None
                                          and camera_health(cam.id).get("frames", 0) > 3
                                          and cam.id not in W.engine.report.stalled_sources))
        pump(4)
    finally:
        if with_screen and "_has_screen_layer" in W.__dict__:
            del W.__dict__["_has_screen_layer"]
    rebuilds = COUNTS["start_engine"]
    R.check(f"{label}: camera recovers", settled and not W._lost_cameras,
            f"lost={dict(W._lost_cameras)} health={camera_health(cam.id)}")
    R.check(f"{label}: no rebuild loop", rebuilds <= 3,
            f"{rebuilds} canvas rebuilds in about 30 s")
    R.check(f"{label}: engine error-free afterwards",
            W.engine is not None and not W.engine.report.error,
            W.engine.report.error if W.engine else "engine gone")
    W._forget_lost_cameras()


def step_recovery_plain() -> None:
    _recovery("camera loss, single source", with_screen=False)


def step_recovery_screen() -> None:
    _recovery("camera loss beside a screen share (reattach path)", with_screen=True)


def step_screen() -> None:
    if ARGS.no_screen:
        R.skip("portal screen share", "--no-screen")
        return
    token = config.load_screen_restore_token()
    original = W._open_screen_cast

    def with_token(options):
        merged = dict(options)
        if token and "restore_token" not in merged:
            merged["restore_token"] = token
        merged.setdefault("timeout", 60.0)
        return original(merged)

    W._open_screen_cast = with_token
    print(">>> if a screen picker appears, choose a screen or window (60 s) <<<", flush=True)
    try:
        layer, took = timed(W._add_screen_layer)
    finally:
        W._open_screen_cast = original
    R.check("screen layer created", layer is not None,
            f"{took:.1f}s; {W.statusBar().currentMessage()}")
    if layer is None:
        return
    CTX["screen"] = layer
    got = pump(20, until=lambda: camera_health(layer.id).get("frames", 0) > 3)
    R.check("screen frames arrive", got, f"health={camera_health(layer.id)}")
    pump(2.0)
    R.check("engine error-free with a screen share", engine_ok(),
            W.engine.report.error if W.engine else "engine gone")
    cam = CTX.get("cam")
    if cam is not None:
        W.apply_layout("pip")
        pump(1.5)
    if CTX.get("vcam_ok") and W.vcam_on:
        frames, tail = consumer_frames(2.5)
        R.check("the virtual camera carries the screen share", frames >= 15, f"{frames} frames")
    segments, _ = _record(4.0)
    _check_recording(segments, "recording a screen share", 4.0)
    W.remove_layers([layer.id])
    pump(1.0)
    again, took = timed(W._add_screen_layer)
    R.check("a removed share comes back without another picker",
            again is not None and took < 4.0, f"{took:.1f}s")
    pump(3.0)
    if again is not None:
        CTX["screen"] = again
        R.check("the re-added share delivers frames",
                camera_health(again.id).get("frames", 0) > 3, f"health={camera_health(again.id)}")
    if cam is not None and W.engine is not None:
        # the real reconnect paths, with a real screen layer in the scene
        _recovery("camera loss beside a real screen share (rebuild path)", False)
        _recovery("camera loss beside a real screen share (drop and reattach path)", False,
                  prefix="Camera source error")


def step_screen_cleanup() -> None:
    layer = CTX.pop("screen", None)
    if layer is not None and W.scene.layer(layer.id) is not None:
        W.remove_layers([layer.id])
        pump(1.0)


def step_steady() -> None:
    if W.engine is None:
        R.skip("steady state", "no canvas")
        return
    W.toggle_record()
    process = psutil.Process()
    rss0 = process.memory_info().rss / 2 ** 20
    samples = []
    start = time.monotonic()
    while time.monotonic() - start < 20:
        pump(1.0)
        if W.engine is None:
            break
        samples.append((W.engine.report.instant_fps, W.engine.report.stalled_sources[:]))
    _, took = timed(W.toggle_record)
    rss1 = process.memory_info().rss / 2 ** 20
    rate = W.project.output.fps
    steady = [fps for fps, _ in samples[5:]] or [0]
    R.check("canvas keeps its rate for 20 s (camera + vcam + recording)",
            min(steady) >= 0.8 * rate,
            f"min {min(steady):.1f}, avg {sum(steady) / len(steady):.1f} of {rate} fps", warn=True)
    R.check("no source stalls during the run", all(not s for _, s in samples),
            str([s for _, s in samples if s][:2]))
    R.check("memory growth over 20 s", rss1 - rss0 < 80, f"{rss0:.0f} → {rss1:.0f} MiB", warn=True)
    _check_recording(list(W.recording_segments), "steady-state recording", 20.0)


def step_shutdown() -> None:
    _, took = timed(W.close)
    pump(1.0)
    R.check("window closes promptly", took < 6.0, f"{took:.1f}s")
    R.check("engine is gone after close", W.engine is None)
    device = config.camera_device()
    R.check("webcam released at exit", device_fds(device) == 0,
            f"{device_fds(device)} descriptors still open")
    stray = [p.info["name"] for p in psutil.process_iter(["name"])
             if p.info["name"] == "ffmpeg"]
    R.check("no stray helper processes", not stray, str(stray))
    for path in sorted((WORK / "rec").glob("*.mp4")) if (WORK / "rec").exists() else []:
        info = probe(str(path))
        R.check(f"recording {path.name} is a valid file", "error" not in info and
                (info.get("video") or {}).get("packets", 0) > 0,
                str(info.get("error", "")))


# ====================================================== full-coverage steps
import contextlib  # noqa: E402
import socket  # noqa: E402

import numpy as np  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QKeySequence, QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMenu, QTabWidget  # noqa: E402

PORTRAIT = ROOT / "tests" / "data" / "armstrong.jpg"


def picture():
    """The preview as a small RGB array (for 'did the picture change?')."""
    image = W.preview.image
    if image is None or image.isNull():
        return None
    small = image.scaled(96, 54, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation
                         ).convertToFormat(QImage.Format.Format_RGBA8888)
    raw = np.frombuffer(bytes(small.constBits()), dtype=np.uint8)
    rows = raw.reshape(small.height(), small.bytesPerLine())
    pixels = rows[:, : small.width() * 4].reshape(small.height(), small.width(), 4)
    return pixels[:, :, :3].astype(np.int16)


def change(first, second) -> float:
    if first is None or second is None:
        return -1.0
    return float(np.abs(first - second).mean())


def go_scene(index: int) -> None:
    W.switch_scene(index)
    pump(1.8)


def need_engine() -> bool:
    if W.engine is None:
        W.start_engine()
        pump(1.5)
    return W.engine is not None


def engine_ok() -> bool:
    return W.engine is not None and not W.engine.report.error


def wait_filters(layer_id: str, timeout: float = 8.0) -> bool:
    chain = W.engine._layer_chains.get(layer_id) if W.engine else None
    done = True
    for key in ("slot", "slot2"):
        slot = chain.effects.get(key) if chain else None
        if slot is not None:
            done = slot.idle.wait(timeout) and done
    return done


def pad_property(layer_id: str, name: str):
    pad = W.engine._layer_pads.get(layer_id) if W.engine else None
    return pad.get_property(name) if pad is not None else None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def send_mouse(widget, kind, pos, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    left = Qt.MouseButton.LeftButton
    none = Qt.MouseButton.NoButton
    if kind == QEvent.Type.MouseMove:
        event = QMouseEvent(kind, pos, pos, none, left, modifiers)
    elif kind == QEvent.Type.MouseButtonPress:
        event = QMouseEvent(kind, pos, pos, left, left, modifiers)
    else:
        event = QMouseEvent(kind, pos, pos, left, none, modifiers)
    QApplication.sendEvent(widget, event)


def drag(widget, start, end, steps: int = 10,
         modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    send_mouse(widget, QEvent.Type.MouseButtonPress, start, modifiers)
    for index in range(1, steps + 1):
        point = QPointF(start.x() + (end.x() - start.x()) * index / steps,
                        start.y() + (end.y() - start.y()) * index / steps)
        send_mouse(widget, QEvent.Type.MouseMove, point, modifiers)
        pump(0.01)
    send_mouse(widget, QEvent.Type.MouseButtonRelease, end, modifiers)
    pump(0.1)


class FakeMenu(QMenu):
    """Stands in for QMenu inside the app: answers exec() itself and never blocks."""

    chooser = None

    def exec(self, *args, **kwargs):  # noqa: A003
        chooser = FakeMenu.chooser
        return chooser.pick(self) if chooser is not None else None


class MenuChoice:
    """Answer the next menu the app opens by picking the action with this data/label."""

    def __init__(self, wanted: str) -> None:
        self.wanted = wanted
        self.labels: list[str] = []
        self.found = False

    def _walk(self, menu):
        for action in menu.actions():
            sub = action.menu()
            if sub is not None:
                hit = self._walk(sub)
                if hit is not None:
                    return hit
                continue
            self.labels.append(action.text())
            if str(action.data() or "") == self.wanted or action.text() == self.wanted:
                return action
        return None

    def pick(self, menu):
        action = self._walk(menu)
        self.found = action is not None
        return action

    def __enter__(self):
        FakeMenu.chooser = self
        return self

    def __exit__(self, *exc) -> None:
        FakeMenu.chooser = None


@contextlib.contextmanager
def patched(owner, name, value):
    original = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, original)


def show_tab(title: str) -> bool:
    for tabs in W.findChildren(QTabWidget):
        for index in range(tabs.count()):
            if tabs.tabText(index) == title:
                tabs.setCurrentIndex(index)
                return True
    return False


def volume_of(path: str, seconds: float = 4.0) -> dict:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-t", str(seconds), "-i", path, "-vn", "-af",
         "volumedetect", "-f", "null", "-"], capture_output=True, text=True, check=False)
    mean = re.search(r"mean_volume:\s*(-?[\d.]+)", out.stderr)
    peak = re.search(r"max_volume:\s*(-?[\d.]+)", out.stderr)
    return {"mean": float(mean.group(1)) if mean else None,
            "max": float(peak.group(1)) if peak else None}


def open_settings(index: int = 0) -> bool:
    """The default window keeps these controls in a Settings dialog; classic has tabs."""
    if hasattr(W, "_open_settings"):
        W._open_settings(index)
        return True
    return False


def close_settings() -> None:
    dialog = getattr(W, "_settings", None)
    if dialog is not None:
        dialog.hide()


def only_in_default_ui(label: str, *attrs: str) -> bool:
    missing = [a for a in attrs if not hasattr(W, a)]
    if missing:
        R.skip(label, f"controls only in the default window ({missing[0]} missing here)")
        return True
    return False


def record_for(seconds: float) -> list[str]:
    segments, _ = _record(seconds)
    return segments


# ------------------------------------------------------------------ scenes
def step_scene_management() -> None:
    base = len(W.project.scenes)
    has_cards = hasattr(W, "_scene_context_menu")
    W.add_scene()
    pump(1.5)
    R.check("add a scene", len(W.project.scenes) == base + 1)
    scene = W.project.scenes[-1]
    with patched(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Renamed scene", True))):
        W.rename_scene(scene.id)
    R.check("rename a scene", scene.name == "Renamed scene", scene.name)
    if has_cards:
        W._refresh_scene_cards()
        with MenuChoice("Remove") as choice:
            W._scene_context_menu(scene.id, QPoint(0, 0))
        pump(1.5)
        R.check("scene card menu removes a scene",
                choice.found and len(W.project.scenes) == base, f"{len(W.project.scenes)} scenes")
    else:
        W.remove_scene(scene.id)
        pump(1.5)
        R.check("remove a scene", len(W.project.scenes) == base, f"{len(W.project.scenes)} scenes")
    R.check("a valid scene stays active",
            W.project.active_scene in {s.id for s in W.project.scenes})
    alive("after scene management")


def step_slots() -> None:
    if only_in_default_ui("layout slots", "stage_add_buttons"):
        return
    W.add_scene()
    pump(1.5)
    W.apply_layout("split-2")
    pump(0.5)
    R.check("a + appears for each empty slot", len(W.stage_add_buttons) == 2,
            f"{len(W.stage_add_buttons)} buttons")
    preset = layouts.layout_preset("split-2")
    with MenuChoice("add:test") as choice:
        W._slot_menu(0, W.stage_add_buttons[0])
    pump(1.5)
    tests = [l for l in W.scene.layers if l.kind == "test"]
    R.check("the + menu adds a source into its slot",
            choice.found and len(tests) == 1 and tuple(tests[0].box) == tuple(preset.boxes[0]),
            f"layers={[(l.kind, l.box) for l in W.scene.layers]}")
    R.check("the filled slot loses its +", len(W.stage_add_buttons) == 1,
            f"{len(W.stage_add_buttons)} buttons")
    with MenuChoice("add:test") as choice:
        W._slot_menu(1, W.stage_add_buttons[0])
    pump(1.5)
    tests = [l for l in W.scene.layers if l.kind == "test"]
    R.check("second slot filled", len(tests) == 2 and not W.stage_add_buttons)
    if len(tests) == 2:
        first, second = tests
        before = (tuple(first.box), tuple(second.box))
        W._assign_slot(1, first)
        pump(0.8)
        R.check("assigning a source to a slot swaps the occupants",
                tuple(first.box) == before[1] and tuple(second.box) == before[0])
        R.check("the live canvas followed the swap",
                pad_property(first.id, "xpos") == int(round(first.box[0] * W.project.output.canvas_w)))
    alive("after slots")


# ------------------------------------------------------------ adjust effects
def step_adjust_effects() -> None:
    go_scene(0)
    layer = CTX.get("cam") or CTX.get("test_layer")
    if layer is None or W.engine is None:
        R.skip("effect controls", "no source layer")
        return
    W.preview.select_layer(layer.id)
    pump(0.4)
    problems = []
    for key, slider in W._effect_sliders.items():
        low, high = slider.minimum(), slider.maximum()
        for value in (low, high, (low + high) // 2):
            slider.setValue(value)
            pump(0.15)
            if not engine_ok():
                problems.append(f"{key}={value}")
                break
    R.check(f"all {len(W._effect_sliders)} effect sliders work at min, max and middle",
            not problems, "; ".join(problems))
    bad = []
    for index in range(W.effect_preset.count()):
        W.effect_preset.setCurrentIndex(index)
        pump(0.3)
        if not engine_ok():
            bad.append(W.effect_preset.itemText(index))
    R.check(f"all {W.effect_preset.count()} look presets apply", not bad, ", ".join(bad))
    W._reset_effects()
    pump(0.3)
    W._set_effect("color_effect", "sepia")
    W._copy_effects()
    other = next((l for l in W.scene.layers if l.id != layer.id
                  and l.kind in W._CAPTURE_KINDS), None)
    if other is not None:
        W.preview.select_layer(other.id)
        W._paste_effects()
        R.check("copy and paste effects between layers",
                other.effects.color_effect == "sepia", other.effects.color_effect)
        W._clear_effects()
        R.check("clear removes every effect", other.effects.color_effect == "none")
    W.preview.select_layer(layer.id)
    W.effects_all.setChecked(True)
    show_tab("Effects")
    show_tab("Browse")
    W.effect_category.setCurrentIndex(W.effect_category.findData("Distort"))
    pump(0.3)
    tile = next((r for r in range(W.effect_grid.count())
                 if not W._effect_entries[r]["label"].startswith("No ")), None)
    captured = [l for l in W.scene.layers if l.visible and l.kind in W._CAPTURE_KINDS]
    if tile is not None:
        wanted = W._effect_entries[tile]["apply"]["filter"]
        W._effect_item_clicked(W.effect_grid.item(tile))
        pump(0.8)
        R.check("'apply to all sources' reaches every source (tile click)",
                bool(captured) and all(l.effects.filter == wanted for l in captured),
                f"{[(l.name, l.effects.filter) for l in captured]}")
        W._effect_item_clicked(W.effect_grid.item(0))      # "No filter" on all
        pump(0.5)
    W.effects_all.setChecked(False)
    W._reset_effects()
    alive("after effect controls")


# ------------------------------------------------------------- every effect tile
def step_catalog() -> None:
    quick = ARGS.quick
    W.add_scene()
    pump(1.5)
    layer = W.add_layer("image", source=str(PORTRAIT), box=(0.0, 0.0, 1.0, 1.0))
    if layer is None:
        R.check("portrait test picture added", False, str(PORTRAIT))
        return
    R.check("add_layer honours the box it is given (used by the layout slots)",
            tuple(layer.box) == (0.0, 0.0, 1.0, 1.0), f"box={layer.box}")
    layer.box = (0.0, 0.0, 1.0, 1.0)        # work around it so the catalogue can run
    W.restart_engine() if W.engine is not None else None
    need_engine()
    pump(2.0)
    W.preview.select_layer(layer.id)
    show_tab("Effects")
    show_tab("Browse")
    W._clear_effects()
    pump(0.8)
    clean = picture()
    R.check("portrait renders", clean is not None and image_mean(W.preview.image) > 5,
            f"mean level {image_mean(W.preview.image):.1f}")
    thumbs_missing: list[str] = []
    totals = {"tiles": 0}
    for category in W.EFFECT_CATEGORIES:
        W.effect_category.setCurrentIndex(W.effect_category.findData(category))
        pump(0.3)
        if W.effect_grid.isVisible():
            pump(25, until=lambda: not W._thumb_queue)
        grid = W.effect_grid
        entries = list(W._effect_entries)
        if W.effect_grid.isVisible():
            thumbs_missing += [f"{category}/{entries[r]['label']}" for r in range(grid.count())
                               if grid.item(r).icon().isNull()]
        rows = list(range(grid.count()))
        if quick:
            rows = rows[::7]
        broken: list[str] = []
        silent: list[str] = []
        tested = 0
        for row in rows:
            entry = entries[row]
            label, kind = entry["label"], entry["kind"]
            if label.startswith("No ") or label == "Original":
                continue
            tested += 1
            totals["tiles"] += 1
            W._effect_item_clicked(grid.item(row))
            done = wait_filters(layer.id)
            pump(0.35 if category == "Emoji rain" else 0.55)
            if not engine_ok():
                broken.append(f"{label}: "
                              f"{W.engine.report.error if W.engine else 'engine gone'}")
                need_engine()
                continue
            if not done:
                broken.append(f"{label}: filter swap never finished")
            if kind == "particles":
                first = picture()
                pump(0.5)
                if change(first, picture()) < 0.05:
                    silent.append(f"{label} (not moving)")
                if not W._animation_layers():
                    broken.append(f"{label}: no animation layer was added")
                W._effect_item_clicked(grid.item(0))      # "No animation"
                pump(0.25)
                continue
            after = picture()
            chain = W.engine._layer_chains.get(layer.id) if W.engine else None
            slot = chain.effects.get("slot") if chain else None
            runner = getattr(slot, "runner", None)
            if runner is not None and runner.errors:
                broken.append(f"{label}: its processor raised {runner.errors}x")
            if kind in ("filter", "frame"):
                W._effect_item_clicked(grid.item(0))      # "No filter" / "No frame"
            else:
                W._clear_effects()
            wait_filters(layer.id)
            pump(0.45)
            moved = change(after, picture())
            if moved < 0.25:
                silent.append(f"{label} ({moved:.2f})")
        R.check(f"{category}: {tested} tiles apply without errors", not broken,
                "; ".join(broken[:4]))
        R.check(f"{category}: every tile visibly changes the picture", not silent,
                "; ".join(silent[:8]), warn=True)
    R.check("every tile has a preview thumbnail", not thumbs_missing,
            f"{len(thumbs_missing)} missing: {thumbs_missing[:6]}", warn=True)
    R.info("effect tiles exercised", str(totals["tiles"]))
    W._clear_effects()
    alive("after the effect catalog")


# --------------------------------------------------------------- backdrops
def step_backdrops() -> None:
    from anycam.sources import ANIMATION_PRESETS

    if not hasattr(W, "inset_on_backdrop"):
        R.check("the window has backdrop controls (README lists 10 static and animated backdrops)",
                False, "this window never builds the Backdrops tab, and calling "
                "_apply_background() raises AttributeError: inset_on_backdrop. "
                "Run --ui classic to test them.", warn=True)
        return

    W.add_scene()
    pump(1.2)
    base = W.add_layer("test", box=(0.0, 0.0, 1.0, 1.0))
    need_engine()
    pump(1.5)
    previous = picture()
    failures = []
    for name in overlay.BACKGROUND_NAMES:
        W._apply_background(name)
        pump(1.0)
        backdrops = W._active_backdrops()
        shot = picture()
        if len(backdrops) != 1 or not engine_ok() or change(previous, shot) < 0.2:
            failures.append(f"{name}: backdrops={len(backdrops)} change={change(previous, shot):.2f}")
        elif base is not None and backdrops[0].z >= base.z:
            failures.append(f"{name}: not behind the source (z {backdrops[0].z} >= {base.z})")
        previous = shot
    R.check(f"{len(overlay.BACKGROUND_NAMES)} static backdrops apply behind the source",
            not failures, "; ".join(failures[:4]))
    W._clear_backdrop()
    pump(1.0)
    R.check("clearing the backdrop removes it and restores the source",
            not W._active_backdrops() and base is not None
            and all(abs(a - b) < 0.01 for a, b in zip(base.box, (0.0, 0.0, 1.0, 1.0))),
            f"box={base.box if base else None}")
    if base is not None:
        W.remove_layers([base.id])      # let the backdrop fill the picture so motion is visible
        pump(0.8)
    failures = []
    for key in ANIMATION_PRESETS:
        W._apply_animation(key)
        pump(1.0)
        first = picture()
        pump(0.7)
        moved = change(first, picture())
        if len(W._active_backdrops()) != 1 or not engine_ok() or moved < 0.1:
            failures.append(f"{key}: backdrops={len(W._active_backdrops())} movement={moved:.2f}")
    R.check(f"{len(ANIMATION_PRESETS)} animated backdrops move", not failures,
            "; ".join(failures[:4]))
    W._clear_backdrop()
    pump(0.8)
    alive("after backdrops")


# -------------------------------------------------------------------- drawing
def step_draw() -> None:
    from anycam import animated

    W.add_scene()
    pump(1.2)
    W.add_layer("test", box=(0.0, 0.0, 1.0, 1.0))
    need_engine()
    pump(1.5)
    W.toggle_draw(True)
    R.check("draw mode is on", W.preview.edit_mode == "draw" and not W.draw_bar.isHidden())
    surface = W.preview
    canvas = surface.canvas_rect()
    before = picture()
    for index, tool in enumerate(("pen", "marker", "arrow", "box")):
        W._draw_tool(tool)
        y = canvas.y() + canvas.height() * (0.2 + 0.15 * index)
        drag(surface, QPointF(canvas.x() + canvas.width() * 0.2, y),
             QPointF(canvas.x() + canvas.width() * 0.6, y + 30))
        layer = W._drawing_layer()
        strokes = animated.parse_strokes(layer.source) if layer else []
        R.check(f"{tool} stroke is recorded", len(strokes) == index + 1,
                f"{len(strokes)} strokes")
    W._draw_colour("#5eead0")
    W.draw_width_slider.setValue(12)
    pump(0.8)
    R.check("strokes show up in the picture", change(before, picture()) > 0.2,
            f"change {change(before, picture()):.2f}")
    W._draw_undo()
    layer = W._drawing_layer()
    R.check("draw undo removes the last stroke",
            layer is not None and len(animated.parse_strokes(layer.source)) == 3)
    W._draw_clear()
    R.check("draw clear removes them all",
            layer is not None and not animated.parse_strokes(layer.source))
    W.toggle_draw(False)
    R.check("done returns to move mode", W.preview.edit_mode == "move" and W.draw_bar.isHidden())
    alive("after drawing")


# ---------------------------------------------------------- mouse and fields
def step_editing() -> None:
    from anycam.gui import OverlayDialog

    W.add_scene()
    pump(1.5)
    a = W.add_layer("test", box=(0.05, 0.10, 0.40, 0.40))
    a.source = "smpte"
    b = W.add_layer("test", box=(0.55, 0.10, 0.40, 0.40))
    open_settings(0)
    pump(0.8)
    need_engine()
    pump(1.5)
    surface = W.preview
    canvas = surface.canvas_rect()
    cw, ch = canvas.width(), canvas.height()
    canvas_w = W.project.output.canvas_w
    W.snap_check.setChecked(False)
    surface.select_layer(a.id)
    pump(0.2)

    # ---- move
    x0, y0 = a.box[0], a.box[1]
    centre = surface.box_rect(a).center()
    drag(surface, centre, QPointF(centre.x() + 0.10 * cw, centre.y() + 0.05 * ch))
    dx, dy = a.box[0] - x0, a.box[1] - y0
    R.check("dragging a layer moves it", abs(dx - 0.10) < 0.04 and abs(dy - 0.05) < 0.04,
            f"moved ({dx:.3f}, {dy:.3f}), wanted (0.100, 0.050)")
    R.check("the live canvas follows the drag",
            pad_property(a.id, "xpos") == int(round(a.box[0] * canvas_w)),
            f"pad {pad_property(a.id, 'xpos')} vs box {a.box[0] * canvas_w:.0f}")
    moved = tuple(a.box)
    W.undo()
    pump(1.0)
    R.check("undo puts it back", abs(a.box[0] - x0) < 1e-6 and abs(a.box[1] - y0) < 1e-6,
            f"{a.box}")
    W.redo()
    pump(1.0)
    R.check("redo moves it again", abs(a.box[0] - moved[0]) < 1e-6, f"{a.box}")

    # ---- resize by the corner handle
    surface.select_layer(a.id)
    rect = surface.box_rect(a)
    w0, h0 = a.box[2], a.box[3]
    drag(surface, rect.bottomRight(),
         QPointF(rect.right() + 0.08 * cw, rect.bottom() + 0.06 * ch))
    R.check("dragging the corner handle resizes", a.box[2] > w0 + 0.03 and a.box[3] > h0 + 0.03,
            f"{w0:.2f}x{h0:.2f} -> {a.box[2]:.2f}x{a.box[3]:.2f}")
    R.check("the live canvas follows the resize",
            abs(int(pad_property(a.id, "width")) - a.box[2] * canvas_w) <= 2)

    # ---- snapping
    surface.select_layer(b.id)
    W.snap_check.setChecked(True)
    W.snap_threshold.setValue(14)
    rect = surface.box_rect(b)
    drag(surface, rect.center(),
         QPointF(rect.center().x() + (canvas.x() + 0.005 * cw - rect.left()), rect.center().y()))
    R.check("snap pulls a layer to the canvas edge", abs(b.box[0]) < 1e-6, f"x={b.box[0]:.4f}")
    W.snap_check.setChecked(False)
    rect = surface.box_rect(b)
    drag(surface, rect.center(), QPointF(rect.center().x() + 0.2 * cw, rect.center().y()))
    rect = surface.box_rect(b)
    drag(surface, rect.center(),
         QPointF(rect.center().x() + (canvas.x() + 0.005 * cw - rect.left()), rect.center().y()))
    R.check("no snapping when snap is off", b.box[0] > 0.002, f"x={b.box[0]:.4f}")

    # ---- keyboard nudge
    surface.select_layer(b.id)
    surface.setFocus()
    x1, y1 = b.box[0], b.box[1]
    for _ in range(5):
        QTest.keyClick(surface, Qt.Key.Key_Right)
    for _ in range(2):
        QTest.keyClick(surface, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    pump(0.7)
    R.check("arrow keys nudge a layer",
            abs((b.box[0] - x1) - 0.010) < 0.004 and abs((b.box[1] - y1) - 0.040) < 0.006,
            f"moved ({b.box[0] - x1:.3f}, {b.box[1] - y1:.3f}), wanted (0.010, 0.040)")

    # ---- multi-select + group move
    surface.select_layer(a.id)
    send_mouse(surface, QEvent.Type.MouseButtonPress, surface.box_rect(b).center(),
               Qt.KeyboardModifier.ControlModifier)
    send_mouse(surface, QEvent.Type.MouseButtonRelease, surface.box_rect(b).center(),
               Qt.KeyboardModifier.ControlModifier)
    R.check("ctrl-click adds to the selection", set(surface.selected_ids) == {a.id, b.id},
            str(surface.selected_ids))
    ax, bx = a.box[0], b.box[0]
    centre = surface.box_rect(a).center()
    drag(surface, centre, QPointF(centre.x() + 0.03 * cw, centre.y()))
    R.check("a group drag moves every selected layer by the same amount",
            abs((a.box[0] - ax) - (b.box[0] - bx)) < 0.01 and a.box[0] > ax + 0.01,
            f"a {a.box[0] - ax:+.3f}, b {b.box[0] - bx:+.3f}")
    surface.select_layer(a.id)

    # ---- crop mode
    W._set_edit_mode("crop")
    pump(1.8)
    rect = surface.crop_rect(a)
    left_mid = QPointF(rect.left(), rect.center().y())
    drag(surface, left_mid, QPointF(left_mid.x() + 0.2 * rect.width(), left_mid.y()))
    pump(1.8)
    R.check("a crop handle trims the layer", a.crop[0] > 0.1, f"crop={a.crop}")
    R.check("the canvas rebuilt with the crop and kept running", engine_ok())
    W._reset_crop()
    pump(1.8)
    R.check("reset crop clears it", not any(a.crop), f"crop={a.crop}")
    W._set_edit_mode("move")
    pump(1.8)

    # ---- numeric fields, opacity
    surface.select_layer(a.id)
    pump(0.3)
    W.box_spins["x"].setValue(0.2)
    R.check("the X field moves the layer",
            abs(a.box[0] - 0.2) < 0.01 and
            pad_property(a.id, "xpos") == int(round(a.box[0] * canvas_w)),
            f"x={a.box[0]:.3f}")
    W.crop_spins["l"].setValue(0.15)
    pump(1.2)
    R.check("the crop field crops", abs(a.crop[0] - 0.15) < 0.01, f"crop={a.crop}")
    W.crop_spins["l"].setValue(0.0)
    pump(1.2)
    W.opacity_slider.setValue(40)
    pump(0.3)
    R.check("the opacity slider sets alpha on the live canvas",
            abs(a.alpha - 0.4) < 0.02 and abs(float(pad_property(a.id, "alpha")) - 0.4) < 0.02,
            f"alpha={a.alpha}, pad={pad_property(a.id, 'alpha')}")
    W.opacity_slider.setValue(100)

    # ---- align + stacking
    surface.select_layer(a.id)
    checks = (("left", lambda l: abs(l.box[0]) < 1e-6),
              ("right", lambda l: abs(l.box[0] + l.box[2] - 1) < 1e-6),
              ("top", lambda l: abs(l.box[1]) < 1e-6),
              ("bottom", lambda l: abs(l.box[1] + l.box[3] - 1) < 1e-6),
              ("hcenter", lambda l: abs(l.box[0] - (1 - l.box[2]) / 2) < 1e-6),
              ("vcenter", lambda l: abs(l.box[1] - (1 - l.box[3]) / 2) < 1e-6))
    failed = []
    for action, ok in checks:
        W._align_layers(action)
        pump(0.1)
        if not ok(a):
            failed.append(action)
    R.check("all six align actions", not failed, ", ".join(failed))
    for action in ("raise", "lower", "top", "bottom"):
        W._move_layer(action)
        pump(0.2)
    by_z = [l.id for l in sorted(W.scene.layers, key=lambda l: l.z)]
    by_pad = sorted((l.id for l in W.scene.layers if l.id in W.engine._layer_pads),
                    key=lambda i: W.engine._layer_pads[i].get_property("zorder"))
    R.check("stacking order matches the live canvas", by_z == by_pad, f"{by_z} vs {by_pad}")

    # ---- visibility checkbox in the layer list
    row = next((r for r in range(W.layer_list.count())
                if W.layer_list.item(r).data(Qt.ItemDataRole.UserRole) == a.id), None)
    if row is not None:
        item = W.layer_list.item(row)
        item.setCheckState(Qt.CheckState.Unchecked)
        pump(0.4)
        hidden = (not a.visible) and float(pad_property(a.id, "alpha")) == 0.0
        item.setCheckState(Qt.CheckState.Checked)
        pump(0.4)
        shown = a.visible and float(pad_property(a.id, "alpha")) > 0.5
        R.check("the visibility checkbox hides and shows a layer", hidden and shown,
                f"hidden={hidden} shown={shown}")
    else:
        R.check("the layer list lists the layer", False)

    # ---- duplicate, remove, undo the removal
    count = len(W.scene.layers)
    surface.select_layer(a.id)
    W.duplicate_layer()
    pump(1.8)
    R.check("duplicate adds a copy", len(W.scene.layers) == count + 1)
    copy_layer = max(W.scene.layers, key=lambda l: l.z)
    W.remove_layers([copy_layer.id])
    pump(0.8)
    R.check("remove deletes it", len(W.scene.layers) == count)
    W.undo()
    pump(1.8)
    R.check("undo brings a removed layer back", len(W.scene.layers) == count + 1)
    W.remove_layers([max(W.scene.layers, key=lambda l: l.z).id])
    pump(0.8)

    # ---- right-click menu
    menu = W._preview_menu(a.id)
    top = [action.text() for action in menu.actions()]
    R.check("context menu has Input, Output, Duplicate and Remove",
            all(any(word in text for text in top)
                for word in ("Input", "Output", "Duplicate", "Remove")), str(top))
    for wanted, expect in (("add:text", "text"), ("add:test", "test"),
                           ("duplicate", None), ("snapshot", None)):
        count = len(W.scene.layers)
        with MenuChoice(wanted) as choice:
            W._preview_context_menu(a.id, QPoint(0, 0))
        pump(1.8)
        grew = len(W.scene.layers) == count + 1
        if wanted == "snapshot":
            R.check("context menu: snapshot", choice.found, "")
        else:
            R.check(f"context menu: {wanted}", choice.found and grew,
                    f"layers {count} -> {len(W.scene.layers)}")
    with MenuChoice("remove") as choice:
        count = len(W.scene.layers)
        W._preview_context_menu(max(W.scene.layers, key=lambda l: l.z).id, QPoint(0, 0))
    pump(0.8)
    R.check("context menu: remove", choice.found and len(W.scene.layers) == count - 1)

    # ---- ticker / lower third editing
    ticker = W.add_layer("ticker", source="first text")
    lower = W.add_layer("lower_third", source="Name|Title")
    pump(1.5)
    rebuilds = COUNTS["start_engine"]
    ok1 = W.edit_overlay(ticker.id, values=("second text", "#ffffff", "#101418cc"))
    ok2 = W.edit_overlay(lower.id, values=("New name|New title", "#ffffff", "#101418e6"))
    pump(1.0)
    R.check("editing a ticker and a lower third updates them live",
            ok1 and ok2 and COUNTS["start_engine"] == rebuilds and engine_ok(),
            f"rebuilds={COUNTS['start_engine'] - rebuilds}")
    dialog = OverlayDialog(ticker, W)
    R.check("the overlay editor dialog opens with the layer's values",
            dialog.values()[0] == "second text", str(dialog.values()))
    dialog.close()
    close_settings()
    alive("after editing")


def step_camera_settings() -> None:
    go_scene(0)
    cam = CTX.get("cam")
    if cam is None or W.engine is None:
        R.skip("per-layer camera mode", "no camera layer")
        return
    open_settings(0)
    pump(0.5)
    W.preview.select_layer(cam.id)
    pump(0.4)
    W.camera_width.setValue(1280)
    W.camera_height.setValue(720)
    W.camera_fps.setValue(30)
    got = pump(8, until=lambda: W.engine is not None
               and "1280x720@30" in " ".join(W.engine.report.camera_modes))
    R.check("a per-layer camera mode (1280x720@30) is negotiated", got,
            str(W.engine.report.camera_modes if W.engine else None))
    pump(1.0)
    R.check("the canvas stays healthy at that mode", engine_ok())
    W.camera_width.setValue(0)
    W.camera_height.setValue(0)
    W.camera_fps.setValue(0)
    auto = f"1920x1080@{W.project.output.fps}"
    got = pump(8, until=lambda: W.engine is not None
               and auto in " ".join(W.engine.report.camera_modes))
    R.check(f"back to automatic mode ({auto})", got, str(W.engine.report.camera_modes if W.engine else None))
    W._refresh_cameras()
    R.check("the camera list offers the device",
            any(W.camera_combo.itemData(i) for i in range(W.camera_combo.count())),
            f"{W.camera_combo.count()} entries")
    close_settings()
    alive("after camera settings")


def step_playlist_ui() -> None:
    from PIL import Image

    W.add_scene()
    pump(1.5)
    image = WORK / "pl_a.png"
    Image.new("RGB", (320, 180), (200, 40, 40)).save(image)
    clip = WORK / "clip.mp4"
    if not clip.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc=duration=2:size=320x180:rate=30", "-pix_fmt", "yuv420p",
                        str(clip)], check=False)
    open_settings(2)
    with patched(QFileDialog, "getOpenFileNames",
                 staticmethod(lambda *a, **k: ([str(image), str(clip)], ""))):
        W._add_playlist_items()
    pump(2.0)
    R.check("the playlist takes an image and a video", W.playlist_list.count() == 2,
            f"{W.playlist_list.count()} items")
    W.playlist_interval.setValue(1.0)
    W.playlist_enabled.setChecked(True)
    W._store_playlist_state()
    need_engine()
    layers = [W.scene.layer(i) for i in W.playlist_ids]
    seen = set()
    end = time.monotonic() + 5
    while time.monotonic() < end:
        APP.processEvents()
        seen.add(tuple(l.visible for l in layers if l))
        time.sleep(0.02)
    R.check("the playlist cycles between its items", len(seen) >= 2, str(sorted(seen)))
    W._clear_playlist()
    pump(1.8)
    R.check("clearing the playlist removes its layers",
            not W.playlist_ids and not any(l.source in (str(image), str(clip))
                                           for l in W.scene.layers))
    close_settings()
    alive("after the playlist")


# ------------------------------------------------------------- shortcuts
def _press(sequence: str) -> None:
    combo = QKeySequence(sequence)[0]
    QTest.keyClick(W, combo.key(), combo.keyboardModifiers())
    pump(0.4)


def _fire(sequence: str, state, label: str) -> bool:
    """Press the real key; if the window is not active, fall back to the binding."""
    before = state()
    _press(sequence)
    if state() != before:
        return True
    for shortcut in W.shortcuts:
        if shortcut.key().toString() == QKeySequence(sequence).toString():
            R.add(f"{label}: key press not delivered", "WARN",
                  "the window is probably not focused; used the binding directly")
            shortcut.activated.emit()
            pump(0.4)
            return state() != before
    return False


def step_shortcuts() -> None:
    W.activateWindow()
    W.raise_()
    pump(0.5)
    go_scene(0)
    R.info("window focused", str(W.isActiveWindow()))
    cam = CTX.get("cam")
    W.record_button  # noqa: B018  (the controls exist)
    # mic mute
    muted = W.project.audio.mic_muted
    R.check("Ctrl+M toggles the microphone mute",
            _fire("Ctrl+M", lambda: W.project.audio.mic_muted, "Ctrl+M"))
    if W.project.audio.mic_muted != muted:
        _press("Ctrl+M")
    # vcam
    was = W.vcam_on
    R.check("Ctrl+Shift+V toggles the virtual camera",
            _fire("Ctrl+Shift+V", lambda: W.vcam_on, "Ctrl+Shift+V"))
    if W.vcam_on != was:
        _press("Ctrl+Shift+V")
        pump(1.0)
    # preview
    R.check("Ctrl+P stops the preview",
            _fire("Ctrl+P", lambda: W.engine is None, "Ctrl+P"))
    R.check("Ctrl+P starts it again", _fire("Ctrl+P", lambda: W.engine is None, "Ctrl+P")
            and W.engine is not None)
    pump(2.0)
    # snapshot
    folder = config.recordings_dir(W.project.output.record_dir)
    count = len(list(folder.glob("anycam-snapshot-*.png")))
    R.check("F5 saves a snapshot",
            _fire("F5", lambda: len(list(folder.glob("anycam-snapshot-*.png"))), "F5"))
    # record
    R.check("Ctrl+R starts recording", _fire("Ctrl+R", lambda: W.recording, "Ctrl+R"))
    pump(2.5)
    R.check("Ctrl+R stops it", _fire("Ctrl+R", lambda: W.recording, "Ctrl+R")
            and bool(W.recording_segments) and probe(W.recording_segments[-1]).get("video"))
    # layers
    layer = next((l for l in W.scene.layers if l.kind in W._CAPTURE_KINDS), None)
    if layer is not None:
        W.preview.select_layer(layer.id)
        layers = len(W.scene.layers)
        R.check("Ctrl+D duplicates the selected layer",
                _fire("Ctrl+D", lambda: len(W.scene.layers), "Ctrl+D"))
        pump(1.5)
        R.check("Ctrl+Z undoes it", _fire("Ctrl+Z", lambda: len(W.scene.layers), "Ctrl+Z")
                and len(W.scene.layers) == layers)
        pump(1.5)
        R.check("Ctrl+Shift+Z redoes it",
                _fire("Ctrl+Shift+Z", lambda: len(W.scene.layers), "Ctrl+Shift+Z")
                and len(W.scene.layers) == layers + 1)
        pump(1.5)
        R.check("Ctrl+Y also redoes", True if _fire("Ctrl+Z", lambda: len(W.scene.layers), "Ctrl+Z")
                and _fire("Ctrl+Y", lambda: len(W.scene.layers), "Ctrl+Y") else False)
        pump(1.5)
        W.preview.select_layer(layer.id)
        for sequence, ok in (("Ctrl+Alt+Left", lambda: abs(layer.box[0]) < 1e-6),
                             ("Ctrl+Alt+Up", lambda: abs(layer.box[1]) < 1e-6),
                             ("Ctrl+Alt+Right", lambda: abs(layer.box[0] + layer.box[2] - 1) < 1e-6),
                             ("Ctrl+Alt+Down", lambda: abs(layer.box[1] + layer.box[3] - 1) < 1e-6),
                             ("Ctrl+Alt+H", lambda: abs(layer.box[0] - (1 - layer.box[2]) / 2) < 1e-6),
                             ("Ctrl+Alt+V", lambda: abs(layer.box[1] - (1 - layer.box[3]) / 2) < 1e-6)):
            _press(sequence)
            R.check(f"{sequence} aligns the layer", ok(), f"box={layer.box}")
    # scenes
    if len(W.project.scenes) >= 2:
        _press("Ctrl+2")
        pump(1.5)
        two = W.project.active_scene == W.project.scenes[1].id
        _press("Ctrl+1")
        pump(1.5)
        R.check("Ctrl+1 / Ctrl+2 switch scenes",
                two and W.project.active_scene == W.project.scenes[0].id)
    alive("after the shortcuts")


# --------------------------------------------------------------- settings
def step_settings() -> None:
    if only_in_default_ui("settings dialog", "_open_settings"):
        return
    names = []
    for index in range(W.settings_tabs.count() if W._settings else 7):
        W._open_settings(index)
        pump(0.5)
        names.append(W.settings_tabs.tabText(W.settings_tabs.currentIndex()))
        if W._settings is None or not W._settings.isVisible():
            R.check(f"settings tab {index} opens", False)
    R.check("all settings tabs open", len(names) >= 7, ", ".join(names))
    W._open_settings(5)
    pump(0.5)
    got = pump(20, until=lambda: W._diag_thread is None or not W._diag_thread.is_alive())
    pump(0.6)
    R.check("diagnostics: the conversion benchmark finishes",
            got and "Measured" in W.diag_cost.text(), W.diag_cost.text()[:140])
    W._refresh_diag_state()
    caps = config.vcam_exclusive_caps()
    R.check("diagnostics: the fix button matches the module state",
            W.diag_fix.isHidden() == (caps is not False),
            f"exclusive_caps={caps}, fix button hidden={W.diag_fix.isHidden()}")
    R.info("diagnostics text", f"{W.diag_state.text()} | {W.diag_chromium.text()}")
    R.skip("diagnostics: Fix Discord visibility",
           "reloads the kernel module with root; not run automatically")
    W._copy_version_details()
    from PySide6.QtWidgets import QApplication as _App

    clip = _App.clipboard().text() if _App.clipboard() else ""
    R.check("about: copy version details", "AnyCam" in clip and "GStreamer" in clip, clip[:80])
    # output tab
    open_settings(3)
    pump(0.3)
    original = (W.width_spin.value(), W.height_spin.value(), W.fps_spin.value())
    W.width_spin.setValue(1281)
    R.check("odd widths snap to even", W.width_spin.value() % 2 == 0, str(W.width_spin.value()))
    W.width_spin.setValue(1280)
    W.height_spin.setValue(720)
    W.fps_spin.setValue(30)
    W.restart_engine()
    pump(2.5)
    R.check("canvas size and rate from the Output tab apply on rebuild",
            W.engine is not None and (W.engine.canvas_w, W.engine.canvas_h, W.engine.fps)
            == (1280, 720, 30), str((W.engine.canvas_w, W.engine.canvas_h, W.engine.fps)
                                    if W.engine else None))
    for index in range(W.screen_flip_combo.count()):
        W.screen_flip_combo.setCurrentIndex(index)
        pump(0.5)
        if not engine_ok():
            R.check("screen orientation options", False, W.screen_flip_combo.itemText(index))
            break
    else:
        R.check("screen orientation options all apply", True)
    W.width_spin.setValue(original[0])
    W.height_spin.setValue(original[1])
    W.fps_spin.setValue(original[2])
    W.restart_engine()
    pump(2.5)
    close_settings()
    alive("after settings")


def step_output_format() -> None:
    if only_in_default_ui("output format combos", "resolution_combo"):
        return
    go_scene(0)
    if W.engine is None:
        R.skip("output formats", "no canvas")
        return
    original = (W.project.output.canvas_w, W.project.output.canvas_h, W.project.output.fps)
    for size_index, size in enumerate(W._format_sizes):
        if GPU_SAFE and size[0] * size[1] > 1920 * 1080:
            R.skip(f"{size[0]}x{size[1]} formats", "GPU-safe mode (use --gpu-heavy)")
            continue
        for rate_index, rate in enumerate((30, 60)):
            if size[0] * size[1] > 1920 * 1080:
                pump(3.0)
                if not gpu_ok_to_continue():
                    R.add(f"{size[0]}x{size[1]}@{rate}", "FAIL",
                          f"GPU stayed busy, not run: {gpu_state()}")
                    continue
            W.resolution_combo.setCurrentIndex(size_index)
            W.fps_combo.setCurrentIndex(rate_index)
            pump(3.0)
            label = f"{size[0]}x{size[1]}@{rate}"
            if W.engine is None:
                R.check(f"{label}: canvas runs", False, W.statusBar().currentMessage())
                continue
            right = (W.engine.canvas_w, W.engine.canvas_h, W.engine.fps) == (size[0], size[1], rate)
            fps = frames_per_second(2.0)
            R.check(f"{label}: canvas built at that format", right)
            R.check(f"{label}: preview rate", fps >= 0.7 * rate, f"{fps:.1f} fps", warn=True)
            R.check(f"{label}: error free", engine_ok(),
                    W.engine.report.error if W.engine else "")
            if CTX.get("vcam_ok"):
                frames, _ = consumer_frames(1.5)
                R.check(f"{label}: the vcam keeps delivering", frames >= 10,
                        f"{frames} frames", warn=True)
    W.set_output_format(*original)
    pump(3.0)
    alive("after output formats")


def step_encoders() -> None:
    from anycam.pipeline import encoder_plan

    go_scene(0)
    if W.engine is None:
        R.skip("encoders", "no canvas")
        return
    names = {"h264": "h264", "h265": "hevc", "av1": "av1"}
    open_settings(3)
    original = W.encoder_combo.currentData()
    for index in range(W.encoder_combo.count()):
        value = str(W.encoder_combo.itemData(index))
        if GPU_SAFE and value != "cpu":
            R.skip(f"encoder {value}", "GPU-safe mode (use --gpu-heavy)")
            continue
        pump(3.0)                         # let the GPU settle between encoders
        if not gpu_ok_to_continue():
            R.add(f"encoder {value}", "FAIL", f"GPU stayed busy, not run: {gpu_state()}")
            break
        W.encoder_combo.setCurrentIndex(index)
        W.restart_engine()
        pump(2.0)
        plan = encoder_plan(value)
        segments, took = _record(4.0)
        info = probe(segments[-1]) if segments else {"error": "no file"}
        video = info.get("video") or {}
        R.check(f"encoder {value}: file is {names[plan.codec]} as the app announced "
                f"({plan.note})", video.get("codec") == names[plan.codec],
                f"codec={video.get('codec')} {video.get('packets')} packets "
                f"{info.get('error', '')}")
        rate = W.project.output.fps
        R.check(f"encoder {value}: no missing frames", video.get("packets", 0) >= 0.6 * 4 * rate,
                f"{video.get('packets')} packets at {rate} fps", warn=True)
        R.check(f"encoder {value}: stop returns promptly", took < 3.0, f"{took:.1f}s")
    W.encoder_combo.setCurrentIndex(max(0, W.encoder_combo.findData(original)))
    W.restart_engine()
    pump(2.0)
    close_settings()


def step_audio_mix() -> None:
    if ARGS.no_mic:
        R.skip("audio mixing", "--no-mic")
        return
    go_scene(0)
    if W.engine is None:
        R.skip("audio mixing", "no canvas")
        return
    open_settings(1)
    pump(0.4)
    tone = WORK / "tone.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=12", str(tone)], check=False)
    W.media_edit.setText(str(tone))
    W.restart_engine()
    pump(2.0)
    segments, _ = _record(5.0)
    info = probe(segments[-1]) if segments else {}
    loud = volume_of(segments[-1]) if segments else {}
    R.check("an audio bed is mixed into the recording",
            bool(info.get("audio")) and (loud.get("max") or -99) > -30,
            f"audio={bool(info.get('audio'))} volume={loud}")
    W._toggle_audio(True)
    pump(2.5)
    got = pump(5, until=lambda: W.engine is not None and W.engine.mic_level_db is not None)
    R.check("the microphone level meter reports while the mic is on", got,
            f"{W.engine.mic_level_db if W.engine else None} dB")
    W.mic_gain_slider.setValue(10)
    pump(0.5)
    R.check("mic gain applies live", abs(W.engine.mic_volume.get_property("volume") - 10 ** 0.5) < 0.05
            if W.engine and W.engine.mic_volume is not None else False,
            f"{W.engine.mic_volume.get_property('volume') if W.engine and W.engine.mic_volume else None}")
    W.mute_check.setChecked(True)
    pump(2.0)
    R.check("mute silences the microphone", W.engine is not None
            and W.engine.mic_volume is not None and W.engine.mic_volume.get_property("mute"))
    W.mute_check.setChecked(False)
    W.mic_gain_slider.setValue(0)
    monitors = [s for s in config.list_audio_sources() if s.is_monitor]
    if monitors:
        index = W.desktop_combo.findData(monitors[0].name)
        if index >= 0:
            W.desktop_combo.setCurrentIndex(index)
            pump(2.5)
            R.check("desktop audio joins the mix", W.engine is not None
                    and any(i.startswith("desktop:") for i in W.engine.report.audio_inputs),
                    str(W.engine.report.audio_inputs if W.engine else None))
            segments, _ = _record(4.0)
            R.check("a recording with bed + mic + desktop audio plays",
                    bool(segments) and bool((probe(segments[-1]).get("audio") or {})))
            W.desktop_combo.setCurrentIndex(0)
    else:
        R.skip("desktop audio", "no monitor source")
    W.media_edit.setText("")
    W._toggle_audio(False)
    pump(2.0)
    close_settings()
    alive("after audio")


# ----------------------------------------------------------------- streaming
def step_stream() -> None:
    go_scene(0)
    if W.engine is None:
        R.skip("streaming", "no canvas")
        return
    open_settings(4)
    key = "secretkey123456"
    port = free_port()
    url = f"rtmp://127.0.0.1:{port}/live/{key}"
    sink = WORK / "rx.flv"
    receiver = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-listen", "1", "-i", url,
         "-c", "copy", str(sink)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    pump(1.2)
    W.rtmp_edit.setText(url)
    W.toggle_live()
    R.check("Go Live is on", W.live and W.live_button.text() == "Stop stream",
            W.statusBar().currentMessage())
    pump(8)
    R.check("streaming raises no error", W.engine is not None and not W.engine.report.rtmp_error,
            W.engine.report.rtmp_error if W.engine else "engine gone")
    R.check("the preview keeps running while streaming", engine_ok())
    segments, _ = _record(3.0)
    R.check("recording works while streaming", bool(segments) and
            bool(probe(segments[-1]).get("video")))
    W.toggle_live()
    pump(2.0)
    try:
        receiver.wait(timeout=8)
    except subprocess.TimeoutExpired:
        receiver.terminate()
    received = probe(str(sink)) if sink.exists() else {"error": "nothing received"}
    video = received.get("video") or {}
    R.check("an RTMP server receives H.264 video", video.get("codec") == "h264"
            and video.get("packets", 0) > 60, f"{received if 'error' in received else video}")
    R.check("Stop stream turns it off", not W.live and W.live_button.text() == "Go Live")
    leaked = [e for e in R.events if key in e["text"]]
    R.check("the stream key never appears in the app's output", not leaked,
            f"{len(leaked)} lines, e.g. {leaked[0]['text'][:120] if leaked else ''}")
    W.rtmp_edit.setText("rtmp://127.0.0.1:1/live/nobody")
    W.toggle_live()
    pump(8)
    R.check("an unreachable server does not leave the app stuck in Live",
            not W.live, f"live={W.live}; status={W.statusBar().currentMessage()!r}")
    R.check("the preview survives a dead stream", engine_ok())
    if W.live:
        W.toggle_live()
        pump(2.0)
    W.rtmp_edit.setText("")
    close_settings()
    alive("after streaming")


def start_mjpeg_server(port: int):
    """A phone-camera style MJPEG stream (multipart/x-mixed-replace), 640x360 at 15 fps."""
    import io
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from PIL import Image, ImageDraw

    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            n = 0
            try:
                while not stop.is_set():
                    picture = Image.new("RGB", (640, 360), ((n * 7) % 255, 80, 200))
                    ImageDraw.Draw(picture).rectangle([n * 9 % 600, 150, n * 9 % 600 + 40, 190],
                                                      fill=(255, 255, 255))
                    out = io.BytesIO()
                    picture.save(out, "JPEG", quality=80)
                    data = out.getvalue()
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                    self.wfile.flush()
                    n += 1
                    time.sleep(1 / 15)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def close():
        stop.set()
        server.shutdown()
        server.server_close()

    return close


def step_network() -> None:
    go_scene(0)
    if W.engine is None:
        R.skip("network sources", "no canvas")
        return
    before = len(W.scene.layers)
    with patched(QInputDialog, "getText", staticmethod(lambda *a, **k: ("ftp://x/y", True))):
        layer = W.add_layer("network")
    R.check("a non-video URL scheme is refused", layer is None and len(W.scene.layers) == before)
    port = free_port()
    close_server = start_mjpeg_server(port)   # what IP Webcam / DroidCam serve
    pump(0.5)
    layer = W.add_layer("network", source=f"http://127.0.0.1:{port}/video")
    R.check("a phone-camera MJPEG URL is added", layer is not None)
    if layer is not None:
        got = pump(15, until=lambda: camera_health(layer.id).get("frames", 0) > 10)
        R.check("a phone-camera MJPEG URL delivers frames", got, f"health={camera_health(layer.id)}")
        first = picture()
        pump(1.0)
        R.check("the stream is moving", change(first, picture()) > 0.1,
                f"change {change(first, picture()):.2f}")
        R.check("no engine error with the stream", engine_ok(),
                W.engine.report.error if W.engine else "")
        W.remove_layers([layer.id])
    close_server()
    # RTSP (the same local server the test-suite uses)
    try:
        sys.path.insert(0, str(ROOT / "tests"))
        import test_rtsp as rtsp_helpers
    except Exception as exc:  # noqa: BLE001
        R.skip("RTSP source", f"test helper unavailable: {exc}")
        rtsp_helpers = None
    if rtsp_helpers is not None:
        clip = WORK / "rtsp-clip.mp4"
        sdp = WORK / "rtsp.sdp"
        rtsp_helpers._make_clip(clip)
        rtp_port = rtsp_helpers._free_udp_port()
        producer = rtsp_helpers._start_rtp_producer(clip, sdp, rtp_port)
        rtsp = None
        try:
            pump(2.0, until=lambda: sdp.exists())
            rtsp = rtsp_helpers._RTSPTestServer(sdp, rtp_port).start()
            layer = W.add_layer("network", source=f"rtsp://127.0.0.1:{rtsp.port}/test")
            got = pump(15, until=lambda: layer is not None
                       and camera_health(layer.id).get("frames", 0) > 5)
            R.check("an RTSP stream delivers frames", got,
                    f"health={camera_health(layer.id) if layer else None}")
            if layer is not None:
                W.remove_layers([layer.id])
        finally:
            producer.terminate()
            if rtsp is not None:
                rtsp.close()
    # an unreachable camera and credentials in the URL
    secret = "hunter2pass"
    layer = W.add_layer("network", source=f"rtsp://user:{secret}@127.0.0.1:1/none")
    started = time.monotonic()
    pump(6.0)
    R.check("an unreachable network source does not freeze the window",
            R.gaps.get(R.step, 0) < 2.0, f"longest freeze {R.gaps.get(R.step, 0):.1f}s")
    R.check("the canvas keeps running with a dead network layer", W.engine is not None)
    R.check("the camera password is not shown in the layer name",
            layer is not None and secret not in layer.name, layer.name if layer else "")
    leaked = [e for e in R.events if secret in e["text"]]
    R.check("the camera password never appears in the app's output", not leaked,
            f"{len(leaked)} lines")
    if layer is not None:
        W.remove_layers([layer.id])
        pump(1.0)
    alive("after network sources")


# ---------------------------------------------------------- project files
def step_project_io() -> None:
    from anycam.model import Project, load_scene_preset, save_scene_preset

    go_scene(0)
    path = WORK / "project.json"
    original = config.project_path
    config.project_path = lambda: path
    try:
        W.save_project()
        R.check("save writes the project", path.exists(), W.statusBar().currentMessage())
        R.check("the project file is private", (path.stat().st_mode & 0o077) == 0,
                oct(path.stat().st_mode & 0o777))
        loaded = Project.load(path)
        R.check("the saved project loads back with the same scenes and layers",
                [(s.name, [l.id for l in s.layers]) for s in loaded.scenes]
                == [(s.name, [l.id for l in s.layers]) for s in W.project.scenes])
        W.save_project()
        R.check("a second save keeps a .bak of the first",
                path.with_name(path.name + ".bak").exists())
        good = path.read_text()
        ids = {s.id: [l.id for l in s.layers] for s in W.project.scenes}
        W.reload_project()
        pump(3.0)
        R.check("reload restores the project", {s.id: [l.id for l in s.layers]
                                                for s in W.project.scenes} == ids)
        R.check("reload restarts the preview", W.engine is not None and engine_ok())
        path.write_text("{ this is not json")
        W.reload_project()
        pump(0.5)
        R.check("a corrupt file is reported and the open project is kept",
                bool(W.project.scenes) and "Could not load" in W.statusBar().currentMessage(),
                W.statusBar().currentMessage())
        path.write_text(json.dumps({"scenes": [{"layers": [{"kind": "camera"}, {"x": 1}]}],
                                    "output": {"canvas_w": "wide", "fps": -3}}))
        project = Project.load(path)
        R.check("a damaged project loads with repairs instead of failing",
                bool(project.scenes) and project.output.fps >= 1
                and len(project.load_warnings) >= 1, str(project.load_warnings[:2]))
        W.reload_project()
        pump(3.0)
        R.check("the window survives loading a damaged project", W.engine is None or engine_ok())
        path.write_text(good)          # put the real project back for the later steps
        W.reload_project()
        pump(3.0)
        cam = CTX.get("cam")
        if cam is not None:
            CTX["cam"] = next((l for s in W.project.scenes for l in s.layers if l.id == cam.id), None)
        R.check("the real project is restored", CTX.get("cam") is not None or cam is None)
    finally:
        config.project_path = original
    preset = save_scene_preset(W.scene, WORK / "presets")
    again = load_scene_preset(preset)
    R.check("scene presets round-trip", [l.id for l in again.layers]
            == [l.id for l in W.scene.layers], str(preset.name))
    need_engine()
    pump(2.0)


def step_multiview() -> None:
    go_scene(0)
    if W.engine is None:
        R.skip("multi-view", "no canvas")
        return
    W.toggle_multiview()
    pump(2.5)
    view = getattr(W, "_multiview", None)
    R.check("multi-view opens", view is not None)
    if view is not None:
        R.check("multi-view shows a preview per live source", len(view.previews) >= 1,
                f"{len(view.previews)} previews")
        W.toggle_multiview()
        pump(1.0)
        R.check("multi-view closes", getattr(W, "_multiview", None) is None)
    alive("after multi-view")


def step_snapshot() -> None:
    go_scene(0)
    if W.engine is None:
        R.skip("snapshot", "no canvas")
        return
    folder = config.recordings_dir(W.project.output.record_dir)
    before = set(folder.glob("anycam-snapshot-*.png"))
    pump(1.0)
    W.snapshot()
    pump(0.5)
    new = sorted(set(folder.glob("anycam-snapshot-*.png")) - before)
    R.check("the snapshot button writes a PNG", bool(new), W.statusBar().currentMessage())
    if new:
        from PIL import Image

        with Image.open(new[-1]) as shot:
            size = shot.size
        image = W.preview.image
        R.check("the snapshot has the preview's size",
                image is not None and size == (image.width(), image.height()),
                f"{size} vs {(image.width(), image.height()) if image else None}")


# --------------------------------------------------------------------- stress
def step_stress() -> None:
    go_scene(0)
    if W.engine is None:
        R.skip("stress", "no canvas")
        return
    process = psutil.Process()
    fds0 = len(os.listdir("/proc/self/fd"))
    threads0 = threading.active_count()
    rss0 = process.memory_info().rss / 2 ** 20
    started = time.monotonic()
    for _ in range(25):
        layer = W.add_layer("test")
        pump(0.05)
        if layer is not None:
            W.remove_layers([layer.id])
        pump(0.05)
    R.check("25 rapid add/remove cycles", engine_ok(), "")
    for index in range(20):
        W.switch_scene(index % 2)
        pump(0.1)
    go_scene(0)
    R.check("20 rapid scene switches", engine_ok() or W.engine is None, "")
    target = CTX.get("cam") or next((l for l in W.scene.layers if l.kind in W._CAPTURE_KINDS), None)
    if target is not None and W.engine is not None:
        W.preview.select_layer(target.id)
        keys = ["bulge", "tilt", "none", "fisheye", "blur", "none", "pinch", "twirl"] * 5
        for key in keys:
            W._set_effect("filter", key)
            pump(0.05)
        wait_filters(target.id, 15)
        W._set_effect("filter", "none")
        wait_filters(target.id, 15)
        R.check("40 rapid filter changes", engine_ok() and wait_filters(target.id, 10), "")
    for _ in range(6):
        W.toggle_record()
        pump(0.6)
        W.toggle_record()
        pump(0.3)
    R.check("6 quick record start/stop cycles", not W.recording and engine_ok() or W.engine is None, "")
    for _ in range(10):
        W.toggle_vcam()
        pump(0.15)
    if W.vcam_on != bool(CTX.get("vcam_ok")):
        W.toggle_vcam()
    pump(1.5)
    R.check("10 rapid virtual camera toggles", engine_ok(), "")
    for _ in range(5):
        W.toggle_engine()
        pump(0.4)
        W.toggle_engine()
        pump(1.2)
    pump(3.0)
    R.check("5 preview stop/start cycles", W.engine is not None and engine_ok(), "")
    n0 = FR["n"]
    pump(2.0)
    R.check("frames still flow after the stress run", FR["n"] > n0 + 20, f"{FR['n'] - n0} frames in 2 s")
    fds1 = len(os.listdir("/proc/self/fd"))
    threads1 = threading.active_count()
    rss1 = process.memory_info().rss / 2 ** 20
    R.check("no descriptor leak", fds1 - fds0 <= 25, f"{fds0} -> {fds1}")
    R.check("no thread leak", threads1 - threads0 <= 12, f"{threads0} -> {threads1}")
    R.check("memory after the stress run", rss1 - rss0 < 400, f"{rss0:.0f} -> {rss1:.0f} MiB",
            warn=True)
    R.info("stress run took", f"{time.monotonic() - started:.0f}s")


def step_autosave() -> None:
    """The window must save the project by itself, even before Settings was opened."""
    path = WORK / "autosave.json"
    original = config.project_path
    config.project_path = lambda: path
    built = getattr(W, "_settings", None) is not None
    W._persist = True
    try:
        W._mark_dirty()
        pump(2.5)
    finally:
        W._persist = False
        config.project_path = original
    R.info("settings dialog already built", str(built))
    R.check("autosave writes the project (Settings never opened)", path.exists(),
            W.statusBar().currentMessage())


STEPS = [
    ("startup", step_startup),
    ("autosave", step_autosave),
    ("test-source", step_test_source),
    ("camera", step_camera),
    ("screen", step_screen),
    ("vcam", step_vcam),
    ("record", step_record),
    ("record-audio", step_record_audio),
    ("record-resize", step_record_resize),
    ("scenes", step_scenes),
    ("scene-management", step_scene_management),
    ("layouts", step_layouts),
    ("slots", step_slots),
    ("effects", step_effects),
    ("adjust-effects", step_adjust_effects),
    ("catalog", step_catalog),
    ("backdrops", step_backdrops),
    ("overlays", step_overlays),
    ("draw", step_draw),
    ("editing", step_editing),
    ("camera-settings", step_camera_settings),
    ("release-camera", step_release_camera),
    ("playlist", step_playlist),
    ("playlist-ui", step_playlist_ui),
    ("shortcuts", step_shortcuts),
    ("settings", step_settings),
    ("output-format", step_output_format),
    ("encoders", step_encoders),
    ("audio-mix", step_audio_mix),
    ("stream", step_stream),
    ("network", step_network),
    ("project-io", step_project_io),
    ("multiview", step_multiview),
    ("snapshot", step_snapshot),
    ("recovery-plain", step_recovery_plain),
    ("recovery-screen", step_recovery_screen),
    ("screen-cleanup", step_screen_cleanup),
    ("stress", step_stress),
    ("steady", step_steady),
    ("shutdown", step_shutdown),
]


# -------------------------------------------------------------------- main
def install_probes() -> None:
    global W
    R.step = "setup"
    # No polkit prompts on the user's desktop during a test run.
    config.refresh_vcam_udev = lambda *a, **k: "skipped by live-app-check"
    for name in ("critical", "warning", "information", "question"):
        setattr(QMessageBox, name, staticmethod(
            lambda *a, _name=name, **k: (R.event("dialog", f"{_name}: {a[1:3]}"),
                                         QMessageBox.StandardButton.Ok)[1]))
    qInstallMessageHandler(lambda mode, ctx, message: R.event("qt", message))
    import anycam.gui as gui_module
    import anycam.gui_many as many_module

    gui_module.QMenu = FakeMenu          # menus can never block the script
    many_module.QMenu = FakeMenu
    project = empty_project()
    if GPU_SAFE:
        project.output.fps = 30
    if ARGS.ui == "classic":
        from anycam.gui import AnyCamWindow as WindowClass
    else:
        WindowClass = ManyCamWindow
    W = WindowClass(project)
    W.resize(1440, 900)
    original_status = W._status

    def status(text, *a, **k):
        R.event("status", text)
        return original_status(text, *a, **k)

    W._status = status
    original_frame = W._on_frame

    def counted(sample):
        FR["n"] += 1
        return original_frame(sample)

    W._on_frame = counted
    original_start = W.start_engine

    def counted_start(*a, **k):
        COUNTS["start_engine"] += 1
        return original_start(*a, **k)

    W.start_engine = counted_start
    W.project.output.record_dir = str(WORK / "rec")
    W.show()
    APP.processEvents()
    # UI freeze watchdog
    watchdog = QTimer(W)
    watchdog.setInterval(40)

    def tick() -> None:
        now = time.monotonic()
        gap = now - LAST_TICK[0]
        LAST_TICK[0] = now
        if gap > 0.7:
            R.gaps[R.step] = max(R.gaps.get(R.step, 0.0), gap)
            R.event("freeze", f"UI did not respond for {gap:.1f}s")

    watchdog.timeout.connect(tick)
    watchdog.start()


def run() -> int:
    only = {name.strip() for name in ARGS.only.split(",") if name.strip()}
    if subprocess.run(["pgrep", "-f", "anycam gui"], capture_output=True).stdout.split():
        print("AnyCam is already running; close it first (two writers fight for /dev/video10).")
        return 2
    install_probes()
    for name, function in STEPS:
        if only and name not in only and name not in ("startup", "shutdown"):
            continue
        R.step = name
        print(f"\n=== {name} ===", flush=True)
        if not gpu_ok_to_continue():
            R.add("GPU stayed busy", "FAIL",
                  f"stopping before '{name}' to protect the desktop: {gpu_state()}")
            break
        LAST_TICK[0] = time.monotonic()
        try:
            function()
        except Exception:  # noqa: BLE001
            R.add("step crashed", "FAIL", traceback.format_exc().strip().splitlines()[-1])
            R.event("exception", traceback.format_exc())
        rss = psutil.Process().memory_info().rss / 2 ** 20
        print(f"        [{name}] finished; memory {rss:.0f} MiB, "
              f"{len(os.listdir('/proc/self/fd'))} descriptors, {threading.active_count()} threads, "
              f"GPU (util %, MiB used, MiB total) {gpu_state()}", flush=True)
        gap = R.gaps.get(name, 0.0)
        if gap > 1.5:
            R.add("window stayed responsive", "FAIL", f"froze for {gap:.1f}s during '{name}'")
    return finish()


def finish() -> int:
    R.step = "analysis"
    print("\n=== analysis ===", flush=True)
    stderr = [e for e in R.events if e["kind"] == "stderr"]
    tracebacks = [e for e in stderr if "Traceback (most recent call last)" in e["text"]]
    R.check("no unhandled Python exception inside the app", not tracebacks,
            f"{len(tracebacks)} traceback(s) in app-stderr.log")
    dialogs = [e for e in R.events if e["kind"] == "dialog"]
    R.check("no error dialogs shown", not dialogs, "; ".join(e["text"] for e in dialogs[:3]))
    keywords = re.compile(r"error|fail|could not|unavailable|disconnect|no fresh|stalled|lost",
                          re.I)
    noisy = [e for e in R.events if e["kind"] == "status" and keywords.search(e["text"])]
    seen = Counter(e["text"] for e in noisy)
    for text, count in seen.most_common(12):
        R.add("status bar message", "WARN", f"{count}x at '{next(e['step'] for e in noisy if e['text'] == text)}': {text}")
    errors = [e for e in stderr if re.search(r"\[capture-error\]|sink error|rejected|failed", e["text"], re.I)]
    for text, count in Counter(e["text"] for e in errors).most_common(8):
        R.add("app log line", "WARN", f"{count}x: {text[:200]}")
    gst = REPORT_DIR / "gstreamer.log"
    if gst.exists():
        lines = [l for l in gst.read_text(errors="replace").splitlines()
                 if re.search(r"\b(ERROR|CRITICAL)\b", l)]
        kinds = Counter(re.sub(r"0x[0-9a-f]+|\d+", "N", l.split(" ", 4)[-1])[:160] for l in lines)
        R.check("no GStreamer ERROR/CRITICAL lines", not lines, f"{len(lines)} line(s)")
        for text, count in kinds.most_common(6):
            R.add("gstreamer", "WARN", f"{count}x {text}")
        warns = len([l for l in gst.read_text(errors="replace").splitlines() if " WARN " in l])
        R.info("GStreamer warnings", f"{warns} (see gstreamer.log)")
    return write_report()


def write_report() -> int:
    counts = Counter(row["status"] for row in R.rows)
    failed = [row for row in R.rows if row["status"] == "FAIL"]
    warned = [row for row in R.rows if row["status"] == "WARN"]
    lines = [f"# AnyCam live check {STAMP}", "",
             f"{counts['PASS']} passed, {counts['FAIL']} failed, {counts['WARN']} warnings, "
             f"{counts['SKIP']} skipped.  Took {R.now():.0f}s.", ""]
    for title, rows in (("Failures", failed), ("Warnings", warned)):
        lines += [f"## {title}", ""]
        lines += [f"- **[{row['step']}] {row['check']}** — {row['detail']}" for row in rows] or ["- none"]
        lines.append("")
    lines += ["## All checks", "", "| step | check | result | detail |", "|---|---|---|---|"]
    lines += [f"| {r['step']} | {r['check']} | {r['status']} | {str(r['detail']).replace('|', '/')} |"
              for r in R.rows]
    (REPORT_DIR / "report.md").write_text("\n".join(lines) + "\n")
    (REPORT_DIR / "report.json").write_text(json.dumps(
        {"rows": R.rows, "events": R.events[-400:]}, indent=2))
    print(f"\n{counts['PASS']} passed, {counts['FAIL']} FAILED, {counts['WARN']} warnings, "
          f"{counts['SKIP']} skipped")
    for row in failed:
        print(f"  FAIL [{row['step']}] {row['check']} — {row['detail']}")
    print(f"\nreport: {REPORT_DIR / 'report.md'}")
    if not ARGS.keep:
        shutil.rmtree(WORK, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    code = run()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
