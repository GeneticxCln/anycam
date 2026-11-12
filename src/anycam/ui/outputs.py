"""Recording, streaming, the virtual camera and snapshots."""
from __future__ import annotations


import threading
import time
from pathlib import Path

from PySide6.QtWidgets import (
    QMessageBox,
)

from .. import config, overlay, streaming
from ..engine.outputs.recorder import Recorder
from ..engine.outputs.vcam import VcamOutput
from ..pipeline import recording_codec
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class OutputsMixin:
    """Recording, streaming, the virtual camera and snapshots."""

    def toggle_live(self) -> None:
        """Start/stop publishing without touching preview or recording."""
        url = self.rtmp_edit.text().strip()
        if not self.live and not url:
            self._status("Set an RTMP URL before going live")
            return
        problem = streaming.validate_stream_url(url) if not self.live else ""
        if problem:
            self._status(f"Cannot go live: {problem}")
            return
        self.live = not self.live
        self.live_button.setText("Stop stream" if self.live else "Go Live")
        if self.engine is not None:
            self.restart_engine()
            if self.live:
                self._status(f"Publishing to {config.redact_url(url)}")
            else:
                self._status("Stream stopped")
        else:
            if self.live:
                self._status(f"Will publish to {config.redact_url(url)} when preview starts")
            else:
                self._status("Stream stopped")

    # --------------------------------------------------------------- engine
    def _record_path(self) -> Path:
        """Path for the current recording session (one file, no segments)."""
        if self._recording_path:
            return Path(self._recording_path)
        stamp = self._session_stamp or overlay.timestamped_name("anycam", "")
        self._session_stamp = stamp
        # A canvas resize starts a new file (see _rotate_recording): -2, -3, ...
        suffix = f"-{self._segment_index + 1}" if self._segment_index else ""
        path = (config.recordings_dir(self.project.output.record_dir)
                / f"{stamp}{suffix}.mp4")
        self._recording_path = str(path)
        return path

    def _start_recorder(self) -> bool:
        if self._recorder is not None:
            return True
        path = self._record_path()
        recorder = Recorder()
        audio = (self.audio_on or bool(self.project.audio.media_file)
                 or bool(self.project.audio.desktop_device))
        if not recorder.start(str(path), self.project.output.canvas_w,
                              self.project.output.canvas_h, self.project.output.fps,
                              audio=audio,
                              codec=recording_codec(self.project.output.encoder)):
            self._status(f"Recording could not start: {recorder.error}")
            return False
        self._recorder = recorder
        self._recording_path = str(path)
        return True

    def _rotate_recording(self) -> None:
        """Close the current file and continue in a new one.

        One MP4 has a single picture size.  Feeding frames of another size
        into it corrupts or aborts the recording, so a resize ends the file
        (it is saved and listed) and the rebuilt canvas records into the next.
        """
        if self.engine is not None:
            self.engine.detach_recorder()
        self._finish_recorder()
        self._segment_index += 1

    def _finish_recorder(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        self._recorder = None
        report = self._run_with_ui(recorder.stop)
        self._recording_path = ""
        path = str(report.get("path") or "")
        if report.get("ok") and path:
            if path not in self.recording_segments:
                self.recording_segments.append(path)
            warning = str(report.get("warning") or "")
            self._status(f"Saved {path}" + (f" ({warning})" if warning else ""))
        else:
            self._status(
                f"Recording failed: {report.get('error') or 'no frames'}")

    def show_vcam_hint(self, text: str) -> None:
        """Keep a dismissible hint above the preview until replaced."""
        self.vcam_hint.setText(text)
        self.vcam_hint.show()
        self.vcam_dismiss.show()
        self._status(text)

    def dismiss_vcam_hint(self) -> None:
        self.vcam_hint.hide()
        self.vcam_dismiss.hide()

    def hold_vcam_device(self) -> bool:
        """Open the loopback node at launch so apps can select it early.

        No root prompt here: the button still runs the setup path when the
        device is missing.  The idle card in ``VcamOutput`` is what makes the
        node stream before the preview exists.
        """
        if self._vcam_output is not None and self._vcam_output.active:
            return True
        device = self.project.output.vcam_device or config.loopback_device()
        output = VcamOutput()
        if not output.start(device, self.project.output.canvas_w,
                            self.project.output.canvas_h,
                            self.project.output.fps,
                            backend=self._vcam_backend()):
            return False
        self._vcam_output = output
        self._note_vcam_backend(output)
        return True

    def _vcam_backend(self) -> str:
        backend = str(getattr(self.project.output, "vcam_backend", "v4l2"))
        return backend if backend in ("v4l2", "pipewire", "both") else "v4l2"

    def _note_vcam_backend(self, output: VcamOutput) -> None:
        """Log what is published, and why a requested PipeWire node is not."""
        if getattr(output, "publishes_pipewire", False):
            self.log_event("virtual camera: PipeWire camera node “AnyCam” published")
        if getattr(output, "pipewire_error", ""):
            self.log_event(f"virtual camera: no PipeWire node ({output.pipewire_error})")
        self._update_vcam_note()

    def _update_vcam_note(self) -> None:
        note = getattr(self, "vcam_note", None)
        if note is None:
            return
        backend = self._vcam_backend()
        output = getattr(self, "_vcam_output", None)
        parts = []
        if backend in ("v4l2", "both"):
            parts.append("The loopback device is set up automatically on first "
                         "launch (one password prompt) and works in every app.")
        if backend in ("pipewire", "both"):
            parts.append("The PipeWire camera needs no driver, but browsers list "
                         "it only with PipeWire cameras enabled: Firefox "
                         "about:config media.webrtc.camera.allow-pipewire, "
                         "Chrome/Discord/Vesktop --enable-features=WebRtcPipeWireCamera.")
        if output is not None and output.active and getattr(output, "pipewire_error", ""):
            parts.append(f"PipeWire camera not published: {output.pipewire_error}.")
        parts.append("The camera source itself is chosen in Sources or the + menu.")
        note.setText(" ".join(parts))

    def _vcam_backend_changed(self) -> None:
        """Republish the virtual camera on the chosen backend(s) right away."""
        value = str(self.vcam_backend_combo.currentData() or "v4l2")
        if value == self._vcam_backend():
            return
        self.project.output.vcam_backend = value
        self._mark_dirty()
        output = self._vcam_output
        if output is not None and output.active:
            if self.engine is not None:
                self.engine.detach_vcam()
            self._finish_vcam_output()
            if value in ("v4l2", "both"):
                self.setup_vcam_if_needed()
            if self.hold_vcam_device() and self.vcam_on and self.engine is not None:
                self.engine.attach_vcam(self._vcam_output)
            if getattr(self._vcam_output, "publishes_v4l2", False):
                self.ensure_vcam_visible()
        self._update_vcam_note()
        self._status(f"Virtual camera: {self.vcam_backend_combo.currentText()}")

    def setup_vcam_if_needed(self) -> bool:
        """Offer the one-time system setup that makes Discord see AnyCam.

        Runs at launch, before the device is held (a reload needs it closed).
        One password prompt installs the driver settings permanently; every
        later launch is ready with no prompt.  True when the camera is ready.
        """
        if self._vcam_backend() == "pipewire":
            # A PipeWire node needs no kernel module and no root.
            return True
        dev = self.project.output.vcam_device or config.loopback_device()
        reason = config.vcam_setup_needed(dev)
        if not reason:
            return True
        self.log_event(f"virtual camera setup needed: {reason}")
        if self.headless:
            return False
        answer = QMessageBox.question(
            self, "Set up the virtual camera",
            "Discord, Vesktop, Chrome and other apps cannot see AnyCam's "
            f"virtual camera yet: {reason}.\n\n"
            "AnyCam can fix this once, for good. It installs the camera "
            "driver settings and reloads the driver; your password is asked "
            "one time. After that the camera is ready whenever AnyCam is "
            "open.\n\nSet it up now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            self._status("Virtual camera not set up: Discord will not list AnyCam")
            return False
        self._status("Setting up the virtual camera…")
        try:
            error = self._run_with_ui(lambda: config.setup_vcam_system(dev))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            error = str(exc)
        if error:
            self.log_event(f"virtual camera setup failed: {error}")
            QMessageBox.warning(self, "Virtual camera", f"The setup did not finish:\n{error}")
            return False
        self.log_event("virtual camera set up permanently (exclusive_caps=1)")
        self._status("Virtual camera ready: select AnyCam in Discord")
        return True

    def ensure_vcam_visible(self, device: str | None = None) -> None:
        """Make Chromium-based apps list the node AnyCam is feeding.

        ``exclusive_caps=1`` only advertises capture while a writer streams,
        but udev caches the capabilities from device-add time.  Without a
        change event Discord, Vesktop and Chrome keep skipping AnyCam even
        though the module parameter is correct.  This runs once per device and
        only asks for authorization when the cached value is actually wrong.
        """
        if self._vcam_backend() == "pipewire":
            return
        dev = (device or self.project.output.vcam_device
               or config.loopback_device())
        if dev in self._vcam_udev_attempted:
            return
        if config.vcam_exclusive_caps() is not True:
            return
        caps = config.vcam_udev_capabilities(dev)
        if caps is None or "capture" in caps:
            return
        self._vcam_udev_attempted.add(dev)
        result = self._vcam_udev_result = {}
        self._vcam_udev_timer.start()

        def run() -> None:
            # exclusive_caps only advertises capture once a buffer is flowing;
            # give the idle feed a moment to reach v4l2sink before rewiring.
            time.sleep(0.5)
            try:
                result["error"] = config.refresh_vcam_udev(dev)
            except Exception as exc:  # noqa: BLE001 - a hint must not crash the GUI
                result["error"] = str(exc)

        self._vcam_udev_thread = threading.Thread(
            target=run, name="anycam-vcam-udev", daemon=True)
        self._vcam_udev_thread.start()

    def _vcam_udev_poll(self) -> None:
        thread = self._vcam_udev_thread
        if thread is not None and thread.is_alive():
            return
        self._vcam_udev_timer.stop()
        error = str(self._vcam_udev_result.get("error") or "")
        if error:
            self.log_event(
                "virtual camera: Chromium visibility refresh failed — "
                f"{error}")
            return
        self.log_event(
            "virtual camera: announced to Chromium apps (Discord, Chrome)")
        if hasattr(self, "_refresh_diag_state"):
            self._refresh_diag_state()

    def feed_held_vcam(self) -> bool:
        """Make the device held at launch follow the canvas automatically.

        The "Virtual camera ready / start the preview" card is only a
        placeholder: once the engine exists, the held node should show the
        composed scene without a second button press.  The button still turns
        the feed off again.
        """
        output = self._vcam_output
        if output is None or not output.active:
            return False
        self.vcam_on = True
        if hasattr(self, "vcam_button"):
            self.vcam_button.setText("Virtual camera: on")
        if self.engine is not None and not self.engine.attach_vcam(output):
            self._status(self.engine.report.vcam_error
                         or "Virtual camera could not start")
            return False
        return True

    def enable_held_vcam(self) -> bool:
        """Feed the canvas through the loopback node held at launch.

        The idle card says "Start the preview", but a held node whose feed is
        never switched stays on that near-black card forever, which a consumer
        cannot tell apart from a dead camera.  Once AnyCam owns the device, the
        canvas is the virtual camera's picture; turning the button off still
        returns it to the card.
        """
        output = self._vcam_output
        if output is None or not output.active:
            return False
        self.vcam_on = True
        button = getattr(self, "vcam_button", None)
        if button is not None:
            button.setText("Virtual camera: on")
        if self.engine is not None:
            self.engine.attach_vcam(output)
        return True

    def note_vcam_consumers(self) -> None:
        """Warn about apps that opened the node before AnyCam fed it.

        That open fails with EIO and the app shows nothing until it re-opens
        the camera; nothing outside the app can repair it.
        """
        output = self._vcam_output
        if output is None or not output.device:
            return
        names = config.vcam_consumers(output.device)
        if not names:
            return
        label = ", ".join(names[:2]) + ("…" if len(names) > 2 else "")
        self.show_vcam_hint(
            f"{label} already had {Path(output.device).name} open before AnyCam "
            "started feeding it. If it shows no picture, re-select “AnyCam” "
            "in that app's camera list.")

    def _start_vcam_output(self, setup: bool = False) -> bool:
        device = self.project.output.vcam_device or config.loopback_device()
        width = self.project.output.canvas_w
        height = self.project.output.canvas_h
        fps = self.project.output.fps
        output = self._vcam_output
        if output is not None and output.active:
            matches = getattr(output, "matches", None)
            serves = (matches(device, self._vcam_backend()) if callable(matches)
                      else output.device == device)
            if not output.error and serves:
                # Keep the node open while the canvas changes.  OBS's Linux
                # virtual camera is a separate output with one fixed mode;
                # reopening v4l2sink for every canvas resize kills the handle
                # Discord is reading.  The engine tap scales into this mode.
                if self._vcam_canvas_size != (width, height):
                    self._vcam_canvas_size = (width, height)
                    out_w = int(getattr(output, "width", width))
                    out_h = int(getattr(output, "height", height))
                    if (out_w, out_h) != (width, height):
                        self.log_event(
                            f"virtual camera: canvas {width}×{height} scaled to "
                            f"{out_w}×{out_h}")
                return True
            # A sink error leaves the pipeline object alive but dead; a
            # changed device needs new caps.  Only those justify a reopen.
            output.stop()
            self._vcam_output = None
        output = VcamOutput()
        backend = self._vcam_backend()
        if setup and backend != "pipewire" and not Path(device).exists():
            # Creating the device asks polkit, which can take a minute; run the
            # start off the UI thread so the window stays alive meanwhile.
            started = self._run_with_ui(
                lambda: output.start(device, width, height, fps, setup=True,
                                     backend=backend))
        else:
            started = output.start(device, width, height, fps,
                                   setup=setup and backend != "pipewire",
                                   backend=backend)
        if not started:
            self.log_event(
                f"virtual camera: {output.error or 'could not start'}")
            self._status(output.error or "Virtual camera could not start")
            return False
        self._vcam_output = output
        self._vcam_canvas_size = (width, height)
        out_w = int(getattr(output, "width", width))
        out_h = int(getattr(output, "height", height))
        if (out_w, out_h) != (width, height):
            self.log_event(
                f"virtual camera: canvas {width}×{height} scaled to "
                f"{out_w}×{out_h}")
        self._note_vcam_backend(output)
        if not self.headless and getattr(output, "publishes_v4l2", True):
            self.ensure_vcam_visible(device)
        return True

    def _recover_vcam(self) -> bool:
        """Repair a virtual camera that dropped while the canvas kept running.

        A v4l2sink error only kills that output pipeline; it is not the user
        turning the camera off.  Flipping ``vcam_on`` in response made every
        later rebuild build without the tap, so the held node sat on the dark
        "ready" card and Discord showed a black camera until the button was
        pressed again.  Rebuild the output and reattach it instead, throttled
        so a device that stays unavailable does not spin the GUI thread.
        """
        if not self.vcam_on or self.engine is None:
            return False
        now = time.monotonic()
        if now - self._vcam_retry_at < 2.0:
            return False
        self._vcam_retry_at = now
        output = self._vcam_output
        if output is not None and (not output.active or output.error):
            self.log_event(
                f"virtual camera: rebuilding output after "
                f"{output.error or 'sink stopped'}")
            self._finish_vcam_output()
            output = None
        if output is None:
            if not self._start_vcam_output(setup=False):
                return False
            output = self._vcam_output
        if output is None:
            return False
        if not self.engine.attach_vcam(output):
            self.log_event(
                f"virtual camera: reattach failed "
                f"({self.engine.report.vcam_error or 'no canvas tap'})")
            return False
        self._vcam_error_shown = ""
        self.vcam_button.setText("Virtual camera: on")
        self.log_event("virtual camera: recovered and re-attached to the canvas")
        self._status("Virtual camera re-attached to the canvas")
        return True

    def _finish_vcam_output(self) -> None:
        output = self._vcam_output
        self._vcam_output = None
        self._vcam_canvas_size = None
        if output is None:
            return
        report = output.stop()
        if report.get("error"):
            self._status(str(report["error"]))

    def toggle_record(self) -> None:
        if self.recording:
            self.recording = False
            self.record_button.setText("Record")
            if self.engine is not None:
                self.engine.detach_recorder()
            self._finish_recorder()
            return
        problem = config.disk_guard(config.recordings_dir(self.project.output.record_dir),
                                    self.project.output.record_min_free_mb)
        if problem:
            self._status(f"Recording not started: {problem}")
            return
        self.recording = True
        self._segment_index = 0
        self.recording_segments = []
        self._session_stamp = overlay.timestamped_name("anycam", "")
        self._recording_path = ""
        self.record_button.setText("Stop recording")
        if self.engine is not None:
            if not self._start_recorder():
                self.recording = False
                self.record_button.setText("Record")
                return
            tap_codec = self.engine.record_codec()
            if tap_codec is not None and tap_codec != self._recorder.codec:
                # The encoder in the running canvas was chosen before the
                # encoder setting changed; only a rebuild can switch codec.
                self.restart_engine()
                if self.engine is not None:
                    self._status(f"Recording to {self._recording_path}")
                return
            if not self.engine.attach_recorder(self._recorder):
                self.recording = False
                self.record_button.setText("Record")
                self._finish_recorder()
                self._status("Recording could not attach to the running canvas")
                return
            self._status(f"Recording to {self._recording_path}")
        elif not self._start_recorder():
            self.recording = False
            self.record_button.setText("Record")
        else:
            self._status(f"Recording to {self._recording_path}")

    DISK_CHECK_EVERY_S = 5.0

    def check_disk_guard(self, now: float | None = None) -> bool:
        """Stop a recording that is about to fill the disk; True when it did."""
        import time as _time

        now = _time.monotonic() if now is None else now
        if not self.recording or now - getattr(self, "_disk_checked", 0.0) < self.DISK_CHECK_EVERY_S:
            return False
        self._disk_checked = now
        problem = config.disk_guard(config.recordings_dir(self.project.output.record_dir),
                                    self.project.output.record_min_free_mb)
        if not problem:
            return False
        self.toggle_record()
        self._status(f"Recording stopped and saved: {problem}")
        self.log_event(f"recording stopped by the disk guard: {problem}")
        return True

    def toggle_vcam(self) -> None:
        self.vcam_on = not self.vcam_on
        # The user's choice, kept for the next launch (a failed start below
        # clears ``vcam_on`` but is not a choice).
        if self.project.output.vcam_enabled != self.vcam_on:
            self.project.output.vcam_enabled = self.vcam_on
            self._mark_dirty()
        self.vcam_button.setText("Virtual camera: on" if self.vcam_on else "Virtual camera")
        self.project.output.vcam_device = (self.vcam_edit.text().strip()
                                           or config.loopback_device())
        if not self.vcam_on:
            if self.engine is not None:
                self.engine.detach_vcam()
            # The node stays open with the idle card so the next application
            # can select it without waiting for the preview.
            return
        if self.engine is None:
            # A loopback writer with no canvas has nothing to push: the sink
            # never starts streaming, so a consumer that opens the device gets
            # EIO and no camera at all.  The button starts the preview it
            # needs instead of leaving an empty device behind.
            self.start_engine()
            if self.engine is not None and self.vcam_on:
                return
            self.vcam_on = False
            self.vcam_button.setText("Virtual camera")
            self._finish_vcam_output()
            return
        # Clicking the button is a request to use the virtual camera, so use
        # the same pkexec setup path as an engine start.  Without setup=True a
        # missing v4l2loopback device only produced a modprobe instruction.
        if not self._start_vcam_output(setup=True):
            self.vcam_on = False
            self.vcam_button.setText("Virtual camera")
            return
        if not self.engine.attach_vcam(self._vcam_output):
            self.vcam_on = False
            self.vcam_button.setText("Virtual camera")
            self._finish_vcam_output()

    def snapshot(self) -> None:
        image = self.preview.image
        if self.preview.gpu_live and self.preview.video_view is not None:
            # The GPU preview has no CPU frame: read back what it shows, at
            # the size it shows it (the image preview saved its preview-size
            # frame too).
            view = self.preview.video_view
            grabbed = view.grabFramebuffer()
            # The framebuffer is in device pixels but may not say so
            # (devicePixelRatio 1.0 on a 1.5x screen): scale by the width.
            ratio = grabbed.width() / max(1, view.width())
            rect = self.preview.canvas_rect()
            image = grabbed.copy(int(rect.x() * ratio), int(rect.y() * ratio),
                                 int(rect.width() * ratio), int(rect.height() * ratio))
        if image is None or image.isNull():
            self._status("No frame to snapshot")
            return
        try:
            path = config.recordings_dir(self.project.output.record_dir) / overlay.timestamped_name("anycam-snapshot", ".png")
        except OSError as exc:
            self._status(f"Snapshot failed: {exc}")
            return
        # QImage.save reports failure (unwritable folder, full disk) only
        # through its result; the status bar claimed success regardless.
        if not image.save(str(path)):
            self._status(f"Snapshot could not be written to {path}")
            return
        self._status(f"Snapshot saved to {path}")
