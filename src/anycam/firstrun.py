"""First-run check: is this machine ready, and if not, what exactly to do.

``checks`` is plain data (so it is testable and ``anycam doctor`` can print it);
``FirstRunDialog`` shows it once after the first launch.
"""
from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class Check:
    key: str
    title: str
    ok: bool
    detail: str
    fix: str = ""            # a command or an instruction; "" when nothing is needed
    required: bool = True


def install_hint(packages: dict[str, str], distro: str | None = None) -> str:
    """The package-manager command for the distribution, from ``{family: packages}``."""
    family = (distro or distro_family()).lower()
    names = packages.get(family) or packages.get("debian", "")
    commands = {"arch": "sudo pacman -S", "debian": "sudo apt install",
                "fedora": "sudo dnf install", "suse": "sudo zypper install"}
    return f"{commands.get(family, commands['debian'])} {names}".strip()


def distro_family() -> str:
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return "debian"
    ids = " ".join(line.split("=", 1)[1].strip('"').lower() for line in text.splitlines()
                   if line.startswith(("ID=", "ID_LIKE=")))
    for family, words in (("arch", ("arch", "manjaro", "endeavouros")),
                          ("fedora", ("fedora", "rhel", "centos")),
                          ("suse", ("suse",)), ("debian", ("debian", "ubuntu", "mint", "pop"))):
        if any(word in ids for word in words):
            return family
    return "debian"


def checks(elements: dict | None = None, cameras: list[str] | None = None,
           environ: dict | None = None) -> list[Check]:
    """Every check, in the order a person would fix them."""
    env = os.environ if environ is None else environ
    report = elements if elements is not None else config.check_elements()
    found: list[Check] = []

    missing = report.get("missing", [])
    found.append(Check(
        "gstreamer", "GStreamer plugins", not missing,
        "all required plugins are installed" if not missing
        else "missing: " + ", ".join(missing[:8]) + (" …" if len(missing) > 8 else ""),
        "" if not missing else install_hint({
            "debian": "gstreamer1.0-plugins-base gstreamer1.0-plugins-good "
                      "gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav",
            "arch": "gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav",
            "fedora": "gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free "
                      "gstreamer1-plugins-ugly gstreamer1-libav",
            "suse": "gstreamer-plugins-base gstreamer-plugins-good gstreamer-plugins-bad "
                    "gstreamer-plugins-ugly gstreamer-plugins-libav"})))

    devices = cameras if cameras is not None else sorted(
        str(path) for path in Path("/dev").glob("video*"))
    usable = [path for path in devices if os.access(path, os.R_OK | os.W_OK)]
    found.append(Check(
        "camera", "Camera", bool(usable),
        f"{len(usable)} camera device(s) readable" if usable else
        ("a camera exists but you may not open it" if devices else "no camera found"),
        "" if usable else ("sudo usermod -aG video $USER   (then log out and in)" if devices
                           else "plug a camera in, or add a screen, image or video instead"),
        required=False))

    vcam = config.loopback_device()
    vcam_ok = bool(config.vcam_available(vcam)) if hasattr(config, "vcam_available") else False
    found.append(Check(
        "vcam", "Virtual camera", vcam_ok,
        f"{vcam} is ready" if vcam_ok else "no v4l2loopback device for browsers and meeting apps",
        "" if vcam_ok else "scripts/enable-v4l2loopback.sh   (or use the PipeWire camera backend)",
        required=False))

    pipewire = bool(env.get("XDG_RUNTIME_DIR")) and (Path(env["XDG_RUNTIME_DIR"]) / "pipewire-0").exists()
    found.append(Check(
        "audio", "Sound server", pipewire or config.has_command("pactl"),
        "PipeWire or PulseAudio found" if pipewire or config.has_command("pactl")
        else "no sound server found; the microphone and audio outputs will not work",
        "" if pipewire or config.has_command("pactl")
        else install_hint({"debian": "pipewire pipewire-pulse", "arch": "pipewire pipewire-pulse",
                           "fedora": "pipewire pipewire-pulseaudio", "suse": "pipewire pipewire-pulseaudio"}),
        required=False))

    session = str(env.get("XDG_SESSION_TYPE", "")).lower()
    optional = report.get("optional_missing", {})
    screen_ok = "pipewiresrc" not in optional
    found.append(Check(
        "screen", "Screen capture", screen_ok,
        (f"PipeWire capture available ({session or 'unknown session'})" if screen_ok
         else "the GStreamer PipeWire plugin is missing"),
        "" if screen_ok else install_hint({"debian": "gstreamer1.0-pipewire",
                                           "arch": "gst-plugin-pipewire",
                                           "fedora": "pipewire-gstreamer",
                                           "suse": "gstreamer-plugin-pipewire"}),
        required=False))

    found.append(Check(
        "gpu", "GPU canvas", "glvideomixer" not in optional,
        "OpenGL canvas available" if "glvideomixer" not in optional
        else "the CPU canvas will be used (fine for most setups)",
        "", required=False))
    return found


def ready(results: list[Check]) -> bool:
    return all(check.ok for check in results if check.required)


def summary_text(results: list[Check]) -> str:
    lines = [f"AnyCam on {platform.system()} {platform.release()}"]
    for check in results:
        mark = "ok  " if check.ok else ("FAIL" if check.required else "note")
        lines.append(f"[{mark}] {check.title}: {check.detail}")
        if check.fix:
            lines.append(f"       -> {check.fix}")
    return "\n".join(lines)


def _state_path() -> Path:
    return config.state_dir() / "first-run.json"


def already_shown() -> bool:
    try:
        return bool(json.loads(_state_path().read_text()).get("shown"))
    except (OSError, ValueError, AttributeError):
        return False


def mark_shown() -> None:
    try:
        _state_path().write_text(json.dumps({"shown": True, "version": config.VERSION}))
    except OSError:
        pass
