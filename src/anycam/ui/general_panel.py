"""Settings > General: language, project files and keyboard shortcuts."""
from __future__ import annotations

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QComboBox, QGroupBox, QHBoxLayout, QHeaderView, QKeySequenceEdit,
                               QLabel, QPushButton, QTableWidget, QVBoxLayout, QWidget)

from .. import a11y, hotkeys, i18n


class GeneralPanelMixin:
    """Language, project files and the shortcut editor, and the final UI polish pass."""

    def _general_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        language_box = QGroupBox("Language")
        language_layout = QHBoxLayout(language_box)
        self.language_combo = QComboBox()
        for code, name in i18n.available_languages().items():
            self.language_combo.addItem(name, code)
        self.language_combo.setCurrentIndex(
            max(0, self.language_combo.findData(i18n.saved_language())))
        self.language_combo.currentIndexChanged.connect(
            lambda _i: self.set_language(str(self.language_combo.currentData())))
        language_layout.addWidget(self.language_combo, 1)
        layout.addWidget(language_box)

        files = QGroupBox("Project")
        files_layout = QHBoxLayout(files)
        for label, handler in (("Open…", lambda: self.open_project()),
                               ("Save as…", lambda: self.save_project_as()),
                               ("Import scene…", lambda: self.import_scene()),
                               ("Export scene…", lambda: self.export_scene())):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, h=handler: h())
            files_layout.addWidget(button)
        layout.addWidget(files)

        keys_box = QGroupBox("Keyboard shortcuts")
        keys_layout = QVBoxLayout(keys_box)
        self.hotkey_table = QTableWidget(len(hotkeys.ACTIONS), 2)
        self.hotkey_table.setHorizontalHeaderLabels(["Action", "Key"])
        self.hotkey_table.verticalHeader().setVisible(False)
        self.hotkey_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.hotkey_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._hotkey_edits: dict[str, QKeySequenceEdit] = {}
        current = hotkeys.current()
        for row, (action, label, _default) in enumerate(hotkeys.ACTIONS):
            item = QLabel(f"  {label}")
            self.hotkey_table.setCellWidget(row, 0, item)
            edit = QKeySequenceEdit(QKeySequence(current.get(action, "")))
            edit.setMaximumSequenceLength(1)
            edit.setAccessibleName(f"Shortcut for {label}")
            edit.editingFinished.connect(self._hotkeys_edited)
            self.hotkey_table.setCellWidget(row, 1, edit)
            self._hotkey_edits[action] = edit
        keys_layout.addWidget(self.hotkey_table, 1)
        self.hotkey_note = QLabel("")
        self.hotkey_note.setObjectName("muted")
        self.hotkey_note.setWordWrap(True)
        keys_layout.addWidget(self.hotkey_note)
        reset = QPushButton("Reset shortcuts to defaults")
        reset.clicked.connect(self.reset_hotkeys)
        keys_layout.addWidget(reset)
        layout.addWidget(keys_box, 1)
        return page

    # ------------------------------------------------------------- shortcuts
    def _hotkeys_edited(self) -> None:
        mapping = {action: edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                   for action, edit in self._hotkey_edits.items()}
        clashes = self.set_hotkeys(mapping)
        if clashes:
            names = "; ".join(
                f"{key}: " + ", ".join(hotkeys.LABELS[a] for a in actions)
                for key, actions in clashes.items())
            self.hotkey_note.setText(f"Used twice (neither works until one changes): {names}")
        else:
            self.hotkey_note.setText("Shortcuts saved.")

    def reset_hotkeys(self) -> None:
        for action, edit in self._hotkey_edits.items():
            edit.setKeySequence(QKeySequence(hotkeys.DEFAULTS[action]))
        self.set_hotkeys(dict(hotkeys.DEFAULTS))
        self.hotkey_note.setText("Shortcuts are back to their defaults.")

    # -------------------------------------------------------------- language
    def set_language(self, code: str) -> int:
        """Choose the interface language now and for next time; strings changed."""
        if code not in i18n.available_languages():
            code = i18n.DEFAULT
        i18n.save_language(code)
        return self.apply_interface_polish()

    def apply_interface_polish(self, root=None) -> int:
        """Translate (per the saved language) and name every control for assistive tech."""
        language = i18n.saved_language()
        catalog = i18n.load(language)
        targets = [root] if root is not None else [self] + [
            child for child in (getattr(self, "_settings", None),) if child is not None]
        changed = 0
        for target in targets:
            changed += i18n.apply(target, catalog)
            a11y.apply(target)
        return changed
