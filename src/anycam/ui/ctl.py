"""One table of actions for everything that drives the window from outside.

Keyboard shortcuts, the phone remote and ``anycam ctl`` all run the same
actions through ``ctl_dispatch``, so they cannot drift apart.
"""
from __future__ import annotations


class CtlMixin:
    """Actions by name: the hotkey actions, plus ``scene <name>``, ``state`` and ``list``."""

    def ctl_state(self) -> dict:
        return {"scenes": [{"id": scene.id, "name": scene.name} for scene in self.project.scenes],
                "active": self.project.active_scene, "recording": bool(self.recording),
                "muted": bool(self.project.audio.mic_muted),
                "preview": self.engine is not None,
                "vcam": bool(getattr(self, "vcam_on", False))}

    def ctl_scene_index(self, argument: str, ids_only: bool = False) -> int:
        """A scene by id, then (unless ``ids_only``) by name, then by 1-based number."""
        scenes = self.project.scenes
        text = (argument or "").strip()
        for index, scene in enumerate(scenes):
            if scene.id == text:
                return index
        if ids_only:
            return -1
        for index, scene in enumerate(scenes):
            if scene.name.strip().lower() == text.lower():
                return index
        if text.isdigit() and 1 <= int(text) <= len(scenes):
            return int(text) - 1
        return -1

    def ctl_action_names(self) -> list[str]:
        return sorted(set(self._hotkey_actions()) | {"scene", "state", "list"})

    def ctl_dispatch(self, request: dict) -> dict:
        """Run one request on the UI thread; never raises."""
        if not isinstance(request, dict):
            return {"ok": False, "error": "a request is a JSON object"}
        action = str(request.get("action", "")).strip()
        argument = str(request.get("argument", "") or "")
        try:
            if action == "list":
                return {"ok": True, "actions": self.ctl_action_names()}
            if action == "state":
                return {"ok": True, "state": self.ctl_state()}
            if action == "scene":
                index = self.ctl_scene_index(argument)
                if index < 0:
                    return {"ok": False, "error": f"no scene \"{argument}\""}
                self.switch_scene(index)
            else:
                handler = self._hotkey_actions().get(action)
                if handler is None:
                    return {"ok": False, "error": f"unknown action \"{action}\"; try: anycam ctl list"}
                handler()
        except Exception as exc:  # noqa: BLE001 - a bad request must not reach the window
            self.log_event(f"ctl {action}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{action} failed: {type(exc).__name__}"}
        self.log_event(f"ctl: {action}" + (f" {argument}" if argument else ""))
        return {"ok": True, "state": self.ctl_state()}
