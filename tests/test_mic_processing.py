"""Microphone voice processing: webrtcdsp noise suppression and auto gain."""

from __future__ import annotations

import time

import pytest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

from anycam import config  # noqa: E402
from anycam.model import AudioConfig, Project  # noqa: E402
from anycam.pipeline import (  # noqa: E402
    AnyCamEngine,
    describe_mic_dsp,
    mic_dsp_elements,
    mic_dsp_wanted,
)
from anycam.demo import build_demo_project  # noqa: E402

needs_dsp = pytest.mark.skipif(Gst.ElementFactory.find("webrtcdsp") is None,
                               reason="GStreamer webrtcdsp is not installed")


def test_noise_suppression_is_on_by_default_and_old_projects_get_it():
    assert AudioConfig().mic_denoise == "moderate"
    old = Project.from_dict({"audio": {"mic_enabled": True}})
    assert old.audio.mic_denoise == "moderate"
    assert not old.audio.mic_auto_gain


def test_only_known_levels_turn_processing_on():
    assert not mic_dsp_wanted(AudioConfig(mic_denoise="off"))
    assert not mic_dsp_wanted(AudioConfig(mic_denoise="loud"))
    assert mic_dsp_wanted(AudioConfig(mic_denoise="off", mic_auto_gain=True))
    assert mic_dsp_elements(AudioConfig(mic_denoise="off")) == []
    assert describe_mic_dsp(AudioConfig(mic_denoise="high", mic_auto_gain=True)) == \
        "noise suppression high, auto gain"


@needs_dsp
def test_the_dsp_is_configured_without_an_echo_probe():
    elements = mic_dsp_elements(AudioConfig(mic_denoise="very-high"))
    dsp = elements[-1]
    assert dsp.get_factory().get_name() == "webrtcdsp"
    assert dsp.get_property("echo-cancel") is False, \
        "echo cancel needs a webrtcechoprobe AnyCam does not own"
    assert int(dsp.get_property("noise-suppression-level")) == 3
    assert dsp.get_property("noise-suppression") is True
    assert dsp.get_property("gain-control") is False


def _noise_rms(audio: AudioConfig | None, seconds: float = 2.5) -> float:
    pipeline = Gst.Pipeline.new()
    src = Gst.ElementFactory.make("audiotestsrc")
    src.set_property("is-live", True)
    src.set_property("wave", "pink-noise")
    src.set_property("volume", 0.3)
    chain = mic_dsp_elements(audio) if audio is not None else []
    level = Gst.ElementFactory.make("level")
    level.set_property("interval", 200_000_000)
    convert = Gst.ElementFactory.make("audioconvert")
    sink = Gst.ElementFactory.make("fakesink")
    sink.set_property("sync", True)
    elements = [src, *chain, convert, level, sink]
    for element in elements:
        pipeline.add(element)
    for upstream, downstream in zip(elements, elements[1:]):
        assert upstream.link(downstream)
    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)
    values: list[float] = []
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            message = bus.timed_pop_filtered(100 * Gst.MSECOND,
                                             Gst.MessageType.ELEMENT | Gst.MessageType.ERROR)
            if message is None:
                continue
            assert message.type != Gst.MessageType.ERROR, message.parse_error()
            structure = message.get_structure()
            if structure is not None and structure.get_name() == "level":
                values.append(structure.get_value("rms")[0])
    finally:
        pipeline.set_state(Gst.State.NULL)
    assert len(values) >= 5
    tail = values[len(values) // 2:]  # after the suppressor has adapted
    return sum(tail) / len(tail)


@needs_dsp
def test_noise_suppression_really_removes_steady_noise():
    raw = _noise_rms(None)
    moderate = _noise_rms(AudioConfig(mic_denoise="moderate"))
    high = _noise_rms(AudioConfig(mic_denoise="high"))
    assert raw - moderate >= 10, f"moderate removed {raw - moderate:.1f} dB"
    assert raw - high >= 15, f"high removed {raw - high:.1f} dB"
    assert high < moderate


@needs_dsp
def test_the_engine_puts_the_dsp_between_mic_and_mixer(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "microphone", lambda: "fake-mic")
    project = build_demo_project("testsrc", 320, 180, 30)
    project.audio = AudioConfig(mic_enabled=True, mic_device="fake-mic",
                                mic_denoise="high")
    engine = AnyCamEngine(project, preview=True, audio=True,
                          record_path=tmp_path / "a.mp4").build()
    try:
        dsp = engine.pipeline.get_by_name("mic_dsp")
        assert dsp is not None
        peer = dsp.get_static_pad("src").get_peer()
        assert peer is not None and peer.get_parent_element().get_name() == "mic_conv"
        assert engine.report.mic_processing == "noise suppression high"
        assert engine.report.as_dict()["mic_processing"] == "noise suppression high"
    finally:
        engine.pipeline.set_state(Gst.State.NULL)

    project.audio.mic_denoise = "off"
    engine = AnyCamEngine(project, preview=True, audio=True,
                          record_path=tmp_path / "b.mp4").build()
    try:
        assert engine.pipeline.get_by_name("mic_dsp") is None
        assert engine.report.mic_processing == ""
    finally:
        engine.pipeline.set_state(Gst.State.NULL)
