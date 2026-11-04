from __future__ import annotations

import pytest

from anycam.layouts import (
    GRID_GAP, LAYOUT_PRESETS, arrange, capture_layers, grid_boxes, layout_preset,
)
from anycam.model import Layer, Scene, new_id


def _scene(kinds: list[str], boxes: list[tuple] | None = None) -> Scene:
    scene = Scene(new_id("scene"), "Grid")
    for index, kind in enumerate(kinds):
        box = boxes[index] if boxes else (0.0, 0.0, 1.0, 1.0)
        scene.add_layer(Layer(new_id("layer"), kind, f"Layer {index}",
                              box=box, z=index + 1))
    return scene


def test_grid_boxes_without_gaps_cover_the_canvas_exactly():
    assert grid_boxes(2, 2, gap=0.0) == [
        (0.0, 0.0, 0.5, 0.5),
        (0.5, 0.0, 0.5, 0.5),
        (0.0, 0.5, 0.5, 0.5),
        (0.5, 0.5, 0.5, 0.5),
    ]


def test_grid_boxes_honour_gap_and_margin():
    left, right = grid_boxes(2, 1, gap=0.04, margin=0.1)
    assert left == pytest.approx((0.1, 0.1, 0.38, 0.8))
    assert right == pytest.approx((0.52, 0.1, 0.38, 0.8))
    assert right[0] + right[2] == pytest.approx(0.9)


def test_impossible_gaps_collapse_instead_of_overlapping():
    for gap in (0.0, 0.4, 5.0):
        boxes = grid_boxes(3, 2, gap=gap)
        assert len(boxes) == 6
        for x, y, w, h in boxes:
            assert w > 0 and h > 0
            assert 0.0 <= x and x + w <= 1.000001
            assert 0.0 <= y and y + h <= 1.000001


def test_presets_cover_one_to_four_cameras_and_picture_in_picture():
    by_key = {preset.key: preset for preset in LAYOUT_PRESETS}
    assert list(by_key) == ["full", "split-2", "three-up", "three-across",
                            "grid-2x2", "pip"]
    assert by_key["full"].cells == 1
    assert by_key["split-2"].cells == 2
    assert by_key["three-up"].cells == 3
    assert by_key["three-across"].cells == 3
    assert by_key["grid-2x2"].cells == 4
    assert by_key["pip"].cells == 2
    assert layout_preset("nope") is None
    assert layout_preset("") is None


def test_every_preset_stays_inside_the_canvas():
    for preset in LAYOUT_PRESETS:
        for x, y, w, h in preset.boxes:
            assert 0.0 <= x and x + w <= 1.000001
            assert 0.0 <= y and y + h <= 1.000001
            assert w > 0 and h > 0


def test_pip_keeps_a_full_frame_main_and_a_corner_inset():
    preset = layout_preset("pip")
    main, inset = preset.boxes
    assert main == (0.0, 0.0, 1.0, 1.0)
    assert inset[0] > 0.5 and inset[1] > 0.5
    assert inset[2] < 0.5 and inset[3] < 0.5
    assert inset[0] + inset[2] <= 1.0 and inset[1] + inset[3] <= 1.0


def test_three_up_gives_one_large_source_and_two_smaller():
    preset = layout_preset("three-up")
    large, top_right, bottom_right = preset.boxes
    assert large[2] == pytest.approx(0.494)
    assert large[3] == 1.0
    assert top_right[0] == pytest.approx(0.506)
    assert bottom_right[1] == pytest.approx(0.506)
    assert top_right[2] == bottom_right[2] == pytest.approx(0.494)


def test_arrange_tiles_capture_layers_in_stacking_order():
    scene = _scene(["logo", "camera", "camera", "text", "camera"])
    preset = layout_preset("three-up")
    arranged = arrange(scene, preset)

    assert [layer.name for layer in arranged] == ["Layer 1", "Layer 2", "Layer 4"]
    assert [layer.box for layer in arranged] == list(preset.boxes)
    # Overlays are not part of a layout.
    assert scene.layers[0].box == (0.0, 0.0, 1.0, 1.0)
    assert scene.layers[3].box == (0.0, 0.0, 1.0, 1.0)


def test_arrange_leaves_sources_beyond_the_cell_count_alone():
    scene = _scene(["camera"] * 5, boxes=[(0.6, 0.6, 0.3, 0.3)] * 5)
    arranged = arrange(scene, layout_preset("grid-2x2"))

    assert len(arranged) == 4
    assert scene.layers[4].box == (0.6, 0.6, 0.3, 0.3)


def test_capture_layers_skips_hidden_sources():
    scene = _scene(["camera", "camera"])
    scene.layers[0].visible = False

    assert [layer.name for layer in capture_layers(scene)] == ["Layer 1"]


def test_gap_default_is_a_thin_separator():
    preset = layout_preset("split-2")
    left, right = preset.boxes
    assert left[0] == 0.0
    assert right[0] == pytest.approx(GRID_GAP + left[2])
