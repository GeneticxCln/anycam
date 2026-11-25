"""Optional Browser / Web Page layer source backed by QtWebEngineWidgets.

Renders live HTML/CSS/JS overlays (StreamElements, alerts, chat, scoreboards,
local HTML widgets) offscreen at 15-30 fps with transparent backgrounds and
optional custom CSS injection.

Because ``QWebEngineView`` must live on the Qt GUI thread while GStreamer pulls
frames on its streaming thread, a small ``QObject`` bridge owns the offscreen
view and refreshes a double-buffered RGBA ``numpy.ndarray`` on a ``QTimer``.
The GStreamer thread only reads the latest buffer under a lock.
"""
from __future__ import annotations

import json
import threading
from urllib.parse import urlsplit

import numpy as np

DEFAULT_WEB_FPS = 24


def available() -> bool:
    """True when PySide6's QtWebEngineWidgets module can be imported."""
    try:
        from PySide6 import QtWebEngineWidgets  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def validate_web_url(url: str) -> str:
    """Return "" if ``url`` is an allowed http(s) web-source URL, else an error."""
    cleaned = str(url or "").strip()
    if not cleaned:
        return "Web page URL cannot be empty"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in cleaned):
        return "Web page URL contains invalid whitespace or control characters"
    parts = urlsplit(cleaned)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return "Web page URL must start with http:// or https://"
    return ""


def _placeholder_frame(width: int, height: int, message: str) -> np.ndarray:
    """Render a clean translucent RGBA fallback card when WebEngine is absent."""
    from PIL import Image, ImageDraw

    w = max(2, int(width))
    h = max(2, int(height))
    img = Image.new("RGBA", (w, h), (16, 20, 26, 210))
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, w - 3, h - 3], outline=(94, 234, 212, 180), width=2)
    draw.text((16, max(12, h // 2 - 12)), message[:120], fill=(235, 242, 248, 255))
    return np.ascontiguousarray(np.asarray(img, dtype=np.uint8))


def _css_injection_js(css: str) -> str:
    """JavaScript snippet that injects or updates an AnyCam custom stylesheet."""
    payload = json.dumps(css)
    return (
        "(function(){"
        "var id='__anycam_custom_css__';"
        "var el=document.getElementById(id);"
        "if(!el){el=document.createElement('style');el.id=id;"
        "(document.head||document.documentElement).appendChild(el);}"
        f"el.textContent={payload};"
        "})();"
    )


class WebPageGenerator:
    """Thread-safe RGBA frame generator for a live web page layer."""

    def __init__(
        self,
        url: str,
        width: int,
        height: int,
        fps: int = DEFAULT_WEB_FPS,
        custom_css: str = "",
    ) -> None:
        self.url = str(url or "").strip()
        self.width = max(2, int(width))
        self.height = max(2, int(height))
        self.fps = max(1, min(30, int(fps or DEFAULT_WEB_FPS)))
        self.custom_css = str(custom_css or "")
        self._lock = threading.Lock()
        self._closed = False
        self._bridge = None
        problem = validate_web_url(self.url)
        if problem:
            self._latest = _placeholder_frame(self.width, self.height, problem)
            return
        if not available():
            self._latest = _placeholder_frame(
                self.width, self.height,
                "Web source needs QtWebEngine (PySide6-Addons)")
            return
        self._latest = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        self._start_bridge()

    def _start_bridge(self) -> None:
        from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, QTimer, QUrl, Slot
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtWidgets import QApplication
        from PySide6.QtWebEngineCore import QWebEngineScript
        from PySide6.QtWebEngineWidgets import QWebEngineView

        app = QApplication.instance()
        if app is None:
            self._latest = _placeholder_frame(
                self.width, self.height, "Web source requires a Qt GUI session")
            return

        gen = self

        class _Bridge(QObject):
            def __init__(self) -> None:
                super().__init__()
                self.view: QWebEngineView | None = None
                self.timer: QTimer | None = None
                self._started = False

            @Slot()
            def start_view(self) -> None:
                if self._started or gen._closed:
                    return
                self._started = True
                view = QWebEngineView()
                view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                view.setStyleSheet("background: transparent;")
                view.resize(gen.width, gen.height)
                page = view.page()
                page.setBackgroundColor(QColor(0, 0, 0, 0))
                if gen.custom_css:
                    script = QWebEngineScript()
                    script.setName("anycam-custom-css")
                    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
                    script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
                    script.setRunsOnSubFrames(False)
                    script.setSourceCode(_css_injection_js(gen.custom_css))
                    page.scripts().insert(script)
                view.load(QUrl(gen.url))
                view.show()
                self.view = view
                timer = QTimer(self)
                timer.setInterval(max(33, int(round(1000.0 / gen.fps))))
                timer.timeout.connect(self.capture)
                timer.start()
                self.timer = timer

            @Slot()
            def capture(self) -> None:
                if gen._closed or self.view is None:
                    return
                try:
                    pixmap = self.view.grab()
                except Exception:  # noqa: BLE001
                    return
                if pixmap.isNull():
                    return
                image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
                if image.width() != gen.width or image.height() != gen.height:
                    image = image.scaled(
                        gen.width, gen.height,
                        Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                w, h = image.width(), image.height()
                bpl = image.bytesPerLine()
                raw = bytes(image.constBits())[: bpl * h]
                arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, bpl)[:, : w * 4]
                frame = np.ascontiguousarray(arr.reshape(h, w, 4))
                with gen._lock:
                    gen._latest = frame

            @Slot()
            def reload_page(self) -> None:
                if self.view is not None and not gen._closed:
                    self.view.reload()

            @Slot(str)
            def apply_css(self, css: str) -> None:
                if self.view is not None and not gen._closed:
                    self.view.page().runJavaScript(_css_injection_js(css))

            @Slot()
            def shutdown(self) -> None:
                if self.timer is not None:
                    self.timer.stop()
                    self.timer = None
                if self.view is not None:
                    try:
                        self.view.stop()
                        self.view.close()
                        self.view.deleteLater()
                    except Exception:  # noqa: BLE001
                        pass
                    self.view = None
                self.deleteLater()

        bridge = _Bridge()
        gui_thread = app.thread()
        if QThread.currentThread() is gui_thread:
            self._bridge = bridge
            bridge.start_view()
        else:
            bridge.moveToThread(gui_thread)
            self._bridge = bridge
            QMetaObject.invokeMethod(
                bridge, "start_view", Qt.ConnectionType.QueuedConnection)

    def frame(self, _t: float) -> np.ndarray:
        """Return the most recent RGBA frame (H, W, 4 uint8)."""
        with self._lock:
            return self._latest

    def reload(self) -> None:
        """Reload the web page without rebuilding the GStreamer graph."""
        bridge = self._bridge
        if bridge is None or self._closed:
            return
        from PySide6.QtCore import QMetaObject, Qt

        QMetaObject.invokeMethod(
            bridge, "reload_page", Qt.ConnectionType.QueuedConnection)

    def set_css(self, css: str) -> None:
        """Update injected custom CSS live."""
        self.custom_css = str(css or "")
        bridge = self._bridge
        if bridge is None or self._closed:
            return
        from PySide6.QtCore import Q_ARG, QMetaObject, Qt

        QMetaObject.invokeMethod(
            bridge, "apply_css", Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, self.custom_css))

    def close(self) -> None:
        """Tear down the offscreen browser cleanly on the GUI thread."""
        self._closed = True
        bridge, self._bridge = self._bridge, None
        if bridge is None:
            return
        try:
            from PySide6.QtCore import QMetaObject, Qt, QThread
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None and QThread.currentThread() is app.thread():
                bridge.shutdown()
            elif app is not None:
                QMetaObject.invokeMethod(
                    bridge, "shutdown", Qt.ConnectionType.QueuedConnection)
        except Exception:  # noqa: BLE001
            pass
