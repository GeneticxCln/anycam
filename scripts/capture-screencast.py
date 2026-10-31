#!/usr/bin/env python3
"""Capture one live xdg-desktop-portal ScreenCast frame.

The portal chooser is the permission UI: this script requests one monitor or
window, waits for the user to approve it, then grabs a single PipeWire frame
and writes a PNG plus a JSON report.  It is deliberately separate from the GUI
so the evidence can be regenerated without opening the studio.

Example:
    ./scripts/capture-screencast.py --out evidence/07_screen
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from anycam import portal  # noqa: E402


def _portal_version() -> str:
    """Return the ScreenCast interface version, or a useful error string."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = connection.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", ("org.freedesktop.portal.ScreenCast", "version")),
            GLib.VariantType.new("(v)"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        return str(reply.unpack()[0])
    except GLib.Error as exc:
        return f"unavailable: {exc.message}"


def _test_window_class():
    """Load the controlled window from the standalone helper script."""
    path = ROOT / "scripts" / "screencast-test-window.py"
    spec = importlib.util.spec_from_file_location("anycam_screencast_test_window", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TestWindow


def _find_window(title: str) -> dict | None:
    try:
        raw = subprocess.run(
            ["niri", "msg", "--json", "windows"], capture_output=True,
            text=True, timeout=3,
        )
        if raw.returncode != 0:
            return None
        return next((w for w in json.loads(raw.stdout)
                     if w.get("title") == title), None)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _focus_window(title: str) -> None:
    """Bring the controlled test window back to the foreground for capture."""
    window = _find_window(title)
    if window is None:
        return
    try:
        subprocess.run(
            ["niri", "msg", "action", "focus-window", "--id",
             str(window["id"])], capture_output=True, text=True, timeout=3,
        )
        time.sleep(0.4)
    except (OSError, subprocess.SubprocessError):
        return


def _isolate_window(title: str) -> None:
    """Put the test window on an empty niri workspace, away from other apps."""
    window = _find_window(title)
    if window is None:
        return
    try:
        raw = subprocess.run(
            ["niri", "msg", "--json", "workspaces"], capture_output=True,
            text=True, timeout=3,
        )
        if raw.returncode != 0:
            return
        empty = [w for w in json.loads(raw.stdout)
                 if w.get("active_window_id") is None]
        if not empty:
            return
        target = empty[-1]
        subprocess.run(
            ["niri", "msg", "action", "move-window-to-workspace",
             "--window-id", str(window["id"]), "--focus", "true",
             str(target["idx"])], capture_output=True, text=True, timeout=3,
        )
        subprocess.run(
            ["niri", "msg", "action", "focus-workspace", str(target["idx"])],
            capture_output=True, text=True, timeout=3,
        )
        time.sleep(0.4)
    except (OSError, subprocess.SubprocessError):
        return


def capture(out_path: Path, timeout: float, types: int = 3,
            no_target: bool = False, focus_title: str = "") -> dict:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    print("requesting ScreenCast; choose a monitor or window in the portal dialog...",
          flush=True)
    session = portal.request_screen_cast(timeout=timeout, types=types)
    if focus_title:
        _focus_window(focus_title)
        time.sleep(1.0)  # let the compositor present the newly focused workspace
        if os.environ.get("ANYCAM_SCREENCAST_DEBUG"):
            debug_path = out_path.parent / "debug-after-focus.png"
            subprocess.run(
                ["niri", "msg", "action", "screenshot-screen", "--write-to-disk",
                 "true", "--path", str(debug_path)], capture_output=True, text=True,
                timeout=5,
            )
            time.sleep(0.5)
    pipeline = None
    try:
        stream = session.streams[0]
        print(f"portal session ready: node={stream.node_id} "
              f"size={stream.width}x{stream.height}", flush=True)

        pipeline = Gst.Pipeline.new("anycam-screencast-capture")
        src = Gst.ElementFactory.make("pipewiresrc", "screen")
        if src is None:
            raise RuntimeError("pipewiresrc is not installed")
        if not no_target:
            src.set_property("target-object", str(stream.node_id))
            # GStreamer 1.28 still uses the deprecated path as the stream's
            # target id in some builds; setting both keeps the portal node
            # unambiguous.
            src.set_property("path", str(stream.node_id))
        if session.fd >= 0:
            src.set_property("fd", session.fd)
        src.set_property("do-timestamp", True)
        native = None
        convert = Gst.ElementFactory.make("videoconvert", "convert")
        caps = Gst.ElementFactory.make("capsfilter", "rgb")
        caps.set_property("caps", Gst.Caps.from_string(
            "video/x-raw,format=RGB"))
        sink = Gst.ElementFactory.make("appsink", "sink")
        if sink is None:
            raise RuntimeError("appsink is not installed")
        sink.set_property("max-buffers", 1)
        sink.set_property("drop", True)
        sink.set_property("sync", False)
        for element in (src, native, convert, caps, sink):
            if element is not None:
                pipeline.add(element)
        if not src.link(convert):
            raise RuntimeError("could not link the ScreenCast source")
        if not convert.link(caps) or not caps.link(sink):
            raise RuntimeError("could not link the ScreenCast capture pipeline")

        result = pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("the ScreenCast pipeline failed to start")

        sample = None
        deadline = time.monotonic() + max(1.0, timeout)
        settle_until = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            bus = pipeline.get_bus()
            message = bus.timed_pop_filtered(
                100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message is not None:
                if message.type == Gst.MessageType.ERROR:
                    error, debug = message.parse_error()
                    raise RuntimeError(f"{error.message}: {debug}")
                break
            fresh = sink.emit("try-pull-sample", 100 * Gst.MSECOND)
            if fresh is not None:
                sample = fresh
                if time.monotonic() >= settle_until:
                    break
        if sample is None:
            raise RuntimeError("no frame arrived from the ScreenCast stream")

        buffer = sample.get_buffer()
        structure = sample.get_caps().get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("could not map the ScreenCast frame")
        try:
            data = bytes(info.data)
        finally:
            buffer.unmap(info)
        from PIL import Image

        expected = width * height * 3
        if len(data) < expected:
            raise RuntimeError(f"short ScreenCast frame: {len(data)} < {expected}")
        image = Image.frombytes("RGB", (width, height), data[:expected])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(out_path)

        return {
            "ok": True,
            "file": str(out_path),
            "width": width,
            "height": height,
            "bytes": out_path.stat().st_size,
            "portal_version": _portal_version(),
            "session_handle": session.session_handle,
            "node_id": stream.node_id,
            "stream_source_size": [stream.width, stream.height],
            "source_type": stream.source_type,
            "fd_passed": session.fd >= 0,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session": os.environ.get("XDG_SESSION_TYPE", ""),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        }
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="evidence/07_screen",
                        help="directory for screen.png and capture.json")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="seconds to wait for the portal chooser/frame")
    parser.add_argument("--monitor-only", action="store_true",
                        help="restrict the portal chooser to monitors (types=1)")
    parser.add_argument("--window-only", action="store_true",
                        help="restrict the portal chooser to windows (types=2)")
    parser.add_argument("--no-target", action="store_true",
                        help="debug: let pipewiresrc use the remote's default node")
    parser.add_argument("--test-window", action="store_true",
                        help="show the controlled full-screen target window while capturing")
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    frame_path = out_dir / "screen.png"
    report_path = out_dir / "capture.json"
    app = None
    window = None
    if args.test_window:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication(sys.argv[:1])
        window = _test_window_class()()
        window.showFullScreen()
        window.raise_()
        window.activateWindow()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.02)
        _focus_window("AnyCam ScreenCast Test")
    try:
        types = 1 if args.monitor_only else 2 if args.window_only else 3
        try:
            report = capture(frame_path, args.timeout, types, args.no_target,
                             "AnyCam ScreenCast Test" if args.test_window else "")
        except Exception as exc:  # noqa: BLE001 - CLI reports the portal/capture failure
            report = {"ok": False, "error": str(exc), "file": str(frame_path)}
            report_path.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), file=sys.stderr, flush=True)
            return 1
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
        return 0
    finally:
        _close_window(app, window)


def _close_window(app, window) -> None:
    if window is not None:
        window.close()
    if app is not None:
        app.processEvents()
        app.quit()


if __name__ == "__main__":
    sys.exit(main())
