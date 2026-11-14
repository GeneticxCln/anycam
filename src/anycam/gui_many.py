"""ManyCam-inspired GUI layout for AnyCam.

The window keeps three things on screen: the program monitor, a numbered
preset deck, and a sidebar with layouts, effects and scenes.  Everything else
(sources, audio, playlist, output) lives in a separate settings window opened
from the corner button, so the main window is only about composing.

An empty scene shows its layout as empty slots.  Each empty slot is a dashed
``+`` button over the monitor; clicking it assigns a camera, screen, video,
image, phone URL or test pattern to that slot.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QScrollArea,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import config
from .ui import startup
from .gui import (
    AnyCamWindow, PreviewWidget, claim_gui_instance, serve_instance_requests,
)
from .layouts import GRID_KINDS, LAYOUT_PRESETS, layout_preset
from .model import Layer, Project, Scene, empty_project, new_id


def _layout_pixmap(preset, width: int = 72, height: int = 42) -> QPixmap:
    """Draw a tiny preview of a layout: the slot rectangles."""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#0b0d10"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    for index, (x, y, w, h) in enumerate(preset.boxes):
        painter.setBrush(QColor("#5eead4") if index == 0 else QColor("#31424c"))
        painter.drawRoundedRect(
            QRectF(x * width + 2, y * height + 2,
                   max(2.0, w * width - 4), max(2.0, h * height - 4)), 3, 3)
    painter.end()
    return pixmap


def _scene_pixmap(scene, width: int = 84, height: int = 48) -> QPixmap:
    """Composition thumbnail for a preset: its boxes, coloured by kind."""
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor("#0b0d10"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    palette = {
        "camera": QColor("#5eead4"),
        "screen": QColor("#7aa2f7"),
        "network": QColor("#f6c177"),
        "phone": QColor("#f6c177"),
        "video": QColor("#bb9af7"),
        "test": QColor("#9ece6a"),
        "image": QColor("#7dcfff"),
        "text": QColor("#d7e3ea"),
        "logo": QColor("#d7e3ea"),
    }
    for layer in sorted(scene.layers, key=lambda item: item.z):
        if not layer.visible:
            continue
        x, y, w, h = layer.box
        rect = QRectF(x * width + 1, y * height + 1,
                      max(2.0, w * width - 2), max(2.0, h * height - 2))
        if layer.kind in ("text", "logo"):
            rect.setWidth(max(2.0, rect.width() * 0.6))
            rect.setHeight(max(1.0, rect.height() * 0.5))
        painter.setBrush(palette.get(layer.kind, QColor("#31424c")))
        painter.drawRoundedRect(rect, 2, 2)
    painter.end()
    return pixmap


def _is_legacy_default(project: Project) -> bool:
    """The old built-in starter (camera + AnyCam/LIVE titles), untouched.

    Only that exact placeholder is replaced with the empty single layout;
    a project the user edited is loaded as-is.
    """
    if len(project.scenes) != 1:
        return False
    scene = project.scenes[0]
    if scene.name != "Camera" or len(scene.layers) != 3:
        return False
    kinds = [layer.kind for layer in scene.layers]
    return (kinds == ["camera", "text", "text"]
            and not scene.layers[0].source
            and scene.layers[1].source == "AnyCam"
            and scene.layers[2].source == "LIVE")


def _overlap(a, b) -> float:
    """Area shared by two (x, y, w, h) boxes."""
    width = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    height = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0.0, width) * max(0.0, height)


class ManyCamWindow(AnyCamWindow):
    # ------------------------------------------------------------------- ui
    def _load_saved_project(self) -> Project:
        path = config.project_path()
        if not path.exists():
            return empty_project()
        project = super()._load_saved_project()
        if _is_legacy_default(project):
            return empty_project()
        return project

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)

        # ---- top bar -------------------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(8)
        logo_path = config.assets_dir() / "anycam-logo.png"
        if logo_path.exists():
            logo = QLabel()
            logo.setPixmap(QPixmap(str(logo_path)).scaled(
                28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            header.addWidget(logo)
        title = QLabel("AnyCam")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        self.scene_combo = QComboBox()
        self.scene_combo.setVisible(False)
        header.addWidget(self.scene_combo)
        self._build_studio_controls(header)
        self.settings_button = QPushButton("Settings")
        self.settings_button.setToolTip("Sources, audio, playlist and output")
        self.settings_button.clicked.connect(lambda: self._open_settings())
        header.addWidget(self.settings_button)
        self.project_button = QPushButton("Project")
        self.project_button.setToolTip("Open, save as, import and export")
        project_menu = QMenu(self.project_button)
        for label, handler in (("Open project…", lambda: self.open_project()),
                               ("Save", self.save_project),
                               ("Save project as…", lambda: self.save_project_as()),
                               ("Import scene…", lambda: self.import_scene()),
                               ("Export scene…", lambda: self.export_scene())):
            project_menu.addAction(label, handler)
        self.project_button.setMenu(project_menu)
        header.addWidget(self.project_button)
        self.status_chip = QLabel("STOPPED")
        self.status_chip.setObjectName("accent")
        header.addWidget(self.status_chip)
        outer.addLayout(header)

        self.scene_combo.currentIndexChanged.connect(self._scene_changed)
        self._refresh_scenes()

        # Shown when an application opened the loopback node before AnyCam
        # was feeding it: that open fails and only the application can retry.
        self.vcam_hint = QLabel("")
        self.vcam_hint.setObjectName("hint")
        self.vcam_hint.setWordWrap(True)
        self.vcam_hint.hide()
        self.vcam_dismiss = QPushButton("Dismiss")
        self.vcam_dismiss.setObjectName("muted")
        self.vcam_dismiss.hide()
        self.vcam_dismiss.clicked.connect(self.dismiss_vcam_hint)
        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_row.addWidget(self.vcam_hint, 1)
        hint_row.addWidget(self.vcam_dismiss, 0)
        outer.addLayout(hint_row)

        # ---- main area: program monitor (left) + side panel (right) -------
        body = QHBoxLayout()
        body.setSpacing(6)
        outer.addLayout(body, 1)

        preview_panel = QFrame()
        preview_panel.setObjectName("panel")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(6, 6, 6, 6)

        monitor_header = QHBoxLayout()
        program = QLabel("Program")
        program.setObjectName("muted")
        monitor_header.addWidget(program)
        monitor_header.addStretch(1)
        self._syncing_format = False
        self._format_sizes = [(1280, 720), (1920, 1080),
                              (2560, 1440), (3840, 2160)]
        self.resolution_combo = QComboBox()
        for index, (width, height) in enumerate(self._format_sizes):
            self.resolution_combo.addItem(f"{width}×{height}", index)
        self.fps_combo = QComboBox()
        for rate in (30, 60):
            self.fps_combo.addItem(f"{rate} fps", rate)
        self._sync_output_controls()
        monitor_header.addWidget(self.resolution_combo)
        monitor_header.addWidget(self.fps_combo)
        self.resolution_combo.currentIndexChanged.connect(self._format_changed)
        self.fps_combo.currentIndexChanged.connect(self._format_changed)
        preview_layout.addLayout(monitor_header)

        self.preview = PreviewWidget()
        self.preview.set_snap_enabled(self.project.editor.snap_enabled)
        self.preview.set_snap_threshold(self.project.editor.snap_px)
        self.preview.set_scene(self.scene)
        self.preview.set_selection_auto_hide(True)
        self.preview.selection_changed.connect(self._preview_selection)
        self.preview.context_menu_requested.connect(self._preview_context_menu)
        self.preview.geometry_changed.connect(self._preview_geometry)
        self.preview.edit_started.connect(self._preview_edit_started)
        self.preview.edit_finished.connect(self._preview_edit_finished)
        self.preview.installEventFilter(self)
        self._setup_preview_output()
        self.stage_add_buttons: list[QPushButton] = []
        self._stage_slots: list[tuple[QPushButton, tuple]] = []
        self._stage_canvas: tuple | None = None
        preview_layout.addWidget(self.preview, 1)
        self.draw_bar = self._build_draw_bar()
        preview_layout.addWidget(self.draw_bar)

        controls = QHBoxLayout()
        self.start_button = QPushButton("Start preview")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.toggle_engine)
        self.record_button = QPushButton("Record")
        self.record_button.clicked.connect(self.toggle_record)
        self.vcam_button = QPushButton("Virtual cam")
        self.vcam_button.clicked.connect(self.toggle_vcam)
        self.snapshot_button = QPushButton("Snapshot")
        self.snapshot_button.clicked.connect(self.snapshot)
        self.multiview_button = QPushButton("Multi-view")
        self.multiview_button.clicked.connect(self.toggle_multiview)
        self.draw_button = QPushButton("Draw")
        self.draw_button.setCheckable(True)
        self.draw_button.setToolTip("Draw on the picture: pen, marker, arrows and boxes")
        self.draw_button.clicked.connect(self.toggle_draw)
        for button in (self.start_button, self.record_button, self.vcam_button,
                       self.snapshot_button, self.multiview_button, self.draw_button):
            controls.addWidget(button)
        controls.addStretch(1)
        self.stats_label = QLabel("idle")
        self.stats_label.setObjectName("muted")
        controls.addWidget(self.stats_label)
        preview_layout.addLayout(controls)
        body.addWidget(preview_panel, 3)

        side = QTabWidget()
        side.setMinimumWidth(320)
        side.setMaximumWidth(440)
        side.addTab(self._layouts_tab(), "Layouts")
        side.addTab(self._effects_tab(), "Effects")
        # The backdrops the README lists lived only in the classic window;
        # here _apply_background() crashed on the missing inset checkbox.
        side.addTab(self._backdrop_tab(), "Stage")
        side.addTab(self._scene_tab(), "Scenes")
        self.side_tabs = side
        body.addWidget(side, 1)

        # ---- numbered preset deck, directly under the monitor -------------
        deck = QFrame()
        deck.setObjectName("panel")
        deck_layout = QHBoxLayout(deck)
        deck_layout.setContentsMargins(6, 4, 6, 4)
        deck_layout.setSpacing(6)
        preset_label = QLabel("Presets")
        preset_label.setObjectName("muted")
        deck_layout.addWidget(preset_label)
        self.scene_cards = QHBoxLayout()
        self.scene_cards.setSpacing(4)
        scene_box = QWidget()
        scene_box.setLayout(self.scene_cards)
        deck_layout.addWidget(scene_box, 1)
        add_preset = QPushButton("+")
        add_preset.setFixedWidth(32)
        add_preset.setToolTip("New empty preset")
        add_preset.clicked.connect(self.add_scene)
        deck_layout.addWidget(add_preset)
        outer.addWidget(deck)
        self._refresh_scene_cards()

        self.setStatusBar(QStatusBar())
        if self._load_warning:
            self._status(self._load_warning)
        else:
            self._status("Ready. No virtual camera output yet.")

        # Settings is a separate window; build it once, keep it hidden.
        self._settings: QDialog | None = None
        self._diag_thread: threading.Thread | None = None
        self._diag_result: dict = {}
        self._diag_timer = QTimer(self)
        self._diag_timer.setInterval(150)
        self._diag_timer.timeout.connect(self._diag_poll)
        self._fix_thread: threading.Thread | None = None
        self._fix_result: dict = {}
        self._fix_timer = QTimer(self)
        self._fix_timer.setInterval(150)
        self._fix_timer.timeout.connect(self._fix_poll)
        # Events logged before the settings window exists are shown once the
        # Diagnostics tab is built.
        self._pending_log_lines: list[str] = []
        self._settings = self._build_settings()
        self._refresh_stage_slots()

    # --------------------------------------------------------------- settings
    def _build_settings(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle("AnyCam settings")
        dialog.resize(520, 680)
        tabs = QTabWidget(dialog)

        def scrolled(page: QWidget) -> QScrollArea:
            # Each page scrolls instead of forcing the dialog taller than a
            # small screen (the Output page alone wants ~1170 px).
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            area.setWidget(page)
            return area

        tabs.addTab(scrolled(self._layers_tab()), "Sources")
        tabs.addTab(scrolled(self._audio_tab()), "Audio")
        tabs.addTab(scrolled(self._playlist_tab()), "Media")
        tabs.addTab(scrolled(self._output_tab()), "Output")
        tabs.addTab(scrolled(self._stream_tab()), "Stream")
        tabs.addTab(scrolled(self._diagnostics_tab()), "Diagnostics")
        tabs.addTab(scrolled(self._general_tab()), "General")
        tabs.addTab(scrolled(self._about_tab()), "About")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(tabs)
        self.settings_tabs = tabs
        self.apply_interface_polish(dialog)
        return dialog

    def _about_tab(self) -> QWidget:
        """Version, licence, runtime versions and credits."""
        from . import about

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(8)
        title = QLabel(about.headline())
        title.setObjectName("aboutTitle")
        title.setStyleSheet("font-size: 30px; font-weight: 700; color: #5eead0;")
        layout.addWidget(title)
        release = QLabel(about.release_line())
        release.setObjectName("aboutRelease")
        release.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(release)
        blurb = QLabel(
            "A Linux-native virtual camera studio: cameras, screens, video, "
            "overlays, face masks, frames and animations composed into one "
            "picture for Discord, Chrome, OBS and recordings.")
        blurb.setWordWrap(True)
        blurb.setObjectName("muted")
        layout.addWidget(blurb)
        link = QLabel(f'<a href="{about.REPO_URL}">{about.REPO_URL}</a>  \u00b7  '
                      f"{about.LICENSE_NAME} licence")
        link.setOpenExternalLinks(True)
        palette = link.palette()
        palette.setColor(palette.ColorRole.Link, QColor("#5eead0"))   # readable on the dark theme
        link.setPalette(palette)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(link)

        runtime = QGroupBox("This installation")
        grid = QGridLayout(runtime)
        for row, (name, value) in enumerate(about.runtime_rows()):
            label = QLabel(name)
            label.setObjectName("muted")
            text = QLabel(value)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(label, row, 0)
            grid.addWidget(text, row, 1)
        grid.setColumnStretch(1, 1)
        layout.addWidget(runtime)

        credits = QGroupBox("Built with")
        credit_grid = QGridLayout(credits)
        for row, (what, who, licence) in enumerate(about.CREDITS):
            credit_grid.addWidget(QLabel(what), row, 0)
            names = QLabel(who)
            names.setWordWrap(True)
            credit_grid.addWidget(names, row, 1)
            credit_grid.addWidget(QLabel(licence), row, 2)
        credit_grid.setColumnStretch(1, 1)
        layout.addWidget(credits)

        copy = QPushButton("Copy version details")
        copy.setToolTip("Copy the release and runtime versions for a bug report")
        copy.clicked.connect(self._copy_version_details)
        layout.addWidget(copy)
        layout.addStretch(1)
        return page

    def _copy_version_details(self) -> None:
        from . import about

        lines = [f"{about.headline()} - {about.release_line()}"]
        lines += [f"{name}: {value}" for name, value in about.runtime_rows()]
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText("\n".join(lines))
        self._status("Version details copied")

    def _open_settings(self, tab: int = 0) -> None:
        dialog = self._settings
        if dialog is None:
            dialog = self._settings = self._build_settings()
        if not dialog.isVisible():
            dialog.move(self.x() + self.width() - dialog.width() - 24,
                        self.y() + 64)
        if 0 <= tab < self.settings_tabs.count():
            self.settings_tabs.setCurrentIndex(tab)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._refresh_diag_state()
        if not self._diag_result and (
                self._diag_thread is None or not self._diag_thread.is_alive()):
            self.measure_vcam_cost()

    # ----------------------------------------------------------- diagnostics
    def log_event(self, message: str) -> None:
        """Show session events in the Diagnostics tab as they happen."""
        super().log_event(message)
        line = f"{time.strftime('%H:%M:%S')}  {message}"
        widget = getattr(self, "diag_log", None)
        if widget is None:
            pending = getattr(self, "_pending_log_lines", None)
            if pending is not None:
                pending.append(line)
            return
        widget.appendPlainText(line)

    def _diagnostics_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.diag_state = QLabel("Virtual camera: not started")
        self.diag_state.setWordWrap(True)
        layout.addWidget(self.diag_state)
        self.diag_chromium = QLabel("")
        self.diag_chromium.setWordWrap(True)
        layout.addWidget(self.diag_chromium)
        self.diag_fix = QPushButton("Fix Discord visibility")
        self.diag_fix.setToolTip(
            "One-time setup (asks for your password): permanent driver "
            "settings with exclusive_caps=1 so Discord, Vesktop and Chrome "
            "list the camera")
        self.diag_fix.clicked.connect(self.fix_vcam_visibility)
        self.diag_fix.hide()
        layout.addWidget(self.diag_fix)
        self.diag_cost = QLabel("Conversion cost: not measured yet.")
        self.diag_cost.setWordWrap(True)
        layout.addWidget(self.diag_cost)
        self.diag_button = QPushButton("Measure conversion cost")
        self.diag_button.clicked.connect(self.measure_vcam_cost)
        layout.addWidget(self.diag_button)
        hint = QLabel(
            "Measured through the same converters the virtual camera uses, on "
            "synthetic frames at the project size. No camera or loopback "
            "device is opened; the GPU is chosen whenever it can deliver a "
            "frame.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._build_diagnostics_log(layout)
        layout.addStretch(1)
        return page

    def _open_log_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from . import applog

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(applog.log_path().parent)))

    def _copy_log(self) -> None:
        from . import applog

        QApplication.clipboard().setText(applog.tail(400))
        self._status("The last 400 log lines are on the clipboard.")

    def _build_diagnostics_log(self, layout) -> None:
        """Session log: rebuilds, screen-capture reuse and vcam recovery."""
        title = QLabel("Session log")
        title.setObjectName("muted")
        layout.addWidget(title)
        self.diag_log = QPlainTextEdit()
        self.diag_log.setReadOnly(True)
        self.diag_log.setMaximumBlockCount(300)
        self.diag_log.setMinimumHeight(140)
        self.diag_log.setPlaceholderText(
            "Virtual camera, screen capture and rebuild events appear here.")
        layout.addWidget(self.diag_log, 1)
        log_row = QHBoxLayout()
        open_log = QPushButton("Open log folder")
        open_log.clicked.connect(self._open_log_folder)
        copy_log = QPushButton("Copy log")
        copy_log.clicked.connect(self._copy_log)
        log_row.addWidget(open_log)
        log_row.addWidget(copy_log)
        log_row.addStretch(1)
        layout.addLayout(log_row)
        pending = getattr(self, "_pending_log_lines", [])
        self._pending_log_lines = []
        for line in pending:
            self.diag_log.appendPlainText(line)

    def _refresh_diag_state(self) -> None:
        if not hasattr(self, "diag_state"):
            return
        device = self.project.output.vcam_device or config.loopback_device()
        output = getattr(self, "_vcam_output", None)
        backend = getattr(output, "backend", "v4l2") if output is not None else "v4l2"
        if output is not None and output.active:
            path = {"gpu": "GPU (glcolorconvert)",
                    "cpu": "CPU (videoconvert)"}.get(
                        getattr(output, "conversion", ""), "converting")
            where = {"v4l2": device, "pipewire": "PipeWire node “AnyCam”",
                     "both": f"{device} + PipeWire node “AnyCam”"}.get(backend, device)
            self.diag_state.setText(
                f"Virtual camera: {where} · running · {path}")
        else:
            self.diag_state.setText(f"Virtual camera: {device} · stopped")
        if output is not None and output.active and backend == "pipewire":
            # No loopback in use: its driver state is irrelevant here.
            self.diag_chromium.setText(
                "Chromium/Discord visibility: PipeWire camera only — listed by "
                "apps with PipeWire cameras enabled.")
            if hasattr(self, "diag_fix"):
                self.diag_fix.setVisible(False)
            return
        caps = config.vcam_exclusive_caps()
        if caps is None:
            self.diag_chromium.setText(
                "Chromium/Discord visibility: v4l2loopback is not loaded.")
        elif caps:
            udev = config.vcam_udev_capabilities(device)
            if udev is not None and "capture" not in udev:
                sysname = device.rsplit("/", 1)[-1]
                self.diag_chromium.setText(
                    "Chromium/Discord visibility: NOT listed — udev still "
                    f"reports video output for {sysname}. Click the button "
                    "below to set the camera up.")
            else:
                self.diag_chromium.setText(
                    "Chromium/Discord visibility: listed (exclusive_caps=1).")
        else:
            self.diag_chromium.setText(
                "Chromium/Discord visibility: NOT listed — the driver was "
                "loaded with exclusive_caps=0. Click the button below to set "
                "the camera up (one password prompt).")
        if hasattr(self, "diag_fix"):
            # Only an action while the setup is incomplete: a correct module
            # is never reloaded (that would interrupt the camera).
            self.diag_fix.setVisible(bool(config.vcam_setup_needed(device)))

    def fix_vcam_visibility(self) -> None:
        """Reload v4l2loopback so Chromium lists the camera, then re-hold it.

        The module cannot be unloaded while AnyCam keeps ``/dev/video10``
        open, so the window's sink is released first and recreated after the
        reload; a running canvas only detaches its tap, so the preview is not
        interrupted.
        """
        if self._fix_thread is not None and self._fix_thread.is_alive():
            return
        device = self.project.output.vcam_device or config.loopback_device()
        others = config.other_virtual_cameras(device)
        if others:
            # Refuse before touching anything: the reload unloads the whole
            # module, taking these cameras away from the apps using them.
            self.diag_chromium.setText(
                "Chromium/Discord visibility: not reloaded, it would also "
                "remove " + ", ".join(others))
            self._status("Reload v4l2loopback manually: other virtual "
                         "cameras share the module")
            return
        if self.engine is not None:
            self.engine.detach_vcam()
        self._finish_vcam_output()
        self.diag_fix.setEnabled(False)
        self.diag_chromium.setText(
            "Chromium/Discord visibility: reloading v4l2loopback…")
        result = self._fix_result = {}

        def run() -> None:
            try:
                result["error"] = config.setup_vcam_system(device)
            except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
                result["error"] = str(exc)

        self._fix_thread = threading.Thread(
            target=run, name="anycam-vcam-fix", daemon=True)
        self._fix_thread.start()
        self._fix_timer.start()

    def _fix_poll(self) -> None:
        if self._fix_thread is not None and self._fix_thread.is_alive():
            return
        self._fix_timer.stop()
        self.diag_fix.setEnabled(True)
        error = str(self._fix_result.get("error") or "")
        held = self.hold_vcam_device()
        if held and not error:
            # Re-announce the reloaded node with the idle card flowing, so
            # Chromium apps list it straight away.
            self.feed_held_vcam()
            self._vcam_udev_attempted.discard(
                self.project.output.vcam_device or config.loopback_device())
            self.ensure_vcam_visible()
        if not error and self.engine is not None and self.vcam_on and held:
            if not self.engine.attach_vcam(self._vcam_output):
                self._status(self.engine.report.vcam_error
                             or "Virtual camera could not resume")
        self._refresh_diag_state()
        if error:
            # Keep the reason after the refresh: the generic line would hide
            # whether authorization or an application holding the node failed.
            self.diag_chromium.setText(
                f"Chromium/Discord visibility: reload failed — {error}")
            self._status("Could not reload v4l2loopback for Discord")
            return
        self._status("Virtual camera set up: Discord and Chrome can list AnyCam")
        self.note_vcam_consumers()

    def measure_vcam_cost(self) -> None:
        """Benchmark both conversion chains off the GUI thread."""
        if self._diag_thread is not None and self._diag_thread.is_alive():
            return
        width = self.project.output.canvas_w or config.CANVAS_W
        height = self.project.output.canvas_h or config.CANVAS_H
        fps = self.project.output.fps or config.CANVAS_FPS
        self._diag_result = {}
        result = self._diag_result
        self.diag_button.setEnabled(False)
        self.diag_cost.setText("Conversion cost: measuring…")

        def run() -> None:
            try:
                from .engine.outputs.vcam import measure_conversion
                result.update(measure_conversion(width, height, fps))
            except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
                result["error"] = str(exc)

        self._diag_thread = threading.Thread(
            target=run, name="anycam-vcam-diag", daemon=True)
        self._diag_thread.start()
        self._diag_timer.start()

    def _diag_poll(self) -> None:
        if self._diag_thread is not None and self._diag_thread.is_alive():
            return
        self._diag_timer.stop()
        self.diag_button.setEnabled(True)
        self._refresh_diag_state()
        result = self._diag_result
        if result.get("error"):
            self.diag_cost.setText(
                f"Conversion cost: measurement failed ({result['error']})")
            return
        if not result:
            return
        costs = []
        if result.get("gpu") is not None:
            costs.append(f"GPU {result['gpu']:.2f} ms/frame")
        if result.get("cpu") is not None:
            costs.append(f"CPU {result['cpu']:.2f} ms/frame")
        measured = " · ".join(costs) or "path unavailable"
        chosen = "GPU" if result.get("chosen") == "gpu" else "CPU"
        self.diag_cost.setText(
            f"Measured at {result['width']}×{result['height']}@"
            f"{result['fps']} fps: {measured}. Path chosen: {chosen}.")

    # ---------------------------------------------------------------- scenes
    def _refresh_scenes(self) -> None:
        super()._refresh_scenes()
        self._refresh_scene_cards()

    def _refresh_scene_cards(self) -> None:
        if not hasattr(self, "scene_cards"):
            return
        while self.scene_cards.count():
            widget = self.scene_cards.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for index, scene in enumerate(self.project.scenes, start=1):
            button = QPushButton(f"{index}  {scene.name}")
            button.setObjectName("preset")
            button.setIcon(QIcon(_scene_pixmap(scene)))
            button.setIconSize(QSize(72, 40))
            button.setCheckable(True)
            button.setChecked(scene.id == self.project.active_scene)
            button.clicked.connect(
                lambda _checked=False, sid=scene.id: self._switch_scene_card(sid))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda pos, sid=scene.id, btn=button:
                self._scene_context_menu(sid, btn.mapToGlobal(pos)))
            self.scene_cards.addWidget(button)

    def _scene_context_menu(self, scene_id: str, position) -> None:
        menu = QMenu(self)
        rename = menu.addAction("Rename…")
        duplicate = menu.addAction("Duplicate")
        ids = [scene.id for scene in self.project.scenes]
        left = menu.addAction("Move left")
        left.setEnabled(scene_id in ids and ids.index(scene_id) > 0)
        right = menu.addAction("Move right")
        right.setEnabled(scene_id in ids and ids.index(scene_id) < len(ids) - 1)
        menu.addSeparator()
        export = menu.addAction("Export scene…")
        remove = menu.addAction("Remove")
        remove.setEnabled(len(self.project.scenes) > 1)
        chosen = menu.exec(position)
        if chosen is rename:
            self.rename_scene(scene_id)
        elif chosen is duplicate:
            self.duplicate_scene(scene_id)
        elif chosen is left:
            self.move_scene(scene_id, -1)
        elif chosen is right:
            self.move_scene(scene_id, 1)
        elif chosen is export:
            self.export_scene(scene_id)
        elif chosen is remove:
            self.remove_scene(scene_id)

    def add_scene(self) -> None:
        """Add a new preset that starts as an empty single layout."""
        scene = Scene(new_id("scene"), f"Scene {len(self.project.scenes) + 1}",
                      layout="full")
        self._append_scene(scene)

    def _scene_changed(self, index: int) -> None:
        super()._scene_changed(index)
        self._refresh_scene_cards()
        self._sync_layout_controls()
        self._refresh_stage_slots()

    def _switch_scene_card(self, scene_id: str) -> None:
        index = self.scene_combo.findData(scene_id)
        if index >= 0 and index != self.scene_combo.currentIndex():
            self.scene_combo.setCurrentIndex(index)

    # ---------------------------------------------------------------- layers
    def _refresh_layers(self) -> None:
        super()._refresh_layers()
        self._refresh_stage_slots()

    # ---------------------------------------------------------------- layout
    def _layouts_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel(
            "Choose a layout, then click a + on the canvas to fill each slot. "
            "Titles and logos are not part of a layout.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # A project or preset can name a layout this build does not know (a
        # newer build, a hand edit): show it as the single layout instead of
        # failing to build the window.
        self._current_layout_key = (self.scene.layout if layout_preset(self.scene.layout)
                                    else "full")
        grid = QGridLayout()
        grid.setSpacing(6)
        self.layout_buttons: dict[str, QToolButton] = {}
        self._layout_group = QButtonGroup(page)
        self._layout_group.setExclusive(True)
        for index, preset in enumerate(LAYOUT_PRESETS):
            button = QToolButton()
            button.setCheckable(True)
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIcon(QIcon(_layout_pixmap(preset)))
            button.setIconSize(QSize(72, 42))
            button.setText(preset.short)
            button.setToolTip(preset.label)
            button.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Fixed)
            self._layout_group.addButton(button, index)
            self.layout_buttons[preset.key] = button
            grid.addWidget(button, index // 2, index % 2)
        layout.addLayout(grid)
        layout.addStretch(1)
        button = (self.layout_buttons.get(self._current_layout_key)
                  or self.layout_buttons.get("full"))
        if button is not None:
            button.setChecked(True)
        self._layout_group.idClicked.connect(self._layout_clicked)
        return page

    def _layout_clicked(self, index: int) -> None:
        if 0 <= index < len(LAYOUT_PRESETS):
            self.apply_layout(LAYOUT_PRESETS[index].key)

    def _active_layout(self):
        return (layout_preset(self.scene.layout)
                or layout_preset(getattr(self, "_current_layout_key", "full"))
                or LAYOUT_PRESETS[0])

    def _sync_layout_controls(self) -> None:
        if not hasattr(self, "layout_buttons"):
            return
        if layout_preset(self.scene.layout):
            self._current_layout_key = self.scene.layout
        button = self.layout_buttons.get(self._current_layout_key)
        if button is not None:
            button.setChecked(True)

    def _slot_layers(self, preset) -> dict[int, Layer]:
        """Which layer occupies each slot of ``preset`` right now.

        A layer sitting exactly on a slot owns it.  Every other visible
        source still owns a slot: the free one it overlaps most, else the
        first free one.  So moving or resizing a source with the mouse (or a
        stage insetting it) never makes its slot look empty; a slot shows "+"
        only when there are fewer sources than slots.
        """
        wanted = {tuple(round(value, 6) for value in box): index
                  for index, box in enumerate(preset.boxes)}
        slots: dict[int, Layer] = {}
        loose: list[Layer] = []
        for layer in sorted(self.scene.layers, key=lambda item: item.z):
            if not layer.visible or layer.kind not in GRID_KINDS:
                continue
            index = wanted.get(tuple(round(value, 6) for value in layer.box))
            if index is not None and index not in slots:
                slots[index] = layer
            else:
                loose.append(layer)
        for layer in loose:
            free = [index for index in range(len(preset.boxes)) if index not in slots]
            if not free:
                break
            best = max(free, key=lambda index: _overlap(layer.box, preset.boxes[index]))
            slots[best] = layer
        return slots

    def _slot_context_menu(self, button: QPushButton, position) -> None:
        """Right-click on a + overlay: forward it to the layer underneath.

        The + button covers its whole empty slot, so without this a floating
        source below it could not be right-clicked or removed.
        """
        local = QPointF(button.mapTo(self.preview, position))
        layer = self.preview.layer_at(local)
        # A full-canvas overlay (particles) is not "a layer under the +";
        # forwarding to it hid the slot menu whenever one was active.
        if layer is not None and layer.kind != "particles":
            self._preview_context_menu(layer.id, button.mapToGlobal(position))
            return
        for candidate, box in self._stage_slots:
            if candidate is button:
                preset = self._active_layout()
                try:
                    index = list(preset.boxes).index(box)
                except ValueError:
                    index = -1
                self._slot_menu(index, button)
                return

    def _slot_menu(self, index: int, button: QPushButton) -> None:
        preset = self._active_layout()
        if not (0 <= index < len(preset.boxes)):
            return
        box = preset.boxes[index]
        menu = QMenu(self)
        for layer in sorted(self.scene.layers, key=lambda item: item.z):
            if layer.kind not in GRID_KINDS or not layer.visible:
                continue
            action = menu.addAction(f"Use {layer.name}")
            action.setData(layer.id)
        if menu.actions():
            menu.addSeparator()
        for label, kind in (("Add camera…", "camera"),
                            ("Add video…", "video"),
                            ("Add image…", "image"),
                            ("Add phone camera (QR code)…", "phone"),
                            ("Add network stream URL…", "network"),
                            ("Add test pattern", "test")):
            action = menu.addAction(label)
            action.setData(f"add:{kind}")

        screen = menu.addAction("Add native desktop share…")
        screen.setData("add:screen")
        if self._last_screen_session is not None:
            different = menu.addAction("Add a different desktop share…")
            different.setData("add:screen-new")
        share_audio = menu.addMenu("Share desktop audio")
        monitors = [source for source in config.list_audio_sources()
                    if source.is_monitor]
        if not monitors:
            action = share_audio.addAction("No monitor source found")
            action.setEnabled(False)
        for source in monitors:
            action = share_audio.addAction(source.description or source.name)
            action.setData(f"desktop:{source.name}")
        if self.project.audio.desktop_device:
            share_audio.addSeparator()
            action = share_audio.addAction("Stop desktop audio")
            action.setData("desktop:")

        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        data = str(chosen.data() or "")
        if data.startswith("desktop:"):
            self._share_desktop_audio(data.split(":", 1)[1])
            return
        if data.startswith("add:"):
            layer = self.add_layer(data.split(":", 1)[1], box=box)
            if layer is not None:
                self.scene.layout = preset.key
                self._current_layout_key = preset.key
                self._refresh_scene_cards()
                if self.engine is None and self.isVisible():
                    self.toggle_engine()
            return
        layer = self.scene.layer(data)
        if layer is not None:
            self._assign_slot(index, layer)

    def _share_desktop_audio(self, device: str) -> None:
        combo = getattr(self, "desktop_combo", None)
        if combo is not None:
            index = combo.findData(device)
            if index < 0 and device:
                combo.addItem(device, device)
                index = combo.count() - 1
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)
        self.project.audio.desktop_device = device
        self.project.audio.desktop_muted = False
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
        self._status("Desktop audio off" if not device
                     else f"Desktop audio: {device}")

    def _assign_slot(self, index: int, layer: Layer) -> None:
        """Put ``layer`` in slot ``index``, swapping out the current occupant."""
        preset = self._active_layout()
        if not (0 <= index < len(preset.boxes)):
            return
        before = self._layout_snapshot()
        previous = layer.box
        occupant = self._slot_layers(preset).get(index)
        layer.box = preset.boxes[index]
        layer.visible = True
        if occupant is not None and occupant is not layer:
            occupant.box = previous
        self.scene.layout = preset.key
        self._current_layout_key = preset.key
        self._refresh_layers()
        self._record_history(before)
        self._refresh_scene_cards()
        if self.engine is not None:
            # This is a layout edit, not a source edit.  Move the existing
            # compositor pads and reveal the layer through its pad; rebuilding
            # here would drop the screen-capture branch and re-open the portal
            # session (the window picker) under a running consumer.
            for target in (layer, occupant):
                if target is not None:
                    self.engine.set_layer_box(target.id, target.box)
            self.engine.set_layer_visible(layer.id, True)
            self.preview.update()
        elif self.isVisible():
            self.toggle_engine()
        self._status(f"{layer.name} → slot {index + 1}")

    def apply_layout(self, key: str = "") -> list[Layer]:
        """Apply a layout, select it, and remember it on the preset."""
        key = key or "full"
        arranged = super().apply_layout(key)
        self._current_layout_key = key
        button = self.layout_buttons.get(key)
        if button is not None:
            button.setChecked(True)
        self._refresh_stage_slots()
        self._refresh_scene_cards()
        return arranged

    # ----------------------------------------------------------- canvas slots
    def _refresh_stage_slots(self) -> None:
        """One dashed + over every empty slot of the active layout."""
        for button, _box in getattr(self, "_stage_slots", []):
            button.deleteLater()
        self._stage_slots = []
        self.stage_add_buttons = []
        self._stage_canvas = None
        if not hasattr(self, "preview"):
            return
        preset = self._active_layout()
        assigned = self._slot_layers(preset)
        empty = [index for index in range(len(preset.boxes)) if index not in assigned]
        sources = [layer for layer in self.scene.layers if layer.kind in GRID_KINDS]
        if empty and sources:
            # A slot shows "+" although sources exist: record why, so a slot
            # that wrongly looks empty can be traced from Diagnostics.
            insets = getattr(self, "_backdrop_insets", {})
            message = (
                f"layout {preset.key}: empty slot(s) {empty} with sources "
                + "; ".join(
                    f"{layer.kind} {layer.name!r} visible={layer.visible} "
                    f"box={tuple(round(v, 3) for v in layer.box)}"
                    + (f" inset-from={tuple(round(v, 3) for v in insets[layer.id][0])}"
                       if layer.id in insets else "")
                    for layer in sources))
            # Slots refresh often; only log when the picture changes.
            if message != getattr(self, "_last_slot_message", ""):
                self._last_slot_message = message
                self.log_event(message)
        else:
            self._last_slot_message = ""
        for index, box in enumerate(preset.boxes):
            if index in assigned:
                continue
            button = QPushButton("+", self.preview)
            button.setObjectName("slotAdd")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip("Add a source to this slot")
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda pos, btn=button: self._slot_context_menu(btn, pos))
            button.clicked.connect(
                lambda _=False, i=index, btn=button: self._slot_menu(i, btn))
            button.show()
            self._stage_slots.append((button, box))
            self.stage_add_buttons.append(button)
        self._position_stage_slots(force=True)

    def _position_stage_slots(self, force: bool = False) -> None:
        if not hasattr(self, "preview"):
            return
        canvas = self.preview.canvas_rect()
        if canvas.isEmpty() or canvas.width() < 2 or canvas.height() < 2:
            canvas = QRectF(0, 0, max(1, self.preview.width()),
                            max(1, self.preview.height()))
        key = (round(canvas.x(), 1), round(canvas.y(), 1),
               round(canvas.width(), 1), round(canvas.height(), 1))
        if not force and key == self._stage_canvas:
            return
        self._stage_canvas = key
        for button, box in self._stage_slots:
            x, y, w, h = box
            rect = QRectF(canvas.x() + x * canvas.width(),
                          canvas.y() + y * canvas.height(),
                          w * canvas.width(), h * canvas.height()).toRect()
            rect = rect.adjusted(4, 4, -4, -4)
            if rect.width() < 18 or rect.height() < 18:
                button.hide()
                continue
            button.setGeometry(rect)
            button.show()
            button.raise_()

    def eventFilter(self, watched, event):
        if watched is getattr(self, "preview", None) \
                and event.type() == QEvent.Type.Resize:
            self._position_stage_slots()
        return super().eventFilter(watched, event)

    def _tick(self) -> None:
        super()._tick()
        self._position_stage_slots()

    # ---------------------------------------------------------------- format
    def _format_changed(self, _index: int = 0) -> None:
        if self._syncing_format:
            return
        index = self.resolution_combo.currentIndex()
        sizes = getattr(self, "_format_sizes", [])
        if not 0 <= index < len(sizes):
            return
        size = sizes[index]
        rate = self.fps_combo.currentData()
        if not rate:
            return
        self.set_output_format(int(size[0]), int(size[1]), int(rate))

    def _sync_output_controls(self) -> None:
        if not hasattr(self, "resolution_combo"):
            return
        self._syncing_format = True
        try:
            size = (self.project.output.canvas_w, self.project.output.canvas_h)
            sizes = getattr(self, "_format_sizes", None)
            if sizes is None:
                return
            if size not in sizes:
                sizes.append(size)
                self.resolution_combo.addItem(
                    f"{size[0]}×{size[1]}", len(sizes) - 1)
            self.resolution_combo.setCurrentIndex(sizes.index(size))
            rate = self.project.output.fps
            fps_index = self.fps_combo.findData(rate)
            if fps_index < 0:
                self.fps_combo.addItem(f"{rate} fps", rate)
                fps_index = self.fps_combo.count() - 1
            self.fps_combo.setCurrentIndex(fps_index)
        finally:
            self._syncing_format = False

    # --------------------------------------------------------------- runtime
    def _autostart_preview(self) -> None:
        # An empty preset stays stopped until the first source is chosen.
        if self.engine is None and self.isVisible() and self.scene.layers:
            self.toggle_engine()


def run_gui(project: Project | None = None, safe_mode: bool = False) -> int:
    app = QApplication(sys.argv[:1])
    primary, instance_server = claim_gui_instance()
    if not primary:
        return 0
    safe_mode = startup.begin(safe_mode)
    window = ManyCamWindow(project)
    if safe_mode:
        window.setWindowTitle(window.windowTitle() + " (safe mode)")
    if instance_server is not None:
        instance_server.setParent(window)
        window._instance_server = instance_server
        serve_instance_requests(instance_server, window)
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    # The driver must be right before the node is held: the one-time setup
    # may reload v4l2loopback, which needs the device closed.
    if not safe_mode:
        window.setup_vcam_if_needed()
    # Hold the loopback node from launch: apps that remember "AnyCam" must be
    # able to select it before the first source starts the preview, and an
    # open that already failed needs its re-select hint.  Safe mode holds nothing.
    if not safe_mode and window.hold_vcam_device():
        # The card is a placeholder: once a source exists and the preview
        # starts, the held node follows the canvas without a second click
        # (unless the user turned the virtual camera off last time).
        if window.project.output.vcam_enabled:
            window.feed_held_vcam()
        window.note_vcam_consumers()
        window.ensure_vcam_visible()
    if safe_mode:
        window._status("Safe mode: CPU canvas, image preview, no virtual camera, phone link or AI "
                       "effects. Start the preview yourself; restart AnyCam to leave safe mode.")
    else:
        QTimer.singleShot(200, window._autostart_preview)
    QTimer.singleShot(400, window.show_first_run_check)
    # Tray icon and portal global shortcuts (both skipped where unavailable).
    if not safe_mode:
        QTimer.singleShot(500, window.start_desktop_integration)
    startup.mark_ready()
    code = app.exec()
    startup.finish()
    os._exit(code if code is not None else 0)


def run_screenshot(out: str, seconds: float, project: Project | None = None,
                   source: str = "") -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if project is None and source:
        from .demo import build_demo_project
        project = build_demo_project(source, config.CANVAS_W, config.CANVAS_H,
                                     config.CANVAS_FPS)
    app = QApplication(sys.argv[:1])
    window = ManyCamWindow(project)
    window.headless = True
    window.auto_screenshot_path = out
    window._render_seconds = max(0.1, seconds)
    window._screenshot_done = False
    window.resize(1440, 900)
    app.aboutToQuit.connect(window.shutdown)
    QTimer.singleShot(int((window._render_seconds + 5.0) * 1000),
                      window._screenshot_deadline)
    window.show()
    window.start_engine()
    app.exec()
    window.shutdown()
    status = 0 if (window.screenshot_ok and Path(out).exists()
                   and Path(out).stat().st_size > 0) else 1
    os._exit(status)
