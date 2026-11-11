"""The Effects tab: browse, adjust, presets, AI model notes."""
from __future__ import annotations


import copy
import threading

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QScrollArea, QSlider, QTabWidget, QVBoxLayout, QWidget,
)

from .. import overlay
from ..model import (Effects, Layer)
from .. import effect_thumbs, effects as effect_library
from .. import emoji as emoji_library, frames as frames_library, particles as particles_library
from .effect_presets import EFFECT_PRESETS, EFFECT_STYLES  # noqa: F401
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class EffectsPanelMixin:
    """The Effects tab: browse, adjust, presets, AI model notes."""

    def _effects_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        # Adjust is built first: it owns the widgets the sync code fills in.
        adjust = self._effects_adjust_page()
        tabs.addTab(self._effects_browse_page(), "Browse")
        tabs.addTab(adjust, "Adjust")
        self.effects_tabs = tabs
        layout.addWidget(tabs)
        self._sync_effects_controls()
        return page

    def _effects_adjust_page(self) -> QWidget:
        page = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        fx = self._current_effects()
        self._syncing_effects = False
        self._effect_sliders: dict[str, QSlider] = {}
        self._effect_controls: list[QWidget] = []
        self._chroma_controls: list[QWidget] = []

        target = QGroupBox("Target")
        target_layout = QVBoxLayout(target)
        self.effects_target = QLabel("No layer selected")
        self.effects_target.setObjectName("accent")
        target_layout.addWidget(self.effects_target)
        self.effects_hint = QLabel(
            "Select a layer in the preview or Layers tab. Effects are per "
            "layer, so changing one source never alters the rest of the scene.")
        self.effects_hint.setObjectName("muted")
        self.effects_hint.setWordWrap(True)
        target_layout.addWidget(self.effects_hint)
        layout.addWidget(target)

        look = QGroupBox("Look")
        look_layout = QGridLayout(look)
        look_layout.addWidget(QLabel("Preset"), 0, 0)
        self.effect_preset = QComboBox()
        self.effect_preset.addItem("Custom", "")
        for name in EFFECT_PRESETS:
            self.effect_preset.addItem(name, name)
        self.effect_preset.currentIndexChanged.connect(self._apply_effect_preset)
        look_layout.addWidget(self.effect_preset, 0, 1)
        reset = QPushButton("Reset all effects")
        reset.clicked.connect(self._reset_effects)
        look_layout.addWidget(reset, 1, 0, 1, 2)
        layout.addWidget(look)
        self._effect_controls.extend((self.effect_preset, reset))

        colour = QGroupBox("Colour")
        colour_layout = QVBoxLayout(colour)
        for key, label, low, high, value, callback, display in (
            ("brightness", "Brightness", -100, 100, int(round(fx.brightness * 100)),
             lambda v: self._set_effect("brightness", v), None),
            ("contrast", "Contrast", 0, 200, int(round(fx.contrast * 100)),
             lambda v: self._set_effect("contrast", v), None),
            ("saturation", "Saturation", 0, 200, int(round(fx.saturation * 100)),
             lambda v: self._set_effect("saturation", v), None),
            ("hue", "Hue", -180, 180, int(round(fx.hue * 180)),
             lambda v: self._set_effect("hue", v), lambda v: f"{v}\u00b0"),
        ):
            holder, slider, _label = _slider_row(
                label, low, high, value, callback, display=display)
            self._effect_sliders[key] = slider
            colour_layout.addWidget(holder)
        layout.addWidget(colour)
        self._effect_controls.append(colour)

        style = QGroupBox("Film look")
        style_layout = QGridLayout(style)
        style_layout.addWidget(QLabel("Look"), 0, 0)
        self.effect_style = QComboBox()
        for label, value in EFFECT_STYLES.items():
            self.effect_style.addItem(label, value)
        index = self.effect_style.findData(fx.color_effect)
        self.effect_style.setCurrentIndex(index if index >= 0 else 0)
        self.effect_style.currentIndexChanged.connect(
            lambda: self._set_effect("color_effect", self.effect_style.currentData()))
        style_layout.addWidget(self.effect_style, 0, 1)
        self.effect_mirror = QCheckBox("Mirror horizontally")
        self.effect_mirror.setChecked(bool(fx.mirror))
        self.effect_mirror.toggled.connect(
            lambda checked: self._set_effect("mirror", bool(checked)))
        style_layout.addWidget(self.effect_mirror, 1, 0, 1, 2)
        style_note = QLabel(
            "Look-up-table looks and mirror are per-pixel/scanline work, "
            "so they stay live at camera speed.")
        style_note.setObjectName("muted")
        style_note.setWordWrap(True)
        style_layout.addWidget(style_note, 2, 0, 1, 2)
        layout.addWidget(style)
        self._effect_controls.append(style)

        frame = QGroupBox("Window frame")
        frame_layout = QGridLayout(frame)
        frame_layout.addWidget(QLabel("Shape"), 0, 0)
        self.frame_style = QComboBox()
        for value, label in overlay.WINDOW_FRAME_STYLES.items():
            self.frame_style.addItem(label, value)
        index = self.frame_style.findData(fx.frame_style)
        self.frame_style.setCurrentIndex(index if index >= 0 else 0)
        self.frame_style.currentIndexChanged.connect(self._frame_style_changed)
        frame_layout.addWidget(self.frame_style, 0, 1)
        frame_layout.addWidget(QLabel("Colour"), 1, 0)
        frame_colour_row = QHBoxLayout()
        self.frame_color = QLineEdit(fx.frame_color)
        self.frame_color.setMaxLength(9)
        self.frame_color.setPlaceholderText("#5eead0")
        self.frame_color.editingFinished.connect(
            lambda: self._set_effect("frame_color", self.frame_color.text()))
        frame_pick = QPushButton("Pick\u2026")
        frame_pick.clicked.connect(self._pick_frame_color)
        frame_colour_row.addWidget(self.frame_color, 1)
        frame_colour_row.addWidget(frame_pick)
        frame_layout.addLayout(frame_colour_row, 1, 1)
        holder, frame_width, _label = _slider_row(
            "Thickness", 1, 24, int(fx.frame_width),
            lambda v: self._set_effect("frame_width", v),
            display=lambda v: f"{v} px")
        self._effect_sliders["frame_width"] = frame_width
        frame_layout.addWidget(holder, 2, 0, 1, 2)
        frame_layout.addWidget(QLabel("Matte"), 3, 0)
        matte_row = QHBoxLayout()
        self.frame_matte = QLineEdit(fx.frame_matte)
        self.frame_matte.setMaxLength(9)
        self.frame_matte.setPlaceholderText("#0b0d10")
        self.frame_matte.editingFinished.connect(
            lambda: self._set_effect("frame_matte", self.frame_matte.text()))
        matte_pick = QPushButton("Pick\u2026")
        matte_pick.clicked.connect(self._pick_frame_matte)
        matte_row.addWidget(self.frame_matte, 1)
        matte_row.addWidget(matte_pick)
        frame_layout.addLayout(matte_row, 3, 1)
        frame_note = QLabel(
            "Frames follow the picture. Shaped windows (circle, heart, star ...), "
            "the TV screen and the vignette fill the area outside with the "
            "matte colour; the other styles draw a border in the frame colour.")
        frame_note.setObjectName("muted")
        frame_note.setWordWrap(True)
        frame_layout.addWidget(frame_note, 4, 0, 1, 2)
        layout.addWidget(frame)
        self._effect_controls.append(frame)
        self._frame_controls = [self.frame_color, frame_pick, frame_width,
                                self.frame_matte, matte_pick]

        chroma = QGroupBox("Chroma key")
        chroma_layout = QGridLayout(chroma)
        self.chroma_enabled = QCheckBox("Key out a colour")
        self.chroma_enabled.setChecked(fx.chroma_enabled)
        self.chroma_enabled.toggled.connect(self._chroma_toggled)
        chroma_layout.addWidget(self.chroma_enabled, 0, 0, 1, 2)
        chroma_layout.addWidget(QLabel("Key colour"), 1, 0)
        colour_row = QHBoxLayout()
        self.chroma_color = QLineEdit(fx.chroma_key)
        self.chroma_color.setMaxLength(9)
        self.chroma_color.setPlaceholderText("#00ff00")
        self.chroma_color.editingFinished.connect(
            lambda: self._set_effect("chroma_key", self.chroma_color.text()))
        pick = QPushButton("Pick\u2026")
        pick.clicked.connect(self._pick_chroma_color)
        colour_row.addWidget(self.chroma_color, 1)
        colour_row.addWidget(pick)
        chroma_layout.addLayout(colour_row, 1, 1)
        for key, label, value in (
            ("chroma_similarity", "Tolerance", int(round(fx.chroma_similarity * 100))),
            ("chroma_smoothness", "Edge softness", int(round(fx.chroma_smoothness * 100))),
        ):
            holder, slider, _label = _slider_row(
                label, 0, 100, value,
                lambda v, a=key: self._set_effect(a, v / 100.0))
            self._effect_sliders[key] = slider
            chroma_layout.addWidget(holder, 2 if key == "chroma_similarity" else 3,
                                    0, 1, 2)
        chroma_note = QLabel(
            "Pick the backdrop colour. Tolerance widens the key; edge softness "
            "blends the boundary instead of leaving a hard cutout.")
        chroma_note.setObjectName("muted")
        chroma_note.setWordWrap(True)
        chroma_layout.addWidget(chroma_note, 4, 0, 1, 2)
        layout.addWidget(chroma)
        self._effect_controls.append(chroma)
        self._chroma_controls.extend((self.chroma_color, pick,
                                      self._effect_sliders["chroma_similarity"],
                                      self._effect_sliders["chroma_smoothness"]))
        self._update_frame_controls()

        layout.addStretch(1)

        live = QLabel("Changes apply live \u00b7 no canvas rebuild, no output interruption")
        live.setObjectName("muted")
        live.setWordWrap(True)
        layout.addWidget(live)
        scroll.setWidget(body)
        container = QVBoxLayout(page)
        container.addWidget(scroll)
        self._sync_effects_controls()
        return page

    # ------------------------------------------------------- effects browser
    def _effects_browse_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.browse_target = QLabel("No layer selected")
        self.browse_target.setObjectName("accent")
        layout.addWidget(self.browse_target)
        self.effects_all = QCheckBox("Apply to all sources")
        self.effects_all.setToolTip(
            "Apply the chosen effect to every visible camera, screen, video, "
            "network and pattern layer instead of only the selected one.")
        layout.addWidget(self.effects_all)

        row = QHBoxLayout()
        self.effect_category = QComboBox()
        for name in self.EFFECT_CATEGORIES:
            self.effect_category.addItem(name, name)
        self.effect_category.currentIndexChanged.connect(self._populate_effect_grid)
        row.addWidget(self.effect_category, 1)
        self.filter_slot = QComboBox()
        self.filter_slot.addItem("Filter 1", 1)
        self.filter_slot.addItem("Filter 2", 2)
        self.filter_slot.setToolTip(
            "A layer has two filter slots, so two filters can be on at once "
            "(for example a portrait effect and a distortion).")
        self.filter_slot.currentIndexChanged.connect(self._filter_slot_changed)
        row.addWidget(self.filter_slot)
        self.thumb_refresh = QPushButton("Use picture")
        self.thumb_refresh.setToolTip(
            "Redraw the previews from what the program shows right now.")
        self.thumb_refresh.clicked.connect(self._refresh_effect_previews)
        row.addWidget(self.thumb_refresh)
        layout.addLayout(row)

        tools = QHBoxLayout()
        self.effect_search = QLineEdit()
        self.effect_search.setPlaceholderText("Search all effects\u2026")
        self.effect_search.setClearButtonEnabled(True)
        self.effect_search.textChanged.connect(self._populate_effect_grid)
        tools.addWidget(self.effect_search, 1)
        for label, tip, handler in (
                ("Copy", "Copy this layer's effects", self._copy_effects),
                ("Paste", "Paste the copied effects onto the selected layer(s)",
                 self._paste_effects),
                ("Clear", "Remove every effect from the selected layer(s)",
                 self._clear_effects)):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            tools.addWidget(button)
        layout.addLayout(tools)

        self.effect_grid = QListWidget()
        self.effect_grid.setObjectName("effectGrid")
        # The app-wide item padding breaks icon-mode sizing (tiles were clipped
        # to a strip), so the grid carries its own rules.
        self.effect_grid.setStyleSheet(
            "QListWidget#effectGrid::item { padding: 0px; margin: 1px; "
            "border: 2px solid transparent; border-radius: 7px; color: #9fb0bb; }"
            "QListWidget#effectGrid::item:selected { border: 2px solid #5eead0; "
            "background: #12302d; color: #5eead0; }"
            "QListWidget#effectGrid::item:hover { background: #16222a; }")
        self.effect_grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.effect_grid.setIconSize(QSize(112, 63))
        self.effect_grid.setGridSize(QSize(126, 96))
        self.effect_grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.effect_grid.setMovement(QListWidget.Movement.Static)
        self.effect_grid.setWordWrap(True)
        self.effect_grid.setSpacing(2)
        self.effect_grid.setUniformItemSizes(True)
        self.effect_grid.itemClicked.connect(self._effect_item_clicked)
        self.effect_grid.installEventFilter(self)
        layout.addWidget(self.effect_grid, 1)

        holder, strength, label = _slider_row(
            "Strength", 0, 100, 50,
            lambda v: self._set_effect(self._slot_attr() + "_amount", v / 100.0))
        self._effect_sliders["filter_amount"] = strength
        self.filter_strength_row = holder
        layout.addWidget(holder)
        self.effect_note = QLabel(
            "Click a tile to apply it. Effects stack: a look, a filter and a "
            "frame can all be on at once. Changes are live.")
        self.effect_note.setObjectName("muted")
        self.effect_note.setWordWrap(True)
        layout.addWidget(self.effect_note)

        self._thumb_sample_image = None
        self._thumb_sample_default = True
        self._thumb_cache: dict[tuple, QPixmap] = {}
        # Tiles still to show, the first one possibly being drawn by the
        # background renderer (effect_thumbs.TileRenderer).
        self._thumb_queue: list[tuple] = []
        self._thumb_renderer = effect_thumbs.TileRenderer()
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(30)
        self._thumb_timer.timeout.connect(self._render_pending_thumbs)
        self._effect_entries: list[dict] = []
        self._populate_effect_grid()
        return page

    def _effect_catalog(self, category: str) -> list[dict]:
        entries = self._effect_catalog_raw(category)
        for entry in entries:
            entry.setdefault("category", category)
        return entries

    def _search_catalog(self, text: str) -> list[dict]:
        """Every tile, in any category, whose name or description matches."""
        needle = text.strip().lower()
        found: list[dict] = []
        for category in self.EFFECT_CATEGORIES:
            for entry in self._effect_catalog(category):
                if entry["label"].startswith("No "):
                    continue          # "No filter" / "No frame" belong to their category
                haystack = f"{entry['label']} {entry.get('tip', '')} {category}".lower()
                if all(word in haystack for word in needle.split()):
                    found.append(entry)
        return found

    def _effect_catalog_raw(self, category: str) -> list[dict]:
        """The tiles of one category: label, what they set, how they preview."""
        entries: list[dict] = []
        if category == "Looks":
            # One look at a time: a colour preset clears the film style and a
            # film style resets the colour sliders.  Applying only the clicked
            # tile's own fields left an earlier Sepia on through every later
            # look, "Original" included, so swapping looked broken.
            neutral = dict(EFFECT_PRESETS["Original"])
            for name, values in EFFECT_PRESETS.items():
                look = {**values, "color_effect": "none"}
                entries.append({"label": name, "kind": "look",
                                "apply": look, "preview": dict(look)})
            for label, value in EFFECT_STYLES.items():
                if value != "none":
                    film = {**neutral, "color_effect": value}
                    entries.append({"label": label, "kind": "film",
                                    "apply": film, "preview": dict(film)})
        elif category in effect_library.FILTER_CATEGORIES:
            entries.append({"label": "No filter", "kind": "filter",
                            "apply": {"filter": "none"}, "preview": {}})
            for spec in effect_library.available_filters(category):
                values = {"filter": spec.key, "filter_amount": spec.default}
                entries.append({"label": spec.label, "kind": "filter",
                                "apply": values, "preview": values,
                                "tip": spec.description,
                                "amount": bool(spec.prop)})
        elif category in self.FRAME_GROUPS:
            entries.append({"label": "No frame", "kind": "frame",
                            "apply": {"frame_style": "none"}, "preview": {}})
            for key, (label, group, _painter) in frames_library.THEMES.items():
                if group == self.FRAME_GROUPS[category]:
                    values = {"frame_style": key}
                    entries.append({"label": label, "kind": "frame", "apply": values,
                                    "preview": dict(values),
                                    "tip": f"{label}: a finished {group.lower()} frame"})
        elif category == "Animations":
            entries.append({"label": "No animation", "kind": "particles", "source": "",
                            "apply": {}, "preview": {}, "tip": "Remove the animation"})
            for key, (label, group, _factory) in particles_library.PRESETS.items():
                entries.append({"label": label, "kind": "particles", "source": key,
                                "apply": {}, "preview": {}, "particles": key,
                                "tip": f"{group}: adds a moving {label.lower()} layer on top"})
        elif category == "Emoji rain":
            entries.append({"label": "No animation", "kind": "particles", "source": "",
                            "apply": {}, "preview": {}, "tip": "Remove the animation"})
            for code, name, group, words in emoji_library.available():
                entries.append({"label": name, "kind": "particles", "source": f"emoji:{code}",
                                "apply": {}, "preview": {}, "particles": f"emoji:{code}",
                                "tip": f"{group}: makes {name.lower()} fall across the picture ({words})"})
        elif category in ("Frames", "Shapes"):
            entries.append({"label": "No frame", "kind": "frame",
                            "apply": {"frame_style": "none"}, "preview": {}})
            for key, label in overlay.WINDOW_FRAME_STYLES.items():
                shaped = key in overlay.SHAPED_FRAME_STYLES
                if (key == "none" or key in frames_library.THEMES
                        or shaped != (category == "Shapes")):
                    continue
                values = {"frame_style": key}
                entries.append({"label": label, "kind": "frame",
                                "apply": values,
                                "preview": {**values, "frame_color": "#ff4d7a",
                                            "frame_width": 6}})
        return entries

    def _slot_attr(self) -> str:
        """The Effects field of the chosen filter slot: ``filter`` or ``filter2``."""
        combo = getattr(self, "filter_slot", None)
        return "filter2" if combo is not None and combo.currentData() == 2 else "filter"

    def _filter_slot_changed(self, *_args) -> None:
        self._sync_effects_controls()

    def _effect_entry_matches(self, entry: dict, fx: Effects) -> bool:
        apply = entry["apply"]
        if entry["kind"] in ("look", "film"):
            return all((getattr(fx, key) == value) if isinstance(value, str)
                       else abs(float(getattr(fx, key)) - float(value)) < 1e-4
                       for key, value in apply.items())
        if entry["kind"] == "particles":
            current = self._animation_layers()
            if not entry["source"]:
                return not current
            return bool(current) and current[0].source == entry["source"]
        if entry["kind"] == "filter":
            return getattr(fx, self._slot_attr()) == apply["filter"]
        return all(getattr(fx, key) == value for key, value in apply.items()
                   if key != "filter_amount")

    def _thumb_sample(self):
        if self._thumb_sample_image is None:
            image = getattr(self.preview, "image", None)
            self._thumb_sample_default = image is None or image.isNull()
            self._thumb_sample_image = self._current_picture_sample()
        return self._thumb_sample_image

    def _current_picture_sample(self):
        """The program picture for previews, or the built-in sample."""
        from PIL import Image

        image = getattr(self.preview, "image", None)
        if image is not None and not image.isNull():
            rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
            width, height = rgba.width(), rgba.height()
            data = bytes(rgba.constBits())[: width * height * 4]
            return Image.frombytes("RGBA", (width, height), data).convert("RGB")
        return effect_thumbs.default_sample()

    def _populate_effect_grid(self, *_args) -> None:
        category = str(self.effect_category.currentData() or "Looks")
        query = self.effect_search.text() if hasattr(self, "effect_search") else ""
        self._effect_entries = (self._search_catalog(query) if query.strip()
                                else self._effect_catalog(category))
        self.effect_grid.clear()
        self._thumb_queue.clear()
        self._thumb_renderer.forget()
        sample_id = id(self._thumb_sample_image)
        for index, entry in enumerate(self._effect_entries):
            item = QListWidgetItem(entry["label"])
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(entry.get("tip") or entry["label"])
            # An ::item style rule makes Qt size a tile from its text alone,
            # which clipped the thumbnail to a strip; state the size outright.
            item.setSizeHint(QSize(122, 92))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter
                                  | Qt.AlignmentFlag.AlignTop)
            key_category = entry.get("category", category)
            cached = self._thumb_cache.get((key_category, entry["label"], sample_id))
            if cached is not None:
                item.setIcon(QIcon(cached))
            else:
                self._thumb_queue.append((item, key_category, entry, sample_id))
            self.effect_grid.addItem(item)
        if self._thumb_queue and self.effect_grid.isVisible():
            self._thumb_timer.start()
        self._select_current_effect_tile()

    def _render_pending_thumbs(self) -> None:
        """Show finished tiles and hand the next one to the background renderer.

        The GUI thread only turns a finished picture into an icon; drawing a
        tile (up to ~220 ms for a mask) happens on the renderer's thread.
        Only while the grid is on screen: tiles for a tab nobody opened (or a
        window that has closed) are not worth the work, and the queue is
        picked up again when the grid is shown.
        """
        finished = self._thumb_renderer.take()
        if finished is not None:
            token, picture = finished
            if self._thumb_queue and self._thumb_queue[0] is token:
                self._thumb_queue.pop(0)
                item, category, entry, sample_id = token
                if picture is not None:
                    width, height = picture.size
                    image = QImage(picture.tobytes(), width, height, width * 3,
                                   QImage.Format.Format_RGB888).copy()
                    pixmap = QPixmap.fromImage(image)
                    self._thumb_cache[(category, entry["label"], sample_id)] = pixmap
                    try:
                        item.setIcon(QIcon(pixmap))
                    except RuntimeError:
                        pass  # the list was repopulated while this tile was drawn
        if not self.effect_grid.isVisible():
            self._thumb_timer.stop()
            return
        if not self._thumb_queue:
            self._thumb_timer.stop()
            return
        if not self._thumb_renderer.busy:
            token = self._thumb_queue[0]
            _item, category, entry, _sample_id = token
            sample = self._thumb_sample()
            self._thumb_renderer.submit(token, category, entry, sample,
                                        self._thumb_sample_default)

    def _refresh_effect_previews(self) -> None:
        self._thumb_sample_image = None
        self._thumb_sample()
        self._thumb_cache.clear()
        self._populate_effect_grid()

    def _copy_effects(self) -> None:
        layer = self._effects_layer()
        if layer is None:
            self._status("Select a layer to copy effects from")
            return
        self._effects_clipboard = copy.deepcopy(layer.effects)
        self._status(f"Copied the effects of {layer.name}")

    def _paste_effects(self) -> None:
        clipboard = getattr(self, "_effects_clipboard", None)
        targets = self._effect_targets()
        if clipboard is None or not targets:
            self._status("Copy a layer's effects first, then select where to paste")
            return
        for layer in targets:
            layer.effects = copy.deepcopy(clipboard)
            self._apply_effects(layer)
        self._sync_effects_controls()
        self._mark_dirty()
        self._status(f"Pasted effects onto {len(targets)} layer(s)")

    def _clear_effects(self) -> None:
        targets = self._effect_targets()
        if not targets:
            self._status("Select a layer in the preview or Layers tab first")
            return
        for layer in targets:
            layer.effects = Effects()
            self._apply_effects(layer)
        self._sync_effects_controls()
        self._mark_dirty()
        self._status(f"Cleared the effects of {len(targets)} layer(s)")

    def _effect_targets(self) -> list[Layer]:
        if self.effects_all.isChecked():
            return [layer for layer in self.scene.layers
                    if layer.visible and layer.kind in self._CAPTURE_KINDS]
        layer = self._effects_layer()
        return [layer] if layer is not None else []

    def _animation_layers(self) -> list[Layer]:
        return [layer for layer in self.scene.layers if layer.kind == "particles"]

    def _set_animation(self, source: str) -> None:
        """Show one animation at a time: swap it live, toggle it off, or clear it.

        Several full-canvas animation layers piled up (one per click) and
        starved the canvas, so there is only ever one.  Picking another swaps the
        generator inside the running layer; picking the active one again, or
        "No animation", removes it.
        """
        from .. import particles

        current = self._animation_layers()
        if not source or (current and current[0].source == source):
            if current:
                self.remove_layers([layer.id for layer in current])
                self._status("Animation removed")
            self._select_current_effect_tile()
            return
        if current:
            keep, extra = current[0], current[1:]
            if extra:
                self.remove_layers([layer.id for layer in extra])
            before = self._layout_snapshot()
            keep.source = source
            keep.name = particles.label_for(source)
            if self.engine is not None and not self.engine.update_overlay(keep):
                self.remove_layers([keep.id])
                self.add_layer("particles", source=source)
            self._refresh_layers()
            self._record_history(before)
            self._mark_dirty()
        else:
            self.add_layer("particles", source=source)
        self._status(f"Animation: {particles.label_for(source)} (click it again to remove)")
        self._select_current_effect_tile()

    def _effect_item_clicked(self, item) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is None or not 0 <= int(index) < len(self._effect_entries):
            return
        entry = self._effect_entries[int(index)]
        if entry["kind"] == "particles":
            self._set_animation(entry["source"])
            return
        targets = self._effect_targets()
        if not targets:
            self._status("Select a layer in the preview or Layers tab first")
            return
        attr = self._slot_attr()
        for layer in targets:
            for key, value in entry["apply"].items():
                if entry["kind"] == "filter":
                    # Filter tiles go to the chosen slot.
                    key = key.replace("filter", attr, 1)
                setattr(layer.effects, key, value)
            self._apply_effects(layer)
        self._sync_effects_controls()
        self._mark_dirty()
        count = f" on {len(targets)} sources" if len(targets) > 1 else ""
        self._status(f"{entry['label']}{count}")

    def _select_current_effect_tile(self) -> None:
        layer = self._effects_layer()
        if layer is None or not hasattr(self, "effect_grid"):
            return
        self.effect_grid.blockSignals(True)
        try:
            self.effect_grid.clearSelection()
            for index, entry in enumerate(self._effect_entries):
                if self._effect_entry_matches(entry, layer.effects):
                    self.effect_grid.setCurrentRow(index)
                    break
        finally:
            self.effect_grid.blockSignals(False)

    def _sync_filter_strength(self, fx: Effects) -> None:
        attr = self._slot_attr()
        spec = effect_library.FILTERS.get(getattr(fx, attr))
        has_amount = bool(spec is not None and spec.prop)
        self.filter_strength_row.setVisible(has_amount)
        slider = self._effect_sliders.get("filter_amount")
        if slider is not None and has_amount:
            slider.blockSignals(True)
            slider.setValue(int(round(getattr(fx, attr + "_amount") * 100)))
            slider.blockSignals(False)

    def _current_effects(self) -> Effects:
        layer = self._effects_layer()
        return layer.effects if layer is not None else Effects()

    def _effects_layer(self) -> Layer | None:
        """The layer the Effects panel edits.

        That is the selected layer.  With nothing selected, or only an animation
        layer selected (effects mean nothing to those), it is the main picture:
        the top visible camera, screen, video, network or pattern layer.  Without
        this, clicking a frame or filter straight after opening the app did
        nothing but flash a status message.
        """
        if not hasattr(self, "preview"):
            return None
        selected = self.preview.selected_layer()
        if selected is not None and selected.kind != "particles":
            return selected
        for kinds in (self._CAPTURE_KINDS, ("image",)):
            # Live sources first; a still image only when it is the picture.
            # Never the stage: it is a background, not the picture.
            main = [layer for layer in self.scene.layers
                    if layer.visible and layer.kind in kinds
                    and getattr(layer, "role", "") != "backdrop"]
            if main:
                return max(main, key=lambda layer: layer.z)
        return selected

    def _apply_effects(self, layer: Layer) -> None:
        """Push effect values onto the running layer without rebuilding."""
        if self.engine is None:
            return
        apply = getattr(self.engine, "set_layer_effects", None)
        if apply is not None and not apply(layer.id, layer.effects):
            if (getattr(layer, "role", "") == "backdrop"
                    and layer.id in getattr(self.engine, "_layer_chains", {})):
                # This layer was built without a live effect rack (a stage's
                # fast path): rebuild so the effect is not silently dropped.
                self.restart_engine()
        self.preview.update()

    def _set_effect(self, attr: str, value) -> None:
        if getattr(self, "_syncing_effects", False):
            return
        layer = self._effects_layer()
        if layer is None:
            self._status("Select a layer in the preview or Layers tab first")
            return
        fx = layer.effects
        if attr == "brightness":
            fx.brightness = max(-1.0, min(1.0, float(value) / 100.0))
        elif attr in ("contrast", "saturation"):
            setattr(fx, attr, max(0.0, min(2.0, float(value) / 100.0)))
        elif attr == "hue":
            fx.hue = max(-1.0, min(1.0, float(value) / 180.0))
        elif attr in ("chroma_similarity", "chroma_smoothness"):
            setattr(fx, attr, max(0.0, min(1.0, float(value))))
        elif attr == "chroma_key":
            fx.chroma_key = str(value).strip() or "#00ff00"
        elif attr == "color_effect":
            choice = str(value or "none").lower()
            fx.color_effect = choice if choice in EFFECT_STYLES.values() else "none"
        elif attr == "mirror":
            fx.mirror = bool(value)
            if (layer.kind in ("image", "logo") and any(layer.crop)
                    and self.engine is not None):
                # The crop of a picture is rendered into its PNG and depends on
                # the mirror (left/right swap), so it cannot change live.
                self._mark_dirty()
                self.restart_engine()
                return
        elif attr == "frame_style":
            choice = str(value or "none").lower()
            fx.frame_style = choice if choice in overlay.WINDOW_FRAME_STYLES else "none"
            self._update_frame_controls()
        elif attr == "frame_color":
            fx.frame_color = str(value).strip() or "#5eead0"
        elif attr == "frame_matte":
            fx.frame_matte = str(value).strip() or "#0b0d10"
        elif attr in ("filter_amount", "filter2_amount"):
            setattr(fx, attr, max(0.0, min(1.0, float(value))))
        elif attr == "frame_width":
            fx.frame_width = max(1, min(48, int(value)))
        else:
            setattr(fx, attr, value)
        self._apply_effects(layer)
        self._mark_dirty()

    def _chroma_toggled(self, enabled: bool) -> None:
        if getattr(self, "_syncing_effects", False):
            return
        self._set_effect("chroma_enabled", bool(enabled))
        self._sync_effects_controls()

    def _frame_style_changed(self, _index: int = 0) -> None:
        self._set_effect("frame_style", self.frame_style.currentData())

    def _pick_frame_color(self) -> None:
        current = QColor(self.frame_color.text().strip())
        if not current.isValid():
            current = QColor("#5eead0")
        colour = QColorDialog.getColor(current, self, "Window frame colour")
        if not colour.isValid():
            return
        self.frame_color.setText(colour.name())
        self._set_effect("frame_color", colour.name())

    def _pick_frame_matte(self) -> None:
        current = QColor(self.frame_matte.text().strip())
        if not current.isValid():
            current = QColor("#0b0d10")
        colour = QColorDialog.getColor(current, self, "Matte colour")
        if not colour.isValid():
            return
        self.frame_matte.setText(colour.name())
        self._set_effect("frame_matte", colour.name())

    def _update_frame_controls(self) -> None:
        layer = self._effects_layer()
        enabled = bool(layer is not None
                       and layer.effects.frame_style != "none")
        for widget in getattr(self, "_frame_controls", []):
            widget.setEnabled(enabled)

    def _pick_chroma_color(self) -> None:
        current = QColor(self.chroma_color.text().strip())
        if not current.isValid():
            current = QColor("#00ff00")
        colour = QColorDialog.getColor(current, self, "Chroma key colour")
        if not colour.isValid():
            return
        self.chroma_color.setText(colour.name())
        self._set_effect("chroma_key", colour.name())

    def _apply_effect_preset(self, _index: int = 0) -> None:
        if getattr(self, "_syncing_effects", False):
            return
        name = self.effect_preset.currentData()
        if not name or name not in EFFECT_PRESETS:
            return
        layer = self._effects_layer()
        if layer is None:
            self._status("Select a layer before choosing an effect preset")
            return
        for attr, value in EFFECT_PRESETS[name].items():
            setattr(layer.effects, attr, float(value))
        self._apply_effects(layer)
        self._sync_effects_controls()
        self._mark_dirty()
        self._status(f"Effect preset: {name}")

    def _match_effect_preset(self, fx: Effects) -> str:
        for name, values in EFFECT_PRESETS.items():
            if all(abs(float(getattr(fx, attr, 0.0)) - value) < 1e-4
                   for attr, value in values.items()):
                return name
        return ""

    def _reset_effects(self) -> None:
        layer = self._effects_layer()
        if layer is None:
            self._status("Select a layer before resetting effects")
            return
        layer.effects = Effects()
        self._apply_effects(layer)
        self._sync_effects_controls()
        self._mark_dirty()
        self._status(f"Reset effects on {layer.name}")

    def _sync_effects_controls(self) -> None:
        """Mirror the selected layer's effect values into the panel."""
        if not hasattr(self, "effects_target"):
            return
        layer = self._effects_layer()
        self._syncing_effects = True
        try:
            enabled = layer is not None
            for widget in self._effect_controls:
                widget.setEnabled(enabled)
            if hasattr(self, "effect_grid"):
                self.browse_target.setText(
                    f"{layer.name}  \u00b7  {layer.kind}" if layer is not None
                    else "No layer selected")
                if layer is not None:
                    self._sync_filter_strength(layer.effects)
                    self._select_current_effect_tile()
                else:
                    self.filter_strength_row.setVisible(False)
            if layer is None:
                self.effects_target.setText("No layer selected")
                self.effects_hint.setText(
                    "Select a layer in the preview or Layers tab. Effects are "
                    "per layer, so changing one source never alters the rest "
                    "of the scene.")
                self._update_frame_controls()
                self._update_chroma_controls()
                self.effect_preset.setCurrentIndex(0)
                return
            fx = layer.effects
            self.effects_target.setText(f"{layer.name}  \u00b7  {layer.kind}")
            self.effects_hint.setText(
                "These settings apply only to the selected layer. Changes are "
                "written straight to the running graph.")
            self._effect_sliders["brightness"].setValue(
                int(round(fx.brightness * 100)))
            self._effect_sliders["contrast"].setValue(
                int(round(fx.contrast * 100)))
            self._effect_sliders["saturation"].setValue(
                int(round(fx.saturation * 100)))
            self._effect_sliders["hue"].setValue(int(round(fx.hue * 180)))
            style_index = self.effect_style.findData(fx.color_effect)
            self.effect_style.setCurrentIndex(style_index if style_index >= 0 else 0)
            self.effect_mirror.setChecked(bool(fx.mirror))
            frame_index = self.frame_style.findData(fx.frame_style)
            self.frame_style.setCurrentIndex(frame_index if frame_index >= 0 else 0)
            self.frame_color.setText(fx.frame_color or "#5eead0")
            self.frame_matte.setText(fx.frame_matte or "#0b0d10")
            self._effect_sliders["frame_width"].setValue(int(fx.frame_width))
            self._update_frame_controls()
            self._effect_sliders["chroma_similarity"].setValue(
                int(round(fx.chroma_similarity * 100)))
            self._effect_sliders["chroma_smoothness"].setValue(
                int(round(fx.chroma_smoothness * 100)))
            self.chroma_enabled.setChecked(bool(fx.chroma_enabled))
            self.chroma_color.setText(fx.chroma_key or "#00ff00")
            self._update_chroma_controls()
            name = self._match_effect_preset(fx)
            index = self.effect_preset.findData(name)
            self.effect_preset.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._syncing_effects = False

    def _update_chroma_controls(self) -> None:
        enabled = bool(getattr(self, "chroma_enabled", None)
                       and self.chroma_enabled.isChecked()
                       and self._effects_layer() is not None)
        for widget in getattr(self, "_chroma_controls", []):
            widget.setEnabled(enabled)

    def _stop_thumbnails(self) -> None:
        timer = getattr(self, "_thumb_timer", None)
        if timer is not None:
            timer.stop()
        queue = getattr(self, "_thumb_queue", None)
        if queue is not None:
            queue.clear()
        renderer = getattr(self, "_thumb_renderer", None)
        if renderer is not None:
            renderer.forget()

    def _update_effects_device_note(self) -> None:
        from .. import inference

        cuda = inference.gpu_available()
        self.effects_device_note.setText(
            f"Now running on: {inference.describe()}. Background, face and "
            "mask effects use these models; the GPU is shared with the canvas "
            "and the encoder, so the CPU is the default."
            + ("" if cuda else
               " To use the GPU, replace onnxruntime with onnxruntime-gpu and "
               "its CUDA/cuDNN libraries: pip uninstall onnxruntime && "
               "pip install 'onnxruntime-gpu[cuda,cudnn]'."))

    def _update_segmentation_note(self) -> None:
        from .. import models as model_catalog

        rvm = model_catalog.CATALOG["rvm"]
        have = model_catalog.model_path("rvm") is not None
        wanted = self.segmentation_combo.currentData() == "rvm"
        downloading = self._model_download is not None
        self.model_download_button.setVisible(wanted and not have)
        self.model_download_button.setEnabled(not downloading)
        if not wanted:
            text = ("MediaPipe selfie segmentation, bundled with AnyCam "
                    "(Apache-2.0). Fast; edges are softer.")
        elif have:
            text = (f"In use: {rvm.label}. Soft edges and hair, stable between "
                    f"frames. Licence {rvm.license}, from "
                    f"<a href='{rvm.source}'>its author</a>.")
        else:
            text = (f"{rvm.note} Click Download to fetch it once "
                    f"({rvm.size / 1e6:.0f} MB from <a href='{rvm.source}'>"
                    "github.com/PeterL1n</a>, checked against a pinned SHA-256). "
                    "Until then the standard model is used.")
        if downloading:
            done, total = self._model_download.get("progress", (0, rvm.size))
            text = f"Downloading… {done / 1e6:.1f} / {total / 1e6:.0f} MB"
        self.segmentation_note.setText(text)

    def _segmentation_changed(self, _index: int = 0) -> None:
        from .. import segment

        value = str(self.segmentation_combo.currentData() or "mediapipe")
        self.project.output.segmentation_model = value
        self._mark_dirty()
        segment.set_model(value)
        self._update_segmentation_note()
        self.log_event(f"person cut-out model: chosen {value}, "
                       f"in use {segment.active_model()}")
        self._status(f"Person cut-out: {self.segmentation_combo.currentText()}"
                     + ("" if segment.active_model() == value else
                        " (download it to use it)"))

    def _download_rvm(self) -> None:
        """Fetch the RVM model on a thread; the UI polls for progress."""
        from .. import models as model_catalog

        if self._model_download is not None:
            return
        state: dict = {"progress": (0, model_catalog.CATALOG["rvm"].size)}
        self._model_download = state

        def work() -> None:
            try:
                model_catalog.download(
                    "rvm", progress=lambda done, total: state.__setitem__(
                        "progress", (done, total)))
                state["ok"] = True
            except Exception as exc:  # noqa: BLE001 - shown to the user
                state["error"] = str(exc)
            state["finished"] = True

        threading.Thread(target=work, name="anycam-model-download", daemon=True).start()
        self._model_download_timer.start()
        self._update_segmentation_note()

    def _poll_model_download(self) -> None:
        from .. import segment

        state = self._model_download
        if state is None:
            self._model_download_timer.stop()
            return
        if not state.get("finished"):
            self._update_segmentation_note()
            return
        self._model_download_timer.stop()
        self._model_download = None
        if state.get("ok"):
            segment.reset()          # pick the new model up on the next refresh
            self._status("Robust Video Matting downloaded and in use")
            self.log_event("model rvm downloaded and verified")
        else:
            error = state.get("error", "unknown error")
            self._status(f"Model download failed: {error}")
            self.log_event(f"model rvm download failed: {error}")
        self._update_segmentation_note()

    def _effects_device_changed(self, _index: int = 0) -> None:
        from .. import inference

        value = str(self.effects_device_combo.currentData() or "cpu")
        self.project.output.effects_device = value
        self._mark_dirty()
        inference.set_preference(value)
        self._update_effects_device_note()
        self.log_event(f"AI effects device: {inference.describe()}")
        self._status(f"AI effects run on: {inference.describe()}")
