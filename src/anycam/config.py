"""Runtime configuration, paths and device discovery for AnyCam."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "AnyCam"
APP_ID = "anycam"
VERSION = "0.1.0"

CANVAS_W = 1920
CANVAS_H = 1080
CANVAS_FPS = 60


def _xdg(env: str, default: str) -> Path:
    value = os.environ.get(env)
    return Path(value) if value else Path.home() / default


def state_dir() -> Path:
    path = _xdg("XDG_STATE_HOME", ".local/state") / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    path = _xdg("XDG_CONFIG_HOME", ".config") / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = _xdg("XDG_CACHE_HOME", ".cache") / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def xdg_videos_dir() -> Path:
    """The user's video directory.

    ``XDG_VIDEOS_DIR`` is normally set by the desktop session, but it is not a
    standard variable to read blindly: the location lives in
    ``user-dirs.dirs``.  Honour the environment first, then that file.
    """
    env = os.environ.get("XDG_VIDEOS_DIR")
    if env:
        return Path(env).expanduser()
    config_file = _xdg("XDG_CONFIG_HOME", ".config") / "user-dirs.dirs"
    try:
        for line in config_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("XDG_VIDEOS_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                value = value.replace("$HOME", str(Path.home()))
                if value:
                    return Path(value).expanduser()
    except OSError:
        pass
    return Path.home() / "Videos"


def recordings_dir(base: str = "") -> Path:
    """Recording/snapshot directory, honouring the project's record_dir."""
    path = Path(base).expanduser() if base else xdg_videos_dir() / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def redact_url(url: str) -> str:
    """Hide a stream key before it reaches a report or a status bar."""
    if not url:
        return ""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return "***"
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments:
        segments[-1] = "***"
    return urlunsplit((parts.scheme, parts.netloc,
                       "/" + "/".join(segments) if segments else "", "", ""))


def project_path() -> Path:
    return config_dir() / "project.json"


def assets_dir() -> Path:
    """Runtime assets shipped inside the package, with a checkout fallback."""
    packaged = Path(__file__).resolve().parent / "assets"
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parent.parent.parent / "assets"


def has_command(name: str) -> bool:
    return shutil.which(name) is not None


def run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


@dataclass
class CameraInfo:
    device: str
    name: str = ""
    formats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"device": self.device, "name": self.name, "formats": self.formats}


def list_video_devices() -> list[CameraInfo]:
    """Enumerate V4L2 capture devices without importing GStreamer."""
    devices: list[CameraInfo] = []
    if not Path("/sys/class/video4linux").exists():
        return devices
    for entry in sorted(Path("/sys/class/video4linux").glob("video*")):
        dev = f"/dev/{entry.name}"
        name = (entry / "name").read_text(errors="replace").strip() if (entry / "name").exists() else ""
        code, out = run(["v4l2-ctl", "-d", dev, "--list-formats"])
        if code != 0 or not out.strip():
            continue
        formats = re.findall(r"\[\d+\]:\s+'([^']+)'", out)
        devices.append(CameraInfo(dev, name, formats))
    return devices


def camera_device(preferred: str | None = None) -> str:
    """Pick a usable camera: env override, then preferred, then first capture node."""
    for candidate in (preferred, os.environ.get("ANYCAM_CAMERA")):
        if candidate and Path(candidate).exists():
            return candidate
    for cam in list_video_devices():
        # Never pick AnyCam's own v4l2loopback output as the default camera;
        # doing so would feed the compositor back into itself.
        if cam.formats and "anycam" not in (cam.name or "").lower():
            return cam.device
    return "/dev/video0"


@dataclass
class AudioSource:
    name: str
    description: str
    is_monitor: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "monitor": self.is_monitor}


def list_audio_sources() -> list[AudioSource]:
    code, out = run(["pactl", "-f", "json", "list", "sources"])
    if code != 0:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    sources = []
    for item in data:
        props = item.get("properties") or {}
        name = item.get("name", "")
        description = item.get("description", "")
        is_monitor = (
            props.get("device.class") == "monitor"
            or name.endswith(".monitor")
            or description.startswith("Monitor of")
        )
        sources.append(AudioSource(name=name, description=description, is_monitor=is_monitor))
    return sources


def microphone() -> str:
    env = os.environ.get("ANYCAM_MIC")
    if env:
        return env
    sources = list_audio_sources()
    for source in sources:
        desc = (source.description or "").lower()
        if not source.is_monitor and any(k in desc for k in ("microphone", "mic", "yeti", "streamcam")):
            return source.name
    for source in sources:
        if not source.is_monitor:
            return source.name
    return ""


def loopback_device() -> str:
    """Return the first AnyCam/v4l2loopback node, or the conventional /dev/video10."""
    env = os.environ.get("ANYCAM_VCAM")
    if env:
        return env
    for cam in list_video_devices():
        if "anycam" in (cam.name or "").lower():
            return cam.device
    if Path("/dev/video10").exists():
        return "/dev/video10"
    return "/dev/video10"


def vcam_available() -> bool:
    dev = loopback_device()
    if not Path(dev).exists():
        return False
    code, out = run(["v4l2-ctl", "-d", dev, "--info"])
    return code == 0


def vcam_pixel_format(device: str) -> str:
    """Pick a GStreamer raw format the loopback device advertises.

    v4l2loopback supports YUYV on every build; NV12 is the fallback for
    devices that were created with a restricted format list.
    """
    code, out = run(["v4l2-ctl", "-d", device, "--list-formats"])
    if code == 0:
        formats = set(re.findall(r"\[\d+\]:\s+'([^']+)'", out))
        if "YUYV" in formats:
            return "YUY2"
        if "NV12" in formats:
            return "NV12"
    return "YUY2"


def system_summary() -> dict:
    code, gst = run(["gst-inspect-1.0", "--version"])
    return {
        "app": APP_NAME,
        "version": VERSION,
        "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}.{os.sys.version_info.micro}",
        "gstreamer": gst.splitlines()[0].strip() if code == 0 and gst else "unavailable",
        "camera": camera_device(),
        "microphone": microphone(),
        "vcam": loopback_device(),
        "vcam_available": vcam_available(),
        "session": os.environ.get("XDG_SESSION_TYPE", ""),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
    }
