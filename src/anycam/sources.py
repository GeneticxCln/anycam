"""Source chains for the AnyCam compositor.

Every source produces a normalised RGBA video stream at the layer box size and
frame rate.  Aspect ratio is preserved: the picture is scaled to fit inside the
box and the remaining bands are letterboxed, so a 16:9 camera in a portrait
box shows the whole frame instead of a squeezed one.  A small amount of probing
is used for cameras so the pipeline does not fail when a device does not
advertise the exact MJPG mode we prefer.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .geometry import crop_pixels, fit_contain


def make(factory: str, name: str | None = None, **props) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise RuntimeError(f"GStreamer element '{factory}' is not installed")
    for key, value in props.items():
        element.set_property(key.replace("_", "-"), value)
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
    pool = _closest_size(modes, width, height) or modes
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

    nvjpegdec is installed here but rejects the StreamCam's MJPG frames
    ("No valid frames decoded before end of stream"), so software jpegdec is
    the safe default.  Set ANYCAM_JPEG_DECODER=nv to opt in on other cameras.
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


def _effects_chain(layer) -> list[Gst.Element]:
    """Optional colour / blur / chroma-key elements for a camera-like source."""
    fx = getattr(layer, "effects", None)
    if fx is None:
        return []
    elements: list[Gst.Element] = []
    if fx.chroma_enabled:
        alpha = make("alpha", method="custom", prefer_passthrough=True)
        red, green, blue = parse_chroma_color(fx.chroma_key or "#00ff00")
        alpha.set_property("target-r", red)
        alpha.set_property("target-g", green)
        alpha.set_property("target-b", blue)
        # ``noise-level`` is 0..64 (float), not a byte; multiplying by 255 made
        # anything above 25% clamp to the maximum and key out the picture.
        similarity = max(0.0, min(1.0, float(fx.chroma_similarity)))
        smoothness = max(0.0, min(1.0, float(fx.chroma_smoothness)))
        alpha.set_property("noise-level", similarity)
        alpha.set_property("black-sensitivity", max(1, int(round(smoothness * 128))))
        alpha.set_property("white-sensitivity", max(1, int(round(smoothness * 128))))
        elements.append(alpha)
    if fx.brightness or fx.contrast != 1.0 or fx.saturation != 1.0:
        balance = make("videobalance")
        balance.set_property("brightness", max(-1.0, min(1.0, fx.brightness)))
        balance.set_property("contrast", max(0.0, min(3.0, fx.contrast)))
        balance.set_property("saturation", max(0.0, min(3.0, fx.saturation)))
        elements.append(balance)
    if fx.blur and fx.blur > 0.2:
        blur = make("gaussianblur")
        blur.set_property("sigma", float(fx.blur))
        elements.append(blur)
    return elements


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


def _fit_letterbox(chain: SourceChain, src_w: int, src_h: int,
                   width: int, height: int, rate: bool = True) -> None:
    """Scale a source into the box preserving aspect and pad with black bands.

    The compositor pad stays exactly the layer box, so the drag/resize handles
    and the picture now describe the same rectangle.  ``videobox`` uses
    negative insets to *add* borders.
    """
    rect = fit_contain(src_w, src_h, width, height)
    chain.add(make("videoscale"))
    if (rect.w, rect.h) != (int(width), int(height)):
        chain.add(caps_filter(
            f"video/x-raw,format=RGBA,width={rect.w},height={rect.h}"))
        box = make("videobox", fill="black")
        box.set_property("left", -rect.x)
        box.set_property("top", -rect.y)
        box.set_property("right", -(int(width) - rect.w - rect.x))
        box.set_property("bottom", -(int(height) - rect.h - rect.y))
        chain.add(box)
    if rate:
        chain.add(make("videorate"))


def _finish_chain(chain: SourceChain, width: int, height: int, fps: int) -> None:
    tail = caps_filter(caps_string(width, height, fps))
    chain.add(tail)
    chain.tail = tail


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
    effects = _effects_chain(layer)
    if effects:
        chain.add(*effects, make("videoconvert"))
    # Crop percentages apply to the camera frame, not the compositor box.
    chain.add(*_crop_chain(layer, mode.width, mode.height))
    cropped_w, cropped_h = cropped_size(layer, mode.width, mode.height)
    _fit_letterbox(chain, cropped_w, cropped_h, width, height)
    _finish_chain(chain, width, height, fps)
    chain.camera_mode = mode
    chain.capture_request = (cap_w, cap_h, cap_fps)
    chain.camera_probe_error = camera_probe_error(device)
    chain.source_size = (mode.width, mode.height)
    return chain



def _crop_chain(layer, width: int, height: int) -> list[Gst.Element]:
    """Optional videocrop from a normalised (left, top, right, bottom) crop."""
    crop = getattr(layer, "crop", None)
    if not crop or not any(v != 0.0 for v in crop):
        return []
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
    chain.add(decoder, make("videoconvert"))
    chain.add(*_crop_chain(layer, width, height))
    chain.add(make("imagefreeze"))
    # Overlay images are pre-rendered at box size with their transparency
    # already baked in (fit=False stretches them to the pad, which is what the
    # rendered PNG expects).  A raw image file is fitted with black bars.
    if fit:
        cropped_w, cropped_h = cropped_size(layer, width, height)
        _fit_letterbox(chain, cropped_w, cropped_h, width, height, rate=False)
    effects = _effects_chain(layer)
    if effects:
        chain.add(*effects, make("videoconvert"))
    _finish_chain(chain, width, height, fps)
    chain.source_size = (width, height)
    return chain


def file_video_chain(path: str, width: int, height: int, fps: int, layer=None) -> SourceChain:
    chain = SourceChain()
    src = make("filesrc", location=str(Path(path).expanduser()))
    chain.add(src)
    chain.pause_elements.append(src)
    decode = make("decodebin")
    chain.add(decode, make("videoconvert"))
    effects = _effects_chain(layer)
    if effects:
        chain.add(*effects, make("videoconvert"))
    source_w, source_h = video_size(path) or (width, height)
    chain.add(*_crop_chain(layer, source_w, source_h))
    cropped_w, cropped_h = cropped_size(layer, source_w, source_h)
    _fit_letterbox(chain, cropped_w, cropped_h, width, height)
    _finish_chain(chain, width, height, fps)
    chain.dynamic_from = decode
    chain.source_size = (source_w, source_h)
    return chain


def network_chain(uri: str, width: int, height: int, fps: int, layer=None,
                  source_size: tuple[int, int] | None = None) -> SourceChain:
    chain = SourceChain()
    decode = make("uridecodebin", uri=uri)
    chain.add(decode, make("videoconvert"))
    effects = _effects_chain(layer)
    if effects:
        chain.add(*effects, make("videoconvert"))
    # The stream's own size is unknown before negotiation.  Until a probe tells
    # us otherwise, treat the canvas as the source so crop fractions stay a
    # fraction of a normal 1080p feed rather than of the (much smaller) box.
    source_w, source_h = source_size or (width, height)
    chain.add(*_crop_chain(layer, source_w, source_h))
    cropped_w, cropped_h = cropped_size(layer, source_w, source_h)
    _fit_letterbox(chain, cropped_w, cropped_h, width, height)
    _finish_chain(chain, width, height, fps)
    chain.dynamic_from = decode
    chain.pause_elements.append(decode)
    chain.source_size = (source_w, source_h)
    return chain


def screen_chain(node_id: int, width: int, height: int, fps: int, layer=None,
                 fd: int | None = None,
                 expected_size: tuple[int, int] | None = None) -> SourceChain:
    """Capture a PipeWire node exported by xdg-desktop-portal ScreenCast.

    ``fd`` is the PipeWire remote returned by OpenPipeWireRemote.  Passing it is
    what makes a portal-scoped node accessible; without it the caller must
    already have a PipeWire connection that can see the node.

    ``path`` takes the node id.  ``target-object`` expects a node name or
    serial, so passing the numeric id there negotiated a different source (the
    webcam) while reporting success.
    """
    chain = SourceChain()
    props = {"do_timestamp": True, "path": str(int(node_id))}
    if fd is not None and fd >= 0:
        props["fd"] = int(fd)
    src = make("pipewiresrc", **props)
    chain.add(src, make("videoconvert"))
    chain.check_element = src
    chain.expected_size = tuple(expected_size) if expected_size else None
    effects = _effects_chain(layer)
    if effects:
        chain.add(*effects, make("videoconvert"))
    # Crop against the stream size the portal reported, not the layer box.
    source_w, source_h = expected_size or (width, height)
    chain.add(*_crop_chain(layer, source_w, source_h))
    cropped_w, cropped_h = cropped_size(layer, source_w, source_h)
    _fit_letterbox(chain, cropped_w, cropped_h, width, height)
    _finish_chain(chain, width, height, fps)
    chain.pause_elements.append(src)
    chain.source_size = (source_w, source_h)
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
    effects = _effects_chain(layer)
    if effects:
        chain.add(*effects, make("videoconvert"))
    chain.add(*_crop_chain(layer, width, height))
    cropped_w, cropped_h = cropped_size(layer, width, height)
    _fit_letterbox(chain, cropped_w, cropped_h, width, height)
    _finish_chain(chain, width, height, fps)
    chain.pause_elements.append(src)
    chain.source_size = (width, height)
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
    src = make("intervideosrc", f"src_{channel}", channel=channel)
    rate = caps_filter(f"video/x-raw,framerate={fps}/1")
    chain.add(src, rate, make("videoconvert"))
    effects = _effects_chain(layer)
    if effects:
        chain.add(*effects, make("videoconvert"))
    src_w, src_h = source_size
    chain.add(*_crop_chain(layer, src_w, src_h))
    cropped_w, cropped_h = cropped_size(layer, src_w, src_h)
    _fit_letterbox(chain, cropped_w, cropped_h, width, height)
    _finish_chain(chain, width, height, fps)
    chain.source_size = (src_w, src_h)
    return chain


def build_chain(layer, width: int, height: int, fps: int, screen_node: int | None = None,
                screen_fd: int | None = None, canvas: tuple[int, int] | None = None,
                screen_size: tuple[int, int] | None = None) -> SourceChain:
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
                            expected_size=screen_size)
    if kind == "image" or kind == "logo":
        return image_chain(layer.source, width, height, fps, layer)
    if kind == "video":
        return file_video_chain(layer.source, width, height, fps, layer)
    if kind == "network":
        return network_chain(layer.source, width, height, fps, layer,
                             source_size=(canvas_w, canvas_h))
    if kind == "test":
        return test_chain(width, height, fps, layer)
    raise ValueError(f"unsupported layer kind: {kind}")
