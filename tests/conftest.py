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
    from anycam import config as anycam_config
    from anycam import portal

    def no_portal(*_args, **_kwargs):
        raise portal.PortalError(
            "hardware-free test environment: no desktop portal")

    patch = pytest.MonkeyPatch()
    patch.setattr(anycam_config, "list_video_devices", lambda *a, **k: [])
    patch.setattr(anycam_config, "list_audio_sources", lambda *a, **k: [])
    patch.setattr(anycam_config, "vcam_available", lambda device=None: False)
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
    yield store
    credentials.set_store(None)
