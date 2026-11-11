"""Phase 4: audio mixer meters, media bed loop/playlist, voice presets, sync delay."""
from __future__ import annotations

import math
import struct
import time
import wave

import gi
import numpy as np

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam.engine import audio_helpers  # noqa: E402
from anycam.model import AudioConfig, Layer, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)


def write_tone(path, seconds: float = 0.6, freq: float = 440.0, amplitude: float = 0.5) -> str:
    rate = 48000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(
            struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(int(rate * seconds))))
    return str(path)


def audio_project(**audio) -> Project:
    project = Project(name="audio")
    project.output.canvas_w, project.output.canvas_h, project.output.fps = 160, 90, 15
    scene = Scene("scene_audio", "Audio")
    scene.add_layer(Layer("bg", "test", "BG", source="black", box=(0, 0, 1, 1), z=1))
    project.scenes = [scene]
    project.active_scene = scene.id
    project.audio = AudioConfig(**audio)
    return project


def run(engine: AnyCamEngine, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        engine.pump(40)


def loud(engine: AnyCamEngine, name: str = "media") -> bool:
    peak, _rms = engine.audio_levels().get(name, (-100.0, -100.0))
    return peak > -20.0


# ------------------------------------------------------------------ presets
def test_audio_settings_are_sanitised_and_round_trip():
    audio = AudioConfig.from_dict({
        "mic_dynamics": "BROADCAST", "mic_eq": "warm", "mic_delay_ms": 99999,
        "desktop_delay_ms": -5, "media_playlist": ["/a.wav", 7, "", "/b.wav"],
        "media_shuffle": "yes"})
    assert audio.mic_dynamics == "broadcast" and audio.mic_eq == "warm"
    assert audio.mic_delay_ms == 2000 and audio.desktop_delay_ms == 0
    assert audio.media_playlist == ["/a.wav", "/b.wav"] and audio.media_shuffle is True
    assert AudioConfig.from_dict({"mic_dynamics": "wild", "mic_eq": 4}).mic_dynamics == "off"
    assert AudioConfig.from_dict(None).media_playlist == []
    assert AudioConfig.from_dict(audio.__dict__).mic_eq == "warm"
    assert audio_helpers.delay_ms("x") == 0 and audio_helpers.delay_ms(1e9) == 2000


def voice_chain_rms(audio: AudioConfig, freq: float, volume: float) -> float:
    """RMS through the real mic voice elements, built the way the engine builds them."""
    pipeline = Gst.Pipeline.new("voice")
    source = Gst.parse_bin_from_description(
        f"audiotestsrc wave=sine freq={freq} volume={volume} num-buffers=40 samplesperbuffer=2400 "
        "! capsfilter caps=audio/x-raw,format=F32LE,rate=48000,channels=2", True)
    sink = Gst.ElementFactory.make("appsink", "k")
    sink.set_property("sync", False)
    sink.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=F32LE,rate=48000,channels=2"))
    tail = Gst.ElementFactory.make("audioconvert", "tail")
    elements = [source, *audio_helpers.voice_elements(audio), tail, sink]
    for element in elements:
        pipeline.add(element)
    for first, second in zip(elements, elements[1:], strict=False):
        assert first.link(second)
    pipeline.set_state(Gst.State.PLAYING)
    chunks = []
    while True:
        sample = sink.emit("pull-sample")
        if sample is None:
            break
        ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
        chunks.append(np.frombuffer(info.data, dtype=np.float32).copy())
        sample.get_buffer().unmap(info)
    pipeline.set_state(Gst.State.NULL)
    return float(np.sqrt((np.concatenate(chunks)[20000:] ** 2).mean()))


def test_the_compressor_levels_loud_peaks_and_leaves_quiet_speech():
    plain = AudioConfig()
    squeezed = AudioConfig(mic_dynamics="compressor")
    assert voice_chain_rms(squeezed, 440, 0.8) < 0.7 * voice_chain_rms(plain, 440, 0.8)
    quiet_plain, quiet_squeezed = (voice_chain_rms(plain, 440, 0.1),
                                   voice_chain_rms(squeezed, 440, 0.1))
    assert abs(quiet_squeezed - quiet_plain) < 0.01


def test_the_gate_silences_background_noise_but_passes_speech():
    gated = AudioConfig(mic_dynamics="gate")
    assert voice_chain_rms(gated, 440, 0.005) < 0.0015          # the hiss below the threshold
    assert voice_chain_rms(gated, 440, 0.5) > 0.3               # a voice well above it


def test_the_equaliser_presets_shape_the_spectrum():
    voice = AudioConfig(mic_eq="voice")
    assert voice_chain_rms(voice, 29, 0.5) < 0.2 * voice_chain_rms(AudioConfig(), 29, 0.5)
    assert voice_chain_rms(voice, 3770, 0.2) > 1.5 * voice_chain_rms(AudioConfig(), 3770, 0.2)
    assert audio_helpers.voice_elements(AudioConfig()) == []


def test_a_sync_delay_element_holds_audio_back():
    assert audio_helpers.delay_element(0, "d") is None
    queue = audio_helpers.delay_element(250, "d")
    assert queue.get_property("min-threshold-time") == 250 * Gst.MSECOND
    assert queue.get_property("max-size-time") > 250 * Gst.MSECOND


# ------------------------------------------------------------------ the mixer
def test_the_media_bed_reaches_its_meter_and_the_master_meter(tmp_path):
    tone = write_tone(tmp_path / "tone.wav", seconds=3.0)
    engine = AnyCamEngine(audio_project(media_file=tone), preview=True, audio=True,
                          on_frame=lambda _sample: None).build()
    engine.start()
    try:
        run(engine, 1.5)
        assert loud(engine, "media"), engine.audio_levels()
        assert loud(engine, "master"), engine.audio_levels()
        assert not loud(engine, "mic")
    finally:
        engine.stop()


def test_a_short_bed_loops_and_a_one_shot_bed_goes_quiet(tmp_path):
    tone = write_tone(tmp_path / "short.wav", seconds=0.5)
    looping = AnyCamEngine(audio_project(media_file=tone, media_loop=True), preview=True, audio=True,
                           on_frame=lambda _sample: None).build()
    looping.start()
    try:
        run(looping, 3.0)                       # six file lengths
        assert loud(looping, "media"), looping.audio_levels()
    finally:
        looping.stop()
    once = AnyCamEngine(audio_project(media_file=tone, media_loop=False), preview=True, audio=True,
                        on_frame=lambda _sample: None).build()
    once.start()
    try:
        run(once, 3.0)
        assert not loud(once, "media"), once.audio_levels()
    finally:
        once.stop()


def test_a_playlist_plays_each_file_then_wraps(tmp_path):
    first = write_tone(tmp_path / "one.wav", seconds=0.4, freq=300)
    second = write_tone(tmp_path / "two.wav", seconds=0.4, freq=900)
    engine = AnyCamEngine(audio_project(media_file=first, media_playlist=[second], media_loop=True),
                          preview=True, audio=True, on_frame=lambda _sample: None).build()
    engine.start()
    seen = set()
    try:
        end = time.monotonic() + 3.5
        while time.monotonic() < end:
            engine.pump(40)
            seen.add(engine._media_index)
        assert seen == {0, 1}, seen
        assert loud(engine, "media")
    finally:
        engine.stop()


def test_a_missing_bed_file_is_reported_not_fatal(tmp_path):
    engine = AnyCamEngine(audio_project(media_file=str(tmp_path / "gone.wav")), preview=True, audio=True,
                          on_frame=lambda _sample: None).build()
    engine.start()
    try:
        run(engine, 0.5)
        assert "not found" in engine.report.media_error
    finally:
        engine.stop()


# ------------------------------------------------------------ audio outputs
def test_virtual_microphone_properties_make_a_recordable_source():
    text = audio_helpers.virtual_mic_properties('My "Mic"; rm -rf')
    assert "media.class=Audio/Source" in text and "node.name=anycam-microphone" in text
    structure = Gst.Structure.from_string(text)[0]
    assert structure is not None and structure.get_name() == "props"
    assert structure.get_string("media.class") == "Audio/Source"
    # Quotes and shell characters in the name are dropped, not passed on.
    assert ";" not in structure.get_string("node.description")


def test_audio_outputs_are_built_only_when_the_server_is_there(tmp_path, monkeypatch):
    from anycam.engine import audio as audio_module

    tone = write_tone(tmp_path / "t.wav", seconds=1.0)
    project = audio_project(media_file=tone, virtual_mic=True, monitor_device="default")

    monkeypatch.setattr(audio_module, "audio_server_socket", lambda kind: "")
    engine = AnyCamEngine(project, preview=True, audio=True,
                          on_frame=lambda _sample: None).build()
    try:
        assert "PipeWire is not running" in engine.report.virtual_mic_error
        assert "server" in engine.report.audio_monitor_error
        assert engine.pipeline.get_by_name("audio_vmic_sink") is None
    finally:
        engine.stop()

    monkeypatch.setattr(audio_module, "audio_server_socket", lambda kind: "/run/fake")
    engine = AnyCamEngine(project, preview=True, audio=True,
                          on_frame=lambda _sample: None).build()
    try:
        sink = engine.pipeline.get_by_name("audio_vmic_sink")
        assert sink is not None
        assert sink.get_property("stream-properties").get_string("media.class") == "Audio/Source"
        assert engine.report.virtual_mic == "AnyCam Microphone"
        assert engine.pipeline.get_by_name("audio_monitor_sink") is not None
        assert engine.report.audio_monitor == "default output"
    finally:
        engine.stop()


# ------------------------------------------------------------- video-layer sound
import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def clip(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg unavailable")
    path = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=size=160x90:rate=15:duration=1", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=1", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(path)],
        check=True)
    return str(path)


def video_project(path: str, **layer) -> Project:
    project = audio_project()
    project.scenes[0].add_layer(Layer("clip", "video", "Clip", source=path,
                                      box=(0, 0, 1, 1), z=2, **layer))
    return project


def test_a_video_layers_sound_is_mixed_and_loops_with_the_picture(clip):
    engine = AnyCamEngine(video_project(clip), preview=True, audio=True,
                          on_frame=lambda _sample: None).build()
    engine.start()
    try:
        run(engine, 3.5)                        # 3+ loops of a 1 s clip
        assert loud(engine, "layeraudio_clip"), engine.audio_levels()
        assert loud(engine, "master"), engine.audio_levels()
    finally:
        engine.stop()


def test_a_muted_or_hidden_video_layer_is_silent_and_volume_applies(clip):
    layer_kwargs = dict(muted=True)
    engine = AnyCamEngine(video_project(clip, **layer_kwargs), preview=True, audio=True,
                          on_frame=lambda _sample: None).build()
    engine.start()
    try:
        run(engine, 2.0)
        assert not loud(engine, "layeraudio_clip"), engine.audio_levels()
        assert engine.set_layer_volume("clip", 0.25, False)
        run(engine, 1.0)
        peak = engine.audio_levels()["layeraudio_clip"][0]
        assert -35 < peak < -8, peak                       # 0.25 of ~-6 dB is about -18 dB
        engine.set_layer_visible("clip", False)
        run(engine, 1.2)
        assert not loud(engine, "layeraudio_clip")
    finally:
        engine.stop()


def test_removing_the_video_layer_removes_its_audio_input(clip):
    project = video_project(clip)
    engine = AnyCamEngine(project, preview=True, audio=True,
                          on_frame=lambda _sample: None).build()
    engine.start()
    try:
        run(engine, 1.0)
        assert "clip" in engine._layer_audio
        engine.detach_layer("clip")
        assert "clip" not in engine._layer_audio
        assert engine.pipeline.get_by_name("layeraudio_clip_volume") is None
        run(engine, 0.5)
        assert engine.report.error == ""
    finally:
        engine.stop()
