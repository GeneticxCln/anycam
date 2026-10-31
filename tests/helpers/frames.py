"""Real pixels from a real GStreamer pipeline.

The audit's core criticism was that the suite asserted model fields and never
looked at the picture.  These helpers build an engine, take one composited
preview frame and expose pixel access so a test can say "this corner is black".
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam.pipeline import AnyCamEngine  # noqa: E402


@dataclass
class Frame:
    """One RGBA preview frame, row-major, no row padding."""

    width: int
    height: int
    data: bytes

    def pixel(self, x: int, y: int) -> tuple[int, int, int, int]:
        x = max(0, min(self.width - 1, int(x)))
        y = max(0, min(self.height - 1, int(y)))
        offset = (y * self.width + x) * 4
        red, green, blue, alpha = self.data[offset:offset + 4]
        return red, green, blue, alpha

    def average(self, x0: int, y0: int, x1: int, y1: int) -> tuple[float, float, float]:
        """Average RGB over a half-open pixel rectangle."""
        x0 = max(0, min(self.width - 1, int(x0)))
        y0 = max(0, min(self.height - 1, int(y0)))
        x1 = max(x0 + 1, min(self.width, int(x1)))
        y1 = max(y0 + 1, min(self.height, int(y1)))
        total = [0, 0, 0]
        count = 0
        for y in range(y0, y1):
            row = y * self.width
            for x in range(x0, x1):
                offset = (row + x) * 4
                total[0] += self.data[offset]
                total[1] += self.data[offset + 1]
                total[2] += self.data[offset + 2]
                count += 1
        return tuple(value / count for value in total)  # type: ignore[return-value]

    def corner_is_black(self, box: tuple[int, int, int, int] = (0, 0, 24, 24)) -> bool:
        return max(self.average(*box)) < 24


def grab_frame(project, timeout: float = 6.0, **engine_options) -> Frame:
    """Run ``project`` for at most ``timeout`` seconds and return one frame."""
    frames: list[Frame] = []

    def on_frame(sample) -> None:
        buffer = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        width = int(caps.get_value("width"))
        height = int(caps.get_value("height"))
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return
        try:
            raw = bytes(info.data)[: width * height * 4]
        finally:
            buffer.unmap(info)
        frames.append(Frame(width, height, raw))

    engine = AnyCamEngine(project, preview=True, on_frame=on_frame,
                          **engine_options).build()
    engine.start()
    end = time.monotonic() + timeout
    frame = None
    try:
        # Source pads preroll at different speeds, so the first frames can
        # still be black (or show only one layer).  Wait for the graph to
        # settle, then inspect the latest frame.
        while time.monotonic() < end and len(frames) < 10:
            engine.pump(50)
        # Snapshot before teardown: stop() flushes the remaining frames and
        # the last of those is black.
        frame = frames[-1] if frames else None
    finally:
        engine.stop()
    assert frame is not None, "pipeline produced no preview frame"
    return frame
