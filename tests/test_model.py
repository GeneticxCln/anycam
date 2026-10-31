import json

import pytest

from anycam.model import (
    AudioConfig, EditorConfig, Effects, Layer, OutputConfig, PlaylistConfig,
    PlaylistItem, Project, Scene, default_project, load_scene_preset, new_id,
    camera_grid_scene, save_scene_preset, two_camera_scene,
)


def test_default_project_has_layered_scene():
    project = default_project()
    scene = project.scene
    assert scene.id == project.active_scene
    kinds = [layer.kind for layer in scene.ordered()]
    assert kinds[0] == "camera"
    assert kinds.count("text") >= 2
    # overlays must sort above the camera
    assert scene.ordered()[-1].z > scene.ordered()[0].z


def test_project_json_roundtrip(tmp_path):
    project = default_project("Roundtrip")
    project.output = OutputConfig(canvas_w=1280, canvas_h=720, fps=25, rtmp_url="rtmp://localhost/live")
    project.audio = AudioConfig(mic_device="mic", mic_gain_db=3.5, mic_muted=True)
    path = project.save(tmp_path / "project.json")
    loaded = Project.load(path)
    assert loaded.name == "Roundtrip"
    assert loaded.output.canvas_w == 1280
    assert loaded.output.rtmp_url == "rtmp://localhost/live"
    assert loaded.audio.mic_gain_db == 3.5
    assert loaded.audio.mic_muted is True
    assert len(loaded.scene.layers) == len(project.scene.layers)


def test_effects_and_playlist_are_serialised():
    layer = Layer(new_id("layer"), "video", "Clip", source="/tmp/a.mp4",
                  box=(0.1, 0.2, 0.3, 0.4), crop=(0.05, 0.0, 0.1, 0.15),
                  effects=Effects(brightness=0.2, blur=4.0, chroma_enabled=True),
                  playlist=[PlaylistItem("/tmp/a.png", 3.0)])
    data = layer.to_dict()
    restored = Layer.from_dict(json.loads(json.dumps(data)))
    assert restored.effects.brightness == 0.2
    assert restored.effects.chroma_enabled is True
    assert restored.playlist[0].source == "/tmp/a.png"
    assert restored.box == (0.1, 0.2, 0.3, 0.4)
    assert restored.crop == (0.05, 0.0, 0.1, 0.15)


def test_scene_preset_roundtrip(tmp_path):
    scene = Scene("s1", "Desk", layers=[Layer("l1", "camera", "Cam", source="/dev/video0")])
    path = save_scene_preset(scene, tmp_path)
    loaded = load_scene_preset(path)
    assert loaded.name == "Desk"
    assert loaded.layers[0].source == "/dev/video0"


def test_two_camera_scene_is_side_by_side():
    scene = two_camera_scene("/dev/video2", "/dev/video3")
    assert scene.name == "Two cameras"
    cameras = [layer for layer in scene.layers if layer.kind == "camera"]
    assert [layer.source for layer in cameras] == ["/dev/video2", "/dev/video3"]
    assert cameras[0].box == (0.0, 0.0, 0.5, 1.0)
    assert cameras[1].box == (0.5, 0.0, 0.5, 1.0)
    assert cameras[1].z > cameras[0].z
    restored = Scene.from_dict(scene.to_dict())
    assert [layer.box for layer in restored.layers] == [
        (0.0, 0.0, 0.5, 1.0), (0.5, 0.0, 0.5, 1.0)]


def test_camera_grid_scene_fills_four_slots():
    scene = camera_grid_scene(["/dev/video2", "/dev/video3",
                               "/dev/video4", "/dev/video5"])
    assert [layer.source for layer in scene.layers] == [
        "/dev/video2", "/dev/video3", "/dev/video4", "/dev/video5"]
    assert [layer.box for layer in scene.layers] == [
        (0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 0.5, 0.5),
        (0.0, 0.5, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5)]
    restored = Scene.from_dict(scene.to_dict())
    assert len(restored.layers) == 4


def test_camera_grid_scene_leaves_configured_slots_empty():
    scene = camera_grid_scene(["/dev/video2", "/dev/video3", "", "/dev/video5"])
    assert [layer.source for layer in scene.layers] == [
        "/dev/video2", "/dev/video3", "/dev/video5"]
    assert [layer.box for layer in scene.layers] == [
        (0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 0.5, 0.5),
        (0.5, 0.5, 0.5, 0.5)]


def test_camera_grid_scene_needs_a_device():
    with pytest.raises(ValueError, match="at least one device"):
        camera_grid_scene(["", ""])


def test_layer_camera_capture_overrides_roundtrip(tmp_path):
    project = default_project("Modes")
    layer = next(l for l in project.scene.layers if l.kind == "camera")
    layer.camera_width, layer.camera_height, layer.camera_fps = 1280, 720, 30
    loaded = Project.load(project.save(tmp_path / "modes.json"))
    camera = next(l for l in loaded.scene.layers if l.id == layer.id)
    assert (camera.camera_width, camera.camera_height, camera.camera_fps) == (1280, 720, 30)


def test_editor_and_scene_playlist_roundtrip(tmp_path):
    project = default_project("State")
    project.editor = EditorConfig(snap_enabled=False, snap_px=12.0)
    project.scene.playlist = PlaylistConfig(
        layer_ids=[layer.id for layer in project.scene.layers[:2]],
        interval=2.5, loop=False, index=1,
    )
    loaded = Project.load(project.save(tmp_path / "state.json"))
    assert loaded.editor.snap_enabled is False
    assert loaded.editor.snap_px == 12.0
    playlist = loaded.scene.playlist
    assert playlist.layer_ids == [layer.id for layer in project.scene.layers[:2]]
    assert playlist.interval == 2.5
    assert playlist.loop is False
    assert playlist.index == 1


def test_missing_state_fields_use_defaults():
    loaded = Project.from_dict({"scenes": [{"id": "s", "name": "Old"}]})
    assert loaded.editor.snap_enabled is True
    assert loaded.editor.snap_px == 6.0
    assert loaded.scene.playlist.layer_ids == []
    assert loaded.scene.playlist.loop is True


def test_audio_mixing_fields_roundtrip(tmp_path):
    project = default_project("Audio")
    project.audio.media_file = "/tmp/music.wav"
    project.audio.media_gain_db = -6.0
    project.audio.mic_gain_db = 4.5
    loaded = Project.load(project.save(tmp_path / "audio.json"))
    assert loaded.audio.media_file == "/tmp/music.wav"
    assert loaded.audio.media_gain_db == -6.0
    assert loaded.audio.mic_gain_db == 4.5
