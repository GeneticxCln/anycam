"""Modern side-tab navigation container for the AnyCam Settings window."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QStackedWidget, QVBoxLayout, QWidget,
)


class SideTabWidget(QWidget):
    """Vertical side-tab settings shell with a ``QTabWidget``-compatible API.

    Left rail: grouped, checkable navigation buttons.
    Right pane: page title/subtitle banner above a scrollable content stack.
    """

    currentChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pages: list[QWidget] = []
        self._buttons: list[QPushButton] = []
        self._subtitles: list[str] = []
        self._sections: list[str] = []
        self._last_section = ""

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Left navigation sidebar
        sidebar = QFrame(self)
        sidebar.setObjectName("settingsSidebar")
        sidebar.setFixedWidth(196)
        side_outer = QVBoxLayout(sidebar)
        side_outer.setContentsMargins(10, 14, 10, 12)
        side_outer.setSpacing(6)

        brand = QLabel("Settings", sidebar)
        brand.setObjectName("settingsBrand")
        side_outer.addWidget(brand)

        nav_scroll = QScrollArea(sidebar)
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        nav_scroll.viewport().setStyleSheet("background: transparent;")
        nav_host = QWidget()
        nav_host.setStyleSheet("background: transparent;")
        self._nav_layout = QVBoxLayout(nav_host)
        self._nav_layout.setContentsMargins(0, 2, 0, 4)
        self._nav_layout.setSpacing(3)
        self._nav_layout.addStretch(1)
        nav_scroll.setWidget(nav_host)
        side_outer.addWidget(nav_scroll, 1)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idClicked.connect(self.setCurrentIndex)

        # Right content area
        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        header = QFrame(content)
        header.setObjectName("settingsHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 14, 20, 12)
        header_layout.setSpacing(2)
        self._header_title = QLabel("", header)
        self._header_title.setObjectName("settingsPageTitle")
        self._header_sub = QLabel("", header)
        self._header_sub.setObjectName("settingsPageSub")
        self._header_sub.setWordWrap(True)
        header_layout.addWidget(self._header_title)
        header_layout.addWidget(self._header_sub)
        content_layout.addWidget(header)

        self._stack = QStackedWidget(content)
        content_layout.addWidget(self._stack, 1)

        root.addWidget(sidebar)
        root.addWidget(content, 1)

    # ------------------------------------------------- QTabWidget-compatible API
    def addTab(
        self,
        widget: QWidget,
        label: str,
        *,
        section: str = "",
        subtitle: str = "",
    ) -> int:
        index = len(self._pages)
        insert_at = self._nav_layout.count() - 1  # keep trailing stretch
        if section and section != self._last_section:
            header = QLabel(section)
            header.setObjectName("settingsSection")
            self._nav_layout.insertWidget(insert_at, header)
            insert_at += 1
            self._last_section = section

        button = QPushButton(label)
        button.setObjectName("settingsNavBtn")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAccessibleName(f"{label} settings")
        self._group.addButton(button, index)
        self._nav_layout.insertWidget(insert_at, button)

        self._pages.append(widget)
        self._buttons.append(button)
        self._subtitles.append(subtitle)
        self._sections.append(section)
        self._stack.addWidget(widget)

        if index == 0:
            self.setCurrentIndex(0)
        return index

    def count(self) -> int:
        return len(self._pages)

    def tabText(self, index: int) -> str:
        if 0 <= index < len(self._buttons):
            return self._buttons[index].text()
        return ""

    def setTabText(self, index: int, text: str) -> None:
        if 0 <= index < len(self._buttons):
            self._buttons[index].setText(text)
            if index == self.currentIndex():
                self._header_title.setText(text)

    def currentIndex(self) -> int:
        return self._stack.currentIndex()

    def setCurrentIndex(self, index: int) -> None:
        if not (0 <= index < len(self._pages)):
            return
        changed = self._stack.currentIndex() != index
        self._stack.setCurrentIndex(index)
        self._buttons[index].setChecked(True)
        self._sync_header(index)
        if changed:
            self.currentChanged.emit(index)

    def currentWidget(self) -> QWidget | None:
        return self._stack.currentWidget()

    def widget(self, index: int) -> QWidget | None:
        if 0 <= index < len(self._pages):
            return self._pages[index]
        return None

    def indexOf(self, widget: QWidget) -> int:
        try:
            return self._pages.index(widget)
        except ValueError:
            return -1

    def section_for(self, index: int) -> str:
        if 0 <= index < len(self._sections):
            return self._sections[index]
        return ""

    def sync_header(self) -> None:
        """Refresh the page header banner (e.g. after a language switch)."""
        idx = self.currentIndex()
        if 0 <= idx < len(self._pages):
            self._sync_header(idx)

    def _sync_header(self, index: int) -> None:
        from .. import i18n

        self._header_title.setText(self._buttons[index].text())
        sub = i18n.translate(self._subtitles[index]) if self._subtitles[index] else ""
        self._header_sub.setText(sub)
        self._header_sub.setVisible(bool(sub))
