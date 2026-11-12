"""Motion detection on the canvas picture.

``detect`` compares two small grey pictures; ``MotionMonitor`` turns a stream of
those comparisons into one decision per quiet/active spell: motion must last
``hold_s`` before it counts, and after it fires nothing fires again for
``cooldown_s``.  The window feeds it the preview frames and acts on the result
(switch scene, notify).
"""
from __future__ import annotations

import numpy as np

SAMPLE_WIDTH = 96          # the comparison runs on a picture this wide


def to_grey(rgba: np.ndarray, width: int = SAMPLE_WIDTH) -> np.ndarray:
    """A small float grey copy of an (h, w, 3|4) uint8 picture (box-averaged)."""
    height, full = rgba.shape[:2]
    step = max(1, full // width)
    small = rgba[: height - height % step : step, : full - full % step : step, :3].astype(np.float32)
    return small @ np.array([0.299, 0.587, 0.114], dtype=np.float32)


def detect(previous: np.ndarray, current: np.ndarray, threshold: float = 18.0
           ) -> tuple[float, tuple[float, float, float, float] | None]:
    """(share of the picture that changed, box of the change as 0..1 fractions).

    ``threshold`` is the grey-level difference (0..255) that counts as change.
    """
    if previous.shape != current.shape:
        return 0.0, None
    changed = np.abs(current - previous) >= threshold
    share = float(changed.mean())
    if not changed.any():
        return 0.0, None
    rows = np.flatnonzero(changed.any(axis=1))
    cols = np.flatnonzero(changed.any(axis=0))
    height, width = changed.shape
    return share, (cols[0] / width, rows[0] / height,
                   (cols[-1] + 1) / width, (rows[-1] + 1) / height)


class MotionMonitor:
    """Decides when motion is worth acting on."""

    def __init__(self, sensitivity: float = 0.5, hold_s: float = 1.0,
                 cooldown_s: float = 30.0) -> None:
        self.set_sensitivity(sensitivity)
        self.hold_s, self.cooldown_s = max(0.0, hold_s), max(0.0, cooldown_s)
        self._previous: np.ndarray | None = None
        self._since: float | None = None
        self._quiet_until = 0.0
        self.level = 0.0                  # share of the picture moving in the last frame
        self.region: tuple[float, float, float, float] | None = None

    def set_sensitivity(self, value: float) -> None:
        """0 (only big movement) .. 1 (the slightest)."""
        self.sensitivity = max(0.0, min(1.0, float(value)))

    @property
    def min_share(self) -> float:
        return 0.002 + (1.0 - self.sensitivity) * 0.10      # 0.2 % .. 10.2 % of the picture

    @property
    def threshold(self) -> float:
        return 10.0 + (1.0 - self.sensitivity) * 30.0        # grey levels

    def update(self, rgba: np.ndarray, now: float) -> bool:
        """Feed one frame; True when motion has just earned an action."""
        grey = to_grey(rgba)
        previous, self._previous = self._previous, grey
        if previous is None:
            return False
        self.level, self.region = detect(previous, grey, self.threshold)
        if self.level < self.min_share:
            self._since = None
            return False
        if self._since is None:
            self._since = now
        if now < self._quiet_until or now - self._since < self.hold_s:
            return False
        self._since = None
        self._quiet_until = now + self.cooldown_s
        return True

    def reset(self) -> None:
        self._previous = None
        self._since = None
