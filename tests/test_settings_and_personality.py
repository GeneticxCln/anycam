"""Side-tab Settings layout, sorted AI/Shortcuts tabs and Personality design bubbles."""
from __future__ import annotations

import stat

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QGridLayout  # noqa: E402

from anycam import a11y  # noqa: E402
from anycam.gui_many import ManyCamWindow  # noqa: E402
from anycam.model import empty_project  # noqa: E402
from anycam.ui import theme  # noqa: E402
from anycam.ui.personality_panel import PersonalityBubble  # noqa: E402
from anycam.ui.settings_view import SideTabWidget  # noqa: E402


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("ANYCAM_PERSONALITY", raising=False)
    app = QApplication.instance() or QApplication([])
    win = ManyCamWindow(empty_project())
    win.headless = True
    win._persist = False
    yield win, app
    win.engine = None
    win.close()
    app.processEvents()


def test_settings_uses_sorted_side_tabs_and_fits_small_screens(window):
    win, app = window
    win._open_settings()
    app.processEvents()

    tabs = win.settings_tabs
    assert isinstance(tabs, SideTabWidget)
    names = [tabs.tabText(i) for i in range(tabs.count())]
    assert names == [
        "Sources", "Audio", "Media", "Output", "Stream",
        "AI", "Personality", "Shortcuts", "General", "Diagnostics", "About",
    ]
    assert tabs.section_for(names.index("Output")) == "Studio"
    assert tabs.section_for(names.index("AI")) == "Intelligence"
    assert tabs.section_for(names.index("Personality")) == "Appearance"
    assert tabs.section_for(names.index("General")) == "System"

    hint = win._settings.minimumSizeHint()
    assert hint.width() <= 1366 and hint.height() <= 768

    # No overlapping cells in any QGridLayout inside the redesigned Settings dialog.
    for grid in win._settings.findChildren(QGridLayout):
        occupied: set[tuple[int, int]] = set()
        for idx in range(grid.count()):
            r, c, rs, cs = grid.getItemPosition(idx)
            for rr in range(r, r + max(1, rs)):
                for cc in range(c, c + max(1, cs)):
                    assert (rr, cc) not in occupied
                    occupied.add((rr, cc))


def test_ai_settings_and_shortcuts_are_cleanly_sorted_into_their_own_tabs(window):
    win, app = window
    tabs = win.settings_tabs
    names = [tabs.tabText(i) for i in range(tabs.count())]

    output_page = tabs.widget(names.index("Output"))
    ai_page = tabs.widget(names.index("AI"))
    shortcuts_page = tabs.widget(names.index("Shortcuts"))
    general_page = tabs.widget(names.index("General"))

    # All AI controls live in the dedicated AI tab (and not duplicated in Output/General).
    for widget in (win.effects_device_combo, win.segmentation_combo,
                   win.model_download_button, win.effects_isolated_check,
                   win.packs_note, win.motion_check):
        assert ai_page.isAncestorOf(widget)
        assert not output_page.isAncestorOf(widget)
        assert not general_page.isAncestorOf(widget)

    # Shortcuts editor and reference live together in Shortcuts.
    assert shortcuts_page.isAncestorOf(win.hotkey_table)
    assert shortcuts_page.isAncestorOf(win.global_shortcuts_label)
    assert not output_page.isAncestorOf(win.global_shortcuts_label)
    assert not general_page.isAncestorOf(win.hotkey_table)

    # Output keeps canvas, vcam, encoder, recording and screen capture.
    for widget in (win.width_spin, win.canvas_combo, win.vcam_edit,
                   win.encoder_combo, win.record_dir_edit, win.screen_flip_combo):
        assert output_page.isAncestorOf(widget)


def test_personality_tab_has_one_design_bubble_per_personality_and_persists(window):
    win, app = window
    tabs = win.settings_tabs
    names = [tabs.tabText(i) for i in range(tabs.count())]
    personality_page = tabs.widget(names.index("Personality"))
    win._open_settings(names.index("Personality"))
    app.processEvents()

    bubbles = personality_page.findChildren(PersonalityBubble)
    assert len(bubbles) == len(theme.PERSONALITIES) >= 6
    keys = [b.personality.key for b in bubbles]
    assert len(set(keys)) == len(theme.PERSONALITIES)
    assert keys == [p.key for p in theme.PERSONALITIES]

    # Every personality bubble paints cleanly and has an accessible name.
    a11y.apply(win._settings)
    assert a11y.unnamed(win._settings) == []
    for bubble in bubbles:
        assert bubble.accessibleName()
        bubble.grab()  # exercises PersonalityBubble.paintEvent without errors

    # Clicking a bubble switches personality live and persists owner-only.
    win._persist = True
    target = win._personality_bubbles["midnight-neon"]
    target.click()
    app.processEvents()

    neon = theme.PERSONALITY_BY_KEY["midnight-neon"]
    assert win._personality == neon
    assert target.isChecked()
    assert not win._personality_bubbles["mint-studio"].isChecked()
    assert neon.accent in win.styleSheet()
    assert neon.accent in win._settings.styleSheet()
    assert win.personality_title.text() == neon.name
    assert theme.saved_personality() == "midnight-neon"
    assert stat.S_IMODE(theme.personality_file().stat().st_mode) == 0o600

    # Light personality also applies cleanly and Reset returns to Mint Studio.
    win._personality_bubbles["paper-daylight"].click()
    app.processEvents()
    daylight = theme.PERSONALITY_BY_KEY["paper-daylight"]
    assert win._personality == daylight
    assert daylight.bg in win.styleSheet()

    win.personality_reset_button.click()
    app.processEvents()
    assert win._personality.key == theme.DEFAULT_PERSONALITY
    assert theme.saved_personality() == theme.DEFAULT_PERSONALITY
