"""Smoke tests for the bundled YuNet face detector."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest

from anycam.face import FaceDetector, model_available


def test_model_is_bundled():
    assert model_available()


def test_detector_runs_on_synthetic_frame():
    if not model_available():
        return
    pytest.importorskip("onnxruntime", reason="optional face-detection dependency")
    detector = FaceDetector()
    frame = (np.random.rand(120, 160, 3) * 255).astype("uint8")
    faces = detector.detect(frame)
    assert isinstance(faces, list)


def test_detector_finds_a_real_face():
    pytest.importorskip("onnxruntime", reason="optional face-detection dependency")
    from PIL import Image

    frame = np.asarray(Image.open(Path(__file__).parent / "data" / "armstrong.jpg")
                       .convert("RGB"))
    faces = FaceDetector().detect(frame)
    assert faces, "the bundled model did not detect the reference portrait"
    best = max(faces, key=lambda item: item.score)
    assert best.score > 0.8
    assert 0.45 < best.x < 0.65
    assert 0.10 < best.y < 0.30
    assert best.w > 0.10 and best.h > 0.10


def test_missing_onnxruntime_is_reported():
    # The module must import even if onnxruntime is absent; availability is
    # reported via model_available()/FaceDetector construction.
    assert importlib.util.find_spec("anycam.face") is not None


def test_preprocessing_is_raw_bgr_and_decodes_yunet_heads():
    """A one-cell detection proves the BGR range and log-size decoder."""
    calls = []

    class FakeSession:
        def run(self, _outputs, feed):
            calls.append(feed["input"])
            return _yunet_outputs()

    detector = FaceDetector.__new__(FaceDetector)
    detector.input_size = (640, 640)
    detector._session = FakeSession()
    detector._input_name = "input"
    detector._output_names = _yunet_names()

    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    frame[:, :160, 0] = 255          # red on the left (RGB input)
    frame[:, 160:, 2] = 255          # blue on the right
    faces = detector.detect(frame)

    assert len(calls) == 1
    blob = calls[0]
    assert blob.shape == (1, 3, 640, 640)
    assert blob.dtype == np.float32
    # YuNet expects 0..255 BGR, not the old mean/std normalised RGB blob.
    assert float(blob.max()) > 200.0
    assert float(blob[0, 0, 0, -1]) > 200.0   # blue becomes channel 0
    assert float(blob[0, 2, 0, 0]) > 200.0    # red becomes channel 2

    assert len(faces) == 1
    found = faces[0]
    assert found.score == pytest.approx(np.sqrt(0.9 * 0.8), rel=1e-5)
    assert found.x == pytest.approx(0.45, abs=1e-5)
    assert found.y == pytest.approx(0.45, abs=1e-5)
    assert found.w == pytest.approx(0.10, abs=1e-5)
    assert found.h == pytest.approx(0.10, abs=1e-5)


def _yunet_names() -> list[str]:
    return [f"{head}_{stride}" for head in ("cls", "obj", "bbox", "kps")
            for stride in (8, 16, 32)]


def _yunet_outputs() -> list[np.ndarray]:
    outputs = []
    for head in ("cls", "obj", "bbox", "kps"):
        for stride in (8, 16, 32):
            cells = (640 // stride) ** 2
            width = 4 if head == "bbox" else 10 if head == "kps" else 1
            outputs.append(np.zeros((1, cells, width), dtype=np.float32))
    # cls_8 and obj_8 are the first and fourth tensors; bbox_8 is the seventh.
    index = 40 * 80 + 40
    outputs[0][0, index, 0] = 0.9
    outputs[3][0, index, 0] = 0.8
    outputs[6][0, index] = (0.0, 0.0, np.log(8.0), np.log(8.0))
    return outputs
