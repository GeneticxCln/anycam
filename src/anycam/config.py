"""Runtime configuration, paths and device discovery for AnyCam."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import privileged

APP_NAME = "AnyCam"
APP_ID = "anycam"
VERSION = "1.1.0"

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
    return urlunsplit((parts.scheme, host_of(url),
                       "/" + "/".join(segments) if segments else "", "", ""))


def host_of(url: str) -> str:
    """``host[:port]`` of a URL, without any ``user:password@`` part.

    ``urlsplit(...).netloc`` keeps the credentials, so using it as a label or
    in a redacted URL leaked an RTSP camera's password.
    """
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.rpartition("@")[2]


_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")


def redact_text(text: str, *urls: str) -> str:
    """Remove stream secrets from a free-form message.

    Element errors (``rtmp2sink``, ``uridecodebin``) often quote the location
    they were given, stream key and camera password included.  Mask every
    given URL, the secret pieces it carries, and any ``user:password@`` that
    appears in the text on its own.
    """
    if not text:
        return text
    from urllib.parse import urlsplit

    for url in urls:
        if not url:
            continue
        text = text.replace(url, redact_url(url))
        parts = urlsplit(url)
        # The userinfo pattern below removes ``user:pw@`` wherever it appears;
        # this catches the bare password, but not a short one that would also
        # match ordinary words.
        if parts.password and len(parts.password) >= 4:
            text = text.replace(parts.password, "***")
        segments = [segment for segment in parts.path.split("/") if segment]
        # A short last segment ("live") is a path, not a key; masking it would
        # garble unrelated words in the message.
        if parts.scheme and segments and len(segments[-1]) >= 6:
            text = text.replace(segments[-1], "***")
    return _USERINFO.sub(lambda match: match.group("scheme"), text)


def screen_token_path() -> Path:
    return state_dir() / "screen-restore-token"


def load_screen_restore_token() -> str:
    """The portal restore token saved by an earlier launch, or ""."""
    try:
        return screen_token_path().read_text().strip()
    except OSError:
        return ""


def save_screen_restore_token(token: str) -> None:
    """Remember the portal's restore token (owner-only; it grants access).

    It lives beside the other state rather than in the project: it is a
    credential for this user's session, not part of a shareable project.
    """
    token = (token or "").strip()
    if not token:
        return
    path = screen_token_path()
    temp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(token + "\n")
        os.replace(temp, path)
    except OSError:
        pass


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
    devices = list_video_devices()
    own = loopback_device()  # once: it scans the devices again
    for cam in devices:
        # Never pick AnyCam's own v4l2loopback output as the default camera;
        # doing so would feed the compositor back into itself.
        if cam.formats and "anycam" not in (cam.name or "").lower() \
                and cam.device != own:
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


def screen_flip_method(value: str | None = None) -> str:
    """Orientation correction for portal screen captures.

    Automatic follows the ``image-orientation`` tag exposed by the
    compositor.  It deliberately does not force a rotation on KWin: rotating
    the captured source also flips AnyCam's preview and every vcam consumer.
    Use rotate-180, vertical-flip or none from the Output settings (or
    ANYCAM_SCREEN_FLIP) when a specific share needs correcting.
    """
    raw = os.environ.get("ANYCAM_SCREEN_FLIP", "") if value is None else value
    value = str(raw or "").strip().lower()
    aliases = {
        "": "automatic", "auto": "automatic", "180": "rotate-180",
        "rotate180": "rotate-180", "vertical": "vertical-flip",
        "vflip": "vertical-flip", "horizontal": "horizontal-flip",
        "hflip": "horizontal-flip", "none": "none",
    }
    method = aliases.get(value, value)
    allowed = {
        "automatic", "none", "clockwise", "rotate-180",
        "counterclockwise", "horizontal-flip", "vertical-flip",
        "upper-left-diagonal", "upper-right-diagonal",
    }
    if method not in allowed:
        method = "automatic"
    return method


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
        result = privileged.run_as_root(
            ["modprobe", "v4l2loopback", f"video_nr={number}",
             "card_label=AnyCam", "exclusive_caps=1"], timeout=60)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:  # noqa: BLE001
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
    if not path:
        return None
    try:
        values = Path(path).read_text()
    except OSError:
        return None
    return values.split(",")[0].strip() == "Y"


def vcam_udev_capabilities(device: str | None = None) -> str | None:
    """The capabilities udev announces for the loopback node.

    Chromium-based apps enumerate V4L2 nodes through udev and skip a node that
    advertises video output as well as capture.  This is the value Discord and
    Chrome actually read, which can lag behind the module parameter because
    udev caches it from device-add time.
    """
    dev = device or loopback_device()
    if shutil.which("udevadm") is None:
        return None
    code, out = run(["udevadm", "info", "--query=property", "--name", dev])
    if code != 0:
        return None
    for line in out.splitlines():
        line = line.strip().removeprefix("E: ")
        if line.startswith("ID_V4L_CAPABILITIES="):
            return line.split("=", 1)[1].strip()
    return ""


def refresh_vcam_udev(device: str | None = None, *, prompt: bool = True) -> str:
    """Re-read the node's capabilities so Chromium apps list AnyCam.

    With ``exclusive_caps=1`` v4l2loopback only advertises capture while a
    writer streams, but udev caches the capabilities from device-add time.
    AnyCam therefore has to re-run ``v4l_id`` after its idle feed starts;
    without that Discord, Vesktop and Chrome keep skipping the node even
    though the module parameter is correct.  Returns "" on success.
    """
    dev = device or loopback_device()
    current = vcam_udev_capabilities(dev)
    if current is not None and "capture" in current:
        return ""
    if trigger_vcam_uevent(dev):
        # The setup's udev rule lets the desktop user announce the change
        # without root; udev re-reads the capabilities asynchronously.
        for _ in range(40):
            current = vcam_udev_capabilities(dev)
            if current is not None and "capture" in current:
                return ""
            time.sleep(0.05)
    match = re.search(r"video(\d+)$", dev)
    if match is None:
        return f"virtual camera device {dev} is not a video node"
    number = match.group(1)
    manual = (f"sudo udevadm trigger --action=change "
              f"--sysname-match=video{number}")
    if shutil.which("udevadm") is None:
        return f"udevadm is missing; run: {manual}"
    if not prompt or shutil.which("pkexec") is None:
        return f"refreshing Chromium visibility needs root; run: {manual}"
    try:
        result = privileged.run_as_root(
            ["udevadm", "trigger", "--action=change",
             "--sysname-match", f"video{number}"], timeout=60)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return f"could not refresh Chromium visibility: {exc}"
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        reason = lines[-1] if lines else f"pkexec exited {result.returncode}"
        if result.returncode in (126, 127):
            reason = ("authorization was cancelled or no polkit agent is "
                      "running")
        return f"{reason}. Run: {manual}"
    # udev updates the database asynchronously after the change event.
    for _ in range(40):
        current = vcam_udev_capabilities(dev)
        if current is not None and "capture" in current:
            return ""
        time.sleep(0.05)
    return (f"{dev} still reports {current or 'no capabilities'}; "
            f"Chromium may not list it. Run: {manual}")


def other_virtual_cameras(device: str | None = None,
                          sysfs: str = "/sys/class/video4linux") -> list[str]:
    """Virtual camera nodes other than ``device`` (OBS's, another loopback).

    ``modprobe -r v4l2loopback`` removes every device the module created, so
    reloading it for AnyCam's node would also pull the camera out from under
    another application.  Virtual nodes live under ``devices/virtual``.
    """
    own = Path(device or loopback_device()).name
    found: list[str] = []
    try:
        entries = sorted(Path(sysfs).glob("video*"))
    except OSError:
        return found
    for entry in entries:
        if entry.name == own:
            continue
        try:
            real = str(entry.resolve())
            name = (entry / "name").read_text(errors="replace").strip()
        except OSError:
            continue
        if "/devices/virtual/video4linux/" in real:
            found.append(f"/dev/{entry.name} ({name})" if name
                         else f"/dev/{entry.name}")
    return found


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
    others = other_virtual_cameras(dev)
    if others:
        return ("reloading v4l2loopback would also remove "
                + ", ".join(others)
                + "; close the apps using them and reload manually: " + manual)
    if shutil.which("modprobe") is None:
        return f"modprobe is missing; run: {manual}"
    if not prompt or shutil.which("pkexec") is None:
        return f"reloading needs root; run: {manual}"
    try:
        result = privileged.run_as_root(
            # One prompt for the unload and the load: pkexec authorises the
            # shell, which runs both steps only if the unload succeeds.
            ["/bin/sh", "-c", f"modprobe -r v4l2loopback && {load}"],
            timeout=120)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:  # noqa: BLE001
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


# ------------------------------------------------- one-time system setup
# AnyCam's own files (the names scripts/enable-v4l2loopback.sh and older
# instructions used too, so the setup replaces a stale exclusive_caps=0).
VCAM_MODPROBE_CONF = Path("/etc/modprobe.d/anycam.conf")
VCAM_MODULES_LOAD = Path("/etc/modules-load.d/anycam.conf")
VCAM_UDEV_RULE = Path("/etc/udev/rules.d/70-anycam-vcam.rules")
# Exit code of the setup script when the config is installed but the module
# could not be reloaded because something still has the camera open.
VCAM_SETUP_BUSY = 3
# Exit code when the module reloaded but something else forced
# exclusive_caps back to 0 (another modprobe.d file).
VCAM_SETUP_OVERRIDDEN = 4

VCAM_SETUP_SCRIPT = r"""set -e
# Files an earlier AnyCam build of this setup wrote under another name.
rm -f /etc/modprobe.d/anycam-v4l2loopback.conf /etc/modules-load.d/anycam-v4l2loopback.conf
cat > {modprobe} <<'ANYCAM'
# Written by AnyCam: one virtual camera that Chromium apps (Discord,
# Vesktop, Chrome) can list.  exclusive_caps=1 is the part they need.
options v4l2loopback video_nr={number} card_label=AnyCam exclusive_caps=1
ANYCAM
cat > {modules} <<'ANYCAM'
# Written by AnyCam: load the virtual camera driver at boot.
v4l2loopback
ANYCAM
cat > {rule} <<'ANYCAM'
# Written by AnyCam: with exclusive_caps=1 the camera only offers capture
# while AnyCam writes to it, but udev remembers the capabilities from boot.
# Let the video group re-announce the device so apps see AnyCam without root.
ACTION=="add", SUBSYSTEM=="video4linux", ATTR{{name}}=="AnyCam", RUN+="/bin/sh -c 'chgrp video /sys%p/uevent; chmod 0664 /sys%p/uevent'"
ANYCAM
udevadm control --reload || true
caps=$(cut -c1 /sys/module/v4l2loopback/parameters/exclusive_caps 2>/dev/null || true)
if [ "$caps" != "Y" ]; then
  if [ -d /sys/module/v4l2loopback ] && ! modprobe -r v4l2loopback; then
    exit {busy}
  fi
  modprobe v4l2loopback
  udevadm settle --timeout=5 || true
fi
uevent=/sys/class/video4linux/video{number}/uevent
if [ -e "$uevent" ]; then
  chgrp video "$uevent" && chmod 0664 "$uevent"
fi
caps=$(cut -c1 /sys/module/v4l2loopback/parameters/exclusive_caps 2>/dev/null || true)
[ "$caps" = "Y" ] || exit {overridden}
"""


def trigger_vcam_uevent(device: str | None = None) -> bool:
    """Ask udev to re-read the node's capabilities, without root.

    Works once the setup's udev rule made the node's ``uevent`` writable for
    the video group.  Chromium apps re-list cameras on that change event.
    """
    path = Path("/sys/class/video4linux") / Path(device or loopback_device()).name / "uevent"
    try:
        path.write_text("change\n")
    except OSError:
        return False
    return True


def _effective_exclusive_caps(config_text: str) -> bool | None:
    """exclusive_caps as modprobe will apply it: the last value given wins."""
    value: bool | None = None
    for line in config_text.splitlines():
        line = line.split("#", 1)[0]
        if line.split()[:2] != ["options", "v4l2loopback"]:
            continue
        for match in re.finditer(r"\bexclusive_caps=([^\s,]+)", line):
            value = match.group(1).upper() in ("1", "Y", "YES", "TRUE")
    return value


def _vcam_configured_permanently() -> bool:
    """modprobe's effective config loads v4l2loopback with exclusive_caps=1."""
    code, out = run(["modprobe", "-c"]) if shutil.which("modprobe") else (1, "")
    if code != 0:
        return False
    return _effective_exclusive_caps(out) is True


def vcam_caps_overrides(dirs: tuple[str, ...] = ("/etc/modprobe.d", "/usr/lib/modprobe.d",
                                                 "/run/modprobe.d")) -> list[str]:
    """Config files other than AnyCam's that set exclusive_caps to 0."""
    found: list[str] = []
    for directory in dirs:
        try:
            files = sorted(Path(directory).glob("*.conf"))
        except OSError:
            continue
        for conf in files:
            if conf == VCAM_MODPROBE_CONF:
                continue
            try:
                text = conf.read_text(errors="replace")
            except OSError:
                continue
            if _effective_exclusive_caps(text) is False:
                found.append(str(conf))
    return found


def vcam_setup_needed(device: str | None = None) -> str:
    """Why the one-time virtual camera setup is still needed ("" if done)."""
    dev = device or loopback_device()
    caps = vcam_exclusive_caps()
    if caps is None or not Path(dev).exists():
        return "the virtual camera driver is not loaded"
    if caps is not True:
        return "the driver is loaded without exclusive_caps=1, so Chromium apps skip it"
    if not _vcam_configured_permanently():
        return "the driver settings are not permanent (lost at the next reboot)"
    uevent = Path("/sys/class/video4linux") / Path(dev).name / "uevent"
    if not os.access(uevent, os.W_OK):
        return "AnyCam cannot tell apps that the camera is ready"
    return ""


def setup_vcam_system(device: str | None = None) -> str:
    """Install the virtual camera permanently with one password prompt.

    Writes the modprobe and boot configs and the udev rule, then reloads
    v4l2loopback with ``exclusive_caps=1`` if it was loaded without it.  The
    caller must have closed its own handles on the device.  Returns "" on
    success, a reason otherwise.
    """
    dev = device or loopback_device()
    match = re.search(r"video(\d+)$", dev)
    if match is None:
        return f"virtual camera device {dev} is not a video node"
    number = match.group(1)
    if vcam_exclusive_caps() is not True:
        others = other_virtual_cameras(dev)
        if others:
            return ("setting up AnyCam's camera would remove "
                    + ", ".join(others) + "; close the apps using them first")
    script = VCAM_SETUP_SCRIPT.format(
        modprobe=VCAM_MODPROBE_CONF, modules=VCAM_MODULES_LOAD,
        rule=VCAM_UDEV_RULE, number=number, busy=VCAM_SETUP_BUSY,
        overridden=VCAM_SETUP_OVERRIDDEN)
    try:
        result = privileged.run_as_root(["/bin/sh", "-c", script])
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return f"could not set up the virtual camera: {exc}"
    if result.returncode == VCAM_SETUP_BUSY:
        return ("the settings are installed, but another app (Discord, a browser) "
                "has the virtual camera open, so the driver could not be "
                "reloaded. Close its camera and try again, or reboot")
    if result.returncode == VCAM_SETUP_OVERRIDDEN:
        overrides = vcam_caps_overrides()
        return ("the driver was reloaded but another setting forces "
                "exclusive_caps=0" + (": " + ", ".join(overrides) if overrides else "")
                + ". Remove that line and run the setup again")
    if result.returncode != 0:
        return f"setup failed: {privileged.failure_reason(result)}"
    for _ in range(60):
        if not vcam_setup_needed(dev):
            return ""
        time.sleep(0.05)
    return f"setup ran but {vcam_setup_needed(dev)}"


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


# The virtual camera follows the project canvas up to 4K.  It keeps that
# mode once started; re-negotiating the loopback device for every canvas
# change closes the node and leaves apps (Discord) on a dead handle.  A 4K
# canvas therefore gets a real 4K vcam instead of being reduced to 1080p.
VCAM_MAX_W = 3840
VCAM_MAX_H = 2160


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
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "gstreamer": gst.splitlines()[0].strip() if code == 0 and gst else "unavailable",
        "camera": camera_device(),
        "microphone": microphone(),
        "vcam": loopback_device(),
        "vcam_available": vcam_available(),
        "session": os.environ.get("XDG_SESSION_TYPE", ""),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
    }
