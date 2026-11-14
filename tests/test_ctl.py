"""``anycam ctl``: one JSON line per request over the owner-only instance socket."""
import json
import os
import stat
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from anycam import ctl  # noqa: E402
from anycam.gui import AnyCamWindow  # noqa: E402
from anycam.ui.instance import claim_gui_instance, serve_instance_requests  # noqa: E402

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    app = QApplication.instance() or QApplication([])
    name = f"anycam-test-{uuid.uuid4().hex[:8]}"
    primary, server = claim_gui_instance(name)
    assert primary and server is not None
    window = AnyCamWindow()
    window._instance_server = server
    serve_instance_requests(server, window)
    yield app, window, ctl.socket_path(name)
    window._persist = False
    window.close()
    server.close()
    app.processEvents()


def ask(app, path, action, argument=""):
    """Run the blocking client on a thread while the Qt loop serves it."""
    box = {}

    def work():
        try:
            box["reply"] = ctl.request(action, argument, path=path)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    while thread.is_alive():
        app.processEvents()
        thread.join(0.01)
    if "error" in box:
        raise box["error"]
    return box["reply"]


def test_the_client_finds_the_socket_qt_listens_on(served):
    _app, _window, path = served
    assert os.path.exists(path) and stat.S_ISSOCK(os.stat(path).st_mode)


def test_the_socket_is_owner_only(served):
    _app, _window, path = served
    assert os.stat(path).st_mode & 0o077 == 0


def test_list_names_the_hotkey_actions_and_the_extras(served):
    app, _window, path = served
    reply = ask(app, path, "list")
    assert reply["ok"] and {"record", "mute", "next-scene", "scene-1", "scene", "state"} <= set(reply["actions"])


def test_state_reports_scenes_and_recording(served):
    app, window, path = served
    reply = ask(app, path, "state")["state"]
    assert reply["recording"] is False and reply["active"] == window.project.active_scene
    assert [s["name"] for s in reply["scenes"]] == [s.name for s in window.project.scenes]


def test_scene_switches_by_name_number_and_id(served):
    app, window, path = served
    if len(window.project.scenes) < 2:
        from anycam.model import Scene, new_id

        window.project.scenes.append(Scene(new_id("scene"), "Close up"))
    second = window.project.scenes[1]
    reply = ask(app, path, "scene", "Close up")
    assert reply["ok"], reply
    assert reply["state"]["active"] == second.id
    assert ask(app, path, "scene", "1")["state"]["active"] == window.project.scenes[0].id
    assert ask(app, path, "scene", second.id)["state"]["active"] == second.id


def test_mute_runs_the_same_action_as_the_hotkey(served):
    app, window, path = served
    before = window.project.audio.mic_muted
    ask(app, path, "mute")
    assert window.project.audio.mic_muted != before


def test_unknown_actions_and_scenes_are_refused_not_raised(served):
    app, _window, path = served
    assert ask(app, path, "no-such")["ok"] is False
    reply = ask(app, path, "scene", "nonexistent")
    assert reply["ok"] is False and "nonexistent" in reply["error"]


def test_a_line_that_is_not_json_gets_an_error_and_a_bare_show_still_raises(served):
    app, window, path = served
    import socket

    def raw(data):
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(5)
        client.connect(path)
        client.sendall(data)
        out = b""
        box = {}

        def read():
            try:
                box["data"] = client.recv(4096)
            except OSError:
                box["data"] = b""

        thread = threading.Thread(target=read)
        thread.start()
        while thread.is_alive():
            app.processEvents()
            thread.join(0.01)
        client.close()
        return box["data"] or out

    reply = json.loads(raw(b"this is not json\n"))
    assert reply["ok"] is False
    raw(b"show")                     # what a second launch sends: no reply expected
    app.processEvents()


def test_with_no_window_the_cli_exits_2(tmp_path):
    env = dict(os.environ, TMPDIR=str(tmp_path), PYTHONPATH=SRC)
    result = subprocess.run([sys.executable, "-m", "anycam", "ctl", "state"], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and "not running" in result.stderr
