"""Scene transitions: fade, slide and wipe between two scenes on one canvas.

The old scene's layers stay on the compositor for the length of the
transition ("ghosts") while the new scene's layers are added next to them; the
compositor pads of both groups are then animated (opacity or position) and the
ghosts are removed at the end.  Nothing downstream - preview, recorder, stream,
virtual camera - sees a rebuild or a timestamp reset.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

TRANSITIONS = ("cut", "fade",
               "slide-left", "slide-right", "slide-up", "slide-down",
               "wipe-left", "wipe-right", "wipe-up", "wipe-down")
LABELS = {
    "cut": "Cut", "fade": "Fade",
    "slide-left": "Slide left", "slide-right": "Slide right",
    "slide-up": "Slide up", "slide-down": "Slide down",
    "wipe-left": "Wipe in from right", "wipe-right": "Wipe in from left",
    "wipe-up": "Wipe in from bottom", "wipe-down": "Wipe in from top",
}
MIN_SECONDS, MAX_SECONDS = 0.1, 5.0
FRAME_SECONDS = 1.0 / 60.0


def transition_kind(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in TRANSITIONS else "cut"


def transition_seconds(milliseconds) -> float:
    try:
        seconds = float(milliseconds) / 1000.0
    except (TypeError, ValueError):
        return 0.5
    if seconds != seconds:
        return 0.5
    return max(MIN_SECONDS, min(MAX_SECONDS, seconds))


def ease(progress: float) -> float:
    """Smoothstep: starts and ends gently."""
    progress = max(0.0, min(1.0, progress))
    return progress * progress * (3.0 - 2.0 * progress)


@dataclass(frozen=True)
class Step:
    """Where the two groups of layers are at one moment of a transition."""

    out_dx: int = 0
    out_dy: int = 0
    in_dx: int = 0
    in_dy: int = 0
    out_alpha: float = 1.0       # multiplies each outgoing layer's own opacity
    in_alpha: float = 1.0


def step_at(kind: str, progress: float, canvas_w: int, canvas_h: int) -> Step:
    """The state ``progress`` (0..1, already eased) of the way through ``kind``."""
    kind = transition_kind(kind)
    p = max(0.0, min(1.0, progress))
    if kind == "cut":
        return Step(out_alpha=0.0 if p >= 1.0 else 1.0, in_alpha=1.0)
    if kind == "fade":
        return Step(out_alpha=1.0 - p, in_alpha=p)
    direction, _, side = kind.partition("-")
    unit_x = {"left": -1, "right": 1}.get(side, 0)
    unit_y = {"up": -1, "down": 1}.get(side, 0)
    # "slide-left": everything moves left; the new scene arrives from the right.
    # "wipe-left": the new scene arrives from the right over a still old scene.
    in_dx = int(round(-unit_x * canvas_w * (1.0 - p)))
    in_dy = int(round(-unit_y * canvas_h * (1.0 - p)))
    if direction == "slide":
        return Step(int(round(unit_x * canvas_w * p)), int(round(unit_y * canvas_h * p)),
                    in_dx, in_dy)
    return Step(0, 0, in_dx, in_dy)


@dataclass
class Running:
    kind: str
    seconds: float
    ghosts: list
    incoming: list
    started: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class TransitionMixin:
    """Runs a transition on the live compositor (see module docstring)."""

    _transition: Running | None = None

    @property
    def transitioning(self) -> bool:
        return self._transition is not None

    def _begin_transition(self, kind: str, seconds: float, ghosts: list, incoming: list) -> None:
        run = Running(kind, seconds, ghosts, incoming)
        self._transition = run
        self._apply_transition_step(run, 0.0)
        run.thread = threading.Thread(target=self._transition_loop, args=(run,),
                                      name="anycam-transition", daemon=True)
        run.thread.start()

    def _transition_loop(self, run: Running) -> None:
        while not run.stop.is_set():
            progress = (time.monotonic() - run.started) / run.seconds
            try:
                self._apply_transition_step(run, ease(progress))
            except Exception:  # noqa: BLE001 - a pad vanishing must not kill the loop
                pass
            if progress >= 1.0:
                break
            run.stop.wait(FRAME_SECONDS)
        run.done.set()

    def _apply_transition_step(self, run: Running, progress: float) -> None:
        step = step_at(run.kind, progress, self.canvas_w, self.canvas_h)
        for layers, dx, dy, scale in ((run.ghosts, step.out_dx, step.out_dy, step.out_alpha),
                                      (run.incoming, step.in_dx, step.in_dy, step.in_alpha)):
            for layer in layers:
                pad = self._layer_pads.get(layer.id)
                if pad is None or not layer.visible:
                    continue
                rect = self._box_pixels(layer)
                pad.set_property("xpos", int(rect[0] + dx))
                pad.set_property("ypos", int(rect[1] + dy))
                pad.set_property("alpha", float(max(0.0, min(1.0, layer.alpha * scale))))

    def finish_transition(self) -> None:
        """Drop the old scene's layers and put the new ones exactly in place."""
        run, self._transition = self._transition, None
        if run is None:
            return
        run.stop.set()
        if run.thread is not None:
            run.thread.join(timeout=1.0)
        for layer in run.incoming:
            pad = self._layer_pads.get(layer.id)
            if pad is None:
                continue
            rect = self._box_pixels(layer)
            pad.set_property("xpos", int(rect[0]))
            pad.set_property("ypos", int(rect[1]))
            pad.set_property("alpha", float(max(0.0, min(1.0, layer.alpha))) if layer.visible else 0.0)
        for layer in run.ghosts:
            visible = layer.visible
            self.detach_layer(layer.id)
            layer.visible = visible       # the old scene returns exactly as it was
        self._ghosts = []
        self.refresh_layer_order()
        self._sync_standby_label()

    def poll_transition(self) -> bool:
        """Finish a transition whose animation has run out (call from the UI thread)."""
        run = self._transition
        if run is not None and run.done.is_set():
            self.finish_transition()
        return self._transition is not None
