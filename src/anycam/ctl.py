"""``anycam ctl <action> [argument]``: drive the running window from a terminal or a key binding.

A plain Unix-socket client with no Qt import, so a compositor key binding gets an
answer quickly.  The window listens on the socket it already uses to raise itself
when a second copy is started (owner-only), and reads one JSON object per line:
``{"action": "record"}`` answers ``{"ok": true, "state": {...}}`` or
``{"ok": false, "error": "..."}``.

Exit codes: 0 done, 1 refused, 2 AnyCam is not running.
"""
from __future__ import annotations

import json
import os
import socket
import sys

TIMEOUT_S = 5.0
MAX_REPLY = 1 << 20


def socket_path(name: str | None = None) -> str:
    """Where the window's ``QLocalServer`` listens (Qt puts it in the temp directory)."""
    return os.path.join(os.environ.get("TMPDIR") or "/tmp", name or f"anycam-gui-{os.getuid()}")


def request(action: str, argument: str = "", path: str | None = None) -> dict:
    """Send one request; raises ``ConnectionError`` when nothing is listening."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(TIMEOUT_S)
    try:
        client.connect(path or socket_path())
    except OSError as exc:
        client.close()
        raise ConnectionError(str(exc)) from exc
    try:
        client.sendall(json.dumps({"action": action, "argument": argument}).encode() + b"\n")
        data = b""
        while b"\n" not in data and len(data) < MAX_REPLY:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        raise ConnectionError(str(exc)) from exc
    finally:
        client.close()
    try:
        return json.loads(data.split(b"\n", 1)[0].decode())
    except ValueError:
        return {"ok": False, "error": "AnyCam sent an answer that is not JSON"}


def main(action: str, argument: str = "") -> int:
    try:
        reply = request(action, argument)
    except ConnectionError:
        print("AnyCam is not running (nothing answers on its control socket).", file=sys.stderr)
        return 2
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1
