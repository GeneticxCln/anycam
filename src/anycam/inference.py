"""Where AnyCam's ONNX models run: the CPU by default, CUDA when chosen.

Every model session (person segmentation, face detection, any model a user
adds) is created here, so one setting moves them all.  The GPU is opt-in: the
canvas, NVENC and the virtual camera already share it, so CUDA only runs when
the user picks it *and* onnxruntime has the CUDA provider; anything else, or
any failure creating a CUDA session, falls back to the CPU.

Today's bundled models are tiny (the 256x144 segmentation runs in ~1 ms on the
CPU); the GPU path matters for heavier models.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

ENV = "ANYCAM_EFFECTS_DEVICE"
DEVICES = ("cpu", "gpu")
# Keep a model's CUDA arena small and growing only as asked: the GPU is shared
# with the compositor and the encoders.
GPU_MEMORY_LIMIT = 512 * 1024 * 1024

_lock = threading.Lock()
_preference = "cpu"
_listeners: list = []


def preference() -> str:
    """The device asked for: the environment wins over the saved setting."""
    env = os.environ.get(ENV, "").strip().lower()
    return env if env in DEVICES else _preference


def set_preference(device: str) -> bool:
    """Choose ``cpu`` or ``gpu``; True when it changed (sessions are rebuilt)."""
    global _preference
    device = str(device or "cpu").strip().lower()
    if device not in DEVICES:
        device = "cpu"
    with _lock:
        if device == _preference:
            return False
        _preference = device
        listeners = list(_listeners)
    for reset in listeners:
        try:
            reset()
        except Exception:  # noqa: BLE001 - a cache reset must not break the switch
            pass
    return True


def on_change(reset) -> None:
    """Call ``reset`` (drop cached sessions) whenever the device changes."""
    with _lock:
        if reset not in _listeners:
            _listeners.append(reset)


def available_providers() -> list[str]:
    try:
        import onnxruntime as ort
    except ImportError:
        return []
    try:
        return list(ort.get_available_providers())
    except Exception:  # noqa: BLE001
        return []


def gpu_available() -> bool:
    return "CUDAExecutionProvider" in available_providers()


def providers() -> list:
    """The provider list for a new session."""
    if preference() == "gpu" and gpu_available():
        return [("CUDAExecutionProvider", {
            "device_id": 0,
            "gpu_mem_limit": GPU_MEMORY_LIMIT,
            "arena_extend_strategy": "kSameAsRequested",
        }), "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


# Threads per session.  The effect models are small (256x144 person cut-out,
# face detection), so one or two threads keep up; ONNX Runtime's default is a
# pool the size of the CPU whose idle threads busy-spin between runs.  Measured
# on a 16-thread machine at 1080p60: a virtual background cost ~460% of a core,
# almost all of it seven spinning pool threads.
INTRA_OP_THREADS = 2


def session_options():
    """Small thread pools that sleep instead of spinning while idle."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = INTRA_OP_THREADS
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return options


def session(path: str | Path):
    """An onnxruntime session on the chosen device (CPU on any failure)."""
    import onnxruntime as ort

    wanted = providers()
    if wanted != ["CPUExecutionProvider"]:
        try:
            return ort.InferenceSession(str(path), sess_options=session_options(),
                                        providers=wanted)
        except Exception as exc:  # noqa: BLE001 - CUDA libraries missing or broken
            print(f"[effects] CUDA session for {Path(path).name} failed, using the "
                  f"CPU: {exc}", file=sys.stderr, flush=True)
    return ort.InferenceSession(str(path), sess_options=session_options(),
                                providers=["CPUExecutionProvider"])


def device_of(session) -> str:
    """``gpu`` or ``cpu``: where a session actually runs."""
    try:
        first = session.get_providers()[0]
    except Exception:  # noqa: BLE001
        return "cpu"
    return "gpu" if first == "CUDAExecutionProvider" else "cpu"


def describe() -> str:
    asked = preference()
    if asked == "gpu" and not gpu_available():
        return ("CPU (GPU chosen, but onnxruntime has no CUDA provider: install "
                "onnxruntime-gpu with CUDA and cuDNN)")
    return "NVIDIA GPU (CUDA)" if asked == "gpu" else "CPU"
