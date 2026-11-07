"""The added static stages: real pictures, distinct, cached once."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from anycam import overlay, stages


@pytest.fixture(autouse=True)
def cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.mark.parametrize("name", list(stages.STATIC))
def test_each_static_stage_is_a_real_picture(name):
    assert name in overlay.BACKGROUND_NAMES, "listed in the Stage tab"
    path = overlay.render_background(name, 320, 180)
    with Image.open(path) as opened:
        assert opened.size == (320, 180)
        pixels = np.asarray(opened.convert("RGB"), np.float32)
    assert 8 < pixels.mean() < 247, "not black or white"
    assert pixels.std() > 3, "not a flat colour"


def test_the_static_stages_look_different_from_each_other():
    pictures = {name: np.asarray(Image.open(overlay.render_background(name, 160, 90))
                                 .convert("RGB"), np.float32)
                for name in stages.STATIC}
    names = list(pictures)
    for index, first in enumerate(names):
        for second in names[index + 1:]:
            difference = np.abs(pictures[first] - pictures[second]).mean()
            assert difference > 6, f"{first} and {second} look the same"


def test_a_static_stage_is_rendered_once_and_then_cached(monkeypatch):
    path = overlay.render_background("Blueprint", 320, 180)
    calls = []
    monkeypatch.setitem(stages.STATIC, "Blueprint",
                        lambda w, h: calls.append((w, h)) or Image.new("RGB", (w, h)))
    assert overlay.render_background("Blueprint", 320, 180) == path
    assert calls == [], "served from the cache"
