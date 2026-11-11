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
