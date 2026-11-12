"""GPU preview: the canvas drawn by ``qml6glsink`` inside a ``QQuickWidget``.

The default preview copies every frame into Python, wraps it in a QImage and
paints it on the GUI thread.  Here GStreamer uploads the frame to a texture
that Qt Quick draws, so the GUI thread only composites.  Measured on the
reference machine at 1080p60 (prototype, same source): main thread 2-3%
against 8-9%, whole process 18% against 71%.

Requirements, all checked by ``available()``:

* the ``qml6glsink`` element (``gst-plugin-qml6`` on Arch), built against the
  **same** Qt as PySide6.  pip's PySide6 bundles its own Qt, which the system
  plugin refuses to load against (``version 'Qt_6.12' not found``); the
  distribution's ``pyside6`` package matches;
* QtQuickWidgets and an OpenGL Qt Quick backend;
* a real windowing platform (not ``offscreen``, which the tests use).

``ANYCAM_PREVIEW=image`` forces the default preview, ``ANYCAM_PREVIEW=gpu``
skips nothing but the platform check.
"""

from __future__ import annotations

import ctypes
import os
import sys
from functools import lru_cache
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

QML = """
import QtQuick
import org.freedesktop.gstreamer.Qt6GLVideoItem
Rectangle {
    id: root
    // The view stays visible (Qt Quick only creates the GL context the sink
    // shares while it is shown); ``live`` swaps the video for the idle text.
    property bool live: false
    color: "#07090b"
    GstGLQt6VideoItem {
        objectName: "anycamVideo"
        anchors.fill: parent
        forceAspectRatio: true
        visible: root.live
    }
    Text {
        visible: !root.live
        anchors.centerIn: parent
        horizontalAlignment: Text.AlignHCenter
        text: "Preview stopped\nAnyCam"
        color: "#41525c"
        font.pointSize: 13
    }
}
"""


def _log(message: str) -> None:
    print(f"[gpu-preview] {message}", file=sys.stderr, flush=True)


def requested() -> str:
    """``ANYCAM_PREVIEW``: "image", "gpu" or "" (automatic)."""
    value = os.environ.get("ANYCAM_PREVIEW", "").strip().lower()
    return value if value in ("image", "gpu") else ""


@lru_cache(maxsize=1)
def unavailable_reason() -> str:
    """Why the GPU preview cannot run here; empty when it can.

    Creating one ``qml6glsink`` also registers the QML item type, which must
    happen before any QML that imports it is loaded.
    """
    if requested() == "image":
        return "ANYCAM_PREVIEW=image"
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQuickWidgets import QQuickWidget  # noqa: F401
    except ImportError as exc:
        return f"QtQuickWidgets is missing ({exc})"
    app = QGuiApplication.instance()
    platform = app.platformName() if app is not None else ""
    if platform in ("offscreen", "minimal", ""):
        return f"no window system ({platform or 'no QApplication'})"
    Gst.init(None)
    if Gst.ElementFactory.find("qml6glsink") is None:
        return "qml6glsink is not installed (gst-plugin-qml6)"
    if Gst.ElementFactory.make("qml6glsink", None) is None:
        # The plugin exists but did not load: almost always a Qt mismatch
        # between PySide6 and the plugin (GStreamer logs the exact symbol).
        return ("qml6glsink did not load; PySide6 and gst-plugin-qml6 must use "
                "the same Qt (use the distribution's pyside6, not pip's)")
    return ""


def available() -> bool:
    return not unavailable_reason()


def use_opengl_scene_graph() -> None:
    """Pin Qt Quick to OpenGL; qml6glsink shares Qt's GL context.

    Must run before the first Qt Quick window exists.
    """
    from PySide6.QtQuick import QQuickWindow, QSGRendererInterface

    QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.OpenGL)


def _qml_file() -> Path:
    """The QML scene, in the user's own cache folder.

    It used to go to a predictable name in /tmp: another local user could
    create that file first (the write then raised and the window failed to
    open) or plant a symlink there for AnyCam to overwrite.
    """
    from . import config

    path = config.cache_dir() / "gpu-preview.qml"
    if path.is_file() and not path.is_symlink():
        try:
            if path.read_text(encoding="utf-8") == QML:
                return path
        except OSError:
            pass
    temporary = path.with_name(f".gpu-preview-{os.getpid()}.qml")
    temporary.write_text(QML, encoding="utf-8")
    temporary.replace(path)        # replaces a symlink itself, never its target
    return path


def _pointer_of(obj) -> int:
    """The C pointer behind a PyGObject object (its ``__gpointer__`` capsule)."""
    get = ctypes.pythonapi.PyCapsule_GetPointer
    get.restype = ctypes.c_void_p
    get.argtypes = [ctypes.py_object, ctypes.c_char_p]
    return int(get(obj.__gpointer__, None))


@lru_cache(maxsize=1)
def _g_object_set():
    gobject = ctypes.CDLL("libgobject-2.0.so.0")
    function = gobject.g_object_set
    function.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p]
    function.restype = None
    return function


def make_video_view(parent):
    """A ``QQuickWidget`` showing one ``GstGLQt6VideoItem``, or None."""
    from PySide6.QtCore import QUrl
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQuickWidgets import QQuickWidget

    try:
        qml = _qml_file()
    except OSError as exc:
        # The image preview takes over; the window must still open.
        _log(f"could not write the QML scene: {exc}")
        return None
    view = QQuickWidget(parent)
    view.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    view.setSource(QUrl.fromLocalFile(str(qml)))
    if view.status() != QQuickWidget.Status.Ready:
        _log("QML failed: " + "; ".join(error.toString() for error in view.errors()))
        view.deleteLater()
        return None
    item = view.rootObject().findChild(QQuickItem, "anycamVideo")
    if item is None:
        _log("the video item is missing from the QML scene")
        view.deleteLater()
        return None
    view.video_item = item
    return view


def view_ready(view) -> bool:
    """True once the Quick view has a live scene graph and a shown window.

    ``qml6glsink`` takes Qt's GL context from the item's window.  Attaching
    while that context is still being created used to build a GPU canvas that
    reported ``preview_backend == "gpu"``, raised no error, and never produced
    a frame (measured on the reference machine; the mixer then sat waiting for
    a context that never arrived).  A later scene switch could block stopping
    those layers.
    """
    try:
        if not view.isVisible():
            return False
        window = view.quickWindow() if hasattr(view, "quickWindow") else None
        if window is None:
            # Not a QQuickWidget (or a test double): nothing more to check.
            return True
        # A QQuickWidget renders into its own offscreen QQuickWindow, which is
        # therefore never "visible" or "exposed"; only the scene graph tells
        # whether the GL context the sink shares exists yet.
        return bool(window.isSceneGraphInitialized())
    except Exception:  # noqa: BLE001 - the image preview takes over
        return False


def attach_sink(sink: Gst.Element, view) -> bool:
    """Point ``qml6glsink``'s ``widget`` property at the view's video item.

    PyGObject cannot pass a C++ object to a ``gpointer`` property, so the
    pointer goes through ``g_object_set`` directly.
    """
    try:
        import shiboken6

        item = getattr(view, "video_item", None)
        if item is None or not shiboken6.isValid(item):
            return False
        # The sink takes Qt's GL context from the item's window, which Qt
        # Quick creates once the (visible) view has rendered; without it the
        # pipeline fails to start ("failed to retrieve wrapped context").
        if not view.isVisible() or not view_ready(view):
            return False
        _g_object_set()(_pointer_of(sink), b"widget",
                        int(shiboken6.getCppPointer(item)[0]), None)
    except Exception as exc:  # noqa: BLE001 - the image preview takes over
        _log(f"could not attach the sink: {exc}")
        return False
    return sink.get_property("widget") is not None
