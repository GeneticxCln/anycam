"""Files AnyCam wrote that no longer read, set aside instead of deleted.

``check`` finds the project, ``hotkeys.json`` and ``phones.json`` that do not
parse as the JSON object they should be.  ``set_aside`` moves a file into
``set-aside/<date-time>/`` under the config directory; nothing here ever
deletes a user file.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class Problem:
    kind: str       # "project", "hotkeys" or "phones"
    path: Path
    why: str


def readable_object(path: Path) -> str:
    """"" when ``path`` holds a JSON object, else why not."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return f"cannot be read ({exc.strerror or exc})"
    except ValueError as exc:
        return f"is not valid JSON ({exc})"
    return "" if isinstance(data, dict) else "does not hold a JSON object"


def aside_folder(when: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when if when is not None else time.time()))
    return config.config_dir() / "set-aside" / stamp


def set_aside(path: Path, folder: Path | None = None) -> Path | None:
    """Move ``path`` into the set-aside folder; returns where it went, or ``None`` on failure."""
    folder = folder or aside_folder()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / path.name
        counter = 1
        while target.exists():
            counter += 1
            target = folder / f"{path.name}.{counter}"
        shutil.move(str(path), str(target))
    except OSError:
        return None
    return target


def check() -> list[Problem]:
    found = []
    candidates = [("project", config.current_project_path()),
                  ("hotkeys", config.config_dir() / "hotkeys.json"),
                  ("phones", config.state_dir() / "phones.json")]
    for kind, path in candidates:
        if path.exists():
            why = readable_object(path)
            if why:
                found.append(Problem(kind, path, why))
    return found


def repair(problems: list[Problem]) -> list[tuple[Problem, Path | None]]:
    """Set every problem file aside; a project with a readable ``.bak`` gets it back."""
    folder = aside_folder()
    done = []
    for problem in problems:
        moved = set_aside(problem.path, folder)
        if moved is not None and problem.kind == "project":
            backup = problem.path.with_name(problem.path.name + ".bak")
            if backup.exists() and not readable_object(backup):
                try:
                    shutil.copy2(backup, problem.path)
                except OSError:
                    pass
        done.append((problem, moved))
    return done
