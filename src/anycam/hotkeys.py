"""In-window keyboard shortcuts: the defaults and the user's own choices.

The map ``{action id: key sequence}`` is kept in ``hotkeys.json`` in the config
directory, apart from the project, because shortcuts belong to the person using
the app, not to a shareable scene file.  This module has no Qt in it; the
window builds its ``QShortcut`` objects from :func:`current`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

# (action id, label, default key).  Scene keys are generated below.
_BASE: tuple[tuple[str, str, str], ...] = (
    ("preview", "Start or stop the preview", "Ctrl+P"),
    ("record", "Start or stop recording", "Ctrl+R"),
    ("vcam", "Virtual camera on or off", "Ctrl+Shift+V"),
    ("snapshot", "Save a snapshot", "F5"),
    ("mute", "Mute or unmute the microphone", "Ctrl+M"),
    ("undo", "Undo", "Ctrl+Z"),
    ("redo", "Redo", "Ctrl+Shift+Z"),
    ("redo-alt", "Redo (second key)", "Ctrl+Y"),
    ("duplicate", "Duplicate the selected layer", "Ctrl+D"),
    ("lock", "Lock or unlock the selected layer", "Ctrl+L"),
    ("next-scene", "Next scene", "Ctrl+]"),
    ("previous-scene", "Previous scene", "Ctrl+["),
    ("align-left", "Align left", "Ctrl+Alt+Left"),
    ("align-right", "Align right", "Ctrl+Alt+Right"),
    ("align-top", "Align top", "Ctrl+Alt+Up"),
    ("align-bottom", "Align bottom", "Ctrl+Alt+Down"),
    ("align-hcenter", "Centre horizontally", "Ctrl+Alt+H"),
    ("align-vcenter", "Centre vertically", "Ctrl+Alt+V"),
)
ACTIONS: tuple[tuple[str, str, str], ...] = _BASE + tuple(
    (f"scene-{n}", f"Switch to scene {n}", f"Ctrl+{n}") for n in range(1, 10))
DEFAULTS: dict[str, str] = {action: key for action, _label, key in ACTIONS}
LABELS: dict[str, str] = {action: label for action, label, _key in ACTIONS}

_KEY = re.compile(r"^(?:(?:Ctrl|Shift|Alt|Meta)\+)*[^+\s]+$|^(?:(?:Ctrl|Shift|Alt|Meta)\+)+\+$",
                  re.IGNORECASE)


def path() -> Path:
    return config.config_dir() / "hotkeys.json"


def valid_key(text: str) -> bool:
    """Whether ``text`` looks like a Qt key sequence for one key press (or empty = unbound)."""
    text = str(text or "").strip()
    return text == "" or bool(_KEY.match(text))


def load_saved() -> dict[str, str]:
    """The user's overrides (only known actions with usable keys)."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {action: str(key).strip() for action, key in data.items()
            if action in DEFAULTS and isinstance(key, str) and valid_key(key)}


def current() -> dict[str, str]:
    """Every action with its key: the user's choice, else the default ("" = unbound)."""
    return {**DEFAULTS, **load_saved()}


def conflicts(mapping: dict[str, str]) -> dict[str, list[str]]:
    """``{key: [action ids]}`` for each key bound to more than one action."""
    seen: dict[str, list[str]] = {}
    for action, key in mapping.items():
        if key:
            seen.setdefault(key.lower(), []).append(action)
    return {key: actions for key, actions in seen.items() if len(actions) > 1}


def save(mapping: dict[str, str]) -> None:
    """Remember the choices that differ from the defaults (nothing else is written)."""
    overrides = {action: key for action, key in mapping.items()
                 if action in DEFAULTS and valid_key(key) and key != DEFAULTS[action]}
    target = path()
    if not overrides:
        target.unlink(missing_ok=True)
        return
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(overrides, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)


# ---------------------------------------------------------------- global keys
# The keys that work while another application has focus are owned by the
# desktop (xdg-desktop-portal GlobalShortcuts): AnyCam can only *suggest* a
# trigger when it binds them, and the user may change them in the desktop's
# own settings afterwards.  The suggestions can be overridden here.
def global_path() -> Path:
    return config.config_dir() / "global-hotkeys.json"


def global_triggers(defaults: dict[str, str]) -> dict[str, str]:
    """``defaults`` with the user's suggested triggers (portal syntax, e.g. ``CTRL+ALT+R``)."""
    try:
        data = json.loads(global_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(defaults)
    if not isinstance(data, dict):
        return dict(defaults)
    merged = dict(defaults)
    for identifier, trigger in data.items():
        if identifier in defaults and isinstance(trigger, str) and valid_key(trigger):
            merged[identifier] = trigger.strip().upper()
    return merged


def save_global(overrides: dict[str, str], defaults: dict[str, str]) -> None:
    """Remember suggested triggers that differ from ``defaults``."""
    changed = {identifier: trigger.strip().upper() for identifier, trigger in overrides.items()
               if identifier in defaults and valid_key(trigger)
               and trigger.strip().upper() != defaults[identifier]}
    target = global_path()
    if not changed:
        target.unlink(missing_ok=True)
        return
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(changed, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)
