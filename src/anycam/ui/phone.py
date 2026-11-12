"""The phone link and the multi-view dialog."""
from __future__ import annotations



from PySide6.QtWidgets import (
    QDialog,
    QMessageBox,
)

from .. import config
from ..phone_dialog import PhonePairDialog
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class PhoneMixin:
    """The phone link and the multi-view dialog."""

    # ------------------------------------------------------------- phone link
    def _phone_link(self, *, quiet: bool = False):
        """The running phone link, started on first use (None if it cannot)."""
        if self._phone_service is not None and self._phone_service.running:
            return self._phone_service
        from ..phone.server import default_service

        service = self._phone_service or default_service(
            on_event=self._phone_events.event.emit)
        service.control_handler = self._phone_control_call
        service.control_enabled = bool(self.project.output.phone_control)
        try:
            service.start()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.log_event(f"phone link: {exc}")
            if quiet or self.headless:
                self._status(f"Phone link unavailable: {exc}")
            else:
                QMessageBox.warning(self, "Phone camera", f"The phone link could not start:\n{exc}")
            return None
        self._phone_service = service
        self.log_event(f"phone link listening on {service.page_url()}")
        return service

    # ------------------------------------------------------- remote control
    def _phone_control_call(self, action: str, data: dict, device: str) -> dict:
        """Called on a network thread: run the request on the UI thread and wait."""
        from concurrent.futures import Future, TimeoutError as FutureTimeout

        outcome: Future = Future()
        self._phone_events.control.emit((action, data, device, outcome))
        try:
            return outcome.result(timeout=5.0)
        except FutureTimeout:
            return {"error": "AnyCam did not answer in time."}

    def _phone_control_run(self, request) -> None:
        action, data, device, outcome = request
        try:
            outcome.set_result(self.phone_control(action, data, device))
        except Exception as exc:  # noqa: BLE001 - tell the phone, keep the window
            outcome.set_result({"error": f"{type(exc).__name__}"})
            self.log_event(f"phone control {action}: {exc}")

    def phone_control(self, action: str, data: dict, device: str = "") -> dict:
        """What the remote page can do; returns the state to show afterwards."""
        if action == "scene":
            index = next((i for i, scene in enumerate(self.project.scenes)
                          if scene.id == data.get("scene_id")), -1)
            if index < 0:
                return {"error": "No such scene."}
            self.switch_scene(index)
        elif action == "mute":
            self.toggle_mic_mute()
        elif action == "record":
            self.toggle_record()
        elif action != "state":
            return {"error": "Unknown action."}
        if action != "state":
            self.log_event(f"phone remote ({self._phone_name(device) or device}): {action}")
        return {"scenes": [{"id": scene.id, "name": scene.name} for scene in self.project.scenes],
                "active": self.project.active_scene, "recording": bool(self.recording),
                "muted": bool(self.project.audio.mic_muted)}

    def set_phone_control(self, enabled: bool) -> None:
        self.project.output.phone_control = bool(enabled)
        if self._phone_service is not None:
            self._phone_service.control_enabled = bool(enabled)
        self._mark_dirty()
        if enabled:
            service = self._phone_link(quiet=True)
            if service is not None:
                self._status(f"Phone remote: open {service.page_url()}remote on a paired phone")
        else:
            self._status("Phone remote control is off")

    def _stop_phone_link(self) -> None:
        service, self._phone_service = self._phone_service, None
        if service is not None:
            service.stop()

    def _phone_name(self, device: str) -> str:
        service = self._phone_service
        if service is not None:
            return service.pairing.name(device)
        from ..phone.auth import Pairing

        return Pairing(config.state_dir() / "phones.json").name(device)

    def _pair_phone(self) -> tuple[str, str] | None:
        """Show the QR code until a phone streams; its ``(device, name)``."""
        service = self._phone_link()
        if service is None:
            return None
        dialog = PhonePairDialog(service, self, log=self.log_event)
        self._phone_dialog = dialog
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._phone_dialog = None
        if not accepted or not dialog.device:
            return None
        return dialog.device, dialog.device_name or dialog.device

    def _on_phone_event(self, event: str, device: str, detail: str) -> None:
        """Phone-link events, delivered on the UI thread."""
        if self._phone_dialog is not None:
            self._phone_dialog.on_phone_event(event, device, detail)
        name = self._phone_name(device) if event == "failed" else detail
        message = {
            "paired": f"Phone paired: {name}",
            "connecting": f"{name} is connecting…",
            "streaming": f"{name} is live",
            "stopped": f"{name} stopped its camera",
            "disconnected": f"{name} disconnected",
            "failed": f"{name} could not connect: {detail}",
        }.get(event)
        if message:
            self.log_event(f"phone {device}: {message}")
            self._status(message)

    def toggle_multiview(self) -> None:
        if getattr(self, "_multiview", None) is not None:
            try:
                self._multiview.close()
            except Exception:
                pass
            self._multiview = None
            return
        if self.engine is None:
            self._status("Start preview first")
            return
        from ..multiview import MultiViewDialog
        pool = getattr(self, "_source_pool", None)
        if pool is None and self.engine is not None:
            pool = getattr(self.engine, "pool", None)
        if pool is None:
            self._status("No source pool available")
            return
        dlg = MultiViewDialog(pool, parent=self)
        self._multiview = dlg
        # Forget it however it was closed, or the next click would only
        # "close" a dialog that is already gone instead of opening a new one.
        dlg.finished.connect(lambda _result=0, d=dlg: self._multiview_closed(d))
        dlg.show()

    def _multiview_closed(self, dialog) -> None:
        if getattr(self, "_multiview", None) is dialog:
            self._multiview = None
        dialog.deleteLater()
