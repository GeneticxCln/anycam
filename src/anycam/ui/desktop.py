"""Tray icon, global shortcuts and in-window shortcuts."""
from __future__ import annotations



from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QMenu,
)

from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class DesktopMixin:
    """Tray icon, global shortcuts and in-window shortcuts."""

    # ---------------------------------------------------- desktop integration
    def start_desktop_integration(self) -> None:
        """A tray icon and global shortcuts through the portal, when present."""
        from PySide6.QtWidgets import QSystemTrayIcon

        from .. import shortcuts

        if self._tray is None and QSystemTrayIcon.isSystemTrayAvailable():
            icon = self.windowIcon()
            if icon.isNull():
                icon = QIcon.fromTheme("camera-web", QIcon.fromTheme("camera-video"))
            tray = QSystemTrayIcon(icon, self)
            tray.setToolTip("AnyCam")
            menu = QMenu(self)
            menu.aboutToShow.connect(lambda: self._fill_tray_menu(menu))
            self._fill_tray_menu(menu)
            tray.setContextMenu(menu)
            tray.activated.connect(self._tray_activated)
            tray.show()
            self._tray = tray
        if self._global_shortcuts is None:
            self._global_shortcuts = shortcuts.GlobalShortcuts(
                self._on_global_shortcut, log=self.log_event,
                on_state=self._update_global_shortcuts_label)
            self._global_shortcuts.start()
        self._update_global_shortcuts_label()

    def _stop_desktop_integration(self) -> None:
        tray, self._tray = self._tray, None
        if tray is not None:
            tray.hide()
            tray.deleteLater()
        keys, self._global_shortcuts = self._global_shortcuts, None
        if keys is not None:
            keys.close()

    def _tray_activated(self, reason) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window_visible()

    def _toggle_window_visible(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _fill_tray_menu(self, menu: QMenu) -> None:
        """Rebuilt each time it opens, so the checks and scenes are current."""
        menu.clear()
        shown = self.isVisible() and not self.isMinimized()
        menu.addAction("Hide AnyCam" if shown else "Show AnyCam",
                       self._toggle_window_visible)
        menu.addSeparator()
        for label, checked, slot in (
                ("Virtual camera", self.vcam_on, self.toggle_vcam),
                ("Record", self.recording, self.toggle_record),
                ("Mute microphone", bool(self.project.audio.mic_muted), self.toggle_mic_mute)):
            action = menu.addAction(label, slot)
            action.setCheckable(True)
            action.setChecked(bool(checked))
        scenes = menu.addMenu("Scenes")
        for index, scene in enumerate(self.project.scenes):
            action = scenes.addAction(scene.name, lambda i=index: self.switch_scene(i))
            action.setCheckable(True)
            action.setChecked(scene.id == self.project.active_scene)
        menu.addSeparator()
        menu.addAction("Quit AnyCam", self.close)

    def _on_global_shortcut(self, shortcut: str) -> None:
        """A portal global shortcut, pressed while any app has focus."""
        if getattr(self, "_ui_blocked", 0):
            self.log_event(f"global shortcut ignored while busy: {shortcut}")
            return
        self.log_event(f"global shortcut: {shortcut}")
        if shortcut.startswith("scene-") and shortcut[6:].isdigit():
            index = int(shortcut[6:]) - 1
            if 0 <= index < len(self.project.scenes):
                self.switch_scene(index)
        elif shortcut in ("scene-next", "scene-previous"):
            ids = [scene.id for scene in self.project.scenes]
            if ids:
                current = ids.index(self.project.active_scene) if self.project.active_scene in ids else 0
                step = 1 if shortcut == "scene-next" else -1
                self.switch_scene((current + step) % len(ids))
        elif shortcut == "mute":
            self.toggle_mic_mute()
        elif shortcut == "record":
            self.toggle_record()
        elif shortcut == "vcam":
            self.toggle_vcam()
        self._update_global_shortcuts_label()

    def _update_global_shortcuts_label(self) -> None:
        label = getattr(self, "global_shortcuts_label", None)
        if label is None:
            return
        keys = self._global_shortcuts
        if keys is None:
            text = "Global shortcuts: not started."
        elif keys.state == "bound":
            text = ("Global shortcuts (work while other apps have focus): Ctrl+Alt+1…9 "
                    "scenes, Ctrl+Alt+N / P next / previous scene, Ctrl+Alt+M mute, "
                    "Ctrl+Alt+R record, Ctrl+Alt+V virtual camera. Change them in "
                    "your desktop's shortcut settings.")
        elif keys.state == "binding":
            text = "Global shortcuts: waiting for the desktop to confirm them…"
        else:
            text = f"Global shortcuts unavailable ({keys.state.removeprefix('failed: ')}); "\
                   "the shortcuts above work while AnyCam has focus."
        label.setText(text)

    def _hotkey_actions(self) -> dict:
        """Action id -> what it does (the keys come from ``anycam.hotkeys``)."""
        actions = {
            "preview": self.toggle_engine,
            "record": self.toggle_record,
            "vcam": self.toggle_vcam,
            "snapshot": self.snapshot,
            "mute": self.toggle_mic_mute,
            "undo": self.undo,
            "redo": self.redo,
            "redo-alt": self.redo,
            "duplicate": self.duplicate_layer,
            "lock": self._toggle_selected_lock,
            "next-scene": lambda: self._step_scene(1),
            "previous-scene": lambda: self._step_scene(-1),
            "align-left": lambda: self._align_layers("left"),
            "align-right": lambda: self._align_layers("right"),
            "align-top": lambda: self._align_layers("top"),
            "align-bottom": lambda: self._align_layers("bottom"),
            "align-hcenter": lambda: self._align_layers("hcenter"),
            "align-vcenter": lambda: self._align_layers("vcenter"),
        }
        for index in range(1, 10):
            actions[f"scene-{index}"] = lambda i=index - 1: self.switch_scene(i)
        return actions

    def _toggle_selected_lock(self) -> None:
        layer = self.preview.selected_layer()
        if layer is not None:
            self.set_layer_locked(layer.id, not layer.locked)

    def _step_scene(self, step: int) -> None:
        ids = [scene.id for scene in self.project.scenes]
        if ids:
            current = ids.index(self.project.active_scene) if self.project.active_scene in ids else 0
            self.switch_scene((current + step) % len(ids))

    def _install_shortcuts(self) -> None:
        """Keyboard shortcuts for the actions an operator needs mid-stream.

        The keys are the defaults of ``anycam.hotkeys`` unless the user chose
        others; calling this again replaces the old bindings.
        """
        from .. import hotkeys

        for old in getattr(self, "shortcuts", []):
            old.setEnabled(False)
            old.deleteLater()
        self.shortcuts = []
        keys = hotkeys.current()
        for action, callback in self._hotkey_actions().items():
            key = keys.get(action, "")
            if not key:
                continue
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            shortcut.setObjectName(f"hotkey-{action}")
            self.shortcuts.append(shortcut)

    def set_hotkeys(self, mapping: dict[str, str]) -> dict[str, list[str]]:
        """Save and apply a new key map; returns the keys bound twice (still applied)."""
        from .. import hotkeys

        hotkeys.save({**hotkeys.DEFAULTS, **mapping})
        self._install_shortcuts()
        return hotkeys.conflicts(hotkeys.current())
