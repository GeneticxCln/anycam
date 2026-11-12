"""Global shortcuts through the xdg-desktop-portal GlobalShortcuts interface.

On Wayland an app cannot grab keys while another app has focus; the portal
lets the compositor own the keys and tell AnyCam when one is pressed.  KDE
and Hyprland implement it; elsewhere ``available()`` is False and the
in-window shortcuts are all there is.

Everything is asynchronous on the GLib main context, which Qt's event loop
runs on Linux (``QPAEventDispatcherGlib``), so no thread is needed and the
callback arrives on the UI thread.
"""

from __future__ import annotations

import secrets
import sys
from collections.abc import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

# (id, description, preferred trigger).  The compositor may ask the user to
# confirm or change them; Ctrl+Alt+arrows stay free for in-window alignment.
SHORTCUTS: tuple[tuple[str, str, str], ...] = (
    *((f"scene-{n}", f"Switch to scene {n}", f"CTRL+ALT+{n}") for n in range(1, 10)),
    ("scene-next", "Next scene", "CTRL+ALT+N"),
    ("scene-previous", "Previous scene", "CTRL+ALT+P"),
    ("mute", "Mute or unmute the microphone", "CTRL+ALT+M"),
    ("record", "Start or stop recording", "CTRL+ALT+R"),
    ("vcam", "Virtual camera on or off", "CTRL+ALT+V"),
)


def available(connection: Gio.DBusConnection | None = None) -> bool:
    """Whether the desktop portal offers GlobalShortcuts."""
    try:
        connection = connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = connection.call_sync(
            BUS_NAME, OBJECT_PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (IFACE, "version")), GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE, 2000, None)
        return int(reply.unpack()[0]) >= 1
    except Exception:  # noqa: BLE001 - no bus, no portal, no interface
        return False


class GlobalShortcuts:
    """One portal session with AnyCam's shortcuts bound.

    ``on_activated(shortcut_id)`` runs when the user presses one.  ``state``
    is "idle", "binding", "bound" or "failed: <reason>" for the Settings line.
    """

    def __init__(self, on_activated: Callable[[str], None],
                 log: Callable[[str], None] | None = None,
                 connection: Gio.DBusConnection | None = None,
                 on_state: Callable[[], None] | None = None) -> None:
        self.on_activated = on_activated
        self.on_state = on_state or (lambda: None)
        self.log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self.connection = connection
        self.session_handle = ""
        self.state = "idle"
        self.bound: list[str] = []
        self._subscriptions: list[int] = []

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        if self.state in ("binding", "bound"):
            return True
        try:
            self.connection = self.connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            self.state = f"failed: {exc.message}"
            return False
        if not available(self.connection):
            self.state = "failed: the desktop portal has no GlobalShortcuts"
            return False
        self._subscriptions.append(self.connection.signal_subscribe(
            BUS_NAME, IFACE, "Activated", OBJECT_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_activated))
        self.state = "binding"
        token = self._token()
        self._request(
            "CreateSession",
            GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", token),
                "session_handle_token": GLib.Variant("s", self._token()),
            },)),
            token, self._on_session)
        return True

    def close(self) -> None:
        for subscription in self._subscriptions:
            try:
                self.connection.signal_unsubscribe(subscription)
            except Exception:  # noqa: BLE001
                pass
        self._subscriptions.clear()
        if self.session_handle and self.connection is not None:
            try:
                self.connection.call_sync(
                    BUS_NAME, self.session_handle, SESSION_IFACE, "Close",
                    GLib.Variant("()", ()), None, Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass
        self.session_handle = ""
        self.state = "idle"

    # -------------------------------------------------------------- portal
    @staticmethod
    def _token() -> str:
        return "anycam_" + secrets.token_hex(6)

    def _request_path(self, token: str) -> str:
        sender = (self.connection.get_unique_name() or "").lstrip(":").replace(".", "_")
        return f"{OBJECT_PATH}/request/{sender}/{token}"

    def _request(self, method: str, parameters: GLib.Variant, token: str,
                 on_response: Callable[[int, dict], None]) -> None:
        """Call a portal request method; ``on_response`` gets its Response."""
        path = self._request_path(token)
        holder: dict = {}

        def response(_conn, _sender, _path, _iface, _signal, params):
            self.connection.signal_unsubscribe(holder["id"])
            code, results = params.unpack()
            on_response(int(code), results or {})

        # Subscribe before calling, so an immediate answer cannot be missed.
        holder["id"] = self.connection.signal_subscribe(
            BUS_NAME, REQUEST_IFACE, "Response", path, None,
            Gio.DBusSignalFlags.NONE, response)
        try:
            self.connection.call_sync(BUS_NAME, OBJECT_PATH, IFACE, method, parameters,
                                      GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE,
                                      5000, None)
        except GLib.Error as exc:
            self.connection.signal_unsubscribe(holder["id"])
            self.state = f"failed: {exc.message}"
            self.log(f"global shortcuts: {method} failed: {exc.message}")

    def _on_session(self, code: int, results: dict) -> None:
        handle = results.get("session_handle", "")
        if isinstance(handle, GLib.Variant):
            handle = handle.unpack()
        if code != 0 or not handle:
            self.state = "failed: the portal refused a shortcuts session"
            self.log(f"global shortcuts: CreateSession answered {code}")
            self.on_state()
            return
        self.session_handle = str(handle)
        token = self._token()
        from . import hotkeys

        triggers = hotkeys.global_triggers({identifier: trigger for identifier, _d, trigger in SHORTCUTS})
        shortcuts = [(identifier, {"description": GLib.Variant("s", description),
                                   "preferred_trigger": GLib.Variant("s", triggers[identifier])})
                     for identifier, description, _trigger in SHORTCUTS]
        self._request(
            "BindShortcuts",
            GLib.Variant("(oa(sa{sv})sa{sv})", (
                self.session_handle, shortcuts, "",
                {"handle_token": GLib.Variant("s", token)})),
            token, self._on_bound)

    def _on_bound(self, code: int, results: dict) -> None:
        if code != 0:
            # 1 = the user declined in the compositor's dialog.
            self.state = ("failed: declined" if code == 1
                          else "failed: the portal could not bind the shortcuts")
            self.log(f"global shortcuts: BindShortcuts answered {code}")
            self.on_state()
            return
        bound = results.get("shortcuts", [])
        if isinstance(bound, GLib.Variant):
            bound = bound.unpack()
        self.bound = [str(item[0]) for item in bound] or [item[0] for item in SHORTCUTS]
        self.state = "bound"
        self.log(f"global shortcuts: {len(self.bound)} bound")
        self.on_state()

    def _on_activated(self, _conn, _sender, _path, _iface, _signal, params) -> None:
        session, shortcut, *_rest = params.unpack()
        if session != self.session_handle:
            return
        try:
            self.on_activated(str(shortcut))
        except Exception as exc:  # noqa: BLE001 - a shortcut must not break the app
            self.log(f"global shortcuts: {shortcut} failed: {exc}")
