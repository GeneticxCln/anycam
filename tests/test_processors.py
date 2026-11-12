"""Portrait, face and framing processors, the segmenter and the live slot."""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("gi")
pytest.importorskip("onnxruntime")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
from PIL import Image  # noqa: E402

from anycam import effect_thumbs, effects, face, processors, segment  # noqa: E402
from anycam.model import Effects, Layer, OutputConfig, Project, Scene  # noqa: E402
from anycam.pipeline import AnyCamEngine  # noqa: E402

Gst.init(None)
SIZE = (640, 360)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture(scope="module")
def portrait() -> Image.Image:
    return effect_thumbs.default_sample(SIZE).convert("RGBA")


@pytest.fixture(scope="module")
def regions(portrait):
    """Pixel regions of the portrait's face, from the detector itself."""
    found = face.shared_detector().detect(np.asarray(portrait.convert("RGB")))[0]
    width, height = portrait.size
    x, y = int(found.x * width), int(found.y * height)
    w, h = int(found.w * width), int(found.h * height)
    (lx, ly), (rx, ry), (nx, ny), (ax, ay), (bx, by) = [
        (int(px * width), int(py * height)) for px, py in found.landmarks]

    def pad(box, amount):
        return (box[0] - amount, box[1] - amount, box[2] + amount, box[3] + amount)

    return {
        "face": (x, y, x + w, y + h),
        "core": (x + w // 4, y + h // 4, x + 3 * w // 4, y + 3 * h // 4),
        "eyes": pad((min(lx, rx), min(ly, ry), max(lx, rx), max(ly, ry)), 6),
        "above": (x, max(0, y - 60), x + w, y + 12),
        "between_nose_and_mouth": (ax, ny, bx, min(ay, by)),
        "corner": (0, 0, 120, 80),
        "moved_eyes": pad((min(lx, rx) - 120, min(ly, ry), max(lx, rx) - 120, max(ly, ry)), 6),
    }


def difference(a: Image.Image, b: Image.Image, box=None) -> float:
    if box:
        a, b = a.crop(box), b.crop(box)
    return float(np.abs(np.asarray(a.convert("RGB"), dtype=int)
                        - np.asarray(b.convert("RGB"), dtype=int)).mean())


# --------------------------------------------------------------- segmentation
def test_the_bundled_segmentation_model_separates_the_person(portrait):
    assert segment.available()
    alpha = segment.person_alpha(np.asarray(portrait.convert("RGB")))
    assert alpha.shape == (144, 256)
    # torso (lower middle-right) is person; the top-left corner is moon.
    assert alpha[110:140, 130:230].mean() > 0.8
    assert alpha[0:20, 0:30].mean() < 0.2


def test_segmentation_is_fast_enough_to_run_live(portrait):
    rgb = np.asarray(portrait.convert("RGB"))
    segment.person_alpha(rgb)
    started = time.perf_counter()
    for _ in range(10):
        segment.person_alpha(rgb)
    assert (time.perf_counter() - started) / 10 < 0.05


def test_a_missing_model_means_no_mask_not_a_crash(monkeypatch, portrait):
    monkeypatch.setattr(segment, "_state", {"session": None})
    assert not segment.available()
    assert segment.person_alpha(np.asarray(portrait.convert("RGB"))) is None


def test_without_a_model_the_mask_falls_back_to_the_face(monkeypatch, portrait, regions):
    monkeypatch.setattr(segment, "available", lambda: False)
    mask = processors.PersonMask().compute(portrait, 0.0)
    assert mask.size == portrait.size
    array = np.asarray(mask)
    core = regions["core"]
    assert array[core[1]:core[3], core[0]:core[2]].mean() > 200   # the face
    assert array[0:30, 0:60].mean() < 60                          # far corner


def test_with_no_model_and_no_face_nothing_is_blanked(monkeypatch):
    monkeypatch.setattr(segment, "available", lambda: False)
    empty = Image.new("RGBA", SIZE, (30, 60, 90, 255))
    mask = processors.PersonMask().compute(empty, 0.0)
    assert np.asarray(mask).min() > 250


# ---------------------------------------------------------------------- faces
def test_the_detector_returns_five_landmarks(portrait):
    found = face.shared_detector().detect(np.asarray(portrait.convert("RGB")))
    assert len(found) == 1 and len(found[0].landmarks) == 5
    left_eye, right_eye, nose = found[0].landmarks[:3]
    assert left_eye[0] < right_eye[0]                       # image-left first
    assert nose[1] > left_eye[1]                            # nose below the eyes


def test_tracking_smooths_and_briefly_holds_a_face(portrait):
    track = processors.FaceTrack(interval=0.0, hold=0.5)
    assert len(track.update(portrait, 0.0)) == 1
    blank = Image.new("RGBA", SIZE, (0, 0, 0, 255))
    assert len(track.update(blank, 0.2)) == 1               # held, not flickering
    assert len(track.update(blank, 1.5)) == 0               # gone after the hold


# ----------------------------------------------------------------- processors
# Rounded corners clear the alpha at the corners on purpose.
@pytest.mark.parametrize("key", [spec.key for spec in effects.available_filters()
                                 if spec.processor and spec.key != "round-corners"])
def test_every_processor_returns_an_opaque_picture_of_the_same_size(key, portrait):
    spec = effects.FILTERS[key]
    processor = processors.make_processor(spec.processor, spec.default)
    out = None
    for step in range(3):
        out = processor.process(portrait.copy(), step * 0.2)
    assert out.size == portrait.size and out.mode == "RGBA"
    assert np.asarray(out)[..., 3].min() == 255


def test_background_blur_keeps_the_person_sharp_and_blurs_the_rest(portrait, regions):
    out = processors.make_processor("background:blur", 0.9).process(portrait, 0.0)
    assert difference(portrait, out, regions["core"]) < 4        # the face
    assert difference(portrait, out, regions["corner"]) > 3      # the moon


def test_backdrop_replacement_changes_only_the_background(portrait, regions):
    out = processors.make_processor("background:Sunset", 0.5).process(portrait, 0.0)
    assert difference(portrait, out, regions["core"]) < 4
    assert difference(portrait, out, regions["corner"]) > 25


def test_stickers_land_on_the_right_part_of_the_face(portrait, regions):
    glasses = processors.make_processor("sticker:sunglasses", 0.5).process(portrait, 0.0)
    assert difference(portrait, glasses, regions["eyes"]) > 20       # eyes covered
    assert difference(portrait, glasses, regions["corner"]) == 0      # elsewhere untouched
    hat = processors.make_processor("sticker:party-hat", 0.5).process(portrait, 0.0)
    assert difference(portrait, hat, regions["above"]) > 3          # above the head
    stache = processors.make_processor("sticker:mustache", 0.5).process(portrait, 0.0)
    assert difference(portrait, stache, regions["between_nose_and_mouth"]) > 8


def test_a_sticker_follows_the_face_when_it_moves(portrait, regions):
    processor = processors.make_processor("sticker:sunglasses", 0.5)
    first = processor.process(portrait, 0.0)
    shifted = Image.new("RGBA", SIZE, (0, 0, 0, 255))
    shifted.paste(portrait, (-120, 0))
    second = processor.process(shifted, 1.0)
    assert difference(first, portrait, regions["eyes"]) > 20
    assert difference(second, shifted, regions["moved_eyes"]) > 20


def test_stickers_leave_a_faceless_picture_alone():
    blank = Image.new("RGBA", SIZE, (20, 90, 160, 255))
    out = processors.make_processor("sticker:crown", 0.5).process(blank, 0.0)
    assert difference(blank, out) == 0


def test_privacy_hides_the_face_and_nothing_else(portrait, regions):
    for name in ("privacy:pixelate", "privacy:blur"):
        out = processors.make_processor(name, 0.7).process(portrait, 0.0)
        assert difference(portrait, out, regions["face"]) > 6
        assert difference(portrait, out, regions["corner"]) == 0


def test_zoom_enlarges_the_middle(portrait):
    out = processors.make_processor("zoom:", 0.5).process(portrait, 0.0)
    assert difference(portrait, out) > 10


def test_auto_frame_closes_in_on_the_face_over_time(portrait):
    processor = processors.make_processor("autoframe:", 0.8)
    early = processor.process(portrait, 0.0)
    for step in range(1, 40):
        late = processor.process(portrait, step * 0.05)
    assert difference(early, late) > 6
    assert processor._zoom > 1.2


# ------------------------------------------------------------- live in the engine
def image_project(portrait: Image.Image, tmp_path) -> Project:
    path = tmp_path / "portrait.png"
    portrait.convert("RGB").save(path)
    project = Project(name="proc")
    project.output = OutputConfig(canvas_w=640, canvas_h=360, fps=30)
    scene = Scene("s", "S")
    scene.add_layer(Layer("pic", "image", "Pic", source=str(path),
                          box=(0, 0, 1, 1), z=1))
    project.scenes, project.active_scene = [scene], "s"
    return project


class Live:
    def __init__(self, project):
        self.frames: list[Image.Image] = []
        self.engine = AnyCamEngine(project, preview=True,
                                   on_frame=self._on_frame).build()
        self.engine.start()

    def _on_frame(self, sample):
        caps = sample.get_caps().get_structure(0)
        width, height = int(caps.get_value("width")), int(caps.get_value("height"))
        ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
        if ok:
            try:
                self.frames.append(Image.frombytes(
                    "RGBA", (width, height), bytes(info.data)[: width * height * 4]))
            finally:
                sample.get_buffer().unmap(info)

    def settle(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.engine.pump(30)

    @property
    def slot(self):
        return self.engine._layer_chains["pic"].effects["slot"]


def drain_workers(timeout: float = 3.0) -> None:
    """Wait for workers an earlier test stopped to notice and exit (<=100 ms)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and worker_threads():
        time.sleep(0.05)


def worker_threads() -> list[str]:
    return [t.name for t in threading.enumerate()
            if t.name.startswith("anycam-fx-") and t.is_alive()]


def test_a_processor_runs_live_at_the_canvas_rate_and_changes_the_picture(portrait, tmp_path):
    live = Live(image_project(portrait, tmp_path))
    try:
        live.settle(0.6)
        plain = live.frames[-1]
        live.engine.set_layer_effects("pic", Effects(filter="bg-aurora"))
        assert live.slot.idle.wait(3)
        live.settle(1.2)
        before = live.slot.runner.frames
        live.settle(1.0)
        rate = live.slot.runner.frames - before
        assert live.engine.report.error == ""
        assert difference(plain, live.frames[-1], (0, 0, 120, 80)) > 20
        assert 15 <= rate <= 45, f"processing {rate} frames/s on a 30 fps canvas"
    finally:
        live.engine.stop()


def test_swapping_between_processors_and_filters_never_breaks_the_canvas(portrait, tmp_path):
    live = Live(image_project(portrait, tmp_path))
    try:
        live.settle(0.5)
        for key in ("sunglasses", "bg-blur", "twirl", "auto-frame", "crown",
                    "edge", "face-pixelate", "none", "spotlight"):
            live.engine.set_layer_effects("pic", Effects(filter=key))
            assert live.slot.idle.wait(3), key
            live.settle(0.35)
            assert live.engine.report.error == "", key
        assert live.slot.key == "spotlight"
    finally:
        live.engine.stop()


def test_worker_threads_stop_when_swapped_out_and_when_the_canvas_stops(portrait, tmp_path):
    drain_workers()
    baseline = len(worker_threads())
    live = Live(image_project(portrait, tmp_path))
    try:
        live.settle(0.4)
        live.engine.set_layer_effects("pic", Effects(filter="bg-blur"))
        assert live.slot.idle.wait(3)
        live.settle(0.4)
        assert len(worker_threads()) == baseline + 1
        live.engine.set_layer_effects("pic", Effects(filter="none"))
        assert live.slot.idle.wait(3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(worker_threads()) > baseline:
            time.sleep(0.05)
        assert len(worker_threads()) == baseline, "a swapped-out worker kept running"
        live.engine.set_layer_effects("pic", Effects(filter="sunglasses"))
        assert live.slot.idle.wait(3)
    finally:
        live.engine.stop()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and len(worker_threads()) > baseline:
        time.sleep(0.05)
    assert len(worker_threads()) == baseline, "stopping the canvas left a worker running"


def test_a_processor_chosen_before_building_is_present_from_the_start(portrait, tmp_path):
    project = image_project(portrait, tmp_path)
    project.scene.layers[0].effects = Effects(filter="bg-blur", filter_amount=0.8)
    live = Live(project)
    try:
        live.settle(1.2)
        # A processor's sink must not wait to preroll: built that way, the whole
        # canvas stayed PAUSED and showed nothing.
        state = live.engine.pipeline.get_state(Gst.SECOND)[1]
        assert state == Gst.State.PLAYING, state
        assert live.slot.key == "bg-blur" and live.slot.runner.frames > 5
    finally:
        live.engine.stop()


def test_the_thumbnail_renderer_runs_processors(portrait):
    plain = effect_thumbs.render_thumbnail(Effects(), effect_thumbs.default_sample())
    glasses = effect_thumbs.render_thumbnail(
        Effects(filter="sunglasses"), effect_thumbs.default_sample())
    assert plain is not None and glasses is not None
    assert plain.tobytes() != glasses.tobytes()


# ------------------------------------------------- native live framing
def _run_slot(key, portrait, tmp_path, seconds=2.5, amount=0.8):
    path = tmp_path / "frame.png"
    portrait.convert("RGB").resize((640, 360)).save(path)
    pipeline = Gst.parse_launch(
        f"filesrc location={path} ! pngdec ! imagefreeze is-live=true ! videoconvert ! "
        "videoscale ! video/x-raw,format=RGBA,width=640,height=360,framerate=30/1 ! "
        "identity name=src ! appsink name=out sync=true max-buffers=2 drop=true")
    src, out = pipeline.get_by_name("src"), pipeline.get_by_name("out")
    src.unlink(out)
    bin_, runner = effects.build_slot(key, amount, "slot", (640, 360), 30)
    pipeline.add(bin_)
    assert src.link(bin_) and bin_.link(out)
    pipeline.set_state(Gst.State.PLAYING)
    try:
        time.sleep(seconds)
        sample = out.emit("try-pull-sample", Gst.SECOND)
        error = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        assert error is None, error.parse_error()
        crop = bin_.get_by_name("fx")
        margins = [crop.get_property(k) for k in ("left", "right", "top", "bottom")]
        return sample, margins, runner
    finally:
        runner.stop()
        pipeline.set_state(Gst.State.NULL)


def test_live_zoom_is_a_native_centre_crop(portrait, tmp_path):
    sample, margins, runner = _run_slot("zoom", portrait, tmp_path, 1.0, amount=0.5)
    assert runner.processor is runner and not hasattr(runner, "sink")
    caps = sample.get_caps().get_structure(0)
    assert (caps.get_value("width"), caps.get_value("height")) == (640, 360)
    # 2x zoom: a 320x180 window in the middle.
    assert margins == [160, 160, 90, 90]
    runner.set_amount(0.0)
    assert runner.control.margins == (0, 0, 0, 0)


def test_live_auto_frame_closes_in_on_the_face(portrait, tmp_path):
    sample, margins, runner = _run_slot("auto-frame", portrait, tmp_path, 3.0)
    caps = sample.get_caps().get_structure(0)
    assert (caps.get_value("width"), caps.get_value("height")) == (640, 360)
    assert runner.zoom > 1.15, "the face was found on the tap and the crop eased in"
    assert sum(margins) > 40
    assert runner._stop.is_set(), "stopping the slot stops the control thread"


def test_crop_control_ignores_sub_pixel_jitter():
    crop = Gst.ElementFactory.make("videocrop")
    control = processors._CropControl(crop, 640, 360)
    control.apply(100, 50, 320, 180)
    assert control.margins == (100, 220, 50, 130)
    control.apply(101, 50, 320, 180)          # 1 px: not worth a renegotiation
    assert control.margins == (100, 220, 50, 130)
    control.apply(-20, 400, 9999, 180)        # clamped inside the frame
    assert control.margins == (0, 0, 180, 0)
