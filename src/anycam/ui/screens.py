"""Portal screen captures."""
from __future__ import annotations


import os
import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QMessageBox,
    QProgressDialog,
)

from .. import config, portal
from ..model import (Layer, new_id)
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class ScreenMixin:
    """Portal screen captures."""

    def _request_screen_cast(self, restore_token: str = "") -> portal.ScreenSession:
        """Open the system picker without freezing the Qt event loop.

        ``restore_token`` (from an earlier launch) lets the portal skip the
        picker for the same source; it is only passed when there is one.
        """
        session = self._open_screen_cast(
            {"restore_token": restore_token} if restore_token else {})
        # Keep the newest token for the next launch's saved screen layers.
        config.save_screen_restore_token(getattr(session, "restore_token", ""))
        return session

    def _open_screen_cast(self, options: dict) -> portal.ScreenSession:
        if self.headless:
            return portal.request_screen_cast(**options)
        app = QApplication.instance()
        if app is None:
            return portal.request_screen_cast(**options)
        dialog = QProgressDialog(
            "Choose a screen or window in the system picker…",
            "", 0, 0, self)
        dialog.setWindowTitle("Desktop capture")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setCancelButton(None)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.show()
        try:
            return portal.request_screen_cast(pump=app.processEvents, **options)
        finally:
            dialog.close()
            dialog.deleteLater()

    def _screen_session_in_use(self, session) -> bool:
        """Whether a layer that still exists is already showing ``session``."""
        return any(self._screen_sessions.get(layer.id) is session
                   for scene in self.project.scenes for layer in scene.layers)

    def _add_screen_layer(
            self, box: tuple[float, float, float, float] | None = None,
            *, new_share: bool = False) -> Layer | None:
        """Add a screen stream.

        The open portal stream is reused only when no layer is showing it any
        more (a removed share can be put back without another picker).  While
        a layer is using it, a second "screen" is a different share, so the
        portal is asked again; reusing the one stream made it impossible to
        add a second window or monitor without restarting the app.
        """
        session = self._last_screen_session
        reuse = (not new_share and session is not None and bool(session.streams)
                 and not self._screen_session_in_use(session))
        if reuse:
            print(
                f"[screen-session] reusing node={session.node_id} for a new "
                "screen layer; no portal request",
                file=sys.stderr, flush=True)
        else:
            try:
                session = self._request_screen_cast()
            except Exception as exc:  # noqa: BLE001 - the portal reports user cancellation too
                self._status(f"Screen capture unavailable: {exc}")
                print(f"AnyCam screen capture failed: {exc}", file=sys.stderr,
                      flush=True)
                if not self.headless:
                    QMessageBox.warning(self, "Screen capture", str(exc))
                return None
            self._last_screen_session = session
        stream = session.streams[0]
        size = (f"{stream.width}×{stream.height}"
                if stream.width > 1 and stream.height > 1 else "Screen")
        layer = Layer(new_id("layer"), "screen", f"Screen {size}",
                      source=str(session.node_id),
                      box=box or (0.0, 0.0, 1.0, 1.0), z=0)
        self._screen_sessions[layer.id] = session
        return self._insert_layer(layer)

    def _ensure_screen_sessions(self) -> bool:
        """Re-request portal streams for saved screen layers after a restart."""
        for layer in list(self.scene.layers):
            if layer.kind != "screen" or layer.id in self._screen_sessions:
                continue
            session = self._last_screen_session
            if session is None or not session.streams:
                try:
                    # Saved layers come back without a picker when the portal
                    # still honours last launch's choice.  Adding a new share
                    # never passes a token: the user is picking then.
                    session = self._request_screen_cast(
                        restore_token=config.load_screen_restore_token())
                except Exception as exc:  # noqa: BLE001
                    self._status(f"Screen capture unavailable: {exc}")
                    return False
                self._last_screen_session = session
            else:
                print(
                    f"[screen-session] reusing node={session.node_id} for "
                    f"saved screen layer {layer.id}; no portal request",
                    file=sys.stderr, flush=True)
            print(
                f"[screen-session] layer={layer.id} node={session.node_id} "
                f"fd={session.fd if session.fd >= 0 else 'on-demand'} streams="
                f"{[(s.width, s.height, s.source_type) for s in session.streams]}",
                file=sys.stderr, flush=True)
            layer.source = str(session.node_id)
            self._screen_sessions[layer.id] = session
        return True

    def _screen_fds(self) -> dict[str, int]:
        """One owned portal remote per live screen layer for this build.

        ``pipewiresrc`` closes its fd when its pipeline stops, so the session's
        original fd cannot be handed to a later rebuild.  ``take_remote``
        consumes it once and opens a fresh remote on the same portal session
        for each subsequent canvas.
        """
        fds: dict[str, int] = {}
        try:
            for layer_id, session in self._screen_sessions.items():
                if self.scene.layer(layer_id) is None:
                    continue
                fds[layer_id] = session.take_remote()
        except Exception:
            for fd in fds.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        return fds

    def _close_screen_session(self, layer_id: str) -> None:
        session = self._screen_sessions.pop(layer_id, None)
        if session is None:
            return
        if not any(other is session for other in self._screen_sessions.values()):
            session.close()
            if self._last_screen_session is session:
                self._last_screen_session = None

    def _close_screen_sessions(self) -> None:
        sessions = list(self._screen_sessions.values())
        self._screen_sessions.clear()
        self._last_screen_session = None
        closed: set[int] = set()
        for session in sessions:
            if id(session) not in closed:
                session.close()
                closed.add(id(session))

    def _screen_source_fps(self) -> float | None:
        """Frames per second actually arriving from the screen layer(s).

        Returns None until there is a previous sample, so the label does not
        flash a meaningless number on the first tick.
        """
        engine = self.engine
        if engine is None:
            return None
        health = getattr(engine.report, "source_health", {}) or {}
        total = 0
        found = False
        for layer in self.scene.layers:
            if layer.kind != "screen":
                continue
            entry = health.get(layer.id)
            if entry is None:
                continue
            found = True
            total += int(entry.get("frames") or 0)
        if not found:
            return None
        now = time.monotonic()
        previous = getattr(self, "_screen_fps_prev", None)
        self._screen_fps_prev = (total, now)
        if previous is None or now <= previous[1]:
            return None
        return max(0.0, (total - previous[0]) / (now - previous[1]))

    def _screen_sizes(self) -> dict[str, tuple[int, int]]:
        """Portal stream size per screen layer (crop base and node check)."""
        sizes: dict[str, tuple[int, int]] = {}
        for layer_id, session in self._screen_sessions.items():
            stream = session.streams[0] if session.streams else None
            # A 1x1 (or 0) report is unusable and used to become a square
            # letterbox that showed only part of the shared picture.
            if (stream is not None and stream.width > 1
                    and stream.height > 1):
                sizes[layer_id] = (int(stream.width), int(stream.height))
        return sizes
