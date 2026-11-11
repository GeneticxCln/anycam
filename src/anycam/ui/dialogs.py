"""Small dialogs and the slider row helper."""
from __future__ import annotations



from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QGridLayout, QLabel, QLineEdit,
    QMessageBox,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from .. import overlay
from ..model import (Layer)


def _slider_row(label: str, low: int, high: int, value: int, callback,
                display=None) -> tuple[QWidget, QSlider, QLabel]:
    holder = QWidget()
    row = QGridLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    title = QLabel(label)
    format_value = display or str
    value_label = QLabel(format_value(value))
    value_label.setObjectName("accent")
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(low, high)
    slider.setValue(value)
    row.addWidget(title, 0, 0)
    row.addWidget(value_label, 0, 1, alignment=Qt.AlignmentFlag.AlignRight)
    row.addWidget(slider, 1, 0, 1, 2)
    slider.valueChanged.connect(
        lambda v: (value_label.setText(format_value(v)), callback(v)))
    return holder, slider, value_label


class OverlayDialog(QDialog):
    """Edit a ticker's text, or a lower third's name and title, and its colours."""

    def __init__(self, layer: Layer, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit {layer.name}")
        self._layer = layer
        self._colour = layer.color or "#ffffff"
        self._background = layer.background or "#101418cc"
        form = QFormLayout(self)
        if layer.kind == "lower_third":
            name, _sep, title = str(layer.source or "").partition("|")
            self.first = QLineEdit(name)
            self.second = QLineEdit(title)
            form.addRow("Name", self.first)
            form.addRow("Title", self.second)
        else:
            self.first = QLineEdit(layer.source)
            self.second = None
            form.addRow("Text", self.first)
        self.colour_button = QPushButton(self._colour)
        self.colour_button.clicked.connect(self._pick_colour)
        self.background_button = QPushButton(self._background)
        self.background_button.clicked.connect(self._pick_background)
        form.addRow("Text colour", self.colour_button)
        form.addRow("Background", self.background_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._paint()

    def _paint(self) -> None:
        self.colour_button.setText(self._colour)
        self.background_button.setText(self._background)

    def _pick_colour(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._colour), self, "Text colour")
        if chosen.isValid():
            self._colour = chosen.name()
            self._paint()

    def _pick_background(self) -> None:
        chosen = QColorDialog.getColor(
            QColor(overlay.parse_color(self._background, (16, 20, 24, 204))[0],
                   *overlay.parse_color(self._background, (16, 20, 24, 204))[1:]),
            self, "Background", QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if chosen.isValid():
            self._background = (f"#{chosen.red():02x}{chosen.green():02x}"
                                f"{chosen.blue():02x}{chosen.alpha():02x}")
            self._paint()

    def values(self) -> tuple[str, str, str]:
        """``(source, text colour, background)`` as stored on the layer."""
        if self.second is not None:
            source = f"{self.first.text().strip()}|{self.second.text().strip()}"
        else:
            source = self.first.text().strip()
        return source, self._colour, self._background


class CameraGridDialog(QDialog):
    """Assign detected cameras to the four cells of a 2x2 scene."""

    SLOT_LABELS = ("Top left", "Top right", "Bottom left", "Bottom right")

    def __init__(self, choices: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("New camera grid")
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Assign a camera to each 2\u00d72 cell. Empty cells stay blank; "
            "at least three cameras must be selected."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.combos: list[QComboBox] = []
        for index, label in enumerate(self.SLOT_LABELS):
            combo = QComboBox()
            combo.addItem("Empty", "")
            for name, device in choices:
                combo.addItem(name, device)
            if index < len(choices):
                combo.setCurrentIndex(index + 1)
            self.combos.append(combo)
            form.addRow(label, combo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def slots(self) -> list[str]:
        return [str(combo.currentData() or "") for combo in self.combos]

    def _accept_if_valid(self) -> None:
        devices = [device for device in self.slots() if device]
        if len(devices) < 3:
            QMessageBox.warning(self, "Camera grid",
                                "Select at least three cameras for the grid.")
            return
        if len(set(devices)) != len(devices):
            QMessageBox.warning(self, "Camera grid",
                                "Each grid cell must use a different camera.")
            return
        self.accept()
