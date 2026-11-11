"""Keyboard and screen-reader pass: every control gets a name and is reachable by Tab.

Qt exposes a widget to assistive technology through its accessible name.  Most
of AnyCam's controls sit next to a label or carry a tooltip but have no name of
their own (sliders, spin boxes, combo boxes, icon-only buttons), so a screen
reader would read them as "slider".  :func:`apply` fills the missing names from
the best available source and makes sure the control can take keyboard focus.
"""
from __future__ import annotations


def _clean(text: str) -> str:
    return " ".join(text.replace("&", "").split())


def _previous_label(widget) -> str:
    """The text of the label placed just before ``widget`` in its parent (a form row)."""
    from PySide6.QtWidgets import QLabel

    parent = widget.parentWidget()
    if parent is None:
        return ""
    siblings = [child for child in parent.children() if hasattr(child, "isWidgetType")
                and child.isWidgetType()]
    try:
        index = siblings.index(widget)
    except ValueError:
        return ""
    for sibling in reversed(siblings[max(0, index - 3):index]):
        if isinstance(sibling, QLabel) and sibling.text().strip() and not sibling.pixmap():
            return _clean(sibling.text())
    return ""


_GENERIC = (("QComboBox", "Choice"), ("QAbstractSpinBox", "Number"), ("QAbstractSlider", "Slider"),
            ("QListWidget", "List"), ("QAbstractButton", "Button"))


def _name_for(widget, labels_by_buddy: dict) -> str:
    from PySide6.QtWidgets import QComboBox, QScrollBar

    if isinstance(widget, QScrollBar):
        return "Vertical scroll bar" if widget.orientation().value == 2 else "Horizontal scroll bar"
    label = labels_by_buddy.get(widget)
    if label:
        return label
    for getter in ("text", "toolTip", "placeholderText", "title"):
        function = getattr(widget, getter, None)
        if function is not None:
            try:
                text = _clean(str(function()))
            except Exception:  # noqa: BLE001 - some widgets compute these lazily
                text = ""
            if text:
                return text
    nearby = _previous_label(widget)
    if nearby:
        return nearby
    if isinstance(widget, QComboBox) and widget.currentText():
        return _clean(widget.currentText())
    named = _clean(widget.objectName().replace("_", " "))
    if named:
        return named
    from PySide6 import QtWidgets

    for class_name, generic in _GENERIC:
        if isinstance(widget, getattr(QtWidgets, class_name)):
            return generic
    return ""


def apply(root) -> int:
    """Name and focus-enable the interactive widgets below ``root``; returns the count fixed."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QComboBox,
                                   QLabel, QLineEdit, QListWidget, QPlainTextEdit, QTextEdit,
                                   QWidget)

    interactive = (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QComboBox, QLineEdit,
                   QListWidget, QPlainTextEdit, QTextEdit)
    labels_by_buddy = {}
    for label in root.findChildren(QLabel):
        buddy = label.buddy()
        if buddy is not None and label.text():
            labels_by_buddy[buddy] = _clean(label.text())
    fixed = 0
    for widget in root.findChildren(QWidget):
        if not isinstance(widget, interactive):
            continue
        # The line edit inside a spin box or an editable combo is not a control of its own.
        parent = widget.parentWidget()
        if isinstance(widget, QLineEdit) and isinstance(parent, (QAbstractSpinBox, QComboBox)):
            continue
        changed = False
        if not widget.accessibleName():
            name = _name_for(widget, labels_by_buddy)
            if name:
                widget.setAccessibleName(name)
                changed = True
        if widget.isEnabled() and widget.focusPolicy() == Qt.FocusPolicy.NoFocus:
            widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            changed = True
        fixed += 1 if changed else 0
    return fixed


def unnamed(root) -> list[str]:
    """Interactive widgets that still have no accessible name (for tests and audits)."""
    from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QComboBox,
                                   QLineEdit, QListWidget, QWidget)

    out = []
    for widget in root.findChildren(QWidget):
        if isinstance(widget, (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QComboBox,
                               QListWidget)) and not widget.accessibleName():
            out.append(f"{type(widget).__name__}:{widget.objectName() or '?'}")
        elif isinstance(widget, QLineEdit) and not widget.accessibleName() \
                and not isinstance(widget.parentWidget(), (QAbstractSpinBox, QComboBox)):
            out.append(f"QLineEdit:{widget.objectName() or '?'}")
    return out
