"""Playlist timing and order, without any window in it.

A scene's playlist rotates whole layers (images and clips) on a timer.  This
module answers the two questions the rotation asks: how long does the current
item stay, and which one comes next.
"""
from __future__ import annotations

import random
from collections.abc import Sequence

from .model import PlaylistConfig

MIN_SECONDS = 0.5
MAX_SECONDS = 3600.0


def item_seconds(state: PlaylistConfig, layer_id: str) -> float:
    """How long ``layer_id`` stays: its own duration, else the playlist's interval."""
    own = state.durations.get(layer_id)
    seconds = own if own else state.interval
    return max(MIN_SECONDS, min(MAX_SECONDS, float(seconds)))


def next_index(ids: Sequence[str], index: int, *, loop: bool = True, shuffle: bool = False,
               rng: random.Random | None = None) -> int | None:
    """The item to show after ``index`` (None: the end of a playlist that does not loop)."""
    count = len(ids)
    if count == 0:
        return None
    if shuffle and count > 1:
        choices = [i for i in range(count) if i != index]
        return (rng or random).choice(choices)
    following = index + 1
    if following < count:
        return following
    return 0 if loop else None


def previous_index(ids: Sequence[str], index: int, *, loop: bool = True) -> int | None:
    """The item before ``index`` (wrapping when the playlist loops)."""
    if not ids:
        return None
    if index <= 0:
        return len(ids) - 1 if loop else 0
    return index - 1
