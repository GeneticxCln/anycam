"""Tests for Phase 2 performance & responsiveness improvements."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from anycam import content, effect_thumbs, effect_worker, face, masks, processors
from anycam.model import Effects


def test_yunet_preserves_native_aspect_ratio_without_upscaling():
    shapes = []

    class FakeSession:
        def run(self, _outputs, feed):
            blob = feed["input"]
            shapes.append(blob.shape)
            ph, pw = blob.shape[2], blob.shape[3]
            outs = []
            for head in ("cls", "obj", "bbox", "kps"):
                for stride in (8, 16, 32):
                    cells = (ph // stride) * (pw // stride)
                    width = 4 if head == "bbox" else 10 if head == "kps" else 1
                    outs.append(np.zeros((1, cells, width), dtype=np.float32))
            return outs

    detector = face.FaceDetector.__new__(face.FaceDetector)
    detector.input_size = None
    detector._session = FakeSession()
    detector._input_name = "input"
    detector._output_names = [f"{h}_{s}" for h in ("cls", "obj", "bbox", "kps") for s in (8, 16, 32)]

    # 384x216 -> padded to 384x224 (NOT upscaled to 640x640!)
    frame = np.zeros((216, 384, 3), dtype=np.uint8)
    assert detector.detect(frame) == []
    assert shapes == [(1, 3, 224, 384)]


def test_model_clamps_rgb_gains_to_runtime_range():
    fx = Effects.from_dict({"red_gain": 3.5, "green_gain": -1.0, "blue_gain": 1.25})
    assert (fx.red_gain, fx.green_gain, fx.blue_gain) == (2.0, 0.0, 1.25)


def test_round_corners_and_touchup_glow_invalidation():
    img = Image.new("RGBA", (64, 64), (100, 120, 140, 255))
    rc = processors.RoundCorners(0.5)
    out = rc.process(img, 0.0)
    assert out.getpixel((0, 0))[3] == 0
    assert out.getpixel((32, 32))[3] == 255

    tu = processors.TouchUp("glow", 0.2)
    tu.process(img, 1.0)
    assert tu._glow is not None
    tu.set_amount(0.9)
    assert tu._glow is None


def test_masks_opacity_lut_and_vampire_paint():
    lut = masks._opacity_lut(128)
    assert len(lut) == 256 and lut[0] == 0 and 120 <= lut[255] <= 130
    tpl = masks.base("vampire")
    assert tpl is not None and tpl.mode == "RGBA"


def test_pdf_page_sort_order_is_numeric():
    from pathlib import Path

    names = [Path("page-10.png"), Path("page-2.png"), Path("page-1.png"), Path("page-100.png")]
    ordered = [p.name for p in sorted(names, key=content._page_sort_key)]
    assert ordered == ["page-1.png", "page-2.png", "page-10.png", "page-100.png"]


def test_tile_renderer_forget_increments_generation():
    renderer = effect_thumbs.TileRenderer()
    try:
        gen0 = renderer._generation
        renderer.forget()
        assert renderer._generation == gen0 + 1
    finally:
        renderer.stop()


def test_inference_worker_round_trip():
    pytest.importorskip("onnxruntime")
    worker = effect_worker.InferenceWorker()
    try:
        sample = effect_thumbs.default_sample((320, 180))
        faces = worker.detect_faces(sample)
        assert isinstance(faces, list)
        alpha, sharpen = worker.segment_person(sample)
        assert alpha is not None
        assert alpha.ndim == 2
        assert isinstance(sharpen, bool)
    finally:
        worker.close()
