from gi.repository import GLib

from anycam.portal import (
    ScreenSession, ScreenStream, _call_and_wait, _decode_streams,
    _session_handle,
)


def test_decode_variant_streams():
    results = {
        "streams": GLib.Variant("a(ua{sv})", [(
            42,
            {
                "size": GLib.Variant("(ii)", (1920, 1080)),
                "position": GLib.Variant("(ii)", (0, 0)),
            },
        )]),
        "session_handle": GLib.Variant(
            "s", "/org/freedesktop/portal/desktop/session/1_1/anycam"),
    }
    streams = _decode_streams(results)
    assert len(streams) == 1
    assert streams[0].node_id == 42
    assert streams[0].width == 1920
    assert streams[0].height == 1080
    assert _session_handle(results).endswith("/anycam")


def test_decode_plain_streams_and_empty_results():
    results = {
        "streams": [(7, {"size": (800, 600), "position": (2, 3)})],
        "session_handle": "/session/plain",
    }
    streams = _decode_streams(results)
    assert streams[0].node_id == 7
    assert streams[0].x == 2
    assert streams[0].y == 3
    assert _decode_streams({}) == []
    assert _session_handle({}) == ""


def test_screen_session_close_is_idempotent_without_a_connection():
    session = ScreenSession("", -1, [ScreenStream(3, 640, 360)])
    session.close()
    session.close()
    # A closed session must not look reusable (it has no stream any more).
    assert session.node_id == -1
    assert session.fd == -1


def test_screen_session_replaces_an_fd_consumed_by_pipewiresrc(monkeypatch):
    """pipewiresrc owns its fd; the next consumer needs a fresh portal remote."""
    session = ScreenSession("/session/one", 12, [ScreenStream(3, 640, 360)])
    opened: list[int] = []

    def open_remote() -> int:
        opened.append(1)
        return 13

    monkeypatch.setattr(session, "open_remote", open_remote)
    assert session.take_remote() == 12
    assert session.fd == -1
    assert session.take_remote() == 13
    assert opened == [1]


def test_portal_wait_pumps_the_gui_while_the_picker_is_open():
    """The Qt loop must keep running while the external picker is up."""
    class Connection:
        def __init__(self) -> None:
            self.callback = None

        def signal_subscribe(self, *_args):
            self.callback = _args[-1]
            return 7

        def signal_unsubscribe(self, _subscription) -> None:
            pass

        def call_sync(self, *_args, **_kwargs) -> None:
            def respond() -> bool:
                self.callback(
                    None, None, None, None, None,
                    GLib.Variant("(ua{sv})", (0, {})))
                return False

            GLib.timeout_add(5, respond)

    pumped: list[int] = []
    results = _call_and_wait(
        Connection(), "SelectSources", GLib.Variant("()", ()),
        "/org/freedesktop/portal/desktop/request/test", 1.0,
        pump=lambda: pumped.append(1))
    assert results == {}
    assert pumped
