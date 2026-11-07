"""The live filter library: distortions, retro TV looks and cleanup.

Each filter is one entry in ``FILTERS`` (category, label, GStreamer element,
the property a 0..1 *amount* controls).  Adding a filter is adding an entry; the
UI, the model and the tests all read this table.

A layer carries one **filter slot** in its source chain (``FilterSlot``).  The
slot is a small bin that starts as a pass-through and is swapped for the chosen
filter *while the pipeline runs*, so picking an effect never rebuilds the
canvas, drops a capture, or interrupts the virtual camera.  Colour, film look,
frame and chroma key are separate stages, so they stack with the filter.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

NO_FILTER = "none"

# A swap waits for a buffer to pass its blocked pad.  A source that produces no
# buffers (a static screen share, a stalled capture) would wait for ever, so
# after SWAP_IDLE_AFTER_S the pad is also swapped at an idle moment, and after
# SWAP_GIVE_UP_S the request is abandoned so later changes are not stuck.
SWAP_IDLE_AFTER_S = 2.0
SWAP_GIVE_UP_S = 20.0


@dataclass(frozen=True)
class FilterSpec:
    key: str
    label: str
    category: str
    element: str
    description: str = ""
    # Element wants this raw format (the TV effects only take BGRx, blur AYUV).
    convert: str = ""
    # The property the amount slider drives, and what 0 and 1 map to.
    prop: str = ""
    low: float = 0.0
    high: float = 1.0
    integer: bool = False
    default: float = 0.5
    fixed: tuple[tuple[str, object], ...] = ()
    # A Python per-frame processor (``processors.make_processor`` name) instead
    # of a GStreamer element.  It needs no element, so it is always available.
    processor: str = ""


def _distort(key, label, element, description, prop="", low=0.0, high=1.0,
             default=0.5, integer=False, **fixed):
    return FilterSpec(key, label, "Distort", element, description, "", prop,
                      low, high, integer, default,
                      tuple(sorted(fixed.items())))


def _tv(key, label, element, description, prop="", low=0.0, high=1.0,
        default=0.5, integer=False, **fixed):
    return FilterSpec(key, label, "Retro TV", element, description, "BGRx",
                      prop, low, high, integer, default,
                      tuple(sorted(fixed.items())))


_FILTER_LIST = (
    # -- Distort: geometric warps that work directly on RGBA ------------------
    _distort("pinch", "Pinch", "pinch", "Squeeze the middle inwards",
             "intensity", -1.0, 1.0, 0.8, radius=0.5),
    _distort("bulge", "Bulge", "bulge", "Blow the middle up like a lens",
             "zoom", 1.0, 5.0, 0.5, radius=0.5),
    _distort("sphere", "Sphere", "sphere", "Wrap the middle around a glass ball",
             "refraction", 1.0, 3.0, 0.5, radius=0.5),
    _distort("twirl", "Twirl", "twirl", "Spin the middle into a whirlpool",
             "angle", -6.28, 6.28, 0.75, radius=0.55),
    _distort("stretch", "Stretch", "stretch", "Pull the middle outwards",
             "intensity", 0.0, 1.0, 0.6, radius=0.5),
    _distort("tunnel", "Tunnel", "tunnel", "Bend the picture into a tunnel",
             "radius", 0.1, 0.7, 0.5),
    _distort("fisheye", "Fisheye", "fisheye", "Wide-angle lens"),
    _distort("ripple", "Water ripple", "waterripple",
             "Concentric waves from the centre", "amplitude", 0.0, 40.0, 0.4,
             radius=0.6, wavelength=26.0),
    _distort("marble", "Marble", "marble", "Veined, swirling glass",
             "amount", 0.0, 1.0, 0.5, **{"x-scale": 5.0, "y-scale": 5.0}),
    _distort("kaleidoscope", "Kaleidoscope", "kaleidoscope",
             "Mirror the picture into symmetric facets", "sides", 2, 12, 0.4,
             True, radius=0.55),
    _distort("diffuse", "Frosted glass", "diffuse", "Scatter the pixels",
             "scale", 1.0, 12.0, 0.4),
    _distort("tilt", "Tilt", "rotate", "Rotate the picture",
             "angle", -0.8, 0.8, 0.7, **{"off-edge-pixels": "clamp"}),
    _distort("mirror-left", "Mirror left", "mirror",
             "Reflect the left half", **{"mode": "left"}),
    _distort("mirror-right", "Mirror right", "mirror",
             "Reflect the right half", **{"mode": "right"}),
    _distort("mirror-top", "Mirror top", "mirror",
             "Reflect the top half", **{"mode": "top"}),
    _distort("mirror-bottom", "Mirror bottom", "mirror",
             "Reflect the bottom half", **{"mode": "bottom"}),
    # -- Retro TV: the effectv family, which needs 32-bit RGB -----------------
    _tv("edge", "Edge TV", "edgetv", "Glowing outlines"),
    _tv("aging", "Old film", "agingtv", "Scratches, dust and faded colour",
        "scratch-lines", 0, 20, 0.5, True),
    _tv("dice", "Dice", "dicetv", "Break the picture into rolling dice",
        "square-bits", 1, 5, 0.5, True),
    _tv("quark", "Quark", "quarktv", "Ghostly trails of earlier frames",
        "planes", 2, 40, 0.4, True),
    _tv("radioactive", "Radioactive", "radioactv",
        "Motion glows and fades", **{"mode": "normal", "color": "green"}),
    _tv("reverse", "Scanlines", "revtv", "Waveform scanlines",
        "gain", 10, 150, 0.4, True),
    _tv("raindrops", "Raindrops", "rippletv", "Rain on a window",
        **{"mode": "rain"}),
    _tv("psychedelic", "Psychedelic", "shagadelictv", "Pulsing colour rings"),
    _tv("streak", "Streak", "streaktv", "Motion smears into streaks"),
    _tv("vertigo", "Vertigo", "vertigotv", "A slow hypnotic zoom spiral",
        "speed", 0.02, 0.4, 0.3),
    _tv("warp", "Warp", "warptv", "Heat-haze warping"),
    _tv("optical", "Optical", "optv", "Op-art stripes that follow motion",
        **{"mode": "horizontal-stripes"}),
    # -- Touch-up (the rest are processors below) ----------------------------
    FilterSpec("blur", "Soft focus", "Touch-up", "gaussianblur",
               "Blur the whole picture (done on a quarter-size copy, so it is cheap)",
               "AYUV", "sigma", 0.0, 14.0, False, 0.35,
               (("downscale", 4),)),
)

def _proc(key, label, category, name, description, default=0.5):
    return FilterSpec(key, label, category, "", description, "", "amount",
                      0.0, 1.0, False, default, (), name)


_PROCESSOR_LIST = (
    # -- Portrait: keep the person, change what is behind them ----------------
    _proc("bg-blur", "Blur background", "Portrait", "background:blur",
          "Soft-focus everything behind you", 0.55),
    _proc("bg-dim", "Dim background", "Portrait", "background:dim",
          "Darken what is behind you", 0.6),
    _proc("bg-mono", "Black & white background", "Portrait", "background:mono",
          "Colour for you, grey behind", 0.5),
    _proc("spotlight", "Spotlight", "Portrait", "spotlight:", "A pool of light on you", 0.65),
    _proc("bg-studio", "Virtual background: Studio", "Portrait", "background:Studio",
          "Replace the room with a studio wall"),
    _proc("bg-aurora", "Virtual background: Aurora", "Portrait", "background:Aurora",
          "Replace the room with a night-sky gradient"),
    _proc("bg-sunset", "Virtual background: Sunset", "Portrait", "background:Sunset",
          "Replace the room with a sunset"),
    _proc("bg-ocean", "Virtual background: Ocean", "Portrait", "background:Ocean",
          "Replace the room with deep water"),
    _proc("bg-tunnel", "Virtual background: 3D tunnel", "Portrait", "background:tunnel",
          "You, flying through an animated tunnel"),
    _proc("bg-stars", "Virtual background: Starfield", "Portrait", "background:starfield",
          "You, in space"),
    _proc("bg-plasma", "Virtual background: Plasma", "Portrait", "background:plasma",
          "You, in front of animated plasma"),
    _proc("bg-waves", "Virtual background: Water", "Portrait", "background:ripples",
          "You, in front of rippling water"),
    # -- Face: props that follow your face -----------------------------------
    _proc("sunglasses", "Sunglasses", "Face", "sticker:sunglasses", "Cool shades"),
    _proc("party-hat", "Party hat", "Face", "sticker:party-hat", "Time to celebrate"),
    _proc("crown", "Crown", "Face", "sticker:crown", "Long may you reign"),
    _proc("mustache", "Mustache", "Face", "sticker:mustache", "A fine moustache"),
    _proc("clown-nose", "Clown nose", "Face", "sticker:clown-nose", "Honk"),
    _proc("cat", "Cat", "Face", "sticker:cat", "Ears, nose and whiskers"),
    _proc("halo", "Halo", "Face", "sticker:halo", "An angel"),
    _proc("heart-eyes", "Heart eyes", "Face", "sticker:heart-eyes", "Beating hearts for eyes"),
    _proc("star-eyes", "Star eyes", "Face", "sticker:star-eyes", "Starstruck"),
    _proc("fire-eyes", "Fire eyes", "Face", "sticker:fire-eyes", "Burning gaze"),
    _proc("sparkle-eyes", "Sparkle eyes", "Face", "sticker:sparkle-eyes", "Twinkling eyes"),
    _proc("money-eyes", "Money eyes", "Face", "sticker:money-eyes", "Cha-ching"),
    _proc("dizzy-eyes", "Dizzy eyes", "Face", "sticker:dizzy-eyes", "Seeing stars"),
    _proc("orbit-hearts", "Orbiting hearts", "Face", "sticker:orbit-hearts", "Hearts circle your head"),
    _proc("orbit-stars", "Orbiting stars", "Face", "sticker:orbit-stars", "A crown of stars, spinning"),
    _proc("orbit-butterflies", "Butterflies", "Face", "sticker:orbit-butterflies", "Butterflies flutter around you"),
    _proc("orbit-flowers", "Flower ring", "Face", "sticker:orbit-flowers", "Blossoms circle your head"),
    _proc("orbit-snow", "Snowflake ring", "Face", "sticker:orbit-snow", "Snowflakes swirl around you"),
    _proc("orbit-music", "Music notes", "Face", "sticker:orbit-music", "Notes circle your head"),
    _proc("orbit-party", "Party ring", "Face", "sticker:orbit-party", "Balloons and gifts orbit you"),
    _proc("orbit-bats", "Bat swarm", "Face", "sticker:orbit-bats", "Bats circle your head"),
    _proc("face-frame", "Face frame", "Face", "sticker:face-frame",
          "Corner brackets that follow your face"),
    _proc("face-pixelate", "Pixelate face", "Face", "privacy:pixelate",
          "Hide your face behind blocks"),
    _proc("face-blur", "Blur face", "Face", "privacy:blur", "Hide your face with a blur"),
    # -- Touch-up -------------------------------------------------------------
    _proc("skin-smooth", "Skin smoothing", "Touch-up", "touchup:smooth",
          "Soften skin on faces only; the rest stays sharp", 0.5),
    _proc("soft-glow", "Soft glow", "Touch-up", "touchup:glow",
          "A dreamy bloom around bright areas", 0.45),
    _proc("night-boost", "Low-light boost", "Touch-up", "touchup:night",
          "Lift shadows in a dim room without blowing out highlights", 0.5),
    # -- Framing -------------------------------------------------------------
    _proc("zoom", "Zoom", "Framing", "zoom:", "Digital zoom into the middle", 0.3),
    _proc("auto-frame", "Auto-frame", "Framing", "autoframe:",
          "Keep your face centred as you move"),
)

from . import masks as _masks  # noqa: E402

_MASK_LIST = tuple(
    _proc(f"mask-{key}", label, "Masks", f"mask:{key}",
          f"{label}: a full-face {group.lower()} mask that follows your head", 0.85)
    for key, (label, group, _paint, _anim) in _masks.MASKS.items())

FILTERS: dict[str, FilterSpec] = {
    spec.key: spec for spec in (*_FILTER_LIST, *_PROCESSOR_LIST, *_MASK_LIST)}
FILTER_CATEGORIES: tuple[str, ...] = (
    "Distort", "Retro TV", "Touch-up", "Portrait", "Face", "Masks", "Framing")


def filter_available(key: str) -> bool:
    spec = FILTERS.get(key)
    if spec is None:
        return False
    return bool(spec.processor) or Gst.ElementFactory.find(spec.element) is not None


def available_filters(category: str = "") -> list[FilterSpec]:
    """The filters that can run here, in display order."""
    return [spec for spec in (*_FILTER_LIST, *_PROCESSOR_LIST, *_MASK_LIST)
            if (not category or spec.category == category)
            and filter_available(spec.key)]


def normalize_filter(key: str) -> str:
    key = str(key or "").strip().lower()
    return key if key == NO_FILTER or filter_available(key) else NO_FILTER


def amount_value(spec: FilterSpec, amount: float) -> float | int:
    """Map the 0..1 amount onto the filter's own property range."""
    amount = max(0.0, min(1.0, float(amount)))
    value = spec.low + (spec.high - spec.low) * amount
    return int(round(value)) if spec.integer else value


def _make(factory: str, name: str | None = None) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer element '{factory}' is not installed")
    return element


def _caps(text: str) -> Gst.Element:
    element = _make("capsfilter")
    element.set_property("caps", Gst.Caps.from_string(text))
    return element


def _set(element: Gst.Element, prop: str, value) -> None:
    """Set a property by name; enums accept their nick as a string."""
    element.set_property(prop, value)


def build_slot(key: str, amount: float, name: str, size: tuple[int, int] | None = None,
               fps: int = 30):
    """``(bin, runner)`` applying filter ``key``; ``runner`` is None unless the
    filter is a Python processor, which owns a worker thread to stop later."""
    spec = FILTERS.get(key)
    if spec is not None and spec.processor and filter_available(key):
        from . import processors

        width, height = size or (320, 180)
        return processors.build_processor_bin(
            spec.processor, amount, name, width, height, fps)
    return _build_element_bin(key, amount, name, size), None


def build_slot_bin(key: str, amount: float, name: str,
                   size: tuple[int, int] | None = None, fps: int = 30) -> Gst.Bin:
    """Just the bin (a processor's worker thread is stopped straight away)."""
    bin_, runner = build_slot(key, amount, name, size, fps)
    if runner is not None:
        runner.stop()
    return bin_


def _build_element_bin(key: str, amount: float, name: str,
                       size: tuple[int, int] | None = None) -> Gst.Bin:
    """A bin that applies a GStreamer filter ``key`` (or passes video through)."""
    spec = FILTERS.get(key)
    bin_ = Gst.Bin.new(name)
    elements: list[Gst.Element] = []
    shrink = 1
    if spec is None or not filter_available(key):
        elements.append(_make("identity"))
    else:
        fixed = dict(spec.fixed)
        shrink = int(fixed.pop("downscale", 1))
        if not size:
            shrink = 1         # without a frame size there is nothing to scale to
        if shrink > 1:
            # Work on a smaller copy and scale back up: a full-resolution
            # Gaussian blur made the whole canvas lag.
            small = (max(2, size[0] // shrink) // 2 * 2, max(2, size[1] // shrink) // 2 * 2)
            elements += [_make("videoscale"),
                         _caps(f"video/x-raw,format=RGBA,width={small[0]},height={small[1]}")]
        if spec.convert:
            elements += [_make("videoconvert"),
                         _caps(f"video/x-raw,format={spec.convert}")]
        effect = _make(spec.element, "fx")
        for prop, value in fixed.items():
            _set(effect, prop, value)
        if spec.prop:
            value = amount_value(spec, amount)
            _set(effect, spec.prop, value / shrink if shrink > 1 else value)
        elements.append(effect)
        if spec.convert == "BGRx":
            # The effectv elements leave the padding byte at 0.  Converting
            # that straight to RGBA made the picture fully transparent (black
            # on the canvas) for several of them.  A hop through alpha-less RGB
            # makes the alpha of the converted frame opaque.
            elements += [_make("videoconvert"), _caps("video/x-raw,format=RGB")]
        if spec.convert:
            elements += [_make("videoconvert"), _caps("video/x-raw,format=RGBA")]
        if shrink > 1:
            elements += [_make("videoscale"),
                         _caps(f"video/x-raw,format=RGBA,width={size[0]},height={size[1]}")]
    for element in elements:
        bin_.add(element)
    for first, second in zip(elements, elements[1:], strict=False):
        if not first.link(second):
            raise RuntimeError(
                f"could not link {first.get_name()} -> {second.get_name()}")
    bin_.add_pad(Gst.GhostPad.new("sink", elements[0].get_static_pad("sink")))
    bin_.add_pad(Gst.GhostPad.new("src", elements[-1].get_static_pad("src")))
    return bin_


class SwapGate:
    """Lets only one slot of a chain swap at a time.

    A slot's block probe sits on its *neighbour's* output pad.  If both slots
    swapped at once, the first swap removed the bin the second probe sat on, so
    the second swap waited for a buffer on a dead pad and never finished.  The
    slot that loses the race keeps its request and resumes when the other is
    done.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder = None
        self._waiting: list = []

    def acquire(self, slot) -> bool:
        with self._lock:
            if self._holder is None or self._holder is slot:
                self._holder = slot
                return True
            if slot not in self._waiting:
                self._waiting.append(slot)
            return False

    def release(self, slot) -> None:
        with self._lock:
            if self._holder is not slot:
                return
            self._holder = None
            waiting, self._waiting = self._waiting, []
        for waiter in waiting:
            waiter._resume()


class FilterSlot:
    """The swappable filter position of one source chain."""

    _names = itertools.count(1)  # element names must be unique in a pipeline

    def __init__(self, key: str = NO_FILTER, amount: float = 0.5,
                 chain_elements: list | None = None,
                 size: tuple[int, int] | None = None, fps: int = 30,
                 gate: SwapGate | None = None) -> None:
        self.key = normalize_filter(key)
        self.amount = float(amount)
        self._gate = gate
        self._chain_elements = chain_elements
        self.size, self.fps = size, fps
        self.runner = None
        self._lock = threading.RLock()
        self._busy = False
        self._wanted: tuple[str, float] | None = None
        self.idle = threading.Event()
        self.idle.set()
        self.element, self.runner = self._new_bin(self.key, self.amount)

    def _new_bin(self, key: str, amount: float):
        return build_slot(
            key, amount, f"filter_slot_{next(FilterSlot._names)}_{key}",
            self.size, self.fps)

    def close(self) -> None:
        """Stop a processor's worker thread (the canvas is going away)."""
        if self.runner is not None:
            self.runner.stop()
            self.runner = None

    # ------------------------------------------------------------------ amount
    def set_amount(self, amount: float) -> None:
        self.amount = float(amount)
        spec = FILTERS.get(self.key)
        if self.runner is not None:
            self.runner.processor.set_amount(amount)
            return
        effect = self.element.get_by_name("fx")
        if spec is not None and spec.prop and effect is not None:
            value = amount_value(spec, amount)
            shrink = int(dict(spec.fixed).get("downscale", 1)) if self.size else 1
            _set(effect, spec.prop, value / shrink if shrink > 1 else value)

    # ------------------------------------------------------------------- apply
    def apply(self, key: str, amount: float) -> bool:
        """Show ``key`` at ``amount``; swaps the filter live when it changed.

        The swap happens on the streaming thread while the pad is idle, so this
        returns at once; wait on ``idle`` to know it finished.
        """
        key = normalize_filter(key)
        with self._lock:
            if self._busy:
                self._wanted = (key, float(amount))
                return True
            if key == self.key:
                self.set_amount(amount)
                return True
            if self._gate is not None and not self._gate.acquire(self):
                # A neighbouring slot is swapping: wait for it, then resume.
                self._busy = True
                self._wanted = (key, float(amount))
                self.idle.clear()
                return True
            state = self._start_swap(key, float(amount))
        if state != "scheduled" and self._gate is not None:
            self._gate.release(self)
        return state != "failed"

    def _resume(self) -> None:
        """Run the request that was waiting for a neighbour's swap."""
        with self._lock:
            wanted, self._wanted = self._wanted, None
            self._busy = False
        if wanted is not None:
            self.apply(*wanted)
        with self._lock:
            if not self._busy:
                self.idle.set()

    def _start_swap(self, key: str, amount: float) -> str:
        """Begin the swap; ``"scheduled"`` (it will finish by itself),
        ``"done"`` (finished at once) or ``"failed"`` (nothing changed)."""
        old, old_runner = self.element, self.runner
        parent = old.get_parent()
        try:
            new, new_runner = self._new_bin(key, amount)
        except Exception:  # noqa: BLE001 - keep what is showing
            return "failed"
        if parent is None:
            # Not part of a pipeline yet (still being built): just replace it.
            self._replace_in_list(old, new)
            self.element, self.runner = new, new_runner
            self.key, self.amount = key, amount
            if old_runner is not None:
                old_runner.stop()
            return "done"
        sink, src = old.get_static_pad("sink"), old.get_static_pad("src")
        if sink.get_peer() is None or src.get_peer() is None:
            if new_runner is not None:
                new_runner.stop()
            return "failed"
        self._busy = True
        self.idle.clear()

        def relink() -> None:
            ok = False
            try:
                # The neighbours *now*: they are re-read here rather than
                # captured earlier, so a topology change cannot leave this
                # relinking against pads that are no longer connected.
                upstream, downstream = sink.get_peer(), src.get_peer()
                # Stop the old bin first.  A processor's appsrc has its own task
                # and kept pushing into the unlinked pad, which posted a
                # "not-linked" error that tore the whole canvas down.
                old.set_state(Gst.State.NULL)
                upstream.unlink(sink)
                src.unlink(downstream)
                parent.remove(old)
                parent.add(new)
                new.sync_state_with_parent()
                ok = (upstream.link(new.get_static_pad("sink"))
                      == Gst.PadLinkReturn.OK
                      and new.get_static_pad("src").link(downstream)
                      == Gst.PadLinkReturn.OK)
            finally:
                if ok:
                    self._replace_in_list(old, new)
                    self.element, self.runner = new, new_runner
                    self.key, self.amount = key, amount
                    if old_runner is not None:
                        old_runner.stop()
                elif new_runner is not None:
                    new_runner.stop()
                self._finish()

        if parent.get_state(0)[1] != Gst.State.PLAYING:
            relink()
            return "scheduled"

        claim = threading.Lock()
        done = [False]
        probe = {"pad": sink.get_peer(), "id": 0}

        def once() -> bool:
            with claim:
                if done[0]:
                    return False
                done[0] = True
                return True

        def on_block(_pad, _info):
            if once():
                relink()
            return Gst.PadProbeReturn.REMOVE

        def arm(pad) -> None:
            probe["pad"] = pad
            probe["id"] = pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, on_block)

        started = time.monotonic()

        def watchdog() -> None:
            """Re-arm if the probed pad stopped being this slot's real upstream."""
            if done[0]:
                return
            current = sink.get_peer()
            if current is not None and current is not probe["pad"]:
                try:
                    probe["pad"].remove_probe(probe["id"])
                except Exception:  # noqa: BLE001 - the old pad may be gone
                    pass
                arm(current)
            age = time.monotonic() - started
            if age >= SWAP_GIVE_UP_S:
                if once():
                    try:
                        probe["pad"].remove_probe(probe["id"])
                    except Exception:  # noqa: BLE001
                        pass
                    if new_runner is not None:
                        new_runner.stop()
                    self._finish()      # unblock later requests; nothing changed
                return
            if age >= SWAP_IDLE_AFTER_S and not probe.get("idle"):
                # No buffer came through: relink when the pad is idle instead.
                probe["idle"] = True
                try:
                    probe["pad"].add_probe(Gst.PadProbeType.IDLE, on_block)
                except Exception:  # noqa: BLE001 - keep waiting for a buffer
                    probe["idle"] = False
            timer = threading.Timer(0.5, watchdog)
            timer.daemon = True
            timer.start()

        arm(probe["pad"])
        timer = threading.Timer(0.5, watchdog)
        timer.daemon = True
        timer.start()
        return "scheduled"

    def _finish(self) -> None:
        wanted = None
        with self._lock:
            self._busy = False
            wanted, self._wanted = self._wanted, None
        self.idle.set()
        if self._gate is not None:
            self._gate.release(self)       # a waiting neighbour may go now
        if wanted is not None:
            self.apply(*wanted)

    def _replace_in_list(self, old, new) -> None:
        if self._chain_elements is not None and old in self._chain_elements:
            self._chain_elements[self._chain_elements.index(old)] = new
