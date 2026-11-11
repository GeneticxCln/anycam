"""GPU canvas (glvideomixer), the native frame counter, empty drawings."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from anycam import animated, pipeline  # noqa: E402
from anycam.model import Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)


def _gl_canvas_works() -> bool:
    return not pipeline.gl_canvas_problem()


needs_gl_canvas = pytest.mark.skipif(not _gl_canvas_works(),
                                     reason="no working GL canvas here")


def _project(*layers: Layer, width=640, height=360) -> Project:
    project = Project(name="canvas")
    project.output = OutputConfig(canvas_w=width, canvas_h=height, fps=30)
    scene = Scene("scene_canvas", "Canvas")
    for layer in layers or (Layer("bg", "test", "BG", source="smpte", box=(0, 0, 1, 1), z=1),):
        scene.add_layer(layer)
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def _run(engine: AnyCamEngine, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        engine.pump(20)
        engine.deliver_frame()


# ------------------------------------------------------------- choosing
def test_the_canvas_setting_accepts_auto_and_cpu_only():
    assert OutputConfig().canvas_backend == "auto"
    assert OutputConfig.from_dict({"canvas_backend": "cpu"}).canvas_backend == "cpu"
    assert OutputConfig.from_dict({"canvas_backend": "metal"}).canvas_backend == "auto"


@pytest.mark.parametrize("value, expected", [("gpu", "gpu"), ("CPU", "cpu"), ("fast", ""), ("", "")])
def test_anycam_canvas_overrides(monkeypatch, value, expected):
    monkeypatch.setenv("ANYCAM_CANVAS", value)
    assert pipeline.canvas_requested() == expected


def test_a_direct_engine_defaults_to_the_cpu_canvas(monkeypatch):
    monkeypatch.delenv("ANYCAM_CANVAS", raising=False)
    engine = AnyCamEngine(_project(), preview=True)
    assert engine.canvas == "cpu"
    assert AnyCamEngine(_project(), preview=True, canvas="gpu").canvas == "gpu"


def test_an_unusable_gl_canvas_falls_back_to_the_compositor(monkeypatch):
    monkeypatch.setattr("anycam.engine.controller.gl_canvas_problem", lambda: "glvideomixer is not installed")
    engine = AnyCamEngine(_project(), preview=True, canvas="gpu").build()
    try:
        assert engine.canvas_backend == "cpu"
        assert engine.compositor.get_factory().get_name() == "compositor"
    finally:
        engine.stop()


# ------------------------------------------------------------- the canvas
@needs_gl_canvas
def test_the_gpu_canvas_uploads_every_layer_and_downloads_once():
    layers = (Layer("bg", "test", "BG", source="smpte", box=(0, 0, 1, 1), z=1),
              Layer("pip", "test", "PiP", source="ball", box=(0.6, 0.6, 0.3, 0.3), z=2))
    engine = AnyCamEngine(_project(*layers), preview=True, canvas="gpu").build()
    try:
        assert engine.canvas_backend == "gpu"
        names = [child.get_factory().get_name() for child in engine.pipeline.children]
        # background + two layers, each behind a pool-dropping identity.
        assert names.count("glupload") == 3
        assert names.count("gldownload") == 1
        uploads = [c for c in engine.pipeline.children if c.get_factory().get_name() == "glupload"]
        for upload in uploads:
            upstream = upload.get_static_pad("sink").get_peer().get_parent_element()
            assert upstream.get_factory().get_name() == "identity"
            assert upstream.get_property("drop-allocation") is True
        pad = engine._layer_pads["pip"]
        assert pad.get_property("sizing-policy").value_nick == "keep-aspect-ratio"
    finally:
        engine.stop()


@needs_gl_canvas
def test_cpu_and_gpu_canvases_produce_the_same_picture():
    import numpy as np

    def frame(canvas):
        frames = []

        def on_frame(sample):
            buf = sample.get_buffer()
            caps = sample.get_caps().get_structure(0)
            w, h = caps.get_value("width"), caps.get_value("height")
            ok, info = buf.map(Gst.MapFlags.READ)
            frames.append(np.frombuffer(bytes(info.data), np.uint8)[:w * h * 4].reshape(h, w, 4).copy())
            buf.unmap(info)

        layers = (Layer("bg", "test", "BG", source="smpte", box=(0, 0, 1, 1), z=1),
                  Layer("box", "test", "Box", source="smpte", box=(0.55, 0.55, 0.4, 0.4), z=2, alpha=0.6))
        engine = AnyCamEngine(_project(*layers), preview=True, preview_target=(640, 360),
                              on_frame=on_frame, canvas=canvas).build()
        engine.start()
        try:
            _run(engine, 2.0)
        finally:
            engine.stop()
        assert frames, f"no {canvas} frames"
        return frames[-1][..., :3].astype(int)

    cpu, gpu = frame("cpu"), frame("gpu")
    difference = np.abs(cpu - gpu)
    # smpte's bottom-right block is random noise that changes every frame
    # (it runs to the right edge, for the background and for the inset);
    # ignore that area and compare everything else.
    h, w = difference.shape[:2]
    difference[int(h * 0.75):, int(w * 0.70):] = 0
    assert difference.mean() < 1.0, difference.mean()
    assert (difference.max(axis=2) > 8).mean() < 0.02


@needs_gl_canvas
def test_the_gpu_canvas_follows_live_layer_moves():
    layer = Layer("pip", "test", "PiP", source="smpte", box=(0.1, 0.1, 0.3, 0.3), z=2)
    engine = AnyCamEngine(_project(Layer("bg", "test", "BG", source="black", box=(0, 0, 1, 1), z=1), layer),
                          preview=True, canvas="gpu").build()
    engine.start()
    try:
        _run(engine, 1.0)
        assert engine.set_layer_box("pip", (0.5, 0.5, 0.4, 0.4)) is not False
        pad = engine._layer_pads["pip"]
        assert pad.get_property("xpos") == 320 and pad.get_property("ypos") == 180
        frames = engine._frame_count
        _run(engine, 0.8)
        assert engine._frame_count > frames
        assert not engine.report.error
    finally:
        engine.stop()


# ------------------------------------------------------- frame counting
def test_canvas_frames_are_counted_without_a_python_probe_on_the_mixer():
    engine = AnyCamEngine(_project(), preview=True).build()
    try:
        output = engine.compositor.get_static_pad("src")
        assert not any(pad is output for pad, _probe in engine._pad_probes)
        assert engine._frame_counter.get_factory().get_name() == "fakesink"
        engine.start()
        _run(engine, 1.2)
        counted = engine._frame_count
        assert counted > 15
        # start() resets the count; a reset is relative to what the sink saw.
        engine._frame_count = 0
        assert 0 <= engine._frame_count <= 2
    finally:
        engine.stop()


def test_the_first_buffer_probe_runs_once():
    layer = Layer("cam", "test", "Cam", source="smpte", box=(0, 0, 1, 1), z=1)
    engine = AnyCamEngine(_project(layer), preview=True).build()
    engine.start()
    try:
        _run(engine, 1.0)
        assert "cam" in engine._pads_with_data
        pad = engine._layer_pads["cam"]
        assert not any(p is pad for p, _probe in engine._pad_probes)
    finally:
        engine.stop()


# --------------------------------------------------------------- drawings
def test_an_empty_drawing_is_left_out_of_the_canvas():
    empty = Layer("draw", "drawing", "Drawing", source=animated.dump_strokes([]),
                  box=(0, 0, 1, 1), z=5)
    engine = AnyCamEngine(_project(Layer("bg", "test", "BG", source="smpte", box=(0, 0, 1, 1), z=1),
                                   empty), preview=True).build()
    try:
        assert "draw" not in engine._layer_pads
        # The GUI rebuilds when the first stroke cannot be applied live.
        assert engine.set_layer_strokes("draw", [{"tool": "pen", "points": [[0, 0], [1, 1]]}]) is False
    finally:
        engine.stop()


def test_a_drawing_with_strokes_is_built():
    strokes = [{"tool": "pen", "color": "#ff0000", "width": 0.01, "points": [[0.1, 0.1], [0.9, 0.9]]}]
    drawing = Layer("draw", "drawing", "Drawing", source=animated.dump_strokes(strokes),
                    box=(0, 0, 1, 1), z=5)
    engine = AnyCamEngine(_project(Layer("bg", "test", "BG", source="smpte", box=(0, 0, 1, 1), z=1),
                                   drawing), preview=True).build()
    try:
        assert "draw" in engine._layer_pads
    finally:
        engine.stop()


# --------------------------------------------------------------------- GUI
@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def test_the_window_picks_the_canvas_from_the_setting(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ANYCAM_CANVAS", raising=False)
    from anycam.demo import build_demo_project
    from anycam.gui import AnyCamWindow

    window = AnyCamWindow(build_demo_project("testsrc", 320, 180, 30))
    try:
        assert window._canvas_choice() == "gpu"           # automatic
        window.project.output.canvas_backend = "cpu"
        assert window._canvas_choice() == "cpu"
        window.project.output.canvas_backend = "auto"
        window._gpu_canvas_failed = True                  # failed this session
        assert window._canvas_choice() == "cpu"
        monkeypatch.setenv("ANYCAM_CANVAS", "gpu")
        assert window._canvas_choice() == "gpu"           # the override wins
    finally:
        window.close()


def test_a_gpu_canvas_that_fails_to_start_retries_on_the_cpu(app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ANYCAM_CANVAS", raising=False)
    from anycam import gui
    from anycam.demo import build_demo_project

    window = gui.AnyCamWindow(build_demo_project("testsrc", 320, 180, 30))
    window.headless = True
    asked = []
    real = gui.AnyCamEngine

    class FlakyGpuEngine(real):
        def start(self):
            asked.append(self.canvas)
            if self.canvas == "gpu":
                self.canvas_backend = "gpu"
                raise RuntimeError("GL context lost")
            return super().start()

    monkeypatch.setattr("anycam.ui.engine_control.AnyCamEngine", FlakyGpuEngine)
    try:
        window.start_engine()
        assert asked == ["gpu", "cpu"]
        assert window.engine is not None and window.engine.canvas_backend == "cpu"
        assert window._gpu_canvas_failed
    finally:
        window.shutdown()
        window.close()


# ------------------------------------------- colour looks on the GPU canvas
def _distinct_frames_with_a_look(monkeypatch=None, latency=None) -> tuple[int, int]:
    """Distinct canvas pictures in 1.5 s before and after a colour look."""
    import copy
    import zlib

    if latency is not None:
        monkeypatch.setattr("anycam.engine.controller.GL_MIXER_LATENCY_NS", latency)
    seen: list[int] = []

    def on_frame(sample):
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(info.data)
            seen.append(zlib.crc32(data[len(data) // 3: len(data) // 3 + 120000]))
            buf.unmap(info)

    layer = Layer("cam", "test", "Cam", source="snow", box=(0.09, 0.14, 0.79, 0.71), z=1)
    project = _project(layer, width=1920, height=1080)
    project.output.fps = 60
    engine = AnyCamEngine(project, preview=True, on_frame=on_frame, canvas="gpu").build()
    engine.start()
    try:
        _run(engine, 1.5)
        seen.clear()
        _run(engine, 1.5)
        before = len(set(seen))
        look = copy.deepcopy(layer.effects)
        look.saturation, look.contrast, look.brightness = 1.12, 1.05, 0.03    # Warm
        assert engine.set_layer_effects("cam", look)
        _run(engine, 0.5)
        seen.clear()
        _run(engine, 1.5)
        return before, len(set(seen))
    finally:
        engine.stop()


@needs_gl_canvas
def test_the_gpu_mixer_waits_for_layers_slowed_by_effects():
    """With no extra latency glvideomixer dropped a camera's frames once a
    colour look made them late, and repeated its last one: 60 fps of the same
    picture.  It reproduced with the real camera path (pool + intervideosrc)
    from 0 to 10 ms of latency and was gone from 20 ms; a synthetic source does
    not reproduce it, so the setting itself is checked here."""
    assert pipeline.GL_MIXER_LATENCY_NS >= 20_000_000
    engine = AnyCamEngine(_project(), preview=True, canvas="gpu").build()
    try:
        assert engine.compositor.get_property("latency") == pipeline.GL_MIXER_LATENCY_NS
    finally:
        engine.stop()


@needs_gl_canvas
def test_a_colour_look_keeps_the_gpu_canvas_moving():
    """Smoke test: pictures keep changing with a look on (see above)."""
    before, after = _distinct_frames_with_a_look()
    assert before > 20
    assert after > 20, f"only {after} distinct pictures in 1.5 s with a colour look on"


# ----------------------------------------------- colour step as a GPU shader
def _look_frame(canvas: str, look: dict):
    import numpy as np

    frames = []

    def on_frame(sample):
        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        frames.append(np.frombuffer(bytes(info.data), np.uint8)[:w * h * 4].reshape(h, w, 4).copy())
        buf.unmap(info)

    layer = Layer("bg", "test", "BG", source="smpte", box=(0, 0, 1, 1), z=1)
    for key, value in look.items():
        setattr(layer.effects, key, value)
    engine = AnyCamEngine(_project(layer), preview=True, preview_target=(640, 360),
                          on_frame=on_frame, canvas=canvas).build()
    engine.start()
    try:
        _run(engine, 1.5)
        rack = engine._layer_chains["bg"].effects
    finally:
        engine.stop()
    picture = frames[-1][..., :3].astype(int)
    h, w = picture.shape[:2]
    # smpte's random-noise block (bottom right, bottom left when mirrored).
    picture[int(h * 0.75):, :] = 0
    return picture, rack


@needs_gl_canvas
@pytest.mark.parametrize("look", [
    dict(brightness=0.0, contrast=1.35, saturation=0.0, hue=0.0),        # Noir
    dict(brightness=0.03, contrast=1.05, saturation=1.12, hue=-0.035),   # Warm
    dict(brightness=0.0, contrast=1.0, saturation=1.0, hue=0.0, mirror=True),
])
def test_a_look_on_the_gpu_canvas_matches_the_cpu_canvas(look):
    import numpy as np

    cpu, _ = _look_frame("cpu", look)
    gpu, rack = _look_frame("gpu", look)
    # The GPU did the work: the CPU colour step and mirror stayed neutral.
    assert rack["balance"].get_property("contrast") == 1.0
    assert rack["balance"].get_property("saturation") == 1.0
    assert rack["videoflip"].get_property("video-direction").value_nick == "identity"
    difference = np.abs(cpu - gpu)
    assert difference.mean() < 2.0, difference.mean()


@needs_gl_canvas
def test_the_colour_step_returns_to_the_cpu_when_a_filter_follows_it():
    """A filter, frame, film style or chroma key comes after the colour step;
    running it on the GPU after them would change the picture."""
    _, rack = _look_frame("gpu", dict(contrast=1.35, saturation=0.0, filter="blur"))
    assert rack["balance"].get_property("contrast") == pytest.approx(1.35)
    assert rack["balance"].get_property("saturation") == 0.0
    uniforms = rack["gl_balance"].get_property("uniforms")
    assert uniforms.get_value("contrast") == pytest.approx(1.0)
    assert uniforms.get_value("saturation") == pytest.approx(1.0)
