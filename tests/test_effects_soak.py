"""Swap filters continuously on a live source and check nothing accumulates.

A short soak (10 s by default; set ANYCAM_SOAK_SECONDS for a long one).  It
looks for what a per-swap leak would show: worker threads, memory, a stalled
canvas or an engine error.
"""
from __future__ import annotations

import ctypes
import gc
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import psutil
import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam import effects  # noqa: E402
from anycam.model import Effects, Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)
SECONDS = float(os.environ.get("ANYCAM_SOAK_SECONDS", "10"))


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def workers() -> list[str]:
    return [t.name for t in threading.enumerate()
            if t.name.startswith(("anycam-fx-", "anycam-backdrop-")) and t.is_alive()]


def test_swapping_filters_for_a_while_leaks_nothing_and_never_stalls():
    layer = Layer("cam", "test", "Cam", source="smpte", box=(0, 0, 1, 1), z=1)
    project = Project(name="soak")
    project.output = OutputConfig(canvas_w=640, canvas_h=360, fps=30)
    scene = Scene("s", "S")
    scene.add_layer(layer)
    project.scenes, project.active_scene = [scene], "s"
    frames = [0]
    engine = AnyCamEngine(project, preview=True,
                          on_frame=lambda sample: frames.__setitem__(0, frames[0] + 1)).build()
    engine.start()
    keys = [spec.key for spec in effects.available_filters()]
    process = psutil.Process()
    # One-time costs (two ONNX models, the sprite cache) are not a leak.
    import numpy as np

    from anycam import face, processors, segment

    segment.person_alpha(np.zeros((144, 256, 3), dtype=np.uint8))
    detector = face.shared_detector()
    if detector is not None:
        detector.detect(np.zeros((360, 640, 3), dtype=np.uint8))
    for name in processors._SPRITES:
        processors.sprite(name)
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            engine.pump(30)
        gc.collect()
        baseline_rss = process.memory_info().rss / 2 ** 20
        started = time.monotonic()
        swaps, last_frames, last_check = 0, frames[0], started
        while time.monotonic() - started < SECONDS:
            first = keys[swaps % len(keys)]
            second = keys[(swaps * 7 + 3) % len(keys)]
            engine.set_layer_effects(
                "cam", Effects(filter=first, filter2=second if swaps % 3 == 0 else "none"))
            swaps += 1
            end = time.monotonic() + 0.2
            while time.monotonic() < end:
                engine.pump(20)
            assert engine.report.error == "", f"after {swaps} swaps: {engine.report.error}"
            assert len(workers()) <= 6, f"{len(workers())} workers alive: {workers()}"
            now = time.monotonic()
            if now - last_check >= 2.0:
                assert frames[0] > last_frames + 15, "the canvas stalled while swapping"
                last_frames, last_check = frames[0], now
        chain = engine._layer_chains["cam"].effects
        assert chain["slot"].idle.wait(3) and chain["slot2"].idle.wait(3)
        # Each processor swap runs a worker thread, and glibc keeps a freed arena
        # per thread until it is trimmed.  Compare after trimming, so a real
        # leak shows and allocator slack does not (it measured 200+ MiB raw).
        gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except OSError:
            pass
        grown = process.memory_info().rss / 2 ** 20 - baseline_rss
        assert swaps >= 20
        assert grown < 120, f"memory grew {grown:.0f} MiB over {swaps} swaps"
    finally:
        engine.stop()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and workers():
        time.sleep(0.05)
    assert workers() == [], "workers survived the canvas"
