"""A log file that survives a crash.

``install()`` copies everything the app prints to stderr into
``anycam.log`` in the state directory (timestamped, with known secrets masked)
and points ``faulthandler`` at the same file, so a native crash in GStreamer or
a driver leaves every thread's Python stack behind.  The file is rotated at
512 KiB with one old copy (``anycam.log.1``) kept.  Nothing leaves the machine.

Text that GStreamer or a driver writes straight to file descriptor 2 does not
pass through Python and is not captured; it still reaches the terminal.
"""
from __future__ import annotations

import faulthandler
import io
import os
import sys
import threading
import time
from pathlib import Path

from . import config, credentials

LOG_NAME = "anycam.log"
MAX_BYTES = 512 * 1024

_lock = threading.Lock()
_handle = None
_path: Path | None = None
_installed = False


def log_path() -> Path:
    return config.state_dir() / LOG_NAME


def _mask(text: str) -> str:
    text = config.redact_text(text)
    for secret in credentials.known_secrets():
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def _open() -> None:
    """Open the log for appending, rotating a big old one first."""
    global _handle, _path
    path = log_path()
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(LOG_NAME + ".1"))
        _handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        os.chmod(path, 0o600)
        _path = path
    except OSError:
        _handle = None
        return
    try:
        faulthandler.enable(file=_handle, all_threads=True)
    except (RuntimeError, ValueError, OSError):
        pass


def write(line: str) -> None:
    """Append one timestamped line (used by the stderr copy and by callers)."""
    global _handle
    with _lock:
        if _handle is None:
            return
        try:
            _handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {_mask(line.rstrip())}\n")
            if _path is not None and _handle.tell() > MAX_BYTES * 2:
                _handle.close()
                _handle = None
                _open()
        except (OSError, ValueError):
            pass


class _Tee(io.TextIOBase):
    """Stands in for ``sys.stderr``: the terminal still gets everything."""

    def __init__(self, original) -> None:
        self._original = original
        self._pending = ""

    def write(self, text):
        try:
            self._original.write(text)
        except Exception:  # noqa: BLE001 - no terminal (started from a launcher)
            pass
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            if line.strip():
                write(line)
        return len(text)

    def flush(self):
        try:
            self._original.flush()
        except Exception:  # noqa: BLE001
            pass

    def fileno(self):
        return self._original.fileno()

    def isatty(self):
        try:
            return self._original.isatty()
        except Exception:  # noqa: BLE001
            return False

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")


def install() -> Path | None:
    """Start logging; returns the log's path (``None`` when it cannot be opened)."""
    global _installed
    if _installed:
        return _path
    _open()
    if _handle is None:
        return None
    _installed = True
    sys.stderr = _Tee(sys.stderr)
    write(f"--- AnyCam started (pid {os.getpid()}) ---")
    return _path


def uninstall() -> None:
    """For tests: restore stderr and close the file."""
    global _handle, _installed, _path
    if isinstance(sys.stderr, _Tee):
        sys.stderr = sys.stderr._original
    with _lock:
        if _handle is not None:
            try:
                faulthandler.disable()
                _handle.close()
            except (OSError, ValueError):
                pass
        _handle, _path, _installed = None, None, False


def tail(lines: int = 200) -> str:
    """The last lines of the log, for the Diagnostics tab's Copy button."""
    try:
        text = log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
