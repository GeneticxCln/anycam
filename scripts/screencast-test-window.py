#!/usr/bin/env python3
"""Show a controlled window for the live ScreenCast evidence capture.

This avoids putting a personal desktop or camera image in the repository.  Run
it while ``capture-screencast.py --window-only`` is waiting for the portal
chooser, then pick the window titled ``AnyCam ScreenCast Test``.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


class TestWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AnyCam ScreenCast Test")
        self.resize(960, 540)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0.0, QColor("#07151a"))
        gradient.setColorAt(0.55, QColor("#0f3b38"))
        gradient.setColorAt(1.0, QColor("#4a2b57"))
        painter.fillRect(self.rect(), gradient)

        cell = 48
        painter.setPen(QPen(QColor("#5eead0"), 1))
        for x in range(0, self.width(), cell):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), cell):
            painter.drawLine(0, y, self.width(), y)

        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont("DejaVu Sans", 34, QFont.Weight.Bold))
        painter.drawText(QRectF(48, 64, self.width() - 96, 70),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         "AnyCam ScreenCast")
        painter.setPen(QColor("#5eead0"))
        painter.setFont(QFont("DejaVu Sans", 19))
        painter.drawText(QRectF(48, 140, self.width() - 96, 50),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         "xdg-desktop-portal + PipeWire live capture")
        painter.setPen(QColor("#d7e3ea"))
        painter.setFont(QFont("DejaVu Sans", 15))
        painter.drawText(QRectF(48, 210, self.width() - 96, 40),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         "This controlled window is the screen-capture evidence target.")

        painter.setPen(QPen(QColor("#5eead0"), 5))
        painter.drawRoundedRect(QRectF(32, 32, self.width() - 64, self.height() - 64),
                                18, 18)
        painter.end()


def main() -> int:
    app = QApplication(sys.argv[:1])
    window = TestWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
