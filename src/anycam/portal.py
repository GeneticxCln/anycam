"""Minimal xdg-desktop-portal ScreenCast client.

The portal is the only supported way for a sandboxed/Wayland application to
ask for a screen or window stream.  It returns a PipeWire node id and an fd;
``pipewiresrc`` needs both, so the GUI keeps the session alive for as long as
the screen layer exists.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
SESSION_IFACE = "org.freedesktop.portal.Session"
REQUEST_IFACE = "org.freedesktop.portal.Request"


class PortalError(RuntimeError):
    """Raised when the portal is unavailable, denies a request or times out."""


@dataclass
class ScreenStream:
    node_id: int
    width: int = 0
    height: int = 0
    x: int = 0
    y: int = 0
    source_type: int = 0


@dataclass
class ScreenSession:
    """A live ScreenCast session.

    ``fd`` is the PipeWire remote fd returned by OpenPipeWireRemote.  It is a
    process-local resource and deliberately not serialised with the project.
    """

    session_handle: str
    fd: int
    streams: list[ScreenStream] = field(default_factory=list)
    restore_token: str = ""
    connection: Gio.DBusConnection | None = None

    @property
    def node_id(self) -> int:
        return self.streams[0].node_id if self.streams else -1

    def close(self) -> None:
        _close_session(self.connection, self.session_handle)
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


def _close_session(connection: Gio.DBusConnection | None,
                   session_handle: str) -> None:
    if connection is None or not session_handle:
        return
    try:
        connection.call_sync(
            BUS_NAME, session_handle, SESSION_IFACE, "Close",
            GLib.Variant("()", ()), None, Gio.DBusCallFlags.NONE, 2000, None,
        )
    except GLib.Error:
        pass


def _connection() -> Gio.DBusConnection:
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:
        raise PortalError(f"no D-Bus session bus: {exc.message}") from exc


def _request_path(connection: Gio.DBusConnection, token: str) -> str:
    sender = (connection.get_unique_name() or "").lstrip(":").replace(".", "_")
    return f"{OBJECT_PATH}/request/{sender}/{token}"


def _call_and_wait(
    connection: Gio.DBusConnection,
    method: str,
    parameters: GLib.Variant,
    request_path: str,
    timeout: float,
) -> dict:
    """Call a ScreenCast request method and wait for its Response signal.

    The subscription is installed before the method call, so a portal that
    answers immediately (for example with an error) cannot race us.  Request
    methods return a Request object path; the caller derives that path from the
    ``handle_token`` it sends, which is how the matching response is found.
    """
    loop = GLib.MainLoop()
    state: dict = {}
    timed_out = {"value": False}

    def on_response(_conn, _sender, _path, _iface, _signal, params) -> None:
        response, results = params.unpack()
        state["response"] = int(response)
        state["results"] = results or {}
        loop.quit()

    def on_timeout() -> bool:
        timed_out["value"] = True
        loop.quit()
        return False

    subscription = connection.signal_subscribe(
        BUS_NAME, REQUEST_IFACE, "Response", request_path, None,
        Gio.DBusSignalFlags.NONE, on_response,
    )
    timer = GLib.timeout_add(int(max(1.0, timeout) * 1000), on_timeout)
    try:
        connection.call_sync(
            BUS_NAME, OBJECT_PATH, SCREENCAST_IFACE, method, parameters,
            GLib.VariantType.new("(o)"), Gio.DBusCallFlags.NONE, -1, None,
        )
        loop.run()
    except GLib.Error as exc:
        raise PortalError(f"ScreenCast.{method} failed: {exc.message}") from exc
    finally:
        if timer and not timed_out["value"]:
            GLib.source_remove(timer)
        connection.signal_unsubscribe(subscription)
    if timed_out["value"]:
        raise PortalError("screen-cast request timed out")
    if not state:
        raise PortalError("screen-cast request ended without a response")
    if state.get("response", 2) != 0:
        # 1 = cancelled by the user, 2 = other error.  The portal does not
        # expose a more specific reason through this signal.
        raise PortalError("screen-cast request was cancelled or denied")
    return state.get("results", {})


def _unpack(value):
    return value.unpack() if isinstance(value, GLib.Variant) else value


def _decode_streams(results: dict) -> list[ScreenStream]:
    streams: list[ScreenStream] = []
    raw = _unpack(results.get("streams", [])) or []
    for item in raw:
        node_id, props = item
        props = _unpack(props) or {}
        size = _unpack(props.get("size", (0, 0))) or (0, 0)
        position = _unpack(props.get("position", (0, 0))) or (0, 0)
        streams.append(ScreenStream(
            node_id=int(node_id),
            width=int(size[0]),
            height=int(size[1]),
            x=int(position[0]),
            y=int(position[1]),
            source_type=int(_unpack(props.get("source_type", 0)) or 0),
        ))
    return streams


def _session_handle(results: dict) -> str:
    value = _unpack(results.get("session_handle", ""))
    return str(value) if value else ""


def request_screen_cast(timeout: float = 120.0, restore_token: str = "",
                        types: int = 3) -> ScreenSession:
    """Ask the portal for one monitor/window stream.

    ``types`` is the portal's source bitmask: 1 = monitor, 2 = window, 3 = both.
    This is synchronous: the portal's own chooser dialog is the UI.  The caller
    should keep the returned session alive for as long as the stream is used.
    """
    connection = _connection()
    token = f"anycam_{secrets.token_hex(6)}"

    # 1. CreateSession -> session_handle.
    create_token = f"{token}_create"
    create_results = _call_and_wait(
        connection, "CreateSession",
        GLib.Variant("(a{sv})", ({
            "handle_token": GLib.Variant("s", create_token),
            "session_handle_token": GLib.Variant("s", token),
        },)),
        _request_path(connection, create_token), timeout,
    )
    session_handle = _session_handle(create_results)
    if not session_handle:
        raise PortalError("the portal returned no ScreenCast session")

    session = ScreenSession(session_handle=session_handle, fd=-1,
                            connection=connection)
    try:
        # 2. SelectSources -> one monitor/window, with the cursor embedded
        # (cursor_mode 2; 1 means hidden).
        select_token = f"{token}_select"
        select_request = _request_path(connection, select_token)
        select_options = {
            "handle_token": GLib.Variant("s", select_token),
            "types": GLib.Variant("u", int(types)),  # 1 monitor | 2 window
            "multiple": GLib.Variant("b", False),
            # 1 = hidden, 2 = embedded, 4 = metadata.  The old value 1 hid
            # the cursor, so screen captures never showed a pointer.
            "cursor_mode": GLib.Variant("u", 2),     # embedded
        }
        if restore_token:
            select_options["restore_token"] = GLib.Variant("s", restore_token)
        _call_and_wait(
            connection, "SelectSources",
            GLib.Variant("(oa{sv})", (session_handle, select_options)),
            select_request, timeout,
        )

        # 3. Start -> streams and a restore token.
        start_token = f"{token}_start"
        start_results = _call_and_wait(
            connection, "Start",
            GLib.Variant("(osa{sv})", (
                session_handle, "",
                {"handle_token": GLib.Variant("s", start_token)},
            )),
            _request_path(connection, start_token), timeout,
        )
        streams = _decode_streams(start_results)
        if not streams:
            raise PortalError("the portal returned no screen streams")

        # 4. OpenPipeWireRemote -> fd for pipewiresrc.
        reply, fd_list = connection.call_with_unix_fd_list_sync(
            BUS_NAME, OBJECT_PATH, SCREENCAST_IFACE, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session_handle, {})),
            GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, -1, None, None,
        )
        fd_index = int(reply.unpack()[0])
        if fd_list is None:
            raise PortalError("the portal returned no PipeWire fd")
        raw_fd = fd_list.get(fd_index)
        if raw_fd < 0:
            raise PortalError("the portal returned an invalid PipeWire fd")
        # The UnixFDList owns (or borrows) the returned descriptor; duplicate
        # it so the session outlives the reply object without a double-close.
        fd = os.dup(raw_fd)
        restore = _unpack(start_results.get("restore_token", "")) or ""
        session.fd = fd
        session.streams = streams
        session.restore_token = str(restore)
        return session
    except Exception:
        session.close()
        raise
