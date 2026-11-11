"""Undo history of layout edits."""
from __future__ import annotations




from ..model import (Scene)


class LayoutHistory:
    """Snapshot-based undo/redo for a scene's layers.

    A snapshot stores every layer's serialised state, in scene order, so one
    undo step can cover a move, a stacking/opacity change, or a structural
    edit such as adding, removing or duplicating a layer.  Playlist and scene
    changes still clear the stacks because they also change the active graph.
    """

    def __init__(self, limit: int = 100):
        self.limit = limit
        self._undo: list[list[dict]] = []
        self._redo: list[list[dict]] = []

    @staticmethod
    def snapshot(scene: Scene) -> list[dict]:
        return [layer.to_dict() for layer in scene.layers]

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def push(self, before: list[dict], after: list[dict]) -> bool:
        if before == after:
            return False
        self._undo.append(before)
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        return True

    def undo(self, current: list[dict]) -> list[dict] | None:
        if not self._undo:
            return None
        self._redo.append(current)
        return self._undo.pop()

    def redo(self, current: list[dict]) -> list[dict] | None:
        if not self._redo:
            return None
        self._undo.append(current)
        return self._redo.pop()

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
