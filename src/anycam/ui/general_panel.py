"""Settings > General: language, project files and keyboard shortcuts."""
from __future__ import annotations

import shutil
import subprocess

import numpy as np
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QKeySequenceEdit, QLabel, QPushButton,
                               QSlider, QTableWidget, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from .. import a11y, hotkeys, i18n
from ..motion import MotionMonitor


class GeneralPanelMixin:
    """Language, project files and the shortcut editor, and the final UI polish pass."""

    _motion_monitor = None

    def _general_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        language_box = QGroupBox("Language")
        language_layout = QHBoxLayout(language_box)
        self.language_combo = QComboBox()
        for code, name in i18n.available_languages().items():
            self.language_combo.addItem(name, code)
        self.language_combo.setCurrentIndex(
            max(0, self.language_combo.findData(i18n.saved_language())))
        self.language_combo.currentIndexChanged.connect(
            lambda _i: self.set_language(str(self.language_combo.currentData())))
        language_layout.addWidget(self.language_combo, 1)
        layout.addWidget(language_box)

        check_button = QPushButton("Check this computer\u2026")
        check_button.setToolTip("Is the camera, virtual camera, sound and screen capture ready?")
        check_button.clicked.connect(lambda: self.show_first_run_check(force=True))
        language_layout.addWidget(check_button)

        files = QGroupBox("Project")
        files_layout = QHBoxLayout(files)
        for label, handler in (("Open…", lambda: self.open_project()),
                               ("Save as…", lambda: self.save_project_as()),
                               ("Import scene…", lambda: self.import_scene()),
                               ("Export scene…", lambda: self.export_scene())):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, h=handler: h())
            files_layout.addWidget(button)
        layout.addWidget(files)

        layout.addWidget(self._motion_group())

        remote_box = QGroupBox("Phone remote")
        remote_layout = QVBoxLayout(remote_box)
        self.phone_control_check = QCheckBox(
            "Let paired phones switch scenes, mute and record (page: /remote)")
        self.phone_control_check.setChecked(self.project.output.phone_control)
        self.phone_control_check.toggled.connect(self.set_phone_control)
        remote_note = QLabel("A phone must be paired first (Add \u2192 Phone camera). Control stays on your "
                             "local network and only works for paired phones.")
        remote_note.setObjectName("muted")
        remote_note.setWordWrap(True)
        remote_layout.addWidget(self.phone_control_check)
        remote_layout.addWidget(remote_note)
        layout.addWidget(remote_box)

        panel_box = QGroupBox("Floating panel")
        panel_layout = QVBoxLayout(panel_box)
        self.panel_check = QCheckBox("Show a small always-on-top panel (round preview, record, mute, scenes)")
        self.panel_check.toggled.connect(self.set_overlay_panel)
        panel_note = QLabel("Wayland desktops may ignore always-on-top and where the panel is placed; "
                            "drag it where you want it.")
        panel_note.setObjectName("muted")
        panel_note.setWordWrap(True)
        panel_layout.addWidget(self.panel_check)
        panel_layout.addWidget(panel_note)
        layout.addWidget(panel_box)

        keys_box = QGroupBox("Keyboard shortcuts")
        keys_layout = QVBoxLayout(keys_box)
        self.hotkey_table = QTableWidget(len(hotkeys.ACTIONS), 2)
        self.hotkey_table.setHorizontalHeaderLabels(["Action", "Key"])
        self.hotkey_table.verticalHeader().setVisible(False)
        self.hotkey_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.hotkey_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._hotkey_edits: dict[str, QKeySequenceEdit] = {}
        current = hotkeys.current()
        for row, (action, label, _default) in enumerate(hotkeys.ACTIONS):
            item = QLabel(f"  {label}")
            self.hotkey_table.setCellWidget(row, 0, item)
            edit = QKeySequenceEdit(QKeySequence(current.get(action, "")))
            edit.setMaximumSequenceLength(1)
            edit.setAccessibleName(f"Shortcut for {label}")
            edit.editingFinished.connect(self._hotkeys_edited)
            self.hotkey_table.setCellWidget(row, 1, edit)
            self._hotkey_edits[action] = edit
        keys_layout.addWidget(self.hotkey_table, 1)
        self.hotkey_note = QLabel("")
        self.hotkey_note.setObjectName("muted")
        self.hotkey_note.setWordWrap(True)
        keys_layout.addWidget(self.hotkey_note)
        reset = QPushButton("Reset shortcuts to defaults")
        reset.clicked.connect(self.reset_hotkeys)
        keys_layout.addWidget(reset)
        layout.addWidget(keys_box, 1)
        return page

    # ------------------------------------------------------------- shortcuts
    def _hotkeys_edited(self) -> None:
        mapping = {action: edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                   for action, edit in self._hotkey_edits.items()}
        clashes = self.set_hotkeys(mapping)
        if clashes:
            names = "; ".join(
                f"{key}: " + ", ".join(hotkeys.LABELS[a] for a in actions)
                for key, actions in clashes.items())
            self.hotkey_note.setText(f"Used twice (neither works until one changes): {names}")
        else:
            self.hotkey_note.setText("Shortcuts saved.")

    def reset_hotkeys(self) -> None:
        for action, edit in self._hotkey_edits.items():
            edit.setKeySequence(QKeySequence(hotkeys.DEFAULTS[action]))
        self.set_hotkeys(dict(hotkeys.DEFAULTS))
        self.hotkey_note.setText("Shortcuts are back to their defaults.")

    # -------------------------------------------------------------- language
    def set_language(self, code: str) -> int:
        """Choose the interface language now and for next time; strings changed."""
        if code not in i18n.available_languages():
            code = i18n.DEFAULT
        i18n.save_language(code)
        return self.apply_interface_polish()

    def apply_interface_polish(self, root=None) -> int:
        """Translate (per the saved language) and name every control for assistive tech."""
        language = i18n.saved_language()
        catalog = i18n.load(language)
        targets = [root] if root is not None else [self] + [
            child for child in (getattr(self, "_settings", None),) if child is not None]
        changed = 0
        for target in targets:
            changed += i18n.apply(target, catalog)
            a11y.apply(target)
        return changed


    # ---------------------------------------------------------- first run
    def show_first_run_check(self, force: bool = False) -> bool:
        """The welcome checklist, once (or on demand); True when it was shown."""
        from .. import firstrun
        from .dialogs import FirstRunDialog

        if self.headless or (firstrun.already_shown() and not force):
            return False
        dialog = FirstRunDialog(firstrun.checks(), self)
        firstrun.mark_shown()
        dialog.exec()
        return True

    # ------------------------------------------------------ floating panel
    _overlay_panel = None

    def set_overlay_panel(self, shown: bool) -> None:
        from .overlay_panel import OverlayPanel

        if shown and self._overlay_panel is None:
            self._overlay_panel = OverlayPanel(self)
        if self._overlay_panel is not None:
            if shown:
                self._overlay_panel.refresh()
                self._overlay_panel.show()
            else:
                self._overlay_panel.hide()
        if hasattr(self, "panel_check") and self.panel_check.isChecked() != bool(shown):
            self.panel_check.blockSignals(True)
            self.panel_check.setChecked(bool(shown))
            self.panel_check.blockSignals(False)

    # ------------------------------------------------------ motion detection
    def _motion_group(self) -> QGroupBox:
        box = QGroupBox("Motion detection")
        grid = QGridLayout(box)
        motion = self.project.motion
        self.motion_check = QCheckBox("Watch the canvas for movement")
        self.motion_check.setChecked(motion.enabled)
        self.motion_sensitivity = QSlider(Qt.Orientation.Horizontal)
        self.motion_sensitivity.setRange(0, 100)
        self.motion_sensitivity.setValue(int(round(motion.sensitivity * 100)))
        self.motion_hold = QDoubleSpinBox()
        self.motion_hold.setRange(0.0, 60.0)
        self.motion_hold.setSuffix(" s")
        self.motion_hold.setValue(motion.hold_s)
        self.motion_cooldown = QDoubleSpinBox()
        self.motion_cooldown.setRange(0.0, 3600.0)
        self.motion_cooldown.setSuffix(" s")
        self.motion_cooldown.setValue(motion.cooldown_s)
        self.motion_scene = QComboBox()
        self.motion_notify = QCheckBox("Show a desktop notification")
        self.motion_notify.setChecked(motion.notify)
        self.motion_level_label = QLabel("")
        self.motion_level_label.setObjectName("muted")
        self.refresh_motion_scenes()
        for widget, signal in ((self.motion_check, "toggled"), (self.motion_sensitivity, "valueChanged"),
                               (self.motion_hold, "valueChanged"), (self.motion_cooldown, "valueChanged"),
                               (self.motion_scene, "currentIndexChanged"), (self.motion_notify, "toggled")):
            getattr(widget, signal).connect(self._motion_settings_changed)
        grid.addWidget(self.motion_check, 0, 0, 1, 2)
        grid.addWidget(QLabel("Sensitivity"), 1, 0)
        grid.addWidget(self.motion_sensitivity, 1, 1)
        grid.addWidget(QLabel("Must last"), 2, 0)
        grid.addWidget(self.motion_hold, 2, 1)
        grid.addWidget(QLabel("Then wait"), 3, 0)
        grid.addWidget(self.motion_cooldown, 3, 1)
        grid.addWidget(QLabel("Switch to scene"), 4, 0)
        grid.addWidget(self.motion_scene, 4, 1)
        grid.addWidget(self.motion_notify, 5, 0, 1, 2)
        grid.addWidget(self.motion_level_label, 6, 0, 1, 2)
        self._motion_monitor = None
        self._motion_settings_changed()
        return box

    def load_motion_widgets(self) -> None:
        """Show the (just loaded) project's motion settings."""
        motion = self.project.motion
        widgets = (self.motion_check, self.motion_sensitivity, self.motion_hold,
                   self.motion_cooldown, self.motion_scene, self.motion_notify)
        for widget in widgets:
            widget.blockSignals(True)
        self.motion_check.setChecked(motion.enabled)
        self.motion_sensitivity.setValue(int(round(motion.sensitivity * 100)))
        self.motion_hold.setValue(motion.hold_s)
        self.motion_cooldown.setValue(motion.cooldown_s)
        self.motion_notify.setChecked(motion.notify)
        self.refresh_motion_scenes()
        for widget in widgets:
            widget.blockSignals(False)
        self._motion_settings_changed()

    def refresh_motion_scenes(self) -> None:
        combo = getattr(self, "motion_scene", None)
        if combo is None:
            return
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Stay on this scene", "")
        for scene in self.project.scenes:
            combo.addItem(scene.name, scene.id)
        combo.setCurrentIndex(max(0, combo.findData(self.project.motion.scene_id)))
        combo.blockSignals(False)

    def _motion_settings_changed(self, *_args) -> None:
        motion = self.project.motion
        motion.enabled = self.motion_check.isChecked()
        motion.sensitivity = self.motion_sensitivity.value() / 100.0
        motion.hold_s = float(self.motion_hold.value())
        motion.cooldown_s = float(self.motion_cooldown.value())
        motion.scene_id = str(self.motion_scene.currentData() or "")
        motion.notify = self.motion_notify.isChecked()
        if motion.enabled:
            monitor = self._motion_monitor
            if monitor is None:
                monitor = self._motion_monitor = MotionMonitor()
            monitor.set_sensitivity(motion.sensitivity)
            monitor.hold_s, monitor.cooldown_s = motion.hold_s, motion.cooldown_s
        else:
            self._motion_monitor = None
            self.motion_level_label.setText("")
        self._mark_dirty()

    def feed_motion(self, rgba: np.ndarray, now: float) -> bool:
        """One preview frame for the motion watch; True when it acted."""
        monitor = self._motion_monitor
        if monitor is None:
            return False
        fired = monitor.update(rgba, now)
        self.motion_level_label.setText(f"Movement now: {monitor.level * 100:.1f} % of the picture")
        if fired:
            self._motion_triggered()
        return fired

    def _motion_triggered(self) -> None:
        motion = self.project.motion
        target = next((i for i, scene in enumerate(self.project.scenes)
                       if scene.id == motion.scene_id), -1)
        message = "Motion detected"
        if target >= 0 and self.project.scenes[target].id != self.project.active_scene:
            self.switch_scene(target)
            message += f" \u2013 switched to {self.project.scenes[target].name}"
        self._status(message)
        self.log_event(f"motion: {message}")
        if motion.notify:
            self.desktop_notify("AnyCam", message)

    @staticmethod
    def desktop_notify(title: str, body: str) -> bool:
        """A desktop notification through notify-send, when there is one."""
        tool = shutil.which("notify-send")
        if tool is None:
            return False
        try:
            subprocess.Popen([tool, "--app-name=AnyCam", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        return True
