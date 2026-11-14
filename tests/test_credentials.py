"""The keyring wrapper: in-memory behaviour always, the real Secret Service only when asked.

The real-keyring tests write to whatever keyring answers on the session bus, so they
run only with ``ANYCAM_TEST_KEYRING=1`` (CI sets it inside a throwaway session).
"""
import os
import uuid

import pytest

from anycam import credentials


def test_a_stored_secret_is_found_again(memory_keyring):
    assert credentials.get_store().store("a", "label", "s3cret")
    assert credentials.get_store().lookup("a") == "s3cret"


def test_lookup_of_an_unknown_id_is_none(memory_keyring):
    assert credentials.get_store().lookup("nothing") is None


def test_remove_forgets_the_secret(memory_keyring):
    store = credentials.get_store()
    store.store("a", "label", "s3cret")
    assert store.remove("a")
    assert store.lookup("a") is None


def test_an_unavailable_keyring_reports_instead_of_raising(memory_keyring):
    memory_keyring.set_available(False)
    store = credentials.get_store()
    assert not credentials.available()
    assert store.store("a", "label", "x") is False
    assert store.lookup("a") is None
    assert store.remove("a") is False
    assert store.error


def test_set_store_none_goes_back_to_libsecret():
    credentials.set_store(None)
    assert isinstance(credentials.get_store(), credentials.LibsecretStore)


def test_libsecret_store_without_the_typelib_never_raises(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "gi.repository" and args and args[2] and "Secret" in args[2]:
            raise ImportError("no Secret")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    store = credentials.LibsecretStore()
    monkeypatch.undo()
    assert not store.available()
    assert store.store("a", "l", "x") is False
    assert store.lookup("a") is None
    assert store.remove("a") is False
    assert "libsecret" in store.error


real = pytest.mark.skipif(os.environ.get("ANYCAM_TEST_KEYRING") != "1",
                          reason="set ANYCAM_TEST_KEYRING=1 to use the real keyring")


@real
def test_the_real_keyring_round_trips():
    store = credentials.LibsecretStore()
    assert store.available(), store.error
    secret_id = f"test-{uuid.uuid4()}"
    try:
        assert store.store(secret_id, "AnyCam test", "pa55-ünï"), store.error
        assert store.lookup(secret_id) == "pa55-ünï"
    finally:
        store.remove(secret_id)
    assert store.lookup(secret_id) is None


# ---------------------------------------------------------------- URL secrets
from anycam import model, streaming  # noqa: E402

KEY = "live_123456_AbCdEfGhIjKlMnOp"
TWITCH = f"rtmp://live.twitch.tv/app/{KEY}"


def test_protect_replaces_a_stream_key_and_reveal_restores_it(memory_keyring):
    saved = credentials.protect(TWITCH, stream=True)
    assert KEY not in saved and saved.startswith("rtmp://live.twitch.tv/app/${secret:")
    assert credentials.reveal(saved) == TWITCH
    assert memory_keyring.values and list(memory_keyring.values.values()) == [KEY]


def test_saving_the_same_secret_again_reuses_its_entry(memory_keyring):
    first = credentials.protect(TWITCH, stream=True)
    second = credentials.protect(TWITCH, stream=True)
    assert first == second and len(memory_keyring.values) == 1


def test_srt_passphrase_and_streamid_and_camera_password_are_protected(memory_keyring):
    srt = "srt://host:9000?streamid=abcd1234&passphrase=hunter2hunter2&latency=120"
    out = credentials.protect(srt)
    assert "abcd1234" not in out and "hunter2" not in out and "latency=120" in out
    assert credentials.reveal(out) == srt
    cam = "rtsp://admin:s3cretpw@192.168.1.5:554/stream1"
    out = credentials.protect(cam)
    assert "s3cretpw" not in out and out.endswith("@192.168.1.5:554/stream1")
    assert credentials.reveal(out) == cam


def test_a_url_without_a_secret_is_left_alone(memory_keyring):
    for url in ("rtmp://host/live", "rtsp://192.168.1.5/stream", "http://example.com/a/b"):
        assert credentials.protect(url, stream=url.startswith("rtmp")) == url
    assert not memory_keyring.values


def test_no_keyring_keeps_the_secret_inline_and_says_so_once(memory_keyring, capsys, monkeypatch):
    monkeypatch.setattr(credentials, "_warned_inline", False)
    memory_keyring.set_available(False)
    assert credentials.protect(TWITCH, stream=True) == TWITCH
    assert credentials.protect(TWITCH, stream=True) == TWITCH
    assert capsys.readouterr().err.count("[credentials]") == 1


def test_a_saved_project_holds_no_secret_and_loads_back_whole(tmp_path, memory_keyring):
    project = model.Project(name="p")
    project.output.rtmp_url = TWITCH
    project.output.rtmp_extra_urls = ["rtmp://other.example/live/extra-key-987654"]
    scene = project.scenes[0] if project.scenes else model.Scene(model.new_id("scene"), "S")
    scene.add_layer(model.Layer(model.new_id("layer"), "network", "Cam",
                                source="rtsp://admin:s3cretpw@10.0.0.9/live"))
    project.scenes = [scene]
    path = project.save(tmp_path / "p.json")
    text = path.read_text() + path.with_name("p.json.bak").read_text() if path.with_name("p.json.bak").exists() \
        else path.read_text()
    for secret in (KEY, "extra-key-987654", "s3cretpw"):
        assert secret not in text
    loaded = model.Project.load(path)
    assert loaded.output.rtmp_url == TWITCH
    assert loaded.output.rtmp_extra_urls == ["rtmp://other.example/live/extra-key-987654"]
    assert loaded.scenes[0].layers[0].source == "rtsp://admin:s3cretpw@10.0.0.9/live"


def test_a_project_shared_to_a_machine_without_the_entry_asks_for_the_key(tmp_path, memory_keyring):
    project = model.Project(name="p")
    project.output.rtmp_url = TWITCH
    path = project.save(tmp_path / "p.json")
    memory_keyring.values.clear()  # another computer's keyring
    loaded = model.Project.load(path)
    assert credentials.has_reference(loaded.output.rtmp_url)
    assert "keyring" in streaming.validate_stream_url(loaded.output.rtmp_url)


def test_a_scene_preset_file_holds_no_camera_password(tmp_path, memory_keyring):
    scene = model.Scene(model.new_id("scene"), "Cams")
    scene.add_layer(model.Layer(model.new_id("layer"), "network", "Cam",
                                source="rtsp://admin:s3cretpw@10.0.0.9/live"))
    path = model.save_scene_preset(scene, tmp_path)
    assert "s3cretpw" not in path.read_text()
    assert model.load_scene_preset(path).layers[0].source == "rtsp://admin:s3cretpw@10.0.0.9/live"


def _old_project_text(extra=""):
    import json

    return json.dumps({
        "name": "old", "output": {"rtmp_url": TWITCH, "rtmp_extra_urls": []},
        "scenes": [{"id": "s1", "name": "S", "layers": [
            {"id": "l1", "kind": "network", "name": "Cam", "source": "rtsp://admin:s3cretpw@10.0.0.9/live"}]}],
        "active_scene": "s1"}, indent=2) + extra


def test_loading_an_old_project_moves_its_secrets_and_scrubs_the_copies(tmp_path, memory_keyring):
    path = tmp_path / "old.json"
    path.write_text(_old_project_text())
    path.with_name("old.json.bak").write_text(_old_project_text())
    path.with_name("old.json.corrupt").write_text(_old_project_text(extra="{{ truncated"))
    loaded = model.Project.load(path)
    assert loaded.output.rtmp_url == TWITCH
    for name in ("old.json", "old.json.bak", "old.json.corrupt"):
        text = (tmp_path / name).read_text()
        assert KEY not in text and "s3cretpw" not in text, name
    assert (path.stat().st_mode & 0o777) == 0o600
    again = model.Project.load(path)
    assert again.output.rtmp_url == TWITCH and len(memory_keyring.values) == 2


def test_an_old_project_is_left_as_it_was_when_no_keyring_works(tmp_path, memory_keyring):
    memory_keyring.set_available(False)
    path = tmp_path / "old.json"
    path.write_text(_old_project_text())
    before = path.read_text()
    assert model.Project.load(path).output.rtmp_url == TWITCH
    assert path.read_text() == before
