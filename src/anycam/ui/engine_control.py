"""Starting, stopping and pumping the engine; the preview tick."""
from __future__ import annotations


import sys
import threading
import time

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QApplication, QMessageBox,
)

from .. import config
from ..pipeline import AnyCamEngine
from ..pool import SCREEN_IDLE_GRACE_S
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class EngineMixin:
    """Starting, stopping and pumping the engine; the preview tick."""

    def start_engine(self) -> None:
        if self.engine is not None:
            return
        # A saved project may name a camera that is not plugged in yet.  Keep
        # its box as a placeholder and start the rest of the scene instead of
        # failing the whole preview.
        missing = self._missing_camera_layers()
        if missing:
            self._lost_cameras.update(missing)
            self._camera_watch.start()
        if any(layer.kind == "phone" for scene in self.project.scenes
               for layer in scene.layers):
            # A saved scene with a phone: listen so the phone can reconnect.
            self._phone_link(quiet=True)
        if not self._ensure_screen_sessions():
            return
        self.project.output.canvas_w = self._even(self.width_spin.value())
        self.project.output.canvas_h = self._even(self.height_spin.value())
        self.project.output.fps = self.fps_spin.value()
        self.project.output.vcam_device = self.vcam_edit.text().strip() or config.loopback_device()
        self.project.output.screen_flip = str(
            self.screen_flip_combo.currentData() or "automatic")
        self.project.output.rtmp_url = self.rtmp_edit.text().strip()
        self.project.audio.mic_enabled = self.audio_on
        self.project.audio.media_file = self.media_edit.text().strip()
        self.preview.canvas_size = (self.project.output.canvas_w,
                                    self.project.output.canvas_h)
        if self.playlist_ids and self.playlist_index < 0:
            # The first item is what the preview starts on (it was added
            # visible).  Leaving the index at -1 made the first timer step
            # "advance" to that same item, showing it for two intervals.
            self.playlist_index = 0
            self.scene.playlist.index = 0
        self._apply_playlist_visibility()
        if (self.recording and self._recorder is not None
                and self._recorder.requested_size not in (
                    None, (self.project.output.canvas_w,
                           self.project.output.canvas_h))):
            # The Output tab changed the canvas while recording.  One MP4 has
            # one picture size, so the rebuilt canvas records to a new file;
            # feeding it into the old one dropped every frame.
            self._rotate_recording()
        if self.recording and not self._start_recorder():
            self.recording = False
            self.record_button.setText("Record")
            return
        recorder = self._recorder
        if self.vcam_on and not self._start_vcam_output(setup=True):
            # Keep the request: the held node is still ours, and _recover_vcam
            # retries.  Clearing vcam_on here made this and every later
            # rebuild build without the tap, leaving the dark ready card on
            # the device while the preview looked fine.
            error = (self._vcam_output.error if self._vcam_output is not None
                     else "virtual camera could not start")
            print(f"[vcam] {error}", file=sys.stderr, flush=True)
        vcam_output = self._vcam_output if self.vcam_on else None
        # In crop mode the selected layer is built uncropped, so the editor's
        # window is drawn over the picture it is actually trimming.
        crop_preview = self.preview.selected_id if self.preview.edit_mode == "crop" else ""
        self._crop_preview_layer_id = crop_preview
        candidate = None
        try:
            candidate = AnyCamEngine(
                self.project,
                recorder=recorder,
                # Without an owned output the engine would build its own
                # v4l2sink, opening the loopback node as a second writer.
                vcam=self.vcam_on and vcam_output is not None,
                vcam_output=vcam_output,
                preview=True,
                # A recording that has an audio track keeps it fed (silence if
                # the microphone is off) so its muxer never waits on a dead pad.
                audio=(self.audio_on or bool(self.project.audio.media_file)
                       or bool(self.project.audio.desktop_device)
                       or (recorder is not None and recorder.has_audio)),
                # Only an explicit Go Live publishes; a saved URL is inert.
                rtmp=self.project.output.rtmp_url if self.live else None,
                screen_fds=self._screen_fds(),
                screen_sizes=self._screen_sizes(),
                preview_crop_layer=crop_preview,
                preview_target=self.preview.display_size(),
                preview_gl_sink=(self._attach_gpu_preview
                                 if self.preview.video_view is not None else None),
                canvas=self._canvas_choice(),
                skip_layers=set(self._lost_cameras),
                pool=self._source_pool,
                on_frame=self._on_frame,
            )
            # Kept before build(): a GPU graph that fails while it is being
            # built (a glupload or GL tee link) must still reach the CPU
            # fallback below, which needs the engine to know its canvas.
            self.engine = candidate.build()
            self.engine.start()
            gpu = getattr(self.engine, "preview_backend", "") == "gpu"
            if self.preview.video_view is not None and not gpu:
                if not self.preview.video_view.isVisible():
                    # Built while the window was hidden (tray, or a shortcut
                    # or tray action triggered a rebuild): the GPU view had
                    # no GL context to share.  That is not a failure of the
                    # GPU preview; rebuild with it once the window shows.
                    self._gpu_preview_pending = True
                    self.log_event("preview: window hidden; GPU preview resumes when it is shown")
                else:
                    self.log_event("preview: GPU preview did not start; using the image preview")
                    self.preview.disable_gpu()
            self.preview.set_gpu_live(gpu)
            self._update_canvas_note()
            self.log_event(f"canvas: {self.engine.canvas_backend.upper()} canvas")
            self.preview.layer_sizes = self.engine.source_sizes()
            if self.vcam_on and not self.engine.report.vcam_active:
                # The held output exists but this canvas has no tap: reattach
                # now instead of waiting for the next status tick.
                self._vcam_retry_at = 0.0
                self._recover_vcam()
        except Exception as exc:  # noqa: BLE001
            failed = self.engine if self.engine is not None else candidate
            self.engine = None
            if (failed is not None and getattr(failed, "canvas_backend", "") == "gpu"
                    and not getattr(self, "_gpu_canvas_failed", False)):
                # The GPU canvas passed its probe but this graph did not start:
                # keep the user's canvas working on the CPU compositor.
                self._gpu_canvas_failed = True
                self.log_event(f"canvas: GPU canvas failed ({exc}); using the CPU compositor")
                try:
                    failed.stop()
                except Exception:  # noqa: BLE001
                    pass
                self.start_engine()
                if self.engine is None:
                    # The CPU compositor failed too: the cause was not the GPU
                    # canvas (a busy camera, a bad encoder), so do not leave
                    # the GPU canvas switched off for the rest of the session.
                    self._gpu_canvas_failed = False
                return
            # A failed rebuild must not leave the Record button armed with a
            # recorder writing in the background.
            if self.recording or self._recorder is not None:
                self.recording = False
                self.record_button.setText("Record")
                self._finish_recorder()
            message = f"Could not start the pipeline:\n{exc}"
            self._status(message.replace("\n", " "))
            if self.headless:
                print(message, file=sys.stderr, flush=True)
            elif not self._quiet_engine_errors:
                QMessageBox.critical(self, "AnyCam", message)
            return
        self._apply_saved_camera_controls()
        self._started_at = time.monotonic()
        self.playlist_last = self._started_at
        if self.playlist_index >= len(self.playlist_ids):
            self.playlist_index = len(self.playlist_ids) - 1
        self._screen_fps_prev = None
        self._source_status = ()
        self.status_chip.setText("WAITING")
        self.start_button.setText("Stop preview")
        # 16 ms so a 60 fps canvas is actually shown at 60 fps.  The preview
        # image is capped at 960x540 (~2.1 MB per copy), which fits the frame
        # budget; a 33 ms tick capped the whole preview at 30 fps.
        self.timer.start(16)
        if self.engine.report.vcam_error:
            self._status(self.engine.report.vcam_error)
        elif self.engine.report.media_errors:
            self._status(
                "Preview running · missing media: "
                + ", ".join(self.engine.report.media_errors))
        elif self.engine.report.media_error:
            self._status(f"Preview running · {self.engine.report.media_error}")
        elif self.engine.report.camera_probe_error:
            self._status(f"Preview running · {self.engine.report.camera_probe_error}")
        else:
            self._status("Preview running")
        if self._lost_cameras:
            devices = ", ".join(sorted(set(self._lost_cameras.values())))
            self._status(f"Preview running · waiting for camera: {devices}")

    def stop_engine(self, *, clear_preview: bool = True) -> None:
        self.timer.stop()
        if self.engine is not None:
            # Finalising takes up to a few seconds (end-of-stream through the
            # muxers).  It runs on a worker while the window keeps painting;
            # ``self.engine`` is cleared first so no timer touches a graph
            # that is shutting down.
            engine, self.engine = self.engine, None
            report = self._run_with_ui(engine.stop)
            if report.get("record_path"):
                self.recording_segments.append(report["record_path"])
                self._status(f"Saved {report['record_path']}")
        # The pool keeps a screen capture alive briefly so an immediate
        # rebuild reuses it (a fresh damage-driven portal stream can be black
        # until the window repaints).  A real stop must release it afterwards.
        QTimer.singleShot(int(SCREEN_IDLE_GRACE_S * 1000) + 200,
                          self._source_pool.sweep)
        self.status_chip.setText("STOPPED")
        self.start_button.setText("Start preview")
        if clear_preview:
            self.preview.set_gpu_live(False)
            self.preview.image = None
            self.preview.layer_sizes = {}
            self.preview.update()

    def restart_engine(self) -> None:
        was_running = self.engine is not None
        if was_running:
            # Keep the last frame on screen while the replacement graph is
            # built.  Clearing it here made every scene/resolution change look
            # like the preview had disappeared, especially when screen capture
            # or the vcam took a few seconds to come back.
            self.stop_engine(clear_preview=False)
            self.start_engine()

    def toggle_engine(self) -> None:
        if self.engine is None:
            self.start_engine()
        else:
            self._forget_lost_cameras()
            self.stop_engine()

    def _teardown(self) -> None:
        """Stop everything and save, whatever fails on the way.

        Each step runs on its own: an exception in one (a pipeline that will
        not stop, a phone link that raises) used to skip the rest, including
        the project save and the recording's finalisation.
        """
        self._closing = True           # no nested event loops while quitting
        steps = (
            ("thumbnails", self._stop_thumbnails),
            ("preview timer", self.timer.stop),
            ("camera watch", self._camera_watch.stop),
            ("transform timer", self._transform_timer.stop),
            ("autosave timer", self._autosave_timer.stop),
            ("pending edits", self.preview.cancel_pending_edits),
            ("engine", lambda: self.stop_engine() if self.engine is not None else None),
            ("recorder", self._finish_recorder),
            ("virtual camera", self._finish_vcam_output),
            ("phone link", self._stop_phone_link),
            ("desktop integration", self._stop_desktop_integration),
            ("source pool", self._source_pool.stop),
            ("project save", self._save_if_needed),
            ("screen sessions", self._close_screen_sessions),
        )
        for name, step in steps:
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - keep shutting down
                print(f"[shutdown] {name} failed: {exc!r}", file=sys.stderr, flush=True)

    def shutdown(self) -> None:
        """Backstop for QApplication.aboutToQuit and headless teardown."""
        self._teardown()

    def closeEvent(self, event) -> None:
        """Finalise the recording and persist edits before the process exits."""
        self._teardown()
        event.accept()

    def _run_with_ui(self, function):
        """Run a blocking call on a thread while Qt keeps handling events."""
        if self.headless or getattr(self, "_closing", False):
            return function()
        result: dict = {}

        def work() -> None:
            try:
                result["value"] = function()
            except Exception as exc:  # noqa: BLE001 - re-raised on the UI thread
                result["error"] = exc

        thread = threading.Thread(target=work, name="anycam-blocking",
                                  daemon=True)
        app = QApplication.instance()
        # The window keeps painting, but clicks and keys are held back: they
        # used to run inside this nested loop (a second Record or a scene
        # switch in the middle of a privileged operation).  Portal shortcuts
        # check ``_ui_blocked`` for the same reason.
        self._ui_blocked = getattr(self, "_ui_blocked", 0) + 1
        try:
            thread.start()
            while thread.is_alive():
                if app is not None:
                    app.processEvents(
                        QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
                thread.join(0.02)
        finally:
            self._ui_blocked -= 1
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def log_event(self, message: str) -> None:
        """Record a session event; the ManyCam window shows these in Diagnostics."""
        print(f"[diag] {message}", file=sys.stderr, flush=True)

    def _log_preview_problem(self, message: str) -> None:
        """Report a dropped preview frame instead of painting black silently."""
        now = time.monotonic()
        if now - getattr(self, "_preview_problem_at", 0.0) < 1.0:
            return
        self._preview_problem_at = now
        print(f"[preview] dropped frame: {message}", file=sys.stderr, flush=True)

    def _on_frame(self, sample) -> None:
        from gi.repository import Gst

        buf = sample.get_buffer()
        caps = sample.get_caps()
        if buf is None or caps is None or caps.get_size() == 0:
            self._log_preview_problem("sample without buffer or caps")
            return
        structure = caps.get_structure(0)
        if not (structure.has_field("width") and structure.has_field("height")):
            self._log_preview_problem(
                f"caps without dimensions: {structure.to_string()}")
            return
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        fmt = (str(structure.get_value("format"))
               if structure.has_field("format") else "")
        if width <= 0 or height <= 0:
            self._log_preview_problem(f"invalid preview size {width}x{height}")
            return
        if fmt not in ("RGBA", "RGB"):
            # The preview branch pins RGBA; anything else would be read with
            # the wrong stride and painted as garbage or black.
            self._log_preview_problem(f"unexpected preview format {fmt!r}")
            return
        stride = width * (4 if fmt == "RGBA" else 3)
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            self._log_preview_problem("buffer could not be mapped")
            return
        try:
            mapped = len(info.data)
            if mapped < stride * height:
                self._log_preview_problem(
                    f"short frame: {mapped} bytes, need {stride * height}")
                return
            raw = bytes(info.data)[: stride * height]
        finally:
            buf.unmap(info)
        if not raw:
            self._log_preview_problem("empty frame")
            return
        self._pending_frame = (width, height, stride, raw, fmt)
        self.frame_ready.emit()

    def _resume_gpu_preview(self) -> None:
        if (self.engine is not None and self.preview.video_view is not None
                and self.preview.video_view.isVisible()
                and getattr(self.engine, "preview_backend", "") != "gpu"):
            self.restart_engine()

    def _setup_preview_output(self) -> None:
        """Resize following and the GPU preview, for either window's preview."""
        # A resize (window drag, maximise, layout change) sends many sizes;
        # the engine follows the last one after a short pause.
        self._preview_resize_timer = QTimer(self)
        self._preview_resize_timer.setSingleShot(True)
        self._preview_resize_timer.setInterval(120)
        self._preview_resize_timer.timeout.connect(self._apply_preview_size)
        self.preview.display_size_changed.connect(
            lambda *_: self._preview_resize_timer.start())
        self._enable_gpu_preview()

    def _enable_gpu_preview(self) -> None:
        """Use the GPU preview when this system can run it (see gl_preview)."""
        from .. import gl_preview

        reason = gl_preview.unavailable_reason()
        if reason:
            if gl_preview.requested() == "gpu" or "did not load" in reason:
                self.log_event(f"preview: GPU preview unavailable ({reason})")
            return
        if self.preview.enable_gpu():
            self.log_event("preview: GPU preview (qml6glsink)")

    def _attach_gpu_preview(self, sink) -> bool:
        from .. import gl_preview

        return gl_preview.attach_sink(sink, self.preview.video_view)

    def _apply_preview_size(self) -> None:
        """Have the engine scale the preview to the widget's shown size."""
        if self.engine is None:
            return
        setter = getattr(self.engine, "set_preview_target", None)
        if callable(setter):
            setter(*self.preview.display_size())

    def _update_preview(self) -> None:
        frame = self._pending_frame
        self._pending_frame = None
        if frame is None:
            return
        width, height, stride, raw, fmt = frame
        qformat = (QImage.Format.Format_RGB888 if fmt == "RGB"
                   else QImage.Format.Format_RGBA8888)
        # No .copy(): the QImage reads ``raw`` in place and the widget keeps
        # ``raw`` alive with it.  The copy cost ~0.75 ms of GUI-thread CPU per
        # frame at panel size.  ``raw`` is immutable bytes, and anything that
        # changes the image (snapshot painting, conversions) detaches first.
        image = QImage(raw, width, height, stride, qformat)
        self.preview.set_image(image, owner=raw)

    def _update_source_status(self) -> None:
        """A running canvas is not proof that its inputs are fresh."""
        if self.engine is None:
            return
        report = self.engine.report
        visible = [layer for layer in self.scene.layers if layer.visible]
        stalled = set(report.stalled_sources)
        stale = [layer.name for layer in visible
                 if layer.id in stalled and layer.id not in self._lost_cameras]
        waiting = [layer.name for layer in visible
                   if layer.id in self._lost_cameras
                   or not report.source_health.get(layer.id, {}).get("frames")]
        slow = [layer for layer in visible if layer.kind == "screen"
                and report.source_health.get(layer.id, {}).get("slow")]
        if stale:
            state = ("STALE", tuple(stale))
            message = "No fresh frames: " + ", ".join(stale)
            if any(layer.kind == "screen" and layer.id in stalled for layer in visible):
                message += " · shared window may be hidden or static"
        elif waiting or (not report.source_health and visible):
            state = ("WAITING", tuple(waiting))
            message = "Waiting for source frames" + (": " + ", ".join(waiting) if waiting else "")
        elif report.instant_fps <= 0:
            state = ("STALLED", ())
            message = "Canvas stopped delivering frames"
        elif slow:
            state = ("SLOW", tuple(layer.id for layer in slow))
            rates = [f"{layer.name}: {report.source_health[layer.id]['fps']:.1f} fps"
                     for layer in slow]
            message = "Slow screen updates · " + ", ".join(rates)
            message += " · may be static or limited by compositor frame delivery"
        else:
            state = ("LIVE", ())
            message = "Source frames resumed"
        self.status_chip.setText(state[0])
        self.status_chip.setToolTip(message)
        previous = getattr(self, "_source_status", ())
        if state != previous:
            self._source_status = state
            if state[0] != "LIVE" or (previous and previous[0] != "LIVE"):
                self._status(message)

    def _tick(self) -> None:
        if self.engine is None:
            return
        # pump() also delivers the newest appsink frame to _on_frame, on this
        # (the main) thread.
        self.engine.pump(0)
        if self._pending_frame is not None:
            self._update_preview()
        mv = getattr(self, "_multiview", None)
        if mv is not None:
            try:
                mv.pump()
            except Exception:
                pass
        grid = getattr(self, "grid", None)
        if grid is not None:
            try:
                grid.pump()
            except Exception:
                pass
        rec = self.engine.sample()
        if self.engine.mic_level_db is not None:
            db = self.engine.mic_level_db
            self.mic_level_label.setText("muted" if db < -80 else f"{db:.0f} dB")
        self._advance_playlist(time.monotonic())
        if rec is not None:
            self._update_source_status()
            stats = (f"{rec['instant_fps']:5.1f} fps · {rec['frames']} frames · "
                     f"CPU {rec['cpu_pct']}% · RSS {rec['rss_mib']} MiB")
            source_fps = self._screen_source_fps()
            if source_fps is not None:
                # The canvas rate says nothing about the *captured* frames: a
                # screen source that stopped producing still repeats its last
                # frame at 60 fps.  Show the source rate next to it.
                stats += f" · screen {source_fps:.0f} fps"
            self.stats_label.setText(stats)
        screen_error = getattr(self.engine.report, "screen_error", "")
        if screen_error and screen_error != self._screen_error_shown:
            self._screen_error_shown = screen_error
            self._status(screen_error)
        source_errors = getattr(self.engine.report, "source_errors", ())
        camera_error = next(
            (error for error in source_errors if self._camera_error_layers(error)), "")
        if camera_error:
            self._handle_pipeline_error(f"Camera source error: {camera_error}")
            return
        if self.engine.report.error:
            self._handle_pipeline_error(
                f"Pipeline error: {self.engine.report.error}")
            return
        rtmp_error = getattr(self.engine.report, "rtmp_error", "")
        if rtmp_error and rtmp_error != getattr(self, "_rtmp_error_shown", ""):
            self._rtmp_error_shown = rtmp_error
            self.live = False
            if hasattr(self, "live_button"):
                self.live_button.setText("Go Live")
            self._status(f"Stream stopped: {rtmp_error}")
        vcam_error = getattr(self.engine.report, "vcam_error", "")
        if vcam_error:
            if vcam_error != self._vcam_error_shown:
                self._vcam_error_shown = vcam_error
                self.log_event(f"virtual camera: {vcam_error}")
                self._status(vcam_error)
            # A sink error is not the user switching the camera off.
            self._recover_vcam()
        elif self._vcam_error_shown:
            self._vcam_error_shown = ""
        if self.vcam_on and not self.engine.report.vcam_active:
            # The output may have failed before this canvas was built, so no
            # vcam_error was ever latched.  Retry until the tap is attached.
            self._recover_vcam()
        source_errors = getattr(self.engine.report, "source_errors", ())
        non_camera = [e for e in source_errors
                      if not self._camera_error_layers(e)]
        if non_camera:
            msg = non_camera[0]
            if msg != getattr(self, "_source_error_shown", ""):
                self._source_error_shown = msg
                self._status(f"Source error: {msg}")
        if self._render_seconds and time.monotonic() - self._started_at >= self._render_seconds:
            self._render_seconds = 0.0
            QTimer.singleShot(0, self._auto_screenshot)

    def _save_screenshot(self) -> bool:
        path = getattr(self, "auto_screenshot_path", "")
        has_frame = self.preview.has_frame()
        if not path:
            return has_frame
        pixmap = self.grab()
        saved = pixmap.save(path)
        print(f"screenshot {path} {pixmap.width()}x{pixmap.height()} "
              f"frame={'yes' if has_frame else 'no'}", flush=True)
        return saved and has_frame

    def _auto_screenshot(self) -> None:
        if getattr(self, "_screenshot_done", False):
            return
        self._screenshot_done = True
        self.screenshot_ok = self._save_screenshot()
        self.stop_engine()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _screenshot_deadline(self) -> None:
        """Hard stop for headless rendering, even if the pipeline never ran."""
        if getattr(self, "_screenshot_done", False):
            return
        self._screenshot_done = True
        self.screenshot_ok = self._save_screenshot()
        print("screenshot deadline reached", file=sys.stderr, flush=True)
        self.stop_engine()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _status(self, text: str) -> None:
        self.statusBar().showMessage(text)
