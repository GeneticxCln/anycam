"""Small dialogs and the slider row helper."""
from __future__ import annotations



from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMessageBox,
    QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from .. import animated, overlay
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
    """Edit a ticker's text, a lower third's name, title and style, a clock's
    format or a timer's countdown, and its colours."""

    def __init__(self, layer: Layer, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit {layer.name}")
        self._layer = layer
        self._colour = layer.color or "#ffffff"
        self._background = layer.background or "#101418cc"
        self.template: QComboBox | None = None
        form = QFormLayout(self)
        if layer.kind == "lower_third":
            name, title, template = animated.split_lower_third(layer.source)
            self.first = QLineEdit(name)
            self.second = QLineEdit(title)
            form.addRow("Name", self.first)
            form.addRow("Title", self.second)
            self.template = QComboBox()
            self.template.addItems(list(animated.LOWER_THIRD_TEMPLATES))
            self.template.setCurrentText(template)
            self.template.currentTextChanged.connect(self._template_changed)
            form.addRow("Style", self.template)
        elif layer.kind == "clock":
            self.first = QLineEdit(layer.source or "%H:%M:%S")
            self.second = None
            form.addRow("Format", self.first)
            hint = QLabel("%H hours · %M minutes · %S seconds · %I:%M %p for 12 hours · "
                          "%A %d %B for the date")
            hint.setWordWrap(True)
            hint.setObjectName("muted")
            form.addRow(hint)
        elif layer.kind == "timer":
            self.first = QLineEdit(layer.source or "countdown:300")
            self.second = None
            form.addRow("Timer", self.first)
            hint = QLabel("05:00 or countdown:300 counts down (red in the last ten seconds); "
                          "stopwatch counts up. Showing the layer starts it again.")
            hint.setWordWrap(True)
            hint.setObjectName("muted")
            form.addRow(hint)
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
        self._template_changed(self.template.currentText() if self.template else "")

    def _template_changed(self, template: str) -> None:
        # A named style brings its own colours; Classic uses the buttons below.
        custom = template in ("", "Classic")
        self.colour_button.setEnabled(custom)
        self.background_button.setEnabled(custom)

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
            template = self.template.currentText() if self.template else "Classic"
            source = animated.join_lower_third(
                self.first.text(), self.second.text(), template)
        else:
            source = self.first.text().strip()
        return source, self._colour, self._background


class CameraControlsDialog(QDialog):
    """Live sliders, switches and menus for a camera's V4L2 controls."""

    def __init__(self, layer: Layer, controls, apply, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Camera controls - {layer.name}")
        self.resize(420, 520)
        self._apply = apply            # (name, value) -> error text or ""
        self._widgets: dict = {}
        outer = QVBoxLayout(self)
        if not controls:
            note = QLabel("This camera exposes no adjustable controls "
                          "(or v4l2-ctl is not installed).")
            note.setWordWrap(True)
            outer.addWidget(note)
        form = QFormLayout()
        for control in controls:
            widget = self._widget_for(control, layer.camera_controls.get(control.name))
            widget.setEnabled(not control.inactive)
            self._widgets[control.name] = (control, widget)
            form.addRow(control.label, widget)
        container = QWidget()
        container.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)
        self.message = QLabel("")
        self.message.setObjectName("muted")
        outer.addWidget(self.message)
        row = QHBoxLayout()
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self.reset_defaults)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(reset)
        row.addStretch(1)
        row.addWidget(close)
        outer.addLayout(row)

    def _widget_for(self, control, saved):
        value = control.clamp(saved) if saved is not None else control.value
        if control.kind == "bool":
            box = QCheckBox()
            box.setChecked(bool(value))
            box.toggled.connect(lambda on, c=control: self._set(c, 1 if on else 0))
            return box
        if control.kind in ("menu", "intmenu") and control.menu:
            combo = QComboBox()
            for index, label in control.menu.items():
                combo.addItem(label, index)
            combo.setCurrentIndex(max(0, combo.findData(value)))
            combo.currentIndexChanged.connect(
                lambda _i, c=control, w=combo: self._set(c, int(w.currentData())))
            return combo
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(control.minimum, control.maximum)
        slider.setSingleStep(control.step)
        slider.setValue(value)
        slider.valueChanged.connect(lambda v, c=control: self._set(c, v))
        return slider

    def _set(self, control, value: int) -> None:
        value = control.clamp(int(value))
        problem = self._apply(control.name, value)
        self.message.setText(f"{control.label}: {problem}" if problem else "")

    def reset_defaults(self) -> None:
        for control, widget in self._widgets.values():
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(control.default))
            elif isinstance(widget, QComboBox):
                widget.setCurrentIndex(max(0, widget.findData(control.default)))
            else:
                widget.setValue(control.default)
            widget.blockSignals(False)
            self._set(control, control.default)


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



class FirstRunDialog(QDialog):
    """Shown once: what is ready on this machine and how to fix what is not."""

    def __init__(self, results, parent=None) -> None:
        from .. import firstrun

        super().__init__(parent)
        self.setWindowTitle("Welcome to AnyCam")
        self.results = results
        layout = QVBoxLayout(self)
        ok = firstrun.ready(results)
        heading = QLabel("Everything AnyCam needs is here." if ok else
                         "A few things need attention before AnyCam can run everything.")
        heading.setWordWrap(True)
        heading.setObjectName("title")
        layout.addWidget(heading)
        self.rows = QListWidget()
        for check in results:
            mark = "\u2713" if check.ok else ("\u2717" if check.required else "\u2022")
            text = f"{mark}  {check.title} \u2014 {check.detail}"
            if check.fix:
                text += f"\n      {check.fix}"
            self.rows.addItem(text)
        layout.addWidget(self.rows, 1)
        buttons = QHBoxLayout()
        copy = QPushButton("Copy report")
        copy.clicked.connect(self.copy_report)
        done = QPushButton("Start")
        done.setObjectName("primary")
        done.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(done)
        layout.addLayout(buttons)
        self.resize(640, 420)

    def copy_report(self) -> None:
        from PySide6.QtGui import QGuiApplication

        from .. import firstrun

        QGuiApplication.clipboard().setText(firstrun.summary_text(self.results))
