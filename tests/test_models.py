"""Optional effect models: verified downloads and the RVM segmenter."""

from __future__ import annotations

import hashlib
import io

import numpy as np
import pytest
from PIL import Image

from anycam import models, segment


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    yield
    segment.set_model("mediapipe")


def _fake_model(monkeypatch, payload: bytes, *, sha: str | None = None):
    info = models.ModelInfo(
        key="rvm", label="Test", filename="test.onnx", url="https://example.invalid/m",
        sha256=sha or hashlib.sha256(payload).hexdigest(), size=len(payload),
        license="MIT", source="https://example.invalid")
    monkeypatch.setitem(models.CATALOG, "rvm", info)
    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout=None):
        requests.append(request)
        return Response(payload)

    monkeypatch.setattr(models.urllib.request, "urlopen", urlopen)
    return info, requests


def test_a_verified_download_lands_in_the_data_dir(monkeypatch, tmp_path):
    payload = b"onnx" * 5000
    info, requests = _fake_model(monkeypatch, payload)
    seen = []
    path = models.download("rvm", progress=lambda done, total: seen.append((done, total)))
    assert path == tmp_path / "data" / "anycam" / "models" / "test.onnx"
    assert path.read_bytes() == payload
    assert models.model_path("rvm") == path
    assert seen[-1] == (len(payload), len(payload))
    assert requests[0].get_header("User-agent") == "AnyCam", "no personal data sent"


def test_a_tampered_download_is_discarded(monkeypatch):
    _fake_model(monkeypatch, b"evil" * 100, sha="0" * 64)
    with pytest.raises(ValueError, match="SHA-256"):
        models.download("rvm")
    assert models.model_path("rvm") is None
    assert not list(models.models_dir().iterdir()), "no partial file left behind"


def test_a_truncated_or_oversized_download_is_refused(monkeypatch):
    info, _ = _fake_model(monkeypatch, b"x" * 100)
    monkeypatch.setitem(models.CATALOG, "rvm", models.ModelInfo(
        **{**info.__dict__, "size": 50}))
    with pytest.raises(ValueError, match="larger"):
        models.download("rvm")
    monkeypatch.setitem(models.CATALOG, "rvm", models.ModelInfo(
        **{**info.__dict__, "size": 500}))
    with pytest.raises(ValueError, match="stopped"):
        models.download("rvm")
    assert models.model_path("rvm") is None


def test_the_real_catalog_entry_points_at_the_authors_release():
    rvm = models.CATALOG["rvm"]
    assert rvm.url.startswith("https://github.com/PeterL1n/RobustVideoMatting/releases/")
    assert len(rvm.sha256) == 64 and rvm.license == "GPL-3.0"


def test_rvm_is_only_used_once_downloaded(monkeypatch):
    segment.set_model("rvm")
    assert segment.chosen_model() == "rvm"
    assert segment.active_model() == "mediapipe"
    assert type(segment.segmenter()) is segment.Segmenter


def test_each_rvm_segmenter_keeps_its_own_recurrent_state(monkeypatch):
    calls = []

    class FakeRVM:
        def run(self, _outputs, feeds):
            calls.append(feeds)
            h, w = feeds["src"].shape[2:]
            step = float(feeds["r1i"].sum()) + 1.0
            state = np.full((1, 16, 4, 4), step, np.float32)
            return [None, np.full((1, 1, h, w), 0.5, np.float32), state, state, state, state]

    monkeypatch.setattr(segment, "_rvm_session", lambda: FakeRVM())
    picture = Image.new("RGB", (1280, 720), (10, 20, 30))
    first, second = segment.RVMSegmenter(), segment.RVMSegmenter()
    alpha = first.alpha(picture)
    assert alpha.shape == (360, 640)
    first.alpha(picture)
    second.alpha(picture)
    assert calls[0]["r1i"].shape == (1, 1, 1, 1), "starts from an empty state"
    assert calls[1]["r1i"].sum() > 0, "the next frame gets the previous state"
    assert calls[2]["r1i"].shape == (1, 1, 1, 1), "another layer has its own state"
    assert float(calls[0]["downsample_ratio"][0]) == pytest.approx(segment.RVM_DOWNSAMPLE)
