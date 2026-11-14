"""Single-instance guard and the freeze-dump helper."""
from __future__ import annotations


import fcntl
import json
import os
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .. import config


INSTANCE_SERVER = f"anycam-gui-{os.getuid()}"


def _raise_window(window) -> None:
    window.showNormal()
    window.raise_()
    window.activateWindow()


_INSTANCE_LOCKS: dict[str, int] = {}


def _take_instance_lock(name: str) -> bool | None:
    """Take the per-user launch lock.

    ``True``: this process now owns it (held until exit; the kernel drops it
    even on a crash, so it can never be stale).  ``False``: another live
    process owns it.  ``None``: locking is unavailable here.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or str(config.state_dir())
    try:
        fd = os.open(os.path.join(base, f"{name}.lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    except OSError:
        os.close(fd)
        return None
    previous = _INSTANCE_LOCKS.pop(name, None)
    if previous is not None:
        os.close(previous)
    _INSTANCE_LOCKS[name] = fd
    return True


def _ask_instance_to_show(name: str, attempts: int, wait_ms: int) -> bool:
    """Ask the running GUI to raise its window; True once it was told."""
    for _ in range(max(1, attempts)):
        probe = QLocalSocket()
        probe.connectToServer(name)
        if probe.waitForConnected(wait_ms):
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(200)
            probe.disconnectFromServer()
            return True
        time.sleep(0.2)
    return False


def claim_gui_instance(name: str = INSTANCE_SERVER
                       ) -> tuple[bool, QLocalServer | None]:
    """Become the only AnyCam GUI, or forward the launch to the running one.

    Two windows mean two writers racing for the single v4l2loopback node; the
    losing writer leaves consumers (Discord) on a black camera.  A second
    launch therefore raises the existing window and exits instead of opening
    another one.

    Ownership is a file lock, not "nobody answered the socket within 500 ms":
    a first window that is busy (a rebuild can block its UI thread for seconds)
    used to look dead, so the second launch deleted its socket and started a
    competing writer.  A held lock means a live owner, however busy it is.
    """
    lock = _take_instance_lock(name)
    if lock is False:
        if not _ask_instance_to_show(name, attempts=12, wait_ms=500):
            print("[guard] AnyCam is already running but did not answer; "
                  "not starting a second window", file=sys.stderr, flush=True)
        return False, None
    if lock is None and _ask_instance_to_show(name, attempts=1, wait_ms=500):
        return False, None
    # We own the launch (or locking is unavailable and nobody answered): any
    # socket file left behind belongs to a crashed instance.
    QLocalServer.removeServer(name)
    server = QLocalServer()
    # Only this user may connect: the socket carries commands.
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        print(f"[guard] single-instance socket failed: {server.errorString()}",
              file=sys.stderr, flush=True)
        server.deleteLater()
        # The lock already guarantees there is no second writer; only the
        # "raise the existing window" convenience is lost.
        return (lock is True), None
    return True, server


MAX_REQUEST = 64 * 1024


class _Request:
    """One client of the instance socket: ``show`` raises the window, a JSON line runs an action."""

    def __init__(self, connection, window) -> None:
        self.connection, self.window = connection, window
        self.buffer = b""
        self.handled = False
        connection.readyRead.connect(self.read)
        connection.disconnected.connect(connection.deleteLater)
        QTimer.singleShot(2000, self.give_up)

    def read(self) -> None:
        if self.handled:
            return
        self.buffer += bytes(self.connection.readAll())
        text = self.buffer.lstrip()
        if text.startswith(b"show"):
            self.handled = True
            _raise_window(self.window)
            self.connection.disconnectFromServer()
        elif b"\n" in text or len(self.buffer) > MAX_REQUEST:
            self.handled = True
            reply = self.run(text.split(b"\n", 1)[0])
            self.connection.write(json.dumps(reply).encode() + b"\n")
            self.connection.flush()
            self.connection.disconnectFromServer()

    def run(self, line: bytes) -> dict:
        try:
            request = json.loads(line.decode("utf-8"))
        except ValueError:
            return {"ok": False, "error": "a request is one JSON object per line"}
        return self.window.ctl_dispatch(request)

    def give_up(self) -> None:
        if not self.handled:
            self.handled = True
            try:
                self.connection.disconnectFromServer()
            except RuntimeError:
                pass          # already deleted


def serve_instance_requests(server: QLocalServer, window) -> None:
    """Serve the instance socket: a second launch raises the window, ``anycam ctl`` runs actions."""
    def on_connection() -> None:
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            if connection is not None:
                window._instance_requests = [r for r in getattr(window, "_instance_requests", [])
                                             if not r.handled] + [_Request(connection, window)]

    server.newConnection.connect(on_connection)


def _register_freeze_dump() -> None:
    """``kill -USR1 <pid>`` writes every thread's Python stack to stderr.

    A frozen window cannot be inspected with gdb without root on systems with
    ``ptrace_scope=1``; this needs neither and changes nothing else.
    """
    try:
        import faulthandler
        import signal

        faulthandler.register(signal.SIGUSR1, all_threads=True)
    except (AttributeError, ValueError, OSError, RuntimeError):
        pass        # no usable stderr (e.g. started without a terminal)
