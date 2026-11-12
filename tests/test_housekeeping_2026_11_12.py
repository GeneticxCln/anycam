"""Phase 0 of the 2026-11-12 work: doctor elements, screencast check, live-add cleanup."""
from __future__ import annotations

import importlib.util
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from pathlib import Path

import pytest

from anycam import __main__ as cli
from anycam import config, model

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("CACHE", "CONFIG", "STATE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))


# ------------------------------------------------------------ element check
def _everything() -> set[str]:
    names: set[str] = set()
    for entry in config.REQUIRED_ELEMENTS:
        names.update(entry.split("|")[:1])
    return names


def test_a_complete_install_reports_nothing_missing():
    report = config.check_elements(_everything())
    assert report["missing"] == []


def test_each_missing_required_element_is_named():
    have = _everything() - {"x264enc", "valve"}
    assert config.check_elements(have)["missing"] == ["valve", "x264enc"]


def test_either_rtmp_sink_satisfies_the_stream_requirement():
    base = _everything() - {"rtmp2sink"}
    assert "rtmp2sink|rtmpsink" in config.check_elements(base)["missing"]
    assert config.check_elements(base | {"rtmpsink"})["missing"] == []
    assert config.check_elements(base | {"rtmp2sink"})["missing"] == []


def test_optional_elements_never_fail_the_check():
    report = config.check_elements(_everything())
    assert "glvideomixer" in report["optional_missing"]
    assert report["missing"] == []


def test_without_gst_inspect_the_check_says_so(monkeypatch):
    monkeypatch.setattr(config, "run", lambda cmd, timeout=5.0: (127, "no such file"))
    assert config.installed_elements() is None
    assert config.check_elements()["missing"] == ["gst-inspect-1.0"]


def test_inspect_output_is_parsed(monkeypatch):
    out = "coreelements:  tee: Transforms\nvideoconvertscale:  videoconvert: Colorspace\n"
    monkeypatch.setattr(config, "run", lambda cmd, timeout=5.0: (0, out))
    assert config.installed_elements() == {"tee", "videoconvert"}


def test_the_elements_command_sets_its_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(config, "installed_elements", lambda: _everything() - {"queue"})
    assert cli.main(["elements"]) == 1
    assert "MISSING queue" in capsys.readouterr().out
    monkeypatch.setattr(config, "installed_elements", lambda: _everything())
    assert cli.main(["elements"]) == 0


def test_setup_script_uses_the_shared_list():
    text = (ROOT / "scripts" / "setup.sh").read_text()
    assert "anycam elements" in text
    assert "for element in" not in text


# ------------------------------------------------------ screencast checking
def _screencast_module():
    pytest.importorskip("gi")
    spec = importlib.util.spec_from_file_location(
        "capture_screencast", ROOT / "scripts" / "capture-screencast.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_flat_or_wrong_shaped_frame_is_not_ok():
    from PIL import Image

    problems = _screencast_module().frame_problems
    busy = Image.effect_noise((160, 90), 60).convert("RGB")
    assert problems(busy, (1920, 1080)) == []
    assert problems(busy, (0, 0)) == []                      # size unknown
    assert problems(busy, (3840, 2160)) == []                # same shape, HiDPI
    assert any("flat" in p for p in problems(Image.new("RGB", (160, 90), (0, 0, 0)),
                                             (1920, 1080)))
    assert any("portal stream" in p for p in problems(busy, (1080, 1920)))
    assert any("only" in p for p in problems(Image.new("RGB", (1, 1)), (1920, 1080)))


# --------------------------------------------------- a failed live layer add
def test_a_layer_that_fails_to_attach_leaves_the_canvas_intact(monkeypatch):
    pytest.importorskip("gi")
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    from anycam.pipeline import AnyCamEngine

    project = model.default_project()
    project.scenes[0].layers = [model.Layer("a", "test", "T", source="ball")]
    project.output.canvas_w, project.output.canvas_h, project.output.fps = 320, 180, 15
    engine = AnyCamEngine(project, preview=True)
    engine.build()
    engine.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            engine.pump(50)
        pads_before = len(engine.compositor.sinkpads)
        chains_before = len(engine.chains)
        frames_before = engine._frame_count

        monkeypatch.setattr(engine, "_link_to_mixer",
                            lambda *a, **k: Gst.PadLinkReturn.NOFORMAT)
        extra = model.Layer("b", "animation", "Bd", source="plasma",
                            box=(0.0, 0.0, 0.5, 0.5))
        project.scenes[0].add_layer(extra)
        assert engine.add_layer(extra) is False
        monkeypatch.undo()

        assert len(engine.compositor.sinkpads) == pads_before
        assert len(engine.chains) == chains_before
        assert "b" not in engine._layer_pads and "b" not in engine._layer_chains
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and engine._frame_count <= frames_before + 3:
            engine.pump(50)
        assert engine._frame_count > frames_before + 3, "the canvas froze"
    finally:
        engine.stop()


# ----------------------------------------------------------- vcam persistence
def test_the_virtual_camera_setting_round_trips(tmp_path):
    project = model.default_project()
    assert project.output.vcam_enabled is True               # unchanged default
    project.output.vcam_enabled = False
    saved = project.save(tmp_path / "p.json")
    assert model.Project.load(saved).output.vcam_enabled is False
    assert model.Project.from_dict({"output": {"vcam_enabled": "no"}}).output.vcam_enabled is False
    assert model.Project.from_dict({}).output.vcam_enabled is True


def test_the_vcam_button_remembers_the_choice():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from anycam.gui_many import ManyCamWindow

    app = QApplication.instance() or QApplication([])
    window = ManyCamWindow(model.empty_project())
    window.headless = True
    window._persist = False

    class FakeEngine:
        def detach_vcam(self):
            pass

    try:
        window.engine = FakeEngine()
        window.vcam_on = True
        window.toggle_vcam()                                 # switches it off
        assert window.vcam_on is False
        assert window.project.output.vcam_enabled is False
    finally:
        window.engine = None
        window.close()
        app.processEvents()


# ------------------------------------------------------------ live scene switch
def _two_scene_project():
    project = model.Project(name="switch")
    project.output.canvas_w, project.output.canvas_h, project.output.fps = 320, 180, 15
    first = model.Scene(model.new_id("scene"), "One")
    first.add_layer(model.Layer("a1", "test", "Ball", source="ball", box=(0, 0, 1, 1)))
    first.add_layer(model.Layer("a2", "text", "Title", source="ONE", box=(0.1, 0.1, 0.4, 0.2)))
    hidden = model.Layer("a3", "test", "Hidden", source="snow", box=(0.5, 0.5, 0.3, 0.3))
    hidden.visible = False
    first.add_layer(hidden)
    second = model.Scene(model.new_id("scene"), "Two")
    second.add_layer(model.Layer("b1", "test", "Bars", source="smpte", box=(0, 0, 0.5, 1)))
    second.add_layer(model.Layer("b2", "test", "Checks", source="checkers-8",
                                 box=(0.5, 0, 0.5, 1)))
    project.scenes = [first, second]
    project.active_scene = first.id
    return project, first, second


def _run(engine, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        engine.pump(40)


def test_switching_scenes_swaps_layers_on_the_same_canvas():
    pytest.importorskip("gi")
    from anycam.pipeline import AnyCamEngine

    project, first, second = _two_scene_project()
    engine = AnyCamEngine(project, preview=True).build()
    engine.start()
    try:
        _run(engine, 1.0)
        assert set(engine._layer_pads) == {"a1", "a2", "a3"}
        pipeline_id = id(engine.pipeline)
        compositor = engine.compositor

        assert engine.switch_scene(second) is True
        _run(engine, 0.5)
        assert set(engine._layer_pads) == {"b1", "b2"}
        assert engine.scene is second and engine.compositor is compositor
        assert id(engine.pipeline) == pipeline_id
        # background standby + the two new layers; the old pads were released
        assert len(compositor.sinkpads) == 3
        # the scene we left comes back exactly as it was (a3 stays hidden)
        assert [(l.id, l.visible) for l in first.layers] == [
            ("a1", True), ("a2", True), ("a3", False)]

        before = engine._frame_count
        assert engine.switch_scene(first) is True
        _run(engine, 1.0)
        assert set(engine._layer_pads) == {"a1", "a2", "a3"}
        assert engine._frame_count > before + 5, "the canvas stopped after the switch"
        assert not engine.report.error
    finally:
        engine.stop()


def test_a_live_scene_switch_keeps_one_continuous_recording(tmp_path):
    pytest.importorskip("gi")
    import shutil
    import subprocess

    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is required")
    from anycam.engine.outputs.recorder import Recorder
    from anycam.pipeline import AnyCamEngine

    project, first, second = _two_scene_project()
    out = tmp_path / "switching.mp4"
    recorder = Recorder()
    assert recorder.start(str(out), 320, 180, 15)
    engine = AnyCamEngine(project, recorder=recorder, preview=True).build()
    engine.start()
    try:
        for scene in (first, second, first, second):
            assert engine.switch_scene(scene)
            _run(engine, 0.8)
    finally:
        engine.stop()
    result = recorder.stop()
    assert result["ok"], result
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                           "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    assert float(info.stdout) > 2.5          # one file across all four switches


def test_a_scene_with_a_screen_share_asks_for_a_rebuild():
    pytest.importorskip("gi")
    from anycam.pipeline import AnyCamEngine

    project, first, second = _two_scene_project()
    shared = model.Scene(model.new_id("scene"), "Share")
    shared.add_layer(model.Layer("s1", "screen", "Screen", source="42"))
    project.scenes.append(shared)
    engine = AnyCamEngine(project, preview=True).build()
    engine.start()
    try:
        _run(engine, 0.5)
        assert engine.switch_scene(shared) is False
        assert engine.scene is first             # nothing was touched
    finally:
        engine.stop()


def test_the_window_switches_scenes_without_restarting_the_engine(monkeypatch):
    pytest.importorskip("PySide6")
    pytest.importorskip("gi")
    from PySide6.QtWidgets import QApplication

    from anycam.gui_many import ManyCamWindow

    app = QApplication.instance() or QApplication([])
    project, first, second = _two_scene_project()
    window = ManyCamWindow(project)
    window.headless = True
    window._persist = False
    restarts = []
    monkeypatch.setattr(window, "restart_engine", lambda: restarts.append(1))
    try:
        window.start_engine()
        assert window.engine is not None
        engine = window.engine
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not window.preview.has_frame():
            window.engine.pump(40)
            app.processEvents()
        window.switch_scene(1)
        app.processEvents()
        assert window.engine is engine and restarts == []
        assert engine.scene is second
        window.switch_scene(0)
        app.processEvents()
        assert engine.scene is first and restarts == []
    finally:
        window.shutdown()
        window.close()
        app.processEvents()


def test_stopping_the_engine_keeps_the_window_responsive():
    pytest.importorskip("PySide6")
    pytest.importorskip("gi")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from anycam.gui_many import ManyCamWindow

    app = QApplication.instance() or QApplication([])
    project, first, second = _two_scene_project()
    window = ManyCamWindow(project)
    window.headless = True            # start without the threaded path ...
    window._persist = False
    try:
        window.start_engine()
        assert window.engine is not None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not window.preview.has_frame():
            window.engine.pump(40)
            app.processEvents()
        window.headless = False       # ... then stop through it
        ran = []
        for delay in (30, 60, 90):
            QTimer.singleShot(delay, lambda delay=delay: ran.append(delay))
        window.stop_engine()
        assert window.engine is None
        assert ran, "no Qt event ran while the engine was finalising"
    finally:
        window.headless = True
        window.shutdown()
        window.close()
        app.processEvents()


# ------------------------------------------------- camera loss and return, live
def test_a_lost_camera_becomes_a_card_and_comes_back_without_a_rebuild():
    pytest.importorskip("gi")
    from anycam.pipeline import AnyCamEngine

    project = model.Project(name="loss")
    project.output.canvas_w, project.output.canvas_h, project.output.fps = 320, 180, 15
    scene = model.Scene(model.new_id("scene"), "S")
    scene.add_layer(model.Layer("ball", "test", "Ball", source="ball", box=(0, 0, 0.5, 1)))
    scene.add_layer(model.Layer("cam", "camera", "Cam", source="/dev/video98",
                                box=(0.5, 0, 0.5, 1)))
    project.scenes, project.active_scene = [scene], scene.id
    engine = AnyCamEngine(project, preview=True).build()
    engine.start()
    try:
        end = time.monotonic() + 8
        while time.monotonic() < end and not engine.report.source_errors:
            engine.pump(50)
        assert engine.report.source_errors, "the missing device never reported an error"
        pipeline_id = id(engine.pipeline)
        pads = len(engine.compositor.sinkpads)

        assert engine.mark_layer_lost("cam") is True
        assert engine.mark_layer_lost("cam") is True          # idempotent
        before = engine._frame_count
        end = time.monotonic() + 3
        while time.monotonic() < end and engine.report.source_errors:
            engine.pump(50)
        assert engine.report.source_errors == [], "the dead capture was not released"
        assert engine.report.camera_placeholders == ["Cam"]
        assert len(engine.compositor.sinkpads) == pads
        assert id(engine.pipeline) == pipeline_id
        _run(engine, 1.0)
        assert engine._frame_count > before + 5

        assert engine.attach_capture("cam") is True
        _run(engine, 0.5)
        assert len(engine.compositor.sinkpads) == pads        # the card is gone, not piled up
        assert engine.report.camera_placeholders == []
        assert not engine.skip_layers
    finally:
        engine.stop()
