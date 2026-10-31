from gi.repository import GLib

from anycam.portal import ScreenSession, ScreenStream, _decode_streams, _session_handle


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
    assert session.node_id == 3
    assert session.fd == -1
