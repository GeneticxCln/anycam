from pathlib import Path

from PIL import Image

from anycam import overlay
from anycam.model import Layer


def test_parse_color_variants():
    assert overlay.parse_color("#ff0000") == (255, 0, 0, 255)
    assert overlay.parse_color("#00ff0080") == (0, 255, 0, 128)
    assert overlay.parse_color("nope", (1, 2, 3, 4)) == (1, 2, 3, 4)


def test_render_text_png_is_transparent_outside_badge(tmp_path):
    out = tmp_path / "badge.png"
    overlay.render_text_png("AnyCam", 480, 110, 54, out=out)
    image = Image.open(out)
    assert image.mode == "RGBA"
    assert image.size == (480, 110)
    assert image.getpixel((2, 2))[3] == 0
    # the badge centre must contain opaque or nearly opaque pixels
    assert image.getpixel((240, 55))[3] > 200


def test_face_frame_has_transparent_centre_and_visible_border(tmp_path):
    out = tmp_path / "face.png"
    overlay.render_face_frame(style="frame", out=out)
    image = Image.open(out)
    assert image.mode == "RGBA"
    assert image.size == (320, 320)
    assert image.getpixel((160, 160))[3] == 0
    assert image.getpixel((160, 14))[3] > 0
    spotlight = tmp_path / "spotlight.png"
    overlay.render_face_frame(style="spotlight", out=spotlight)
    assert Image.open(spotlight).getpixel((160, 160))[3] > 0


def test_camera_placeholder_is_an_opaque_labeled_card(tmp_path):
    out = tmp_path / "placeholder.png"
    overlay.render_camera_placeholder("Camera 2", "/dev/video3", 640, 360, out=out)
    image = Image.open(out)
    assert image.mode == "RGBA"
    assert image.size == (640, 360)
    # The card fills its box: no transparent corner hole over the compositor.
    assert image.getpixel((2, 2))[3] == 255
    assert image.getpixel((320, 180))[3] == 255


def test_fit_rgba_preserves_aspect(tmp_path):
    source = tmp_path / "logo.png"
    Image.new("RGBA", (200, 100), (255, 0, 0, 255)).save(source)
    out = tmp_path / "fit.png"
    overlay.fit_rgba(source, 200, 200, out=out)
    image = Image.open(out)
    assert image.size == (200, 200)
    # a 2:1 logo inside a square box leaves transparent bands top and bottom
    assert image.getpixel((100, 5))[3] == 0
    assert image.getpixel((100, 100))[3] == 255


def test_text_layer_pixel_size_matches_box():
    layer = Layer("l", "text", "T", source="Hello", box=(0.5, 0.5, 0.25, 0.1), font_size=0.05)
    x, y, w, h = overlay.layer_pixels(layer, 1920, 1080)
    assert (x, y, w, h) == (960, 540, 480, 108)
