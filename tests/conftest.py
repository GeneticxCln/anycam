"""Shared test setup.

Puts ``src`` on ``sys.path`` and adds one switch:

    pytest tests --no-hardware        # or ANYCAM_TEST_ENV=ci

hides the cameras, the v4l2loopback node, the audio server and the desktop
portal, so the suite takes the same paths as the GitHub runner, where none of
them exist.  The display is already hidden everywhere by
``QT_QPA_PLATFORM=offscreen``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# The encoder probe starts a child process; tests ask for it explicitly.
os.environ.setdefault("ANYCAM_ENCODER_PROBE", "off")

HARDWARE_FREE_ENV = "ANYCAM_TEST_ENV"
HARDWARE_FREE_VALUES = {"ci", "none", "hardware-free"}


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--no-hardware",
        action="store_true",
        default=False,
        help=(
            "Hide cameras, the v4l2loopback node, audio sources and the desktop "
            "portal, reproducing the GitHub runner on a machine that has them."
        ),
    )


def hardware_free(config) -> bool:
    if config.getoption("--no-hardware", default=False):
        return True
    return os.environ.get(HARDWARE_FREE_ENV, "").strip().lower() in HARDWARE_FREE_VALUES


@pytest.fixture(scope="session", autouse=True)
def hardware_free_environment(request):
    """Report the devices a bare GitHub runner reports."""
    if not hardware_free(request.config):
        yield False
        return
    import subprocess

    from anycam import config as anycam_config
    from anycam import portal, privileged

    def no_portal(*_args, **_kwargs):
        raise portal.PortalError(
            "hardware-free test environment: no desktop portal")

    orig_run = anycam_config.run
    orig_subprocess_run = subprocess.run
    orig_list_audio_sinks = anycam_config.list_audio_sinks
    orig_vcam_exclusive_caps = anycam_config.vcam_exclusive_caps
    orig_vcam_udev_capabilities = anycam_config.vcam_udev_capabilities
    orig_refresh_vcam_udev = anycam_config.refresh_vcam_udev
    orig_run_as_root = privileged.run_as_root

    def fake_list_audio_sinks(*args, **kwargs):
        if anycam_config.run is not orig_run:
            return orig_list_audio_sinks(*args, **kwargs)
        return []

    def fake_vcam_exclusive_caps(
        path="/sys/module/v4l2loopback/parameters/exclusive_caps",
        device=None,
    ):
        if path and not str(path).startswith("/sys/"):
            return orig_vcam_exclusive_caps(path, device)
        return None

    def fake_vcam_udev_capabilities(*args, **kwargs):
        if anycam_config.run is not orig_run:
            return orig_vcam_udev_capabilities(*args, **kwargs)
        return None

    def fake_refresh_vcam_udev(*args, **kwargs):
        if anycam_config.subprocess.run is not orig_subprocess_run:
            return orig_refresh_vcam_udev(*args, **kwargs)
        return ""

    def fake_run_as_root(args, *, timeout: float = 180):
        if privileged.subprocess.run is not orig_subprocess_run:
            return orig_run_as_root(args, timeout=timeout)
        raise RuntimeError("hardware-free test environment: pkexec disabled")

    patch = pytest.MonkeyPatch()
    patch.setattr(anycam_config, "list_video_devices", lambda *a, **k: [])
    patch.setattr(anycam_config, "list_audio_sources", lambda *a, **k: [])
    patch.setattr(anycam_config, "list_audio_sinks", fake_list_audio_sinks)
    patch.setattr(anycam_config, "vcam_available", lambda device=None: False)
    patch.setattr(anycam_config, "vcam_exclusive_caps", fake_vcam_exclusive_caps)
    patch.setattr(anycam_config, "vcam_udev_capabilities", fake_vcam_udev_capabilities)
    patch.setattr(anycam_config, "refresh_vcam_udev", fake_refresh_vcam_udev)
    patch.setattr(privileged, "run_as_root", fake_run_as_root)
    patch.setattr(portal, "_connection", no_portal)
    try:
        yield True
    finally:
        patch.undo()


@pytest.fixture(autouse=True)
def memory_keyring():
    """Tests never touch the real desktop keyring: each gets its own in-memory one."""
    from anycam import credentials

    store = credentials.MemoryStore()
    credentials.set_store(store)
    credentials._known_ids.clear()
    yield store
    credentials.set_store(None)


@pytest.fixture(autouse=True)
def forget_encoder_probe():
    """Each test starts with no probe answer in memory."""
    try:
        from anycam.engine import encoders
    except Exception:  # noqa: BLE001 - GStreamer bindings missing
        yield
        return
    encoders.forget_probe()
    yield
    encoders.forget_probe()
