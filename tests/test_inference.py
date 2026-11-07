"""Where the ONNX models run: CPU by default, CUDA only when chosen and present."""

from __future__ import annotations

import pytest

from anycam import face, inference, segment


@pytest.fixture(autouse=True)
def cpu_afterwards(monkeypatch):
    monkeypatch.delenv(inference.ENV, raising=False)
    yield
    inference.set_preference("cpu")


def test_the_cpu_is_the_default():
    assert inference.preference() == "cpu"
    assert inference.providers() == ["CPUExecutionProvider"]


def test_gpu_without_cuda_stays_on_the_cpu(monkeypatch):
    monkeypatch.setattr(inference, "gpu_available", lambda: False)
    inference.set_preference("gpu")
    assert inference.providers() == ["CPUExecutionProvider"]
    assert "no CUDA provider" in inference.describe()


def test_gpu_with_cuda_asks_for_cuda_with_a_memory_cap(monkeypatch):
    monkeypatch.setattr(inference, "gpu_available", lambda: True)
    inference.set_preference("gpu")
    first, fallback = inference.providers()
    assert first[0] == "CUDAExecutionProvider"
    assert first[1]["gpu_mem_limit"] == inference.GPU_MEMORY_LIMIT
    assert fallback == "CPUExecutionProvider"


def test_a_broken_cuda_session_falls_back_to_the_cpu(monkeypatch):
    import onnxruntime as ort

    monkeypatch.setattr(inference, "gpu_available", lambda: True)
    inference.set_preference("gpu")
    calls = []
    real = ort.InferenceSession

    def fake(path, providers=None, **kwargs):
        calls.append(providers)
        if providers and providers[0] != "CPUExecutionProvider":
            raise RuntimeError("libcudnn.so.9: cannot open shared object file")
        return real(path, providers=providers, **kwargs)

    monkeypatch.setattr(ort, "InferenceSession", fake)
    session = inference.session(segment.model_path())
    assert inference.device_of(session) == "cpu"
    assert calls[-1] == ["CPUExecutionProvider"] and len(calls) == 2


def test_switching_device_drops_the_cached_sessions():
    assert segment.available()
    assert face.shared_detector() is not None
    assert segment._state and face._SHARED
    assert inference.set_preference("gpu")
    assert not segment._state and not face._SHARED, "rebuilt on the new device"
    assert not inference.set_preference("gpu"), "no change, no rebuild"
    assert segment.available()


def test_the_environment_overrides_the_setting(monkeypatch):
    monkeypatch.setenv(inference.ENV, "gpu")
    assert inference.preference() == "gpu"
    monkeypatch.setenv(inference.ENV, "toaster")
    assert inference.preference() == "cpu"
