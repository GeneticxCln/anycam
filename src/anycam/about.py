"""What the About tab shows: release, licence, runtime versions and credits."""
from __future__ import annotations

import platform
import sys

from . import config

RELEASE_TAG = f"v{config.VERSION}"
RELEASE_NAME = f"Version {config.VERSION.split('.')[0]}"
REPO_URL = "https://github.com/GeneticxCln/anycam"
LICENSE_NAME = "MIT"

# (what, who made it, licence)
CREDITS: tuple[tuple[str, str, str], ...] = (
    ("Emoji pictures", "Google Noto Emoji (pinned commit 99701e0)", "Apache-2.0"),
    ("Face detector", "YuNet, from the OpenCV Zoo", "Apache-2.0"),
    ("Person segmentation", "MediaPipe selfie segmentation (ONNX conversion)", "Apache-2.0"),
    ("Sample portrait", "NASA official portrait (preview only)", "Public domain"),
    ("Interface", "Qt for Python (PySide6)", "LGPL-3.0"),
    ("Media engine", "GStreamer", "LGPL-2.1+"),
)


def _gstreamer_version() -> str:
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        return Gst.version_string().replace("GStreamer ", "")
    except Exception:  # noqa: BLE001 - the About page must never fail to open
        return "not available"


def _module_version(name: str, attribute: str = "__version__") -> str:
    try:
        module = __import__(name)
        return str(getattr(module, attribute, "installed"))
    except Exception:  # noqa: BLE001
        return "not installed"


def headline() -> str:
    """``AnyCam v1`` — the short name used in the title of the About tab."""
    return f"{config.APP_NAME} v{config.VERSION.split('.')[0]}"


def release_line() -> str:
    """``Version 1  ·  release 1.3.0`` (the major number, then the full release)."""
    return f"{RELEASE_NAME}  ·  release {config.VERSION}"


def runtime_rows() -> list[tuple[str, str]]:
    """Versions of what this copy of AnyCam is running on."""
    return [
        ("Release", f"{RELEASE_TAG} ({RELEASE_NAME.lower()})"),
        ("Python", platform.python_version()),
        ("Qt (PySide6)", _module_version("PySide6")),
        ("GStreamer", _gstreamer_version()),
        ("onnxruntime", _module_version("onnxruntime")),
        ("System", f"{platform.system()} {platform.release()} ({platform.machine()})"),
        ("Interpreter", sys.executable),
    ]
