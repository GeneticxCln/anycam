"""Themed frames, the emoji pack and particle animations."""
from __future__ import annotations

import hashlib
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("gi")

from PIL import Image  # noqa: E402

from anycam import emoji, frames, overlay, particles  # noqa: E402
from anycam.model import Layer  # noqa: E402
from tests.test_overlays_and_stacking import Live, backdrop, diff, region  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


# ------------------------------------------------------------------ emoji pack
def test_every_catalogue_emoji_is_bundled_and_matches_its_pinned_hash():
    folder = emoji.emoji_dir()
    hashes = json.loads((folder / "SHA256.json").read_text())
    assert len(emoji.CATALOGUE) >= 150
    assert len(emoji.available()) == len(emoji.CATALOGUE)
    for code, name, _cat, _words in emoji.CATALOGUE:
        path = folder / f"{code}.png"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path.name], name
        picture = emoji.load(code)
        assert picture.size == (128, 128) and picture.getchannel("A").getextrema()[1] == 255


def test_emoji_search_and_lookup():
    assert any(item[1] == "Pizza" for item in emoji.search("pizza"))
    assert emoji.search("hearts", "Hearts") or emoji.search("love", "Hearts")
    assert emoji.by_name("Christmas tree") is not None
    assert emoji.load("not-a-code") is None


def test_the_emoji_pack_has_a_licence_notice():
    notice = (emoji.emoji_dir() / "NOTICE.md").read_text()
    assert "Apache" in notice and "99701e07d3d1fe1bdbcdb14964547f0fd592966f" in notice


# --------------------------------------------------------------------- frames
def test_there_are_many_themed_frames_in_several_groups():
    assert len(frames.THEMES) >= 30
    assert {"Holiday", "Nature", "Fun", "Tech", "Fancy"} <= set(frames.GROUPS)
    for key in frames.THEMES:
        assert key in overlay.WINDOW_FRAME_STYLES


@pytest.mark.parametrize("key", list(frames.THEMES))
def test_a_themed_frame_is_transparent_in_the_middle_and_painted_at_the_edge(key, tmp_path):
    path = overlay.render_window_frame(480, 270, key, "#5eead0", 6, out=tmp_path / "f.png")
    image = Image.open(path)
    assert image.size == (480, 270) and image.mode == "RGBA"
    alpha = image.getchannel("A")
    assert alpha.getpixel((240, 135)) == 0, key
    edge = np.asarray(alpha.crop((0, 0, 480, 24)))
    assert edge.mean() > 8, f"{key} draws nothing along the top edge"


def test_themed_frames_use_many_colours_not_one():
    for key in ("xmas", "halloween", "valentine", "birthday", "garden", "candy", "rainbow"):
        image = Image.open(overlay.render_window_frame(480, 270, key, "#5eead0", 6))
        pixels = np.asarray(image)[image.getchannel("A").point(lambda v: 255 if v > 200 else 0)
                                   .getbbox() and slice(None)]
        opaque = pixels[pixels[..., 3] > 200][:, :3] // 32
        assert len({tuple(p) for p in opaque}) > 25, f"{key} is nearly one colour"


def test_different_frames_are_different_pictures():
    renders = {key: np.asarray(Image.open(overlay.render_window_frame(320, 180, key, "#fff", 6)))
               for key in frames.THEMES}
    keys = list(renders)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            assert diff(renders[a], renders[b]) > 0.5, (a, b)


def test_frame_thickness_scales_a_themed_frame():
    thin = Image.open(overlay.render_window_frame(480, 270, "gold", "#fff", 2)).getchannel("A")
    thick = Image.open(overlay.render_window_frame(480, 270, "gold", "#fff", 14)).getchannel("A")
    assert np.asarray(thick).mean() > np.asarray(thin).mean() * 1.5


# ------------------------------------------------------------------ particles
def test_there_are_many_particle_animations_in_groups():
    assert len(particles.PRESETS) >= 35
    assert {"Weather", "Love", "Party", "Sparkle", "Holiday", "Fun"} <= set(particles.GROUPS)


@pytest.mark.parametrize("key", list(particles.PRESETS))
def test_every_particle_preset_draws_moving_transparent_frames(key):
    generator, size = particles.make(key, 640, 360)
    # Sparse effects (a shooting star crosses now and then) may be empty at one
    # instant, so look for a representative moment within a few seconds.
    times = [2.0 + 0.25 * k for k in range(24)]
    best = max(times, key=lambda t: int((generator.frame(t)[..., 3] > 0).sum()))
    first, later = generator.frame(best), generator.frame(best + 0.4)
    assert first.shape == (size[1], size[0], 4) and first.dtype == np.uint8
    covered = (first[..., 3] > 0).mean()
    assert 0.0005 < covered < 0.5, f"{key} covers {covered:.4f} of the frame"
    assert (first[..., 3] == 0).mean() > 0.5          # an overlay, not a backdrop
    assert diff(first, later) > 0.02, f"{key} does not move"


def test_particle_frames_depend_only_on_time():
    generator, _ = particles.make("snow", 480, 270)
    a = generator.frame(3.3)
    generator.frame(9.0)
    assert (generator.frame(3.3) == a).all()


def test_one_frame_is_cheap_even_at_full_hd():
    import time

    for key in ("snow", "confetti", "hearts", "leaves", "matrix", "fireworks"):
        generator, _ = particles.make(key, 1920, 1080)
        generator.frame(0.0)
        started = time.perf_counter()
        for step in range(8):
            generator.frame(1.0 + step / 30)
        per_frame = (time.perf_counter() - started) / 8 * 1000
        assert per_frame < 25, f"{key} takes {per_frame:.1f} ms"


def test_the_internal_size_is_capped_but_keeps_the_aspect_ratio():
    assert particles.internal_size(1920, 1080) == (960, 540)
    assert particles.internal_size(320, 180) == (320, 180)


def test_any_emoji_can_fall_and_unknown_names_fall_back():
    generator, _ = particles.make("emoji:1f355", 480, 270)
    assert generator.frame(1.0)[..., 3].max() == 255
    fallback, _ = particles.make("no-such-thing", 480, 270)
    assert fallback.frame(1.0).shape[2] == 4
    assert particles.label_for("emoji:1f355") == "Pizza rain"
    assert particles.label_for("snow") == "Snowfall"


def test_a_particle_layer_runs_in_the_engine_over_the_picture_below():
    layer = Layer("snow", "particles", "Snow", source="hearts", box=(0, 0, 1, 1), z=5)
    live = Live(backdrop(None), layer)
    try:
        live.settle(0.8)
        first = live.frames[-1]
        live.settle(0.6)
        later = live.frames[-1]
        assert live.engine.report.error == ""
        # The animation moves...
        assert diff(region(first, (0, 0, 640, 360)), region(later, (0, 0, 640, 360))) > 0.2
    finally:
        live.stop()
    plain = Live(backdrop(None))
    try:
        plain.settle(0.8)
        base = region(plain.frames[-1], (0, 0, 640, 360))
    finally:
        plain.stop()
    # ...and it sits on top of the backdrop without replacing it.
    assert diff(base, region(first, (0, 0, 640, 360))) > 0.2
    assert diff(base, region(first, (0, 0, 640, 360))) < 40


# ---------------------------------------------------------------------- masks
@pytest.fixture(scope="module")
def portrait():
    from anycam import effect_thumbs

    return effect_thumbs.default_sample((640, 360)).convert("RGBA")


@pytest.fixture(scope="module")
def face_box(portrait):
    pytest.importorskip("onnxruntime")
    from anycam import face

    found = face.shared_detector().detect(np.asarray(portrait.convert("RGB")))[0]
    width, height = portrait.size
    return (int(found.x * width), int(found.y * height),
            int((found.x + found.w) * width), int((found.y + found.h) * height))


def test_there_are_many_masks_in_groups():
    from anycam import masks

    assert len(masks.MASKS) >= 20
    assert {"Sci-fi", "Heroes", "Spooky", "Fun", "Animals"} <= set(masks.GROUPS)
    for key in ("cyber", "web-hero", "robot", "skull", "tiger"):
        assert key in masks.MASKS


def test_masks_are_registered_as_effects():
    from anycam import effects, masks

    listed = {spec.key for spec in effects.available_filters("Masks")}
    assert {f"mask-{key}" for key in masks.MASKS} <= listed
    assert "Masks" in effects.FILTER_CATEGORIES


@pytest.mark.parametrize("key", [
    "cyber", "neon-lines", "robot", "cyborg", "alien", "web-hero", "domino", "ninja", "skull",
    "sugar-skull", "zombie", "vampire", "devil", "clown", "harlequin", "pirate", "masquerade",
    "snowman", "tiger", "panda", "puppy", "fox"])
def test_a_mask_changes_the_face_and_leaves_the_background_alone(key, portrait, face_box):
    from anycam import processors

    processor = processors.make_processor(f"mask:{key}", 0.9)
    out = processor.process(portrait.copy(), 0.0)
    before, after = np.asarray(portrait.convert("RGB")), np.asarray(out.convert("RGB"))
    x0, y0, x1, y1 = face_box
    inside = np.abs(before[y0:y1, x0:x1].astype(int) - after[y0:y1, x0:x1].astype(int)).mean()
    assert inside > 8, f"{key} barely changes the face ({inside:.1f})"
    corner = np.abs(before[:60, :120].astype(int) - after[:60, :120].astype(int)).mean()
    assert corner == 0, f"{key} paints outside the head"
    processor.close()


def test_a_mask_follows_the_face_when_it_moves(portrait, face_box):
    from anycam import processors

    x0, y0, x1, y1 = face_box
    moved = Image.new("RGBA", portrait.size, (0, 0, 0, 255))
    moved.alpha_composite(portrait.crop((x0 - 20, y0 - 30, x1 + 20, y1 + 40)), (x0 - 20 + 150, y0 - 30))
    processor = processors.make_processor("mask:clown", 1.0)
    out = np.asarray(processor.process(moved.copy(), 0.0).convert("RGB")).astype(int)
    base = np.asarray(moved.convert("RGB")).astype(int)
    changed = np.abs(out - base).sum(axis=2) > 60
    cols = np.where(changed.any(axis=0))[0]
    assert cols.min() > x0 + 100, "the mask stayed where the face was"
    assert cols.max() < x1 + 190
    processor.close()


def test_a_mask_rotates_scales_and_lands_on_the_landmarks():
    """Place a mask on made-up landmarks: the warp must obey roll, scale and position."""
    import math

    from anycam import masks

    def axis_angle(canvas):
        ys, xs = np.nonzero(np.asarray(canvas.getchannel("A")) > 128)
        values, vectors = np.linalg.eigh(np.cov(np.vstack([xs, ys])))
        major = vectors[:, int(np.argmax(values))]
        return math.degrees(math.atan2(major[1], major[0])) % 180, xs.mean(), ys.mean(), len(xs)

    def put(roll, gap, centre):
        canvas = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
        r = math.radians(roll)
        ex, ey = math.cos(r), math.sin(r)                      # along the eyes
        dx, dy = -math.sin(r), math.cos(r)                     # down the face
        mid = centre
        left = (mid[0] - ex * gap / 2, mid[1] - ey * gap / 2)
        right = (mid[0] + ex * gap / 2, mid[1] + ey * gap / 2)
        mouth = (mid[0] + dx * gap * masks.MOUTH_Y, mid[1] + dy * gap * masks.MOUTH_Y)
        masks.place(canvas, "domino", 0.0, left, right, mouth)
        return axis_angle(canvas)

    level = put(0, 100, (400, 250))
    turned = put(30, 100, (400, 250))
    assert level[0] < 3 or level[0] > 177                       # a level mask is wide, not tilted
    assert 25 < turned[0] < 35, turned                          # the warp follows a 30 degree roll
    big = put(0, 200, (400, 250))
    assert 3.2 < big[3] / level[3] < 4.8                        # twice the eye gap, four times the area
    moved = put(0, 100, (550, 300))
    assert abs((moved[1] - level[1]) - 150) < 4 and abs((moved[2] - level[2]) - 50) < 4


def test_animated_masks_change_over_time_but_static_ones_do_not(portrait):
    from anycam import masks

    assert diff(np.asarray(masks.template("cyber", 0.0)), np.asarray(masks.template("cyber", 0.4))) > 0.05
    assert (np.asarray(masks.template("skull", 0.0)) == np.asarray(masks.template("skull", 0.4))).all()


def test_masks_leave_a_faceless_picture_alone():
    from anycam import processors

    blank = Image.new("RGBA", (320, 180), (30, 40, 50, 255))
    out = processors.make_processor("mask:robot", 1.0).process(blank.copy(), 0.0)
    assert (np.asarray(out) == np.asarray(blank)).all()


def test_a_mask_costs_little_once_the_face_is_tracked(portrait):
    import time

    from anycam import processors

    big = portrait.resize((1920, 1080))
    processor = processors.make_processor("mask:cyber", 0.9)
    processor.process(big.copy(), 0.0)
    started = time.perf_counter()
    for step in range(10):                  # inside the tracker's detection interval
        processor.process(big.copy(), 0.0 + step * 0.005)
    per_frame = (time.perf_counter() - started) / 10 * 1000
    assert per_frame < 40, f"{per_frame:.1f} ms"
    processor.close()


@pytest.mark.parametrize("key", ["heart-eyes", "star-eyes", "fire-eyes", "orbit-hearts",
                                 "orbit-stars", "orbit-butterflies", "orbit-bats"])
def test_emoji_face_effects_draw_on_the_face(key, portrait, face_box):
    from anycam import processors

    processor = processors.make_processor(f"sticker:{key}", 0.5)
    first = np.asarray(processor.process(portrait.copy(), 0.0).convert("RGB")).astype(int)
    later = np.asarray(processor.process(portrait.copy(), 0.45).convert("RGB")).astype(int)
    base = np.asarray(portrait.convert("RGB")).astype(int)
    assert np.abs(first - base).mean() > 0.3
    if key.startswith("orbit"):
        assert np.abs(first - later).mean() > 0.2, "orbit does not move"
    processor.close()


# ------------------------------------------------- one animation, removable
def test_the_switcher_changes_the_animation_without_changing_size():
    generator, size = particles.make("snow", 480, 270)
    switcher = particles.Switcher(generator, 480, 270)
    before = switcher.frame(2.0)
    switcher.set_source("hearts")
    after = switcher.frame(2.0)
    assert before.shape == after.shape == (size[1], size[0], 4)
    assert diff(before, after) > 0.05


def test_an_animation_can_be_swapped_in_a_running_canvas():
    layer = Layer("anim", "particles", "Snow", source="snow", box=(0, 0, 1, 1), z=5)
    live = Live(backdrop(None), layer)
    try:
        live.settle(0.6)
        controller = live.engine._layer_chains["anim"].control
        pads_before = set(live.engine._layer_pads)
        layer.source = "matrix"
        assert live.engine.update_overlay(layer) is True
        live.settle(0.6)
        assert controller.generator.__class__.__name__ == "MatrixRain"
        assert set(live.engine._layer_pads) == pads_before          # no rebuild, no new layer
        assert live.engine.report.error == ""
    finally:
        live.stop()


@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    for name in ("CACHE", "CONFIG", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    from tests.test_effects_browser import close, make_window

    app = QApplication.instance() or QApplication([])
    win = make_window(app)
    yield win, app
    close(win, app)


def _click(window, app, category, label):
    box = window.effect_category
    box.setCurrentIndex(box.findData(category))
    app.processEvents()
    for row in range(window.effect_grid.count()):
        item = window.effect_grid.item(row)
        if item.text() == label:
            window._effect_item_clicked(item)
            app.processEvents()
            return
    raise AssertionError(f"no tile {label!r} in {category}")


def _animations(window):
    return [layer for layer in window.scene.layers if layer.kind == "particles"]


def test_clicking_animations_never_stacks_them_and_they_can_be_cleared(window):
    win, app = window
    _click(win, app, "Animations", "Snowfall")
    assert [layer.source for layer in _animations(win)] == ["snow"]
    first_id = _animations(win)[0].id
    for _ in range(3):                                   # the same tile again toggles it off/on
        _click(win, app, "Animations", "Snowfall")
    assert len(_animations(win)) <= 1
    _click(win, app, "Animations", "Rising hearts")
    _click(win, app, "Animations", "Fireworks")
    _click(win, app, "Animations", "Confetti")
    layers = _animations(win)
    assert len(layers) == 1 and layers[0].source == "confetti"
    assert layers[0].name == "Confetti"
    _click(win, app, "Animations", "No animation")
    assert _animations(win) == []
    del first_id


def test_picking_another_animation_reuses_the_layer(window):
    win, app = window
    _click(win, app, "Animations", "Rising hearts")
    layer = _animations(win)[0]
    _click(win, app, "Animations", "Fireworks")
    assert _animations(win)[0] is layer and layer.source == "fireworks"
    _click(win, app, "Animations", "Fireworks")          # the active one again clears it
    assert _animations(win) == []


def test_emoji_rain_is_one_animation_too_and_shares_the_slot(window):
    win, app = window
    _click(win, app, "Emoji rain", "Pizza")
    _click(win, app, "Animations", "Snowfall")
    _click(win, app, "Emoji rain", "Cake")
    layers = _animations(win)
    assert len(layers) == 1 and layers[0].source.startswith("emoji:")
    _click(win, app, "Emoji rain", "No animation")
    assert _animations(win) == []


def test_the_active_animation_tile_is_highlighted(window):
    win, app = window
    _click(win, app, "Animations", "Rising hearts")
    row = win.effect_grid.currentRow()
    assert win.effect_grid.item(row).text() == "Rising hearts"


def _feeder_threads():
    import threading

    return [t for t in threading.enumerate() if t.name.startswith("anycam-backdrop")
            and t.is_alive()]


def test_removing_an_animation_stops_its_feeder_thread_and_the_canvas_stays_live():
    import time

    baseline = len(_feeder_threads())
    live = Live(backdrop(None))
    try:
        live.settle(0.4)
        for round_ in range(4):                               # add and clear again and again
            layer = Layer(f"a{round_}", "particles", "Anim", source="snow", box=(0, 0, 1, 1),
                          z=5 + round_)
            live.engine.scene.add_layer(layer)
            assert live.engine.add_layer(layer)
            live.settle(0.3)
            assert len(_feeder_threads()) == baseline + 1
            live.engine.detach_layer(layer.id)
            live.engine.scene.remove_layer(layer.id)
            deadline = time.monotonic() + 3
            while len(_feeder_threads()) > baseline and time.monotonic() < deadline:
                live.settle(0.05)
            assert len(_feeder_threads()) == baseline, "a removed animation is still running"
        count = len(live.frames)
        live.settle(1.0)
        assert len(live.frames) - count >= 15, "the canvas stopped producing frames"
        assert live.engine.report.error == ""
    finally:
        live.stop()


def _preview_pixels(win):
    image = win.preview.image.convertToFormat(win.preview.image.Format.Format_RGB888)
    raw = np.frombuffer(image.constBits(), dtype=np.uint8)
    return raw.reshape(image.height(), image.bytesPerLine())[:, :image.width() * 3].astype(int)


def _motion(win, app):
    from tests.test_effects_browser import pump

    before = _preview_pixels(win)
    pump(app, 0.4)
    return float(np.abs(before - _preview_pixels(win)).mean())


def test_animations_keep_working_after_one_is_cleared_in_the_running_app(window):
    """Regression: clearing an animation froze the canvas, so nothing added later showed."""
    from tests.test_effects_browser import pump

    win, app = window
    win.start_engine()
    pump(app, 1.5)
    baseline = _motion(win, app)                          # the test pattern itself moves a little
    _click(win, app, "Emoji rain", "Pizza")
    pump(app, 0.8)
    assert _motion(win, app) > baseline + 1.0, "the first animation is not visible"
    _click(win, app, "Emoji rain", "No animation")
    pump(app, 0.8)
    assert _animations(win) == []
    for category, label in (("Emoji rain", "Cake"), ("Animations", "Rising hearts"),
                            ("Animations", "Confetti")):
        _click(win, app, category, label)
        pump(app, 0.8)
        assert _motion(win, app) > baseline + 1.0, f"{label} is not visible after a clear"
    _click(win, app, "Animations", "Confetti")            # toggle off ...
    _click(win, app, "Animations", "Snowfall")            # ... and a new one still shows
    pump(app, 0.8)
    assert _motion(win, app) > baseline + 1.0
    assert win.engine.report.error == ""
    win.stop_engine()


# ----------------------------- effects work without selecting a layer first
def _main_layer(win):
    return next(layer for layer in win.scene.layers if layer.kind == "test")


def _deselect(win, app):
    win.preview.set_selection([], None)
    app.processEvents()
    assert win.preview.selected_layer() is None


def test_a_frame_applies_to_the_main_picture_when_nothing_is_selected(window):
    win, app = window
    _deselect(win, app)
    _click(win, app, "Tech frames", "Cyber HUD")
    assert _main_layer(win).effects.frame_style == "cyber-hud"
    _click(win, app, "Tech frames", "RGB gaming")
    assert _main_layer(win).effects.frame_style == "gaming"
    assert "Cam 0" in win.browse_target.text()


def test_every_group_of_frames_works_straight_after_opening_the_app(window):
    win, app = window
    _deselect(win, app)
    for category, label, style in (("Holiday frames", "Christmas", "xmas"),
                                   ("Nature frames", "Ocean", "ocean"),
                                   ("Fun frames", "Comic book", "comic"),
                                   ("Fancy frames", "Gold frame", "gold"),
                                   ("Frames", "Neon glow", "neon"),
                                   ("Shapes", "Heart window", "heart")):
        _click(win, app, category, label)
        assert _main_layer(win).effects.frame_style == style, category


def test_adding_an_animation_does_not_steal_the_selection_from_the_picture(window):
    win, app = window
    main = _main_layer(win)
    win.preview.select_layer(main.id)
    _click(win, app, "Animations", "Rising hearts")
    assert win.preview.selected_layer() is main
    _click(win, app, "Holiday frames", "Christmas")
    assert main.effects.frame_style == "xmas"
    assert all(layer.effects.frame_style == "none" for layer in _animations(win))


def test_effects_skip_a_selected_animation_layer_and_go_to_the_picture(window):
    win, app = window
    _click(win, app, "Animations", "Snowfall")
    win.preview.select_layer(_animations(win)[0].id)      # even if the user selects it
    _click(win, app, "Tech frames", "Cyber HUD")
    assert _main_layer(win).effects.frame_style == "cyber-hud"
    assert _animations(win)[0].effects.frame_style == "none"


def test_a_selected_non_animation_layer_still_gets_the_effect(window):
    win, app = window
    extra = Layer("extra", "image", "Logo", source="", box=(0.1, 0.1, 0.2, 0.2), z=9)
    win.scene.add_layer(extra)
    win._refresh_layers()
    win.preview.select_layer("extra")
    _click(win, app, "Tech frames", "Cyber HUD")
    assert extra.effects.frame_style == "cyber-hud"
    assert _main_layer(win).effects.frame_style == "none"


def test_a_tech_frame_shows_in_the_running_app_without_selecting_anything(window):
    from tests.test_effects_browser import pump

    win, app = window
    win.start_engine()
    pump(app, 1.5)
    _deselect(win, app)
    before = _preview_pixels(win)
    _click(win, app, "Tech frames", "RGB gaming")
    pump(app, 1.0)
    after = _preview_pixels(win)
    h, w = before.shape[0], before.shape[1] // 3
    edge = np.abs(before - after)[: h // 12].mean()
    centre = np.abs(before - after)[h // 3: 2 * h // 3, w // 2: w].mean()
    assert edge > 8, f"no frame appeared at the edge ({edge:.1f})"
    assert edge > centre * 2
    win.stop_engine()


# ------------------------------------- generated sources never share a name
def test_generated_source_names_are_unique_not_derived_from_object_addresses():
    """Regression: ``id(chain) & 0xFFFF`` was reused, so two appsrc elements got the
    same name and ``Bin.add`` refused the second ("could not add anim_particles_c940")."""
    import inspect

    from anycam import sources

    numbers = [sources._next_source_number() for _ in range(5000)]
    assert len(set(numbers)) == 5000 and numbers == sorted(numbers)
    for builder in (sources.animated_chain, sources.animation_chain):
        assert "id(chain)" not in inspect.getsource(builder), builder.__name__


def test_two_generated_layers_built_back_to_back_get_different_element_names():
    from anycam import sources

    layer = Layer("x", "particles", "P", source="snow", box=(0, 0, 1, 1), z=2)
    first = sources.animated_chain(layer, 320, 180, 30)
    second = sources.animated_chain(layer, 320, 180, 30)
    try:
        names = [chain.elements[0].get_name() for chain in (first, second)]
        assert names[0] != names[1], names
    finally:
        for chain in (first, second):
            for hook in chain.cleanup:
                hook()


# ------------------------- a still image can be the picture; live sources win
def test_effects_go_to_a_still_image_when_it_is_the_only_picture(window):
    win, app = window
    win.scene.layers.clear()
    still = Layer("still", "image", "Portrait", source="", box=(0, 0, 1, 1), z=1)
    win.scene.add_layer(still)
    win._refresh_layers()
    _deselect(win, app)
    _click(win, app, "Tech frames", "Cyber HUD")
    assert still.effects.frame_style == "cyber-hud"
    assert "Portrait" in win.browse_target.text()


def test_a_live_source_beats_a_logo_image_on_top_of_it(window):
    win, app = window
    logo = Layer("logo", "image", "Logo", source="", box=(0.8, 0.05, 0.15, 0.15), z=9)
    win.scene.add_layer(logo)
    win._refresh_layers()
    _deselect(win, app)
    _click(win, app, "Tech frames", "Cyber HUD")
    assert _main_layer(win).effects.frame_style == "cyber-hud"
    assert logo.effects.frame_style == "none"
