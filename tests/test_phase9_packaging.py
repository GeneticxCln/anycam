"""Phase 9: the packaging recipes build and agree with the source tree."""
from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


def version() -> str:
    for line in (ROOT / "pyproject.toml").read_text().splitlines():
        if line.startswith("version ="):
            return line.split('"')[1]
    raise AssertionError("no version in pyproject.toml")


def test_the_metainfo_is_well_formed_and_matches_the_project():
    tree = ET.parse(PACKAGING / "metainfo" / "io.github.geneticxcln.AnyCam.metainfo.xml")
    root = tree.getroot()
    assert root.tag == "component" and root.get("type") == "desktop-application"
    assert root.findtext("id") == "io.github.geneticxcln.AnyCam"
    assert root.findtext("project_license") == "MIT"
    assert root.find("launchable").text == "anycam.desktop"
    assert any(release.get("version") == version() for release in root.iter("release"))


def test_the_desktop_file_names_a_real_icon_and_command():
    text = (ROOT / "anycam.desktop").read_text()
    assert "Exec=anycam gui" in text and "Icon=anycam" in text
    assert (ROOT / "src" / "anycam" / "assets" / "anycam-logo.png").is_file()


@pytest.mark.parametrize("script", ["deb/build.sh", "appimage/build.sh"])
def test_build_scripts_parse_and_pass_shellcheck(script):
    path = PACKAGING / script
    subprocess.run(["bash", "-n", str(path)], check=True)
    if shutil.which("shellcheck"):
        result = subprocess.run(["shellcheck", "-S", "warning", str(path)],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stdout


def test_the_pkgbuild_parses_and_tracks_the_version():
    text = (PACKAGING / "arch" / "PKGBUILD").read_text()
    subprocess.run(["bash", "-n", str(PACKAGING / "arch" / "PKGBUILD")], check=True)
    assert f"pkgver={version()}" in text
    assert "python-pillow" in text and "gstreamer" in text and "install -Dm644 LICENSE" in text


def test_the_flatpak_manifest_lists_what_the_app_needs():
    yaml = pytest.importorskip("yaml")
    manifest = yaml.safe_load((PACKAGING / "flatpak" / "io.github.geneticxcln.AnyCam.yml").read_text())
    assert manifest["app-id"] == "io.github.geneticxcln.AnyCam" and manifest["command"] == "anycam"
    permissions = " ".join(manifest["finish-args"])
    for needed in ("--device=all", "--share=network", "--socket=pulseaudio"):
        assert needed in permissions
    assert {module["name"] for module in manifest["modules"]} >= {"anycam"}


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb unavailable")
def test_the_deb_builds_and_holds_the_package(tmp_path):
    result = subprocess.run([str(PACKAGING / "deb" / "build.sh"), str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    package = tmp_path / f"anycam_{version()}_all.deb"
    assert package.is_file()
    listing = subprocess.run(["dpkg-deb", "--contents", str(package)], capture_output=True,
                             text=True, check=True).stdout
    for expected in ("usr/bin/anycam", "usr/share/applications/anycam.desktop",
                     "usr/lib/python3/dist-packages/anycam/__main__.py",
                     "usr/lib/python3/dist-packages/anycam/assets/i18n/de.json",
                     "usr/lib/python3/dist-packages/anycam/phone/static/remote.html",
                     "usr/share/doc/anycam/copyright"):
        assert expected in listing, expected
    assert "__pycache__" not in listing and ".pyc" not in listing
    control = subprocess.run(["dpkg-deb", "--field", str(package)], capture_output=True,
                             text=True, check=True).stdout
    assert f"Version: {version()}" in control and "Depends:" in control
    assert "python3-gi" in control and "v4l-utils" in control


def test_every_data_file_the_code_loads_is_declared_for_installation():
    declared = (ROOT / "pyproject.toml").read_text()
    for pattern in ("phone/static/*.html", "phone/static/*.js", "assets/i18n/*.json",
                    "assets/models/*.onnx", "assets/emoji/*.png"):
        assert pattern in declared, pattern


# -------------------------------------------------------------------- first run
from anycam import firstrun  # noqa: E402


def by_key(results):
    return {check.key: check for check in results}


def test_a_complete_machine_passes_every_check(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "pipewire-0").write_text("")
    camera = tmp_path / "video0"
    camera.write_text("")
    from anycam import config

    monkeypatch.setattr(config, "vcam_available", lambda device=None: True)
    results = firstrun.checks(elements={"missing": [], "optional_missing": {}},
                              cameras=[str(camera)],
                              environ={"XDG_RUNTIME_DIR": str(runtime), "XDG_SESSION_TYPE": "wayland"})
    assert firstrun.ready(results) and all(check.ok for check in results)
    assert "wayland" in by_key(results)["screen"].detail


def test_missing_pieces_come_with_the_command_that_fixes_them(tmp_path, monkeypatch):
    from anycam import config

    monkeypatch.setattr(config, "vcam_available", lambda device=None: False)
    monkeypatch.setattr(config, "has_command", lambda name: False)
    monkeypatch.setattr(firstrun, "distro_family", lambda: "arch")
    results = by_key(firstrun.checks(
        elements={"missing": ["x264enc", "flvmux"], "optional_missing": {"pipewiresrc": "x",
                                                                         "glvideomixer": "y"}},
        cameras=[], environ={"XDG_RUNTIME_DIR": str(tmp_path)}))
    assert not firstrun.ready(list(results.values()))              # GStreamer is required
    assert "x264enc, flvmux" in results["gstreamer"].detail
    assert results["gstreamer"].fix.startswith("sudo pacman -S gst-plugins-base")
    assert "plug a camera in" in results["camera"].fix and not results["camera"].required
    assert "enable-v4l2loopback" in results["vcam"].fix
    assert results["audio"].fix.startswith("sudo pacman -S pipewire")
    assert "gst-plugin-pipewire" in results["screen"].fix
    assert results["gpu"].ok is False and results["gpu"].fix == ""


def test_a_camera_the_user_cannot_open_points_at_the_video_group():
    results = by_key(firstrun.checks(elements={"missing": []}, cameras=["/dev/video0"]))
    if not os.access("/dev/video0", os.R_OK | os.W_OK):
        assert "usermod -aG video" in results["camera"].fix


def test_install_hints_follow_the_distribution():
    packages = {"debian": "a b", "fedora": "c"}
    assert firstrun.install_hint(packages, "debian") == "sudo apt install a b"
    assert firstrun.install_hint(packages, "fedora") == "sudo dnf install c"
    assert firstrun.install_hint(packages, "arch") == "sudo pacman -S a b"          # falls back
    assert firstrun.distro_family() in ("arch", "debian", "fedora", "suse")


def test_the_report_text_lists_every_check_with_its_fix():
    text = firstrun.summary_text([
        firstrun.Check("a", "Alpha", True, "fine"),
        firstrun.Check("b", "Beta", False, "broken", "do this"),
        firstrun.Check("c", "Gamma", False, "meh", "", required=False)])
    assert "[ok  ] Alpha: fine" in text and "[FAIL] Beta: broken" in text
    assert "-> do this" in text and "[note] Gamma" in text


def test_the_welcome_shows_once(tmp_path, monkeypatch):
    for name in ("CACHE", "CONFIG", "STATE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    assert firstrun.already_shown() is False
    firstrun.mark_shown()
    assert firstrun.already_shown() is True


def test_the_check_command_prints_the_report(capsys):
    from anycam.__main__ import main

    code = main(["check"])
    out = capsys.readouterr().out
    assert code in (0, 1) and "GStreamer plugins" in out and "Virtual camera" in out


def test_the_dialog_lists_the_results_and_copies_the_report(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    for name in ("CACHE", "CONFIG", "STATE", "DATA"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from anycam.ui.dialogs import FirstRunDialog

    QApplication.instance() or QApplication([])
    results = [firstrun.Check("a", "Alpha", True, "fine"),
               firstrun.Check("b", "Beta", False, "broken", "do this")]
    dialog = FirstRunDialog(results)
    assert dialog.rows.count() == 2 and "do this" in dialog.rows.item(1).text()
    dialog.copy_report()
    assert "Beta: broken" in QGuiApplication.clipboard().text()


# ---------------------------------------------------------------- compat harness
from anycam import compat  # noqa: E402


def test_the_page_result_is_classified():
    done = {"state": "done", "width": 1280, "height": 720, "fps": 30, "label": "AnyCam",
            "mean": 90.5, "std": 40}
    state, detail, resolution = compat.classify(done, fake=False)
    assert state == "ok" and resolution == "1280x720 @30" and "AnyCam" in detail
    missing = {"state": "error", "error": "AnyCam is not enumerated among 1 video inputs",
               "devices": ["fake"], "found": False}
    assert compat.classify(missing, fake=False)[0] == "failed"
    assert compat.classify(missing, fake=True)[0] == "ok"           # the self-test: it ran and saw a camera
    assert compat.classify({"state": "error", "error": "x", "devices": [], "found": False}, True)[0] == "failed"
    assert compat.classify({"state": "permission…"}, fake=False)[0] == "error"
    assert compat.classify({}, fake=False)[0] == "error"


def test_the_table_and_json_list_every_browser():
    rows = [compat.Outcome("chrome", "/x", "anycam", "ok", "fine", resolution="1280x720 @30", version="v1"),
            compat.Outcome("firefox", "", "anycam", "not found", "firefox | is not installed")]
    table = compat.as_markdown(rows)
    assert "| chrome v1 | anycam | ok | 1280x720 @30 fine |" in table
    assert "firefox / is not installed" in table                    # a pipe cannot break the table
    import json

    assert [row["browser"] for row in json.loads(compat.to_json(rows))] == ["chrome", "firefox"]


@pytest.mark.skipif(not compat.find_chromium_family(), reason="no Chromium-family browser installed")
def test_a_real_browser_runs_the_check_page_with_its_test_camera():
    server, url = compat.serve_web()
    try:
        name, binary = compat.find_chromium_family()[0]
        outcome = compat.run_chromium(name, binary, url, fake=True, timeout=45)
    finally:
        server.shutdown()
    assert outcome.state == "ok", outcome
    assert outcome.mode == "fake-device" and outcome.devices
