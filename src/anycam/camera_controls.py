"""V4L2 camera controls (brightness, exposure, zoom, focus ...) through ``v4l2-ctl``.

The controls belong to the device, not to the canvas: changing one is instant
and does not touch the capture pipeline.  Parsing and setting are separate from
any window so they can be tested with canned ``v4l2-ctl`` output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config

_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9_]+)\s+0x[0-9a-fA-F]+\s+\((?P<kind>[a-z0-9]+)\)\s*:\s*(?P<rest>.*)$")
_FIELD = re.compile(r"(?P<key>min|max|step|default|value)=(?P<value>-?\d+)")
_MENU = re.compile(r"^\s+(?P<index>-?\d+):\s*(?P<label>.+?)\s*$")
_FLAGS = re.compile(r"flags=(?P<flags>[a-z\-, ]+)")


@dataclass
class Control:
    name: str
    kind: str                       # int, bool, menu, intmenu, button ...
    minimum: int = 0
    maximum: int = 1
    step: int = 1
    default: int = 0
    value: int = 0
    menu: dict[int, str] = field(default_factory=dict)
    inactive: bool = False          # e.g. exposure_absolute while auto exposure is on
    read_only: bool = False

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").capitalize()

    @property
    def adjustable(self) -> bool:
        return self.kind in ("int", "bool", "menu", "intmenu") and not self.read_only

    def clamp(self, value: int) -> int:
        """``value`` brought into range and onto a legal step."""
        if self.kind == "bool":
            return 1 if value else 0
        if self.kind in ("menu", "intmenu") and self.menu:
            return value if value in self.menu else self.default
        value = max(self.minimum, min(self.maximum, int(value)))
        if self.step > 1:
            value = self.minimum + round((value - self.minimum) / self.step) * self.step
            value = max(self.minimum, min(self.maximum, value))
        return value


def parse_controls(text: str) -> list[Control]:
    """Controls from ``v4l2-ctl --list-ctrls`` output, in the driver's order."""
    controls: list[Control] = []
    current: Control | None = None
    for line in text.splitlines():
        match = _LINE.match(line)
        if match:
            values = {m.group("key"): int(m.group("value"))
                      for m in _FIELD.finditer(match.group("rest"))}
            flags = _FLAGS.search(match.group("rest"))
            flag_text = flags.group("flags") if flags else ""
            current = Control(
                name=match.group("name"), kind=match.group("kind"),
                minimum=values.get("min", 0),
                maximum=values.get("max", 1 if match.group("kind") == "bool" else 0),
                step=max(1, values.get("step", 1)),
                default=values.get("default", 0), value=values.get("value", values.get("default", 0)),
                inactive="inactive" in flag_text,
                read_only="read-only" in flag_text)
            controls.append(current)
            continue
        menu = _MENU.match(line)
        if menu and current is not None and current.kind in ("menu", "intmenu"):
            current.menu[int(menu.group("index"))] = menu.group("label")
    return controls


def list_controls(device: str) -> list[Control]:
    """The adjustable controls of ``device`` (empty when v4l2-ctl cannot read it)."""
    code, out = config.run(["v4l2-ctl", "-d", device, "--list-ctrls-menus"], timeout=5.0)
    if code != 0:
        return []
    return [control for control in parse_controls(out) if control.adjustable]


def set_control(device: str, name: str, value: int) -> str:
    """Set one control; "" on success, else the reason."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        return f"{name!r} is not a control name"
    code, out = config.run(["v4l2-ctl", "-d", device, f"--set-ctrl={name}={int(value)}"], timeout=5.0)
    if code != 0:
        return (out or "v4l2-ctl failed").strip().splitlines()[-1]
    return ""


def apply_saved(device: str, saved: dict[str, int]) -> list[str]:
    """Re-apply remembered values (a replug resets some drivers); the failures."""
    problems = []
    for name, value in saved.items():
        problem = set_control(device, name, value)
        if problem:
            problems.append(f"{name}: {problem}")
    return problems
