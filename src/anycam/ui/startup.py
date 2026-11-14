"""What the GUI does before the window exists and after it is gone.

``begin`` starts the log, writes the unclean-exit marker and decides about safe
mode; ``finish`` removes the marker.  The app ends with ``os._exit``, which runs
no exit handlers, so ``finish`` must be called explicitly.
"""
from __future__ import annotations

from .. import applog, config, crashguard, safemode


def begin(safe_mode: bool = False, ask: bool = True) -> bool:
    """Returns whether safe mode is on for this run."""
    applog.install()
    crash = crashguard.begin(config.VERSION)
    if crash is not None:
        applog.write(f"[guard] {crash.summary()}"
                     + (" (the computer restarted since)" if crash.rebooted else ""))
        if ask and not safe_mode:
            safe_mode = _ask_safe_mode(crash)
    if safe_mode:
        safemode.enable()
        applog.write("[guard] safe mode is on: CPU canvas, image preview, no virtual camera, "
                     "no phone link, no AI effects")
    else:
        from ..engine import encoders

        encoders.start_background_probe()    # a child process; the answer is cached
    return safe_mode


def _ask_safe_mode(crash) -> bool:
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("AnyCam did not quit cleanly")
    box.setText(crash.summary())
    box.setInformativeText(
        "Start in safe mode? It uses the CPU canvas and the image preview, holds no virtual "
        "camera, skips the phone link and the AI effects, and changes nothing in your project. "
        f"The log is at {applog.log_path()}.")
    safe = box.addButton("Start in safe mode", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Start normally", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is safe


def mark_ready() -> None:
    crashguard.stage("running")


def finish() -> None:
    crashguard.end()
