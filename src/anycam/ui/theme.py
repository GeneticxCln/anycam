"""The application style sheet."""

STYLE = """
QMainWindow, QWidget { background: #0b0d10; color: #d7e3ea; font-family: "Noto Sans", "DejaVu Sans"; font-size: 13px; }
QFrame#panel { background: #101418; border: 1px solid #1e2830; border-radius: 10px; }
QLabel#title { color: #ffffff; font-size: 20px; font-weight: 700; }
QLabel#muted { color: #7d8b96; }
QLabel#accent { color: #5eead0; font-weight: 700; }
QLabel#hint { background: #12211f; border: 1px solid #5eead0; border-radius: 7px; padding: 7px 10px; color: #d7e3ea; }
QPushButton { background: #172026; border: 1px solid #26343d; border-radius: 7px; padding: 7px 12px; }
QPushButton:hover { border-color: #5eead0; }
QPushButton:pressed { background: #1d2b31; }
QPushButton#primary { background: #5eead0; color: #07120f; font-weight: 700; border: none; }
QPushButton#preset { text-align: left; padding: 7px 10px; font-weight: 600; }
QPushButton#preset:checked { border-color: #5eead0; color: #5eead0; background: #12211f; }
QPushButton#slotAdd { background: transparent; border: 2px solid transparent; border-radius: 10px; color: #5eead0; font-size: 30px; font-weight: 300; }
QPushButton#slotAdd:hover { border: 2px dashed #5eead0; background: rgba(18, 33, 31, 180); }
QPushButton#danger { background: #3a1d22; border-color: #7a2f3a; }
QListWidget { background: #0e1216; border: 1px solid #1e2830; border-radius: 7px; }
QListWidget::item { padding: 6px; }
QListWidget::item:selected { background: #173a37; color: #5eead0; }
QTabWidget::pane { border: 1px solid #1e2830; border-radius: 8px; }
QTabBar::tab { background: #101418; padding: 7px 12px; border: 1px solid #1e2830; }
QTabBar::tab:selected { color: #5eead0; }
QSlider::groove:horizontal { height: 4px; background: #1e2830; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; margin: -5px 0; border-radius: 7px; background: #5eead0; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #0e1216; border: 1px solid #26343d; border-radius: 6px; padding: 5px; }
"""
