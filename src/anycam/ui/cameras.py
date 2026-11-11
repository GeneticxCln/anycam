"""Camera discovery, modes, loss and reconnection."""
from __future__ import annotations


import re
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QInputDialog,
)

from .. import config
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401

# Phrases a disconnected or wedged camera produces.  An error from another
# element (an encoder, the virtual camera's writer) has none of them, so a
# camera node that happens to be missing does not turn it into a disconnect.
_CAMERA_LOSS = re.compile(
    r"no such device|device (?:gone|disconnected|not found|busy|removed)"
    r"|error reading from device|could not read from resource")


class CameraMixin:
    """Camera discovery, modes, loss and reconnection."""

    def _scan_camera_choices(self) -> list[tuple[str, str]]:
        """Return (label, device) for usable V4L2 capture nodes."""
        choices: list[tuple[str, str]] = []
        seen: set[str] = set()
        # Never offer AnyCam's own virtual camera as an input; selecting it
        # would feed the compositor output back into itself.
        vcam = self.project.output.vcam_device or config.loopback_device()
        for camera in config.list_video_devices():
            stable = config.camera_identity(camera.device)
            if not camera.formats or stable in seen:
                continue
            name = camera.name or "Camera"
            if camera.device == vcam or stable == vcam or "anycam" in name.lower():
                continue
            # The label stays human-readable; the stored value is the stable
            # path so the combo still matches after a replug.
            choices.append((f"{name} · {camera.device}", stable))
            seen.add(stable)
        default = config.camera_identity(config.camera_device())
        if (default and default not in seen and default != vcam
                and Path(default).exists()):
            choices.insert(0, (f"Default · {default}", default))
        return choices

    def _refresh_cameras(self) -> None:
        """Rescan V4L2 devices without disturbing the selected layer."""
        if not hasattr(self, "camera_combo"):
            return
        self._camera_choices = self._scan_camera_choices()
        layer = self.preview.selected_layer()
        self._sync_camera_combo(layer.source if layer and layer.kind == "camera" else "")
        self.camera_combo.setEnabled(layer is not None and layer.kind == "camera")

    def _sync_camera_combo(self, source: str) -> None:
        choices = list(self._camera_choices)
        if source and all(device != source for _label, device in choices):
            choices.insert(0, (f"Unavailable · {source}", source))
        if not choices:
            choices = [("No V4L2 camera detected", "")]
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        for label, device in choices:
            self.camera_combo.addItem(label, device)
        index = self.camera_combo.findData(source) if source else 0
        self.camera_combo.setCurrentIndex(index if index >= 0 else 0)
        self.camera_combo.blockSignals(False)

    def _camera_device_changed(self, _index: int) -> None:
        layer = self.preview.selected_layer()
        source = self.camera_combo.currentData()
        if (layer is None or layer.kind != "camera" or not source
                or layer.source == source):
            return
        layer.source = config.camera_identity(str(source))
        self._lost_cameras.pop(layer.id, None)
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        self._status(f"Camera source: {source}")

    def _camera_mode_changed(self, _value: int = 0) -> None:
        layer = self.preview.selected_layer()
        if layer is None or layer.kind != "camera":
            return
        values = (self.camera_width.value(), self.camera_height.value(),
                  self.camera_fps.value())
        if (layer.camera_width, layer.camera_height, layer.camera_fps) == values:
            return
        layer.camera_width, layer.camera_height, layer.camera_fps = values
        self._mark_dirty()
        # Width, height and rate are three fields: rebuild once after the last
        # change instead of reopening the camera three times in a row.
        timer = getattr(self, "_camera_mode_timer", None)
        if timer is None:
            timer = self._camera_mode_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(300)
            timer.timeout.connect(self._apply_camera_mode)
        timer.start()
        width, height, fps = values
        mode = (f"{width}×{height}" if width and height else "auto size")
        self._status(f"Camera capture: {mode} @ {fps or 'auto'} fps")

    def _apply_camera_mode(self) -> None:
        if self.engine is not None:
            self.restart_engine()

    # --------------------------------------------------- camera reconnection
    def _missing_camera_layers(self) -> dict[str, str]:
        """Camera layers whose V4L2 node is gone, mapped from layer id to device."""
        return {
            layer.id: layer.source
            for layer in self.scene.layers
            if layer.kind == "camera" and layer.source
            and not Path(layer.source).exists()
        }

    def _camera_error_layers(self, message: str) -> dict[str, str]:
        """Map a pipeline error to camera layers even when the node still exists.

        UVC/Logitech devices can stay present but refuse a new stream after too
        many opens.  Quarantining them like an unplugged camera puts a
        placeholder in the graph and lets the reconnect watch retry with
        backoff instead of stopping the preview until a reboot.
        """
        cameras = {layer.id: layer.source for layer in self.scene.layers
                   if layer.kind == "camera" and layer.source}
        if not cameras:
            return {}
        text = message.lower()
        # Match the device as a whole name: "/dev/video1" is a substring of
        # "/dev/video10" (the virtual camera), which swapped a healthy camera
        # for a placeholder whenever the vcam reported an error.
        explicit = {layer_id: device for layer_id, device in cameras.items()
                    if re.search(re.escape(device.lower()) + r"(?![\w.-])", text)}
        if explicit:
            return explicit
        # Pool errors name their capture channel; blame only the cameras that
        # capture feeds, and nothing at all when it feeds a different kind.
        channel = re.search(r"anycam-src-\d+", text)
        if channel is not None and self.engine is not None:
            fed = self.engine.layers_for_channel(channel.group(0))
            if fed:
                return {layer_id: device for layer_id, device in cameras.items()
                        if layer_id in fed}
        # Only the camera source element identifies a camera failure.  The old
        # list also matched "no space left" and "failed to allocate", so a full
        # disk or a GPU allocation error from any element quarantined every
        # camera and swapped them for placeholder cards.
        return cameras if "v4l2src" in text else {}

    def _has_screen_layer(self) -> bool:
        """Whether this scene is using a portal screen capture."""
        return any(layer.kind == "screen" for layer in self.scene.layers)

    def _handle_pipeline_error(self, message: str) -> None:
        """Treat an unplugged or wedged camera as a temporary loss."""
        # Blame cameras only when the error is about a camera.  A camera node
        # that happens to be missing used to turn every error (an encoder, the
        # virtual camera) into "camera disconnected" and an immediate rebuild,
        # which repeated as long as the real error did.
        blamed = self._camera_error_layers(message)
        absent = self._missing_camera_layers()
        text = message.lower()
        about_cameras = (bool(blamed) or "v4l2src" in text
                         or (bool(absent) and _CAMERA_LOSS.search(text) is not None))
        missing = {**(absent if about_cameras else {}), **blamed}
        if not missing:
            self._status(message)
            self.stop_engine()
            self._status(message)  # keep the failure visible after finalisation
            return
        self._lost_cameras.update(missing)
        # A camera that fails again after a reconnect must not be reopened in
        # a tight loop; repeated opens are what wedges some UVC devices.
        self._camera_retry_ms = min(10000, int(self._camera_retry_ms * 1.5))
        if self.engine is not None and all(
                self.scene.layer(layer_id) is not None for layer_id in missing):
            # Swap each lost camera for its card on the running canvas: the
            # other sources, the recording and the virtual camera keep going.
            if all(self.engine.mark_layer_lost(layer_id) for layer_id in missing):
                devices = ", ".join(sorted(set(missing.values())))
                self._status(f"{devices} disconnected · placeholder shown; "
                             f"retrying in {self._camera_retry_ms / 1000:.1f}s")
                self._camera_watch.start(self._camera_retry_ms)
                return
        if (self.engine is not None and self._has_screen_layer()
                and message.startswith("Camera source error:")):
            # Do not rebuild a PiP just because the camera failed.  A rebuild
            # stops the screen pipewiresrc and needs another portal remote; if
            # the share cannot be reopened the whole PiP disappears.  Drop the
            # failed capture and retry it on the existing canvas channel.
            dropped = [layer_id for layer_id in missing
                       if self.engine.drop_capture(layer_id)]
            if len(dropped) == len(missing):
                devices = ", ".join(sorted(set(missing.values())))
                self._status(
                    f"{devices} unavailable · screen preview kept live · "
                    f"retrying in {self._camera_retry_ms / 1000:.1f}s")
                self._camera_watch.start(self._camera_retry_ms)
                return
        self._rebuild_without_lost_cameras(message)

    def _rebuild_without_lost_cameras(self, message: str = "") -> None:
        """Rebuild with a placeholder card for each lost camera."""
        if self.engine is not None:
            self.stop_engine()
        devices = ", ".join(sorted(set(self._lost_cameras.values())))
        live = [layer for layer in self.scene.layers
                if layer.id not in self._lost_cameras]
        prefix = f"{message} · " if message else ""
        self._quiet_engine_errors = True
        try:
            self.start_engine()
        finally:
            self._quiet_engine_errors = False
        if self.engine is None:
            self._status(f"{prefix}{devices} disconnected · preview could not restart")
            self._camera_watch.start(self._camera_retry_ms)
            return
        summary = ("placeholder shown; other layers live" if live
                   else "showing placeholders")
        self._status(f"{prefix}{devices} disconnected · {summary}")
        self._camera_watch.start(self._camera_retry_ms)

    def _reconnect_cameras(self) -> None:
        """Rebuild the preview when a lost camera device reappears."""
        if not self._lost_cameras:
            self._camera_watch.stop()
            return
        restored = {layer_id: device
                    for layer_id, device in self._lost_cameras.items()
                    if Path(device).exists()}
        if not restored:
            return
        for layer_id in restored:
            del self._lost_cameras[layer_id]
        if self.engine is not None:
            # Reattach the camera capture on the running canvas.  Rebuilding
            # here would stop every other source (a screen share loses its
            # portal session) and the recording and virtual camera feed.
            failed = [layer_id for layer_id in restored
                      if not self.engine.attach_capture(layer_id)]
            if failed:
                self._lost_cameras.update(
                    {layer_id: restored[layer_id] for layer_id in failed})
                self._camera_retry_ms = min(10000, int(self._camera_retry_ms * 1.7))
                self._camera_watch.start(self._camera_retry_ms)
                self._status(
                    "Camera returned but the live capture could not resume; "
                    f"retrying in {self._camera_retry_ms / 1000:.1f}s"
                )
                return
            self._status(
                "Camera reconnected: "
                + ", ".join(sorted(set(restored.values()))))
            if self._lost_cameras:
                self._camera_watch.start(self._camera_retry_ms)
            else:
                self._camera_retry_ms = 2000
                self._camera_watch.stop()
            return
        self._quiet_engine_errors = True
        try:
            if self.engine is not None:
                self.restart_engine()
            else:
                self.start_engine()
        finally:
            self._quiet_engine_errors = False
        if self.engine is None:
            # The node exists but the capture did not come back (UVC needs a
            # moment after a replug, or the device is still busy).  Keep the
            # placeholder and retry with backoff instead of giving up; giving up
            # here is what used to force a reboot.
            self._lost_cameras.update(restored)
            self._camera_retry_ms = min(10000, int(self._camera_retry_ms * 1.7))
            self._camera_watch.start(self._camera_retry_ms)
            self._status(
                "Camera returned but the preview could not restart; "
                f"retrying in {self._camera_retry_ms / 1000:.1f}s"
            )
            return
        # Do not rescan V4L2 here: ``v4l2-ctl`` opens every node, exactly while
        # the camera we just reopened is negotiating its stream.  The combo
        # already holds the stable by-id path.
        if self._lost_cameras:
            self._camera_watch.start(self._camera_retry_ms)
        else:
            self._camera_watch.stop()
        devices = ", ".join(sorted(set(restored.values())))
        self._status(f"Camera reconnected: {devices}")

    def _forget_lost_cameras(self) -> None:
        """Drop reconnect state when the scene or the preview changes."""
        self._lost_cameras.clear()
        self._camera_retry_ms = 2000
        self._camera_watch.stop()

    def _choose_camera_device(self) -> str:
        # A UVC replug renumbers the node (for example video0 -> video3).
        # Refresh a cached choice that no longer exists instead of adding a
        # dead source to the running PiP.
        if (not self._camera_choices
                or any(not Path(device).exists()
                       for _label, device in self._camera_choices)):
            self._camera_choices = self._scan_camera_choices()
        choices = self._camera_choices
        if not choices:
            self._status("No V4L2 camera detected")
            return ""
        if len(choices) == 1:
            return choices[0][1]
        labels = [label for label, _device in choices]
        label, accepted = QInputDialog.getItem(
            self, "Camera source", "V4L2 device:", labels, 0, False)
        if not accepted or not label:
            return ""
        return choices[labels.index(label)][1]

    def _camera_layer_name(self, source: str) -> str:
        for label, device in self._camera_choices:
            if device == source:
                return label.split(" · ", 1)[0]
        return "Camera"
