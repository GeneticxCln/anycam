#!/usr/bin/env python3
"""Spike 2: GStreamer appsink -> Qt QVideoSink (QML VideoOutput) at 1080p30.

Measures delivered/dropped frames, conversion cost, UI event-loop latency,
CPU/RSS and GPU utilisation. Produces a JSON report and a window screenshot.
"""
import argparse
import faulthandler
import json
import os
import statistics
import subprocess
import sys
import time

import gi
import numpy as np
import psutil

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp  # noqa: E402

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Property, Qt, QSize  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402
from PySide6.QtMultimedia import QVideoSink, QVideoFrame, QVideoFrameFormat  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtQuick import QQuickWindow  # noqa: E402

Gst.init(None)

W, H, FPS = 1920, 1080, 30


class Backend(QObject):
    statsChanged = Signal()

    def __init__(self, sink):
        super().__init__()
        self.sink = sink
        self.frame_count = 0
        self.push_count = 0
        self.convert_ms = []
        self.start = time.monotonic()
        self.samples = []
        self.retained = []

    @Property(str, notify=statsChanged)
    def statsText(self):
        elapsed = max(time.monotonic() - self.start, 1e-6)
        fps = self.frame_count / elapsed
        expected = elapsed * FPS
        dropped = max(0.0, expected - self.frame_count)
        return (
            f"delivered {self.frame_count}  {fps:5.1f} fps\n"
            f"dropped   {dropped:5.1f}  "
            f"{100.0 * dropped / max(expected, 1):5.2f} %\n"
            f"sink      {self.push_count} frames"
        )


def gpu_state():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3).stdout.strip()
        util, mem = [x.strip() for x in out.split(",")[:2]]
        return {"gpu_util_pct": float(util), "gpu_mem_mib": float(mem)}
    except Exception:
        return {"gpu_util_pct": None, "gpu_mem_mib": None}


def main():
    faulthandler.enable()
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--source", default="testsrc")
    ap.add_argument("--out", default="evidence/02_preview_bridge")
    ap.add_argument("--shot-at", type=float, default=0.0)
    ap.add_argument("--frame-mode", choices=["image", "map"], default="map")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    app = QGuiApplication(sys.argv[:1])
    backend = Backend(None)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    qml = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Preview.qml")
    engine.load(QUrl.fromLocalFile(qml))
    if not engine.rootObjects():
        print("QML failed to load", file=sys.stderr)
        return 2
    window = engine.rootObjects()[0]
    video_output = window.findChild(QObject, "output")
    sink = video_output.property("videoSink") if video_output is not None else None
    if sink is None:
        print("VideoOutput did not expose a videoSink", file=sys.stderr)
        return 3
    backend.sink = sink
    sink.videoFrameChanged.connect(lambda _f: setattr(backend, "push_count", backend.push_count + 1))

    if args.source == "camera":
        src = (
            "v4l2src device=/dev/video0 do-timestamp=true ! "
            "image/jpeg,width=1920,height=1080,framerate=30/1 ! jpegdec"
        )
    else:
        src = "videotestsrc is-live=true pattern=ball"

    pipeline = Gst.parse_launch(
        f"{src} ! videoconvert ! video/x-raw,format=RGBA,width={W},height={H},"
        f"framerate={FPS}/1 ! "
        "appsink name=appsink emit-signals=true max-buffers=2 drop=true sync=true"
    )
    appsink = pipeline.get_by_name("appsink")
    proc = psutil.Process()
    proc.cpu_percent(None)

    def on_sample(_sink):
        sample = _sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        t0 = time.perf_counter()
        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        w = caps.get_value("width")
        h = caps.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            stride = w * 4
            if args.frame_mode == "image":
                img = QImage(info.data, w, h, stride,
                             QImage.Format.Format_RGBA8888).copy()
                buf.unmap(info)
                frame = QVideoFrame(img)
            else:
                fmt = QVideoFrameFormat(QSize(w, h),
                                        QVideoFrameFormat.PixelFormat.Format_RGBA8888)
                frame = QVideoFrame(fmt)
                if frame.map(QVideoFrame.MapMode.WriteOnly):
                    frame.bits(0)[:] = info.data[: w * h * 4]
                    frame.unmap()
                buf.unmap(info)
            backend.frame_count += 1
            if frame.isValid():
                backend.sink.setVideoFrame(frame)
                backend.retained.append(frame)
                if len(backend.retained) > 8:
                    backend.retained.pop(0)
        backend.convert_ms.append((time.perf_counter() - t0) * 1000.0)
        return Gst.FlowReturn.OK

    appsink.connect("new-sample", on_sample)

    # UI event-loop latency: a 50 ms timer; record how late callbacks arrive.
    ui_latency = []
    tick_last = [time.monotonic()]

    def tick():
        now = time.monotonic()
        ui_latency.append((now - tick_last[0] - 0.05) * 1000.0)
        tick_last[0] = now
        backend.statsChanged.emit()

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(tick)
    timer.start(50)

    samples = []

    def sample_state():
        cpu = proc.cpu_percent(None)
        g = gpu_state()
        rec = {"t": round(time.monotonic() - backend.start, 2),
               "cpu_pct": cpu, "rss_mib": round(proc.memory_info().rss / 2**20, 1),
               **g, "frames": backend.frame_count}
        samples.append(rec)
        print(f"T+{rec['t']:6.1f}s frames={rec['frames']} rss={rec['rss_mib']} gpu={rec['gpu_util_pct']} vram={rec['gpu_mem_mib']}", file=sys.stderr, flush=True)

    sampler = QTimer()
    sampler.timeout.connect(sample_state)
    sampler.start(1000)

    screenshot_path = os.path.join(args.out, "preview_window.png")
    state = {"shot": False}

    def maybe_shot():
        if args.shot_at > 0 and not state["shot"] and time.monotonic() - backend.start > args.shot_at:
            state["shot"] = True
            grab = window.grabWindow()
            grab.save(screenshot_path)
            print("screenshot", screenshot_path, grab.size())

    shot_timer = QTimer()
    shot_timer.timeout.connect(maybe_shot)
    shot_timer.start(1000)

    pipeline.set_state(Gst.State.PLAYING)

    def finish():
        elapsed = time.monotonic() - backend.start
        expected = elapsed * FPS
        delivered = backend.frame_count
        conv = backend.convert_ms[1:] or [0.0]

        def pct(vals, p):
            if not vals:
                return None
            vals = sorted(vals)
            k = min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1))))
            return round(vals[k], 2)

        cpu_vals = [s["cpu_pct"] for s in samples if s["cpu_pct"] is not None]
        gpu_vals = [s["gpu_util_pct"] for s in samples if s["gpu_util_pct"] is not None]
        rss = [s["rss_mib"] for s in samples]
        report = {
            "spike": "02_preview_bridge",
            "source": args.source,
            "frame_mode": args.frame_mode,
            "resolution": [W, H],
            "target_fps": FPS,
            "duration_s": round(elapsed, 3),
            "delivered_frames": delivered,
            "expected_frames": round(expected, 1),
            "delivered_fps": round(delivered / elapsed, 3),
            "dropped_frames": round(max(0.0, expected - delivered), 1),
            "dropped_pct": round(100.0 * max(0.0, expected - delivered) / max(expected, 1), 3),
            "sink_frames": backend.push_count,
            "convert_ms_avg": round(statistics.mean(conv), 3),
            "convert_ms_p95": pct(conv, 95),
            "convert_ms_max": round(max(conv), 3),
            "ui_timer_latency_ms_p95": pct(ui_latency, 95),
            "ui_timer_latency_ms_max": pct(ui_latency, 100),
            "process_cpu_pct_avg": round(statistics.mean(cpu_vals), 2) if cpu_vals else None,
            "process_cpu_pct_max": round(max(cpu_vals), 2) if cpu_vals else None,
            "gpu_util_pct_avg": round(statistics.mean(gpu_vals), 2) if gpu_vals else None,
            "gpu_util_pct_max": max(gpu_vals) if gpu_vals else None,
            "rss_mib_start": rss[0] if rss else None,
            "rss_mib_end": rss[-1] if rss else None,
            "samples": samples,
            "screenshot": screenshot_path,
            "qt_platform": QGuiApplication.platformName(),
            "pass": (
                delivered / elapsed >= FPS * 0.99
                and (max(0.0, expected - delivered) / max(expected, 1)) < 0.01
                and (pct(ui_latency, 95) or 999) < 100
            ),
        }
        path = os.path.join(args.out, "preview_bridge.json")
        with open(path, "w") as fh:
            json.dump(report, fh, indent=2)
        print(json.dumps(report, indent=2), flush=True)
        try:
            appsink.disconnect_by_func(on_sample)
        except Exception:
            pass
        try:
            pipeline.set_state(Gst.State.NULL)
        except Exception:
            pass
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0 if report["pass"] else 1)

    QTimer.singleShot(int(args.seconds * 1000), finish)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
