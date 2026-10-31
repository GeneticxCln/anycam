#!/usr/bin/env python3
"""Drive a real camera unplug/replug through AnyCam and save evidence.

The script starts an offscreen AnyCam window with one camera layer at
``--device``, waits for the StreamCam to disappear (the operator unplugs it),
captures the placeholder frame, then waits for the device to return and
captures the reconnected frame.  It writes ``report.json`` plus the three PNGs
into ``--out``.

Usage:
    QT_QPA_PLATFORM=offscreen PYTHONPATH=src \
      .venv/bin/python scripts/verify-camera-reconnect.py --out evidence/12_camera_reconnect

Exit codes: 0 pass, 1 timeout/failed check, 2 startup failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from anycam.gui import AnyCamWindow  # noqa: E402
from anycam.model import Layer, Project, Scene  # noqa: E402

def _save(window: AnyCamWindow, path: Path) -> bool:
    pixmap = window.grab()
    return bool(pixmap.save(str(path)))


def _write(out_dir: Path, report: dict) -> None:
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify-camera-reconnect", description=__doc__)
    parser.add_argument("--device", default="/dev/video0", help="StreamCam V4L2 node")
    parser.add_argument("--out", default="evidence/12_camera_reconnect",
                        help="artifact directory")
    parser.add_argument("--canvas-width", type=int, default=1280)
    parser.add_argument("--canvas-height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--unplug-timeout", type=float, default=300.0,
                        help="how long to wait for the physical unplug")
    parser.add_argument("--reconnect-timeout", type=float, default=120.0,
                        help="how long to wait for the device to return")
    parser.add_argument("--settle", type=float, default=20.0,
                        help="max wait for the preview after each transition")
    args = parser.parse_args(argv)

    device = Path(args.device)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report: dict = {
        "date": datetime.now().astimezone().isoformat(timespec="seconds"),
        "device": str(device),
        "ok": False,
        "checks": {},
        "screenshots": [],
    }

    project = Project(name="Camera reconnect evidence")
    project.output.canvas_w = args.canvas_width
    project.output.canvas_h = args.canvas_height
    project.output.fps = args.fps
    scene = Scene("scene_reconnect", "Reconnect")
    camera = Layer("layer_reconnect", "camera", "StreamCam", source=str(device),
                   box=(0.0, 0.0, 1.0, 1.0), z=1)
    scene.add_layer(camera)
    project.scenes = [scene]
    project.active_scene = scene.id

    app = QApplication(sys.argv[:1])
    window = AnyCamWindow(project)
    window.headless = True
    window.resize(1440, 900)
    window.show()
    window.start_engine()

    def pump_until(predicate, timeout: float) -> bool:
        end = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < end:
            app.processEvents()
            if predicate():
                return True
            time.sleep(0.03)
        app.processEvents()
        return bool(predicate())

    def finish(code: int) -> int:
        window.close()
        app.processEvents()
        report["duration_s"] = round(time.monotonic() - started, 2)
        _write(out_dir, report)
        print(json.dumps(report, indent=2), flush=True)
        os._exit(code)

    if not pump_until(
        lambda: window.engine is not None
        and window.engine.report.camera_mode
        and window.preview.image is not None,
        args.settle,
    ):
        report["error"] = "preview did not start: " + window.statusBar().currentMessage()
        return finish(2)

    engine = window.engine
    assert engine is not None
    report["before"] = {
        "camera_mode": engine.report.camera_mode,
        "frames": engine._frame_count,
        "probe_error": engine.report.camera_probe_error,
    }
    report["checks"]["preview_started"] = True
    report["screenshots"].append("before.png")
    _save(window, out_dir / "before.png")
    print(f"READY_FOR_UNPLUG device={device} mode={engine.report.camera_mode}",
          flush=True)

    # The operator unplugs the camera now.  The node disappearing is the first
    # signal; the pipeline error and rebuild follow within a few GUI ticks.
    unplug_started = time.monotonic()
    unplugged = pump_until(lambda: not device.exists(), args.unplug_timeout)
    report["unplug"] = {
        "detected": unplugged,
        "wait_s": round(time.monotonic() - unplug_started, 2)
        if unplugged else None,
    }
    report["checks"]["device_disappeared"] = unplugged
    if not unplugged:
        report["error"] = "the device node never disappeared (no physical unplug seen)"
        return finish(1)

    placeholder = pump_until(
        lambda: window.engine is not None
        and camera.id in window.engine.skip_layers
        and window.engine.report.camera_placeholders
        and window.engine._frame_count >= 3
        and window.preview.image is not None,
        args.settle,
    )
    current = window.engine
    report["placeholder"] = {
        "shown": bool(placeholder),
        "engine_present": current is not None,
        "lost_layers": sorted(window._lost_cameras),
        "camera_placeholders": list(current.report.camera_placeholders) if current else [],
        "frames": current._frame_count if current else 0,
        "error": current.report.error if current else window.statusBar().currentMessage(),
    }
    report["checks"]["placeholder_shown"] = bool(
        current is not None and camera.id in current.skip_layers
        and current.report.camera_placeholders)
    report["screenshots"].append("placeholder.png")
    _save(window, out_dir / "placeholder.png")
    print(f"PLACEHOLDER_READY lost={report['placeholder']['lost_layers']} "
          f"frames={report['placeholder']['frames']}", flush=True)

    # The operator plugs it back in.  Wait for the watcher to rebuild with the
    # real camera and for fresh composited frames.
    replug_started = time.monotonic()
    returned = pump_until(device.exists, args.reconnect_timeout)
    report["reconnect"] = {
        "device_returned": returned,
        "wait_s": round(time.monotonic() - replug_started, 2) if returned else None,
    }
    report["checks"]["device_returned"] = returned
    if not returned:
        report["error"] = "the device node did not return within the timeout"
        return finish(1)

    reconnected = pump_until(
        lambda: window.engine is not None
        and camera.id not in window._lost_cameras
        and camera.id not in window.engine.skip_layers
        and camera.id in window.engine._layer_pads
        and window.engine._frame_count >= 5,
        max(args.settle, 30.0),
    )
    current = window.engine
    report["after"] = {
        "reconnected": bool(reconnected),
        "camera_mode": current.report.camera_mode if current else "",
        "frames": current._frame_count if current else 0,
        "lost_layers": sorted(window._lost_cameras),
        "camera_placeholders": list(current.report.camera_placeholders) if current else [],
        "error": current.report.error if current else "",
    }
    report["checks"]["camera_reconnected"] = bool(reconnected)
    report["screenshots"].append("reconnected.png")
    _save(window, out_dir / "reconnected.png")
    print(f"RECONNECTED mode={report['after']['camera_mode']} "
          f"frames={report['after']['frames']}", flush=True)

    report["ok"] = all(report["checks"].values())
    return finish(0 if report["ok"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
