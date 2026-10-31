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
except Exception:  # pragma: no cover - platform without GStreamer
    HAS_COMPOSITOR = False

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
