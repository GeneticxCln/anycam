"""Phase 4 tests: major studio feature upgrades."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anycam import content, effects, luts, processors, sources, web_source
from anycam.demo import build_demo_project
from anycam.engine import audio_helpers
from anycam.engine.outputs import vcam
from anycam.model import AudioConfig, Effects, Layer, OutputConfig, Scene, empty_project, new_id
from anycam.pipeline import AnyCamEngine


def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ----------------------------------------------------------------- Chroma Key GPU + Spill
def test_chroma_spill_model_and_gpu_shader():
    fx = Effects.from_dict({"chroma_enabled": True, "chroma_spill": 1.8})
    assert fx.chroma_spill == 1.0
    assert sources.gpu_colour_is_equivalent(fx) is True
    assert "chroma_spill" in sources.BALANCE_SHADER
    assert "smoothstep" in sources.BALANCE_SHADER


def test_chroma_spill_ui_slider_syncs():
    _qapp()
    from anycam.gui_many import ManyCamWindow

    project = empty_project()
    layer = Layer(new_id("layer"), "test", "Test",
                  effects=Effects(chroma_enabled=True, chroma_spill=0.65))
    project.scene.add_layer(layer)
    win = ManyCamWindow(project)
    win.headless = True
    win._persist = False
    try:
        win.preview.select_layer(layer.id)
        win._sync_effects_controls()
        assert win._effect_sliders["chroma_spill"].value() == 65
        win._set_effect("chroma_spill", 0.4)
        assert layer.effects.chroma_spill == pytest.approx(0.4)
    finally:
        win.close()


# ----------------------------------------------------------------- Whiteboard Region Blur
def test_scene_blur_regions_roundtrip_and_post_compositor_blur():
    scene = Scene("s1", "Main", blur_regions=[(0.1, 0.2, 0.3, 0.4)])
    restored = Scene.from_dict(scene.to_dict())
    assert restored.blur_regions == [(0.1, 0.2, 0.3, 0.4)]

    project = build_demo_project("testsrc", 320, 180, 30)
    project.scene.blur_regions = [(0.2, 0.2, 0.5, 0.5)]
    engine = AnyCamEngine(project, preview=True).build()
    try:
        assert engine._blur_probe is not None
        assert engine.set_blur_regions([]) is True
        assert engine._blur_probe is None
        assert engine.set_blur_regions([(0.0, 0.0, 0.5, 0.5)]) is True
        assert engine._blur_probe is not None
    finally:
        engine.pipeline.set_state(engine.pipeline.get_state(0)[1].NULL)


def test_draw_panel_blur_tool_adds_and_undoes_blur_region():
    _qapp()
    from anycam.gui_many import ManyCamWindow

    win = ManyCamWindow(empty_project())
    win.headless = True
    win._persist = False
    try:
        assert "blur" in win.draw_tool_buttons
        win._draw_tool("blur")
        win._draw_stroke_finished({"tool": "blur", "points": [[0.1, 0.2], [0.5, 0.6]]})
        assert len(win.scene.blur_regions) == 1
        rx, ry, rw, rh = win.scene.blur_regions[0]
        assert (rx, ry, rw, rh) == pytest.approx((0.1, 0.2, 0.4, 0.4))
        win._draw_undo()
        assert win.scene.blur_regions == []
    finally:
        win.close()


# ----------------------------------------------------------------- Web Page Source
def test_web_url_validation_and_layer_creation():
    assert web_source.validate_web_url("https://example.com/overlay") == ""
    assert web_source.validate_web_url("http://localhost:8080/alerts") == ""
    assert web_source.validate_web_url("file:///etc/passwd") != ""
    assert web_source.validate_web_url("javascript:alert(1)") != ""
    assert web_source.validate_web_url("") != ""

    layer = Layer.from_dict({"id": "w1", "kind": "web", "name": "Alerts",
                             "source": "https://example.com", "custom_css": "body{opacity:0.9}"})
    assert layer.custom_css == "body{opacity:0.9}"


def test_web_generator_fallback_frame_when_unavailable(monkeypatch):
    monkeypatch.setattr(web_source, "available", lambda: False)
    gen = web_source.WebPageGenerator("https://example.com", 320, 180)
    try:
        frame = gen.frame(0.0)
        assert frame.shape == (180, 320, 4)
        assert frame.dtype == np.uint8
    finally:
        gen.close()


# ----------------------------------------------------------------- Studio Mode Live Preview
def test_studio_mode_second_preview_panel_and_engine_size_overrides():
    _qapp()
    from anycam.gui_many import ManyCamWindow

    project = empty_project()
    scene2 = Scene(new_id("scene"), "Scene 2")
    scene2.add_layer(Layer(new_id("layer"), "test", "Pattern", z=1))
    project.scenes.append(scene2)

    eng = AnyCamEngine(project, scene=scene2, preview=True, canvas="cpu",
                       canvas_w=640, canvas_h=360, fps=20)
    assert (eng.canvas_w, eng.canvas_h, eng.fps) == (640, 360, 20)

    win = ManyCamWindow(project)
    win.headless = True
    win._persist = False
    try:
        assert not win.studio_preview_panel.isVisible()
        win.studio_check.setChecked(True)
        assert not win.studio_preview_panel.isHidden()
        assert win.studio_preview.scene is scene2
        win.studio_check.setChecked(False)
        assert win.studio_preview_panel.isHidden()
    finally:
        win.close()


# ----------------------------------------------------------------- Multiple Virtual Cameras
def test_multi_vcam_model_and_pipewire_properties():
    out = OutputConfig.from_dict({
        "vcam_device": "/dev/video10",
        "vcam_extra_devices": ["/dev/video11", "  /dev/video12  "],
        "vcam_pipewire_count": 3,
    })
    assert out.vcam_extra_devices == ["/dev/video11", "/dev/video12"]
    assert out.vcam_pipewire_count == 3

    props = vcam.pipewire_stream_properties(description="AnyCam 2", node_name="anycam-2")
    assert props.get_value("node.name") == "anycam-2"
    assert props.get_value("node.description") == "AnyCam 2"


# ----------------------------------------------------------------- Mic Echo Cancellation
def test_mic_echo_cancel_config_and_elements(monkeypatch):
    audio = AudioConfig.from_dict({"mic_echo_cancel": True, "mic_denoise": "moderate"})
    assert audio.mic_echo_cancel is True
    assert audio_helpers.mic_dsp_wanted(AudioConfig(mic_denoise="off", mic_echo_cancel=True)) is True
    assert "echo cancel" in audio_helpers.describe_mic_dsp(audio)

    if audio_helpers.echo_cancel_available():
        els = audio_helpers.mic_dsp_elements(audio, echo_probe_name="mic_echo_probe")
        dsp = els[-1]
        assert dsp.get_property("echo-cancel") is True
        assert dsp.get_property("probe") == "mic_echo_probe"


# ----------------------------------------------------------------- LibreOffice PPTX/ODP Slides
def test_presentation_pages_reports_missing_libreoffice(tmp_path, monkeypatch):
    deck = tmp_path / "talk.pptx"
    deck.write_bytes(b"fake")
    monkeypatch.setattr(content.shutil, "which", lambda _name: None)
    pages, err = content.presentation_pages(str(deck))
    assert pages == []
    assert "LibreOffice" in err


# ----------------------------------------------------------------- 3D .cube LUTs
def test_cube_lut_parser_and_processor(tmp_path):
    # 2x2x2 inverting LUT (R, G, B -> 1-R, 1-G, 1-B)
    cube_lines = ["TITLE \"Invert\"", "LUT_3D_SIZE 2"]
    for b in (0.0, 1.0):
        for g in (0.0, 1.0):
            for r in (0.0, 1.0):
                cube_lines.append(f"{1.0 - r:.1f} {1.0 - g:.1f} {1.0 - b:.1f}")
    lut_file = tmp_path / "invert.cube"
    lut_file.write_text("\n".join(cube_lines) + "\n", encoding="utf-8")

    lut = luts.load_cube(lut_file)
    assert lut.title == "Invert"
    assert lut.size == 2

    src = Image.new("RGBA", (16, 16), (255, 0, 128, 255))
    proc = processors.make_processor(f"lut:{lut_file}", 1.0)
    out = proc.process(src, 0.0)
    px = out.getpixel((8, 8))
    assert px[0] <= 2 and px[1] >= 253 and abs(px[2] - 127) <= 2 and px[3] == 255

    # Discovery in effects registry
    count = effects.reload_luts(tmp_path)
    assert count == 1
    assert "lut-invert" in effects.FILTERS
    # Clean up global registry
    effects.reload_luts(tmp_path / "empty")


# ----------------------------------------------------------------- Transient + Spectral Denoiser
def test_transient_denoiser_suppresses_keyboard_clicks_and_builds_gst_elements():
    from anycam import audio_denoise

    denoiser = audio_denoise.TransientSpectralDenoiser(sample_rate=48000, strength=0.9)
    rng = np.random.default_rng(7)
    # Prime noise floor with quiet room tone (0.01 amplitude), then inject a sharp HF keyboard click.
    quiet = (rng.standard_normal(4800) * 0.01).astype(np.float32)
    denoiser.process_chunk(quiet)
    click = (rng.standard_normal(2048) * 0.01).astype(np.float32)
    # Sharp broadband impulse at sample 600..616 (mechanical switch click)
    click[600:616] += np.array([0.85, -0.75, 0.70, -0.65, 0.55, -0.50, 0.40, -0.35] * 2,
                               dtype=np.float32)
    out = denoiser.process_chunk(click)
    assert out.shape == click.shape
    assert float(np.max(np.abs(out))) < float(np.max(np.abs(click))) * 0.65

    els = audio_denoise.denoise_elements("test_trans")
    assert len(els) == 4
    assert audio_helpers.describe_voice_fx(AudioConfig(mic_neural_denoise=True)) == "transient click suppressor"


# ----------------------------------------------------------------- 3D Head Pose + Perspective Masks
def test_3d_head_pose_expression_and_perspective_mask_warp():
    from anycam import face, masks

    # Turned right (nose shifted right of eye midpoint) + rolled slightly.
    lms = ((0.35, 0.40), (0.65, 0.42), (0.57, 0.54), (0.40, 0.68), (0.60, 0.70))
    pose = face.estimate_head_pose(lms)
    assert pose.yaw > 0.2
    assert pose.roll > 0.03

    mouth_open, smile = face.estimate_expression(None, lms)
    assert 0.0 <= mouth_open <= 1.0
    assert 0.0 <= smile <= 1.0

    canvas = Image.new("RGBA", (320, 240), (20, 20, 20, 255))
    masks.place(
        canvas, "cyber", 0.0,
        left_eye=(110.0, 96.0), right_eye=(210.0, 100.0),
        mouth=(160.0, 165.0), nose=(182.0, 130.0), mouth_open=0.5,
    )
    arr = np.asarray(canvas)
    assert int(np.sum(arr[..., :3] != 20)) > 500


# ----------------------------------------------------------------- Game / Window Capture & NDI
def test_game_capture_pipewire_discovery_and_ndi_detection(monkeypatch):
    import json
    from anycam import game_capture, ndi

    fake_dump = json.dumps([
        {
            "id": 77,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Video/Source",
                    "node.name": "obs-vkcapture-eldenring",
                    "node.description": "Elden Ring (vkcapture)",
                }
            },
        },
        {
            "id": 78,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Video/Source",
                    "node.name": "anycam",
                    "node.description": "AnyCam",
                }
            },
        },
    ])

    class FakeRes:
        returncode = 0
        stdout = fake_dump

    monkeypatch.setattr(game_capture.shutil, "which", lambda _n: "/usr/bin/pw-dump")
    monkeypatch.setattr(game_capture.subprocess, "run", lambda *a, **k: FakeRes())
    nodes = game_capture.list_pipewire_game_nodes()
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == 77
    assert nodes[0]["is_game"] is True

    chain = game_capture.build_game_chain("pw:77", 320, 180, 30)
    assert chain.tail is not None

    # NDI cleanly reports availability and refuses gracefully when plugin absent.
    if not ndi.src_available():
        with pytest.raises(RuntimeError, match="gst-plugin-ndi"):
            ndi.build_ndi_chain("STUDIO (Cam)", 320, 180, 30)


# ----------------------------------------------------------------- Per-Output Resolution / FPS
def test_per_output_resolution_and_fps_overrides():
    out = OutputConfig.from_dict({
        "canvas_w": 1920, "canvas_h": 1080, "fps": 60,
        "record_w": 1281, "record_h": 721, "record_fps": 30,
        "stream_w": 1280, "stream_h": 720, "stream_fps": 30,
        "vcam_w": 1280, "vcam_h": 720, "vcam_fps": 30,
        "ndi_name": "AnyCam Studio",
    })
    assert (out.record_w, out.record_h, out.record_fps) == (1280, 720, 30)
    assert (out.stream_w, out.stream_h, out.stream_fps) == (1280, 720, 30)
    assert (out.vcam_w, out.vcam_h, out.vcam_fps) == (1280, 720, 30)
    assert out.ndi_name == "AnyCam Studio"

    project = build_demo_project("testsrc", 640, 360, 30)
    project.output.stream_w = 320
    project.output.stream_h = 180
    project.output.stream_fps = 15
    engine = AnyCamEngine(project, preview=True)
    assert engine._output_format("rtmp") == (320, 180, 15)
    assert engine._output_format("record") == (640, 360, 30)
    scale_els = engine._output_scale_elements("rtmp", "rtmp")
    assert len(scale_els) == 3
    assert engine._output_scale_elements("record", "record") == []

