"""Camera mode selection rules (the 1080p60 MJPG path)."""
from anycam import config, sources
from anycam.model import Layer
from anycam.sources import CameraMode, camera_capture_request, choose_from_modes


def _streamcam_modes() -> list[CameraMode]:
    """The 1920x1080 modes the Logitech StreamCam actually advertises."""
    return [
        CameraMode("YUYV", 1920, 1080, 30),
        CameraMode("YUYV", 1920, 1080, 24),
        CameraMode("MJPG", 1920, 1080, 60),
        CameraMode("MJPG", 1920, 1080, 50),
        CameraMode("MJPG", 1920, 1080, 30),
        CameraMode("NV12", 1920, 1080, 30),
        CameraMode("NV12", 1920, 1080, 25),
    ]


def test_1080p60_selects_mjpg():
    mode = choose_from_modes(_streamcam_modes(), 1920, 1080, 60)
    assert (mode.pixel_format, mode.width, mode.height, mode.fps) == ("MJPG", 1920, 1080, 60)


def test_exact_size_beats_other_sizes():
    modes = [CameraMode("MJPG", 1280, 720, 60), CameraMode("MJPG", 1920, 1080, 30)]
    mode = choose_from_modes(modes, 1920, 1080, 60)
    assert (mode.width, mode.height, mode.fps) == (1920, 1080, 30)


def test_60_request_prefers_higher_rate_over_30():
    modes = [CameraMode("YUYV", 1920, 1080, 30), CameraMode("MJPG", 1920, 1080, 50)]
    mode = choose_from_modes(modes, 1920, 1080, 60)
    assert mode.fps == 50


def test_mjpg_preferred_at_same_rate():
    modes = [CameraMode("YUYV", 1920, 1080, 30), CameraMode("NV12", 1920, 1080, 30),
             CameraMode("MJPG", 1920, 1080, 30)]
    mode = choose_from_modes(modes, 1920, 1080, 30)
    assert mode.pixel_format == "MJPG"


def test_layer_capture_overrides_resolve_into_a_mode():
    layer = Layer("l", "camera", "Cam", source="/dev/video2",
                  camera_width=1280, camera_height=720, camera_fps=30)
    assert camera_capture_request(layer, 1920, 1080, 60) == (1280, 720, 30)
    plain = Layer("l", "camera", "Cam", source="/dev/video2")
    assert camera_capture_request(plain, 1920, 1080, 60) == (1920, 1080, 60)
    modes = [CameraMode("MJPG", 1920, 1080, 60),
             CameraMode("MJPG", 1280, 720, 30)]
    request = camera_capture_request(layer, 1920, 1080, 60)
    mode = choose_from_modes(modes, *request)
    assert (mode.width, mode.height, mode.fps) == (1280, 720, 30)


def test_empty_probe_returns_requested_mode():
    mode = choose_from_modes([], 1920, 1080, 60)
    assert (mode.width, mode.height, mode.fps) == (1920, 1080, 60)


def test_loopback_continuous_formats_use_yuyv(monkeypatch):
    monkeypatch.setattr(sources, "camera_formats",
                        lambda _device: ["BGR4", "YUYV", "YV12"])
    mode = sources._loopback_mode("/dev/video10", 640, 480, 30)
    assert mode == CameraMode("YUYV", 640, 480, 30)
    assert sources._gst_raw_format("YUYV") == "YUY2"


def test_loopback_without_a_raw_format_returns_none(monkeypatch):
    monkeypatch.setattr(sources, "camera_formats", lambda _device: ["MJPG"])
    assert sources._loopback_mode("/dev/video10", 640, 480, 30) is None


def test_default_camera_skips_anycam_loopback(monkeypatch):
    cameras = [
        config.CameraInfo("/dev/video10", "AnyCam", ["YUYV"]),
        config.CameraInfo("/dev/video2", "Desk Cam", ["MJPG"]),
    ]
    monkeypatch.setattr(config, "list_video_devices", lambda: cameras)
    monkeypatch.delenv("ANYCAM_CAMERA", raising=False)
    assert config.camera_device() == "/dev/video2"
