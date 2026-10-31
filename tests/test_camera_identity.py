"""Stable camera identity for replug recovery (audit B46)."""
from anycam import config


def test_prefers_video_index0_symlink(tmp_path):
    target = tmp_path / "video2"
    target.touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-Cam-video-index1").symlink_to(target)
    (by_id / "usb-Cam-video-index0").symlink_to(target)

    assert config.camera_identity(str(target), by_id_dir=by_id) == str(
        by_id / "usb-Cam-video-index0")


def test_returns_the_node_when_no_by_id_link_matches(tmp_path):
    target = tmp_path / "video2"
    target.touch()
    by_id = tmp_path / "by-id"
    by_id.mkdir()

    assert config.camera_identity(str(target), by_id_dir=by_id) == str(target)


def test_keeps_a_missing_by_id_path_for_reconnect(tmp_path):
    missing = tmp_path / "by-id" / "usb-Cam-video-index0"

    assert config.camera_identity(str(missing), by_id_dir=tmp_path / "by-id") == str(missing)
