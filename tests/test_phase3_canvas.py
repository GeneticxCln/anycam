"""Phase 3: fit modes, flip/rotate, zoom/pan and the pixel-level checks."""
from __future__ import annotations

import gi
import numpy as np
import pytest

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam import geometry, sources  # noqa: E402
from anycam.model import Layer  # noqa: E402

Gst.init(None)


def layer(**kwargs) -> Layer:
    return Layer("l1", "test", "t", **kwargs)


def asymmetric(width: int = 8, height: int = 4) -> np.ndarray:
    """RGBA picture whose every pixel is unique, so any move shows."""
    picture = np.zeros((height, width, 4), dtype=np.uint8)
    picture[..., 0] = np.arange(width, dtype=np.uint8)[None, :] * 20
    picture[..., 1] = np.arange(height, dtype=np.uint8)[:, None] * 40
    picture[..., 2] = 77
    picture[..., 3] = 255
    return picture


def run_chain(picture: np.ndarray, target: Layer, box: tuple[int, int]) -> np.ndarray:
    """Push ``picture`` through the real fit stage of ``target``; return the frame."""
    height, width = picture.shape[:2]
    chain = sources.SourceChain()
    fitted = sources._fit_letterbox(chain, width, height, box[0], box[1],
                                    rate=False, layer=target)
    pipeline = Gst.Pipeline.new("t")
    src = Gst.ElementFactory.make("appsrc", "src")
    src.set_property("caps", Gst.Caps.from_string(
        f"video/x-raw,format=RGBA,width={width},height={height},framerate=1/1"))
    src.set_property("format", Gst.Format.TIME)
    sink = Gst.ElementFactory.make("appsink", "sink")
    sink.set_property("sync", False)
    sink.set_property("max-buffers", 1)
    elements = [src, *chain.elements, sink]
    for element in elements:
        pipeline.add(element)
    for first, second in zip(elements, elements[1:], strict=False):
        assert first.link(second)
    pipeline.set_state(Gst.State.PLAYING)
    buffer = Gst.Buffer.new_wrapped(picture.tobytes())
    buffer.pts = 0
    src.emit("push-buffer", buffer)
    src.emit("end-of-stream")
    sample = sink.emit("try-pull-sample", 5 * Gst.SECOND)
    assert sample is not None, "no frame came out of the fit stage"
    caps = sample.get_caps().get_structure(0)
    out_w, out_h = caps.get_value("width"), caps.get_value("height")
    assert (out_w, out_h) == (fitted.w, fitted.h)
    ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
    assert ok
    frame = np.frombuffer(info.data, dtype=np.uint8)[: out_w * out_h * 4]
    frame = frame.reshape(out_h, out_w, 4).copy()
    sample.get_buffer().unmap(info)
    pipeline.set_state(Gst.State.NULL)
    return frame


# --------------------------------------------------------------- pure geometry
def test_every_flip_rotate_combination_has_a_videoflip_direction():
    seen = {geometry.flip_direction(h, v, r)
            for h in (False, True) for v in (False, True) for r in geometry.ROTATIONS}
    assert seen == {"identity", "90r", "180", "90l", "horiz", "vert", "ul-lr", "ur-ll"}


def test_rotation_snaps_to_quarter_turns():
    assert geometry.rotation_degrees(90) == 90
    assert geometry.rotation_degrees(-90) == 270
    assert geometry.rotation_degrees(450) == 90
    assert geometry.rotation_degrees(44) == 0
    assert geometry.rotation_degrees(46) == 90
    assert geometry.rotation_degrees("junk") == 0
    assert geometry.rotated_size(1920, 1080, 90) == (1080, 1920)
    assert geometry.rotated_size(1920, 1080, 180) == (1920, 1080)


def test_cover_window_fills_the_box_aspect():
    left, top, right, bottom = geometry.framing_margins(1920, 1080, 500, 500, "cover")
    assert (top, bottom) == (0, 0)
    assert 1920 - left - right == 1080          # a square window of the full height
    left, top, right, bottom = geometry.framing_margins(1080, 1920, 500, 500, "cover")
    assert (left, right) == (0, 0)
    assert 1920 - top - bottom == 1080


def test_zoom_window_follows_the_pan_and_stays_inside():
    corner = geometry.framing_margins(1000, 600, 1000, 600, "contain", 2.0, (0.0, 0.0))
    assert corner == (0, 0, 500, 300)
    far = geometry.framing_margins(1000, 600, 1000, 600, "contain", 2.0, (1.0, 1.0))
    assert far == (500, 300, 0, 0)
    centre = geometry.framing_margins(1000, 600, 1000, 600, "contain", 2.0, (0.5, 0.5))
    assert centre == (250, 150, 250, 150)
    # An absurd zoom and pan still leave a picture.
    left, top, right, bottom = geometry.framing_margins(
        640, 360, 640, 360, "cover", 1e9, (5.0, -3.0))
    assert 640 - left - right >= 2 and 360 - top - bottom >= 2


def test_the_box_may_leave_the_canvas():
    box = geometry.box_pixels(layer(box=(-0.25, 0.5, 1.5, 1.0)), 1000, 500)
    assert (box.x, box.y, box.w, box.h) == (-250, 250, 1500, 500)
    far = geometry.box_pixels(layer(box=(-50.0, 90.0, 99.0, 0.0)), 1000, 500)
    assert far.x == -2000 and far.y == 1500 and far.w == 4000 and far.h == 1


def test_layer_transform_fields_survive_a_round_trip_and_bad_data():
    original = Layer("a", "test", "a", fit="cover", flip_h=True, rotation=90,
                     zoom=2.5, pan=(0.25, 0.75))
    again = Layer.from_dict(original.to_dict())
    assert (again.fit, again.flip_h, again.rotation, again.zoom, again.pan) == (
        "cover", True, 90, 2.5, (0.25, 0.75))
    broken = Layer.from_dict({"kind": "test", "fit": "banana", "rotation": 77,
                              "zoom": "x", "pan": ["a", None]})
    assert broken.fit == "contain" and broken.rotation == 90
    assert broken.zoom == 1.0 and broken.pan == (0.5, 0.5)
    huge = Layer.from_dict({"kind": "test", "zoom": 1e9, "pan": [9, -9]})
    assert huge.zoom == geometry.MAX_ZOOM and huge.pan == (1.0, 0.0)


# ------------------------------------------------------ real pixels, real chain
@pytest.mark.parametrize("flip_h,flip_v,rotation", [
    (h, v, r) for h in (False, True) for v in (False, True) for r in geometry.ROTATIONS])
def test_flip_and_rotation_match_the_numpy_reference(flip_h, flip_v, rotation):
    picture = asymmetric()
    expected = picture
    if flip_h:
        expected = expected[:, ::-1]
    if flip_v:
        expected = expected[::-1]
    expected = np.rot90(expected, -(rotation // 90))
    frame = run_chain(picture, layer(flip_h=flip_h, flip_v=flip_v, rotation=rotation),
                      (expected.shape[1], expected.shape[0]))
    assert frame.shape == expected.shape
    assert (frame == expected).all()


def test_stretch_ignores_the_aspect_and_fills_the_box():
    picture = np.zeros((4, 8, 4), dtype=np.uint8)
    picture[..., 3] = 255
    picture[:, :4, 0] = 255         # left half red
    frame = run_chain(picture, layer(fit="stretch"), (6, 6))
    assert frame.shape == (6, 6, 4)
    assert frame[:, :3, 0].min() == 255 and frame[:, 3:, 0].max() == 0


def test_cover_fills_the_box_and_cuts_the_overflow():
    picture = np.zeros((4, 12, 4), dtype=np.uint8)
    picture[..., 3] = 255
    picture[:, :4, 0] = 255         # left third red
    picture[:, 4:8, 1] = 255        # middle third green
    picture[:, 8:, 2] = 255         # right third blue
    frame = run_chain(picture, layer(fit="cover"), (4, 4))
    assert frame.shape == (4, 4, 4)
    # The 4x4 window is the middle: all green, none of the edges.
    assert frame[..., 1].min() == 255 and frame[..., 0].max() == 0 and frame[..., 2].max() == 0


def test_contain_still_letterboxes_with_a_transform():
    picture = asymmetric(8, 4)
    frame = run_chain(picture, layer(flip_h=True), (8, 8))
    assert frame.shape == (4, 8, 4)         # the picture keeps its shape; the pad centres it


def test_zoom_shows_the_panned_quarter():
    picture = np.zeros((4, 8, 4), dtype=np.uint8)
    picture[..., 3] = 255
    picture[:2, :4, 0] = 255        # top-left quarter red
    picture[2:, 4:, 2] = 255        # bottom-right quarter blue
    top_left = run_chain(picture, layer(zoom=2.0, pan=(0.0, 0.0)), (4, 2))
    assert top_left[..., 0].min() == 255 and top_left[..., 2].max() == 0
    bottom_right = run_chain(picture, layer(zoom=2.0, pan=(1.0, 1.0)), (4, 2))
    assert bottom_right[..., 2].min() == 255 and bottom_right[..., 0].max() == 0


# -------------------------------------------------------- whole-canvas pixels
import time  # noqa: E402

from anycam.model import Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

CANVAS = (320, 180)


def scene_project(*layers: Layer) -> Project:
    project = Project(name="phase3")
    project.output.canvas_w, project.output.canvas_h = CANVAS
    project.output.fps = 30
    scene = Scene("scene_p3", "P3")
    for item in layers:
        scene.add_layer(item)
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


class Shot:
    """Run an engine and keep its newest canvas frame as an RGBA array."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.pixels: np.ndarray | None = None
        self.engine = AnyCamEngine(project, preview=True, on_frame=self._on_frame).build()

    def _on_frame(self, sample) -> None:
        caps = sample.get_caps().get_structure(0)
        width, height = caps.get_value("width"), caps.get_value("height")
        ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
        if not ok:
            return
        try:
            self.pixels = np.frombuffer(info.data, dtype=np.uint8)[
                : width * height * 4].reshape(height, width, 4).copy()
        finally:
            sample.get_buffer().unmap(info)

    def settle(self, seconds: float = 1.2) -> np.ndarray:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.engine.pump(40)
        assert self.pixels is not None, "the canvas produced no frame"
        return self.pixels

    def __enter__(self) -> "Shot":
        self.engine.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.engine.stop()


def colour_at(pixels: np.ndarray, fx: float, fy: float) -> tuple[int, int, int]:
    height, width = pixels.shape[:2]
    return tuple(int(v) for v in pixels[int(fy * height), int(fx * width)][:3])


def is_red(rgb) -> bool:
    return rgb[0] > 200 and rgb[1] < 60 and rgb[2] < 60


def is_dark(rgb) -> bool:
    return max(rgb) < 90


def test_a_box_hanging_off_the_canvas_is_clipped_not_refused():
    red = Layer("red", "test", "Red", source="red", box=(-0.5, 0.0, 1.0, 1.0), z=1)
    with Shot(scene_project(red)) as shot:
        pixels = shot.settle()
    assert is_red(colour_at(pixels, 0.1, 0.5))      # on the canvas
    assert not is_red(colour_at(pixels, 0.8, 0.5))  # past the box's right edge


def test_a_box_larger_than_the_canvas_fills_it():
    red = Layer("red", "test", "Red", source="red", box=(-0.5, -0.5, 2.0, 2.0), z=1)
    with Shot(scene_project(red)) as shot:
        pixels = shot.settle()
    for fx, fy in ((0.02, 0.02), (0.98, 0.02), (0.5, 0.5), (0.02, 0.98), (0.98, 0.98)):
        assert is_red(colour_at(pixels, fx, fy)), (fx, fy)


def test_cover_layers_fill_their_pad_and_contain_layers_centre_in_it():
    cover = Layer("c", "test", "Cover", source="red", box=(0, 0, 0.5, 1.0), z=1, fit="cover")
    contain = Layer("n", "test", "Contain", source="blue", box=(0.5, 0, 0.5, 1.0), z=2)
    with Shot(scene_project(cover, contain)) as shot:
        engine = shot.engine
        shot.settle()
        assert engine._layer_pads["c"].get_property("sizing-policy").value_nick == "none"
        assert engine._layer_pads["n"].get_property("sizing-policy").value_nick == \
            "keep-aspect-ratio"


def test_live_zoom_and_box_changes_need_no_rebuild():
    layer_zoom = Layer("z", "test", "Z", source="smpte", box=(0, 0, 1, 1), z=1,
                       zoom=1.5, pan=(0.5, 0.5))
    plain = Layer("p", "test", "P", source="smpte", box=(0, 0, 1, 1), z=2)
    with Shot(scene_project(layer_zoom, plain)) as shot:
        shot.settle(0.6)
        engine = shot.engine
        layer_zoom.zoom = 3.0
        layer_zoom.pan = (0.0, 0.0)
        assert engine.set_layer_framing("z") is True
        assert engine.set_layer_framing("p") is False   # built without a framing stage
        engine.set_layer_box("z", (0.0, 0.0, 0.5, 0.5))
        shot.settle(0.6)
        framing = engine._layer_chains["z"].effects["framing"]
        assert framing.margins is not None and framing.margins[0] == 0


# ----------------------------------------------------------------- RGB levels
def test_rgb_gains_are_clamped_and_reach_the_shader_uniforms():
    from anycam.model import Effects

    assert sources.rgb_gains(Effects()) == (1.0, 1.0, 1.0)
    assert sources.rgb_gains(Effects(red_gain=9.0, green_gain=-1.0, blue_gain=0.5)) == (2.0, 0.0, 0.5)
    assert "red_gain" in sources.BALANCE_SHADER and "blue_gain" in sources.BALANCE_SHADER
    if Gst.ElementFactory.find("glshader") is None:
        pytest.skip("glshader unavailable")
    shader = Gst.ElementFactory.make("glshader", None)
    sources._set_balance_uniforms(shader, 0.0, 1.0, 1.0, 0.0, (1.5, 1.0, 0.25))
    uniforms = shader.get_property("uniforms")
    assert abs(uniforms.get_value("red_gain") - 1.5) < 1e-6
    assert abs(uniforms.get_value("blue_gain") - 0.25) < 1e-6


def test_effects_levels_round_trip():
    from anycam.model import Effects

    fx = Effects.from_dict({"red_gain": 1.25, "blue_gain": "x"})
    assert fx.red_gain == 1.25 and fx.blue_gain == 1.0
