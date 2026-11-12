"""Effect packs: third-party picture effects dropped into a folder.

A pack is a directory in ``<data>/effects/`` with a ``pack.json``::

    {"name": "film-grain", "version": "1.0", "author": "A. Person", "license": "MIT",
     "effects": [{"key": "grain", "label": "Film grain", "description": "...",
                  "file": "grain.py", "default": 0.4}]}

and the Python file defines ``class Effect`` with ``process(image, now) -> image``
(a Pillow RGBA picture), and optionally ``__init__(amount)``, ``set_amount`` and
``close``.  Pack code never runs in the main process: the effect worker
(:mod:`anycam.effect_worker`) imports it, so a crash or a hang there only costs
the effect.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config

MAX_FILE_BYTES = 1_000_000
_SAFE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
CATEGORY = "Packs"


@dataclass(frozen=True)
class PackEffect:
    pack: str
    key: str
    label: str
    description: str
    path: Path
    default: float
    license: str
    author: str

    @property
    def processor(self) -> str:
        return f"pack:{self.pack}/{self.key}"

    @property
    def filter_key(self) -> str:
        return f"pack-{self.pack}-{self.key}"


def packs_dir() -> Path:
    path = config.data_dir() / "effects"
    path.mkdir(parents=True, exist_ok=True)
    return path


def discover(root: Path | None = None) -> tuple[list[PackEffect], list[str]]:
    """(effects, problems) from every valid pack under ``root``."""
    root = Path(root) if root is not None else packs_dir()
    effects: list[PackEffect] = []
    problems: list[str] = []
    for manifest in sorted(root.glob("*/pack.json")):
        folder = manifest.parent
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{folder.name}: pack.json unreadable ({exc})")
            continue
        found, issues = _read_manifest(folder, data)
        effects.extend(found)
        problems.extend(f"{folder.name}: {issue}" for issue in issues)
    return effects, problems


def _read_manifest(folder: Path, data) -> tuple[list[PackEffect], list[str]]:
    if not isinstance(data, dict):
        return [], ["pack.json is not an object"]
    name = str(data.get("name") or folder.name).strip().lower()
    if not _SAFE.match(name):
        return [], [f"the pack name {name!r} is not a plain lowercase name"]
    licence = str(data.get("license") or "").strip()
    author = str(data.get("author") or "").strip()
    out: list[PackEffect] = []
    problems: list[str] = []
    entries = data.get("effects")
    if not isinstance(entries, list) or not entries:
        return [], ["the pack lists no effects"]
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("an effect entry is not an object")
            continue
        key = str(entry.get("key") or "").strip().lower()
        if not _SAFE.match(key):
            problems.append(f"effect key {key!r} is not a plain lowercase name")
            continue
        relative = str(entry.get("file") or "")
        path = (folder / relative).resolve()
        if not relative or folder.resolve() not in path.parents or path.suffix != ".py":
            problems.append(f"{key}: the file must be a .py inside the pack")
            continue
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            problems.append(f"{key}: {relative} is missing or larger than 1 MB")
            continue
        try:
            default = max(0.0, min(1.0, float(entry.get("default", 0.5))))
        except (TypeError, ValueError):
            default = 0.5
        out.append(PackEffect(name, key, str(entry.get("label") or key)[:60],
                              str(entry.get("description") or "")[:200], path, default,
                              licence, author))
    return out, problems


def find(pack: str, key: str, root: Path | None = None) -> PackEffect | None:
    effects, _problems = discover(root)
    return next((effect for effect in effects if effect.pack == pack and effect.key == key), None)


class _Adapter:
    """Gives a pack's ``Effect`` the full Processor interface."""

    def __init__(self, effect, amount: float) -> None:
        self.effect = effect
        self.amount = amount

    def set_amount(self, amount: float) -> None:
        self.amount = max(0.0, min(1.0, float(amount)))
        setter = getattr(self.effect, "set_amount", None)
        if callable(setter):
            setter(self.amount)

    def process(self, image, now: float):
        return self.effect.process(image, now)

    def close(self) -> None:
        closer = getattr(self.effect, "close", None)
        if callable(closer):
            closer()


def load_effect(variant: str, amount: float, root: Path | None = None):
    """Import the pack effect ``"pack/key"`` and return it as a processor."""
    pack, _, key = variant.partition("/")
    found = find(pack, key, root)
    if found is None:
        raise KeyError(f"no effect pack entry {variant!r}")
    module_name = f"anycam_pack_{pack}_{key}".replace("-", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(module_name, found.path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {found.path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    cls = getattr(module, "Effect", None)
    if cls is None:
        raise ImportError(f"{found.path.name} defines no class Effect")
    try:
        effect = cls(amount)
    except TypeError:
        effect = cls()
    return _Adapter(effect, float(amount))
