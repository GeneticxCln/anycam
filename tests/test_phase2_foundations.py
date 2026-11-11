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
