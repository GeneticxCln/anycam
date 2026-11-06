"""The About tab, the version-1 release number and the release packaging."""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from anycam import about, config

ROOT = Path(__file__).resolve().parents[1]


def test_this_is_version_one_everywhere():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert re.fullmatch(r"\d+\.\d+\.\d+", config.VERSION)
    assert config.VERSION.split(".")[0] == "1"                # this is still major version 1
    assert pyproject["project"]["version"] == config.VERSION
    import anycam

    assert anycam.__version__ == config.VERSION
    assert about.RELEASE_TAG == f"v{config.VERSION}"
    assert about.headline() == "AnyCam v1"
    assert "Version 1" in about.release_line() and config.VERSION in about.release_line()


def test_about_lists_the_runtime_and_credits():
    rows = dict(about.runtime_rows())
    assert {"Release", "Python", "Qt (PySide6)", "GStreamer", "onnxruntime", "System"} <= set(rows)
    assert rows["Release"].startswith(f"v{config.VERSION}")
    names = " ".join(item[0] + item[1] for item in about.CREDITS)
    assert "Noto" in names and "YuNet" in names and "MediaPipe" in names
    assert all(item[2] for item in about.CREDITS)           # every credit names a licence
    assert about.REPO_URL.startswith("https://github.com/")


def test_module_version_reports_missing_packages_instead_of_raising():
    assert about._module_version("definitely_not_installed_pkg") == "not installed"


@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    for name in ("CACHE", "CONFIG", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))
    from tests.test_effects_browser import close, make_window

    app = QApplication.instance() or QApplication([])
    win = make_window(app)
    yield win, app
    close(win, app)


def test_settings_has_an_about_tab_showing_version_one(window):
    from PySide6.QtWidgets import QLabel

    win, app = window
    names = [win.settings_tabs.tabText(i) for i in range(win.settings_tabs.count())]
    assert names[-1] == "About"
    win._open_settings(names.index("About"))
    app.processEvents()
    page = win.settings_tabs.currentWidget()
    assert win.settings_tabs.tabText(win.settings_tabs.currentIndex()) == "About"
    text = " ".join(label.text() for label in page.findChildren(QLabel))
    assert "AnyCam v1" in text
    assert "Version 1" in text and config.VERSION in text
    assert about.REPO_URL in text and "MIT" in text
    assert "Noto" in text


def test_about_does_not_disturb_the_other_settings_tabs(window):
    win, _app = window
    names = [win.settings_tabs.tabText(i) for i in range(win.settings_tabs.count())]
    assert names[:6] == ["Sources", "Audio", "Media", "Output", "Stream", "Diagnostics"]


def test_copy_version_details_puts_the_release_on_the_clipboard(window):
    from PySide6.QtWidgets import QApplication

    win, app = window
    win._copy_version_details()
    text = QApplication.clipboard().text()
    assert "AnyCam v1" in text and config.VERSION in text and "Python:" in text


# ------------------------------------------------------------ release packaging
def test_the_release_workflow_builds_a_tarball_and_publishes_on_a_version_tag():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert re.search(r"tags:\s*\n\s*-\s*['\"]?v\*", workflow)
    assert "workflow_dispatch" in workflow
    assert "contents: write" in workflow                    # needed to create the release
    assert "sdist" in workflow and ".tar.gz" in workflow
    assert "SHA256SUMS" in workflow
    assert "gh release" in workflow or "action-gh-release" in workflow
    assert "pyproject.toml" in workflow and "GITHUB_REF" in workflow   # the tag must match the version


def test_the_emoji_and_models_are_declared_as_package_data():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["anycam"]
    for pattern in ("assets/emoji/*.png", "assets/models/*.onnx", "assets/*.jpg", "ui/*.qss"):
        assert pattern in data, pattern
