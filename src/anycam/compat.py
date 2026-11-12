"""Browser compatibility harness for the virtual camera.

Runs ``web/vcam-check.html`` in each browser it can find and records what the
browser saw, so "works in Chrome/Firefox/..." is a table of measured results
rather than a claim.  Chromium-family browsers are driven with ``--dump-dom``;
Firefox goes through ``scripts/verify-firefox.py``.  ``fake=True`` gives
Chromium its built-in test camera instead of the AnyCam device: it proves the
harness and the browser work, and is reported as such, never as AnyCam support.
"""
from __future__ import annotations

import http.server
import json
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
CHROMIUM_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                  "chrome", "brave-browser", "microsoft-edge", "vivaldi")
PLAYWRIGHT_ROOTS = ("/opt/pw-browsers",)


@dataclass
class Outcome:
    browser: str
    binary: str
    mode: str                      # "anycam" or "fake-device" (harness self-test)
    state: str = "not run"         # ok | failed | error | not found | not run
    detail: str = ""
    devices: list[str] = field(default_factory=list)
    resolution: str = ""
    version: str = ""


def find_chromium_family() -> list[tuple[str, str]]:
    """[(name, binary)] of the Chromium-family browsers installed here."""
    found = []
    for name in CHROMIUM_NAMES:
        path = shutil.which(name)
        if path:
            found.append((name, path))
    for root in PLAYWRIGHT_ROOTS:
        for path in sorted(Path(root).glob("chromium-*/chrome-linux*/chrome")):
            found.append(("chromium (playwright)", str(path)))
    seen, unique = set(), []
    for name, path in found:
        real = str(Path(path).resolve())
        if real not in seen:
            seen.add(real)
            unique.append((name, path))
    return unique


def classify(result: dict, fake: bool) -> tuple[str, str, str]:
    """(state, detail, resolution) from the page's ``window.__result``."""
    state = result.get("state", "")
    if state == "done":
        resolution = f"{result.get('width')}x{result.get('height')}"
        if result.get("fps"):
            resolution += f" @{result['fps']:g}"
        return "ok", f"opened {result.get('label', 'AnyCam')} (mean {result.get('mean')}, std {result.get('std')})", resolution
    devices = result.get("devices") or []
    if fake and state == "error" and devices and not result.get("found"):
        # The page wants AnyCam by name; the test camera is not it.  The browser ran,
        # asked for permission and listed a camera, which is all a self-test can show.
        return "ok", f"browser ran and listed {len(devices)} test camera(s)", ""
    if state == "error":
        return "failed", str(result.get("error", "the check failed")), ""
    return "error", "the page never finished (the browser blocked the camera or timed out)", ""


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


def serve_web() -> tuple[http.server.ThreadingHTTPServer, str]:
    """A throwaway server for ``web/`` (localhost counts as a secure origin)."""
    handler = lambda *a, **k: _Quiet(*a, directory=str(WEB), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/vcam-check.html"


class _Cdp:
    """A minimal Chrome DevTools Protocol client over a WebSocket (standard library only)."""

    def __init__(self, ws_url: str, timeout: float = 15.0) -> None:
        import base64
        import os
        import socket
        from urllib.parse import urlsplit

        parts = urlsplit(ws_url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise OSError("DevTools closed the connection")
            header += chunk
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise OSError("DevTools refused the WebSocket")
        self.buffer = header.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    def _read(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("DevTools closed the connection")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def _send(self, text: str) -> None:
        import os
        import struct

        payload = text.encode()
        mask = os.urandom(4)
        length = len(payload)
        head = bytes([0x81])
        if length < 126:
            head += bytes([0x80 | length])
        elif length < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", length)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _receive(self) -> str:
        import struct

        message = b""
        while True:
            first, second = self._read(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)
            if first & 0x0F in (1, 0):
                message += payload
                if first & 0x80:
                    return message.decode()

    def call(self, method: str, **params):
        self.next_id += 1
        self._send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            reply = json.loads(self._receive())
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise OSError(str(reply["error"]))
                return reply.get("result", {})

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def run_chromium(name: str, binary: str, url: str, *, fake: bool, timeout: float = 60.0) -> Outcome:
    """Open the check page in a Chromium-family browser and read ``window.__result``."""
    import tempfile
    import time
    import urllib.request

    outcome = Outcome(name, binary, "fake-device" if fake else "anycam")
    try:
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        outcome.version = version.stdout.strip().splitlines()[0] if version.stdout.strip() else ""
    except (OSError, subprocess.TimeoutExpired):
        pass
    profile = tempfile.mkdtemp(prefix="anycam-compat-")
    command = [binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
               "--use-fake-ui-for-media-stream", "--remote-debugging-port=0",
               f"--user-data-dir={profile}"]
    if fake:
        command.append("--use-fake-device-for-media-stream")
    command.append(url)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    cdp = None
    try:
        port = 0
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not port:
            line = process.stderr.readline()
            match = re.search(r"DevTools listening on ws://127\.0\.0\.1:(\d+)/", line)
            if match:
                port = int(match.group(1))
            elif not line and process.poll() is not None:
                break
        if not port:
            outcome.state, outcome.detail = "error", "the browser did not start"
            return outcome
        page_ws = ""
        while time.monotonic() < deadline and not page_ws:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                for target in json.load(response):
                    if target.get("type") == "page" and "vcam-check" in target.get("url", ""):
                        page_ws = target["webSocketDebuggerUrl"]
            if not page_ws:
                time.sleep(0.2)
        if not page_ws:
            outcome.state, outcome.detail = "error", "the check page did not open"
            return outcome
        cdp = _Cdp(page_ws)
        end = time.monotonic() + timeout
        result: dict = {}
        while time.monotonic() < end:
            result = json.loads(cdp.evaluate("JSON.stringify(window.__result || {})") or "{}")
            if result.get("state") in ("done", "error"):
                break
            time.sleep(0.4)
        outcome.devices = [str(item) for item in result.get("devices", [])]
        outcome.state, outcome.detail, outcome.resolution = classify(result, fake)
    except (OSError, ValueError) as exc:
        outcome.state, outcome.detail = "error", str(exc)
    finally:
        if cdp is not None:
            cdp.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return outcome


def run_firefox(url_script: Path = ROOT / "scripts" / "verify-firefox.py", out: Path | None = None) -> Outcome:
    binary = shutil.which("firefox")
    outcome = Outcome("firefox", binary or "", "anycam")
    if not binary:
        outcome.state, outcome.detail = "not found", "firefox is not installed"
        return outcome
    out = out or Path("evidence") / "compat-firefox"
    result = subprocess.run([str(url_script), "--out", str(out)], capture_output=True, text=True)
    codes = {0: "ok", 1: "failed", 2: "not found", 3: "failed"}
    outcome.state = codes.get(result.returncode, "error")
    outcome.detail = (result.stdout.strip().splitlines() or [""])[-1][:200]
    return outcome


def matrix(fake: bool = False, include_firefox: bool = True) -> list[Outcome]:
    server, url = serve_web()
    try:
        rows = [run_chromium(name, binary, url, fake=fake) for name, binary in find_chromium_family()]
    finally:
        server.shutdown()
    if include_firefox and not fake:
        rows.append(run_firefox())
    if not rows:
        rows.append(Outcome("(none)", "", "fake-device" if fake else "anycam", "not found",
                            "no supported browser is installed"))
    return rows


def as_markdown(rows: list[Outcome]) -> str:
    lines = ["| Browser | Mode | Result | Detail |", "|---|---|---|---|"]
    for row in rows:
        detail = (row.resolution + " " if row.resolution else "") + row.detail
        lines.append(f"| {row.browser} {row.version} | {row.mode} | {row.state} | "
                     f"{detail.strip().replace('|', '/')} |")
    return "\n".join(lines)


def to_json(rows: list[Outcome]) -> str:
    return json.dumps([asdict(row) for row in rows], indent=2)
