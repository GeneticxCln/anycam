"""The unclean-exit marker, safe mode's effects, and the repair of unreadable files."""
import json
import os

import pytest

from anycam import config, crashguard, repair, safemode
from anycam.ui import startup


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    yield tmp_path
    crashguard._state.clear()


def test_a_clean_exit_leaves_no_marker_and_no_crash(state):
    assert crashguard.begin("1.0") is None
    assert crashguard.marker_path().exists()
    crashguard.end()
    assert not crashguard.marker_path().exists()
    assert crashguard.begin("1.0") is None


def test_a_leftover_marker_is_reported_with_the_stage(state):
    crashguard.begin("1.0")
    crashguard.stage("building the canvas and preview")
    crashguard._state.clear()           # the process died: no end()
    crash = crashguard.begin("1.1")
    assert crash is not None
    assert crash.stage == "building the canvas and preview" and crash.version == "1.0"
    assert "building the canvas" in crash.summary() and not crash.rebooted


def test_a_marker_from_another_boot_is_flagged_as_a_restart(state):
    path = crashguard.marker_path()
    path.write_text(json.dumps({"stage": "running", "boot_id": "another-boot", "started": 1}))
    crash = crashguard.previous_crash()
    assert crash is not None and crash.rebooted


def test_an_unreadable_marker_still_counts_as_a_crash(state):
    crashguard.marker_path().write_text("{not json")
    assert crashguard.previous_crash() is not None


def test_the_marker_is_owner_only(state):
    crashguard.begin("1.0")
    assert (crashguard.marker_path().stat().st_mode & 0o777) == 0o600


# ------------------------------------------------------------------ safe mode
@pytest.fixture
def clean_safe_mode():
    saved = {k: os.environ.get(k) for k in ("ANYCAM_CANVAS", "ANYCAM_PREVIEW")}
    yield
    safemode.disable()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_startup_turns_safe_mode_on_by_flag_without_asking(state, clean_safe_mode):
    assert startup.begin(safe_mode=True, ask=False) is True
    assert safemode.active()
    assert os.environ["ANYCAM_CANVAS"] == "cpu" and os.environ["ANYCAM_PREVIEW"] == "image"
    startup.finish()


def test_startup_after_a_crash_without_a_dialog_stays_normal(state, clean_safe_mode):
    crashguard.begin("1.0")
    crashguard._state.clear()
    assert startup.begin(safe_mode=False, ask=False) is False
    assert not safemode.active()
    startup.finish()
    assert not crashguard.marker_path().exists()


def test_ai_effects_pass_the_picture_through_in_safe_mode(clean_safe_mode):
    pytest.importorskip("gi")
    from anycam import processors

    safemode.enable()
    bin_, runner = processors.build_processor_bin("background:blur", 0.5, "t", 64, 48, 30)
    try:
        assert type(runner.processor) is processors.Processor
    finally:
        runner.processor.close()


def test_safe_mode_changes_nothing_in_the_saved_project(state, clean_safe_mode):
    from anycam import model

    project = model.default_project()
    before = json.dumps(project.to_dict(), sort_keys=True)
    safemode.enable()
    assert json.dumps(project.to_dict(), sort_keys=True) == before
    assert project.output.canvas_backend != "cpu" or model.default_project().output.canvas_backend == "cpu"


# --------------------------------------------------------------------- repair
def test_check_finds_unreadable_files_and_leaves_good_ones(state):
    config.project_path().write_text("{broken")
    (config.config_dir() / "hotkeys.json").write_text('{"record": "Ctrl+R"}')
    (config.state_dir() / "phones.json").write_text("[1, 2]")
    kinds = sorted(p.kind for p in repair.check())
    assert kinds == ["phones", "project"]


def test_repair_sets_files_aside_never_deletes_and_restores_the_backup(state):
    path = config.project_path()
    path.write_text("{broken")
    path.with_name(path.name + ".bak").write_text('{"name": "good"}')
    done = repair.repair(repair.check())
    (problem, moved), = done
    assert moved is not None and moved.read_text() == "{broken"
    assert "set-aside" in str(moved)
    assert json.loads(path.read_text())["name"] == "good"


def test_a_project_that_does_not_load_is_restored_from_its_backup(state, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(state / "videos"))
    from anycam import model

    good = model.default_project()
    good.name = "the good copy"
    path = config.project_path()
    good.save(path)
    path.with_name(path.name + ".bak").write_text(path.read_text())
    path.write_text('{"scenes": [ broken')

    from PySide6.QtWidgets import QApplication

    from anycam.gui import AnyCamWindow

    app = QApplication.instance() or QApplication([])
    window = AnyCamWindow()
    try:
        assert window.project.name == "the good copy"
        assert "Restored the last good copy" in window._load_warning
        assert list((config.config_dir() / "set-aside").glob("*/project.json"))
    finally:
        window._persist = False
        window.close()
        app.processEvents()
