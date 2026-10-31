"""Two-camera integration test with a synthetic V4L2 source.

This is hardware-backed and opt-in so the default suite never opens a user's
camera.  Point the test at one real camera and one v4l2loopback node:

    ANYCAM_TEST_CAMERA=/dev/video0 \
    ANYCAM_TEST_V4L2_SINK=/dev/video10 \
    PYTHONPATH=src .venv/bin/python -m pytest tests/test_multicam.py -v

The test feeds a live ``videotestsrc`` into the loopback node, then asks the
normal ``AnyCamEngine`` to composite two camera layers.  A buffer probe on each
source chain proves that both devices actually delivered frames; preview
delivery and a clean pipeline report prove the compositor consumed them.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: E402
except (ImportError, ValueError):
    pytest.skip("GStreamer typelib is unavailable", allow_module_level=True)

Gst.init(None)

CAMERA = os.environ.get("ANYCAM_TEST_CAMERA", "")
LOOPBACK = os.environ.get("ANYCAM_TEST_V4L2_SINK", "")
pytestmark = pytest.mark.skipif(
    not CAMERA or not LOOPBACK
    or not Path(CAMERA).exists() or not Path(LOOPBACK).exists()
    or Path(CAMERA).resolve() == Path(LOOPBACK).resolve()
    or Gst.ElementFactory.find("compositor") is None
    or Gst.ElementFactory.find("videotestsrc") is None
    or Gst.ElementFactory.find("videoconvert") is None
    or Gst.ElementFactory.find("v4l2src") is None
    or Gst.ElementFactory.find("v4l2sink") is None,
    reason="set ANYCAM_TEST_CAMERA and ANYCAM_TEST_V4L2_SINK to two V4L2 devices",
)


def _feed_loopback(device: str) -> Gst.Pipeline:
    pipeline = Gst.parse_launch(
        "videotestsrc is-live=true pattern=ball ! "
        "videoconvert ! "
        "video/x-raw,format=YUY2,width=640,height=480,framerate=30/1 ! "
        f"v4l2sink device={device}"
    )
    assert pipeline is not None
    result = pipeline.set_state(Gst.State.PLAYING)
    assert result != Gst.StateChangeReturn.FAILURE
    time.sleep(1.5)
    return pipeline


def test_two_camera_layers_composite_distinct_v4l2_devices(record_property):
    from anycam.model import Layer, Project, Scene
    from anycam.pipeline import AnyCamEngine

    producer = _feed_loopback(LOOPBACK)
    engine = None
    try:
        project = Project(name="multicam")
        project.output.canvas_w = 640
        project.output.canvas_h = 480
        project.output.fps = 30
        scene = Scene("scene_multicam", "Two cameras")
        scene.add_layer(Layer(
            "camera_main", "camera", "Main camera", source=CAMERA,
            box=(0.0, 0.0, 0.5, 1.0), z=1,
        ))
        scene.add_layer(Layer(
            "camera_synthetic", "camera", "Synthetic camera", source=LOOPBACK,
            box=(0.5, 0.0, 0.5, 1.0), z=2,
        ))
        project.scenes = [scene]
        project.active_scene = scene.id

        frames = []
        engine = AnyCamEngine(project, preview=True, on_frame=frames.append).build()
        assert len(engine.chains) == 2
        counts: dict[int, int] = {}

        def count_buffer(_pad, _info, index: int):
            counts[index] = counts.get(index, 0) + 1
            return Gst.PadProbeReturn.OK

        for index, chain in enumerate(engine.chains):
            chain.output_pad.add_probe(Gst.PadProbeType.BUFFER, count_buffer, index)

        report = engine.run(4.0)
        assert report["error"] == "", report["error"]
        assert report["frames"] > 10
        assert frames, "compositor produced no preview samples"
        assert len(counts) == 2, counts
        assert min(counts.values()) > 5, f"one camera delivered too few buffers: {counts}"
        record_property("camera", CAMERA)
        record_property("synthetic_source", LOOPBACK)
        record_property("buffer_counts", str(counts))
        record_property("composited_frames", report["frames"])
    finally:
        if engine is not None:
            engine.pipeline.set_state(Gst.State.NULL)
        producer.set_state(Gst.State.NULL)
