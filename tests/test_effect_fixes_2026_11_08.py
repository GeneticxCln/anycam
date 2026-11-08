"""Effects regressions found while testing the GPU canvas (2026-11-08).

* ONNX Runtime sessions used the default thread pool: one thread per CPU,
  idle threads busy-spinning.  A virtual background cost ~460% of a core
  on a 16-thread machine, almost all of it spinning.
* Looks: a film style (Sepia ...) stayed on through every later look,
  "Original" included, so swapping looks seemed broken.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from anycam import inference


# ------------------------------------------------------------------ ONNX
def test_sessions_use_a_small_pool_that_does_not_spin():
    pytest.importorskip("onnxruntime")
    options = inference.session_options()
    assert options.intra_op_num_threads == inference.INTRA_OP_THREADS <= 2
    assert options.inter_op_num_threads == 1
    assert options.get_session_config_entry("session.intra_op.allow_spinning") == "0"
    assert options.get_session_config_entry("session.inter_op.allow_spinning") == "0"


def test_every_bundled_model_session_gets_those_options(monkeypatch):
    ort = pytest.importorskip("onnxruntime")
    from pathlib import Path

    seen = []
    real = ort.InferenceSession

    def recording(path, sess_options=None, providers=None, **kwargs):
        seen.append(sess_options)
        return real(path, sess_options=sess_options, providers=providers, **kwargs)

    monkeypatch.setattr(ort, "InferenceSession", recording)
    models = sorted((Path(inference.__file__).parent / "assets" / "models").glob("*.onnx"))
    assert models, "no bundled models"
    for model in models:
        inference.session(model)
    assert len(seen) == len(models)
    for options in seen:
        assert options is not None
        assert options.get_session_config_entry("session.intra_op.allow_spinning") == "0"


# ----------------------------------------------------------------- Looks
@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    for name in ("CACHE", "CONFIG", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    from tests.test_effects_browser import close, make_window

    app = QApplication.instance() or QApplication([])
    win = make_window(app)
    win.preview.select_layer(win.scene.layers[0].id)
    app.processEvents()
    yield win, app
    close(win, app)


def _click(win, app, label):
    from tests.test_themed_content import _click as click

    click(win, app, "Looks", label)


@pytest.mark.parametrize("first, then, colour, saturation", [
    ("Sepia", "Warm", "none", 1.12),
    ("Sepia", "Original", "none", 1.0),
    ("Mono", "Sepia", "sepia", 1.0),
    ("Heat camera", "Mono", "none", 0.0),
])
def test_a_look_replaces_the_previous_one(window, first, then, colour, saturation):
    win, app = window
    _click(win, app, first)
    _click(win, app, then)
    fx = win.scene.layers[0].effects
    assert fx.color_effect == colour
    assert fx.saturation == pytest.approx(saturation)


def test_swapping_looks_reaches_the_running_pipeline(window):
    from tests.test_effects_browser import pump

    win, app = window
    win.start_engine()
    pump(app, 1.0)
    layer_id = win.scene.layers[0].id
    for label, preset, saturation in (("Sepia", "sepia", 1.0), ("Warm", "none", 1.12),
                                      ("Heat camera", "heat", 1.0), ("Original", "none", 1.0)):
        _click(win, app, label)
        rack = win.engine._layer_chains[layer_id].effects
        assert rack["style"].get_property("preset").value_nick == preset, label
        # On the GPU canvas a plain colour look runs in the GPU shader and the
        # CPU element stays neutral; either way the value must be applied once.
        cpu = rack["balance"].get_property("saturation")
        gpu = (rack["gl_balance"].get_property("uniforms").get_value("saturation")
               if "gl_balance" in rack else 1.0)
        applied = gpu if abs(cpu - 1.0) < 1e-6 else cpu
        assert applied == pytest.approx(saturation, abs=1e-5), label
        assert abs(cpu - 1.0) < 1e-6 or abs(gpu - 1.0) < 1e-6, f"{label}: applied twice"
    win.stop_engine()


def test_the_selected_tile_follows_the_applied_look(window):
    win, app = window
    _click(win, app, "Sepia")
    _click(win, app, "Warm")
    win._select_current_effect_tile()
    row = win.effect_grid.currentRow()
    assert win._effect_entries[row]["label"] == "Warm"


# ------------------------------------------------- tile previews off the GUI
def test_drawing_tiles_never_blocks_the_window(window):
    """Two mask tiles per tick on the GUI thread blocked it ~0.4 s at a time
    and froze the camera through the GIL; ticks now only hand work over."""
    import time

    from tests.test_effects_browser import open_effects, pump, show_category

    win, app = window
    open_effects(win, app)
    show_category(win, "Masks")
    longest = 0.0
    deadline = time.monotonic() + 40
    while win._thumb_queue and time.monotonic() < deadline:
        started = time.monotonic()
        win._render_pending_thumbs()
        longest = max(longest, time.monotonic() - started)
        app.processEvents()
        time.sleep(0.01)
    assert not win._thumb_queue, "the mask tiles never finished"
    assert longest < 0.08, f"a tick blocked the window for {longest * 1000:.0f} ms"
    missing = [win.effect_grid.item(row).text() for row in range(win.effect_grid.count())
               if win.effect_grid.item(row).icon().isNull()]
    assert len(missing) <= 1, missing      # "No filter" has no picture of its own


def test_tiles_of_the_built_in_sample_are_reused_from_disk(tmp_path, monkeypatch):
    from anycam import effect_thumbs

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    entry = {"label": "Sepia", "preview": {"color_effect": "sepia"}}
    sample = effect_thumbs.default_sample()
    first = effect_thumbs.tile_picture("Looks", entry, sample, True)
    assert first is not None
    # Only the thumbnail folder: the effect chain caches frame overlays too.
    cached = list((tmp_path / "cache").rglob("effect-thumbs/*.png"))
    assert len(cached) == 1
    monkeypatch.setattr(effect_thumbs, "render_thumbnail",
                        lambda *a, **k: pytest.fail("should come from the disk cache"))
    again = effect_thumbs.tile_picture("Looks", entry, sample, True)
    assert again.size == first.size
    # A live camera picture is never written to disk.
    monkeypatch.undo()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache2"))
    effect_thumbs.tile_picture("Looks", entry, sample, False)
    assert not list((tmp_path / "cache2").rglob("effect-thumbs/*.png"))


# ------------------------------------------------------- QoS and colour looks
def _layer_engine():
    from anycam.model import Layer, OutputConfig, Project, Scene
    from anycam.pipeline import AnyCamEngine

    project = Project(name="qos")
    project.output = OutputConfig(canvas_w=640, canvas_h=360, fps=30)
    scene = Scene("scene_qos", "QoS")
    scene.add_layer(Layer("cam", "test", "Cam", source="snow",
                          box=(0.05, 0.05, 0.9, 0.9), z=1))
    project.scenes = [scene]
    project.active_scene = scene.id
    return AnyCamEngine(project, preview=True), scene.layers[0]


def test_layer_chain_filters_never_drop_frames_for_qos():
    """gdkpixbufoverlay with QoS on turned 60 frames in into 0 out while a
    colour look was active (the mixer reported the extra latency)."""
    from gi.repository import Gst

    engine, layer = _layer_engine()
    engine.build()
    try:
        chain = engine._layer_chains[layer.id]

        def walk(element):
            yield element
            if isinstance(element, Gst.Bin):
                for child in element.iterate_recurse():
                    yield child

        checked = 0
        for element in chain.elements:
            for item in ([element] if not isinstance(element, Gst.Bin)
                         else list(element.children)):
                klass = item.get_factory().get_metadata("klass") if item.get_factory() else ""
                if item.find_property("qos") is not None and "Sink" not in klass:
                    assert item.get_property("qos") is False, item.get_factory().get_name()
                    checked += 1
        assert checked >= 5
        assert "frame" in chain.effects
        assert chain.effects["frame"].get_property("qos") is False
    finally:
        engine.stop()


def test_a_colour_look_keeps_the_layer_flowing():
    import copy
    import time

    from gi.repository import Gst

    engine, layer = _layer_engine()
    engine.build()
    out = [0]
    engine._layer_chains[layer.id].output_pad.add_probe(
        Gst.PadProbeType.BUFFER, lambda *_: out.__setitem__(0, out[0] + 1) or Gst.PadProbeReturn.OK)
    engine.start()
    try:
        def run(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                engine.pump(20)
                engine.deliver_frame()

        run(1.0)
        look = copy.deepcopy(layer.effects)
        look.saturation, look.contrast = 0.0, 1.1          # Mono
        assert engine.set_layer_effects(layer.id, look)
        before = out[0]
        run(1.5)
        assert out[0] - before > 30, f"only {out[0] - before} frames in 1.5 s with a look on"
    finally:
        engine.stop()


# ----------------------------------------- processors on live sources
def _queue_and_sink(bin_):
    queue = next(e for e in bin_.children if e.get_factory().get_name() == "queue")
    sink = next(e for e in bin_.children if e.get_factory().get_name() == "appsink")
    return queue, sink


@pytest.mark.parametrize("live", [True, False])
def test_a_processor_takes_the_newest_frame_from_a_live_source_only(live):
    """Back-pressure from a busy mask blocked the camera thread: camera frames
    were dropped, videorate duplicated (394 in 7 s) and the mask showed 7-19
    pictures a second.  A frozen image still needs the back-pressure."""
    from anycam import processors

    bin_, runner = processors.build_processor_bin("mask:robot", 0.85, f"slot_{live}",
                                                  320, 180, 30, live=live)
    try:
        queue, sink = _queue_and_sink(bin_)
        assert (queue.get_property("leaky").value_nick == "downstream") is live
        assert sink.get_property("drop") is live
    finally:
        runner.stop()


def test_camera_layers_get_live_slots_and_images_do_not():
    from anycam.model import Layer
    from anycam.sources import LIVE_KINDS, SourceChain, _finish_chain

    assert {"camera", "screen", "phone", "network"} <= LIVE_KINDS
    assert "image" not in LIVE_KINDS and "test" not in LIVE_KINDS
    for kind, expected in (("camera", True), ("image", False)):
        chain = SourceChain()
        chain.effects = {}
        _finish_chain(chain, 320, 180, 30, layer=Layer("l", kind, "L"))
        assert chain.effects["slot"].live is expected
        assert chain.effects["slot2"].live is expected
        for cleanup in chain.cleanup:
            cleanup()


# --------------------------------------------- videorate on live sources
def test_live_sources_get_a_drop_only_videorate():
    """With an effect slower than the frame rate, a duplicating videorate fed
    the source thread copies of the same frame: Distort showed 4 new frames in
    2 s (51 out).  A live source may only drop; the mixer repeats by itself."""
    from anycam.sources import SourceChain, _fit_letterbox

    for live in (True, False):
        chain = SourceChain()
        _fit_letterbox(chain, 1920, 1080, 960, 540, live=live)
        rate = next(e for e in chain.elements if e.get_factory().get_name() == "videorate")
        assert rate.get_property("drop-only") is live


@pytest.mark.parametrize("kind, live", [("camera", True), ("network", True),
                                        ("image", False), ("test", False)])
def test_the_layer_tail_marks_live_kinds(kind, live):
    from anycam.model import Layer
    from anycam.sources import layer_tail

    chain = layer_tail(Layer("l", kind, "L"), (1920, 1080), 960, 540, 30, "test-channel")
    rates = [e for e in chain.elements if e.get_factory().get_name() == "videorate"]
    assert rates, "no videorate in the tail"
    assert all(rate.get_property("drop-only") is live for rate in rates)
    for cleanup in chain.cleanup:
        cleanup()
