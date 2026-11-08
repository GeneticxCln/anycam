"""Source chains for the AnyCam compositor.

Every source produces a normalised RGBA video stream at the layer box size and
frame rate.  Aspect ratio is preserved: the picture is scaled to fit inside the
box (the compositor pad centres it), so a 16:9 camera in a portrait
box shows the whole frame instead of a squeezed one.  A small amount of probing
is used for cameras so the pipeline does not fail when a device does not
advertise the exact MJPG mode we prefer.
"""
from __future__ import annotations

import functools
import itertools

import os
import re
import sys
import weakref
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from . import config, procedural
from .geometry import Rect, crop_pixels, fit_contain
from .effects import FilterSlot, SwapGate
from .model import Effects


def no_qos(element: Gst.Element) -> Gst.Element:
    """Turn off QoS frame dropping on a filter or converter (not on sinks).

    Upstream of a live mixer QoS only does harm.  The mixer reports late input
    and QoS-enabled transforms start discarding frames to catch up.  A colour
    look adds ~14 ms per frame on the source thread; that made the frame
    overlay (gdkpixbufoverlay) drop every camera frame, 60 in and 0 out, for
    as long as the look was on: the camera froze.
    """
    factory = element.get_factory()
    klass = factory.get_metadata("klass") if factory is not None else ""
    if "Sink" not in (klass or "") and element.find_property("qos") is not None:
        element.set_property("qos", False)
    if isinstance(element, Gst.Bin):
        # glvideoflip and friends are bins around a QoS-enabled transform.
        for child in element.children:
            no_qos(child)
    return element


def make(factory: str, name: str | None = None, **props) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer element '{factory}' is not installed")
    for key, value in props.items():
        element.set_property(key.replace("_", "-"), value)
    return no_qos(element)


def make_optional(factory: str, name: str | None = None, **props) -> Gst.Element:
    """Make an element, skipping properties this build does not provide.

    ``pipewiresrc`` gained ``provide-clock`` in a newer PipeWire than Ubuntu
    24.04 ships (1.0.5).  Setting it there raises ``TypeError`` and took the
    whole screen chain down instead of falling back to the default.
    """
    element = make(factory, name)
    for key, value in props.items():
        prop = key.replace("_", "-")
        if element.find_property(prop) is not None:
            element.set_property(prop, value)
    return element


def caps_string(width: int, height: int, fps: int, fmt: str = "RGBA") -> str:
    return f"video/x-raw,format={fmt},width={width},height={height},framerate={fps}/1"


def caps_filter(text: str, name: str | None = None) -> Gst.Element:
    element = make("capsfilter", name)
    element.set_property("caps", Gst.Caps.from_string(text))
    return element


@dataclass
class CameraMode:
    pixel_format: str
    width: int
    height: int
    fps: int


_LAST_PROBE_ERROR: dict[str, str] = {}


def camera_probe_error(device: str) -> str:
    """Why the last ``camera_modes(device)`` probe returned no modes (or "")."""
    return _LAST_PROBE_ERROR.get(device, "")


def camera_modes(device: str) -> list[CameraMode]:
    """Parse ``v4l2-ctl --list-formats-ext`` into usable modes."""
    import subprocess

    try:
        proc = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-formats-ext"],
            capture_output=True, text=True, timeout=5,
        )
        out = proc.stdout
        if proc.returncode != 0:
            _LAST_PROBE_ERROR[device] = (proc.stderr or "v4l2-ctl probe failed").strip()
            return []
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LAST_PROBE_ERROR[device] = f"camera probe failed: {exc}"
        return []
    _LAST_PROBE_ERROR.pop(device, None)
    modes: list[CameraMode] = []
    fmt = None
    size = None
    for line in out.splitlines():
        m = re.search(r"\[\d+\]:\s+'([^']+)'", line)
        if m:
            fmt = m.group(1)
            continue
        m = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            continue
        m = re.search(r"Interval:\s+Discrete\s+([\d.]+)s\s+\(([\d.]+)\s+fps\)", line)
        if m and fmt and size:
            try:
                fps = int(round(float(m.group(2))))
            except ValueError:
                continue
            modes.append(CameraMode(fmt, size[0], size[1], fps))
    return modes


# MJPG first: it is the only format that carries 1080p60 on the StreamCam and
# it needs less USB bandwidth than the raw formats at the same resolution.
PREFERRED_FORMATS = ("MJPG", "H264", "NV12", "YUYV")

# V4L2 fourcc names do not always match GStreamer's raw-video format names.
_RAW_GST_FORMATS = {
    "YUYV": "YUY2",
    "UYVY": "UYVY",
    "NV12": "NV12",
    "YU12": "I420",
    "YV12": "YV12",
    "BGR4": "BGRx",
    "RGB4": "RGBx",
    "BGR3": "BGR",
    "RGB3": "RGB",
}


def _gst_raw_format(fourcc: str) -> str:
    return _RAW_GST_FORMATS.get(fourcc, fourcc)


def _format_rank(fmt: str) -> int:
    return PREFERRED_FORMATS.index(fmt) if fmt in PREFERRED_FORMATS else 99


def _closest_size(modes: list[CameraMode], width: int, height: int) -> list[CameraMode]:
    """All modes at the size closest to the request.

    ``v4l2-ctl`` lists small modes first, so taking the first entry when there
    is no exact match silently selects 320x240 for a 720p request.  Prefer the
    smallest advertised size that still covers the request, then the largest
    available: both avoid upscaling a tiny frame into a big box.
    """
    sizes = {(m.width, m.height) for m in modes}
    if not sizes:
        return []
    if (width, height) in sizes:
        return [m for m in modes if (m.width, m.height) == (width, height)]

    def rank(size: tuple[int, int]) -> tuple[int, int]:
        w, h = size
        covers = w >= width and h >= height
        area = w * h
        return (0, area) if covers else (1, -area)

    best = min(sizes, key=rank)
    return [m for m in modes if (m.width, m.height) == best]


def choose_from_modes(modes: list[CameraMode], width: int, height: int, fps: int) -> CameraMode:
    """Pick the best advertised mode.

    The requested size wins when it is advertised.  Otherwise the nearest size
    is used instead of whatever the driver happens to list first.  Within that
    size an exact frame rate wins, then the closest higher rate, then the
    closest lower rate.  MJPG is preferred because it is the only format that
    carries 1080p60 on the StreamCam and it uses less USB bandwidth.
    """
    if not modes:
        return CameraMode("MJPG", width, height, fps)
    # A size only counts when the camera delivers it at a usable rate.  The
    # Logitech StreamCam lists 2304x1296 as YUYV at 5 fps; preferring the
    # largest size made a 1440p or 4K canvas run the camera at 5 fps.
    usable = [m for m in modes if m.fps >= min(fps, 24)]
    if not usable:
        fastest = max(m.fps for m in modes)
        usable = [m for m in modes if m.fps == fastest]
    pool = _closest_size(usable, width, height) or usable
    exact = [m for m in pool if m.fps == fps]
    if exact:
        return sorted(exact, key=lambda m: _format_rank(m.pixel_format))[0]
    higher = sorted((m for m in pool if m.fps > fps), key=lambda m: (m.fps, _format_rank(m.pixel_format)))
    if higher:
        return higher[0]
    return sorted(pool, key=lambda m: (abs(m.fps - fps), _format_rank(m.pixel_format)))[0]


def choose_camera_mode(device: str, width: int, height: int, fps: int) -> CameraMode:
    return choose_from_modes(camera_modes(device), width, height, fps)


def camera_formats(device: str) -> list[str]:
    """List V4L2 fourcc names for a device.

    ``camera_modes`` needs discrete frame intervals.  Loopback/test devices
    advertise continuous sizes without intervals, so they need this fallback
    to negotiate a supported raw format instead of blindly requesting MJPG.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["v4l2-ctl", "-d", device, "--list-formats"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return re.findall(r"\[\d+\]:\s+'([^']+)'", proc.stdout)


def _loopback_mode(device: str, width: int, height: int, fps: int) -> CameraMode | None:
    formats = camera_formats(device)
    for fourcc in ("YUYV", "NV12", "UYVY"):
        if fourcc in formats:
            return CameraMode(fourcc, width, height, fps)
    for fourcc in formats:
        if fourcc in _RAW_GST_FORMATS:
            return CameraMode(fourcc, width, height, fps)
    return None


def jpeg_decoder():
    """Pick the MJPG decoder.

    nvjpegdec (NVDEC) only decodes 4:2:0 JPEG, and UVC webcams such as the
    StreamCam send 4:2:2 MJPEG: it fails with "no valid frames found" (checked
    2026-11-08 with GStreamer 1.28 and driver 615: the same frame re-encoded
    4:2:0 decodes, 4:2:2 does not).  jpegdec is also the cheapest CPU choice
    measured: 34% of a core for 1080p60 decode plus RGBA conversion, against
    61% for avdec_mjpeg; idct-method=ifast saved nothing.  Set
    ANYCAM_JPEG_DECODER=nv to opt in on a camera that sends 4:2:0.
    """
    choice = os.environ.get("ANYCAM_JPEG_DECODER", "auto").lower()
    if choice in ("nv", "nvjpeg") and Gst.ElementFactory.find("nvjpegdec"):
        return make("nvjpegdec")
    return make("jpegdec")


@dataclass
class SourceChain:
    """A linked source chain ending in a caps filter that feeds the compositor."""

    elements: list[Gst.Element] = field(default_factory=list)
    tail: Gst.Element | None = None
    pause_elements: list[Gst.Element] = field(default_factory=list)
    dynamic_from: Gst.Element | None = None
    # Negotiated source size before the box fit: the crop editor and the
    # normalised crop both need to know what "100%" means for this layer.
    source_size: tuple[int, int] | None = None
    # The live effect rack (balance, blur, alpha) is kept on the chain so a
    # slider can change properties on the running graph without a rebuild.
    effects: dict = field(default_factory=dict)
    # Called when the canvas goes away (a procedural source stops its thread).
    cleanup: list = field(default_factory=list)
    # Object with ``restart()`` (a lower third slides in again when shown).
    control: object = None

    def add(self, *elements: Gst.Element) -> None:
        self.elements.extend(elements)

    @property
    def output_pad(self):
        if self.tail is None:
            raise RuntimeError("source chain has no tail element")
        return self.tail.get_static_pad("src")


def parse_chroma_color(value: str, default: tuple[int, int, int] = (0, 255, 0)) -> tuple[int, int, int]:
    """Parse ``#rgb``/``#rrggbb`` into bytes without ever raising.

    A malformed colour used to raise ``ValueError`` inside the graph build and
    stop the whole preview; ``#fff`` (the compact form) was silently ignored.
    """
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) >= 6:
        try:
            return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return default
    return default


def _effect_number(value, default: float) -> float:
    """Read a numeric effect value without letting malformed JSON stop a build."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


COLOR_EFFECTS = frozenset({"none", "sepia", "heat", "xray", "xpro", "yellowblue"})

ANIMATION_PRESETS: dict[str, dict[str, object]] = {
    key: {"label": generator.label}
    for key, generator in procedural.GENERATORS.items()
}


def _effects_chain(layer) -> dict[str, Gst.Element]:
    """The cheap, always-present effect rack for one layer.

    Everything here is a per-pixel or scanline transform: ``videobalance``
    for colour, ``coloreffects`` for the film LUTs, ``videoflip`` for mirror,
    and ``alpha`` for the chroma key.  They stay wired so every control is
    live and nothing rebuilds the canvas.  Heavier effects (blur, distortion,
    the retro TV family) are not here: they live in the swappable filter slot
    (``anycam.effects``) so a layer only pays for the one it uses.
    """
    fx = getattr(layer, "effects", None) or Effects()
    balance = make("videobalance")
    # ``set`` with alpha=1 preserves an existing per-pixel alpha channel (text
    # and logo layers), while ``custom`` performs the chroma key.
    alpha = make("alpha", method="set", alpha=1.0, prefer_passthrough=True)
    flip = make("videoflip", video_direction="identity")
    elements = {"balance": balance, "videoflip": flip, "alpha": alpha}
    if Gst.ElementFactory.find("coloreffects") is not None:
        elements["style"] = make("coloreffects", preset="none")
    apply_effects(elements, fx)
    return elements


# videobalance's own maths as a fragment shader (BT.601 studio-range matrices,
# luma contrast around 16 plus brightness, chroma hue rotation and saturation,
# with its clamps and rounding).  glcolorbalance uses different contrast maths
# (Noir came out ~10/255 off); this matches videobalance within ~1/255.
BALANCE_SHADER = """
#ifdef GL_ES
precision highp float;
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
uniform float brightness;
uniform float contrast;
uniform float saturation;
uniform float hue;
void main () {
  vec4 c = texture2D(tex, v_texcoord);
  vec3 rgb = c.rgb * 255.0;
  float y  = clamp( 0.256816*rgb.r + 0.504154*rgb.g + 0.0979137*rgb.b + 16.0, 0.0, 255.0);
  float cb = clamp(-0.148246*rgb.r - 0.29102*rgb.g  + 0.439266*rgb.b + 128.0, 0.0, 255.0);
  float cr = clamp( 0.439271*rgb.r - 0.367833*rgb.g - 0.071438*rgb.b + 128.0, 0.0, 255.0);
  y = clamp(floor(16.0 + (y - 16.0) * contrast + brightness * 255.0 + 0.5), 0.0, 255.0);
  float i = cb - 128.0;
  float j = cr - 128.0;
  float hc = cos(3.14159265 * hue);
  float hs = sin(3.14159265 * hue);
  float u = clamp(floor(128.0 + (i * hc + j * hs) * saturation + 0.5), 0.0, 255.0);
  float v = clamp(floor(128.0 + (-i * hs + j * hc) * saturation + 0.5), 0.0, 255.0);
  vec3 o;
  o.r = 1.16438*(y-16.0) + 1.59603*(v-128.0);
  o.g = 1.16438*(y-16.0) - 0.391762*(u-128.0) - 0.812968*(v-128.0);
  o.b = 1.16438*(y-16.0) + 2.01723*(u-128.0);
  gl_FragColor = vec4(clamp(o / 255.0, 0.0, 1.0), c.a);
}
"""


def gl_rack_elements(mirror_on_gpu: bool = True) -> dict[str, Gst.Element] | None:
    """GPU colour balance (and mirror) for a layer on the GPU canvas, or None.

    ``mirror_on_gpu`` is False for a layer whose crop is not left/right
    symmetric: the GPU mirror runs after the crop, the CPU one before it, so
    mirroring there would show the opposite edge of the picture.
    """
    if Gst.ElementFactory.find("glshader") is None:
        return None
    shader = make("glshader")
    shader.set_property("fragment", BALANCE_SHADER)
    _set_balance_uniforms(shader, 0.0, 1.0, 1.0, 0.0)
    rack = {"gl_balance": shader}
    if mirror_on_gpu and Gst.ElementFactory.find("glvideoflip") is not None:
        rack["gl_flip"] = make("glvideoflip", video_direction="identity")
    return rack


def _set_balance_uniforms(shader: Gst.Element, brightness: float, contrast: float,
                          saturation: float, hue: float) -> None:
    from gi.repository import GObject

    # glshader only takes float uniforms; doubles were ignored (all zero: black).
    uniforms = Gst.Structure.new_empty("uniforms")
    for key, value in (("brightness", brightness), ("contrast", contrast),
                       ("saturation", saturation), ("hue", hue)):
        typed = GObject.Value(GObject.TYPE_FLOAT)
        typed.set_float(float(value))
        uniforms.set_value(key, typed)
    shader.set_property("uniforms", uniforms)


def gpu_colour_is_equivalent(fx: Effects) -> bool:
    """Whether the colour step can run after the upload without changing the
    picture: nothing order-sensitive follows it in the CPU rack (a filter or
    processor, a frame, a film style, the chroma key)."""
    return (str(getattr(fx, "filter", "none") or "none") == "none"
            and str(getattr(fx, "filter2", "none") or "none") == "none"
            and str(getattr(fx, "frame_style", "none") or "none") == "none"
            and str(getattr(fx, "color_effect", "none") or "none") == "none"
            and not bool(getattr(fx, "chroma_enabled", False)))


_frame_shown: "weakref.WeakKeyDictionary[Gst.Element, tuple]" = weakref.WeakKeyDictionary()
_frame_fallback_noted = False


@functools.lru_cache(maxsize=6)
def _frame_pixbuf(path: str, _stamp: tuple):
    """The frame PNG as an in-memory GdkPixbuf (decoded here, by Pillow)."""
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib
    from PIL import Image

    with Image.open(path) as image:
        rgba = image.convert("RGBA")
    width, height = rgba.size
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(rgba.tobytes()), GdkPixbuf.Colorspace.RGB, True, 8,
        width, height, width * 4)


def _set_frame_picture(frame: Gst.Element, path) -> None:
    """Give ``gdkpixbufoverlay`` the frame picture without its file loader.

    Setting ``location`` makes gdk-pixbuf decode the file on the calling (GUI)
    thread, and gdk-pixbuf 2.44 does that through glycin: a sandboxed
    ``bwrap`` loader process per element, about 10 ms each time, on every
    build and effect change.  A frozen window was found blocked on a lock
    while glycin's loader thread waited on a process that no longer existed.
    An already decoded pixbuf needs no loader, and an unchanged picture is
    not handed over again.
    """
    path = str(path)
    try:
        info = os.stat(path)
        stamp = (info.st_mtime_ns, info.st_size)
    except OSError:
        stamp = (0, 0)
    key = (path, stamp)
    if _frame_shown.get(frame) == key:
        return
    try:
        pixbuf = _frame_pixbuf(path, stamp)
    except Exception as exc:  # noqa: BLE001 - fall back to the element's own loader
        global _frame_fallback_noted
        if not _frame_fallback_noted:
            _frame_fallback_noted = True
            print(f"[frame] decoding frame pictures in-process failed ({exc}); "
                  "using gdk-pixbuf's file loader", file=sys.stderr, flush=True)
        frame.set_property("location", path)
    else:
        frame.set_property("pixbuf", pixbuf)
    _frame_shown[frame] = key


def apply_effects(elements: dict, fx: Effects) -> None:
    """Apply an Effects model to a live rack without renegotiating the graph."""
    if not elements:
        return
    brightness = max(-1.0, min(1.0, _effect_number(getattr(fx, "brightness", 0.0), 0.0)))
    # videobalance advertises 0..2 for contrast/saturation.  The old GUI
    # offered 300%, which is outside the element range and could fail at
    # runtime, so both the clamp and the panel range now stop at 200%.
    contrast = max(0.0, min(2.0, _effect_number(getattr(fx, "contrast", 1.0), 1.0)))
    saturation = max(0.0, min(2.0, _effect_number(getattr(fx, "saturation", 1.0), 1.0)))
    hue = max(-1.0, min(1.0, _effect_number(getattr(fx, "hue", 0.0), 0.0)))
    mirror = bool(getattr(fx, "mirror", False))
    # On the GPU canvas the colour step and the mirror run as shaders after the
    # upload whenever that gives the same picture: a colour look on the CPU
    # cost ~13 ms per 1080p frame on the source thread.  Otherwise the CPU
    # elements do it, as on the CPU canvas.
    gpu = elements.get("gl_balance") is not None and gpu_colour_is_equivalent(fx)
    gl_balance = elements.get("gl_balance")
    if gl_balance is not None:
        if gpu:
            _set_balance_uniforms(gl_balance, brightness, contrast, saturation, hue)
        else:
            _set_balance_uniforms(gl_balance, 0.0, 1.0, 1.0, 0.0)
    gl_flip = elements.get("gl_flip")
    # The GPU mirror only exists when the layer's crop is left/right symmetric
    # (see _link_to_mixer); otherwise the CPU mirror, which runs before the
    # crop, keeps the region drawn in the crop editor.
    flip_on_gpu = gpu and gl_flip is not None
    if gl_flip is not None:
        gl_flip.set_property("video-direction", "horiz" if flip_on_gpu and mirror else "identity")
    balance = elements.get("balance")
    if balance is not None:
        neutral = gpu
        balance.set_property("brightness", 0.0 if neutral else brightness)
        balance.set_property("contrast", 1.0 if neutral else contrast)
        balance.set_property("saturation", 1.0 if neutral else saturation)
        balance.set_property("hue", 0.0 if neutral else hue)

    for slot_key, attr in (("slot", "filter"), ("slot2", "filter2")):
        slot = elements.get(slot_key)
        if slot is not None:
            slot.apply(getattr(fx, attr, "none"),
                       _effect_number(getattr(fx, f"{attr}_amount", 0.5), 0.5))

    flip = elements.get("videoflip")
    if flip is not None:
        flip.set_property("video-direction",
                          "horiz" if mirror and not flip_on_gpu else "identity")

    style = elements.get("style")
    if style is not None:
        effect = str(getattr(fx, "color_effect", "none") or "none").lower()
        style.set_property("preset", effect if effect in COLOR_EFFECTS else "none")

    frame = elements.get("frame")
    if frame is not None:
        from . import overlay
        width = int(frame.get_property("overlay-width") or 640)
        height = int(frame.get_property("overlay-height") or 360)
        frame_style = str(getattr(fx, "frame_style", "none") or "none").lower()
        if frame_style not in overlay.WINDOW_FRAME_STYLES:
            frame_style = "none"
        frame_color = str(getattr(fx, "frame_color", "#5eead0") or "#5eead0")
        frame_width = max(1, min(48, int(_effect_number(
            getattr(fx, "frame_width", 6), 6))))
        frame_matte = str(getattr(fx, "frame_matte", "#0b0d10") or "#0b0d10")
        try:
            path = overlay.render_window_frame(
                width, height, frame_style, frame_color, frame_width,
                matte=frame_matte)
        except Exception as exc:  # noqa: BLE001 - a frame must never stop the canvas
            # This runs inside the chain build: an exception here aborted
            # "Could not start the pipeline", on every launch, because the
            # effect is saved in the project.  Show the layer unframed.
            print(f"[frame] could not draw {frame_style} at {width}x{height}: {exc}",
                  file=sys.stderr, flush=True)
            frame_style = "none"
            path = overlay.render_window_frame(width, height, "none")
        _set_frame_picture(frame, path)
        # A transparent PNG plus alpha=0 is a true bypass for the common case.
        frame.set_property("alpha", 0.0 if frame_style == "none" else 1.0)

    alpha = elements.get("alpha")
    if alpha is not None:
        if bool(getattr(fx, "chroma_enabled", False)):
            red, green, blue = parse_chroma_color(
                str(getattr(fx, "chroma_key", "#00ff00") or "#00ff00"))
            alpha.set_property("method", "custom")
            alpha.set_property("target-r", red)
            alpha.set_property("target-g", green)
            alpha.set_property("target-b", blue)
            # ``noise-level`` is 0..64, so map the UI's 0..1 tolerance onto
            # that range.  Keeping the default sensitivity at ~100 is also
            # essential: the old 13 made bright green count as "too bright to
            # key", which is why the checkbox appeared to do nothing.
            similarity = max(0.0, min(1.0, _effect_number(
                getattr(fx, "chroma_similarity", 0.35), 0.35)))
            smoothness = max(0.0, min(1.0, _effect_number(
                getattr(fx, "chroma_smoothness", 0.10), 0.10)))
            alpha.set_property("noise-level", similarity * 64.0)
            sensitivity = max(1, min(128, int(round(128.0 - smoothness * 28.0))))
            alpha.set_property("black-sensitivity", sensitivity)
            alpha.set_property("white-sensitivity", sensitivity)
        else:
            # Clear any previous key and restore opaque output.  Switching the
            # method live is supported by the element and avoids a graph
            # rebuild when the checkbox is toggled.
            alpha.set_property("method", "set")
            alpha.set_property("alpha", 1.0)


def _add_effects(chain: SourceChain, layer) -> None:
    elements = _effects_chain(layer)
    chain.effects = elements
    chain.add(elements["balance"])
    if "style" in elements:
        chain.add(elements["style"])
    chain.add(elements["videoflip"], elements["alpha"], make("videoconvert"))


def camera_capture_request(layer, width: int, height: int,
                           fps: int) -> tuple[int, int, int]:
    """Resolve a layer's optional capture mode over the fallback size.

    ``width``/``height`` must be the canvas, not the layer box: a picture-in-
    picture box used to decide the camera mode, so enlarging the box silently
    kept the small capture size.
    """
    if layer is None:
        return max(2, width), max(2, height), max(1, fps)
    cap_w = max(2, int(getattr(layer, "camera_width", 0) or width))
    cap_h = max(2, int(getattr(layer, "camera_height", 0) or height))
    cap_fps = max(1, int(getattr(layer, "camera_fps", 0) or fps))
    return cap_w, cap_h, cap_fps


def cropped_size(layer, width: int, height: int) -> tuple[int, int]:
    """Source size after the layer's normalised crop."""
    crop = getattr(layer, "crop", None) or (0.0, 0.0, 0.0, 0.0)
    left, top, right, bottom = crop_pixels(width, height, crop)
    return max(1, width - left - right), max(1, height - top - bottom)


SCALE_THREADS = min(4, max(1, (os.cpu_count() or 2) // 4))


def _fit_letterbox(chain: SourceChain, src_w: int, src_h: int,
                   width: int, height: int, rate: bool = True,
                   live: bool = False, fps: int = 0) -> Rect:
    """Scale a source to fit inside the box, preserving its aspect ratio.

    The chain outputs only the fitted picture (the returned rectangle's size).
    The compositor pad is the layer box and uses ``sizing-policy=
    keep-aspect-ratio``, so it centres the picture in the box and keeps doing
    so when the box is moved or resized live.  Baking black bands into the
    frame here (the old ``videobox``) distorted the picture as soon as the pad
    changed shape without a rebuild.  The bands are now transparent.
    """
    rect = fit_contain(src_w, src_h, width, height)
    # Several threads: a camera shrunk into a box (a stage insets it) is a
    # full-frame resample at the canvas rate; on one thread it saturated a
    # core and the layer's leaky queue dropped frames (visible lag).
    chain.add(make("videoscale", n_threads=SCALE_THREADS))
    chain.add(caps_filter(
        f"video/x-raw,format=RGBA,width={rect.w},height={rect.h}"))
    if rate:
        # A live source must not be padded with duplicates.  When an effect
        # made the source thread slower than the frame rate, videorate filled
        # the gaps with copies, the thread spent its time on those, the source
        # queue dropped the real frames, and a Distort filter showed 4 new
        # frames in 2 s (51 frames out, 47 of them copies).  The mixer repeats
        # a layer's last frame by itself; videorate only drops for a live one.
        rate_element = make("videorate", drop_only=bool(live))
        if live and fps > 0:
            # Drop-only cannot raise a slower source to a fixed higher rate
            # (30 fps into a 60/1 chain failed to link), so a live chain caps
            # the rate here and leaves it open in its final caps.
            rate_element.set_property("max-rate", int(fps))
        chain.add(rate_element)
    return rect


# Sources that pace themselves (a clock-driven capture), as opposed to a
# frozen image or a generated card that must be paced by back-pressure.
LIVE_KINDS = frozenset({"camera", "screen", "phone", "network"})


def _finish_chain(chain: SourceChain, width: int, height: int, fps: int, *,
                  pin_rate: bool = True, layer=None) -> None:
    caps = caps_string(width, height, fps) if pin_rate else (
        f"video/x-raw,format=RGBA,width={width},height={height}")
    if layer is not None:
        # The swappable filter stage sits after the fit, so a distortion is
        # relative to the picture the viewer sees and costs no more than the
        # fitted size.  It is a pass-through until a filter is chosen.
        fx = getattr(layer, "effects", None) or Effects()
        gate = SwapGate()    # the two slots must not swap at the same time
        live = getattr(layer, "kind", "") in LIVE_KINDS
        slot = FilterSlot(getattr(fx, "filter", "none"),
                          getattr(fx, "filter_amount", 0.5), chain.elements,
                          size=(int(width), int(height)), fps=int(fps), gate=gate,
                          live=live)
        chain.add(slot.element)
        chain.effects["slot"] = slot
        chain.cleanup.append(slot.close)
        slot2 = FilterSlot(getattr(fx, "filter2", "none"),
                           getattr(fx, "filter2_amount", 0.5), chain.elements,
                           size=(int(width), int(height)), fps=int(fps), gate=gate,
                           live=live)
        chain.add(slot2.element)
        chain.effects["slot2"] = slot2
        chain.cleanup.append(slot2.close)
    if layer is not None and Gst.ElementFactory.find("gdkpixbufoverlay") is not None:
        frame = make("gdkpixbufoverlay", overlay_width=int(width),
                     overlay_height=int(height))
        chain.add(frame)
        chain.effects["frame"] = frame
    tail = caps_filter(caps)
    chain.add(tail)
    chain.tail = tail
    if layer is not None:
        apply_effects(chain.effects, getattr(layer, "effects", None) or Effects())


def camera_chain(device: str, width: int, height: int, fps: int, layer=None,
                 capture: tuple[int, int, int] | None = None) -> SourceChain:
    cap_w, cap_h, cap_fps = capture or camera_capture_request(layer, width, height, fps)
    modes = camera_modes(device)
    mode = choose_from_modes(modes, cap_w, cap_h, cap_fps)
    if not modes:
        # v4l2loopback and similar synthetic devices expose continuous sizes
        # but no discrete intervals.  Use their format list to pick a raw
        # format instead of the MJPG default, which they do not implement.
        mode = _loopback_mode(device, cap_w, cap_h, cap_fps) or mode
    chain = SourceChain()
    src = make("v4l2src", device=device, do_timestamp=True, io_mode="mmap")
    chain.add(src)
    chain.pause_elements.append(src)
    if mode.pixel_format in ("MJPG", "JPEG"):
        caps = caps_filter(f"image/jpeg,width={mode.width},height={mode.height},framerate={mode.fps}/1")
        chain.add(caps, jpeg_decoder())
    elif mode.pixel_format == "H264":
        caps = caps_filter(f"video/x-h264,width={mode.width},height={mode.height},framerate={mode.fps}/1")
        chain.add(caps, make("h264parse"), make("avdec_h264"))
    else:
        fmt = _gst_raw_format(mode.pixel_format)
        caps = caps_filter(f"video/x-raw,format={fmt},width={mode.width},height={mode.height},framerate={mode.fps}/1")
        chain.add(caps)
    chain.add(make("videoconvert"))
    _add_effects(chain, layer)
    # Crop percentages apply to the camera frame, not the compositor box.
    chain.add(*_crop_chain(layer, mode.width, mode.height))
    cropped_w, cropped_h = cropped_size(layer, mode.width, mode.height)
    live = getattr(layer, "kind", "") in LIVE_KINDS
    fitted = _fit_letterbox(chain, cropped_w, cropped_h, width, height,
                            live=live, fps=fps)
    _finish_chain(chain, fitted.w, fitted.h, fps, layer=layer, pin_rate=not live)
    chain.camera_mode = mode
    chain.capture_request = (cap_w, cap_h, cap_fps)
    chain.camera_probe_error = camera_probe_error(device)
    chain.source_size = (mode.width, mode.height)
    return chain



def _crop_chain(layer, width: int, height: int,
                mirrored_after: bool = False) -> list[Gst.Element]:
    """Optional videocrop from a normalised (left, top, right, bottom) crop.

    ``mirrored_after`` is for chains that crop first and mirror later: the
    crop is taken from the opposite edge so it matches what the user drew on
    the mirrored picture (the other chains mirror before cropping).
    """
    crop = getattr(layer, "crop", None)
    if not crop or not any(v != 0.0 for v in crop):
        return []
    if mirrored_after and getattr(getattr(layer, "effects", None), "mirror", False):
        crop = (crop[2], crop[1], crop[0], crop[3])
    # Clamp exactly like geometry.crop_pixels so a 1.0 crop cannot request a
    # zero-pixel frame (videocrop then stalls instead of negotiating).
    left, top, right, bottom = crop_pixels(width, height, crop)
    vc = make("videocrop")
    vc.set_property("left", left)
    vc.set_property("top", top)
    vc.set_property("right", right)
    vc.set_property("bottom", bottom)
    return [vc, make("videoconvert")]


_VIDEO_SIZE_CACHE: dict[tuple[str, float], tuple[int, int]] = {}


def video_size(path: str) -> tuple[int, int] | None:
    """Probe a video file's pixel size (used to crop it by source fractions)."""
    import subprocess

    source = Path(path).expanduser()
    try:
        mtime = source.stat().st_mtime
    except OSError:
        return None
    key = (str(source.resolve()), mtime)
    if key in _VIDEO_SIZE_CACHE:
        return _VIDEO_SIZE_CACHE[key]
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(source)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.strip().split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            size = (int(parts[0]), int(parts[1]))
            _VIDEO_SIZE_CACHE[key] = size
            return size
    return None


def _image_pixel_size(path: Path) -> tuple[int, int] | None:
    """Natural pixel size of an image file, or ``None`` when unreadable."""
    try:
        from PIL import Image

        with Image.open(path) as opened:
            return int(opened.width), int(opened.height)
    except Exception:  # noqa: BLE001 - an unreadable file is the decoder's problem
        return None


def image_chain(path: str, width: int, height: int, fps: int, layer=None,
                fit: bool = True) -> SourceChain:
    chain = SourceChain()
    chain.dynamic_from = None
    source = Path(path).expanduser()
    src = make("filesrc", location=str(source))
    chain.add(src)
    chain.pause_elements.append(src)
    suffix = source.suffix.lower()
    if suffix == ".png":
        decoder = make("pngdec")
    elif suffix in (".jpg", ".jpeg"):
        decoder = make("jpegdec")
    elif suffix == ".webp" and Gst.ElementFactory.find("webpdec"):
        decoder = make("webpdec")
    else:
        decoder = make("decodebin")
        chain.dynamic_from = decoder
    if (layer is not None and getattr(layer, "role", "") == "backdrop" and not fit
            and (getattr(layer, "effects", None) or Effects()) == Effects()):
        # A stage never changes and has no effect rack: scale it once and
        # repeat the finished frame.  With imagefreeze early, the fit, effect
        # rack, filter slots and overlay re-ran on the same 1080p picture 60
        # times a second (a third of a core, and lag on the camera).
        tail = caps_filter(caps_string(int(width), int(height), fps))
        chain.add(decoder, make("videoconvert"), make("videoscale", n_threads=SCALE_THREADS),
                  caps_filter(f"video/x-raw,format=RGBA,width={int(width)},height={int(height)}"),
                  make("imagefreeze"), tail)
        chain.tail = tail
        chain.source_size = (int(width), int(height))
        return chain
    chain.add(decoder, make("videoconvert"))
    # A raw image is cropped and fitted by its own size.  Taking the box size
    # for it stretched every photo to the box and applied crop fractions to
    # the wrong dimensions.  Pre-rendered overlays (fit=False) are box-sized.
    src_w, src_h = (_image_pixel_size(source) or (width, height)) if fit else (width, height)
    chain.add(*_crop_chain(layer, src_w, src_h, mirrored_after=True))
    chain.add(make("imagefreeze"))
    # Overlay images are pre-rendered at box size with their transparency
    # already baked in (fit=False stretches them to the pad, which is what the
    # rendered PNG expects).  A raw image file is fitted inside the box.
    fitted = Rect(0, 0, int(width), int(height))
    if fit:
        cropped_w, cropped_h = cropped_size(layer, src_w, src_h)
        fitted = _fit_letterbox(chain, cropped_w, cropped_h, width, height, rate=False)
    _add_effects(chain, layer)
    _finish_chain(chain, fitted.w, fitted.h, fps, layer=layer)
    chain.source_size = (src_w, src_h)
    return chain


def file_video_chain(path: str, width: int, height: int, fps: int, layer=None) -> SourceChain:
    chain = SourceChain()
    src = make("filesrc", location=str(Path(path).expanduser()))
    chain.add(src)
    chain.pause_elements.append(src)
    decode = make("decodebin")
    chain.add(decode, make("videoconvert"))
    _add_effects(chain, layer)
    source_w, source_h = video_size(path) or (width, height)
    chain.add(*_crop_chain(layer, source_w, source_h))
    cropped_w, cropped_h = cropped_size(layer, source_w, source_h)
    fitted = _fit_letterbox(chain, cropped_w, cropped_h, width, height)
    _finish_chain(chain, fitted.w, fitted.h, fps, layer=layer)
    chain.dynamic_from = decode
    chain.source_size = (source_w, source_h)
    return chain


def network_chain(uri: str, width: int, height: int, fps: int, layer=None,
                  source_size: tuple[int, int] | None = None) -> SourceChain:
    chain = SourceChain()
    decode = make("uridecodebin", uri=uri)
    chain.add(decode, make("videoconvert"))
    _add_effects(chain, layer)
    # The stream's own size is unknown before negotiation.  Until a probe tells
    # us otherwise, treat the canvas as the source so crop fractions stay a
    # fraction of a normal 1080p feed rather than of the (much smaller) box.
    source_w, source_h = source_size or (width, height)
    # Bring the stream to that size first (letterboxed, square pixels), so the
    # crop below really is a fraction of the frame it cuts.  Cropping the raw
    # stream by canvas-sized pixel counts was wrong for any other size.
    chain.add(make("videoscale", n_threads=SCALE_THREADS), caps_filter(
        f"video/x-raw,format=RGBA,width={source_w},height={source_h},pixel-aspect-ratio=1/1"))
    chain.add(*_crop_chain(layer, source_w, source_h))
    cropped_w, cropped_h = cropped_size(layer, source_w, source_h)
    live = getattr(layer, "kind", "") in LIVE_KINDS
    fitted = _fit_letterbox(chain, cropped_w, cropped_h, width, height,
                            live=live, fps=fps)
    _finish_chain(chain, fitted.w, fitted.h, fps, layer=layer, pin_rate=not live)
    chain.dynamic_from = decode
    chain.pause_elements.append(decode)
    chain.source_size = (source_w, source_h)
    return chain


def screen_chain(node_id: int, width: int, height: int, fps: int, layer=None,
                 fd: int | None = None,
                 expected_size: tuple[int, int] | None = None,
                 flip: str | None = None) -> SourceChain:
    """Capture a PipeWire node exported by xdg-desktop-portal ScreenCast.

    ``fd`` is the PipeWire remote returned by OpenPipeWireRemote.  Passing it is
    what makes a portal-scoped node accessible; without it the caller must
    already have a PipeWire connection that can see the node.

    ``path`` takes the node id.  ``target-object`` expects a node name or
    serial, so passing the numeric id there negotiated a different source (the
    webcam) while reporting success.
    """
    chain = SourceChain()
    # Screen nodes are DMA-BUF on Wayland compositors (niri and KineticWE/KWin
    # included).  pipewiresrc cannot hand those to videoconvert directly ("no
    # more input formats"); upload to GL and download to system memory first.
    # Compositors that expose only system memory take the old always-copy path.
    props = {"do_timestamp": True, "path": str(int(node_id)),
             # A screencast is damage-driven: the producer only supplies a new
             # buffer when the shared surface changes.  pipewiresrc would
             # otherwise offer that stream's clock as the canvas clock, so a
             # static or throttled window made the whole preview (and every
             # other layer) advance at the share's frame rate -- the "1 fps"
             # window share.  Keep the canvas on the system clock and repeat
             # the last screen frame until fresh damage arrives.
             "provide_clock": False,
             # PipeWire defaults to a 16-buffer pool.  A screencast can run at
             # the monitor refresh (up to 120 Hz), so 16 queued frames mean the
             # preview is up to a second behind the desktop.  A small pool
             # keeps the picture live and lets the producer drop stale damage.
             "max_buffers": 3, "min_buffers": 1}
    if fd is not None and fd >= 0:
        props["fd"] = int(fd)
    src = make_optional("pipewiresrc", **props)
    chain.add(src)
    # A live screencast must never back-pressure the PipeWire stream: the
    # compositor only consumes ``fps`` frames of a stream that can run at the
    # monitor's refresh, and once the producer's buffer pool is empty the
    # capture stops for good (the preview then keeps the last frame).  One
    # slot, leaking downstream, drops stale frames and always returns the rest
    # to PipeWire, so a stalled consumer (window focus) cannot lock capture up.
    chain.add(make("queue", leaky=2, max_size_buffers=1,
                   max_size_bytes=0, max_size_time=0, silent=True))
    gl_ready = all(Gst.ElementFactory.find(name) is not None
                   for name in ("glupload", "glcolorconvert", "gldownload"))
    gl_scale = gl_ready and Gst.ElementFactory.find("glcolorscale") is not None
    # Some portal/compositor combinations report a 1x1 stream or the logical
    # size instead of the buffer size.  Building the letterbox from that
    # stretches the picture, which is how a window share ends up filling only
    # part of the box; fall back to the box itself when the size is unusable.
    if expected_size and expected_size[0] > 1 and expected_size[1] > 1:
        source_w, source_h = int(expected_size[0]), int(expected_size[1])
    else:
        source_w, source_h = int(width), int(height)
    fit = fit_contain(source_w, source_h, width, height)
    if gl_ready:
        chain.add(make("glupload"))
        if gl_scale:
            # Scale the 4K screencast on the GPU.  Downloading full 3840x2160
            # frames up to 120 times a second and scaling them on the CPU is
            # what made the shared screen lag.
            chain.add(make("glcolorscale"))
        chain.add(make("glcolorconvert"), make("gldownload"))
        if gl_scale:
            chain.add(caps_filter(
                f"video/x-raw,format=RGBA,width={max(2, fit.w)},height={max(2, fit.h)}"))
    else:
        src.set_property("always-copy", True)
    # A window share must not come out upside down.  Honour the compositor's
    # image-orientation tag when it is present; KWin/NVIDIA can drop it while
    # still rotating the buffer, so screen_flip_method() also accepts a forced
    # correction through ANYCAM_SCREEN_FLIP.
    if Gst.ElementFactory.find("videoflip") is not None:
        chain.add(make("videoflip", method=config.screen_flip_method(flip)))
    chain.add(make("videoconvert"))
    chain.add(caps_filter("video/x-raw,format=RGBA"))
    chain.check_element = src
    chain.expected_size = (source_w, source_h)
    _add_effects(chain, layer)
    # Crop against the frame the crop was computed on: the fitted size when the
    # GPU already scaled it, the native size otherwise.
    crop_base = (fit.w, fit.h) if gl_scale else (source_w, source_h)
    chain.add(*_crop_chain(layer, crop_base[0], crop_base[1]))
    cropped_w, cropped_h = cropped_size(layer, crop_base[0], crop_base[1])
    fitted = _fit_letterbox(chain, cropped_w, cropped_h, width, height, rate=False)
    # The canvas layer_tail owns the output rate.  A forced framerate here
    # makes a damage-driven DMA-BUF screencast emit one frame and then stall.
    # The same applies to videorate: a screencast advertises framerate=0/1
    # (variable), and a video-rate element fed with that can stop passing
    # frames entirely.
    _finish_chain(chain, fitted.w, fitted.h, fps, pin_rate=False, layer=layer)
    chain.pause_elements.append(src)
    chain.source_size = crop_base
    return chain


def template_chain(width: int, height: int, fps: int) -> SourceChain:
    """The branded slate shown when a layout has no sources.

    This replaces the old bare test pattern: an empty canvas now reads as an
    AnyCam surface with a hint, rather than looking like a broken feed.
    """
    width = max(2, int(width))
    height = max(2, int(height))
    chain = SourceChain()
    src = make("videotestsrc", is_live=True, pattern="black")
    # textoverlay sizes are Pango points and read much larger than the
    # pixel count suggests; keep them modest so the slate never overlaps.
    title_size = max(24, min(90, height // 24))
    hint_size = max(10, min(26, height // 90))
    chain.add(
        src,
        make("videoconvert"),
        make("videoscale"),
        caps_filter(f"video/x-raw,width={width},height={height}"),
        make("textoverlay", text="AnyCam", halignment="center",
             valignment="center",
             font_desc=f"DejaVu Sans Bold {title_size}",
             color=0xFFFFFFFF),
        make("videoconvert"),
        make("textoverlay", text="Empty layout  ·  click + to add a source",
             halignment="center", valignment="bottom",
             font_desc=f"DejaVu Sans {hint_size}", color=0xFF7D8B96),
        make("videoconvert"),
        make("videorate"),
    )
    _finish_chain(chain, width, height, fps)
    chain.pause_elements.append(src)
    chain.source_size = (width, height)
    return chain


# videotestsrc pattern names accepted from a test layer's ``source``.
TEST_PATTERNS = frozenset({
    "ball", "smpte", "snow", "black", "white", "red", "green", "blue",
    "checkers-1", "checkers-2", "checkers-4", "checkers-8", "circular",
    "blink", "smpte75", "zone-plate", "gamut", "chroma-zone-plate",
    "solid-color", "bar", "pinwheel",
})


def test_chain(width: int, height: int, fps: int, layer=None) -> SourceChain:
    chain = SourceChain()
    pattern = str(getattr(layer, "source", "") or "").strip().lower()
    if pattern not in TEST_PATTERNS:
        pattern = "ball"
    src = make("videotestsrc", is_live=True, pattern=pattern)
    chain.add(src, make("videoconvert"))
    # Fill the box before cropping: a test layer is synthetic, so its own
    # geometry is the box (this also makes crop behave like every other kind).
    chain.add(caps_filter(f"video/x-raw,format=RGBA,width={width},height={height}"))
    chain.add(make("videoconvert"))
    _add_effects(chain, layer)
    chain.add(*_crop_chain(layer, width, height))
    cropped_w, cropped_h = cropped_size(layer, width, height)
    fitted = _fit_letterbox(chain, cropped_w, cropped_h, width, height)
    _finish_chain(chain, fitted.w, fitted.h, fps, layer=layer)
    chain.pause_elements.append(src)
    chain.source_size = (width, height)
    return chain


_source_counter = itertools.count(1)


def _next_source_number() -> int:
    """A process-wide unique number for generated-source element names.

    The names used ``id(chain) & 0xFFFF``: a removed layer's element stays in
    the pipeline (linked, alpha 0), and a new chain could be allocated at a
    reused address, so two ``appsrc`` elements got the same name and the live
    add failed (forcing a full rebuild).
    """
    return next(_source_counter)


def animation_chain(preset: str, width: int, height: int, fps: int,
                    layer=None) -> SourceChain:
    """A live animated backdrop drawn procedurally (see ``anycam.procedural``).

    The generator runs at a small internal size with the box's aspect ratio,
    so scaling it up fills the box instead of leaving bands.
    """
    chain = SourceChain()
    key = procedural.generator_key(preset)
    source = procedural.ProceduralSource(
        key, f"backdrop_{key}_{_next_source_number():x}", width, height, fps)
    chain.add(source.appsrc)
    chain.cleanup.append(source.stop)
    fitted = _fit_letterbox(chain, source.width, source.height, width, height)
    _finish_chain(chain, fitted.w, fitted.h, fps, layer=layer)
    chain.source_size = (source.width, source.height)
    return chain


ANIMATED_KINDS = ("ticker", "lower_third", "gif", "drawing", "particles")


def animated_chain(layer, width: int, height: int, fps: int) -> SourceChain:
    """Ticker, lower third or animated image, drawn procedurally with alpha."""
    from . import animated

    kind = layer.kind
    width, height = max(8, int(width)), max(8, int(height))
    if kind == "ticker":
        generator = animated.Ticker(width, height, layer.source, layer.color,
                                    layer.background)
        size = (width, height)
    elif kind == "lower_third":
        name, _sep, title = str(layer.source or "").partition("|")
        generator = animated.LowerThird(width, height, name.strip(), title.strip(),
                                        layer.color, layer.background)
        size = (width, height)
    elif kind == "particles":
        from . import particles
        generator, size = particles.make(layer.source, width, height)
        controller = particles.Switcher(generator, width, height)
        generator = controller
    elif kind == "drawing":
        generator = animated.DrawingCanvas(width, height, animated.parse_strokes(layer.source))
        size = (width, height)
        fps = min(int(fps), 10)        # it only changes when a stroke is added
    else:
        generator = animated.GifPlayer(str(Path(layer.source).expanduser()))
        size = generator.size
    chain = SourceChain()
    source = procedural.ProceduralSource(
        kind, f"anim_{kind}_{_next_source_number():x}", width, height, fps,
        generator=generator, size=size)
    chain.add(source.appsrc)
    chain.cleanup.append(source.stop)
    chain.control = generator
    fitted = _fit_letterbox(chain, size[0], size[1], width, height)
    _finish_chain(chain, fitted.w, fitted.h, fps, layer=layer)
    chain.source_size = size
    return chain


def layer_tail(layer, source_size: tuple[int, int], width: int, height: int,
               fps: int, channel: str) -> SourceChain:
    """The canvas half of a captured layer, fed by ``intervideosrc``.

    The capture pipeline (`anycam.pool`) emits the source's native frame; crop,
    effects and the letterbox fit are per layer and stay here.  The rate is
    pinned: ``intervideosrc`` negotiates 30/1 by default and silently drops
    every second frame of a 60 fps capture.
    """
    chain = SourceChain()
    # Two layers can share one capture channel.  The element name must still
    # be unique inside the canvas pipeline, or the second element is refused
    # by Gst.Bin.add and the link fails with a misleading "not in a pipeline".
    tag = re.sub(r"[^A-Za-z0-9_-]", "_", getattr(layer, "id", "") or "layer")
    src = make("intervideosrc", f"src_{channel}_{tag}", channel=channel)
    rate = caps_filter(f"video/x-raw,framerate={fps}/1")
    # intervideosrc answers a latency query with a maximum below its minimum,
    # so the compositor's own query failed on every layer of this kind.  A
    # small leaky queue gives it a consistent range (and never blocks it).
    buffer = make("queue", f"q_{channel}_{tag}", leaky=2, max_size_buffers=2,
                  max_size_bytes=0, max_size_time=200_000_000, silent=True)
    chain.add(src, buffer, rate, make("videoconvert"))
    _add_effects(chain, layer)
    src_w, src_h = source_size
    chain.add(*_crop_chain(layer, src_w, src_h))
    cropped_w, cropped_h = cropped_size(layer, src_w, src_h)
    live = getattr(layer, "kind", "") in LIVE_KINDS
    fitted = _fit_letterbox(chain, cropped_w, cropped_h, width, height,
                            live=live, fps=fps)
    _finish_chain(chain, fitted.w, fitted.h, fps, layer=layer, pin_rate=not live)
    chain.source_size = (src_w, src_h)
    return chain


def background_chain(width: int, height: int, fps: int) -> SourceChain:
    """The permanent branded standby behind every scene layer.

    A live ``videotestsrc`` never EOSes, so the compositor always has a pad
    producing frames at the canvas caps.  Removing the last source therefore
    shows this slate immediately instead of leaving the previous video (or a
    black background) on the virtual camera.  GStreamer spells the YUYV raw
    format ``YUY2``; the device itself is still YUYV.
    """
    width = max(2, int(width))
    height = max(2, int(height))
    chain = SourceChain()
    src = make("videotestsrc", "background_src", is_live=True,
               pattern="black")
    tail = caps_filter(
        f"video/x-raw,format=YUY2,width={width},height={height},"
        f"framerate={fps}/1")
    chain.add(
        src,
        caps_filter(f"video/x-raw,width={width},height={height},"
                    f"framerate={fps}/1"),
        make("videoconvert"),
        make("textoverlay", "background_label", text="AnyCam",
             halignment="center", valignment="center",
             # 96 px at 1080p; scale the point size with the canvas so a
             # small preview does not become one clipped letter.
             font_desc=f"Sans {max(24, min(96, height // 11))}",
             color=0xFFFFFFFF),
        tail,
    )
    chain.tail = tail
    chain.pause_elements.append(src)
    chain.source_size = (width, height)
    return chain


def build_chain(layer, width: int, height: int, fps: int, screen_node: int | None = None,
                screen_fd: int | None = None, canvas: tuple[int, int] | None = None,
                screen_size: tuple[int, int] | None = None,
                screen_flip: str | None = None) -> SourceChain:
    kind = layer.kind
    canvas_w, canvas_h = canvas or (width, height)
    if kind == "camera":
        # The capture mode follows the canvas, not the layer box.
        capture = camera_capture_request(layer, canvas_w, canvas_h, fps)
        return camera_chain(layer.source or "/dev/video0", width, height, fps,
                            layer, capture=capture)
    if kind == "screen":
        # A GUI screen layer stores its portal node id in ``source``.  The
        # ``screen_node`` argument remains for the headless demo/CLI path.
        node = None
        if layer.source:
            try:
                node = int(layer.source)
            except ValueError:
                node = None
        node = node if node is not None else screen_node
        if node is None:
            raise RuntimeError("screen layer requires a PipeWire node id from the portal")
        return screen_chain(node, width, height, fps, layer, fd=screen_fd,
                            expected_size=screen_size, flip=screen_flip)
    if kind == "image" or kind == "logo":
        return image_chain(layer.source, width, height, fps, layer)
    if kind == "video":
        return file_video_chain(layer.source, width, height, fps, layer)
    if kind == "network":
        return network_chain(layer.source, width, height, fps, layer,
                             source_size=(canvas_w, canvas_h))
    if kind == "test":
        return test_chain(width, height, fps, layer)
    if kind == "animation":
        return animation_chain(layer.source, width, height, fps, layer)
    if kind in ANIMATED_KINDS:
        return animated_chain(layer, width, height, fps)
    if kind == "template":
        return template_chain(width, height, fps)
    raise ValueError(f"unsupported layer kind: {kind}")
