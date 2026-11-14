"""Did the last run end properly?

After the app takes its single-instance lock it writes ``running.json`` in the
state directory (pid, boot id, version, start time and the stage it is in).  A
clean quit removes it.  Because the lock guarantees one owner at a time, a
marker that is already there at start can only be left by a run that crashed,
was killed or lost power.  The stage in it says what that run was doing, so
the next start can offer safe mode with a reason.  Nothing leaves the machine.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

MARKER = "running.json"


@dataclass(frozen=True)
class Crash:
    stage: str
    version: str
    started: float
    pid: int
    rebooted: bool

    def summary(self) -> str:
        what = self.stage or "starting"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started)) if self.started else "earlier"
        return f"The last run (from {when}) did not quit cleanly. It was: {what}."


def marker_path() -> Path:
    return config.state_dir() / MARKER


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _write(data: dict) -> None:
    path = marker_path()
    temp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data))
        os.replace(temp, path)
    except OSError:
        pass


def previous_crash() -> Crash | None:
    """The marker an earlier run left, or ``None`` after a clean exit."""
    path = marker_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}   # an unreadable marker still means the run did not finish
    return Crash(stage=str(data.get("stage", "")), version=str(data.get("version", "")),
                 started=float(data.get("started", 0) or 0), pid=int(data.get("pid", 0) or 0),
                 rebooted=bool(data.get("boot_id")) and data.get("boot_id") != boot_id())


_state: dict = {}


def begin(version: str) -> Crash | None:
    """Call once, right after taking the instance lock.  Returns the last run's crash, if any."""
    crash = previous_crash()
    _state.clear()
    _state.update(pid=os.getpid(), boot_id=boot_id(), version=version,
                  started=time.time(), stage="starting")
    _write(_state)
    return crash


def stage(name: str) -> None:
    """Record what the app is about to do (cheap; call before a risky step)."""
    if not _state:
        return
    _state["stage"] = name
    _write(_state)


def end() -> None:
    """A clean exit: remove the marker (``os._exit`` runs no handlers, so call this)."""
    _state.clear()
    try:
        marker_path().unlink()
    except OSError:
        pass
