"""Phase 6: effects in a worker process, effect packs, favourites."""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pytest
from PIL import Image

from anycam import effect_worker, effects, packs
from anycam.effect_worker import RemoteProcessor


def picture(value=(200, 30, 30, 255), size=(64, 48)) -> Image.Image:
    return Image.new("RGBA", size, value)


def wait_ready(remote: RemoteProcessor, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not remote.ready and time.monotonic() < deadline:
        time.sleep(0.05)
    assert remote.ready, remote.status()


def pixel(image: Image.Image) -> tuple:
    return tuple(np.asarray(image)[0, 0])


def process_until(remote, predicate, seconds: float = 30.0, **kw) -> Image.Image:
    deadline = time.monotonic() + seconds
    out = remote.process(picture(**kw), 0.0)
    while not predicate(out) and time.monotonic() < deadline:
        time.sleep(0.1)
        out = remote.process(picture(**kw), 0.0)
    return out


def test_frames_travel_through_the_worker_and_come_back_processed():
    remote = RemoteProcessor("selftest:invert")
    try:
        wait_ready(remote)
        out = remote.process(picture(), 0.0)
        assert pixel(out) == (55, 225, 225, 255) and out.size == (64, 48)
        big = remote.process(picture(size=(320, 180)), 0.0)        # the block grows
        assert big.size == (320, 180) and pixel(big) == (55, 225, 225, 255)
        assert remote.worker_pid() not in (0, os.getpid())
    finally:
        remote.close()


def test_the_picture_passes_through_until_the_worker_is_up():
    remote = RemoteProcessor("selftest:invert")
    try:
        first = remote.process(picture(), 0.0)
        assert not remote.ready and pixel(first) == (200, 30, 30, 255)
    finally:
        remote.close()


def test_a_crashing_worker_is_replaced_and_the_picture_never_stops():
    remote = RemoteProcessor("selftest:crash")
    remote.RETRY_FIRST_S = 0.2
    try:
        wait_ready(remote)
        pids = {remote.worker_pid()}
        outputs = []
        deadline = time.monotonic() + 40
        while remote.restarts < 1 and time.monotonic() < deadline:
            outputs.append(remote.process(picture(), 0.0))
            time.sleep(0.05)
        assert remote.restarts >= 1, remote.status()
        assert all(out.size == (64, 48) for out in outputs)        # a frame every time
        assert "exited" in remote.last_error or "stopped" in remote.last_error
        wait_ready(remote)
        assert remote.worker_pid() not in pids                      # a new process
    finally:
        remote.close()


def test_a_hung_worker_is_given_up_on():
    remote = RemoteProcessor("selftest:hang")
    remote.FRAME_TIMEOUT_S = 0.5
    remote.RETRY_FIRST_S = 0.2
    try:
        wait_ready(remote)
        started = time.monotonic()
        for _ in range(4):
            out = remote.process(picture(), 0.0)
            assert out.size == (64, 48)
            if remote.restarts:
                break
        assert remote.restarts >= 1 and time.monotonic() - started < 10
        assert "stopped answering" in remote.last_error
    finally:
        remote.close()


def test_an_effect_that_raises_leaves_the_frame_alone_and_the_worker_alive():
    remote = RemoteProcessor("selftest:raise")
    try:
        wait_ready(remote)
        out = remote.process(picture(), 0.0)
        assert pixel(out) == (200, 30, 30, 255) and remote.ready
        assert "selftest failure" in remote.last_error
    finally:
        remote.close()


def test_a_worker_that_cannot_build_its_processor_reports_why():
    remote = RemoteProcessor("selftest-nope:x")
    try:
        deadline = time.monotonic() + 30
        while not remote.last_error and time.monotonic() < deadline:
            time.sleep(0.1)
        assert "KeyError" in remote.last_error and not remote.ready
    finally:
        remote.close()


def test_isolation_applies_to_models_and_packs_but_not_to_cheap_effects(monkeypatch):
    monkeypatch.delenv("ANYCAM_AI_WORKER", raising=False)
    effect_worker.set_isolation(False)
    assert not effect_worker.wants_isolation("background:blur")
    assert effect_worker.wants_isolation("pack:demo/x")               # third-party: always
    effect_worker.set_isolation(True)
    try:
        assert effect_worker.wants_isolation("background:blur")
        assert effect_worker.wants_isolation("sticker:crown")
        assert not effect_worker.wants_isolation("zoom:")
        assert not effect_worker.wants_isolation("corners:")
        monkeypatch.setenv("ANYCAM_AI_WORKER", "0")
        assert not effect_worker.wants_isolation("background:blur")
    finally:
        effect_worker.set_isolation(False)


# ----------------------------------------------------------------- effect packs
PACK_CODE = '''
from PIL import ImageOps

class Effect:
    def __init__(self, amount=0.5):
        self.amount = amount

    def process(self, image, now):
        return ImageOps.invert(image.convert("RGB")).convert("RGBA")
'''


def write_pack(root, name="demo", **overrides):
    folder = root / name
    folder.mkdir(parents=True)
    manifest = {"name": name, "version": "1.0", "author": "Tester", "license": "MIT",
                "effects": [{"key": "flip", "label": "Inverter", "description": "Negative",
                             "file": "flip.py", "default": 0.7}]}
    manifest.update(overrides)
    (folder / "pack.json").write_text(json.dumps(manifest))
    (folder / "flip.py").write_text(PACK_CODE)
    return folder


def test_a_valid_pack_is_discovered_with_its_licence(tmp_path):
    write_pack(tmp_path)
    found, problems = packs.discover(tmp_path)
    assert problems == []
    assert [(e.pack, e.key, e.label, e.license, e.author) for e in found] == [
        ("demo", "flip", "Inverter", "MIT", "Tester")]
    assert found[0].processor == "pack:demo/flip" and found[0].default == 0.7


@pytest.mark.parametrize("bad, text", [
    ({"name": "Bad Name!"}, "plain lowercase"),
    ({"effects": []}, "no effects"),
    ({"effects": [{"key": "x", "file": "../escape.py"}]}, "inside the pack"),
    ({"effects": [{"key": "X Y", "file": "flip.py"}]}, "plain lowercase"),
    ({"effects": [{"key": "x", "file": "missing.py"}]}, "missing"),
    ({"effects": ["nope"]}, "not an object"),
])
def test_bad_packs_are_refused_with_a_reason(tmp_path, bad, text):
    write_pack(tmp_path, **bad)
    found, problems = packs.discover(tmp_path)
    assert found == [] and any(text in problem for problem in problems), problems


def test_broken_json_does_not_stop_other_packs(tmp_path):
    write_pack(tmp_path, "good")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "pack.json").write_text("{not json")
    found, problems = packs.discover(tmp_path)
    assert [e.pack for e in found] == ["good"] and "unreadable" in problems[0]


def test_a_pack_effect_runs_in_the_worker_process(tmp_path):
    write_pack(tmp_path)
    packs_root = tmp_path
    os.environ["XDG_DATA_HOME"] = str(tmp_path / "data")
    (tmp_path / "data" / "anycam" / "effects").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "data" / "anycam" / "effects" / "demo"
    import shutil

    shutil.copytree(packs_root / "demo", target)
    try:
        remote = RemoteProcessor("pack:demo/flip")
        try:
            wait_ready(remote)
            out = remote.process(picture(), 0.0)
            assert pixel(out) == (55, 225, 225, 255)
        finally:
            remote.close()
    finally:
        os.environ.pop("XDG_DATA_HOME", None)


def test_pack_effects_join_the_filter_library(tmp_path, monkeypatch):
    write_pack(tmp_path)
    try:
        assert effects.reload_packs(tmp_path) == []
        spec = effects.FILTERS["pack-demo-flip"]
        assert spec.category == "Packs" and spec.processor == "pack:demo/flip"
        assert spec in effects.available_filters("Packs")
        assert effects.normalize_filter("pack-demo-flip") == "pack-demo-flip"
    finally:
        effects.reload_packs(tmp_path / "nothing-here")
    assert "pack-demo-flip" not in effects.FILTERS


# -------------------------------------------------------------------- favourites
@pytest.fixture
def clean_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


def test_favourites_keep_order_survive_and_drop_unknown_keys(clean_config):
    assert effects.favourites() == []
    effects.set_favourite("fisheye", True)
    effects.set_favourite("bg-blur", True)
    effects.set_favourite("not-a-filter", True)
    assert effects.favourites() == ["fisheye", "bg-blur"]
    effects.set_favourite("fisheye", False)
    assert effects.favourites() == ["bg-blur"]
    path = effects._favourites_path()
    path.write_text("{broken")
    assert effects.favourites() == []
    path.write_text(json.dumps(["bg-blur", "bg-blur", 5, "gone"]))
    assert effects.favourites() == ["bg-blur"]


# --------------------------------------------------------------------- the window
@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    for name in ("CACHE", "CONFIG", "STATE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    from PySide6.QtWidgets import QApplication

    from anycam.gui_many import ManyCamWindow
    from anycam.model import Layer, Project, Scene

    QApplication.instance() or QApplication([])
    project = Project(name="p6")
    scene = Scene("scene_a", "Alpha")
    scene.add_layer(Layer("l1", "test", "Ball", source="ball"))
    project.scenes, project.active_scene = [scene], scene.id
    win = ManyCamWindow(project)
    win.headless = True
    win._persist = False
    yield win
    win.engine = None
    win.close()
    effect_worker.set_isolation(False)


def test_the_favourite_star_follows_the_selected_filter(window):
    layer = window.scene.layers[0]
    window.preview.selected_id = layer.id
    layer.effects.filter = "fisheye"
    window._sync_effects_controls()
    assert window.favourite_check.isEnabled() and not window.favourite_check.isChecked()
    window.favourite_check.setChecked(True)
    assert effects.favourites() == ["fisheye"]
    window.effect_category.setCurrentIndex(window.effect_category.findData("Favourites"))
    labels = [entry["label"] for entry in window._effect_entries]
    assert labels == ["Fisheye"]
    layer.effects.filter = "none"
    window._sync_effects_controls()
    assert not window.favourite_check.isEnabled()


def test_the_isolation_setting_reaches_the_worker_switch(window):
    assert not window.effects_isolated_check.isChecked() and not effect_worker.isolation_enabled()
    window.effects_isolated_check.setChecked(True)
    assert window.project.output.effects_isolated is True
    assert effect_worker.isolation_enabled() and effect_worker.wants_isolation("background:blur")
    window.effects_isolated_check.setChecked(False)
    assert window.project.output.effects_isolated is False
    assert not effect_worker.wants_isolation("background:blur")


def test_packs_appear_in_the_categories_and_the_settings_summary(window, tmp_path):
    write_pack(tmp_path / "data" / "anycam" / "effects")
    assert effects.reload_packs() == []
    window.effect_category.setCurrentIndex(window.effect_category.findData("Packs"))
    assert "Inverter" in [entry["label"] for entry in window._effect_entries]
    assert "1 effect pack entry" in window._packs_summary()
    effects.reload_packs(tmp_path / "none")


def test_a_worker_restart_holds_the_last_processed_frame():
    """While the worker starts or backs off, the raw picture used to pass
    through and flash the effect off (audit item 8)."""
    import threading

    processed = picture((200, 30, 30, 255))
    processor = object.__new__(RemoteProcessor)
    processor._lock = threading.Lock()
    processor._conn = None
    processor._starting = True          # no spawn attempt from this test
    processor._retry_at = float("inf")
    processor._closed = True
    processor._last_good = processed

    raw = picture((30, 30, 200, 255))
    assert pixel(processor.process(raw, 0.0)) == (200, 30, 30, 255)

    resized = picture((30, 30, 200, 255), size=(16, 12))
    assert processor.process(resized, 0.0) is resized   # a resize cannot be held

    fresh = object.__new__(RemoteProcessor)
    fresh._lock = threading.Lock()
    fresh._conn = None
    fresh._starting = True
    fresh._retry_at = float("inf")
    fresh._closed = True
    fresh._last_good = None
    assert fresh.process(raw, 0.0) is raw               # first frames still pass through
