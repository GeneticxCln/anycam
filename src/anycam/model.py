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


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@dataclass
class Effects:
    brightness: float = 0.0          # -1 .. 1
    contrast: float = 1.0            # 0 .. 3
    saturation: float = 1.0          # 0 .. 3
    blur: float = 0.0                # 0 .. 40 px sigma
    chroma_enabled: bool = False
    chroma_key: str = "#00ff00"
    chroma_similarity: float = 0.35
    chroma_smoothness: float = 0.10
    # Reserved for the background/face effects.  They are serialised so a
    # future build can load projects, but the compositor does not implement
    # them and the GUI controls are disabled.
    background_mode: str = "none"    # none | blur | replace
    background_blur: float = 18.0
    background_image: str = ""
    face_effect: str = "none"        # none | spotlight | frame
    face_strength: float = 0.6

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Effects":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


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

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PlaylistConfig":
        data = data or {}
        ids = data.get("layer_ids", [])
        if not isinstance(ids, list):
            ids = []
        return cls(
            layer_ids=[str(value) for value in ids if value],
            interval=float(data.get("interval") or 3.0),
            loop=bool(data.get("loop", True)),
            index=int(data.get("index", -1)),
        )


@dataclass
class EditorConfig:
    """Per-project editing preferences that should survive a restart."""

    snap_enabled: bool = True
    snap_px: float = 6.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EditorConfig":
        data = data or {}
        return cls(
            snap_enabled=bool(data.get("snap_enabled", True)),
            snap_px=float(data.get("snap_px", 6.0)),
        )


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Layer":
        data = dict(data)
        if "effects" in data:
            data["effects"] = Effects.from_dict(data["effects"])
        data["box"] = tuple(data.get("box", (0, 0, 1, 1)))
        data["crop"] = tuple(data.get("crop", (0, 0, 0, 0)))
        data["playlist"] = [PlaylistItem(**p) if isinstance(p, dict) else p
                            for p in data.get("playlist", [])]
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["box"] = list(self.box)
        d["crop"] = list(self.crop)
        return d


@dataclass
class Scene:
    id: str
    name: str
    layers: list[Layer] = field(default_factory=list)
    transition: str = "cut"      # reserved: scene changes are hard cuts today
    playlist: PlaylistConfig = field(default_factory=PlaylistConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scene":
        return cls(
            id=data.get("id") or new_id("scene"),
            name=data.get("name", "Scene"),
            layers=[Layer.from_dict(x) for x in data.get("layers", [])],
            transition=data.get("transition", "cut"),
            playlist=PlaylistConfig.from_dict(data.get("playlist")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name,
                "layers": [l.to_dict() for l in self.layers],
                "transition": self.transition,
                "playlist": asdict(self.playlist)}

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
    media_file: str = ""
    media_loop: bool = True      # reserved: the media bed plays once today
    media_gain_db: float = 0.0
    media_muted: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AudioConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class OutputConfig:
    canvas_w: int = 1920
    canvas_h: int = 1080
    fps: int = 60
    vcam_device: str = "/dev/video10"
    # Empty means "follow the XDG video directory".  The field used to be
    # ignored: recordings always went to ~/Videos/AnyCam regardless.
    record_dir: str = ""
    rtmp_url: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutputConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class Project:
    name: str = "AnyCam project"
    output: OutputConfig = field(default_factory=OutputConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    editor: EditorConfig = field(default_factory=EditorConfig)
    scenes: list[Scene] = field(default_factory=list)
    active_scene: str = ""

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
            "scenes": [s.to_dict() for s in self.scenes],
            "active_scene": self.active_scene,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        p = cls(
            name=data.get("name", "AnyCam project"),
            output=OutputConfig.from_dict(data.get("output", {})),
            audio=AudioConfig.from_dict(data.get("audio", {})),
            editor=EditorConfig.from_dict(data.get("editor")),
            scenes=[Scene.from_dict(s) for s in data.get("scenes", [])],
            active_scene=data.get("active_scene", ""),
        )
        if not p.scenes:
            p.scenes = [Scene(new_id("scene"), "Scene 1")]
            p.active_scene = p.scenes[0].id
        if not p.active_scene:
            p.active_scene = p.scenes[0].id
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
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        if path.exists():
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
    path.write_text(json.dumps(scene.to_dict(), indent=2) + "\n")
    return path


def load_scene_preset(path: str | Path) -> Scene:
    return Scene.from_dict(json.loads(Path(path).read_text()))
