"""The filter library, the live filter slot and the rewritten window frames."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from PIL import Image  # noqa: E402

from anycam import effects, overlay  # noqa: E402
from anycam.model import Effects, Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)
pytestmark = pytest.mark.skipif(
    Gst.ElementFactory.find("compositor") is None, reason="compositor missing")


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def project_with(layer: Layer) -> Project:
    project = Project(name="fx")
    project.output = OutputConfig(canvas_w=320, canvas_h=180, fps=30)
    scene = Scene("s", "S")
    scene.add_layer(layer)
    project.scenes, project.active_scene = [scene], "s"
    return project


class Runner:
    """A running engine that records the newest preview frame."""

    def __init__(self, layer: Layer):
        self.layer = layer
        self.frames: list[bytes] = []
        self.engine = AnyCamEngine(project_with(layer), preview=True,
                                   on_frame=self._on_frame).build()
        self.engine.start()

    def _on_frame(self, sample) -> None:
        caps = sample.get_caps().get_structure(0)
        width, height = int(caps.get_value("width")), int(caps.get_value("height"))
        ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
        if ok:
            try:
                self.frames.append(bytes(info.data)[: width * height * 4])
            finally:
                sample.get_buffer().unmap(info)

    def settle(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.engine.pump(30)

    @property
    def slot(self):
        return self.engine._layer_chains[self.layer.id].effects["slot"]

    def brightness(self) -> float:
        data = self.frames[-1]
        sample = [data[i] for i in range(0, len(data), 4 * 11)]
        return sum(sample) / len(sample)

    def stop(self) -> None:
        self.engine.stop()


def smpte() -> Layer:
    return Layer("pic", "test", "Pic", source="smpte", box=(0, 0, 1, 1), z=1)


# ------------------------------------------------------------------ registry
def test_the_library_is_large_and_every_entry_names_a_real_element():
    available = effects.available_filters()
    assert len(available) >= 25
    assert {spec.category for spec in available} == set(effects.FILTER_CATEGORIES)
    assert len(available) >= 55
    for spec in available:
        # A processor runs Python and needs no element; everything else does.
        assert spec.processor or Gst.ElementFactory.find(spec.element) is not None


def test_a_filter_with_a_missing_element_is_not_offered(monkeypatch):
    real_find = Gst.ElementFactory.find
    monkeypatch.setattr(
        Gst.ElementFactory, "find",
        lambda name: None if name == "pinch" else real_find(name))
    assert "pinch" not in [spec.key for spec in effects.available_filters()]
    assert effects.normalize_filter("pinch") == "none"


def test_unknown_filter_names_fall_back_to_none():
    assert effects.normalize_filter("") == "none"
    assert effects.normalize_filter("no-such-filter") == "none"
    assert effects.normalize_filter(" TWIRL ") == "twirl"


def test_amount_maps_onto_each_filters_own_range():
    pinch = effects.FILTERS["pinch"]
    assert effects.amount_value(pinch, 0.0) == -1.0
    assert effects.amount_value(pinch, 1.0) == 1.0
    assert effects.amount_value(pinch, 5.0) == 1.0      # clamped
    sides = effects.FILTERS["kaleidoscope"]
    assert effects.amount_value(sides, 0.0) == 2
    assert isinstance(effects.amount_value(sides, 0.5), int)


def test_every_filter_builds_a_linked_bin():
    for spec in effects.available_filters():
        bin_ = effects.build_slot_bin(spec.key, spec.default, "probe")
        assert bin_.get_static_pad("sink") is not None
        assert bin_.get_static_pad("src") is not None


def test_amount_changes_the_property_live():
    slot = effects.FilterSlot("pinch", 1.0)
    effect = slot.element.get_by_name("fx")
    assert effect.get_property("intensity") == pytest.approx(1.0)
    slot.set_amount(0.0)
    assert effect.get_property("intensity") == pytest.approx(-1.0)


# --------------------------------------------------------- live swapping
def test_every_filter_can_be_swapped_in_live_and_shows_a_picture():
    runner = Runner(smpte())
    try:
        runner.settle(0.8)
        assert runner.brightness() > 40
        for spec in effects.available_filters():
            runner.engine.set_layer_effects(
                "pic", Effects(filter=spec.key, filter_amount=spec.default))
            assert runner.slot.idle.wait(3), f"{spec.key}: swap never finished"
            runner.settle(0.35)
            assert runner.slot.key == spec.key
            assert runner.engine.report.error == "", spec.key
            # A filter that leaves transparent pixels shows as black here:
            # several effectv elements did, until the output was made opaque.
            assert runner.brightness() > 5, f"{spec.key} renders as black"
        runner.engine.set_layer_effects("pic", Effects())
        assert runner.slot.idle.wait(3)
        runner.settle(0.5)
        assert runner.slot.key == "none"
        assert runner.brightness() > 40
    finally:
        runner.stop()


def test_a_burst_of_changes_settles_on_the_last_one():
    runner = Runner(smpte())
    try:
        runner.settle(0.5)
        for key in ("twirl", "bulge", "edge", "sphere", "kaleidoscope"):
            runner.engine.set_layer_effects("pic", Effects(filter=key))
        assert runner.slot.idle.wait(5)
        runner.settle(0.3)
        assert runner.slot.key == "kaleidoscope"
        assert runner.engine.report.error == ""
    finally:
        runner.stop()


def test_a_filter_chosen_before_building_is_present_from_the_first_frame():
    layer = smpte()
    layer.effects = Effects(filter="fisheye")
    runner = Runner(layer)
    try:
        runner.settle(0.4)
        assert runner.slot.key == "fisheye"
        assert runner.slot.element.get_by_name("fx") is not None
    finally:
        runner.stop()


def test_filter_settings_survive_save_and_load():
    layer = smpte()
    layer.effects = Effects(filter="twirl", filter_amount=0.9, frame_matte="#123456")
    project = project_with(layer)
    loaded = Project.from_dict(project.to_dict())
    fx = loaded.scene.layers[0].effects
    assert (fx.filter, fx.filter_amount, fx.frame_matte) == ("twirl", 0.9, "#123456")


# -------------------------------------------------------- standby caption
def test_standby_caption_shows_only_when_the_canvas_is_empty():
    runner = Runner(smpte())
    try:
        runner.settle(0.3)

        def caption_hidden() -> bool:
            chain = runner.engine._background_chain
            label = next(e for e in chain.elements
                         if e.get_name() == "background_label")
            return bool(label.get_property("silent"))

        assert caption_hidden()                    # a source is on the canvas
        runner.engine.detach_layer("pic")
        assert not caption_hidden()                # empty: the slate says AnyCam
        runner.engine.set_layer_visible("pic", True)
    finally:
        runner.stop()


# ----------------------------------------------------------------- frames
def alpha_at(path, x, y) -> int:
    with Image.open(path) as image:
        return image.convert("RGBA").getpixel((x, y))[3]


@pytest.mark.parametrize("style", overlay.SHAPED_FRAME_STYLES)
def test_shaped_windows_cover_the_corners_and_open_the_middle(tmp_path, style):
    path = overlay.render_window_frame(320, 180, style, "#ff3366", 6,
                                       out=tmp_path / "f.png", matte="#101418")
    assert alpha_at(path, 2, 2) == 255            # corner: matted
    assert alpha_at(path, 160, 90) == 0           # middle: the picture shows


def test_circle_is_round_and_oval_fills_the_box(tmp_path):
    circle = overlay.render_window_frame(320, 180, "circle", "#ff3366", 6,
                                         out=tmp_path / "c.png")
    oval = overlay.render_window_frame(320, 180, "oval", "#ff3366", 6,
                                       out=tmp_path / "o.png")
    assert alpha_at(circle, 60, 90) == 255        # round: left side is matted
    assert alpha_at(oval, 60, 90) == 0            # oval reaches towards the sides


def test_matte_colour_is_used_outside_the_shape(tmp_path):
    path = overlay.render_window_frame(320, 180, "heart", "#ff3366", 6,
                                       out=tmp_path / "h.png", matte="#00ff00")
    with Image.open(path) as image:
        assert image.convert("RGBA").getpixel((2, 2))[:3] == (0, 255, 0)


def test_neon_has_a_soft_glow_not_just_a_line(tmp_path):
    path = overlay.render_window_frame(320, 180, "neon", "#ff3366", 6,
                                       out=tmp_path / "n.png")
    with Image.open(path) as image:
        levels = {value for value in image.convert("RGBA").getchannel("A").getdata()}
    assert len(levels) > 20


def test_vignette_darkens_edges_but_not_the_centre(tmp_path):
    path = overlay.render_window_frame(320, 180, "vignette", "#ff3366", 6,
                                       out=tmp_path / "v.png")
    assert alpha_at(path, 160, 90) < 5
    assert alpha_at(path, 2, 2) > 120


def test_every_listed_style_renders(tmp_path):
    assert len(overlay.WINDOW_FRAME_STYLES) >= 17
    for style in overlay.WINDOW_FRAME_STYLES:
        path = overlay.render_window_frame(160, 90, style, "#ff3366", 4,
                                           out=tmp_path / f"{style}.png")
        assert path.exists()


# ------------------------------------------------------ animated backdrops
import threading  # noqa: E402
import warnings  # noqa: E402

import numpy as np  # noqa: E402

from anycam import procedural, sources  # noqa: E402


@pytest.mark.parametrize("key", sorted(procedural.GENERATORS))
def test_each_backdrop_is_a_valid_moving_cheap_picture(key):
    generator = procedural.GENERATORS[key](320, 180)
    with warnings.catch_warnings():
        warnings.simplefilter("error")          # no NaN / overflow warnings
        for step in range(30):
            generator.frame(step / 30)           # let stateful ones evolve
        first = generator.frame(2.0)
        started = time.perf_counter()
        for step in range(20):
            later = generator.frame(2.0 + (step + 1) * 0.05)
        per_frame = (time.perf_counter() - started) / 20
    assert first.shape == (180, 320, 4) and first.dtype == np.uint8
    assert (first[..., 3] == 255).all()
    assert first[..., :3].mean() > 8, "the backdrop is black"
    assert np.abs(first.astype(int) - later.astype(int)).mean() > 0.3, "it is frozen"
    assert per_frame < 0.03, f"{per_frame * 1000:.1f} ms per frame is too slow"


def test_backdrop_names_saved_by_older_builds_still_resolve():
    assert procedural.generator_key("orbit") == "starfield"
    assert procedural.generator_key("pulse") == "ripples"
    assert procedural.generator_key("warp") == "plasma"
    assert procedural.generator_key("nonsense") == "tunnel"
    assert set(sources.ANIMATION_PRESETS) == set(procedural.GENERATORS)


def test_internal_size_keeps_the_boxs_aspect_so_it_fills_the_box():
    assert procedural.internal_size(1920, 1080) == (320, 180)
    assert procedural.internal_size(1080, 1920) == (320, 569)
    assert procedural.internal_size(100, 100) == (100, 100)


def test_an_animation_layer_moves_on_the_canvas_and_its_thread_stops_with_it():
    layer = Layer("anim", "animation", "Anim", source="plasma",
                  box=(0, 0, 1, 1), z=1)
    runner = Runner(layer)
    try:
        runner.settle(0.6)
        first = runner.frames[-1]
        runner.settle(0.6)
        second = runner.frames[-1]
        difference = sum(abs(a - b) for a, b in zip(first[::53], second[::53], strict=False))
        assert difference > 500, "the canvas shows a frozen backdrop"
    finally:
        runner.stop()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(
            thread.name.startswith("anycam-backdrop") and thread.is_alive()
            for thread in threading.enumerate()):
        time.sleep(0.05)
    assert not [thread for thread in threading.enumerate()
                if thread.name.startswith("anycam-backdrop") and thread.is_alive()]
