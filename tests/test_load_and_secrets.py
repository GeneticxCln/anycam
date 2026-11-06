"""Regression tests for tolerant project loading and secret redaction."""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from anycam import config  # noqa: E402
from anycam.model import (  # noqa: E402
    Layer, Project, Scene, empty_project, load_scene_preset,
)
from anycam.pipeline import AnyCamEngine, EngineReport  # noqa: E402
from anycam.pool import CaptureSpec, SourcePool  # noqa: E402

Gst.init(None)

RTSP = "rtsp://admin:hunter2@192.168.1.5:554/stream1"
RTMP = "rtmp://live.example.com/app/sk_live_123456"


# ------------------------------------------------------------------ redaction
def test_redact_url_hides_the_camera_password():
    redacted = config.redact_url(RTSP)
    assert "hunter2" not in redacted and "admin" not in redacted
    assert redacted.startswith("rtsp://192.168.1.5:554/")


def test_host_of_drops_credentials():
    assert config.host_of(RTSP) == "192.168.1.5:554"
    assert config.host_of("http://example.test/live") == "example.test"


def test_redact_text_masks_url_key_and_userinfo():
    message = f"Could not connect to {RTMP}: refused (key sk_live_123456)"
    cleaned = config.redact_text(message, RTMP)
    assert "sk_live_123456" not in cleaned
    assert "live.example.com" in cleaned
    # A URL that was never registered still loses its credentials.
    assert "hunter2" not in config.redact_text(f"open failed: {RTSP}")


def test_redact_text_leaves_short_path_words_alone():
    cleaned = config.redact_text("failed to publish live stream",
                                 "rtmp://host/app/live")
    assert cleaned == "failed to publish live stream"


def test_report_redacts_an_rtmp_error_that_quotes_the_url():
    report = EngineReport(rtmp_url=RTMP,
                          rtmp_error=f"connect to {RTMP} failed")
    assert "sk_live_123456" not in json.dumps(report.as_dict())


def error_message(source, text):
    error = GLib.Error.new_literal(Gst.ResourceError.quark(), text,
                                   Gst.ResourceError.OPEN_READ)
    return Gst.Message.new_error(source, error, "debug")


def test_engine_scrubs_the_stream_key_from_rtmp_bus_errors():
    engine = AnyCamEngine(Project(), preview=True, rtmp=RTMP)
    bus = Gst.Bus.new()
    bus.post(error_message(None, f"Could not connect to {RTMP}"))
    engine._rtmp_bus = bus
    engine._poll_rtmp_bus()
    assert engine.report.rtmp_error
    assert "sk_live_123456" not in engine.report.rtmp_error


def test_pool_scrubs_camera_credentials_from_capture_errors():
    pool = SourcePool()
    try:
        entry = pool.acquire(CaptureSpec("network", RTSP, 320, 180, 30))
        entry.pipeline.post_message(
            error_message(entry.pipeline, f"Could not open {RTSP}"))
        pool.poll_errors()
        assert entry.error
        assert "hunter2" not in entry.error
    finally:
        pool.stop()


# ----------------------------------------------------------- tolerant loading
def load(data) -> Project:
    return Project.from_dict(json.loads(json.dumps(data)))


def test_one_unusable_layer_does_not_refuse_the_project():
    project = load({"scenes": [{"id": "s", "name": "S", "layers": [
        {"id": "keep", "kind": "camera", "name": "Keep", "source": "/dev/video0"},
        {"id": "bad", "name": "no kind"},
        "not an object",
    ]}]})
    assert [layer.id for layer in project.scene.layers] == ["keep"]
    assert len(project.load_warnings) == 2
    assert "skipped layer 2" in project.load_warnings[0]


def test_missing_and_malformed_layer_fields_fall_back_to_defaults():
    project = load({"scenes": [{"id": "s", "layers": [
        {"kind": "camera", "box": [0, 0, 1], "crop": "none",
         "alpha": "opaque", "z": "top", "visible": "no",
         "camera_width": None, "font_size": float("nan") if False else "big",
         "effects": {"brightness": "bright", "mirror": 1, "bogus": 3}},
    ]}]})
    layer = project.scene.layers[0]
    assert layer.id.startswith("layer_")          # generated
    assert layer.name == "camera"
    assert layer.box == (0.0, 0.0, 1.0, 1.0)      # 3 values: default box
    assert layer.crop == (0.0, 0.0, 0.0, 0.0)
    assert layer.alpha == 1.0 and layer.z == 0
    assert layer.visible is False                  # "no" is a valid bool
    assert layer.camera_width == 0 and layer.font_size == 0.05
    assert layer.effects.brightness == 0.0 and layer.effects.mirror is True
    assert not project.load_warnings               # repairs, not drops


def test_non_finite_numbers_are_replaced():
    layer = Layer.from_dict({"id": "n", "kind": "text",
                             "box": [0, 0, float("nan"), 1],
                             "alpha": float("inf")})
    assert layer.box == (0.0, 0.0, 1.0, 1.0) and layer.alpha == 1.0


def test_duplicate_layer_and_scene_ids_are_made_unique():
    project = load({"scenes": [
        {"id": "dup", "layers": [{"id": "a", "kind": "test"},
                                 {"id": "a", "kind": "test"}]},
        {"id": "dup", "layers": []},
    ], "active_scene": "dup"})
    ids = [layer.id for layer in project.scenes[0].layers]
    assert len(set(ids)) == 2
    assert len({scene.id for scene in project.scenes}) == 2
    assert len(project.load_warnings) == 2


def test_bad_playlist_and_settings_values_do_not_raise():
    project = load({
        "output": {"canvas_w": "wide", "canvas_h": 0, "fps": -5},
        "audio": {"mic_gain_db": "loud", "mic_enabled": "yes"},
        "editor": {"snap_px": None},
        "scenes": [{"id": "s", "playlist": {"interval": "fast", "index": "x",
                                            "layer_ids": "oops"}}],
    })
    assert (project.output.canvas_w, project.output.canvas_h) == (1920, 1080)
    assert project.output.fps == 60
    assert project.audio.mic_gain_db == 0.0 and project.audio.mic_enabled
    assert project.editor.snap_px == 6.0
    assert project.scene.playlist.interval == 3.0
    assert project.scene.playlist.layer_ids == []


def test_unknown_active_scene_falls_back_to_the_first():
    project = load({"scenes": [{"id": "only"}], "active_scene": "ghost"})
    assert project.active_scene == "only"


def test_non_object_documents_are_refused_clearly():
    with pytest.raises(ValueError):
        Project.from_dict([1, 2, 3])


def test_valid_projects_round_trip_unchanged(tmp_path):
    project = empty_project()
    layer = Layer("l1", "camera", "Cam", source="/dev/video0",
                  box=(0.1, 0.2, 0.3, 0.4), crop=(0.1, 0.0, 0.1, 0.0), z=3)
    layer.effects.mirror = True
    project.scene.add_layer(layer)
    loaded = Project.load(project.save(tmp_path / "p.json"))
    assert loaded.to_dict() == project.to_dict()
    assert loaded.load_warnings == []
    assert "load_warnings" not in loaded.to_dict()


def test_network_layer_names_lose_credentials_from_older_projects():
    layer = Layer.from_dict({"id": "n", "kind": "network",
                             "name": "admin:hunter2@192.168.1.5:554",
                             "source": RTSP})
    assert layer.name == "192.168.1.5:554"


def test_scene_preset_loading_reports_dropped_layers(tmp_path):
    path = tmp_path / "x.anycam-scene.json"
    path.write_text(json.dumps({"id": "s", "name": "S",
                                "layers": [{"name": "no kind"}]}))
    warnings: list[str] = []
    scene = load_scene_preset(path, warnings)
    assert scene.layers == [] and warnings


# --------------------------------------------------------------- backup guard
def test_saving_over_a_corrupt_file_keeps_the_last_good_backup(tmp_path):
    path = tmp_path / "project.json"
    project = empty_project("good")
    project.save(path)
    project.name = "second"
    project.save(path)                      # .bak now holds "good"
    path.write_text('{"scenes": [ truncated')
    project.name = "third"
    project.save(path)                      # must not copy the truncated file
    backup = json.loads((tmp_path / "project.json.bak").read_text())
    assert backup["name"] == "good"
    assert json.loads(path.read_text())["name"] == "third"


# ------------------------------------------------------------------------ GUI
def test_window_reports_repairs_made_while_loading(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    path = config.project_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    scene = Scene("s", "S")
    scene.add_layer(Layer("ok", "test", "Pattern", source="ball"))
    data = Project(scenes=[scene], active_scene="s").to_dict()
    data["scenes"][0]["layers"].append({"name": "broken, no kind"})
    path.write_text(json.dumps(data))

    from PySide6.QtWidgets import QApplication

    from anycam.gui import AnyCamWindow

    app = QApplication.instance() or QApplication([])
    window = AnyCamWindow()
    try:
        assert "repairs" in window._load_warning
        assert [layer.id for layer in window.scene.layers] == ["ok"]
        assert not path.with_name("project.json.corrupt").exists()
    finally:
        window._persist = False
        window.close()
        app.processEvents()


def test_network_layer_is_named_without_credentials(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))

    from PySide6.QtWidgets import QApplication

    from anycam.gui import AnyCamWindow

    app = QApplication.instance() or QApplication([])
    window = AnyCamWindow()
    window.headless = True
    try:
        layer = window.add_layer("network", RTSP)
        assert layer is not None and layer.source == RTSP
        assert layer.name == "192.168.1.5:554"
    finally:
        window._persist = False
        window.close()
        app.processEvents()
