"""Application personalities and dynamic Qt style sheets."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .. import config


@dataclass(frozen=True)
class Personality:
    """One complete visual personality for the AnyCam interface."""

    key: str
    name: str
    tagline: str
    description: str
    bg: str
    surface: str
    input_bg: str
    border: str
    button_bg: str
    button_border: str
    button_pressed: str
    accent: str
    accent_alt: str
    text: str
    muted: str
    primary_text: str
    danger_bg: str
    danger_border: str
    selected_bg: str
    hint_bg: str
    radius: int = 10
    is_light: bool = False


PERSONALITIES: tuple[Personality, ...] = (
    Personality(
        key="mint-studio",
        name="Mint Studio",
        tagline="Signature dark · mint",
        description="Calm, low-glare dark studio with crisp mint accents for long streaming sessions.",
        bg="#0b0d10",
        surface="#101418",
        input_bg="#0e1216",
        border="#1e2830",
        button_bg="#172026",
        button_border="#26343d",
        button_pressed="#1d2b31",
        accent="#5eead0",
        accent_alt="#2dd4bf",
        text="#d7e3ea",
        muted="#7d8b96",
        primary_text="#07120f",
        danger_bg="#3a1d22",
        danger_border="#7a2f3a",
        selected_bg="#173a37",
        hint_bg="#12211f",
        radius=10,
    ),
    Personality(
        key="midnight-neon",
        name="Midnight Neon",
        tagline="Electric violet · synth",
        description="High-energy midnight canvas with neon violet highlights and deep indigo panels.",
        bg="#0a0814",
        surface="#131024",
        input_bg="#0f0c1d",
        border="#29214a",
        button_bg="#1b1633",
        button_border="#362c61",
        button_pressed="#241d42",
        accent="#c084fc",
        accent_alt="#38bdf8",
        text="#ede9fe",
        muted="#958ab8",
        primary_text="#130720",
        danger_bg="#3f172b",
        danger_border="#8a2a54",
        selected_bg="#2b1e4d",
        hint_bg="#1d1536",
        radius=12,
    ),
    Personality(
        key="sunset-ember",
        name="Sunset Ember",
        tagline="Warm amber · cozy",
        description="Cozy, warm-lit creator desk with glowing tangerine and ember tones.",
        bg="#120d0b",
        surface="#1c1411",
        input_bg="#17100e",
        border="#36251f",
        button_bg="#261b16",
        button_border="#4a332a",
        button_pressed="#30221c",
        accent="#fb923c",
        accent_alt="#f43f5e",
        text="#f5e8e1",
        muted="#a68c80",
        primary_text="#1c0d04",
        danger_bg="#42191c",
        danger_border="#8f2e34",
        selected_bg="#3d2417",
        hint_bg="#291912",
        radius=11,
    ),
    Personality(
        key="rose-velvet",
        name="Rose Velvet",
        tagline="Soft plum · rose",
        description="Plush dark velvet with gentle rounded shapes and luminous rose-quartz accents.",
        bg="#120c11",
        surface="#1b1219",
        input_bg="#160e14",
        border="#352230",
        button_bg="#261823",
        button_border="#492e42",
        button_pressed="#301e2b",
        accent="#f472b6",
        accent_alt="#fb7185",
        text="#f7e8f0",
        muted="#a88799",
        primary_text="#1f0713",
        danger_bg="#421722",
        danger_border="#8f2a44",
        selected_bg="#3d1d31",
        hint_bg="#291522",
        radius=14,
    ),
    Personality(
        key="forest-moss",
        name="Forest Moss",
        tagline="Deep pine · botanical",
        description="Grounded, eye-friendly woodland palette with fresh emerald highlights.",
        bg="#0a100d",
        surface="#111a15",
        input_bg="#0d1511",
        border="#203329",
        button_bg="#17241d",
        button_border="#2c4538",
        button_pressed="#1e3026",
        accent="#4ade80",
        accent_alt="#a3e635",
        text="#e1efe6",
        muted="#7f9b8a",
        primary_text="#06170c",
        danger_bg="#3a1c20",
        danger_border="#7a2f38",
        selected_bg="#183825",
        hint_bg="#12261a",
        radius=10,
    ),
    Personality(
        key="ocean-cobalt",
        name="Ocean Cobalt",
        tagline="Broadcast navy · sky",
        description="Clean television control-room look in deep cobalt and bright sky blue.",
        bg="#090e17",
        surface="#0f1726",
        input_bg="#0c1320",
        border="#1e2e4a",
        button_bg="#152136",
        button_border="#283e63",
        button_pressed="#1c2c47",
        accent="#38bdf8",
        accent_alt="#60a5fa",
        text="#e2ecf8",
        muted="#7c93b0",
        primary_text="#041521",
        danger_bg="#3a1b24",
        danger_border="#7c2d42",
        selected_bg="#16324d",
        hint_bg="#102236",
        radius=9,
    ),
    Personality(
        key="solar-gold",
        name="Solar Gold",
        tagline="Cinema obsidian · gold",
        description="Sharp cinema grading suite with matte obsidian panels and brushed gold accents.",
        bg="#0e0d0a",
        surface="#181611",
        input_bg="#13110d",
        border="#332d20",
        button_bg="#242017",
        button_border="#473e2b",
        button_pressed="#2e291d",
        accent="#facc15",
        accent_alt="#f59e0b",
        text="#f2eee3",
        muted="#9e957c",
        primary_text="#1a1402",
        danger_bg="#401b1b",
        danger_border="#8a3030",
        selected_bg="#382f15",
        hint_bg="#262010",
        radius=8,
    ),
    Personality(
        key="paper-daylight",
        name="Paper Daylight",
        tagline="Bright daylight · clean",
        description="Airy, high-legibility light mode with crisp paper cards and deep teal accents.",
        bg="#f2f5f8",
        surface="#ffffff",
        input_bg="#eaf0f5",
        border="#cfd9e3",
        button_bg="#e6edf3",
        button_border="#bfd0de",
        button_pressed="#d6e2ec",
        accent="#0f766e",
        accent_alt="#0284c7",
        text="#13202b",
        muted="#5b6d7c",
        primary_text="#ffffff",
        danger_bg="#fde8ea",
        danger_border="#d9707d",
        selected_bg="#d2f1ec",
        hint_bg="#e4f6f3",
        radius=10,
        is_light=True,
    ),
)

DEFAULT_PERSONALITY = "mint-studio"
PERSONALITY_BY_KEY: dict[str, Personality] = {p.key: p for p in PERSONALITIES}


def personality_file() -> Path:
    return config.config_dir() / "personality.json"


def saved_personality() -> str:
    env = os.environ.get("ANYCAM_PERSONALITY", "").strip().lower()
    if env in PERSONALITY_BY_KEY:
        return env
    path = personality_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        key = str(data.get("personality", "")).strip().lower() if isinstance(data, dict) else ""
        if key in PERSONALITY_BY_KEY:
            return key
    except (OSError, ValueError):
        pass
    return DEFAULT_PERSONALITY


def save_personality(key: str) -> Path:
    chosen = key if key in PERSONALITY_BY_KEY else DEFAULT_PERSONALITY
    path = personality_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps({"personality": chosen}, indent=2) + "\n").encode("utf-8")
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp, path)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def current_personality() -> Personality:
    return PERSONALITY_BY_KEY.get(saved_personality(), PERSONALITY_BY_KEY[DEFAULT_PERSONALITY])


def build_stylesheet(p: Personality) -> str:
    """Build the full Qt stylesheet for ``p``."""
    title_color = p.text if p.is_light else "#ffffff"
    r = p.radius
    r_small = max(5, r - 3)
    return f"""
QMainWindow, QDialog, QWidget {{ background: {p.bg}; color: {p.text}; font-family: "Noto Sans", "DejaVu Sans"; font-size: 13px; }}
QLabel, QCheckBox, QSlider {{ background: transparent; }}
QScrollArea {{ background: transparent; border: none; }}
QFrame#panel {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: {r}px; }}
QLabel#title {{ color: {title_color}; font-size: 20px; font-weight: 700; }}
QLabel#muted {{ color: {p.muted}; }}
QLabel#accent {{ color: {p.accent}; font-weight: 700; }}
QLabel#hint {{ background: {p.hint_bg}; border: 1px solid {p.accent}; border-radius: {r_small}px; padding: 7px 10px; color: {p.text}; }}
QGroupBox {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: {r}px; margin-top: 12px; padding: 14px 12px 12px 12px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 6px; color: {p.accent}; background: {p.bg}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1.5px solid {p.button_border}; background: {p.input_bg}; }}
QCheckBox::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.button_border}; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {p.accent}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; height: 0px; }}
QPushButton {{ background: {p.button_bg}; border: 1px solid {p.button_border}; border-radius: {r_small}px; padding: 7px 12px; color: {p.text}; }}
QPushButton:hover {{ border-color: {p.accent}; }}
QPushButton:pressed {{ background: {p.button_pressed}; }}
QPushButton#primary {{ background: {p.accent}; color: {p.primary_text}; font-weight: 700; border: none; }}
QPushButton#primary:hover {{ background: {p.accent_alt}; }}
QPushButton#preset {{ text-align: left; padding: 7px 10px; font-weight: 600; }}
QPushButton#preset:checked {{ border-color: {p.accent}; color: {p.accent}; background: {p.hint_bg}; }}
QPushButton#slotAdd {{ background: transparent; border: 2px solid transparent; border-radius: {r}px; color: {p.accent}; font-size: 30px; font-weight: 300; }}
QPushButton#slotAdd:hover {{ border: 2px dashed {p.accent}; background: {p.hint_bg}; }}
QPushButton#danger {{ background: {p.danger_bg}; border-color: {p.danger_border}; color: {p.text}; }}
QListWidget {{ background: {p.input_bg}; border: 1px solid {p.border}; border-radius: {r_small}px; }}
QListWidget::item {{ padding: 6px; }}
QListWidget::item:selected {{ background: {p.selected_bg}; color: {p.accent}; }}
QListWidget#effectGrid::item {{ padding: 0px; margin: 1px; border: 2px solid transparent; border-radius: 7px; color: {p.muted}; }}
QListWidget#effectGrid::item:selected {{ border: 2px solid {p.accent}; background: {p.selected_bg}; color: {p.accent}; }}
QListWidget#effectGrid::item:hover {{ background: {p.button_bg}; }}
QTabWidget::pane {{ border: 1px solid {p.border}; border-radius: {r_small + 1}px; }}
QTabBar::tab {{ background: {p.surface}; padding: 7px 12px; border: 1px solid {p.border}; color: {p.text}; }}
QTabBar::tab:selected {{ color: {p.accent}; }}
QSlider::groove:horizontal {{ height: 4px; background: {p.border}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; margin: -5px 0; border-radius: 7px; background: {p.accent}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {p.input_bg}; border: 1px solid {p.button_border}; border-radius: {max(4, r_small - 1)}px; padding: 5px; color: {p.text}; }}
QTableWidget {{ background: {p.input_bg}; border: 1px solid {p.border}; border-radius: {r_small}px; gridline-color: {p.border}; }}
QHeaderView::section {{ background: {p.surface}; color: {p.muted}; border: none; border-bottom: 1px solid {p.border}; padding: 5px 8px; font-weight: 600; }}
QProgressBar {{ background: {p.input_bg}; border: 1px solid {p.border}; border-radius: 4px; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}
QFrame#settingsSidebar {{ background: {p.surface}; border: none; border-right: 1px solid {p.border}; }}
QLabel#settingsBrand {{ color: {title_color}; font-size: 16px; font-weight: 700; padding: 2px 6px; }}
QLabel#settingsSection {{ color: {p.muted}; font-size: 10px; font-weight: 700; letter-spacing: 1px; padding: 10px 8px 3px 8px; }}
QPushButton#settingsNavBtn {{ background: transparent; border: 1px solid transparent; border-left: 3px solid transparent; border-radius: {r_small}px; padding: 7px 10px; text-align: left; font-weight: 500; color: {p.text}; }}
QPushButton#settingsNavBtn:hover {{ background: {p.button_bg}; border-color: {p.border}; }}
QPushButton#settingsNavBtn:checked {{ background: {p.selected_bg}; border-color: {p.border}; border-left: 3px solid {p.accent}; color: {p.accent}; font-weight: 700; }}
QFrame#settingsHeader {{ background: {p.surface}; border: none; border-bottom: 1px solid {p.border}; }}
QLabel#settingsPageTitle {{ color: {title_color}; font-size: 18px; font-weight: 700; }}
QLabel#settingsPageSub {{ color: {p.muted}; font-size: 12px; }}
QFrame#personalityPreview {{ background: {p.surface}; border: 1px solid {p.accent}; border-radius: {r}px; }}
QLabel#personalityTitle {{ color: {title_color}; font-size: 16px; font-weight: 700; }}
"""


def overlay_stylesheet(p: Personality) -> str:
    """Compact stylesheet for the floating always-on-top panel."""
    return (
        f"QWidget {{ background: {p.surface}; color: {p.text}; }}"
        f"QPushButton {{ background: {p.button_bg}; border: 1px solid {p.button_border};"
        f" border-radius: 6px; padding: 4px 8px; color: {p.text}; }}"
        f"QPushButton:checked {{ background: {p.accent}; color: {p.primary_text}; font-weight: 700; }}"
    )


STYLE = build_stylesheet(PERSONALITY_BY_KEY[DEFAULT_PERSONALITY])
