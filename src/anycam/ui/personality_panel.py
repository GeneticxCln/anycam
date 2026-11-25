"""Settings > Personality: tactile design bubbles, each carrying one personality."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from . import theme


class PersonalityBubble(QAbstractButton):
    """One interactive design bubble representing a single visual personality."""

    def __init__(self, personality: theme.Personality, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.personality = personality
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setText(personality.name)
        self.setToolTip(f"{personality.name} — {personality.description}")
        self.setAccessibleName(f"{personality.name} personality ({personality.tagline})")
        self.setMinimumSize(116, 126)
        self.setMaximumWidth(156)
        self._hovered = False

    def sizeHint(self) -> QSize:
        return QSize(130, 132)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = self.personality
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        checked = self.isChecked()

        # Subtle tile frame when selected or hovered
        if checked or self._hovered:
            tile_bg = QColor(p.surface if checked else p.bg)
            tile_bg.setAlpha(210 if checked else 110)
            tile_border = QColor(p.accent if checked else p.border)
            painter.setBrush(QBrush(tile_bg))
            painter.setPen(QPen(tile_border, 1.8 if checked else 1.0))
            painter.drawRoundedRect(rect, 14, 14)

        # Bubble orb centered in the upper region
        diameter = 66.0
        cx = rect.center().x()
        cy = rect.top() + 44.0
        orb = QRectF(cx - diameter / 2.0, cy - diameter / 2.0, diameter, diameter)

        # Outer selection / hover halo
        if checked or self._hovered:
            halo = orb.adjusted(-4.5, -4.5, 4.5, 4.5)
            halo_color = QColor(p.accent)
            halo_color.setAlpha(230 if checked else 140)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(halo_color, 2.4 if checked else 1.5))
            painter.drawEllipse(halo)

        # Clipped layered design bubble showing bg + surface wave + dual accents
        clip = QPainterPath()
        clip.addEllipse(orb)
        painter.save()
        painter.setClipPath(clip)

        # Base background gradient
        bg_grad = QLinearGradient(orb.topLeft(), orb.bottomRight())
        bg_grad.setColorAt(0.0, QColor(p.surface))
        bg_grad.setColorAt(1.0, QColor(p.bg))
        painter.fillRect(orb, bg_grad)

        # Diagonal secondary accent wash
        accent_wash = QRadialGradient(
            QPointF(orb.right() - diameter * 0.24, orb.bottom() - diameter * 0.24),
            diameter * 0.72,
        )
        c_alt = QColor(p.accent_alt)
        c_alt.setAlpha(200)
        c_alt_edge = QColor(p.accent_alt)
        c_alt_edge.setAlpha(0)
        accent_wash.setColorAt(0.0, c_alt)
        accent_wash.setColorAt(1.0, c_alt_edge)
        painter.fillRect(orb, accent_wash)

        # Primary accent inner sphere
        inner_r = diameter * 0.27
        inner_center = QPointF(cx - diameter * 0.12, cy + diameter * 0.06)
        inner_grad = QRadialGradient(
            QPointF(inner_center.x() - 4, inner_center.y() - 4),
            inner_r * 1.4,
        )
        inner_grad.setColorAt(0.0, QColor(p.accent).lighter(125))
        inner_grad.setColorAt(1.0, QColor(p.accent))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(inner_grad))
        painter.drawEllipse(inner_center, inner_r, inner_r)

        # Small secondary orb dot
        dot_r = diameter * 0.13
        painter.setBrush(QBrush(QColor(p.accent_alt)))
        painter.drawEllipse(QPointF(cx + diameter * 0.2, cy - diameter * 0.14), dot_r, dot_r)

        # Subtle glossy bubble highlight arc
        sheen = QRadialGradient(
            QPointF(orb.left() + diameter * 0.30, orb.top() + diameter * 0.22),
            diameter * 0.42,
        )
        sheen.setColorAt(0.0, QColor(255, 255, 255, 115 if not p.is_light else 160))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(orb, sheen)
        painter.restore()

        # Crisp orb rim
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(p.border), 1.2))
        painter.drawEllipse(orb)

        # Selected badge checkmark at bottom-right of orb
        if checked:
            badge_r = 9.5
            bx = orb.right() - 8.0
            by = orb.bottom() - 8.0
            painter.setBrush(QBrush(QColor(p.accent)))
            painter.setPen(QPen(QColor(p.bg), 1.6))
            painter.drawEllipse(QPointF(bx, by), badge_r, badge_r)
            check_pen = QPen(QColor(p.primary_text), 2.0, Qt.PenStyle.SolidLine,
                             Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(check_pen)
            painter.drawLine(QPointF(bx - 3.6, by + 0.2), QPointF(bx - 1.0, by + 2.8))
            painter.drawLine(QPointF(bx - 1.0, by + 2.8), QPointF(bx + 3.8, by - 2.6))

        # Personality name and tagline below orb
        name_rect = QRectF(rect.left() + 4, orb.bottom() + 8, rect.width() - 8, 18)
        name_font = QFont(self.font())
        name_font.setPointSize(9)
        name_font.setBold(checked)
        painter.setFont(name_font)
        palette_text = self.palette().color(self.foregroundRole())
        painter.setPen(QColor(p.accent) if checked else palette_text)
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         p.name)

        tag_rect = QRectF(rect.left() + 4, name_rect.bottom() + 1, rect.width() - 8, 15)
        tag_font = QFont(self.font())
        tag_font.setPointSize(8)
        painter.setFont(tag_font)
        tag_color = QColor(palette_text)
        tag_color.setAlpha(155)
        painter.setPen(tag_color)
        painter.drawText(tag_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         p.tagline)


class PersonalityPanelMixin:
    """Personality tab with design bubbles and live theme switching."""

    def _personality_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)

        intro = QLabel(
            "Each design bubble gives AnyCam its own visual personality — colors, surface depth, "
            "accent glow and corner feel. Click any bubble to switch live.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        bubbles_box = QGroupBox("Design bubbles")
        grid = QGridLayout(bubbles_box)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(12)

        self._personality_group = QButtonGroup(bubbles_box)
        self._personality_group.setExclusive(True)
        self._personality_bubbles: dict[str, PersonalityBubble] = {}
        active = getattr(self, "_personality", None) or theme.current_personality()

        columns = 4
        for idx, personality in enumerate(theme.PERSONALITIES):
            bubble = PersonalityBubble(personality, bubbles_box)
            bubble.setChecked(personality.key == active.key)
            bubble.clicked.connect(
                lambda _checked=False, key=personality.key: self.set_personality(key))
            self._personality_group.addButton(bubble, idx)
            self._personality_bubbles[personality.key] = bubble
            grid.addWidget(bubble, idx // columns, idx % columns,
                           Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(bubbles_box)

        # Live preview card for the currently active personality
        preview = QFrame()
        preview.setObjectName("personalityPreview")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(14, 12, 14, 12)
        preview_layout.setSpacing(8)

        top_row = QHBoxLayout()
        self.personality_title = QLabel(active.name)
        self.personality_title.setObjectName("personalityTitle")
        self.personality_tagline = QLabel(active.tagline)
        self.personality_tagline.setObjectName("accent")
        top_row.addWidget(self.personality_title)
        top_row.addStretch(1)
        top_row.addWidget(self.personality_tagline)
        preview_layout.addLayout(top_row)

        self.personality_desc = QLabel(active.description)
        self.personality_desc.setObjectName("muted")
        self.personality_desc.setWordWrap(True)
        preview_layout.addWidget(self.personality_desc)

        # Live palette swatches + tactile sample controls
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(8)
        self._personality_swatches: list[QLabel] = []
        for role in ("Background", "Surface", "Accent", "Highlight", "Border"):
            dot = QLabel()
            dot.setFixedSize(22, 22)
            dot.setToolTip(role)
            dot.setAccessibleName(f"{role} swatch")
            self._personality_swatches.append(dot)
            swatch_row.addWidget(dot)
        swatch_row.addStretch(1)

        sample_slider = QSlider(Qt.Orientation.Horizontal)
        sample_slider.setRange(0, 100)
        sample_slider.setValue(68)
        sample_slider.setFixedWidth(110)
        sample_slider.setAccessibleName("Personality preview slider")
        swatch_row.addWidget(sample_slider)

        self.personality_reset_button = QPushButton("Reset to Mint Studio")
        self.personality_reset_button.clicked.connect(
            lambda: self.set_personality(theme.DEFAULT_PERSONALITY))
        swatch_row.addWidget(self.personality_reset_button)
        preview_layout.addLayout(swatch_row)

        layout.addWidget(preview)
        layout.addStretch(1)
        self._sync_personality_preview(active)
        return page

    def _sync_personality_preview(self, personality: theme.Personality) -> None:
        bubbles = getattr(self, "_personality_bubbles", None)
        if bubbles:
            for key, bubble in bubbles.items():
                bubble.blockSignals(True)
                bubble.setChecked(key == personality.key)
                bubble.blockSignals(False)
                bubble.update()
        title = getattr(self, "personality_title", None)
        if title is not None:
            title.setText(personality.name)
        tagline = getattr(self, "personality_tagline", None)
        if tagline is not None:
            tagline.setText(personality.tagline)
        desc = getattr(self, "personality_desc", None)
        if desc is not None:
            desc.setText(personality.description)
        swatches = getattr(self, "_personality_swatches", None)
        if swatches:
            colors = (personality.bg, personality.surface, personality.accent,
                      personality.accent_alt, personality.border)
            for dot, color in zip(swatches, colors):
                dot.setStyleSheet(
                    f"background: {color}; border: 1px solid {personality.border}; "
                    f"border-radius: 11px;")
        reset_btn = getattr(self, "personality_reset_button", None)
        if reset_btn is not None:
            reset_btn.setEnabled(personality.key != theme.DEFAULT_PERSONALITY)

    def apply_personality(self, personality: theme.Personality | None = None) -> theme.Personality:
        """Apply ``personality`` (or the saved one) to the window and child surfaces."""
        chosen = personality or theme.current_personality()
        self._personality = chosen
        css = theme.build_stylesheet(chosen)
        self.setStyleSheet(css)
        settings = getattr(self, "_settings", None)
        if settings is not None:
            settings.setStyleSheet(css)
        overlay = getattr(self, "_overlay_panel", None)
        if overlay is not None:
            overlay.setStyleSheet(theme.overlay_stylesheet(chosen))
        about_title = getattr(self, "_about_title", None)
        if about_title is not None:
            about_title.setStyleSheet(
                f"font-size: 30px; font-weight: 700; color: {chosen.accent};")
        about_link = getattr(self, "_about_link", None)
        if about_link is not None:
            palette = about_link.palette()
            palette.setColor(palette.ColorRole.Link, QColor(chosen.accent))
            about_link.setPalette(palette)
        self._sync_personality_preview(chosen)
        return chosen

    def set_personality(self, key: str) -> theme.Personality:
        """Select a personality by key, persist it, and apply it immediately."""
        chosen = theme.PERSONALITY_BY_KEY.get(
            key, theme.PERSONALITY_BY_KEY[theme.DEFAULT_PERSONALITY])
        if not getattr(self, "headless", False) or getattr(self, "_persist", True):
            try:
                theme.save_personality(chosen.key)
            except OSError:
                pass
        self.apply_personality(chosen)
        if hasattr(self, "_status"):
            self._status(f"Personality: {chosen.name}")
        return chosen
