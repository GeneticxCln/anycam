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
import json
import secrets
import ssl
import sys
import threading
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from . import PHONE_PORT
from .auth import Pairing, clean_name
from .tls import ensure_certificate, fingerprint, lan_address

MAX_BODY = 64 * 1024
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/phone.js": ("phone.js", "text/javascript; charset=utf-8")}

EventHandler = Callable[[str, str, str], None]


def _static(name: str) -> bytes:
    return resources.files(__package__).joinpath("static", name).read_bytes()


class _TLSServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, context: ssl.SSLContext, service):
        self.context = context
        self.service = service
        super().__init__(address, handler)

    def finish_request(self, request, client_address):
        # The TLS handshake runs on the request's own thread: a phone that
        # stalls mid-handshake cannot block accept() for everyone else.
        request.settimeout(15)
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

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes = b"",
              content_type: str = "application/json",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; media-src 'self' blob: mediastream:")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            # Read (and drop) a moderately oversized body before answering
            # 413; closing with unread data resets the connection and the
            # client never sees the answer.
            if 0 < length <= 16 * MAX_BODY:
                self.rfile.read(length)
            self.close_connection = True
            return None
        return self.rfile.read(length) if length else b""

    def _device(self) -> str | None:
        header = self.headers.get("Authorization") or ""
        scheme, _, credential = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return self.server.service.pairing.check(credential.strip())

    # ----------------------------------------------------------------- routes
    def do_GET(self):  # noqa: N802 - stdlib name
        path = self.path.split("?", 1)[0]
        if path == "/api/check":
            device = self._device()
            if device is None:
                return self._json(401, {"error": "not paired"})
            return self._json(200, {"device": device,
                                    "name": self.server.service.pairing.name(device)})
        static = STATIC.get(path)
        if static is None:
            return self._send(404, b"not found", "text/plain")
        name, content_type = static
        return self._send(200, _static(name), content_type)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._body()
        if body is None:
            return self._json(413, {"error": "request too large"})
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
            device, secret = paired
            name = service.pairing.name(device)
            service.emit("paired", device)
            return self._json(200, {"device": device, "secret": secret, "name": name})
        if path == "/api/whip":
            device = self._device()
            if device is None:
                return self._json(401, {"error": "not paired"})
            try:
                session, answer = service.start_stream(device, body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - report, keep serving
                print(f"[phone] {device}: {exc}", file=sys.stderr, flush=True)
                return self._json(503, {"error": str(exc)})
            return self._send(201, answer.encode("utf-8"), "application/sdp",
                              {"Location": f"/api/whip/{session}"})
        return self._json(404, {"error": "not found"})

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
        self.receivers: dict[str, object] = {}
        self._sessions: dict[str, str] = {}

    # -------------------------------------------------------------- lifecycle
    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> None:
        if self._httpd is not None:
            return
        host = self.requested_host or lan_address()
        cert, key = ensure_certificate(self.cert_dir)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        try:
            httpd = _TLSServer((host, self.port), _Handler, context, self)
        except OSError as exc:
            raise RuntimeError(
                f"the phone link cannot listen on {host}:{self.port} ({exc.strerror or exc})"
            ) from exc
        self.host = host
        self.port = httpd.server_address[1]
        self.fingerprint = fingerprint(cert)
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="anycam-phone-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
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
        return f"https://{self.host}:{self.port}/#pair={token}"

    def page_url(self) -> str:
        return f"https://{self.host}:{self.port}/"

    def emit(self, event: str, device: str, detail: str = "") -> None:
        if self.on_event is None:
            return
        name = self.pairing.name(device)
        try:
            self.on_event(event, device, detail or name)
        except Exception as exc:  # noqa: BLE001
            print(f"[phone] event handler failed: {exc}", file=sys.stderr, flush=True)

    def forget(self, device: str) -> None:
        self._drop(device)
        self.pairing.forget(device)

    # ---------------------------------------------------------------- streams
    def start_stream(self, device: str, offer: str) -> tuple[str, str]:
        from .webrtc import PhoneReceiver

        if "m=video" not in offer:
            raise ValueError("the offer has no video")
        # One call per phone: a reconnect replaces the old receiver.
        self._drop(device)
        receiver = PhoneReceiver(
            device, on_event=lambda event, detail: self.emit(event, device, detail))
        try:
            answer = receiver.answer(offer)
        except Exception:
            receiver.stop()
            raise
        session = secrets.token_urlsafe(12)
        # Swap under the lock and stop whatever was replaced: two offers from
        # one phone at once both passed _drop() above, and the second
        # assignment overwrote the first receiver, which kept running untracked.
        with self._lock:
            replaced = self.receivers.get(device)
            self.receivers[device] = receiver
            for key in [key for key, value in self._sessions.items() if value == device]:
                del self._sessions[key]
            self._sessions[session] = device
        if replaced is not None and replaced is not receiver:
            replaced.stop()
        self.emit("connecting", device)
        return session, answer

    def stop_session(self, device: str, session: str) -> None:
        with self._lock:
            if self._sessions.get(session) != device:
                return
        self._drop(device)
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
