"""Animated overlays: scrolling ticker, lower third and GIF/animated images.

Like the procedural backdrops, each is a generator that draws one RGBA frame as
a function of time; ``procedural.ProceduralSource`` feeds it to the canvas.  Unlike
the backdrops they keep an alpha channel, so only the bar, card or sprite
covers the picture behind it.
"""
from __future__ import annotations

import bisect
import json
import math
import threading

import numpy as np
from PIL import Image, ImageDraw, ImageSequence

from . import overlay

MAX_GIF_FRAMES = 400
MAX_GIF_SIDE = 480


def _font(pixels: int):
    return overlay._load_font(max(8, int(pixels)))


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int, int, int]:
    return draw.textbbox((0, 0), text, font=font)


class Ticker:
    """Text that scrolls right to left along a bar, repeating."""

    label = "Ticker"

    def __init__(self, width: int, height: int, text: str, color: str = "#ffffff",
                 background: str = "#101418cc", speed: float = 0.12) -> None:
        self._lock = threading.Lock()
        self._speed_fraction = speed
        self.width, self.height = max(8, int(width)), max(8, int(height))
        text = (text or "").strip() or "AnyCam"
        font = _font(self.height * 0.56)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        item = f"{text}      \u2022      "
        left, top, right, bottom = _text_size(probe, item, font)
        # Advance width, not ink width: the trailing spaces are the gap between
        # repeats and the ink box leaves them out.
        self._period = max(1, math.ceil(probe.textlength(item, font=font)))
        # Enough repeats that the strip always covers the bar while it scrolls.
        repeats = max(2, math.ceil(self.width / self._period) + 1)
        self._strip = Image.new("RGBA", (self._period * repeats, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(self._strip)
        colour = overlay.parse_color(color)
        y = (self.height - (bottom - top)) // 2 - top
        for index in range(repeats):
            draw.text((index * self._period - left, y), item, font=font, fill=colour)
        self._bar = Image.new("RGBA", (self.width, self.height),
                              overlay.parse_color(background, (16, 20, 24, 204)))
        # Scroll speed as a fraction of the bar width per second.
        self._speed = max(0.02, float(speed)) * self.width

    def reconfigure(self, text: str, color: str, background: str) -> None:
        """Change the text and colours while it is running."""
        fresh = Ticker(self.width, self.height, text, color, background,
                       self._speed_fraction)
        with self._lock:
            self.__dict__.update({k: v for k, v in fresh.__dict__.items() if k != "_lock"})

    def frame(self, t: float) -> np.ndarray:
        with self._lock:
            offset = int(t * self._speed) % max(1, self._period)
            out = self._bar.copy()
            out.paste(self._strip, (-offset, 0), self._strip)
        return np.asarray(out, dtype=np.uint8)


# name -> (text colour, card colour, accent colour).  A lower third's ``source``
# is ``name|title|template``; an unknown or missing template is "Classic".
LOWER_THIRD_TEMPLATES: dict[str, tuple[str, str, str]] = {
    "Classic": ("#ffffff", "#101418e6", "#5eead0"),
    "Glass": ("#ffffff", "#ffffff33", "#ffffff"),
    "Bold": ("#101418", "#facc15f2", "#101418"),
    "Broadcast": ("#ffffff", "#1d4ed8f0", "#f97316"),
    "Minimal": ("#ffffff", "#00000099", "#00000000"),
    "Rose": ("#ffffff", "#9f1239eb", "#fda4af"),
}


def split_lower_third(source: str) -> tuple[str, str, str]:
    """``(name, title, template)`` from a lower third's source text."""
    parts = str(source or "").split("|")
    name = parts[0].strip() if parts else ""
    title = parts[1].strip() if len(parts) > 1 else ""
    template = parts[2].strip() if len(parts) > 2 else ""
    return name, title, template if template in LOWER_THIRD_TEMPLATES else "Classic"


def lower_third_style(source: str, color: str, background: str) -> tuple[str, str, str, str, str]:
    """``(name, title, text colour, card colour, accent)`` for a lower third.

    A template named in the source wins; without one the layer's own colours
    are used with the classic accent, as before templates existed.
    """
    name, title, template = split_lower_third(source)
    named = len(str(source or "").split("|")) > 2
    if named:
        text_colour, card, accent = LOWER_THIRD_TEMPLATES[template]
        return name, title, text_colour, card, accent
    return name, title, color, background, LOWER_THIRD_TEMPLATES["Classic"][2]


def join_lower_third(name: str, title: str, template: str = "Classic") -> str:
    """The source text for :func:`split_lower_third` (``|`` in the text is dropped)."""
    clean = lambda text: str(text or "").replace("|", "/").strip()  # noqa: E731
    base = f"{clean(name)}|{clean(title)}"
    return base if template in ("", "Classic") else f"{base}|{template}"


class LowerThird:
    """A name card that slides in from the left when it is shown."""

    label = "Lower third"
    SLIDE_SECONDS = 0.7

    def __init__(self, width: int, height: int, name: str, title: str = "",
                 color: str = "#ffffff", background: str = "#101418e6",
                 accent: str = "#5eead0") -> None:
        self._lock = threading.Lock()
        self._accent = accent
        self.width, self.height = max(8, int(width)), max(8, int(height))
        self._card = self._render(name or "Name", title, color, background, accent)
        self._start: float | None = None

    def _render(self, name, title, color, background, accent) -> Image.Image:
        width, height = self.width, self.height
        card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)
        radius = max(4, int(height * 0.12))
        draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius,
                               fill=overlay.parse_color(background, (16, 20, 24, 230)))
        bar = max(4, int(height * 0.07))
        draw.rounded_rectangle([0, 0, bar, height - 1], radius=bar // 2,
                               fill=overlay.parse_color(accent))
        text_colour = overlay.parse_color(color)
        pad = int(height * 0.22)
        if title:
            big, small = _font(height * 0.36), _font(height * 0.22)
            draw.text((pad, height * 0.14), name, font=big, fill=text_colour)
            draw.text((pad, height * 0.60), title, font=small,
                      fill=(*text_colour[:3], 200))
        else:
            font = _font(height * 0.40)
            _l, top, _r, bottom = _text_size(draw, name, font)
            draw.text((pad, (height - (bottom - top)) / 2 - top), name, font=font,
                      fill=text_colour)
        return card

    def restart(self) -> None:
        """Slide in again (called when the layer is shown)."""
        self._start = None

    def reconfigure(self, name: str, title: str, color: str, background: str,
                    accent: str | None = None) -> None:
        """Change the text and colours while it is running (and slide in again)."""
        if accent is not None:
            self._accent = accent
        card = self._render(name or "Name", title, color, background, self._accent)
        with self._lock:
            self._card = card
            self._start = None

    def frame(self, t: float) -> np.ndarray:
        with self._lock:
            if self._start is None:
                self._start = t
            progress = min(1.0, max(0.0, (t - self._start) / self.SLIDE_SECONDS))
            eased = 1.0 - (1.0 - progress) ** 3
            out = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
            out.paste(self._card, (int(-self.width * (1.0 - eased)), 0), self._card)
        return np.asarray(out, dtype=np.uint8)


def _badge(text: str, width: int, height: int, color: str, background: str) -> Image.Image:
    """``text`` centred on a rounded badge the size of the layer (with alpha)."""
    return overlay.render_text_image(text, width, height, int(height * 0.8), color,
                                     background, "badge")


class Clock:
    """The time of day on a badge; redrawn only when the text changes."""

    label = "Clock"

    def __init__(self, width: int, height: int, fmt: str = "%H:%M:%S",
                 color: str = "#ffffff", background: str = "#101418cc") -> None:
        self._lock = threading.Lock()
        self.width, self.height = max(8, int(width)), max(8, int(height))
        self._fmt, self._color, self._background = self._valid(fmt), color, background
        self._shown = ""
        self._frame: np.ndarray | None = None

    @staticmethod
    def _valid(fmt: str) -> str:
        import time as _time

        import re as _re

        fmt = (fmt or "").strip() or "%H:%M:%S"
        # glibc prints an unknown directive (``%Q``) as it is; accept only the
        # standard ones so a typo falls back to the default instead of showing.
        known = _re.compile(r"%[-_0^#]?[aAbBcCdDeFgGhHIjklmMnpPrRsStTuUVwWxXyYzZ%]")
        if _re.sub(known, "", fmt).count("%"):
            return "%H:%M:%S"
        try:
            return fmt if _time.strftime(fmt) else "%H:%M:%S"
        except (ValueError, OverflowError):
            return "%H:%M:%S"

    def reconfigure(self, fmt: str, color: str, background: str) -> None:
        with self._lock:
            self._fmt, self._color, self._background = self._valid(fmt), color, background
            self._shown, self._frame = "", None

    def text(self) -> str:
        import time as _time

        return _time.strftime(self._fmt)

    def frame(self, t: float) -> np.ndarray:
        with self._lock:
            now = self.text()
            if now != self._shown or self._frame is None:
                self._shown = now
                self._frame = np.asarray(
                    _badge(now, self.width, self.height, self._color, self._background),
                    dtype=np.uint8)
            return self._frame


def parse_timer(source: str) -> tuple[str, float]:
    """``("countdown" | "stopwatch", seconds)`` from a timer's source text.

    ``stopwatch`` counts up from zero; ``countdown:300``, ``countdown 5:00`` and
    a bare ``5:00`` or ``90`` count down from that many minutes:seconds / seconds.
    """
    text = str(source or "").strip().lower()
    if text.startswith("stopwatch"):
        return "stopwatch", 0.0
    text = text.removeprefix("countdown").lstrip(" :=").strip()
    seconds = 0.0
    try:
        if ":" in text:
            for part in text.split(":"):
                seconds = seconds * 60 + float(part or 0)
        elif text:
            seconds = float(text)
    except ValueError:
        seconds = 0.0
    return "countdown", max(0.0, seconds if seconds == seconds else 0.0)


def format_timer(seconds: float) -> str:
    """``MM:SS``, or ``H:MM:SS`` from an hour on."""
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class Timer:
    """A countdown or stopwatch badge.  Showing the layer starts it again."""

    label = "Timer"
    WARN_SECONDS = 10

    def __init__(self, width: int, height: int, source: str = "countdown:300",
                 color: str = "#ffffff", background: str = "#101418cc") -> None:
        self._lock = threading.Lock()
        self.width, self.height = max(8, int(width)), max(8, int(height))
        self._mode, self._total = parse_timer(source)
        self._color, self._background = color, background
        self._start: float | None = None
        self._shown = ""
        self._frame: np.ndarray | None = None

    def restart(self) -> None:
        with self._lock:
            self._start = None

    def reconfigure(self, source: str, color: str, background: str) -> None:
        with self._lock:
            self._mode, self._total = parse_timer(source)
            self._color, self._background = color, background
            self._start, self._shown, self._frame = None, "", None

    def reading(self, t: float) -> tuple[str, bool]:
        """``(text, warning)`` at generator time ``t`` (starts the clock on first use)."""
        if self._start is None:
            self._start = t
        elapsed = max(0.0, t - self._start)
        if self._mode == "stopwatch":
            return format_timer(elapsed), False
        left = max(0.0, self._total - elapsed)
        # whole seconds, rounded up, so 00:00 shows only when it is really over
        shown = int(left) + (0 if left == int(left) else 1)
        return format_timer(shown), shown <= self.WARN_SECONDS

    def frame(self, t: float) -> np.ndarray:
        with self._lock:
            text, warning = self.reading(t)
            key = f"{text}|{warning}"
            if key != self._shown or self._frame is None:
                self._shown = key
                color = "#ff3b5c" if warning else self._color
                self._frame = np.asarray(
                    _badge(text, self.width, self.height, color, self._background),
                    dtype=np.uint8)
            return self._frame


class GifPlayer:
    """Plays an animated GIF, WebP or APNG in a loop (a still image just shows)."""

    label = "Animated image"

    def __init__(self, path: str) -> None:
        self.frames: list[Image.Image] = []
        durations: list[float] = []
        with Image.open(path) as image:
            for index, frame in enumerate(ImageSequence.Iterator(image)):
                if index >= MAX_GIF_FRAMES:
                    break
                picture = frame.convert("RGBA")
                scale = min(1.0, MAX_GIF_SIDE / max(picture.size))
                if scale < 1.0:
                    picture = picture.resize(
                        (max(1, int(picture.width * scale)), max(1, int(picture.height * scale))),
                        Image.LANCZOS)
                self.frames.append(picture)
                durations.append(max(0.02, float(frame.info.get("duration", 100)) / 1000.0))
        if not self.frames:
            raise ValueError(f"no frames in {path}")
        self.size = self.frames[0].size
        self._ends = list(np.cumsum(durations))
        self._total = float(self._ends[-1])
        self._arrays = [np.asarray(f, dtype=np.uint8) for f in self.frames]

    def frame(self, t: float) -> np.ndarray:
        if len(self._arrays) == 1:
            return self._arrays[0]
        index = bisect.bisect_right(self._ends, t % self._total)
        return self._arrays[min(index, len(self._arrays) - 1)]


TOOLS = ("pen", "marker", "arrow", "box")


def parse_strokes(source) -> list[dict]:
    """Strokes from a drawing layer's ``source`` (JSON), ignoring bad data."""
    try:
        data = json.loads(source) if isinstance(source, str) and source.strip() else {}
    except ValueError:
        return []
    strokes = []
    for item in data.get("strokes", []) if isinstance(data, dict) else []:
        try:
            points = [(float(x), float(y)) for x, y in item["points"]]
            width = float(item.get("width", 0.006))
            if (not points or item.get("tool") not in TOOLS
                    or not all(math.isfinite(v) for point in points for v in point)):
                continue
            if not math.isfinite(width):
                width = 0.006
            # Normalised to the layer: a few boxes beyond the edge are fine,
            # 1e300 only overflows the drawing code.
            strokes.append({"tool": item["tool"], "color": str(item.get("color", "#ff3b5c")),
                            "width": min(0.5, max(0.0005, width)),
                            "points": [[min(4.0, max(-3.0, x)), min(4.0, max(-3.0, y))]
                                       for x, y in points]})
        except (KeyError, TypeError, ValueError):
            continue
    return strokes


def dump_strokes(strokes: list[dict]) -> str:
    return json.dumps({"strokes": strokes}, separators=(",", ":"))


class DrawingCanvas:
    """Freehand strokes, markers, arrows and boxes over the picture.

    Strokes are normalised to the layer box, so they survive a resize.  The
    picture is redrawn only when the strokes change; every other frame reuses
    the last one.
    """

    label = "Drawing"
    SUPERSAMPLE = 2

    def __init__(self, width: int, height: int, strokes: list[dict] | None = None) -> None:
        self.width, self.height = max(8, int(width)), max(8, int(height))
        self._lock = threading.Lock()
        self._strokes = list(strokes or [])
        self._frame: np.ndarray | None = None

    def set_strokes(self, strokes: list[dict]) -> None:
        with self._lock:
            self._strokes = [dict(item) for item in strokes]
            self._frame = None

    def frame(self, t: float) -> np.ndarray:
        with self._lock:
            if self._frame is None:
                try:
                    self._frame = self._render(self._strokes)
                except (OverflowError, ValueError, TypeError):
                    # Strokes set from outside parse_strokes: never let one
                    # bad number blank the layer for good.
                    self._frame = np.zeros((self.height, self.width, 4), np.uint8)
            return self._frame

    def _render(self, strokes: list[dict]) -> np.ndarray:
        k = self.SUPERSAMPLE
        canvas = Image.new("RGBA", (self.width * k, self.height * k), (0, 0, 0, 0))
        for stroke in strokes:
            colour = overlay.parse_color(stroke["color"])
            width = max(2, int(stroke["width"] * self.width * k))
            points = [(x * self.width * k, y * self.height * k) for x, y in stroke["points"]]
            layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(layer)
            tool = stroke["tool"]
            if tool == "marker":
                self._line(draw, points, (*colour[:3], 255), width * 4)
                layer.putalpha(layer.getchannel("A").point(lambda v: v * 110 // 255))
            elif tool == "arrow" and len(points) >= 2:
                self._arrow(draw, points[0], points[-1], (*colour[:3], 255), width)
            elif tool == "box" and len(points) >= 2:
                (x0, y0), (x1, y1) = points[0], points[-1]
                draw.rectangle([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
                               outline=(*colour[:3], 255), width=width)
            else:
                self._line(draw, points, (*colour[:3], 255), width)
            canvas = Image.alpha_composite(canvas, layer)
        out = canvas.resize((self.width, self.height), Image.LANCZOS)
        return np.asarray(out, dtype=np.uint8)

    @staticmethod
    def _line(draw, points, colour, width) -> None:
        if len(points) == 1:
            x, y = points[0]
            draw.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=colour)
            return
        draw.line(points, fill=colour, width=width, joint="curve")
        for x, y in (points[0], points[-1]):          # round caps
            draw.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=colour)

    @staticmethod
    def _arrow(draw, start, end, colour, width) -> None:
        draw.line([start, end], fill=colour, width=width)
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        size = width * 4.5
        left = (end[0] - size * math.cos(angle - 0.45), end[1] - size * math.sin(angle - 0.45))
        right = (end[0] - size * math.cos(angle + 0.45), end[1] - size * math.sin(angle + 0.45))
        draw.polygon([end, left, right], fill=colour)
