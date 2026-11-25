"""Phase 5 tests: i18n completeness, OverlayPanel translation, packaging & CI hygiene."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anycam import i18n
from anycam.model import empty_project


def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_de_json_valid_and_has_no_duplicate_keys():
    path = i18n.catalog_dir() / "de.json"
    duplicates: list[str] = []

    def check_dupes(pairs):
        seen: set[str] = set()
        out = {}
        for k, v in pairs:
            if k in seen:
                duplicates.append(k)
            seen.add(k)
            out[k] = v
        return out

    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=check_dupes)
    assert not duplicates, f"duplicate keys in de.json: {duplicates}"
    assert data["_name"] == "Deutsch"
    assert len(data) >= 420
    # Key Phase 3-5 labels present
    for key in (
        "Spill suppression", "Blur", "Filter 4", "+ Web page",
        "Paired phones", "Forget selected", "Forget all",
        "Echo cancellation (remove speaker playback from mic)",
        "Extra devices", "PipeWire cameras", "Preview (next)",
        "Stop recording", "Mute", "Unmute",
    ):
        assert key in data, f"missing German translation for {key!r}"


def test_i18n_apply_translates_combos_headers_and_special_values():
    _qapp()
    from PySide6.QtWidgets import QComboBox, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

    root = QWidget()
    layout = QVBoxLayout(root)
    combo = QComboBox()
    combo.addItem("Off", "off")
    combo.addItem("High", "high")
    table = QTableWidget(0, 2)
    table.setHorizontalHeaderItem(0, QTableWidgetItem("Action"))
    table.setHorizontalHeaderItem(1, QTableWidgetItem("Key"))
    spin = QSpinBox()
    spin.setSpecialValueText("Automatic")
    layout.addWidget(combo)
    layout.addWidget(table)
    layout.addWidget(spin)

    de = i18n.load("de")
    changed = i18n.apply(root, de)
    assert changed >= 5
    assert combo.itemText(0) == "Aus"
    assert combo.itemText(1) == "Hoch"
    assert table.horizontalHeaderItem(0).text() == "Aktion"
    assert table.horizontalHeaderItem(1).text() == "Taste"
    assert spin.specialValueText() == "Automatisch"

    # Switching back to English restores original labels cleanly.
    i18n.apply(root, {})
    assert combo.itemText(0) == "Off"
    assert table.horizontalHeaderItem(0).text() == "Action"
    assert spin.specialValueText() == "Automatic"


def test_overlay_panel_refresh_keeps_translated_labels():
    _qapp()
    from anycam.gui_many import ManyCamWindow
    from anycam.ui.overlay_panel import OverlayPanel

    win = ManyCamWindow(empty_project())
    win.headless = True
    win._persist = False
    try:
        i18n.apply(win, i18n.load("de"))
        panel = OverlayPanel(win)
        try:
            panel.refresh()
            assert panel.record_button.text() == "Aufnehmen"
            assert panel.mute_button.text() == "Stummschalten"
            win.recording = True
            win.project.audio.mic_muted = True
            panel.refresh()
            assert panel.record_button.text() == "Aufnahme beenden"
            assert panel.mute_button.text() == "Stummschaltung aufheben"
        finally:
            panel.close()
    finally:
        i18n.apply(win, {})
        win.close()


def test_packaging_recipes_include_added_dependencies():
    root = Path(__file__).resolve().parents[1]
    deb = (root / "packaging/deb/build.sh").read_text(encoding="utf-8")
    assert "python3-pyside6.qtnetwork" in deb
    assert "gir1.2-secret-1" in deb
    assert "gstreamer1.0-qt6" in deb

    pkgbuild = (root / "packaging/arch/PKGBUILD").read_text(encoding="utf-8")
    assert "libsecret" in pkgbuild
    assert "qrencode" in pkgbuild

    flatpak = (root / "packaging/unsupported/flatpak/io.github.geneticxcln.AnyCam.yml").read_text(
        encoding="utf-8")
    assert "--talk-name=org.freedesktop.secrets" in flatpak

    release_yml = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "INPUT_TAG" in release_yml
