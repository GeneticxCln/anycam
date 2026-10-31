"""Face detection for AnyCam's face-tracked effects.

Uses YuNet (OpenCV Zoo, Apache-2.0) via ONNX Runtime. The model is bundled
at ``src/anycam/assets/models/yunet.onnx`` so the feature works offline. Detection is
deliberately decoupled from the GStreamer pipeline: callers feed it frames and
receive normalised bounding boxes, which the GUI can use to position an overlay
or spotlight. This keeps the heavy inference off the 60 fps compositor thread.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from . import config

MODEL_ENV = "ANYCAM_FACE_MODEL"
DEFAULT_MODEL = config.assets_dir() / "models" / "yunet.onnx"
_STRIDES = (8, 16, 32)


@dataclass
class Face:
    x: float          # normalised 0..1
    y: float
    w: float
    h: float
    score: float


class FaceDetector:
    def __init__(self, model_path: str | Path | None = None, input_size: tuple[int, int] = (640, 640)):
        self.input_size = input_size
        self._session = None
        self._model_path = Path(model_path or os.environ.get(MODEL_ENV) or DEFAULT_MODEL)
        if not self._model_path.exists():
            raise FileNotFoundError(f"face model not found: {self._model_path}")
        try:
            import onnxruntime as ort  # noqa: WPS433
        except ImportError as exc:
            raise RuntimeError("onnxruntime is required for face detection") from exc
        self._session = ort.InferenceSession(str(self._model_path),
                                             providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        self._output_names = [output.name for output in self._session.get_outputs()]

    @property
    def available(self) -> bool:
        return self._session is not None

    def detect(self, frame_rgb: np.ndarray, score_threshold: float = 0.6,
               nms_threshold: float = 0.4, top_k: int = 5) -> list[Face]:
        """Detect faces in an RGB frame. Returns normalised boxes.

        YuNet expects a raw BGR image (no mean subtraction or scaling) whose
        dimensions are a multiple of 32.  The bounding-box head predicts
        centre offsets and log-size relative to each feature-map cell, so the
        decoder below follows OpenCV's ``FaceDetectorYN`` exactly.
        """
        if self._session is None or frame_rgb is None or frame_rgb.size == 0:
            return []
        h, w = frame_rgb.shape[:2]
        if h < 2 or w < 2:
            return []
        iw, ih = self.input_size
        resized = _resize(frame_rgb, (iw, ih))
        rgb = _as_rgb(resized)
        bgr = np.ascontiguousarray(rgb[:, :, ::-1], dtype=np.float32)
        padded = _pad_to_divisor(bgr, 32)
        ph, pw = padded.shape[:2]
        blob = np.transpose(padded, (2, 0, 1))[None, ...]
        try:
            outs = self._session.run(None, {self._input_name: blob})
        except Exception:
            return []
        faces = _decode(outs, self._output_names, iw, ih, pw, ph, score_threshold)
        return _nms(faces, nms_threshold, top_k)


def _as_rgb(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return np.repeat(frame[:, :, None], 3, axis=2)
    return frame[:, :, :3]


def _resize(img: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    from PIL import Image

    pil = Image.fromarray(np.ascontiguousarray(img))
    pil = pil.resize(size, Image.BILINEAR)
    return np.asarray(pil)


def _pad_to_divisor(img: np.ndarray, divisor: int) -> np.ndarray:
    h, w = img.shape[:2]
    ph = ((h - 1) // divisor + 1) * divisor
    pw = ((w - 1) // divisor + 1) * divisor
    if (ph, pw) == (h, w):
        return img
    padded = np.zeros((ph, pw, img.shape[2]), dtype=img.dtype)
    padded[:h, :w] = img
    return padded


def _output(outs: list[np.ndarray], names: list[str], name: str, fallback: int) -> np.ndarray:
    if name in names:
        return outs[names.index(name)]
    return outs[fallback]


def _decode(outs: list[np.ndarray], names: list[str], width: int, height: int,
            padded_width: int, padded_height: int,
            score_threshold: float) -> list[Face]:
    """Decode YuNet's cls/obj/bbox heads into normalised face boxes."""
    results: list[Face] = []
    for index, stride in enumerate(_STRIDES):
        cls = _output(outs, names, f"cls_{stride}", index).reshape(-1)
        obj = _output(outs, names, f"obj_{stride}", index + len(_STRIDES)).reshape(-1)
        bbox = _output(outs, names, f"bbox_{stride}", index + 2 * len(_STRIDES)).reshape(-1, 4)
        # The exported model emits probabilities already; OpenCV clamps them
        # and takes the geometric mean instead of adding a sigmoid here.
        cls = np.clip(cls, 0.0, 1.0)
        obj = np.clip(obj, 0.0, 1.0)
        scores = np.sqrt(cls * obj)
        cols = padded_width // stride
        rows = padded_height // stride
        for i in np.flatnonzero(scores >= score_threshold):
            row, col = divmod(int(i), max(1, cols))
            if row >= rows:
                continue
            cell = bbox[i]
            cx = (col + float(cell[0])) * stride
            cy = (row + float(cell[1])) * stride
            bw = float(np.exp(cell[2])) * stride
            bh = float(np.exp(cell[3])) * stride
            x = (cx - bw / 2.0) / width
            y = (cy - bh / 2.0) / height
            w = bw / width
            h = bh / height
            x1, y1 = max(0.0, x), max(0.0, y)
            x2, y2 = min(1.0, x + w), min(1.0, y + h)
            if x2 - x1 <= 0.0 or y2 - y1 <= 0.0:
                continue
            results.append(Face(x=x1, y=y1, w=x2 - x1, h=y2 - y1,
                                score=float(scores[i])))
    return results


def _nms(faces: Iterable[Face], iou_threshold: float, top_k: int = 5) -> list[Face]:
    faces = sorted(faces, key=lambda f: f.score, reverse=True)
    kept: list[Face] = []
    for f in faces:
        if all(_iou(f, k) < iou_threshold for k in kept):
            kept.append(f)
            if len(kept) >= top_k:
                break
    return kept


def _iou(a: Face, b: Face) -> float:
    ax1, ay1, ax2, ay2 = a.x, a.y, a.x + a.w, a.y + a.h
    bx1, by1, bx2, by2 = b.x, b.y, b.x + b.w, b.y + b.h
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def model_available() -> bool:
    return Path(os.environ.get(MODEL_ENV) or DEFAULT_MODEL).exists()
