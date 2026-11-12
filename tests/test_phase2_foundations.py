"""Phase 2 building blocks: camera controls, translation, accessibility."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from anycam import camera_controls, config

CTRLS = """\
User Controls

                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=140
                       contrast 0x00980901 (int)    : min=0 max=255 step=1 default=128 value=128
        white_balance_automatic 0x0098090c (bool)   : default=1 value=1
           white_balance_temperature 0x0098091a (int)    : min=2000 max=6500 step=10 default=4000 value=4000 flags=inactive
Camera Controls

                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3 (Aperture Priority Mode)
				1: Manual Mode
				3: Aperture Priority Mode
                  zoom_absolute 0x009a090d (int)    : min=100 max=400 step=5 default=100 value=100
                  firmware_ver 0x009a0999 (int)    : min=0 max=10 step=1 default=0 value=3 flags=read-only
                    reset 0x009a0998 (button) : flags=write-only, execute-on-write
"""


def test_controls_are_parsed_with_ranges_menus_and_flags():
    controls = {c.name: c for c in camera_controls.parse_controls(CTRLS)}
    brightness = controls["brightness"]
    assert (brightness.minimum, brightness.maximum, brightness.step) == (0, 255, 1)
    assert (brightness.default, brightness.value) == (128, 140)
    assert controls["white_balance_automatic"].kind == "bool"
    assert controls["white_balance_temperature"].inactive
    assert controls["auto_exposure"].menu == {1: "Manual Mode", 3: "Aperture Priority Mode"}
    assert controls["firmware_ver"].read_only
    assert brightness.label == "Brightness"


def test_listing_keeps_only_what_can_be_adjusted(monkeypatch):
    monkeypatch.setattr(config, "run", lambda cmd, timeout=5.0: (0, CTRLS))
    names = [c.name for c in camera_controls.list_controls("/dev/video0")]
    assert "firmware_ver" not in names and "reset" not in names
    assert "zoom_absolute" in names and "auto_exposure" in names
    monkeypatch.setattr(config, "run", lambda cmd, timeout=5.0: (1, "no such device"))
    assert camera_controls.list_controls("/dev/video0") == []


def test_values_are_clamped_onto_legal_steps():
    zoom = next(c for c in camera_controls.parse_controls(CTRLS) if c.name == "zoom_absolute")
    assert zoom.clamp(10) == 100 and zoom.clamp(999) == 400 and zoom.clamp(103) == 105
    menu = next(c for c in camera_controls.parse_controls(CTRLS) if c.name == "auto_exposure")
    assert menu.clamp(1) == 1 and menu.clamp(2) == menu.default
    flag = next(c for c in camera_controls.parse_controls(CTRLS)
                if c.name == "white_balance_automatic")
    assert flag.clamp(7) == 1 and flag.clamp(0) == 0


def test_setting_a_control_runs_v4l2_ctl_and_reports_failures(monkeypatch):
    calls = []

    def fake(cmd, timeout=5.0):
        calls.append(cmd)
        return (0, "") if "brightness" in cmd[-1] else (1, "VIDIOC_S_CTRL: failed\n")

    monkeypatch.setattr(config, "run", fake)
    assert camera_controls.set_control("/dev/video2", "brightness", 99) == ""
    assert calls[-1] == ["v4l2-ctl", "-d", "/dev/video2", "--set-ctrl=brightness=99"]
    assert "failed" in camera_controls.set_control("/dev/video2", "zoom_absolute", 5)
    assert "not a control name" in camera_controls.set_control("/dev/video2", "a;b", 1)
    problems = camera_controls.apply_saved("/dev/video2", {"brightness": 1, "zoom_absolute": 2})
    assert len(problems) == 1 and problems[0].startswith("zoom_absolute")


# ------------------------------------------------------------ clock and timer
def test_timer_sources_are_parsed():
    from anycam import animated

    parse = animated.parse_timer
    assert parse("stopwatch") == ("stopwatch", 0.0)
    assert parse("countdown:300") == ("countdown", 300.0)
    assert parse("countdown 5:00") == ("countdown", 300.0)
    assert parse("1:02:03") == ("countdown", 3723.0)
    assert parse("90") == ("countdown", 90.0)
    assert parse("") == ("countdown", 0.0)
    assert parse("countdown:abc") == ("countdown", 0.0)
    assert animated.format_timer(65) == "01:05" and animated.format_timer(3725) == "1:02:05"


def test_a_countdown_rounds_up_and_warns_at_the_end():
    from anycam import animated

    timer = animated.Timer(160, 48, "countdown:20")
    assert timer.reading(100.0) == ("00:20", False)           # first call starts it
    assert timer.reading(100.4) == ("00:20", False)           # 19.6 s left shows 20
    assert timer.reading(110.0) == ("00:10", True)            # warning from 10 s
    assert timer.reading(119.9)[0] == "00:01"
    assert timer.reading(125.0) == ("00:00", True)            # stays at zero
    timer.restart()
    assert timer.reading(200.0) == ("00:20", False)
    watch = animated.Timer(160, 48, "stopwatch")
    watch.reading(10.0)
    assert watch.reading(75.0) == ("01:05", False)


def test_timer_and_clock_draw_badges():
    from anycam import animated

    timer = animated.Timer(200, 50, "countdown:60", "#ffffff", "#000000ff")
    first = timer.frame(0.0)
    assert first.shape == (50, 200, 4) and first[..., 3].max() == 255
    assert timer.frame(0.2) is first                         # same second: not redrawn
    assert timer.frame(1.5) is not first                     # a new second is
    clock = animated.Clock(200, 50, "%H:%M")
    assert clock.frame(0.0).shape == (50, 200, 4)
    clock.reconfigure("not a %Q format", "#fff", "#000000ff")  # invalid -> default
    assert clock.text().count(":") == 2
    assert animated.Clock(200, 50, "").text().count(":") == 2


def test_lower_third_templates():
    from anycam import animated

    assert animated.split_lower_third("Ada|Engineer") == ("Ada", "Engineer", "Classic")
    assert animated.split_lower_third("Ada|Engineer|Bold") == ("Ada", "Engineer", "Bold")
    assert animated.split_lower_third("Ada|Engineer|Nope")[2] == "Classic"
    assert animated.join_lower_third("A|b", "T", "Bold") == "A/b|T|Bold"
    assert animated.join_lower_third("Ada", "", "Classic") == "Ada|"
    # a named template overrides the layer's colours; none keeps them
    named = animated.lower_third_style("Ada|CTO|Bold", "#111111", "#222222")
    assert named[2:] == animated.LOWER_THIRD_TEMPLATES["Bold"]
    plain = animated.lower_third_style("Ada|CTO", "#111111", "#222222")
    assert plain[2:4] == ("#111111", "#222222")
    for template in animated.LOWER_THIRD_TEMPLATES:
        card = animated.LowerThird(320, 90, "Ada", "CTO", *animated.LOWER_THIRD_TEMPLATES[template][:2],
                                   animated.LOWER_THIRD_TEMPLATES[template][2])
        assert card.frame(5.0).shape == (90, 320, 4)


def test_clock_and_timer_layers_run_on_the_canvas():
    pytest.importorskip("gi")
    import time

    from anycam import model
    from anycam.pipeline import AnyCamEngine

    project = model.Project(name="dyn")
    project.output.canvas_w, project.output.canvas_h, project.output.fps = 320, 180, 15
    scene = model.Scene(model.new_id("scene"), "S")
    scene.add_layer(model.Layer("c", "clock", "Clock", source="%H:%M:%S", box=(0.0, 0.0, 0.3, 0.1)))
    scene.add_layer(model.Layer("t", "timer", "Timer", source="countdown:30",
                                box=(0.7, 0.0, 0.3, 0.1)))
    project.scenes, project.active_scene = [scene], scene.id
    engine = AnyCamEngine(project, preview=True).build()
    engine.start()
    try:
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            engine.pump(40)
        assert {"c", "t"} <= set(engine._layer_pads)
        assert engine.update_overlay(scene.layer("c")) is True
        assert engine.update_overlay(scene.layer("t")) is True
        assert not engine.report.error
    finally:
        engine.stop()


# =============================================================== the window
@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    for name in ("CACHE", "CONFIG", "STATE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    monkeypatch.delenv("ANYCAM_LANG", raising=False)
    monkeypatch.setenv("LANG", "C")
    return tmp_path


@pytest.fixture
def window(isolated_dirs):
    pytest.importorskip("PySide6")
    pytest.importorskip("gi")
    from PySide6.QtWidgets import QApplication

    from anycam import model
    from anycam.gui_many import ManyCamWindow

    app = QApplication.instance() or QApplication([])
    project = model.Project(name="p2")
    first = model.Scene("scene_a", "Alpha")
    first.add_layer(model.Layer("l1", "test", "Ball", source="ball", box=(0, 0, 0.5, 0.5)))
    first.add_layer(model.Layer("l2", "camera", "Cam", source="/dev/video77",
                                box=(0.5, 0, 0.5, 0.5)))
    second = model.Scene("scene_b", "Beta")
    second.add_layer(model.Layer("l3", "test", "Bars", source="smpte"))
    project.scenes, project.active_scene = [first, second], first.id
    win = ManyCamWindow(project)
    win.headless = True
    win._persist = False
    yield win
    win.engine = None
    win.close()
    app.processEvents()


def test_a_locked_layer_resists_dragging_nudging_and_removal(window):
    from PySide6.QtCore import QPointF

    layer = window.scene.layer("l1")
    assert window.set_layer_locked("l1", True) is True
    assert window.set_layer_locked("l1", True) is False          # no change
    assert "locked" in window.layer_list.item(0).text() or any(
        "locked" in window.layer_list.item(i).text() for i in range(window.layer_list.count()))
    window.preview.select_layer("l1")
    before = tuple(layer.box)
    window.preview._begin_drag(layer, QPointF(10, 10), "move", "")
    assert window.preview._drag is None
    window.remove_layers(["l1"])
    assert window.scene.layer("l1") is layer
    assert "Locked layers were kept" in window.statusBar().currentMessage()
    assert tuple(layer.box) == before
    window.set_layer_locked("l1", False)
    window.remove_layers(["l1"])
    assert window.scene.layer("l1") is None


def test_layers_can_be_renamed(window):
    assert window.rename_layer("l1", "  Main   camera ") is True
    assert window.scene.layer("l1").name == "Main camera"
    assert window.rename_layer("l1", "Main camera") is False       # unchanged
    assert window.rename_layer("l1", "   ") is False               # empty
    assert window.rename_layer("nope", "x") is False
    window.undo()
    assert window.scene.layer("l1").name == "Ball"


def test_scenes_can_be_duplicated_and_reordered(window):
    original = window.project.scenes[0]
    copy = window.duplicate_scene(original.id)
    assert copy is not None and copy.name == "Alpha copy"
    assert [s.name for s in window.project.scenes] == ["Alpha", "Alpha copy", "Beta"]
    assert window.project.active_scene == copy.id
    assert {l.id for l in copy.layers}.isdisjoint({l.id for l in original.layers})
    assert [l.kind for l in copy.layers] == [l.kind for l in original.layers]
    assert window.move_scene(copy.id, 1) is True
    assert [s.name for s in window.project.scenes] == ["Alpha", "Beta", "Alpha copy"]
    assert window.move_scene(copy.id, 5) is False                   # already last
    assert window.move_scene("nope", 1) is False
    assert window.project.active_scene == copy.id                   # still the one shown


def test_projects_open_save_as_export_and_import(window, isolated_dirs):
    from anycam import config, model

    target = isolated_dirs / "work" / "show.json"
    target.parent.mkdir()
    assert window.save_project_as(target) is True
    assert model.Project.load(target).name == "p2"
    assert config.current_project_path() == target                 # reopened next launch
    window.project.name = "changed"
    window._write_project()                                        # autosave goes to show.json
    assert model.Project.load(target).name == "changed"

    other = model.Project(name="other")
    other.scenes = [model.Scene("o1", "Only")]
    other.active_scene = "o1"
    other_file = isolated_dirs / "other.json"
    other.save(other_file)
    assert window.open_project(other_file) is True
    assert window.project.name == "other" and window.project_file == other_file
    assert config.current_project_path() == other_file
    assert window.open_project(isolated_dirs / "missing.json") is False

    window.open_project(target)
    scene_file = isolated_dirs / "alpha.anycam-scene.json"
    assert window.export_scene("scene_a", scene_file) is True
    count = len(window.project.scenes)
    assert window.import_scene(scene_file) is True
    assert len(window.project.scenes) == count + 1
    imported = window.project.scenes[-1]
    assert imported.id not in ("scene_a", "scene_b") and imported.name == "Alpha"
    assert {l.id for l in imported.layers}.isdisjoint({"l1", "l2"})
    assert window.import_scene(isolated_dirs / "nothing.json") is False
    bad = isolated_dirs / "bad.json"
    bad.write_text("[1, 2")
    assert window.import_scene(bad) is False
    # going back to the default file forgets the pointer
    default = config.project_path()
    window.save_project_as(default)
    assert config.current_project_path() == default
    assert not (config.config_dir() / "last-project").exists()


def test_shortcuts_follow_the_user_choice(window, isolated_dirs):
    from anycam import hotkeys

    keys = {s.objectName(): s.key().toString() for s in window.shortcuts}
    assert keys["hotkey-record"] == "Ctrl+R" and keys["hotkey-scene-3"] == "Ctrl+3"
    clashes = window.set_hotkeys({"record": "Ctrl+Alt+R", "mute": ""})
    keys = {s.objectName(): s.key().toString() for s in window.shortcuts}
    assert keys["hotkey-record"] == "Ctrl+Alt+R" and "hotkey-mute" not in keys
    assert clashes == {}
    assert hotkeys.load_saved() == {"record": "Ctrl+Alt+R", "mute": ""}
    clashes = window.set_hotkeys({"record": "Ctrl+M"})
    assert clashes == {"ctrl+m": ["record", "mute"]} or set(clashes.get("ctrl+m", [])) == {"record"}
    window.reset_hotkeys()
    assert hotkeys.load_saved() == {}
    assert {s.objectName(): s.key().toString() for s in window.shortcuts}["hotkey-mute"] == "Ctrl+M"


def test_hotkey_map_validation(isolated_dirs):
    from anycam import hotkeys

    assert hotkeys.valid_key("") and hotkeys.valid_key("F5") and hotkeys.valid_key("Ctrl+Shift+V")
    assert not hotkeys.valid_key("Ctrl+") and not hotkeys.valid_key("two words")
    hotkeys.path().parent.mkdir(parents=True, exist_ok=True)
    hotkeys.path().write_text('{"record": "Ctrl+Q", "nonsense": "F1", "mute": "bad key", "undo": 5}')
    assert hotkeys.load_saved() == {"record": "Ctrl+Q"}
    hotkeys.path().write_text("[1,2")
    assert hotkeys.load_saved() == {}
    hotkeys.save({"record": "Ctrl+R"})                    # equals the default: nothing stored
    assert not hotkeys.path().exists()


def test_camera_controls_are_applied_remembered_and_restored(window, monkeypatch):
    from anycam import camera_controls

    controls = camera_controls.parse_controls(
        "brightness 0x00980900 (int) : min=0 max=255 step=1 default=128 value=128\n"
        "zoom_absolute 0x009a090d (int) : min=100 max=400 step=5 default=100 value=100\n"
        "white_balance_automatic 0x0098090c (bool) : default=1 value=1\n")
    sent = []
    monkeypatch.setattr(camera_controls, "set_control",
                        lambda device, name, value: sent.append((device, name, value)) or "")
    dialog = window.open_camera_controls("l2", controls=controls, show=False)
    assert dialog is not None
    _control, slider = dialog._widgets["brightness"]
    slider.setValue(200)
    assert sent[-1] == ("/dev/video77", "brightness", 200)
    assert window.scene.layer("l2").camera_controls == {"brightness": 200}
    _control, box = dialog._widgets["white_balance_automatic"]
    box.setChecked(False)
    assert window.scene.layer("l2").camera_controls["white_balance_automatic"] == 0
    dialog.reset_defaults()
    assert window.scene.layer("l2").camera_controls["brightness"] == 128
    assert window.open_camera_controls("l1", controls=[], show=False) is None   # not a camera
    # remembered values are re-applied after a start / replug (skipped when headless)
    window.headless = False
    calls = []
    monkeypatch.setattr(camera_controls, "apply_saved",
                        lambda device, saved: calls.append((device, dict(saved))) or [])
    window._apply_saved_camera_controls()
    import time as _time
    deadline = _time.monotonic() + 2
    while not calls and _time.monotonic() < deadline:
        _time.sleep(0.02)
    window.headless = True
    assert calls and calls[0][0] == "/dev/video77" and calls[0][1]["zoom_absolute"] == 100 \
        if "zoom_absolute" in calls[0][1] else calls[0][0] == "/dev/video77"


def test_overlay_dialog_edits_every_dynamic_kind(window):
    from anycam import model
    from anycam.ui.dialogs import OverlayDialog

    third = model.Layer("t1", "lower_third", "Ada", source="Ada|CTO")
    dialog = OverlayDialog(third)
    assert dialog.template.currentText() == "Classic" and dialog.colour_button.isEnabled()
    dialog.template.setCurrentText("Bold")
    assert not dialog.colour_button.isEnabled()                  # the style brings its colours
    assert dialog.values()[0] == "Ada|CTO|Bold"
    dialog.template.setCurrentText("Classic")
    assert dialog.values()[0] == "Ada|CTO"
    clock = OverlayDialog(model.Layer("c1", "clock", "Clock", source=""))
    assert clock.values()[0] == "%H:%M:%S"
    timer = OverlayDialog(model.Layer("t2", "timer", "Timer", source="05:00"))
    assert timer.values()[0] == "05:00"


def test_clock_timer_and_lower_third_layers_are_added_and_edited(window):
    clock = window.add_layer("clock")
    timer = window.add_layer("timer", source="02:30")
    third = window.add_layer("lower_third", source="Ada|CTO|Rose")
    assert (clock.kind, timer.kind, third.kind) == ("clock", "timer", "lower_third")
    assert third.name == "Ada" and clock.source == "%H:%M:%S" and timer.source == "02:30"
    assert window.edit_overlay(timer.id, ("stopwatch", "#ffffff", "#000000ff")) is True
    assert timer.source == "stopwatch"
    assert window.edit_overlay(clock.id, ("", "#fff", "#000")) is False


# ----------------------------------------------------------- language and a11y
def test_the_interface_translates_and_switches_back(window, isolated_dirs, monkeypatch):
    import json

    from anycam import config, i18n

    catalog_dir = isolated_dirs / "assets" / "i18n"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "xx.json").write_text(json.dumps(
        {"_name": "Testish", "Settings": "Einstellungen", "Project": "Projekt"}))
    monkeypatch.setattr(config, "assets_dir", lambda: isolated_dirs / "assets")
    assert i18n.available_languages() == {"en": "English", "xx": "Testish"}
    assert window.settings_button.text() == "Settings"
    assert window.set_language("xx") > 0
    assert window.settings_button.text() == "Einstellungen"
    assert window.project_button.text() == "Projekt"
    assert i18n.saved_language() == "xx"
    window.set_language("en")
    assert window.settings_button.text() == "Settings"           # the English original returns
    assert window.set_language("nope") == 0 and i18n.saved_language() == "en"


def test_every_control_has_an_accessible_name(window):
    from anycam import a11y

    window._open_settings()
    window._settings.show()
    for root in (window, window._settings):
        a11y.apply(root)
        assert a11y.unnamed(root) == [], a11y.unnamed(root)[:10]
    window._settings.hide()


def test_the_german_catalogue_translates_the_window(window, monkeypatch):
    from anycam import i18n

    assert "de" in i18n.available_languages()
    monkeypatch.setenv("ANYCAM_LANG", "de")
    assert i18n.saved_language() == "de"
    window.apply_interface_polish()
    assert window.settings_button.text() == "Einstellungen"
    assert window.project_button.text() == "Projekt"
    window._open_settings()
    # padded labels (the shortcut table) and tab titles are found too
    labels = [w.text() for w in window._settings.findChildren(type(window.status_chip))]
    assert "  Rückgängig" in labels
    tabs = [window.settings_tabs.tabText(i) for i in range(window.settings_tabs.count())]
    assert "Allgemein" in tabs and "Ausgabe" in tabs and "Diagnose" in tabs
    monkeypatch.setenv("ANYCAM_LANG", "en")
    window.apply_interface_polish()
    assert window.settings_button.text() == "Settings"
    assert "General" in [window.settings_tabs.tabText(i) for i in range(window.settings_tabs.count())]


def test_most_visible_strings_have_a_german_translation(window):
    """A scaffold, not full coverage: fail only if the catalogue drifts far behind the UI."""
    from anycam import i18n

    window._open_settings()
    strings = {s for s in i18n.collect(window) | i18n.collect(window._settings)
               if any(ch.isalpha() for ch in s) and len(s.split()) >= 1
               and not s.startswith(("Ctrl", "#", "http", "<")) and s.upper() != s}
    catalog = i18n.load("de")
    missing = sorted(s for s in strings if s not in catalog)
    assert len(missing) <= 0.30 * len(strings), missing[:40]


def test_catalogue_lookup_keeps_padding_and_mnemonics():
    from anycam import i18n

    catalog = {"Save": "Speichern", "Open…": "Öffnen…"}
    assert i18n._lookup(catalog, "  Save ") == "  Speichern "
    assert i18n._lookup(catalog, "&Save") == "Speichern"
    assert i18n._lookup(catalog, "Unknown") == "Unknown"
    assert i18n._lookup({}, "Save") == "Save"


# ------------------------------------------------------ layout and small screens
def test_the_windows_fit_a_1366_by_768_screen(window):
    hint = window.minimumSizeHint()
    assert hint.width() <= 1366 and hint.height() <= 768, (hint.width(), hint.height())
    window._open_settings()
    app_hint = window._settings.minimumSizeHint()
    assert app_hint.height() <= 768, app_hint.height()
    window.resize(1366, 768)
    window.show()
    assert window.width() <= 1366 and window.height() <= 768
    # numeric fields stay readable: wide enough for their largest value
    for spin in (window.width_spin, window.height_spin, window.fps_spin):
        metrics = spin.fontMetrics()
        assert spin.sizeHint().width() >= metrics.horizontalAdvance(str(spin.maximum()))


def test_canvas_edits_reach_the_running_preview_after_a_pause(window, monkeypatch):
    window.project.output.canvas_w, window.project.output.canvas_h = 640, 360
    restarts = []
    monkeypatch.setattr(window, "restart_engine", lambda: restarts.append(1))
    window.engine = object()                       # a preview is running
    window.width_spin.setValue(1280)
    window.height_spin.setValue(720)
    assert window._canvas_apply_timer.isActive() and restarts == []   # not on every keystroke
    window._apply_canvas_edit()                                       # what the timer calls
    assert (window.project.output.canvas_w, window.project.output.canvas_h) == (1280, 720)
    assert restarts == [1]
    window._apply_canvas_edit()                                       # nothing changed: no rebuild
    assert restarts == [1]
    window.engine = None


def test_the_first_playlist_item_does_not_stay_twice_as_long(window):
    from anycam import model

    one = model.Layer("p1", "test", "A", source="ball")
    two = model.Layer("p2", "test", "B", source="smpte")
    for layer in (one, two):
        window.scene.add_layer(layer)
    window.playlist_ids = ["p1", "p2"]
    window.playlist_index = -1
    window._apply_playlist_visibility()                  # nothing chosen yet: untouched
    assert one.visible and two.visible
    window.playlist_index = 0
    window._apply_playlist_visibility()
    assert one.visible and not two.visible
    # starting the engine puts the rotation on item 0 instead of leaving it at -1,
    # so the first timer step moves on rather than "advancing" to the same item
    window.start_engine()
    try:
        assert window.playlist_index == 0
    finally:
        window.stop_engine()


def test_a_region_of_a_source_can_be_kept(window, monkeypatch):
    restarts = []
    monkeypatch.setattr(window, "restart_engine", lambda: restarts.append(1))
    window.engine = object()
    layer = window.scene.layer("l2")
    assert window.set_layer_region("l2", 0.25, 0.1, 0.5, 0.4) is True
    left, top, right, bottom = layer.crop
    assert (left, top) == (0.25, 0.1)
    assert abs(right - 0.25) < 1e-9 and abs(bottom - 0.5) < 1e-9
    assert restarts == [1]                                   # the crop is built into the chain
    # a region cannot run past the picture or collapse to nothing
    window.set_layer_region("l2", 0.9, 0.9, 0.5, 0.5)
    assert layer.crop[0] == 0.9 and layer.crop[2] >= 0.0 and 1 - layer.crop[0] - layer.crop[2] > 0
    assert window.set_layer_region("nope", 0, 0, 1, 1) is False
    window.undo()
    assert window.select_screen_region("l2") is True and window.preview.edit_mode == "crop"
    assert window.select_screen_region("l3") is False        # not in this scene
    window.engine = None


# ---------------------------------------------------------------- the playlist
def test_playlist_timing_and_order():
    import random

    from anycam import model, playlist

    state = model.PlaylistConfig(layer_ids=["a", "b", "c"], interval=4.0, durations={"b": 10.0})
    assert playlist.item_seconds(state, "a") == 4.0 and playlist.item_seconds(state, "b") == 10.0
    state.interval = 0.1
    assert playlist.item_seconds(state, "a") == playlist.MIN_SECONDS
    ids = ["a", "b", "c"]
    assert playlist.next_index(ids, 0) == 1 and playlist.next_index(ids, 2) == 0
    assert playlist.next_index(ids, 2, loop=False) is None
    assert playlist.next_index([], 0) is None
    assert playlist.previous_index(ids, 0) == 2 and playlist.previous_index(ids, 2) == 1
    assert playlist.previous_index(ids, 0, loop=False) == 0
    rng = random.Random(3)
    seen = {playlist.next_index(ids, 1, shuffle=True, rng=rng) for _ in range(40)}
    assert seen == {0, 2}                                  # never the same item twice in a row
    assert playlist.next_index(["only"], 0, shuffle=True) == 0


def test_playlist_settings_round_trip_and_survive_bad_data():
    from anycam import model

    scene = model.Scene("s", "S")
    scene.playlist = model.PlaylistConfig(layer_ids=["a"], durations={"a": 7.5}, shuffle=True, paused=True)
    again = model.Scene.from_dict(scene.to_dict())
    assert again.playlist.durations == {"a": 7.5} and again.playlist.shuffle and again.playlist.paused
    broken = model.PlaylistConfig.from_dict(
        {"durations": {"a": -2, "b": "x", "c": True, "d": 5, "e": float("nan")}, "shuffle": "yes"})
    assert broken.durations == {"d": 5.0} and broken.shuffle is True
    assert model.PlaylistConfig.from_dict({"durations": [1, 2]}).durations == {}


def _playlist_window(window):
    from anycam import model

    for name, source in (("A", "ball"), ("B", "smpte"), ("C", "snow")):
        layer = model.Layer(f"pl_{name}", "test", name, source=source, box=(0, 0, 1, 1),
                            visible=name == "A")
        window.scene.add_layer(layer)
        window.playlist_ids.append(layer.id)
    window._store_playlist_state()
    window._load_playlist_state()
    window.playlist_index = 0
    return window


def test_playlist_items_have_their_own_time_and_can_be_stepped(window):
    window = _playlist_window(window)
    window.playlist_list.setCurrentRow(1)
    assert window.playlist_item_seconds.isEnabled()
    window.playlist_item_seconds.setValue(9.0)
    assert window.scene.playlist.durations == {"pl_B": 9.0}
    assert "9 s" in window.playlist_list.item(1).text()
    window.playlist_item_seconds.setValue(0.0)                  # back to the playlist interval
    assert window.scene.playlist.durations == {}
    assert window.playlist_step(1) is True and window.playlist_index == 1
    assert [window.scene.layer(i).visible for i in window.playlist_ids] == [False, True, False]
    assert window.playlist_step(-1) is True and window.playlist_index == 0
    assert window.playlist_step(-1) is True and window.playlist_index == 2     # wraps


def test_playlist_pause_shuffle_reorder_and_remove(window):
    import time

    window = _playlist_window(window)

    class Engine:
        shown = []

        def set_layer_visible(self, layer_id, visible):
            if visible:
                Engine.shown.append(layer_id)

    window.engine = Engine()
    window.playlist_interval.setValue(1.0)
    window.playlist_last = time.monotonic() - 5
    window.playlist_pause.setChecked(True)
    window._advance_playlist(time.monotonic())
    assert Engine.shown == [] and window.scene.playlist.paused
    window.playlist_pause.setChecked(False)
    window.playlist_last = time.monotonic() - 5
    window._advance_playlist(time.monotonic())
    assert Engine.shown == ["pl_B"]
    window.playlist_shuffle.setChecked(True)
    assert window.scene.playlist.shuffle
    window.playlist_last = time.monotonic() - 5
    window._advance_playlist(time.monotonic())
    assert Engine.shown[-1] in ("pl_A", "pl_C")
    window.playlist_shuffle.setChecked(False)
    window.engine = None

    window.playlist_list.setCurrentRow(0)
    assert window.move_playlist_item(1) is True
    assert window.playlist_ids[:2] == ["pl_B", "pl_A"]
    assert window.move_playlist_item(-5) is False
    assert window.remove_playlist_item(1) is True              # pl_A is gone from the scene too
    assert window.playlist_ids == ["pl_B", "pl_C"] and window.scene.layer("pl_A") is None
    assert window.remove_playlist_item(7) is False


def test_global_shortcut_suggestions_can_be_customised(isolated_dirs):
    from anycam import hotkeys, shortcuts

    defaults = {i: t for i, _d, t in shortcuts.SHORTCUTS}
    assert hotkeys.global_triggers(defaults) == defaults
    hotkeys.save_global({"record": "ctrl+alt+F9", "mute": defaults["mute"], "bogus": "F1"}, defaults)
    merged = hotkeys.global_triggers(defaults)
    assert merged["record"] == "CTRL+ALT+F9" and merged["mute"] == defaults["mute"]
    assert "bogus" not in merged
    hotkeys.save_global({}, defaults)
    assert not hotkeys.global_path().exists()
    hotkeys.global_path().write_text("[not json")
    assert hotkeys.global_triggers(defaults) == defaults
