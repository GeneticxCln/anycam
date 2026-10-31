from anycam.geometry import Rect, box_pixels, clamp, crop_pixels, fit_contain, layer_geometry
from anycam.model import Layer


def test_clamp():
    assert clamp(-1) == 0
    assert clamp(2) == 1
    assert clamp(0.4) == 0.4
    assert clamp(5, 0, 3) == 3


def test_fit_contain_letterboxes_wide_source():
    rect = fit_contain(1920, 1080, 400, 400)
    assert rect == Rect(0, 87, 400, 225)


def test_fit_contain_pillarboxes_tall_source():
    rect = fit_contain(1080, 1920, 400, 400)
    assert rect == Rect(87, 0, 225, 400)


def test_crop_pixels_normalised():
    assert crop_pixels(100, 50, (0.1, 0.2, 0.1, 0.2)) == (10, 10, 10, 10)
    assert crop_pixels(1920, 1080, (0, 0, 0, 0)) == (0, 0, 0, 0)


def test_crop_pixels_never_removes_every_pixel():
    left, top, right, bottom = crop_pixels(100, 100, (1.0, 1.0, 1.0, 1.0))
    assert 100 - left - right >= 1
    assert 100 - top - bottom >= 1


def test_box_pixels_matches_compositor_minimums():
    layer = Layer("l1", "camera", "Cam", box=(0.5, 0.5, 0.0, 0.0))
    rect = box_pixels(layer, 1920, 1080)
    assert (rect.x, rect.y) == (960, 540)
    assert rect.w >= 1 and rect.h >= 1


def test_layer_geometry_centres_pip():
    layer = Layer("l1", "camera", "Cam", box=(0.5, 0.5, 0.25, 0.25))
    geo = layer_geometry(layer, 1920, 1080, 1920, 1080)
    assert geo["xpos"] == 960
    assert geo["ypos"] == 540
    assert geo["width"] == 480
    assert geo["height"] == 270
