"""The effects browser: catalog, applying tiles, previews and layout."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("gi")

from PySide6.QtWidgets import QApplication  # noqa: E402

from anycam import effect_thumbs, effects  # noqa: E402
from anycam.gui_many import ManyCamWindow  # noqa: E402
from anycam.model import Effects, Layer, Project, Scene, new_id  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("CACHE", "CONFIG", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))


def make_window(app, count=1):
    scene = Scene(new_id("scene"), "S")
    for index in range(count):
        scene.add_layer(Layer(f"layer{index}", "test", f"Cam {index}",
                              source="smpte", box=(0, 0, 1, 1), z=index + 1))
    project = Project(name="browser", scenes=[scene], active_scene=scene.id)
    window = ManyCamWindow(project)
    window.headless = True
    window._persist = False
    window.resize(1200, 800)
    window.show()
    app.processEvents()
    return window


def close(window, app):
    # Stop the engine for real: dropping the reference left its pipeline
    # PLAYING, and at interpreter exit an appsink called into a half torn-down
    # Python (SIGSEGV after the tests had passed, exit status 139).
    window._persist = False
    window.shutdown()
    window.close()
    app.processEvents()


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def open_effects(window, app):
    """Make the Effects tab the visible one, as a user opening it would."""
    window.side_tabs.setCurrentIndex(1)
    app.processEvents()


def show_category(window, name):
    window.effect_category.setCurrentIndex(window.effect_category.findData(name))


def click_tile(window, label):
    for row in range(window.effect_grid.count()):
        item = window.effect_grid.item(row)
        if item.text() == label:
            window._effect_item_clicked(item)
            return item
    raise AssertionError(f"no tile named {label!r}")


def test_every_category_offers_a_full_catalog(app):
    window = make_window(app)
    try:
        sizes = {}
        for name in window.EFFECT_CATEGORIES:
            show_category(window, name)
            sizes[name] = window.effect_grid.count()
        assert sizes["Looks"] >= 20 and sizes["Distort"] >= 15
        assert sizes["Retro TV"] >= 12 and sizes["Touch-up"] >= 4
        assert sizes["Frames"] >= 8 and sizes["Shapes"] >= 7
        assert sum(sizes.values()) >= 75
    finally:
        close(window, app)


def test_tiles_are_not_clipped_to_a_strip(app):
    window = make_window(app)
    try:
        show_category(window, "Distort")
        rect = window.effect_grid.visualItemRect(window.effect_grid.item(0))
        assert rect.height() >= 80 and rect.width() >= 100
    finally:
        close(window, app)


def test_previews_are_drawn_for_every_tile(app):
    window = make_window(app)
    try:
        open_effects(window, app)
        show_category(window, "Retro TV")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and window._thumb_queue:
            pump(app, 0.1)
        assert not window._thumb_queue
        missing = [window.effect_grid.item(row).text()
                   for row in range(window.effect_grid.count())
                   if window.effect_grid.item(row).icon().isNull()]
        # "No filter" and every real filter must have a picture.
        assert not missing, missing
    finally:
        close(window, app)


def test_clicking_a_filter_tile_sets_it_and_shows_the_strength_slider(app):
    window = make_window(app)
    layer = window.scene.layers[0]
    try:
        window.preview.select_layer(layer.id)
        show_category(window, "Distort")
        assert window.filter_strength_row.isHidden()
        click_tile(window, "Twirl")
        assert layer.effects.filter == "twirl"
        assert not window.filter_strength_row.isHidden()
        window._effect_sliders["filter_amount"].setValue(20)
        assert layer.effects.filter_amount == pytest.approx(0.2)
        click_tile(window, "No filter")
        assert layer.effects.filter == "none"
        assert window.filter_strength_row.isHidden()
    finally:
        close(window, app)


def test_a_filter_without_an_amount_hides_the_slider(app):
    window = make_window(app)
    layer = window.scene.layers[0]
    try:
        window.preview.select_layer(layer.id)
        show_category(window, "Distort")
        click_tile(window, "Fisheye")
        assert layer.effects.filter == "fisheye"
        assert window.filter_strength_row.isHidden()
    finally:
        close(window, app)


def test_effects_from_different_categories_stack(app):
    window = make_window(app)
    layer = window.scene.layers[0]
    try:
        window.preview.select_layer(layer.id)
        show_category(window, "Distort")
        click_tile(window, "Bulge")
        show_category(window, "Looks")
        click_tile(window, "Sepia")
        click_tile(window, "Noir")
        show_category(window, "Shapes")
        click_tile(window, "Heart window")
        fx = layer.effects
        assert fx.filter == "bulge"               # other categories stack
        # Within Looks one look at a time (2026-11-08): keeping the film look
        # under a colour look left Sepia on through every later look, which
        # users saw as swapping not working.
        assert fx.color_effect == "none"
        assert fx.saturation == 0.0               # Noir's values
        assert fx.frame_style == "heart"
    finally:
        close(window, app)


def test_apply_to_all_sources_reaches_every_visible_capture(app):
    window = make_window(app, count=3)
    window.scene.layers[2].visible = False
    try:
        window.preview.select_layer(window.scene.layers[0].id)
        window.effects_all.setChecked(True)
        show_category(window, "Retro TV")
        click_tile(window, "Old film")
        filters = [layer.effects.filter for layer in window.scene.layers]
        assert filters == ["aging", "aging", "none"]   # the hidden one is left alone
    finally:
        close(window, app)


def test_clicking_a_tile_with_nothing_selected_applies_to_the_main_picture(app):
    """With no selection the effect goes to the top camera/screen/video layer."""
    window = make_window(app, count=2)
    try:
        window.preview.select_layer("")
        show_category(window, "Distort")
        click_tile(window, "Pinch")
        low, top = window.scene.layers
        assert top.effects.filter == "pinch"
        assert low.effects.filter == "none"
    finally:
        close(window, app)


def test_clicking_a_tile_with_no_picture_at_all_explains_itself(app):
    window = make_window(app)
    try:
        window.scene.layers.clear()
        window._refresh_layers()
        window.preview.select_layer("")
        show_category(window, "Distort")
        click_tile(window, "Pinch")
        assert "Select a layer" in window.statusBar().currentMessage()
    finally:
        close(window, app)


def test_the_current_effect_is_highlighted_and_follows_the_selection(app):
    window = make_window(app, count=2)
    first, second = window.scene.layers
    first.effects = Effects(filter="sphere")
    second.effects = Effects(filter="tunnel")
    try:
        show_category(window, "Distort")
        window.preview.select_layer(first.id)
        app.processEvents()
        assert window.effect_grid.currentItem().text() == "Sphere"
        window.preview.select_layer(second.id)
        app.processEvents()
        assert window.effect_grid.currentItem().text() == "Tunnel"
    finally:
        close(window, app)


def test_a_tile_choice_marks_the_project_changed(app):
    window = make_window(app)
    window._persist = True
    window.headless = False
    window._dirty = False
    layer = window.scene.layers[0]
    try:
        window.preview.select_layer(layer.id)
        show_category(window, "Touch-up")
        click_tile(window, "Soft focus")
        assert window._dirty
    finally:
        window._autosave_timer.stop()
        close(window, app)


def test_matte_colour_can_be_edited_on_the_adjust_page(app):
    window = make_window(app)
    layer = window.scene.layers[0]
    try:
        window.preview.select_layer(layer.id)
        window.frame_matte.setText("#224466")
        window.frame_matte.editingFinished.emit()
        assert layer.effects.frame_matte == "#224466"
    finally:
        close(window, app)


def test_thumbnail_renderer_applies_the_real_effect():
    sample = effect_thumbs.default_sample()
    plain = effect_thumbs.render_thumbnail(Effects(), sample)
    twirled = effect_thumbs.render_thumbnail(
        Effects(filter="twirl", filter_amount=0.9), sample)
    framed = effect_thumbs.render_thumbnail(
        Effects(frame_style="heart", frame_color="#ff0000"), sample)
    assert plain is not None and twirled is not None and framed is not None
    assert plain.size == effect_thumbs.THUMB_SIZE
    assert plain.tobytes() != twirled.tobytes()
    assert framed.getpixel((2, 2))[:3] == (11, 13, 16)       # matte outside the heart


def test_every_available_filter_has_a_thumbnail():
    sample = effect_thumbs.default_sample()
    for spec in effects.available_filters():
        picture = effect_thumbs.render_thumbnail(
            Effects(filter=spec.key, filter_amount=spec.default), sample)
        assert picture is not None, spec.key


def test_no_previews_are_rendered_while_the_tab_is_closed(app):
    window = make_window(app)
    try:
        pump(app, 0.5)
        assert window._thumb_queue, "tiles should wait for the tab to be opened"
        assert not window._thumb_timer.isActive()
        assert all(window.effect_grid.item(row).icon().isNull()
                   for row in range(window.effect_grid.count()))
    finally:
        close(window, app)


def test_opening_the_tab_starts_rendering_the_previews(app):
    window = make_window(app)
    try:
        open_effects(window, app)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and window._thumb_queue:
            pump(app, 0.1)
        assert not window._thumb_queue
        assert all(not window.effect_grid.item(row).icon().isNull()
                   for row in range(window.effect_grid.count()))
    finally:
        close(window, app)


def test_a_closed_window_leaves_no_preview_work_behind(app):
    """Spinning an event loop later must not render a dead window's tiles."""
    window = make_window(app)
    open_effects(window, app)
    assert window._thumb_queue
    close(window, app)
    assert not window._thumb_timer.isActive()
    assert not window._thumb_queue
