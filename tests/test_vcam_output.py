"""The virtual camera must be a session output, not an engine-owned pipeline."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("gi")
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    HAS_COMPOSITOR = Gst.ElementFactory.find("compositor") is not None
    HAS_V4L2SINK = Gst.ElementFactory.find("v4l2sink") is not None
except Exception:  # pragma: no cover - platform without GStreamer
    HAS_COMPOSITOR = False
    HAS_V4L2SINK = False

from anycam import config  # noqa: E402
from anycam.demo import build_demo_project  # noqa: E402
from anycam.engine.outputs.vcam import VcamOutput  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

pytestmark = pytest.mark.skipif(not HAS_COMPOSITOR, reason="compositor unavailable")


class FakeVcam:
    active = True
    pixel_format = "YUY2"
    error = ""

    def __init__(self) -> None:
        self.pushed = 0

    def push_sample(self, _sample) -> None:
        self.pushed += 1

    def poll(self) -> None:
        return None


def test_missing_vcam_device_fails_without_a_pipeline():
    output = VcamOutput()
    assert output.start("/dev/anycam-missing", 320, 180, 30) is False
    assert "not available" in output.error
    assert output.active is False


def test_inactive_vcam_output_is_reported():
    """B17/B18: an output that failed to start must be visible in the report."""
    fake = FakeVcam()
    fake.active = False
    fake.error = "configured device disappeared"
    engine = AnyCamEngine(
        build_demo_project("testsrc", 320, 180, 30),
        preview=True,
        vcam=True,
        vcam_output=fake,
    ).build()
    try:
        assert "configured device disappeared" in engine.report.vcam_error
        assert engine.pipeline.get_by_name("vcam_appsink") is None
    finally:
        engine.pipeline.set_state(Gst.State.NULL)


@pytest.mark.skipif(not HAS_V4L2SINK, reason="v4l2sink unavailable")
def test_legacy_vcam_appsrc_is_bounded(monkeypatch):
    """B20: the self-contained vcam bridge used to queue without a bound."""
    monkeypatch.setattr(config, "vcam_available", lambda device=None: True)
    monkeypatch.setattr(config, "vcam_pixel_format", lambda device: "YUY2")
    engine = AnyCamEngine(
        build_demo_project("testsrc", 320, 180, 30),
        preview=True,
        vcam=True,
    ).build()
    try:
        assert engine._vcam_pipeline is not None
        appsrc = engine._vcam_pipeline.get_by_name("vcam_appsrc")
        assert appsrc is not None
        assert appsrc.get_property("max-buffers") == 2
        assert appsrc.get_property("block") is False
    finally:
        if engine._vcam_pipeline is not None:
            engine._vcam_pipeline.set_state(Gst.State.NULL)
        engine.pipeline.set_state(Gst.State.NULL)


def test_engine_uses_a_session_vcam_tap():
    fake = FakeVcam()
    engine = AnyCamEngine(
        build_demo_project("testsrc", 320, 180, 30),
        preview=True,
        vcam=True,
        vcam_output=fake,
    ).build()
    try:
        assert engine.pipeline.get_by_name("vcam_appsink") is not None
        assert engine._vcam_pipeline is None
        engine.start()
        for _ in range(20):
            engine.pump(50)
            time.sleep(0.01)
        assert fake.pushed > 0
        assert engine.report.vcam_active is True
    finally:
        engine.stop()
