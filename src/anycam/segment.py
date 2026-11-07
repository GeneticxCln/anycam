"""Person segmentation for background effects.

Two models:

* ``mediapipe`` (default, bundled): the MediaPipe selfie-segmentation model
  (see ``assets/models/NOTICE.md``), 256x144 in, a soft person probability out.
  ``ANYCAM_SEG_MODEL`` may name another model with the same interface
  (``pixel_values`` 1x3x144x256 in 0..1, ``alphas`` out).
* ``rvm``: Robust Video Matting (MobileNetV3), an optional download (see
  :mod:`anycam.models`).  A true alpha matte with soft edges and hair; it is
  recurrent, so every user of it keeps its own state (:class:`Segmenter`).

All sessions are created through :mod:`anycam.inference` (CPU or CUDA).  When
no model or onnxruntime is available ``person_alpha`` returns ``None`` and
callers fall back to a face-based mask.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from . import config, inference

MODEL_ENV = "ANYCAM_SEG_MODEL"
INPUT_SIZE = (256, 144)  # width, height (MediaPipe)
MODELS = ("mediapipe", "rvm")
RVM_WIDTH = 640           # RVM input width; height follows the frame's shape
RVM_DOWNSAMPLE = 0.4      # internal resolution ~256 px wide, ~10 ms on a CPU

_state: dict = {}
_lock = threading.Lock()
_chosen = "mediapipe"
_generation = 0


def model_path() -> Path:
    env = os.environ.get(MODEL_ENV)
    return Path(env) if env else (
        config.assets_dir() / "models" / "selfie_segmentation_landscape.onnx")


def chosen_model() -> str:
    return _chosen


def active_model() -> str:
    """The model in use: RVM only once it has been downloaded."""
    if _chosen == "rvm":
        from . import models

        if models.model_path("rvm") is not None:
            return "rvm"
    return "mediapipe"


def generation() -> int:
    """Changes whenever the model or the device changes (rebuild segmenters)."""
    return _generation


def set_model(name: str) -> bool:
    """Choose ``mediapipe`` or ``rvm``; True when it changed."""
    global _chosen
    name = name if name in MODELS else "mediapipe"
    if name == _chosen:
        return False
    _chosen = name
    reset()
    return True


def reset() -> None:
    """Drop the cached sessions (model or effects device changed)."""
    global _generation
    with _lock:
        _state.clear()
        _generation += 1


inference.on_change(reset)


def _session():
    """The shared MediaPipe session (stateless, so one serves everyone)."""
    with _lock:
        if "session" not in _state:
            session = None
            path = model_path()
            if path.exists():
                try:
                    session = inference.session(path)
                    _state["input"] = session.get_inputs()[0].name
                except Exception:  # noqa: BLE001 - optional feature
                    session = None
            _state["session"] = session
        return _state["session"]


def _rvm_session():
    with _lock:
        if "rvm" not in _state:
            from . import models

            session = None
            path = models.model_path("rvm")
            if path is not None:
                try:
                    session = inference.session(path)
                except Exception:  # noqa: BLE001 - optional feature
                    session = None
            _state["rvm"] = session
        return _state["rvm"]


def available() -> bool:
    if active_model() == "rvm":
        return _rvm_session() is not None or _session() is not None
    return _session() is not None


def person_alpha(rgb: np.ndarray) -> np.ndarray | None:
    """Probability (0..1) that each pixel of ``rgb`` is a person (MediaPipe).

    ``rgb`` is an ``H x W x 3`` uint8 picture of any size; the result is
    ``144 x 256`` float32, or ``None`` without a usable model.
    """
    session = _session()
    if session is None:
        return None
    picture = Image.fromarray(rgb).resize(INPUT_SIZE, Image.BILINEAR)
    batch = (np.asarray(picture, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]
    try:
        alpha = session.run(None, {_state["input"]: batch})[0]
    except Exception:  # noqa: BLE001 - a bad frame must not stop the stream
        return None
    return np.clip(alpha[0, 0], 0.0, 1.0)


# ------------------------------------------------------------ per-user segmenters
class Segmenter:
    """Turns a (reduced) frame into a person alpha.  One per effect instance."""

    name = "mediapipe"
    # The reduced picture's longer side the caller should hand over.
    sample_longest = 2 * INPUT_SIZE[0]
    # A probability that benefits from a sharper edge (MediaPipe), or a real
    # matte that should be used as it is (RVM).
    sharpen = True

    def alpha(self, picture: Image.Image) -> np.ndarray | None:
        rgb = np.asarray(picture.convert("RGB").resize(INPUT_SIZE, Image.BILINEAR))
        return person_alpha(rgb)


class RVMSegmenter(Segmenter):
    """Robust Video Matting with its recurrent state (kept per instance)."""

    name = "rvm"
    sample_longest = RVM_WIDTH
    sharpen = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recurrent: list[np.ndarray] | None = None
        self._shape: tuple[int, int] | None = None

    def alpha(self, picture: Image.Image) -> np.ndarray | None:
        session = _rvm_session()
        if session is None:
            return super().alpha(picture)
        width = RVM_WIDTH
        height = max(16, int(round(width * picture.height / max(1, picture.width) / 4)) * 4)
        rgb = picture.convert("RGB")
        if rgb.size != (width, height):
            rgb = rgb.resize((width, height), Image.BILINEAR)
        source = (np.asarray(rgb, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]
        with self._lock:
            if self._recurrent is None or self._shape != (width, height):
                self._recurrent = [np.zeros((1, 1, 1, 1), np.float32)] * 4
                self._shape = (width, height)
            feeds = {"src": source,
                     "downsample_ratio": np.array([RVM_DOWNSAMPLE], np.float32)}
            for index, state in enumerate(self._recurrent, start=1):
                feeds[f"r{index}i"] = state
            try:
                _fgr, pha, *recurrent = session.run(None, feeds)
            except Exception:  # noqa: BLE001 - a bad frame must not stop the stream
                self._recurrent = None
                return None
            self._recurrent = recurrent
        return np.clip(pha[0, 0], 0.0, 1.0)


def segmenter() -> Segmenter:
    """A segmenter for one effect, on the active model."""
    if active_model() == "rvm" and _rvm_session() is not None:
        return RVMSegmenter()
    return Segmenter()
