#!/usr/bin/env python3
"""Drive a real Firefox against the AnyCam virtual camera and save evidence.

The page under test (web/vcam-check.html) selects the AnyCam device by label,
opens it with getUserMedia, captures one composited frame and reports its
dimensions, frame rate and pixel statistics.  This script starts a throwaway
HTTP server, a headless Firefox controlled over Marionette, and an AnyCam
pipeline writing the loopback device, then collects the result plus
screenshots into the chosen output directory.

Example (v4l2loopback already loaded, see scripts/enable-v4l2loopback.sh):

    ./scripts/verify-firefox.py --out evidence/02_virtual_camera

Exit codes: 0 pass, 1 failed check, 2 Firefox missing, 3 virtual camera
unavailable.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from anycam import config  # noqa: E402

PAGE = "/vcam-check.html"
FIREFOX_PREFS = {
    # Grant camera access without a permission prompt, and allow autoplay.
    "media.navigator.permission.disabled": True,
    "media.navigator.streams.fake": False,
    "permission.default.camera": 1,
    "permission.default.microphone": 1,
    "media.autoplay.default": 0,
    "media.autoplay.blocking_policy": 0,
    # Use plain V4L2 rather than PipeWire camera portals.
    "media.webrtc.camera.allow-pipewire": False,
    "browser.shell.checkDefaultBrowser": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "toolkit.telemetry.enabled": False,
}


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def log_message(self, *_args):  # keep the console readable
        pass


class Marionette:
    """Minimal Marionette client: navigate, execute and screenshot."""

    def __init__(self, port: int, timeout: float = 30.0):
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.buffer = b""
        self.next_id = 0

    def connect(self, deadline: float) -> None:
        err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                break
            except OSError as exc:
                err = exc
                time.sleep(0.1)
        if self.sock is None:
            raise RuntimeError(f"cannot connect to Marionette on port {self.port}: {err}")
        self.sock.settimeout(0.5)
        self._read_hello(deadline)
        self.command("WebDriver:NewSession", {"capabilities": {}}, timeout=30)

    def _read_hello(self, deadline: float) -> None:
        while time.monotonic() < deadline:
            if self.buffer:
                length_text, sep, rest = self.buffer.partition(b":")
                if sep and len(rest) >= int(length_text):
                    payload, self.buffer = rest[: int(length_text)], rest[int(length_text):]
                    if "marionetteProtocol" in json.loads(payload):
                        return
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise RuntimeError("Marionette closed the connection during hello")
            self.buffer += chunk
        raise RuntimeError("timed out waiting for the Marionette hello")

    def _recv_message(self, deadline: float) -> list:
        while time.monotonic() < deadline:
            if self.buffer:
                length_text, sep, rest = self.buffer.partition(b":")
                if sep:
                    try:
                        length = int(length_text)
                    except ValueError:
                        length = -1
                    if length >= 0 and len(rest) >= length:
                        self.buffer = rest[length:]
                        return json.loads(rest[:length])
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise RuntimeError("Marionette closed the connection")
            self.buffer += chunk
        raise RuntimeError("timed out waiting for a Marionette reply")

    def command(self, name: str, params: dict | None = None, timeout: float | None = None) -> dict:
        self.next_id += 1
        ident = self.next_id
        payload = json.dumps([0, ident, name, params or {}]).encode()
        self.sock.sendall(f"{len(payload)}:".encode() + payload)
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            reply = self._recv_message(deadline)
            if reply[0] == 1 and reply[1] == ident:
                if reply[2]:
                    raise RuntimeError(f"{name} failed: {reply[2]}")
                return reply[3]

    def execute(self, script: str, timeout: float = 20) -> object:
        reply = self.command(
            "WebDriver:ExecuteScript",
            {"script": script, "args": []},
            timeout=timeout,
        )
        return reply.get("value")

    def navigate(self, url: str, timeout: float = 30) -> None:
        self.command("WebDriver:Navigate", {"url": url}, timeout=timeout)

    def screenshot(self, path: Path) -> None:
        reply = self.command("WebDriver:TakeScreenshot", {"full": False}, timeout=30)
        path.write_bytes(base64.b64decode(reply["value"]))

    def close(self) -> None:
        try:
            self.command("WebDriver:DeleteSession", timeout=5)
        except (RuntimeError, OSError):
            pass
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_port(port: int, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def make_colorbars(path: Path, width: int, height: int) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    colors = [
        (235, 235, 235), (235, 235, 16), (16, 235, 235), (16, 235, 16),
        (235, 16, 235), (235, 16, 16), (16, 16, 235), (16, 16, 16),
    ]
    band = width // len(colors)
    for index, color in enumerate(colors):
        draw.rectangle([index * band, 0, (index + 1) * band - 1, height], fill=color)
    draw.text((width // 2 - 60, height // 2 - 8), "AnyCam color bars", fill=(0, 0, 0))
    image.save(path)


def firefox_version(binary: str) -> str:
    result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    return (result.stdout or result.stderr).strip() or "unknown"


def loopback_ok(device: str) -> bool:
    if not Path(device).exists():
        return False
    code, out = config.run(["v4l2-ctl", "-d", device, "--info"])
    return code == 0 and "loopback" in out.lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="evidence/02_virtual_camera",
                        help="artifact directory (default: evidence/02_virtual_camera)")
    parser.add_argument("--device", default="/dev/video10")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=45.0,
                        help="seconds to wait for the page result")
    parser.add_argument("--seconds", type=int, default=120,
                        help="lifetime of the AnyCam feed")
    parser.add_argument("--firefox", default="firefox")
    args = parser.parse_args(argv)

    binary = shutil.which(args.firefox)
    if not binary:
        print(f"firefox not found ({args.firefox}); pass --firefox PATH", file=sys.stderr)
        return 2
    if not loopback_ok(args.device):
        print(f"{args.device} is not a v4l2loopback device; run "
              f"scripts/enable-v4l2loopback.sh first", file=sys.stderr)
        return 3

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    source = out / "firefox-source.png"
    make_colorbars(source, args.width, args.height)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["ANYCAM_VCAM"] = args.device
    cam_log = out / "firefox-anycam.log"
    cam_frame = out / "firefox-composite.png"
    python = ROOT / ".venv/bin/python"
    cam_args = [
        str(python) if python.exists() else sys.executable, "-m", "anycam", "demo",
        "--seconds", str(args.seconds), "--source", str(source),
        "--width", str(args.width), "--height", str(args.height), "--fps", str(args.fps),
        "--vcam", "--shot", str(cam_frame),
    ]
    with cam_log.open("w") as log_handle:
        cam = subprocess.Popen(cam_args, cwd=str(ROOT), stdout=log_handle,
                               stderr=subprocess.STDOUT, env=env, text=True)
    print(f"AnyCam feed: {args.device} {args.width}x{args.height}@{args.fps} "
          f"from {source.name}")
    time.sleep(4)
    if cam.poll() is not None:
        print(f"AnyCam exited while publishing to {args.device}; see {cam_log}",
              file=sys.stderr)
        _tail(cam_log)
        return 3

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{server.server_port}{PAGE}"

    profile = Path(tempfile.mkdtemp(prefix="anycam-firefox-"))
    port = free_port()
    prefs = dict(FIREFOX_PREFS, **{"marionette.port": port})
    (profile / "user.js").write_text("\n".join(
        f'user_pref("{key}", {json.dumps(value)});' for key, value in prefs.items()
    ) + "\n")
    browser_log_path = out / "firefox-browser.log"
    browser_log = browser_log_path.open("w")
    browser_env = {**env, "MOZ_HEADLESS": "1"}
    browser = subprocess.Popen(
        [binary, "--headless", "--marionette", "--no-remote",
         "--profile", str(profile), "about:blank"],
        stdout=browser_log, stderr=subprocess.STDOUT, env=browser_env,
    )

    result: dict | None = None
    screen: Path | None = None
    frame: Path | None = None
    version = firefox_version(binary)
    try:
        if not wait_port(port, timeout=30):
            raise RuntimeError("Firefox did not open the Marionette port")
        driver = Marionette(port)
        driver.connect(time.monotonic() + 30)
        print(f"Firefox: {version}")
        driver.navigate(page_url)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            value = driver.execute("return window.__result;")
            result = value if isinstance(value, dict) else None
            if result and result.get("state") in {"done", "error"}:
                break
            if browser.poll() is not None:
                raise RuntimeError("Firefox exited before the check finished")
            time.sleep(0.3)
        if not result or result.get("state") not in {"done", "error"}:
            raise RuntimeError(f"timed out after {args.timeout:.0f}s waiting for the page result")
        time.sleep(0.4)
        screen = out / "firefox.png"
        driver.screenshot(screen)
        frame_data = driver.execute("return window.__frame || null;")
        if frame_data:
            frame = out / "firefox-frame.png"
            frame.write_bytes(base64.b64decode(frame_data.split(",", 1)[-1]))
        driver.close()
    except Exception as exc:  # noqa: BLE001 - any browser failure is a failed check
        result = {"state": "error", "error": str(exc)}
        print(f"check failed: {exc}", file=sys.stderr)
    finally:
        browser.terminate()
        try:
            browser.wait(timeout=10)
        except subprocess.TimeoutExpired:
            browser.kill()
        browser_log.close()
        server.shutdown()
        server.server_close()
        cam.terminate()
        try:
            cam.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cam.kill()
        shutil.rmtree(profile, ignore_errors=True)
    if cam.returncode not in (0, -15) and not result:
        _tail(cam_log)

    fps = (result or {}).get("fps") or 0
    checks = {
        "anycam_enumerated": bool((result or {}).get("found")),
        "playing": (result or {}).get("state") == "done",
        "resolution": ((result or {}).get("width") == args.width
                       and (result or {}).get("height") == args.height),
        "frame_rate": fps >= args.fps * 0.8,
        "frame_has_content": ((result or {}).get("std") or 0) > 10,
    }
    ok = all(checks.values())

    report = {
        "ok": ok,
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "firefox": {"version": version, "path": binary},
        "anycam": {"device": args.device, "width": args.width, "height": args.height,
                   "fps": args.fps, "source": str(source), "canvas": cam_frame.name},
        "page": page_url,
        "result": result,
        "artifacts": {"screenshot": screen.name if screen else None,
                      "frame": frame.name if frame else None,
                      "anycam_log": cam_log.name, "browser_log": browser_log_path.name},
    }
    (out / "firefox-report.json").write_text(json.dumps(report, indent=2) + "\n")

    lines = [
        f"AnyCam Firefox virtual-camera check — {'PASS' if ok else 'FAIL'}",
        report["timestamp"],
        f"Firefox {version}",
        f"Page: {page_url}",
        f"AnyCam feed: {args.device} {args.width}x{args.height}@{args.fps} "
        f"from {source.name}",
    ]
    if result and result.get("devices"):
        lines.append("Video inputs: " + "; ".join(result["devices"]))
    if result and result.get("state") == "done":
        lines.append(f"Selected: {result.get('label')} "
                     f"{result.get('width')}x{result.get('height')}@{result.get('fps')}")
        lines.append(f"Captured frame: mean={result.get('mean')} std={result.get('std')} "
                     f"canvas={result.get('canvas')}")
    elif result:
        lines.append(f"Error: {result.get('error')}")
    lines.append("Checks: " + ", ".join(f"{name}={'ok' if value else 'FAIL'}"
                                        for name, value in checks.items()))
    lines.append(f"Artifacts: {screen.name if screen else '-'}, "
                 f"{frame.name if frame else '-'}, firefox-report.json")
    lines.append("Command: " + " ".join(sys.argv))
    (out / "firefox.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if ok else 1


def _tail(path: Path, count: int = 15) -> None:
    try:
        lines = path.read_text(errors="replace").splitlines()[-count:]
    except OSError:
        return
    for line in lines:
        print(f"  {path.name}: {line}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
