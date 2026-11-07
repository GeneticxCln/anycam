"""Tray icon and portal global shortcuts."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import gi

gi.require_version("Gio", "2.0")
from gi.repository import GLib  # noqa: E402

from anycam import shortcuts  # noqa: E402


class FakePortal:
    """A session bus that answers like xdg-desktop-portal's GlobalShortcuts."""

    def __init__(self, bind_code=0, version=1):
        self.subscriptions = {}
        self.calls = []
        self.bind_code = bind_code
        self.version = version
        self._next = 1

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        sid = self._next
        self._next += 1
        self.subscriptions[sid] = (iface, member, path, callback)
        return sid

    def signal_unsubscribe(self, sid):
        self.subscriptions.pop(sid, None)

    def emit(self, iface, member, path, params):
        for sub_iface, sub_member, sub_path, callback in list(self.subscriptions.values()):
            if sub_iface == iface and sub_member == member and sub_path in (path, None):
                callback(None, "", path, iface, member, params)

    def call_sync(self, name, path, iface, method, params, reply, flags, timeout, cancel):
        args = params.unpack() if params is not None else ()
        self.calls.append((method, args))
        if method == "Get":
            return GLib.Variant("(v)", (GLib.Variant("u", self.version),))
        if method == "Close":
            return None
        options = args[-1]
        token = options["handle_token"]
        request = f"/org/freedesktop/portal/desktop/request/1_42/{token}"
        if method == "CreateSession":
            self.emit(shortcuts.REQUEST_IFACE, "Response", request, GLib.Variant(
                "(ua{sv})", (0, {"session_handle": GLib.Variant(
                    "s", "/org/freedesktop/portal/desktop/session/1_42/s")})))
        elif method == "BindShortcuts":
            bound = [(identifier, {}) for identifier, _d, _t in shortcuts.SHORTCUTS]
            self.emit(shortcuts.REQUEST_IFACE, "Response", request, GLib.Variant(
                "(ua{sv})", (self.bind_code, {"shortcuts": GLib.Variant("a(sa{sv})", bound)})))
        return GLib.Variant("(o)", (request,))

    def press(self, shortcut, session="/org/freedesktop/portal/desktop/session/1_42/s"):
        self.emit(shortcuts.IFACE, "Activated", "/org/freedesktop/portal/desktop",
                  GLib.Variant("(osta{sv})", (session, shortcut, 0, {})))


def test_shortcuts_are_bound_through_the_portal_and_fire():
    portal = FakePortal()
    pressed, states = [], []
    keys = shortcuts.GlobalShortcuts(pressed.append, log=lambda m: None, connection=portal,
                                     on_state=lambda: states.append(True))
    assert keys.start()
    assert keys.state == "bound" and states
    assert "scene-1" in keys.bound and "vcam" in keys.bound
    bind = next(args for method, args in portal.calls if method == "BindShortcuts")
    triggers = {identifier: options["preferred_trigger"] for identifier, options in bind[1]}
    assert triggers["mute"] == "CTRL+ALT+M"
    assert not any("Left" in t or "Right" in t for t in triggers.values()), \
        "Ctrl+Alt+arrows stay free for in-window alignment"
    portal.press("record")
    portal.press("scene-2", session="/some/other/session")     # not ours
    assert pressed == ["record"]
    keys.close()
    assert ("Close", ()) in portal.calls and keys.state == "idle"


def test_a_declined_binding_or_a_missing_portal_is_reported():
    portal = FakePortal(bind_code=1)
    keys = shortcuts.GlobalShortcuts(lambda s: None, log=lambda m: None, connection=portal)
    keys.start()
    assert keys.state == "failed: declined"
    keys = shortcuts.GlobalShortcuts(lambda s: None, log=lambda m: None,
                                     connection=FakePortal(version=0))
    assert not keys.start()
    assert "no GlobalShortcuts" in keys.state


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    instance = QApplication.instance() or QApplication([])
    yield instance


def test_global_shortcuts_drive_the_same_actions_as_the_keys(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from anycam.demo import build_demo_project
    from anycam.gui import AnyCamWindow
    from anycam.model import Scene

    project = build_demo_project("testsrc", 320, 180, 30)
    project.scenes.append(Scene("second", "Second"))
    window = AnyCamWindow(project)
    calls = []
    window.toggle_record = lambda: calls.append("record")
    window.toggle_vcam = lambda: calls.append("vcam")
    window.toggle_mic_mute = lambda: calls.append("mute")
    window.switch_scene = lambda index: calls.append(("scene", index))
    try:
        for shortcut in ("record", "vcam", "mute", "scene-2", "scene-9", "scene-next",
                         "scene-previous"):
            window._on_global_shortcut(shortcut)
        assert calls == ["record", "vcam", "mute", ("scene", 1), ("scene", 1), ("scene", 1)]
    finally:
        window.close()


def test_the_tray_menu_reflects_the_current_state(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from PySide6.QtWidgets import QMenu

    from anycam.demo import build_demo_project
    from anycam.gui import AnyCamWindow

    window = AnyCamWindow(build_demo_project("testsrc", 320, 180, 30))
    try:
        window.recording = True
        menu = QMenu()
        window._fill_tray_menu(menu)
        actions = {action.text(): action for action in menu.actions() if action.text()}
        assert actions["Record"].isChecked() and not actions["Virtual camera"].isChecked()
        assert "Scenes" in actions and "Quit AnyCam" in actions
        window.recording = False
    finally:
        window.close()
