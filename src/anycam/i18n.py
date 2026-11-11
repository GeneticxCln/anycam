"""Interface translation without touching every string in the code.

Qt's own ``tr()`` would need every label wrapped.  Instead the window is built
in English and :func:`apply` walks its widgets once, replacing each known string
with the catalogue's translation.  The English original is kept on the widget,
so switching language back and forth always starts from the source text.
Strings that do not appear in a catalogue (status messages built at run time,
for example) stay English.

Catalogues are ``assets/i18n/<code>.json``: ``{"_name": "Deutsch", "text": "Text"}``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

DEFAULT = "en"
_SOURCE = "_anycam_source"


def catalog_dir() -> Path:
    return config.assets_dir() / "i18n"


def available_languages() -> dict[str, str]:
    """``{code: native name}``; English is always offered."""
    found = {DEFAULT: "English"}
    for path in sorted(catalog_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found[path.stem] = str(data.get("_name") or path.stem)
    return found


def load(language: str) -> dict[str, str]:
    """The translations for ``language`` (empty for English or an unknown code)."""
    if language in ("", DEFAULT):
        return {}
    path = catalog_dir() / f"{language}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: value for key, value in data.items()
            if isinstance(key, str) and isinstance(value, str) and not key.startswith("_")}


def _setting_path() -> Path:
    return config.config_dir() / "language"


def saved_language() -> str:
    """The language the user chose, else the environment's, else English."""
    forced = os.environ.get("ANYCAM_LANG", "").strip().lower()
    if forced:
        return forced if forced in available_languages() else DEFAULT
    try:
        chosen = _setting_path().read_text(encoding="utf-8").strip().lower()
    except OSError:
        chosen = ""
    if chosen in available_languages():
        return chosen
    system = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
              or os.environ.get("LANG") or "").lower()
    code = system.replace("-", "_").split("_")[0]
    return code if code in available_languages() else DEFAULT


def save_language(language: str) -> None:
    """Remember the choice (owner-only file next to the other settings)."""
    if language not in available_languages():
        language = DEFAULT
    try:
        _setting_path().write_text(language + "\n", encoding="utf-8")
    except OSError:
        pass


def _plain(text: str) -> str:
    """The text without a mnemonic ampersand, for catalogue lookup."""
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def _lookup(catalog: dict[str, str], source: str) -> str:
    if not source or not catalog:
        return source
    if source in catalog:
        return catalog[source]
    plain = _plain(source)
    if plain in catalog:
        # keep the mnemonic position out of it: a translated mnemonic would
        # need its own letter, so the ampersand is dropped
        return catalog[plain]
    return source


def _remember(obj, attribute: str, current: str) -> str:
    """The English original of ``attribute`` of ``obj`` (stored on first sight)."""
    store = obj.property(_SOURCE) or {}
    if not isinstance(store, dict):
        store = {}
    if attribute not in store:
        store[attribute] = current
        obj.setProperty(_SOURCE, store)
    return store[attribute]


def apply(root, catalog: dict[str, str]) -> int:
    """Translate ``root`` and everything below it; the number of strings changed."""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (QAbstractButton, QGroupBox, QLabel, QLineEdit, QMenu,
                                   QTabWidget, QTextEdit, QWidget)

    changed = 0

    def set_text(obj, attribute: str, getter, setter) -> None:
        nonlocal changed
        current = getter()
        if not current:
            return
        source = _remember(obj, attribute, current)
        wanted = _lookup(catalog, source)
        if wanted != current:
            setter(wanted)
            changed += 1

    widgets = [root] + root.findChildren(QWidget)
    for widget in widgets:
        if isinstance(widget, (QAbstractButton, QLabel)):
            if isinstance(widget, QLabel) and widget.pixmap() is not None and not widget.text():
                pass
            else:
                set_text(widget, "text", widget.text, widget.setText)
        if isinstance(widget, QGroupBox):
            set_text(widget, "title", widget.title, widget.setTitle)
        if isinstance(widget, (QLineEdit, QTextEdit)) and hasattr(widget, "placeholderText"):
            set_text(widget, "placeholder", widget.placeholderText, widget.setPlaceholderText)
        if isinstance(widget, QTabWidget):
            bar = widget.tabBar()
            for index in range(widget.count()):
                key = f"tab{index}"
                current = widget.tabText(index)
                source = _remember(bar, key, current)
                wanted = _lookup(catalog, source)
                if wanted != current:
                    widget.setTabText(index, wanted)
                    changed += 1
        if isinstance(widget, QMenu):
            set_text(widget, "title", widget.title, widget.setTitle)
        set_text(widget, "tooltip", widget.toolTip, widget.setToolTip)
        set_text(widget, "windowtitle", widget.windowTitle, widget.setWindowTitle)
    for action in root.findChildren(QAction):
        set_text(action, "text", action.text, action.setText)
        set_text(action, "tooltip", action.toolTip, action.setToolTip)
    return changed


def collect(root) -> set[str]:
    """Every translatable English string in ``root`` (used to build a catalogue)."""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (QAbstractButton, QGroupBox, QLabel, QMenu, QTabWidget, QWidget)

    found: set[str] = set()
    for widget in [root] + root.findChildren(QWidget):
        store = widget.property(_SOURCE) or {}
        texts = []
        if isinstance(widget, (QAbstractButton, QLabel)):
            texts.append(store.get("text", widget.text()))
        if isinstance(widget, (QGroupBox, QMenu)):
            texts.append(store.get("title", widget.title()))
        if isinstance(widget, QTabWidget):
            bar = widget.tabBar()
            bar_store = bar.property(_SOURCE) or {}
            texts += [bar_store.get(f"tab{i}", widget.tabText(i)) for i in range(widget.count())]
        texts.append(store.get("tooltip", widget.toolTip()))
        found.update(_plain(t) for t in texts if t and t.strip())
    for action in root.findChildren(QAction):
        store = action.property(_SOURCE) or {}
        for text in (store.get("text", action.text()), store.get("tooltip", action.toolTip())):
            if text and text.strip():
                found.add(_plain(text))
    return found
