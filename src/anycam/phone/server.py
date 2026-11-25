"""The phone link: a small HTTPS server on the local network.

Routes (all JSON or SDP, no cookies):

* ``GET /`` and ``/phone.js``: the phone page.
* ``POST /api/pair`` ``{"token", "name"}`` -> ``{"device", "secret", "name"}``.
  The token comes from the QR code and works once, for five minutes.
* ``GET /api/check`` (Bearer) -> 200 while the phone is still paired.
* ``POST /api/whip`` (Bearer, ``application/sdp`` offer) -> 201 SDP answer.
* ``DELETE /api/whip/<session>`` (Bearer) ends that phone's stream.

The server runs on its own threads and imports no Qt.  It reports to the app
through ``on_event(event, device_id, name)`` only.
"""

from __future__ import annotations

import http.server
import io
import json
import secrets
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from . import PHONE_PORT
from .auth import Pairing, clean_name
from .tls import ensure_certificate, fingerprint, lan_address

MAX_BODY = 64 * 1024
MAX_CONNECTIONS = 32
HANDSHAKE_TIMEOUT_S = 15
REQUEST_DEADLINE_S = 10.0
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/phone.js": ("phone.js", "text/javascript; charset=utf-8"),
          "/remote": ("remote.html", "text/html; charset=utf-8"),
          "/remote.js": ("remote.js", "text/javascript; charset=utf-8")}

# What a paired phone may ask the remote page to do (everything else is refused).
CONTROL_ACTIONS = ("scene", "mute", "record")

EventHandler = Callable[[str, str, str], None]


def _format_host(host: str) -> str:
    """Bracket IPv6 literals for URLs."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _static(name: str) -> bytes:
    return resources.files(__package__).joinpath("static", name).read_bytes()


class _DeadlineRawIO(io.RawIOBase):
    """Readable socket stream capped by a wall-clock deadline (anti-Slowloris)."""

    def __init__(self, sock: ssl.SSLSocket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request deadline exceeded")
        self._sock.settimeout(remaining)
        try:
            return self._sock.recv_into(b)
        except ssl.SSLWantReadError:
            raise TimeoutError("request deadline exceeded") from None


class _TLSServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, context: ssl.SSLContext, service):
        self.address_family = (
            socket.AF_INET6 if ":" in str(address[0]) else socket.AF_INET
        )
        self.context = context
        self.service = service
        # One thread per connection, and the page is reachable by anything on
        # the network: bound them, so a flood of half-open connections cannot
        # exhaust threads and memory.  A phone needs a handful.
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)       # over the limit: drop it
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def finish_request(self, request, client_address):
        # The TLS handshake runs on the request's own thread: a phone that
        # stalls mid-handshake cannot block accept() for everyone else.
        request.settimeout(HANDSHAKE_TIMEOUT_S)
        try:
            tls = self.context.wrap_socket(request, server_side=True)
        except (OSError, ssl.SSLError):
            # Usually the phone refusing the self-signed certificate before
            # the user accepts it; nothing to answer.
            return
        try:
            super().finish_request(tls, client_address)
        finally:
            try:
                tls.close()
            except OSError:
                pass

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, ssl.SSLError)):
            return
        super().handle_error(request, client_address)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AnyCam"
    sys_version = ""
    server: _TLSServer

    def setup(self) -> None:
        super().setup()
        self._deadline = time.monotonic() + REQUEST_DEADLINE_S
        self.connection.settimeout(REQUEST_DEADLINE_S)
        # Replace rfile with a wall-clock-deadline reader so trickle clients
        # cannot reset socket timeouts indefinitely and hold all worker slots.
        self.rfile = io.BufferedReader(_DeadlineRawIO(self.connection, self._deadline))

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes = b"",
              content_type: str = "application/json",
              headers: dict[str, str] | None = None) -> None:
        self.connection.settimeout(REQUEST_DEADLINE_S)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.close_connection:
            self.send_header("Connection", "close")
        if content_type.startswith("text/html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; frame-ancestors 'none'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; media-src 'self' blob: mediastream:",
            )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)
        if self.close_connection:
            try:
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _body(self) -> tuple[bytes | None, int, str]:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return None, 411, "chunked transfer encoding is not supported"
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b"", 0, ""
        text = raw_length.strip()
        if not text or not text.isascii() or not text.isdigit():
            self.close_connection = True
            return None, 400, "invalid Content-Length"
        length = int(text)
        if length > MAX_BODY:
            # Never drain an oversized body into RAM: answer 413 and close.
            self.close_connection = True
            return None, 413, "request too large"
        if length == 0:
            return b"", 0, ""
        data = self.rfile.read(length)
        if len(data) < length:
            self.close_connection = True
            return None, 400, "incomplete request body"
        return data, 0, ""

    def _device(self) -> str | None:
        header = self.headers.get("Authorization") or ""
        scheme, _, credential = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return self.server.service.pairing.check(credential.strip())

    # ----------------------------------------------------------------- routes
    def do_HEAD(self):  # noqa: N802 - stdlib name
        self.do_GET()

    def do_GET(self):  # noqa: N802 - stdlib name
        path = self.path.split("?", 1)[0]
        if path == "/api/check":
            device = self._device()
            if device is None:
                return self._json(401, {"error": "not paired"})
            return self._json(200, {"device": device,
                                    "name": self.server.service.pairing.name(device)})
        if path == "/api/control/state":
            device = self._device()
            if device is None:
                return self._json(401, {"error": "not paired"})
            reply = self.server.service.control("state", {}, device)
            return self._json(200 if "error" not in reply else 403, reply)
        static = STATIC.get(path)
        if static is None:
            return self._send(404, b"not found", "text/plain")
        name, content_type = static
        return self._send(200, _static(name), content_type)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/api/pair", "/api/control", "/api/whip"):
            self.close_connection = True
            return self._json(404, {"error": "not found"})
        device: str | None = None
        if path in ("/api/control", "/api/whip"):
            device = self._device()
            if device is None:
                self.close_connection = True
                return self._json(401, {"error": "not paired"})
        body, err_status, err_msg = self._body()
        if body is None:
            return self._json(err_status, {"error": err_msg})
        service = self.server.service
        if path == "/api/pair":
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad request"})
            if not isinstance(data, dict):
                return self._json(400, {"error": "bad request"})
            paired = service.pairing.pair(data.get("token", ""), data.get("name"))
            if paired is None:
                return self._json(403, {
                    "error": "This QR code has expired or was already used. "
                             "Show a new one in AnyCam."})
            paired_device, secret = paired
            name = service.pairing.name(paired_device)
            service.emit("paired", paired_device)
            return self._json(200, {"device": paired_device, "secret": secret, "name": name})
        if path == "/api/control":
            assert device is not None
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad request"})
            action = data.get("action") if isinstance(data, dict) else None
            if action not in CONTROL_ACTIONS:
                return self._json(400, {"error": "unknown action"})
            reply = service.control(action, data, device)
            return self._json(200 if "error" not in reply else 403, reply)
        assert device is not None
        try:
            session, answer = service.start_stream(
                device, body.decode("utf-8"), require_paired=True)
        except (ValueError, UnicodeDecodeError) as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - report, keep serving
            print(f"[phone] {device}: {exc}", file=sys.stderr, flush=True)
            return self._json(503, {"error": "could not start the phone stream"})
        return self._send(201, answer.encode("utf-8"), "application/sdp",
                          {"Location": f"/api/whip/{session}"})

    def do_DELETE(self):  # noqa: N802
        device = self._device()
        if device is None:
            return self._json(401, {"error": "not paired"})
        path = self.path.split("?", 1)[0]
        prefix = "/api/whip/"
        if not path.startswith(prefix):
            return self._json(404, {"error": "not found"})
        self.server.service.stop_session(device, path[len(prefix):])
        return self._json(200, {"ok": True})


class PhoneService:
    """Pairing, the HTTPS page and one WebRTC receiver per connected phone."""

    def __init__(self, *, state_dir: Path, cert_dir: Path,
                 on_event: EventHandler | None = None,
                 host: str | None = None, port: int = PHONE_PORT) -> None:
        self.pairing = Pairing(Path(state_dir) / "phones.json")
        self.cert_dir = Path(cert_dir)
        self.on_event = on_event
        self.requested_host = host
        self.port = port
        self.host = ""
        self.fingerprint = ""
        self._httpd: _TLSServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopped = False
        self._forgotten: set[str] = set()
        self.receivers: dict[str, object] = {}
        self._sessions: dict[str, str] = {}
        # The remote page: off until the user allows it, then ``control_handler``
        # (set by the window) runs each request on the UI thread.
        self.control_enabled = False
        self.control_handler: Callable[[str, dict, str], dict] | None = None

    # -------------------------------------------------------------- lifecycle
    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> None:
        if self._httpd is not None:
            return
        host = self.requested_host or lan_address()
        cert, key = ensure_certificate(self.cert_dir, host)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        try:
            httpd = _TLSServer((host, self.port), _Handler, context, self)
        except OSError as exc:
            raise RuntimeError(
                f"the phone link cannot listen on {host}:{self.port} ({exc.strerror or exc})"
            ) from exc
        with self._lock:
            self._stopped = False
            self.host = host
            self.port = httpd.server_address[1]
            self.fingerprint = fingerprint(cert)
            self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="anycam-phone-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        with self._lock:
            receivers = list(self.receivers.values())
            self.receivers.clear()
            self._sessions.clear()
        for receiver in receivers:
            receiver.stop()

    # ---------------------------------------------------------------- pairing
    def pairing_url(self) -> str:
        """A fresh one-time pairing link for the QR code."""
        if self._httpd is None:
            raise RuntimeError("the phone link is not running")
        token = self.pairing.new_token()
        # The token rides in the fragment: the browser never sends it in a
        # request line, so it cannot end up in any log.
        return f"https://{_format_host(self.host)}:{self.port}/#pair={token}"

    def page_url(self) -> str:
        return f"https://{_format_host(self.host)}:{self.port}/"

    def emit(self, event: str, device: str, detail: str = "") -> None:
        if self.on_event is None:
            return
        name = self.pairing.name(device)
        try:
            self.on_event(event, device, detail or name)
        except Exception as exc:  # noqa: BLE001
            print(f"[phone] event handler failed: {exc}", file=sys.stderr, flush=True)

    def control(self, action: str, data: dict, device: str) -> dict:
        """Run a remote-control request; ``{"error": ...}`` when it is not allowed."""
        if not self.control_enabled:
            return {"error": "Remote control is switched off in AnyCam."}
        handler = self.control_handler
        if handler is None:
            return {"error": "AnyCam is not ready for remote control."}
        try:
            return handler(action, {key: data.get(key) for key in ("scene_id", "value")}, device)
        except Exception as exc:  # noqa: BLE001 - never a stack trace to a phone
            print(f"[phone] control {action}: {exc}", file=sys.stderr, flush=True)
            return {"error": "AnyCam could not do that."}

    def forget(self, device: str) -> None:
        with self._lock:
            self._forgotten.add(device)
        self._drop(device)
        self.pairing.forget(device)

    # ---------------------------------------------------------------- streams
    def _on_receiver_closed(self, device: str, receiver: object) -> None:
        with self._lock:
            if self.receivers.get(device) is receiver:
                del self.receivers[device]
                for key in [k for k, v in self._sessions.items() if v == device]:
                    del self._sessions[key]
        # Stop the GStreamer pipeline off its streaming/callback thread.
        threading.Thread(target=receiver.stop, name=f"anycam-phone-stop-{device}",
                         daemon=True).start()

    def start_stream(self, device: str, offer: str, *,
                     require_paired: bool = False) -> tuple[str, str]:
        from .webrtc import PhoneReceiver

        if "m=video" not in offer:
            raise ValueError("the offer has no video")
        with self._lock:
            if self._stopped or device in self._forgotten:
                raise RuntimeError("the phone link stopped or the phone was unpaired")
            was_running = self._httpd is not None
        was_paired = device in self.pairing.devices()
        if require_paired and not was_paired:
            raise RuntimeError("the phone is no longer paired")
        # One call per phone: a reconnect replaces the old receiver.
        self._drop(device)
        receiver = PhoneReceiver(
            device, on_event=lambda event, detail: self.emit(event, device, detail))
        receiver.on_closed = lambda r=receiver: self._on_receiver_closed(device, r)
        try:
            answer = receiver.answer(offer)
        except Exception:
            receiver.stop()
            raise
        session = secrets.token_urlsafe(12)
        # Swap under the lock and re-verify the service and pairing state so a
        # concurrent stop() or forget() cannot leave an orphaned receiver running.
        still_paired = device in self.pairing.devices()
        replaced = None
        abort = False
        with self._lock:
            if (self._stopped
                    or (was_running and self._httpd is None)
                    or device in self._forgotten
                    or ((was_paired or require_paired) and not still_paired)):
                abort = True
            else:
                replaced = self.receivers.get(device)
                self.receivers[device] = receiver
                for key in [key for key, value in self._sessions.items() if value == device]:
                    del self._sessions[key]
                self._sessions[session] = device
        if abort:
            receiver.stop()
            raise RuntimeError("the phone link stopped or the phone was unpaired")
        if replaced is not None and replaced is not receiver:
            replaced.stop()
        self.emit("connecting", device)
        return session, answer

    def stop_session(self, device: str, session: str) -> None:
        with self._lock:
            if self._sessions.get(session) != device:
                return
            receiver = self.receivers.pop(device, None)
            for key in [key for key, value in self._sessions.items() if value == device]:
                del self._sessions[key]
        if receiver is not None:
            receiver.stop()
        self.emit("stopped", device)

    def _drop(self, device: str) -> None:
        with self._lock:
            receiver = self.receivers.pop(device, None)
            for key in [key for key, value in self._sessions.items() if value == device]:
                del self._sessions[key]
        if receiver is not None:
            receiver.stop()

    def status(self, device: str) -> dict:
        with self._lock:
            receiver = self.receivers.get(device)
        if receiver is None:
            return {"connected": False, "frames": 0, "error": ""}
        return {"connected": not receiver.closed, "frames": receiver.frames,
                "last_frame": receiver.last_frame, "error": receiver.error}


def default_service(on_event: EventHandler | None = None) -> PhoneService:
    from .. import config

    return PhoneService(state_dir=config.state_dir(),
                        cert_dir=config.config_dir() / "phone",
                        on_event=on_event)


__all__ = ["PhoneService", "clean_name", "default_service"]
