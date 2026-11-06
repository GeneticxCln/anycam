"""Person segmentation for background effects.

Uses the bundled MediaPipe selfie-segmentation model (see
``assets/models/NOTICE.md``) through onnxruntime.  ``ANYCAM_SEG_MODEL`` may name
another model with the same interface (``pixel_values`` 1x3x144x256 in 0..1,
``alphas`` out).  When no model or onnxruntime is available ``person_alpha``
returns ``None`` and callers fall back to a face-based mask.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np

from . import config

MODEL_ENV = "ANYCAM_SEG_MODEL"
INPUT_SIZE = (256, 144)  # width, height

_state: dict = {}
_lock = threading.Lock()


def model_path() -> Path:
    env = os.environ.get(MODEL_ENV)
    return Path(env) if env else (
        config.assets_dir() / "models" / "selfie_segmentation_landscape.onnx")


def _session():
    with _lock:
        if "session" not in _state:
            session = None
            path = model_path()
            if path.exists():
                try:
                    import onnxruntime as ort

                    session = ort.InferenceSession(
                        str(path), providers=["CPUExecutionProvider"])
                    _state["input"] = session.get_inputs()[0].name
                except Exception:  # noqa: BLE001 - optional feature
                    session = None
            _state["session"] = session
        return _state["session"]


def available() -> bool:
    return _session() is not None


def person_alpha(rgb: np.ndarray) -> np.ndarray | None:
    """Probability (0..1) that each pixel of ``rgb`` is a person.

    ``rgb`` is an ``H x W x 3`` uint8 picture of any size; the result is
    ``144 x 256`` float32, or ``None`` without a usable model.
    """
    session = _session()
    if session is None:
        return None
    from PIL import Image

    picture = Image.fromarray(rgb).resize(INPUT_SIZE, Image.BILINEAR)
    batch = (np.asarray(picture, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]
    try:
        alpha = session.run(None, {_state["input"]: batch})[0]
    except Exception:  # noqa: BLE001 - a bad frame must not stop the stream
        return None
    return np.clip(alpha[0, 0], 0.0, 1.0)
