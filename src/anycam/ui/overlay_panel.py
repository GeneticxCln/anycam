"""A small always-on-top panel: a round look at the canvas and the buttons you need mid-call.

Wayland compositors decide where windows go and may ignore "stay on top", so the
panel asks for it but cannot insist; on X11 it is placed and kept on top.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

CIRCLE = 150


class OverlayPanel(QWidget):
    """Round canvas preview plus Record / Mute / previous and next scene."""

    def __init__(self, window) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self._window = window
        self._drag: QPoint | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("AnyCam panel")
        self.picture = QLabel()
        self.picture.setFixedSize(CIRCLE, CIRCLE)
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setAccessibleName("Canvas preview")
        self.record_button = QPushButton("Record")
        self.mute_button = QPushButton("Mute")
        self.previous_button = QPushButton("◀")
        self.next_button = QPushButton("▶")
        self.scene_label = QLabel("")
        self.scene_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.record_button.clicked.connect(lambda: window.toggle_record())
        self.mute_button.clicked.connect(lambda: window.toggle_mic_mute())
        self.previous_button.clicked.connect(lambda: window._step_scene(-1))
        self.next_button.clicked.connect(lambda: window._step_scene(1))
        for button, name in ((self.record_button, "Record"), (self.mute_button, "Mute microphone"),
                             (self.previous_button, "Previous scene"), (self.next_button, "Next scene")):
            button.setAccessibleName(name)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.picture, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.scene_label)
        row = QHBoxLayout()
        row.addWidget(self.previous_button)
        row.addWidget(self.next_button)
        layout.addLayout(row)
        layout.addWidget(self.record_button)
        layout.addWidget(self.mute_button)
        self.setStyleSheet("QWidget { background: #11161c; color: #e6edf2; }"
                           "QPushButton { background: #1f2a33; border: 1px solid #2c3a45; "
                           "border-radius: 6px; padding: 5px; }"
                           "QPushButton:hover { background: #2a3a46; }")
        self.refresh()

    # --------------------------------------------------------------- content
    def set_frame(self, image: QImage) -> None:
        """Show ``image`` cropped to a circle."""
        if image.isNull():
            return
        side = min(image.width(), image.height())
        square = image.copy((image.width() - side) // 2, (image.height() - side) // 2, side, side)
        scaled = QPixmap.fromImage(square).scaled(
            CIRCLE, CIRCLE, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        round_pixmap = QPixmap(CIRCLE, CIRCLE)
        round_pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(round_pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addEllipse(QRectF(0, 0, CIRCLE, CIRCLE))
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, scaled)
        painter.end()
        self.picture.setPixmap(round_pixmap)

    def refresh(self) -> None:
        window = self._window
        self.record_button.setText("Stop recording" if window.recording else "Record")
        self.mute_button.setText("Unmute" if window.project.audio.mic_muted else "Mute")
        self.scene_label.setText(window.scene.name)

    # ---------------------------------------------------------------- moving
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None
