"""Animated overlays (ticker, lower third, GIF) and the two stacking filter slots."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time

import numpy as np
import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from anycam import animated, effect_thumbs  # noqa: E402
from anycam.model import Effects, Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def gif(tmp_path):
    frames = []
    for index in range(4):
        image = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle([0, 0, 59, 39], fill=(60 * index + 20, 50, 200, 255))
        frames.append(image)
    path = tmp_path / "anim.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100,
                   loop=0, disposal=2)
    return path


def diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(int) - b.astype(int)).mean())


# ----------------------------------------------------------------- generators
def test_the_ticker_scrolls_and_keeps_a_gap_between_repeats():
    ticker = animated.Ticker(400, 60, "HELLO", "#ffffff", "#000000ff")
    first, later = ticker.frame(0.0), ticker.frame(0.6)
    assert first.shape == (60, 400, 4) and diff(first, later) > 5
    # After exactly one period the picture repeats (a seamless loop).
    period_seconds = ticker._period / ticker._speed
    assert diff(ticker.frame(0.0), ticker.frame(period_seconds)) < 3


def test_the_ticker_bar_is_translucent_where_there_is_no_text():
    frame = animated.Ticker(400, 60, "HI", "#ffffff", "#10141880").frame(0.0)
    assert 100 < frame[2, 2, 3] < 160          # the bar's own alpha


def test_the_lower_third_slides_in_and_slides_in_again_on_restart():
    card = animated.LowerThird(400, 80, "Ada Lovelace", "Mathematician")
    start, mid, settled = card.frame(0.0), card.frame(0.35), card.frame(2.0)
    assert start[..., 3].max() == 0 or start[..., 3].sum() < mid[..., 3].sum()
    assert mid[..., 3].sum() < settled[..., 3].sum()
    assert settled[40, 2, 3] > 200             # the accent bar is in place
    card.restart()
    again = card.frame(5.0)                    # a restart begins a new slide
    assert again[..., 3].sum() < settled[..., 3].sum()
    assert card.frame(6.0)[..., 3].sum() > again[..., 3].sum()


def test_a_lower_third_without_a_title_still_renders():
    card = animated.LowerThird(300, 70, "Just a name")
    card.frame(0.0)                      # the first frame starts the slide
    assert card.frame(3.0)[..., 3].max() > 0


def test_the_gif_player_loops_through_its_frames_by_duration(gif):
    player = animated.GifPlayer(str(gif))
    assert player.size == (60, 40)
    seen = {int(player.frame(t)[10, 10, 0]) for t in (0.0, 0.15, 0.25, 0.35)}
    assert len(seen) == 4
    assert diff(player.frame(0.05), player.frame(0.45)) < 1     # 4 x 100 ms loop


def test_a_still_image_is_a_one_frame_animation(tmp_path):
    path = tmp_path / "still.png"
    Image.new("RGBA", (30, 30), (255, 0, 0, 255)).save(path)
    player = animated.GifPlayer(str(path))
    assert diff(player.frame(0.0), player.frame(7.3)) == 0


def test_a_broken_gif_is_reported(tmp_path):
    path = tmp_path / "bad.gif"
    path.write_bytes(b"not a gif")
    with pytest.raises(Exception):
        animated.GifPlayer(str(path))


# ------------------------------------------------------------------ in the engine
class Live:
    def __init__(self, *layers: Layer, width=640, height=360):
        self.frames: list[Image.Image] = []
        project = Project(name="ov")
        project.output = OutputConfig(canvas_w=width, canvas_h=height, fps=30)
        scene = Scene("s", "S")
        for layer in layers:
            scene.add_layer(layer)
        project.scenes, project.active_scene = [scene], "s"
        self.engine = AnyCamEngine(project, preview=True, on_frame=self._on).build()
        self.engine.start()

    def _on(self, sample):
        caps = sample.get_caps().get_structure(0)
        w, h = int(caps.get_value("width")), int(caps.get_value("height"))
        ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
        if ok:
            try:
                self.frames.append(Image.frombytes("RGBA", (w, h), bytes(info.data)[: w * h * 4]))
            finally:
                sample.get_buffer().unmap(info)

    def settle(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.engine.pump(30)

    def stop(self):
        self.engine.stop()


def backdrop(path) -> Layer:
    return Layer("bg", "test", "bg", source="smpte", box=(0, 0, 1, 1), z=1)


def region(image: Image.Image, box) -> np.ndarray:
    return np.asarray(image.crop(box).convert("RGB"))


def test_overlays_only_cover_their_own_area_and_the_ticker_moves(tmp_path):
    ticker = Layer("tick", "ticker", "Ticker", source="NEWS FLASH NEWS FLASH",
                   box=(0, 0.85, 1, 0.15), z=5, color="#ffffff", background="#aa0000ff")
    live = Live(backdrop(tmp_path), ticker)
    try:
        live.settle(0.5)
        first = live.frames[-1]
        live.settle(0.9)
        later = live.frames[-1]
        assert live.engine.report.error == ""
        # The picture above the bar is the untouched backdrop ...
        assert diff(region(first, (0, 0, 640, 200)), region(later, (0, 0, 640, 200))) < 1
        # ... the bar itself is red and its text has moved.
        bar_first, bar_later = region(first, (0, 310, 640, 360)), region(later, (0, 310, 640, 360))
        assert bar_first[..., 0].mean() > 100 > bar_first[..., 2].mean()
        assert diff(bar_first, bar_later) > 4
    finally:
        live.stop()


def test_a_lower_third_slides_in_again_each_time_it_is_shown():
    card = Layer("lt", "lower_third", "LT", source="Grace Hopper|Rear Admiral",
                 box=(0.05, 0.6, 0.6, 0.25), z=5)
    live = Live(Layer("bg", "test", "bg", source="smpte", box=(0, 0, 1, 1), z=1), card,
                width=640, height=360)
    try:
        live.settle(1.8)
        area = (10, 216, 400, 306)
        settled = region(live.frames[-1], area)
        live.engine.set_layer_visible("lt", False)
        live.settle(0.3)
        live.engine.set_layer_visible("lt", True)
        live.settle(0.2)                                   # mid-slide
        sliding = region(live.frames[-1], area)
        live.settle(1.5)
        assert diff(settled, region(live.frames[-1], area)) < 6
        assert diff(settled, sliding) > 8, "it did not slide in again"
    finally:
        live.stop()


def test_a_gif_layer_animates_on_the_canvas(gif):
    layer = Layer("gif", "gif", "G", source=str(gif), box=(0.4, 0.3, 0.2, 0.3), z=5)
    live = Live(Layer("bg", "test", "bg", source="black", box=(0, 0, 1, 1), z=1), layer)
    try:
        live.settle(0.4)
        seen = set()
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            live.engine.pump(30)
            seen.add(tuple(np.asarray(live.frames[-1].convert("RGB"))[130, 320]))
        assert len(seen) >= 3, f"the GIF showed {len(seen)} distinct colours"
    finally:
        live.stop()


def test_a_missing_gif_shows_a_card_instead_of_stopping_the_scene(tmp_path):
    layer = Layer("gif", "gif", "G", source=str(tmp_path / "gone.gif"),
                  box=(0.2, 0.2, 0.4, 0.4), z=5)
    live = Live(Layer("bg", "test", "bg", source="smpte", box=(0, 0, 1, 1), z=1), layer)
    try:
        live.settle(0.6)
        assert live.engine.report.media_errors
        assert live.engine.report.error == ""
    finally:
        live.stop()


def test_overlay_worker_threads_stop_with_the_canvas(gif):
    layer = Layer("gif", "gif", "G", source=str(gif), box=(0.4, 0.3, 0.2, 0.3), z=5)
    live = Live(Layer("bg", "test", "bg", source="smpte", box=(0, 0, 1, 1), z=1), layer)
    live.settle(0.3)
    live.stop()
    deadline = time.monotonic() + 3
    names = lambda: [t.name for t in threading.enumerate()  # noqa: E731
                     if t.name.startswith("anycam-backdrop") and t.is_alive()]
    while time.monotonic() < deadline and names():
        time.sleep(0.05)
    assert not names()


# ------------------------------------------------------------------- stacking
def test_two_filters_run_at_once_and_change_independently():
    layer = Layer("pic", "test", "Pic", source="smpte", box=(0, 0, 1, 1), z=1,
                  effects=Effects(filter="twirl", filter2="edge"))
    live = Live(layer)
    try:
        live.settle(0.5)
        chain = live.engine._layer_chains["pic"]
        one, two = chain.effects["slot"], chain.effects["slot2"]
        assert (one.key, two.key) == ("twirl", "edge")
        both = live.frames[-1]
        live.engine.set_layer_effects(
            "pic", Effects(filter="twirl", filter2="none"))
        assert two.idle.wait(3)
        live.settle(0.4)
        assert (one.key, two.key) == ("twirl", "none")
        assert diff(np.asarray(both.convert("RGB")),
                    np.asarray(live.frames[-1].convert("RGB"))) > 10
        assert live.engine.report.error == ""
    finally:
        live.stop()


def test_a_portrait_effect_and_a_distortion_can_be_combined():
    sample = effect_thumbs.default_sample()
    blur = effect_thumbs.render_thumbnail(Effects(filter="bg-blur"), sample)
    both = effect_thumbs.render_thumbnail(
        Effects(filter="bg-blur", filter2="fisheye"), sample)
    assert blur is not None and both is not None and blur.tobytes() != both.tobytes()


def test_filter_two_settings_survive_save_and_load():
    layer = Layer("pic", "test", "Pic", effects=Effects(filter2="sphere", filter2_amount=0.8))
    scene = Scene("s", "S", layers=[layer])
    loaded = Project.from_dict(Project(scenes=[scene], active_scene="s").to_dict())
    fx = loaded.scene.layers[0].effects
    assert (fx.filter2, fx.filter2_amount) == ("sphere", 0.8)


# ------------------------------------------------------------------------- GUI
@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    for name in ("CONFIG", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    from anycam.gui_many import ManyCamWindow

    scene = Scene("s", "S")
    scene.add_layer(Layer("l0", "test", "Cam", source="smpte", box=(0, 0, 1, 1), z=1))
    win = ManyCamWindow(Project(name="gui", scenes=[scene], active_scene="s"))
    win.headless = True
    win._persist = False
    win.show()
    app.processEvents()
    yield win
    win.engine = None
    win._persist = False
    win.close()
    app.processEvents()


def test_overlay_layers_can_be_added_from_the_source_menu(window, gif):
    ticker = window.add_layer("ticker", "Breaking news")
    third = window.add_layer("lower_third", "Ada|Engineer")
    anim = window.add_layer("gif", str(gif))
    assert (ticker.kind, ticker.source) == ("ticker", "Breaking news")
    assert ticker.box[3] < 0.2 and ticker.box[1] > 0.8          # a strip along the bottom
    assert (third.kind, third.name, third.source) == ("lower_third", "Ada", "Ada|Engineer")
    assert (anim.kind, anim.name) == ("gif", "anim")
    assert window.add_layer("gif", "/no/such/file.gif") is None


def test_new_effect_categories_are_browsable(window):
    sizes = {}
    for name in window.EFFECT_CATEGORIES:
        window.effect_category.setCurrentIndex(window.effect_category.findData(name))
        sizes[name] = window.effect_grid.count()
    assert sizes["Portrait"] >= 13 and sizes["Face"] >= 11 and sizes["Framing"] >= 3


def test_filter_tiles_go_to_the_chosen_slot_and_stack(window):
    layer = window.scene.layers[0]
    window.preview.select_layer(layer.id)

    def click(category, label):
        window.effect_category.setCurrentIndex(window.effect_category.findData(category))
        for row in range(window.effect_grid.count()):
            if window.effect_grid.item(row).text() == label:
                window._effect_item_clicked(window.effect_grid.item(row))
                return
        raise AssertionError(label)

    click("Distort", "Twirl")
    window.filter_slot.setCurrentIndex(1)
    click("Portrait", "Blur background")
    assert (layer.effects.filter, layer.effects.filter2) == ("twirl", "bg-blur")
    # The strength slider follows the slot being looked at.
    window._effect_sliders["filter_amount"].setValue(20)
    assert layer.effects.filter2_amount == pytest.approx(0.2)
    assert layer.effects.filter_amount == pytest.approx(0.75)   # twirl's default, untouched
    window.filter_slot.setCurrentIndex(0)
    click("Distort", "No filter")
    assert (layer.effects.filter, layer.effects.filter2) == ("none", "bg-blur")


def test_both_slots_can_change_at_the_same_moment():
    """Slot 2's swap used to wait forever on a pad slot 1 had just removed."""
    layer = Layer("pic", "test", "Pic", source="smpte", box=(0, 0, 1, 1), z=1)
    live = Live(layer)
    try:
        live.settle(0.4)
        chain = live.engine._layer_chains["pic"].effects
        one, two = chain["slot"], chain["slot2"]
        for first, second in (("pinch", "twirl"), ("sunglasses", "edge"), ("bg-blur", "bulge"),
                              ("none", "none"), ("fisheye", "crown"), ("edge", "zoom")):
            live.engine.set_layer_effects("pic", Effects(filter=first, filter2=second))
            assert one.idle.wait(3) and two.idle.wait(3), (first, second)
            live.settle(0.15)
            assert (one.key, two.key) == (first, second)
            assert live.engine.report.error == ""
    finally:
        live.stop()
