"""Undo and redo of layout edits."""
from __future__ import annotations




from ..model import (Layer)
from .theme import STYLE  # noqa: F401
from .layout_history import LayoutHistory  # noqa: F401
from .preview import PreviewWidget, _PreviewOverlay  # noqa: F401
from .dialogs import CameraGridDialog, OverlayDialog, _slider_row  # noqa: F401


class UndoMixin:
    """Undo and redo of layout edits."""

    # ------------------------------------------------------------- undo/redo
    def _layout_snapshot(self) -> list[dict]:
        return LayoutHistory.snapshot(self.scene)

    def _record_history(self, before: list[dict] | None) -> None:
        if before is None:
            return
        if self.history.push(before, self._layout_snapshot()):
            self._sync_history_buttons()
            self._mark_dirty()

    def _sync_history_buttons(self) -> None:
        if hasattr(self, "undo_button"):
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)

    def _clear_history(self) -> None:
        self.history.clear()
        self._layout_before_edit = None
        self._opacity_before = None
        self._transform_timer.stop()
        self._transform_pending = ""
        self._sync_history_buttons()

    def undo(self) -> None:
        snapshot = self.history.undo(self._layout_snapshot())
        if snapshot is None:
            self._status("Nothing to undo")
            return
        self._restore_layout(snapshot)
        self._sync_history_buttons()
        self._status("Undo")

    def redo(self) -> None:
        snapshot = self.history.redo(self._layout_snapshot())
        if snapshot is None:
            self._status("Nothing to redo")
            return
        self._restore_layout(snapshot)
        self._sync_history_buttons()
        self._status("Redo")

    def _restore_layout(self, snapshot: list[dict]) -> None:
        current = {layer.id: layer for layer in self.scene.layers}
        restored = []
        for state in snapshot:
            fresh = Layer.from_dict(state)
            existing = current.get(fresh.id)
            if existing is None:
                # A removed layer has no live object to update: rebuild it
                # from the full snapshot (source, effects, styling, ...).
                restored.append(fresh)
            else:
                # Keep object identity and only restore the layout fields the
                # history command owns, so an unrecorded effects or visibility
                # change is not silently rolled back by an undo.
                existing.box = fresh.box
                existing.crop = fresh.crop
                existing.alpha = fresh.alpha
                existing.z = fresh.z
                existing.name = fresh.name      # renames are recorded too
                restored.append(existing)
        self.scene.layers = restored
        self._transform_timer.stop()
        self._transform_pending = ""
        self._layout_before_edit = None
        self._opacity_before = None
        self._refresh_layers()
        self._mark_dirty()
        if self.engine is not None:
            self.restart_engine()
