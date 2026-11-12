"""Phase 8: switching scenes, muting and recording from a paired phone."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import ssl

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from anycam.model import Layer, Project, Scene



@pytest.fixture
def service(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    from anycam.phone.server import PhoneService

    svc = PhoneService(state_dir=tmp_path / "state", cert_dir=tmp_path / "cert",
                       host="127.0.0.1", port=0)
    svc.start()
    yield svc
    svc.stop()


def _request(svc, method, path, body=None, headers=None):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(svc.host, svc.port, timeout=10, context=context)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _pair(svc) -> dict:
    """Pair a phone and return its Authorization header."""
    token = svc.pairing_url().split("#pair=", 1)[1]
    status, _h, body = _request(svc, "POST", "/api/pair",
                                json.dumps({"token": token, "name": "Pixel"}),
                                {"Content-Type": "application/json"})
    assert status == 200
    data = json.loads(body)
    return {"Authorization": f"Bearer {data['device']}.{data['secret']}"}


def post(svc, headers, body):
    return _request(svc, "POST", "/api/control", json.dumps(body).encode(),
                    {**headers, "Content-Type": "application/json"})


def test_the_remote_page_and_script_are_served(service):
    status, headers, body = _request(service, "GET", "/remote")
    assert status == 200 and "text/html" in headers["Content-Type"] and b"AnyCam remote" in body
    status, headers, body = _request(service, "GET", "/remote.js")
    assert status == 200 and "javascript" in headers["Content-Type"]
    assert b"/api/control" in body


def test_an_unpaired_phone_cannot_control_anything(service):
    service.control_enabled = True
    service.control_handler = lambda *_: {"ok": True}
    assert _request(service, "POST", "/api/control", b'{"action":"mute"}')[0] == 401
    assert _request(service, "GET", "/api/control/state")[0] == 401
    bad = {"Authorization": "Bearer nobody.wrong"}
    assert _request(service, "GET", "/api/control/state", headers=bad)[0] == 401


def test_control_is_refused_until_it_is_switched_on(service):
    headers = _pair(service)
    service.control_handler = lambda *_: {"ok": True}
    status, _h, body = post(service, headers, {"action": "mute"})
    assert status == 403 and b"switched off" in body
    service.control_enabled = True
    assert post(service, headers, {"action": "mute"})[0] == 200


def test_only_known_actions_with_clean_arguments_reach_the_handler(service):
    headers = _pair(service)
    service.control_enabled = True
    seen = []
    service.control_handler = lambda action, data, device: seen.append((action, data)) or {"ok": 1}
    assert post(service, headers, {"action": "format-disk"})[0] == 400
    assert post(service, headers, {"nothing": 1})[0] == 400
    assert _request(service, "POST", "/api/control", b"not json", headers)[0] == 400
    assert post(service, headers, {"action": "scene", "scene_id": "s2", "evil": "x"})[0] == 200
    assert seen == [("scene", {"scene_id": "s2", "value": None})]      # only the known keys


def test_a_handler_that_fails_does_not_leak_a_trace(service):
    headers = _pair(service)
    service.control_enabled = True

    def boom(*_args):
        raise RuntimeError("secret internal path /home/me/x")

    service.control_handler = boom
    status, _h, body = post(service, headers, {"action": "record"})
    assert status == 403 and b"secret" not in body and b"could not do that" in body


# --------------------------------------------------------------- the window side
@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    for name in ("CACHE", "CONFIG", "STATE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    from PySide6.QtWidgets import QApplication

    from anycam.gui_many import ManyCamWindow

    QApplication.instance() or QApplication([])
    project = Project(name="p8")
    first, second = Scene("scene_a", "Alpha"), Scene("scene_b", "Beta")
    first.add_layer(Layer("l1", "test", "Ball", source="ball"))
    second.add_layer(Layer("l2", "test", "Bars", source="smpte"))
    project.scenes, project.active_scene = [first, second], first.id
    win = ManyCamWindow(project)
    win.headless = True
    win._persist = False
    yield win
    win.engine = None
    win._stop_phone_link()
    win.close()


def test_the_window_answers_scene_mute_and_record_requests(window, monkeypatch):
    toggles = []
    monkeypatch.setattr(type(window), "toggle_record", lambda self: toggles.append("record"))
    state = window.phone_control("state", {})
    assert state["active"] == "scene_a" and [s["name"] for s in state["scenes"]] == ["Alpha", "Beta"]
    assert window.phone_control("scene", {"scene_id": "scene_b"})["active"] == "scene_b"
    assert "error" in window.phone_control("scene", {"scene_id": "nope"})
    assert window.phone_control("mute", {})["muted"] is True
    window.phone_control("record", {})
    assert toggles == ["record"]
    assert "error" in window.phone_control("shutdown", {})


def test_requests_from_other_threads_run_on_the_ui_thread(window):
    import threading

    from PySide6.QtCore import QCoreApplication

    results = []
    thread = threading.Thread(
        target=lambda: results.append(window._phone_control_call(
            "scene", {"scene_id": "scene_b"}, "dev")))
    thread.start()
    import time

    end = time.monotonic() + 5
    while thread.is_alive() and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    thread.join(1)
    assert results and results[0]["active"] == "scene_b"


def test_the_setting_switches_the_service_and_is_saved(window):
    window.phone_control_check.setChecked(True)
    assert window.project.output.phone_control is True
    service = window._phone_service
    if service is not None:                         # started only where TLS tooling exists
        assert service.control_enabled is True
        window.phone_control_check.setChecked(False)
        assert service.control_enabled is False
