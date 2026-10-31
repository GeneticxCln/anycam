"""Text, badge and logo rendering for the compositor.

AnyCam keeps its overlays as original RGBA PNGs generated at runtime.  This
keeps the GStreamer graph simple (one imagefreeze layer per overlay) and means
the same assets are used by the headless demo, the GUI and the tests.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import config
from .geometry import crop_pixels

_FONT_CANDIDATES = (
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
)


def font_path() -> str:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return path
    return ""


def parse_color(value: str, default: tuple[int, int, int, int] = (255, 255, 255, 255)) -> tuple[int, int, int, int]:
    value = (value or "").strip().lstrip("#")
    try:
        if len(value) == 6:
            return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), 255)
        if len(value) == 8:
            return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), int(value[6:8], 16))
    except ValueError:
        pass
    return default


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    path = font_path()
    if path:
        return ImageFont.truetype(path, max(8, int(size)))
    return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, text: str, box: tuple[int, int], start: int) -> ImageFont.FreeTypeFont:
    size = max(8, int(start))
    while size > 8:
        font = _load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= box[0] and bbox[3] - bbox[1] <= box[1]:
            return font
        size = int(size * 0.9)
    return _load_font(8)


def render_text_png(
    text: str,
    width: int,
    height: int,
    font_size: int,
    color: str = "#ffffff",
    background: str = "#101418cc",
    style: str = "badge",
    out: Path | None = None,
) -> Path:
    """Render a text badge with a transparent surround."""
    width, height = max(8, int(width)), max(8, int(height))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if style == "badge":
        bg = parse_color(background, (16, 20, 24, 204))
        radius = max(4, int(min(width, height) * 0.18))
        draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=bg)
    padding_x = max(4, int(width * 0.045))
    padding_y = max(2, int(height * 0.12))
    font = _fit_font(draw, text, (width - 2 * padding_x, height - 2 * padding_y), font_size)
    bbox = draw.textbbox((0, 0), text, font=font)
    tx = (width - (bbox[2] - bbox[0])) // 2 - bbox[0]
    ty = (height - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=parse_color(color))
    if out is None:
        digest = hashlib.sha1(
            f"{text}|{width}x{height}|{font_size}|{color}|{background}|{style}".encode()
        ).hexdigest()[:16]
        out = config.cache_dir() / "overlays" / f"text-{digest}.png"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


def render_face_frame(
    color: str = "#5eead0",
    style: str = "frame",
    size: int = 320,
    out: Path | None = None,
) -> Path:
    """Render the transparent frame that follows a detected face.

    The compositor scales this RGBA image into the face's bounding box, so a
    single small asset works for every camera resolution.  ``spotlight`` adds a
    soft translucent surround; ``frame`` is the plain corner-bracket style.
    """
    size = max(48, int(size))
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rgba = parse_color(color, (94, 234, 208, 255))
    if style == "spotlight":
        draw.rectangle([0, 0, size - 1, size - 1], fill=(0, 0, 0, 70))
        draw.rounded_rectangle(
            [size * 0.08, size * 0.08, size * 0.92, size * 0.92],
            radius=size * 0.12, outline=(*rgba[:3], 150), width=max(2, size // 80),
        )
    margin = max(6, size // 24)
    width = max(3, size // 48)
    draw.rounded_rectangle(
        [margin, margin, size - margin - 1, size - margin - 1],
        radius=size // 10, outline=rgba, width=width,
    )
    # Deliberately small corner ticks make the tracking frame legible even when
    # the face box is only a few percent of the canvas.
    tick = size // 7
    for x, y, dx, dy in (
        (margin, margin, 1, 1),
        (size - margin - 1, margin, -1, 1),
        (margin, size - margin - 1, 1, -1),
        (size - margin - 1, size - margin - 1, -1, -1),
    ):
        draw.line([x, y, x + dx * tick, y], fill=rgba, width=width)
        draw.line([x, y, x, y + dy * tick], fill=rgba, width=width)
    if out is None:
        digest = hashlib.sha1(f"{style}|{color}|{size}".encode()).hexdigest()[:16]
        out = config.cache_dir() / "overlays" / f"face-{digest}.png"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


def render_camera_placeholder(
    name: str,
    device: str,
    width: int,
    height: int,
    out: Path | None = None,
    title: str = "Camera disconnected",
) -> Path:
    """Render the opaque card shown while a camera layer is disconnected.

    The compositor scales the PNG into the camera's normal layer box, so the
    slot keeps its position instead of collapsing or showing black.  The card
    is deliberately generated here (rather than being a bundled asset) so the
    layer name and device path can be included in the label.
    """
    width, height = max(8, int(width)), max(8, int(height))
    image = Image.new("RGBA", (width, height), (16, 20, 24, 255))
    draw = ImageDraw.Draw(image)
    border = max(1, min(width, height) // 140)
    draw.rectangle([0, 0, width - 1, height - 1],
                   outline=(65, 82, 92, 255), width=border)
    # A faint diagonal hatch reads as "no signal" without hiding the text.
    step = max(18, min(width, height) // 5)
    for offset in range(-height, width, step * 2):
        draw.line([(offset, 0), (offset + height, height)],
                  fill=(24, 30, 35, 255), width=1)

    icon_w = max(24, min(int(width * 0.22), int(height * 0.30)))
    icon_h = max(16, int(icon_w * 0.66))
    cx, cy = width // 2, int(height * 0.38)
    body = [cx - icon_w // 2, cy - icon_h // 2,
            cx + icon_w // 2, cy + icon_h // 2]
    stroke = max(1, icon_h // 18)
    draw.rounded_rectangle(body, radius=max(2, icon_h // 6),
                           fill=(26, 33, 39, 255), outline=(94, 234, 208, 255),
                           width=stroke)
    lens = max(3, icon_h // 4)
    draw.ellipse([cx - lens, cy - lens, cx + lens, cy + lens],
                 outline=(94, 234, 208, 255), width=stroke)
    bump_w, bump_h = max(6, icon_w // 4), max(3, icon_h // 5)
    draw.rectangle([cx - bump_w // 2, body[1] - bump_h,
                    cx + bump_w // 2, body[1] + 1], fill=(26, 33, 39, 255))

    detail = (name or "Camera").strip()
    if device:
        detail = f"{detail} · {device}" if detail else device
    max_chars = max(12, int(width / max(6.0, height * 0.055)))
    if len(detail) > max_chars:
        detail = detail[: max_chars - 1].rstrip() + "…"

    def centered(text: str, y: int, font, fill: tuple[int, int, int, int]) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        tx = (width - (bbox[2] - bbox[0])) // 2 - bbox[0]
        ty = y - (bbox[3] - bbox[1]) // 2 - bbox[1]
        draw.text((tx, ty), text, font=font, fill=fill)

    title_font = _fit_font(draw, title, (int(width * 0.84), int(height * 0.16)),
                           max(9, int(height * 0.075)))
    centered(title, int(height * 0.60), title_font, (215, 227, 234, 255))
    detail_font = _fit_font(draw, detail, (int(width * 0.84), int(height * 0.11)),
                            max(8, int(height * 0.045)))
    centered(detail, int(height * 0.75), detail_font, (148, 163, 174, 255))

    if out is None:
        digest = hashlib.sha1(
            f"camera|{title}|{name}|{device}|{width}x{height}".encode()
        ).hexdigest()[:16]
        out = config.cache_dir() / "overlays" / f"camera-placeholder-{digest}.png"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    return out


def render_media_error_card(
    name: str,
    source: str,
    width: int,
    height: int,
    out: Path | None = None,
) -> Path:
    """Opaque card for an image/video/logo/face asset that is missing."""
    return render_camera_placeholder(
        name, source, width, height, out=out, title="Media unavailable"
    )


def _fingerprint(source: Path) -> str:
    """Content identity for the overlay cache.

    Using only the file stem and box size made two different photos called
    ``photo.png`` render the same cached picture.
    """
    try:
        stat = source.stat()
        seed = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        seed = str(source)
    return hashlib.sha1(seed.encode()).hexdigest()[:16]


def fit_rgba(source: Path, width: int, height: int, out: Path | None = None) -> Path:
    """Fit an image/logo into a layer box, preserving aspect, transparent fill."""
    width, height = max(8, int(width)), max(8, int(height))
    image = Image.open(source).convert("RGBA")
    scale = min(width / image.width, height / image.height)
    resized = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2), resized)
    if out is None:
        out = (config.cache_dir() / "overlays"
               / f"img-{source.stem}-{_fingerprint(source)}-{width}x{height}.png")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out


def prepare_rgba(source: Path, width: int, height: int,
                 crop: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
                 out: Path | None = None) -> Path:
    """EXIF-correct, crop and fit a photo/logo into a layer box.

    Cropping before fitting means the normalised crop is a fraction of the
    image the user picked, not of the (smaller) layer box.
    """
    width, height = max(8, int(width)), max(8, int(height))
    with Image.open(source) as opened:
        # Phone photos carry their rotation in EXIF; without this a portrait
        # shot lands sideways in the composition.
        image = ImageOps.exif_transpose(opened).convert("RGBA")
    left, top, right, bottom = crop_pixels(image.width, image.height, crop)
    image = image.crop((left, top,
                        max(left + 1, image.width - right),
                        max(top + 1, image.height - bottom)))
    scale = min(width / image.width, height / image.height)
    resized = image.resize((max(1, int(image.width * scale)),
                           max(1, int(image.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.paste(resized, ((width - resized.width) // 2,
                           (height - resized.height) // 2), resized)
    if out is None:
        crop_key = "-".join(f"{value:.4f}" for value in crop)
        out = (config.cache_dir() / "overlays"
               / f"img-{source.stem}-{_fingerprint(source)}-{width}x{height}-{crop_key}.png")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return out


def image_size(layer) -> tuple[int, int] | None:
    """Natural, EXIF-corrected pixel size of an image/logo layer's source.

    The crop editor needs this to draw its window over the visible picture
    rather than over the layer box (which may include letterbox bands).
    """
    if getattr(layer, "kind", "") not in ("logo", "image"):
        return None
    source = Path(getattr(layer, "source", "") or "").expanduser()
    if not source.exists():
        return None
    try:
        with Image.open(source) as opened:
            width, height = ImageOps.exif_transpose(opened).size
        return (int(width), int(height))
    except (OSError, ValueError):
        return None


def layer_pixels(layer, canvas_w: int, canvas_h: int) -> tuple[int, int, int, int]:
    from .geometry import clamp

    x, y, w, h = layer.box
    return (
        int(round(clamp(x) * canvas_w)),
        int(round(clamp(y) * canvas_h)),
        max(8, int(round(clamp(w, 0.01, 1.0) * canvas_w))),
        max(8, int(round(clamp(h, 0.01, 1.0) * canvas_h))),
    )


def render_layer(layer, canvas_w: int, canvas_h: int,
                 crop: tuple[float, float, float, float] | None = None) -> Path:
    """Return an RGBA PNG for a text or logo layer.

    ``crop`` overrides the layer's own insets; the crop editor passes zeros so
    the preview can show the uncropped picture while the model keeps the crop.
    """
    x, y, w, h = layer_pixels(layer, canvas_w, canvas_h)
    if layer.kind == "text":
        return render_text_png(
            layer.source or layer.name,
            w, h,
            font_size=max(8, int(layer.font_size * canvas_h)),
            color=layer.color,
            background=layer.background,
            style=layer.style,
        )
    if layer.kind in ("logo", "image"):
        source = Path(layer.source).expanduser()
        if not source.exists():
            raise FileNotFoundError(f"overlay asset not found: {source}")
        source_crop = getattr(layer, "crop", (0.0, 0.0, 0.0, 0.0)) if crop is None else crop
        return prepare_rgba(source, w, h, source_crop)
    raise ValueError(f"layer {layer.kind} is not an overlay")


def render_scene(scene, canvas_w: int, canvas_h: int) -> dict[str, Path]:
    """Render every static overlay layer of a scene, keyed by layer id."""
    return {
        layer.id: render_layer(layer, canvas_w, canvas_h)
        for layer in scene.ordered()
        if layer.kind in ("text", "logo", "image")
    }


def timestamped_name(prefix: str = "anycam", suffix: str = ".png") -> str:
    from datetime import datetime

    # Milliseconds avoid collisions when a graph rebuild starts a new segment
    # in the same second as the previous one.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return f"{prefix}-{stamp}{suffix}"
