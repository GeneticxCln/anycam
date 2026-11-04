"""Runtime configuration, paths and device discovery for AnyCam."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
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


def camera_identity(device: str, *, by_id_dir: Path | None = None) -> str:
    """Return the stable ``/dev/v4l/by-id`` name for a V4L2 node.

    A USB camera usually comes back as a different ``/dev/videoN`` after a
    replug.  The by-id symlink carries the serial number and udev points it at
    the new node, so storing that path is what lets a project reconnect without
    the user picking the camera again (audit B46).
    """
    if not device:
        return device
    candidates = Path(by_id_dir) if by_id_dir is not None else Path("/dev/v4l/by-id")
    if not candidates.is_dir():
        return device
    try:
        target = Path(device).resolve(strict=True)
        links = sorted(
            candidates.iterdir(),
            key=lambda link: ("video-index0" not in link.name, link.name),
        )
    except OSError:
        return device
    for link in links:
        try:
            if link.resolve() == target:
                return str(link)
        except OSError:
            continue
    return device


def camera_device(preferred: str | None = None) -> str:
    """Pick a usable camera: env override, then preferred, then first capture node."""
    for candidate in (preferred, os.environ.get("ANYCAM_CAMERA")):
        if candidate and Path(candidate).exists():
            return candidate
    for cam in list_video_devices():
        # Never pick AnyCam's own v4l2loopback output as the default camera;
        # doing so would feed the compositor back into itself.
        if cam.formats and "anycam" not in (cam.name or "").lower() \
                and cam.device != loopback_device():
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


def ensure_vcam_device(device: str | None = None, *, prompt: bool = True) -> str:
    """Make the v4l2loopback device exist, creating it when necessary.

    A virtual-camera app should not ask the user to run modprobe by hand.  The
    module needs root, so the first time we ask through ``pkexec`` (the normal
    polkit prompt) and only fall back to an explicit instruction when that is
    impossible or declined.  Returns "" on success, a reason otherwise.
    """
    dev = device or loopback_device()
    if Path(dev).exists():
        return ""
    match = re.search(r"video(\d+)$", dev)
    if match is None:
        return f"virtual camera device {dev} does not exist"
    number = match.group(1)
    manual = (f"sudo modprobe v4l2loopback video_nr={number} "
              f"card_label=AnyCam exclusive_caps=1")
    if (not prompt or shutil.which("pkexec") is None
            or shutil.which("modprobe") is None):
        return f"{dev} is missing; load v4l2loopback first: {manual}"
    try:
        result = subprocess.run(
            ["pkexec", "modprobe", "v4l2loopback", f"video_nr={number}",
             "card_label=AnyCam", "exclusive_caps=1"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # noqa: BLE001
        return f"could not set up the virtual camera: {exc}"
    # The device node can take a moment to appear after the module loads.
    for _ in range(40):
        if Path(dev).exists():
            return ""
        time.sleep(0.05)
    detail = (result.stderr or result.stdout or "").strip()
    if result.returncode in (126, 127):
        return f"setting up the virtual camera was cancelled. Run: {manual}"
    return (f"virtual camera {dev} is still missing"
            + (f": {detail}" if detail else "") + f". Run: {manual}")


def vcam_exclusive_caps(
        path: str | None = "/sys/module/v4l2loopback/parameters/exclusive_caps"
) -> bool | None:
    """Whether the loaded module announces capture-only, as Chromium needs.

    Chromium-based clients (Discord, Chrome) enumerate v4l2 nodes through udev
    and ignore one that advertises capture and output together, which is what
    the module default (``exclusive_caps=0``) produces.  ``None`` means the
    parameter cannot be read (module not loaded).
    """
    try:
        values = Path(path).read_text() if path else ""
    except OSError:
        return None
    return values.split(",")[0].strip() == "Y"


def reload_vcam_module(device: str | None = None, *,
                       prompt: bool = True) -> str:
    """Reload v4l2loopback with ``exclusive_caps=1`` (Chromium needs it).

    The caller must close every handle it holds on ``device`` first: the
    module cannot be unloaded while the node is open.  Any other application
    still holding it makes the unload fail, which is reported instead of
    retried.  Returns "" on success, a reason otherwise.
    """
    dev = device or loopback_device()
    match = re.search(r"video(\d+)$", dev)
    if match is None:
        return f"virtual camera device {dev} is not a video node"
    number = match.group(1)
    load = (f"modprobe v4l2loopback video_nr={number} card_label=AnyCam "
            f"exclusive_caps=1")
    manual = f"sudo modprobe -r v4l2loopback && sudo {load}"
    if shutil.which("modprobe") is None:
        return f"modprobe is missing; run: {manual}"
    if not prompt or shutil.which("pkexec") is None:
        return f"reloading needs root; run: {manual}"
    try:
        result = subprocess.run(
            # One prompt for the unload and the load: pkexec authorises the
            # shell, which runs both steps only if the unload succeeds.
            ["pkexec", "/bin/sh", "-c",
             f"modprobe -r v4l2loopback && {load}"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # noqa: BLE001
        return f"could not reload v4l2loopback: {exc}"
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        reason = lines[-1] if lines else f"pkexec exited {result.returncode}"
        if result.returncode in (126, 127):
            reason = ("authorization was cancelled or no polkit agent is "
                      "running")
        return f"{reason}. Run: {manual}"
    # The parameter and the node are recreated by udev; give them a moment.
    for _ in range(60):
        if Path(dev).exists() and vcam_exclusive_caps() is True:
            return ""
        time.sleep(0.05)
    return (f"v4l2loopback reloaded but {dev} still reports "
            f"exclusive_caps={vcam_exclusive_caps()}. Run: {manual}")


def vcam_available(device: str | None = None) -> bool:
    dev = device or loopback_device()
    if not Path(dev).exists():
        return False
    code, out = run(["v4l2-ctl", "-d", dev, "--info"])
    return code == 0


def vcam_consumers(device: str | None = None, proc: str = "/proc") -> list[str]:
    """Names of the processes that currently have the loopback node open.

    Best effort: another user's descriptors are unreadable and are skipped.
    The GUI uses this both to tell the user when an app opened the node before
    AnyCam was feeding it (that open fails with ``EIO`` and needs a re-select)
    and to avoid converting frames nobody is watching.
    """
    target = os.path.realpath(device or loopback_device())
    names: list[str] = []
    mine = os.getpid()
    try:
        entries = list(Path(proc).iterdir())
    except OSError:
        return names
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == mine:
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        opened = False
        for fd in fds:
            try:
                if os.readlink(fd) == target:
                    opened = True
                    break
            except OSError:
                continue
        if not opened:
            continue
        try:
            name = (entry / "comm").read_text().strip()
        except OSError:
            name = ""
        if name and name not in names:
            names.append(name)
    return names


# The virtual camera keeps one camera-sized mode while the program canvas
# may be 4K or change while a consumer is watching.  Re-negotiating the
# loopback device for every canvas change closes the node and leaves apps
# (Discord) on a dead handle; scale into this fixed mode instead.
VCAM_MAX_W = 1920
VCAM_MAX_H = 1080


def vcam_output_size(width: int, height: int) -> tuple[int, int]:
    """Fixed virtual-camera mode for a canvas, preserving its aspect ratio."""
    width = max(2, int(width))
    height = max(2, int(height))
    scale = min(1.0, VCAM_MAX_W / width, VCAM_MAX_H / height)
    out_w = max(2, int(round(width * scale)))
    out_h = max(2, int(round(height * scale)))
    # YUYV needs even dimensions in both axes.
    return out_w // 2 * 2, out_h // 2 * 2


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
