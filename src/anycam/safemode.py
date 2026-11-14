"""Safe mode: start with nothing that has crashed the app before.

Switched on by ``anycam gui --safe-mode`` or by answering the question that
follows an unclean exit.  It starts the CPU canvas and the image preview, holds
no virtual camera, does not start the preview by itself, leaves out the tray
icon, portal shortcuts and phone link, and passes pictures through the AI
effects untouched.

It only sets environment overrides and a flag in this process.  Nothing is
written to the project, so leaving safe mode (restart normally) brings back
every setting exactly as it was.
"""
from __future__ import annotations

import os

_active = False


def active() -> bool:
    return _active


def enable() -> None:
    """Switch on (once, before the window exists)."""
    global _active
    _active = True
    os.environ["ANYCAM_CANVAS"] = "cpu"
    os.environ["ANYCAM_PREVIEW"] = "image"


def disable() -> None:
    """For tests: forget the flag (environment overrides are the caller's to undo)."""
    global _active
    _active = False
