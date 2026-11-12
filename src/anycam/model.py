"""Project / scene / layer data model for AnyCam.

The model is deliberately plain data so it can be serialised to JSON, edited by
the GUI, and consumed by the GStreamer builder without circular imports.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import geometry


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# A project file is user data that may be hand-edited, truncated by an older
# build, or written by a newer one.  Loading must keep what is usable and say
# what was dropped, instead of one odd value refusing the whole project.
def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _coerce(value: Any, default: Any) -> Any:
    """``value`` converted to the type of ``default``, or ``default``."""
    kind = type(default)
    try:
        if kind is bool:
            if isinstance(value, str):
                text = value.strip().lower()
                if text in ("true", "1", "yes", "on"):
                    return True
                if text in ("false", "0", "no", "off", ""):
                    return False
                return default
            if isinstance(value, (bool, int, float)):
                return bool(value)
            return default
        if kind is int:
            if isinstance(value, bool):
                return default
            number = float(value)
            return int(number) if _finite(number) else default
        if kind is float:
            if isinstance(value, bool):
                return default
            number = float(value)
            return number if _finite(number) else default
        if kind is str:
            return value if isinstance(value, str) else default
    except (TypeError, ValueError, OverflowError):
        return default
    return value if value is not None else default


def _ident(value: Any, prefix: str) -> str:
    """A usable id: a non-empty string or integer, else a fresh one.

    A hand-edited file can hold a list or object as an id; those are
    unhashable and made the whole project refuse to load.
    """
    if isinstance(value, bool):
        return new_id(prefix)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return new_id(prefix)


def _quad(value: Any, default: tuple[float, float, float, float]
          ) -> tuple[float, float, float, float]:
    """Four finite numbers (a box or a crop), or ``default``."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return default
    try:
        numbers = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return default
    return numbers if all(_finite(item) for item in numbers) else default  # type: ignore[return-value]


def _coerce_fields(cls, data: Any, skip: tuple[str, ...] = ()):
    """Build a settings dataclass from ``data``, defaulting any bad field."""
    instance = cls()
    if not isinstance(data, dict):
        return instance
    for item in fields(cls):
        if item.name in skip or item.name not in data:
            continue
        setattr(instance, item.name,
                _coerce(data[item.name], getattr(instance, item.name)))
    return instance


@dataclass
class Effects:
    brightness: float = 0.0          # -1 .. 1
    contrast: float = 1.0            # 0 .. 2 (GStreamer videobalance limit)
    saturation: float = 1.0          # 0 .. 2
    hue: float = 0.0                 # -1 .. 1 (full hue rotation)
    chroma_enabled: bool = False
    chroma_key: str = "#00ff00"
    chroma_similarity: float = 0.35
    chroma_smoothness: float = 0.10
    # Fast look-up-table style.  Kept separate from the colour sliders so a
    # preset never has to rebuild the layer graph.
    color_effect: str = "none"     # none|sepia|heat|xray|xpro|yellowblue
    mirror: bool = False             # horizontal flip, live
    # RGB levels: a gain per colour channel (1 = unchanged), for white-balance
    # and tint corrections that brightness/contrast cannot make.
    red_gain: float = 1.0
    green_gain: float = 1.0
    blue_gain: float = 1.0
    # One live filter from anycam.effects (distortion, retro TV, cleanup) and
    # its 0..1 strength.  Swapped in while running, so it never rebuilds.
    filter: str = "none"
    filter_amount: float = 0.5
    # A second, independent slot, so two filters stack (e.g. a portrait effect
    # and a distortion).  Filter 2 runs after filter 1.
    filter2: str = "none"
    filter2_amount: float = 0.5
    # Frame drawn over this layer's box.  Rendered to a transparent PNG and
    # overlaid in the source chain, so changing it does not rebuild the canvas.
    frame_style: str = "none"       # see overlay.WINDOW_FRAME_STYLES
    frame_color: str = "#5eead0"
    frame_width: int = 6
    # Fill outside shaped windows (circle, heart ...), vignette and TV bezel.
    frame_matte: str = "#0b0d10"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Effects":
        return _coerce_fields(cls, data)


@dataclass
class PlaylistItem:
    source: str
    duration: float = 4.0
    kind: str = "image"  # image | video


@dataclass
class PlaylistConfig:
    """The scene's rotating playlist state.

    This is deliberately separate from the reserved ``Layer.playlist`` field:
    the GUI currently rotates whole full-frame layers and needs to remember
    which layer ids belong to the rotation and where it stopped.
    """

    layer_ids: list[str] = field(default_factory=list)
    interval: float = 3.0
    loop: bool = True
    index: int = -1
    # Seconds an item stays when it differs from ``interval`` (layer id -> s).
    durations: dict[str, float] = field(default_factory=dict)
    shuffle: bool = False
    paused: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PlaylistConfig":
        data = data if isinstance(data, dict) else {}
        ids = data.get("layer_ids", [])
        if not isinstance(ids, list):
            ids = []
        interval = _coerce(data.get("interval"), 3.0)
        raw_durations = data.get("durations")
        return cls(
            layer_ids=[str(value) for value in ids if value],
            interval=interval if interval > 0 else 3.0,
            loop=_coerce(data.get("loop"), True),
            index=_coerce(data.get("index"), -1),
            durations={str(key): float(value)
                       for key, value in (raw_durations if isinstance(raw_durations, dict)
                                          else {}).items()
                       if isinstance(value, (int, float)) and not isinstance(value, bool)
                       and _finite(float(value)) and value > 0},
            shuffle=_coerce(data.get("shuffle"), False),
            paused=_coerce(data.get("paused"), False),
        )


@dataclass
class EditorConfig:
    """Per-project editing preferences that should survive a restart."""

    snap_enabled: bool = True
    snap_px: float = 6.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EditorConfig":
        return _coerce_fields(cls, data)


@dataclass
class MotionConfig:
    """Act when the canvas picture moves (see anycam.motion)."""

    enabled: bool = False
    sensitivity: float = 0.5         # 0..1
    hold_s: float = 1.0              # motion must last this long
    cooldown_s: float = 30.0         # then stay quiet for this long
    scene_id: str = ""               # switch to this scene ("" = do not switch)
    notify: bool = True              # a desktop notification

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MotionConfig":
        motion = _coerce_fields(cls, data)
        motion.sensitivity = max(0.0, min(1.0, motion.sensitivity))
        motion.hold_s = max(0.0, min(60.0, motion.hold_s))
        motion.cooldown_s = max(0.0, min(3600.0, motion.cooldown_s))
        return motion


@dataclass
class Layer:
    id: str
    kind: str                        # camera|screen|image|video|network|text|logo|playlist
    name: str
    source: str = ""                 # device, path, uri or text
    box: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    crop: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    alpha: float = 1.0
    z: int = 0
    visible: bool = True
    effects: Effects = field(default_factory=Effects)
    # overlay/text styling
    font_size: float = 0.05          # normalised to canvas height
    color: str = "#ffffff"
    background: str = "#00000000"    # #RRGGBBAA badge colour
    style: str = "badge"             # badge | plain
    # Media fields reserved for per-layer playlists and volume (the GUI uses
    # separate layers and the engine mixes audio globally).  Kept for project
    # compatibility; not consumed by the compositor yet.
    playlist: list[PlaylistItem] = field(default_factory=list)
    volume: float = 1.0
    muted: bool = False
    # Optional per-layer V4L2 capture request.  0 means "follow the layer box
    # and canvas rate"; non-zero values select the camera mode independently
    # of the compositor output size.
    camera_width: int = 0
    camera_height: int = 0
    camera_fps: int = 0
    # Reserved scene-decoration marker.  "backdrop" layers sit behind every
    # capture layer and are replaced (not stacked) by the Looks tab.
    role: str = ""
    # A locked layer cannot be moved, resized, cropped or deleted by accident
    # (it still plays, and every property can be changed from its panels).
    locked: bool = False
    # V4L2 control values to re-apply to a camera layer's device on start and
    # after a reconnect (brightness, zoom, focus ... - see camera_controls).
    camera_controls: dict[str, int] = field(default_factory=dict)
    # How the picture sits in its box: contain (letterbox), cover (fill and cut
    # the overflow) or stretch (fill, ignore the aspect).  Flips apply first,
    # then the clockwise quarter-turn ``rotation``; ``zoom`` > 1 magnifies about
    # ``pan`` (0..1 across the picture).  All of them are plain video stages.
    fit: str = "contain"
    flip_h: bool = False
    flip_v: bool = False
    rotation: int = 0
    zoom: float = 1.0
    pan: tuple[float, float] = (0.5, 0.5)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Layer":
        """Read one layer, defaulting any field that is missing or malformed.

        Only a layer that cannot be identified at all (no ``kind``) is refused;
        the caller drops it and reports why.
        """
        if not isinstance(data, dict):
            raise ValueError("layer entry is not an object")
        kind = data.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("layer has no kind")
        layer_id = _ident(data.get("id"), "layer")
        name = data.get("name")
        layer = cls(id=layer_id, kind=kind.strip(),
                    name=name if isinstance(name, str) and name else kind.strip())
        special = ("id", "kind", "name", "box", "crop", "effects", "playlist",
                   "camera_controls", "pan")
        for item in fields(cls):
            if item.name in special or item.name not in data:
                continue
            setattr(layer, item.name,
                    _coerce(data[item.name], getattr(layer, item.name)))
        layer.box = _quad(data.get("box"), layer.box)
        layer.crop = _quad(data.get("crop"), layer.crop)
        layer.effects = Effects.from_dict(data.get("effects"))
        layer.fit = geometry.fit_mode(layer.fit)
        layer.rotation = geometry.rotation_degrees(layer.rotation)
        layer.zoom = geometry.clamp(layer.zoom, 1.0, geometry.MAX_ZOOM)
        pan = data.get("pan")
        if isinstance(pan, (list, tuple)) and len(pan) == 2:
            try:
                numbers = (float(pan[0]), float(pan[1]))
            except (TypeError, ValueError):
                numbers = None
            if numbers and all(_finite(value) for value in numbers):
                layer.pan = (geometry.clamp(numbers[0]), geometry.clamp(numbers[1]))
        controls = data.get("camera_controls")
        if isinstance(controls, dict):
            layer.camera_controls = {
                str(key): int(value) for key, value in controls.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                and _finite(float(value))}
        items = data.get("playlist")
        for entry in items if isinstance(items, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("source"), str):
                layer.playlist.append(PlaylistItem(
                    source=entry["source"],
                    duration=_coerce(entry.get("duration"), 4.0),
                    kind=_coerce(entry.get("kind"), "image")))
        if layer.kind == "network" and "@" in layer.name:
            # Older builds named a network layer after the URL's host part,
            # camera password included.
            layer.name = layer.name.rpartition("@")[2]
        return layer

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["box"] = list(self.box)
        d["crop"] = list(self.crop)
        d["pan"] = list(self.pan)
        return d


@dataclass
class Scene:
    id: str
    name: str
    layers: list[Layer] = field(default_factory=list)
    # How this scene arrives when it is switched to (see engine.transitions).
    transition: str = "cut"
    transition_ms: int = 500
    playlist: PlaylistConfig = field(default_factory=PlaylistConfig)
    # Layout template that last arranged this preset; "" means the boxes
    # were placed by hand.  Stored so switching presets restores the picker.
    layout: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any],
                  warnings: list[str] | None = None) -> "Scene":
        if not isinstance(data, dict):
            raise ValueError("scene entry is not an object")
        name = _coerce(data.get("name"), "Scene") or "Scene"
        layers: list[Layer] = []
        seen: set[str] = set()
        raw = data.get("layers")
        for index, entry in enumerate(raw if isinstance(raw, list) else []):
            try:
                layer = Layer.from_dict(entry)
            except (ValueError, TypeError) as exc:
                if warnings is not None:
                    warnings.append(
                        f"scene \"{name}\": skipped layer {index + 1} ({exc})")
                continue
            if layer.id in seen:
                # Duplicate ids make every lookup by id pick the wrong layer.
                layer.id = new_id("layer")
                if warnings is not None:
                    warnings.append(
                        f"scene \"{name}\": layer {index + 1} repeated an id; "
                        "it was given a new one")
            seen.add(layer.id)
            layers.append(layer)
        return cls(
            id=_ident(data.get("id"), "scene"),
            name=name,
            layers=layers,
            transition=_coerce(data.get("transition"), "cut"),
            transition_ms=max(100, min(5000, _coerce(data.get("transition_ms"), 500))),
            playlist=PlaylistConfig.from_dict(data.get("playlist")),
            layout=_coerce(data.get("layout"), ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name,
                "layers": [l.to_dict() for l in self.layers],
                "transition": self.transition,
                "transition_ms": self.transition_ms,
                "playlist": asdict(self.playlist),
                "layout": self.layout}

    def add_layer(self, layer: Layer) -> Layer:
        # An explicit positive z wins; 0 means "put it on top".
        if layer.z <= 0:
            layer.z = max([l.z for l in self.layers], default=0) + 1
        self.layers.append(layer)
        return layer

    def layer(self, layer_id: str) -> Layer | None:
        return next((l for l in self.layers if l.id == layer_id), None)

    def remove_layer(self, layer_id: str) -> None:
        self.layers = [l for l in self.layers if l.id != layer_id]

    def ordered(self) -> list[Layer]:
        return sorted((l for l in self.layers if l.visible), key=lambda l: l.z)


@dataclass
class AudioConfig:
    mic_device: str = ""
    mic_gain_db: float = 0.0
    mic_muted: bool = False
    # Off unless the user asks for the microphone.  A media bed used to drag
    # the microphone into the graph even when the box was unchecked.
    mic_enabled: bool = False
    # Voice processing on the microphone (webrtcdsp): stationary-noise
    # suppression ("off", "low", "moderate", "high", "very-high") and
    # automatic gain.  Measured on pink noise: -6/-12/-18/-21 dB.
    mic_denoise: str = "moderate"
    mic_auto_gain: bool = False
    media_file: str = ""
    media_loop: bool = True      # a single media file restarts; a playlist wraps round
    media_gain_db: float = 0.0
    media_muted: bool = False
    # Extra audio files played after ``media_file`` in order (or shuffled).
    media_playlist: list[str] = field(default_factory=list)
    media_shuffle: bool = False
    # Gate/compressor ("off", "gate", "compressor", "broadcast") and equaliser
    # ("flat", "voice", "warm", "bright", "bass-cut") on the microphone.
    mic_dynamics: str = "off"
    mic_eq: str = "flat"
    # Per-source sync offsets: audio held back by this many milliseconds, so a
    # microphone that runs ahead of a slow capture card lines up with it.
    mic_delay_ms: int = 0
    desktop_delay_ms: int = 0
    media_delay_ms: int = 0
    # Audio output: a monitor so the streamer hears the mix, and a virtual
    # microphone (a PipeWire source node other apps can record from).
    monitor_device: str = ""
    virtual_mic: bool = False
    # Desktop audio: a PipeWire/PulseAudio monitor source ("what you hear").
    desktop_device: str = ""
    desktop_gain_db: float = 0.0
    desktop_muted: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AudioConfig":
        audio = _coerce_fields(cls, data, skip=("media_playlist",))
        raw = data.get("media_playlist") if isinstance(data, dict) else None
        audio.media_playlist = [item for item in raw if isinstance(item, str) and item] \
            if isinstance(raw, list) else []
        audio.mic_dynamics = audio.mic_dynamics.strip().lower() \
            if audio.mic_dynamics.strip().lower() in ("off", "gate", "compressor", "broadcast") \
            else "off"
        audio.mic_eq = audio.mic_eq.strip().lower() \
            if audio.mic_eq.strip().lower() in ("flat", "voice", "warm", "bright", "bass-cut") \
            else "flat"
        for name in ("mic_delay_ms", "desktop_delay_ms", "media_delay_ms"):
            setattr(audio, name, max(0, min(2000, int(getattr(audio, name)))))
        return audio


@dataclass
class OutputConfig:
    canvas_w: int = 1920
    canvas_h: int = 1080
    fps: int = 60
    vcam_device: str = "/dev/video10"
    # Where the virtual camera is published: "v4l2" (the loopback device,
    # which every app can open), "pipewire" (a PipeWire camera node: no
    # driver or root, but only apps with PipeWire camera support list it) or
    # "both".
    vcam_backend: str = "v4l2"
    # Whether the held virtual camera follows the canvas when the app starts.
    # The Virtual camera button writes it, so "off" survives a restart.
    vcam_enabled: bool = True
    # Portal screen captures: automatic follows the compositor's orientation
    # tag.  rotate-180 / vertical-flip / none are explicit corrections for a
    # share that is wrong.
    screen_flip: str = "automatic"
    # Empty means "follow the XDG video directory".  The field used to be
    # ignored: recordings always went to ~/Videos/AnyCam regardless.
    record_dir: str = ""
    rtmp_url: str = ""
    # Video bitrate in kbit/s for recordings and for the stream; 0 follows the
    # codec and canvas size.  The disk guard stops a recording when the folder
    # has less than ``record_min_free_mb`` MiB left (0 turns the guard off).
    record_bitrate_kbps: int = 0
    stream_bitrate_kbps: int = 0
    record_min_free_mb: int = 500
    # Let a paired phone switch scenes, mute and record from its browser (/remote).
    phone_control: bool = False
    # More destinations that receive the same encode as ``rtmp_url``.
    rtmp_extra_urls: list[str] = field(default_factory=list)
    # H.264 encoder for recordings: auto prefers NVENC when a working NVIDIA
    # driver/plugin is present, nvenc forces it, and cpu forces x264.
    encoder: str = "auto"
    # Where the AI effects' ONNX models run: "cpu" (default) or "gpu" (CUDA,
    # only when onnxruntime has the CUDA provider).  ANYCAM_EFFECTS_DEVICE
    # overrides it.
    effects_device: str = "cpu"
    # Run the AI effects (and effect packs) in a separate process, so a crash
    # in a model never reaches the canvas, the recording or the stream.
    effects_isolated: bool = True
    # Person cut-out model: "mediapipe" (bundled) or "rvm" (Robust Video
    # Matting, used once downloaded from Settings).
    segmentation_model: str = "mediapipe"
    # Where layers are mixed: "auto" (GPU glvideomixer when a test frame
    # passes, else CPU) or "cpu" (the compositor element).  ANYCAM_CANVAS
    # overrides it.
    canvas_backend: str = "auto"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "OutputConfig":
        output = _coerce_fields(cls, data, skip=("rtmp_extra_urls",))
        raw = data.get("rtmp_extra_urls") if isinstance(data, dict) else None
        output.rtmp_extra_urls = ([url.strip() for url in raw if isinstance(url, str) and url.strip()]
                                  if isinstance(raw, list) else [])
        defaults = cls()
        for name in ("record_bitrate_kbps", "stream_bitrate_kbps"):
            value = getattr(output, name)
            setattr(output, name, 0 if value <= 0 else max(500, min(200_000, value)))
        output.record_min_free_mb = max(0, min(1_000_000, output.record_min_free_mb))
        # A canvas or rate the encoder cannot use would only fail later.
        if output.canvas_w < 2 or output.canvas_h < 2:
            output.canvas_w, output.canvas_h = defaults.canvas_w, defaults.canvas_h
        if output.fps < 1:
            output.fps = defaults.fps
        if output.vcam_backend not in ("v4l2", "pipewire", "both"):
            output.vcam_backend = defaults.vcam_backend
        if output.canvas_backend not in ("auto", "cpu"):
            output.canvas_backend = defaults.canvas_backend
        return output


# Largest canvas and rate the window offers; a hand-edited file may hold more.
MAX_CANVAS = (7680, 4320)
MAX_FPS = 240


def normalize_output(output: "OutputConfig", warnings: list[str] | None = None) -> None:
    """Make the canvas something the encoder can run, saying what changed.

    H.264 4:2:0 needs even sizes (the engine refuses odd ones on every start,
    and the project is saved, so it would refuse on every launch), and an
    absurd size or rate would only exhaust memory.
    """
    width = min(max(2, output.canvas_w), MAX_CANVAS[0]) // 2 * 2
    height = min(max(2, output.canvas_h), MAX_CANVAS[1]) // 2 * 2
    fps = min(max(1, output.fps), MAX_FPS)
    if (width, height, fps) != (output.canvas_w, output.canvas_h, output.fps):
        if warnings is not None:
            warnings.append(
                f"canvas {output.canvas_w}x{output.canvas_h}@{output.fps} "
                f"changed to {width}x{height}@{fps}")
        output.canvas_w, output.canvas_h, output.fps = width, height, fps


@dataclass
class Project:
    name: str = "AnyCam project"
    output: OutputConfig = field(default_factory=OutputConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    editor: EditorConfig = field(default_factory=EditorConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    scenes: list[Scene] = field(default_factory=list)
    active_scene: str = ""
    # What loading had to drop or repair; never written back to the file.
    load_warnings: list[str] = field(default_factory=list, repr=False,
                                     compare=False)

    @property
    def scene(self) -> Scene:
        for s in self.scenes:
            if s.id == self.active_scene:
                return s
        if not self.scenes:
            self.scenes.append(Scene(new_id("scene"), "Scene 1"))
        self.active_scene = self.scenes[0].id
        return self.scenes[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "output": asdict(self.output),
            "audio": asdict(self.audio),
            "editor": asdict(self.editor),
            "motion": asdict(self.motion),
            "scenes": [s.to_dict() for s in self.scenes],
            "active_scene": self.active_scene,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        if not isinstance(data, dict):
            raise ValueError("a project file must contain a JSON object")
        warnings: list[str] = []
        scenes: list[Scene] = []
        seen: set[str] = set()
        raw = data.get("scenes")
        for index, entry in enumerate(raw if isinstance(raw, list) else []):
            try:
                scene = Scene.from_dict(entry, warnings)
            except (ValueError, TypeError) as exc:
                warnings.append(f"skipped scene {index + 1} ({exc})")
                continue
            if scene.id in seen:
                scene.id = new_id("scene")
                warnings.append(
                    f"scene \"{scene.name}\" repeated an id; it was given a new one")
            seen.add(scene.id)
            scenes.append(scene)
        p = cls(
            name=_coerce(data.get("name"), "AnyCam project"),
            output=OutputConfig.from_dict(data.get("output")),
            audio=AudioConfig.from_dict(data.get("audio")),
            editor=EditorConfig.from_dict(data.get("editor")),
            motion=MotionConfig.from_dict(data.get("motion")),
            scenes=scenes,
            active_scene=_coerce(data.get("active_scene"), ""),
        )
        normalize_output(p.output, warnings)
        if not p.scenes:
            p.scenes = [Scene(new_id("scene"), "Scene 1")]
            p.active_scene = p.scenes[0].id
        if p.active_scene not in {scene.id for scene in p.scenes}:
            p.active_scene = p.scenes[0].id
        p.load_warnings = warnings
        return p

    def save(self, path: str | Path) -> Path:
        """Write the project atomically, owner-only, keeping one backup.

        A crash mid-write used to leave a truncated JSON file that the next
        autosave replaced with defaults.  The previous good file is kept as
        ``.bak`` and the new one is renamed into place.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.to_dict(), indent=2) + "\n"
        temp = path.with_name(path.name + ".tmp")
        payload = data.encode("utf-8")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(payload)
            while view:  # os.write may write less than asked
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        if path.exists() and _is_readable_project(path):
            # Only a file that still parses is worth keeping as the backup.
            # Copying a truncated one replaced the last good .bak with it, so
            # the second save after a crash destroyed the recovery copy.
            try:
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            except OSError:
                pass
        os.replace(temp, path)
        try:
            # The file contains the stream key; keep it private to the user.
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        return cls.from_dict(json.loads(Path(path).read_text()))


def _is_readable_project(path: Path) -> bool:
    """Whether ``path`` holds a JSON object, i.e. is a usable backup."""
    try:
        return isinstance(json.loads(path.read_text()), dict)
    except (OSError, ValueError):
        return False


def two_camera_scene(left_source: str, right_source: str,
                     name: str = "Two cameras") -> Scene:
    """A ready-made side-by-side scene for two V4L2 camera devices."""
    scene = Scene(new_id("scene"), name)
    scene.add_layer(Layer(
        new_id("layer"), "camera", "Camera 1", source=left_source,
        box=(0.0, 0.0, 0.5, 1.0), z=1,
    ))
    scene.add_layer(Layer(
        new_id("layer"), "camera", "Camera 2", source=right_source,
        box=(0.5, 0.0, 0.5, 1.0), z=2,
    ))
    return scene


def camera_grid_scene(slots: list[str], name: str = "Camera grid",
                      columns: int = 2) -> Scene:
    """A 2x2-style grid with one camera per configurable slot.

    ``slots`` is read in row-major order (top-left, top-right, bottom-left,
    bottom-right for two columns).  An empty string leaves that cell empty, so
    three cameras can occupy any three of the four cells.  The returned scene
    contains ordinary camera layers; they can be moved or re-deviced like any
    other layer.
    """
    cells = [str(value or "").strip() for value in slots]
    if not any(cells):
        raise ValueError("camera grid needs at least one device")
    columns = max(1, min(int(columns), len(cells)))
    rows = (len(cells) + columns - 1) // columns
    cell_w = 1.0 / columns
    cell_h = 1.0 / rows
    scene = Scene(new_id("scene"), name)
    for index, source in enumerate(cells):
        if not source:
            continue
        row, column = divmod(index, columns)
        scene.add_layer(Layer(
            new_id("layer"), "camera", f"Camera {index + 1}", source=source,
            box=(column * cell_w, row * cell_h, cell_w, cell_h), z=index + 1,
        ))
    return scene


def empty_project(name: str = "AnyCam") -> Project:
    """First-run project: one empty single-camera preset.

    The canvas shows a single slot with a ``+`` until the user picks a source;
    no camera is opened just because the app started.
    """
    project = Project(name=name)
    scene = Scene(new_id("scene"), "Camera", layout="full")
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def default_project(name: str = "AnyCam") -> Project:
    """A usable starter scene: full-frame camera + AnyCam title + LIVE badge."""
    project = Project(name=name)
    scene = Scene(new_id("scene"), "Camera")
    scene.add_layer(Layer(new_id("layer"), "camera", "StreamCam", source="", z=1))
    scene.add_layer(Layer(
        new_id("layer"), "text", "Title", source="AnyCam",
        box=(0.035, 0.045, 0.34, 0.095), z=10, font_size=0.062,
        style="badge", color="#ffffff", background="#101418cc",
    ))
    scene.add_layer(Layer(
        new_id("layer"), "text", "Live", source="LIVE",
        box=(0.845, 0.055, 0.12, 0.065), z=11, font_size=0.036,
        style="badge", color="#5eead0", background="#101418cc",
    ))
    project.scenes = [scene]
    project.active_scene = scene.id
    return project


def save_scene_preset(scene: Scene, directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # A scene name is user text: strip anything that could leave the directory
    # or produce an unusable filename.
    safe = re.sub(r"[^a-z0-9._-]+", "-", scene.name.strip().lower()).strip("-.") or "scene"
    path = directory / f"{safe}.anycam-scene.json"
    # Layer sources can carry credentials (rtsp://user:pass@...): owner-only
    # and atomic, exactly like Project.save, never world-readable or torn.
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(scene.to_dict(), indent=2) + "\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    return path


def load_scene_preset(path: str | Path,
                      warnings: list[str] | None = None) -> Scene:
    return Scene.from_dict(json.loads(Path(path).read_text()), warnings)
