"""Regressions for the 2026-11-11 full-project audit."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import time
from pathlib import Path

import pytest

from anycam import animated, config, model

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_VIDEOS_DIR", str(tmp_path / "videos"))


# ----------------------------------------------------------------- config
def test_screen_flip_env_applies_when_the_project_says_automatic(monkeypatch):
    monkeypatch.setenv("ANYCAM_SCREEN_FLIP", "rotate-180")
    # The project always stores a value; "automatic" is its default, not a choice.
    assert config.screen_flip_method("automatic") == "rotate-180"
    assert config.screen_flip_method("") == "rotate-180"
    assert config.screen_flip_method(None) == "rotate-180"
    # An explicit project choice still wins over the environment.
    assert config.screen_flip_method("vertical-flip") == "vertical-flip"
    assert config.screen_flip_method("none") == "none"
    monkeypatch.delenv("ANYCAM_SCREEN_FLIP")
    assert config.screen_flip_method("automatic") == "automatic"


def test_exclusive_caps_is_read_for_the_devices_own_slot():
    text = "options v4l2loopback video_nr=9,10 exclusive_caps=0,1\n"
    slot = config._device_slot
    eff = config._effective_exclusive_caps
    assert slot(text, "/dev/video10") == 1
    assert slot(text, "/dev/video9") == 0
    assert slot(text, "/dev/video3") == 0          # not listed: first slot
    assert eff(text, slot(text, "/dev/video10")) is True
    assert eff(text, slot(text, "/dev/video9")) is False
    # A slot the list does not reach is off, like the module's default.
    assert eff("options v4l2loopback exclusive_caps=1\n", 1) is False
    assert eff("options v4l2loopback exclusive_caps=Y,Y\n") is True


def test_setup_checks_use_the_requested_device(monkeypatch):
    seen = []

    def caps(path=None, device=None):
        seen.append(device)
        return None

    monkeypatch.setattr(config, "vcam_exclusive_caps", caps)
    config.vcam_setup_needed("/dev/video17")
    assert seen == ["/dev/video17"]


def test_a_missing_v4l2_ctl_is_named_once(monkeypatch, capsys):
    monkeypatch.setattr(config, "_v4l2_ctl_noted", False)
    config._note_no_v4l2_ctl("No such file")
    config._note_no_v4l2_ctl("No such file")
    err = capsys.readouterr().err
    assert err.count("install v4l-utils") == 1


# ------------------------------------------------------------------ model
def test_unhashable_ids_do_not_stop_a_project_loading():
    data = {"scenes": [
        {"id": ["x"], "name": "A", "layers": [{"kind": "test", "id": {"a": 1}}]},
        {"id": 7, "name": "B", "layers": []},
    ], "active_scene": "7"}
    project = model.Project.from_dict(data)
    assert [type(s.id) for s in project.scenes] == [str, str]
    assert project.scenes[1].id == "7"
    assert project.active_scene == "7"
    assert isinstance(project.scenes[0].layers[0].id, str)


def test_the_canvas_is_made_encodable_on_load():
    project = model.Project.from_dict(
        {"output": {"canvas_w": 1921, "canvas_h": 1081, "fps": 100000}})
    assert (project.output.canvas_w, project.output.canvas_h) == (1920, 1080)
    assert project.output.fps == model.MAX_FPS
    assert any("changed to 1920x1080" in w for w in project.load_warnings)
    huge = model.Project.from_dict({"output": {"canvas_w": 10 ** 9, "canvas_h": 10 ** 9}})
    assert (huge.output.canvas_w, huge.output.canvas_h) == model.MAX_CANVAS
    fine = model.Project.from_dict({"output": {"canvas_w": 1280, "canvas_h": 720, "fps": 30}})
    assert fine.load_warnings == []


# --------------------------------------------------------------- drawing
@pytest.mark.parametrize("width", ["1e30", "Infinity", "NaN", "-5"])
def test_stroke_widths_cannot_overflow_the_drawing(width):
    source = ('{"strokes":[{"tool":"pen","width":%s,'
              '"points":[[0.1,0.1],[0.2,0.2]]}]}' % width)
    strokes = animated.parse_strokes(source)
    assert strokes and 0.0005 <= strokes[0]["width"] <= 0.5
    frame = animated.DrawingCanvas(64, 36, strokes).frame(0.0)
    assert frame.shape == (36, 64, 4)


def test_non_finite_points_are_dropped_and_far_ones_clamped():
    assert animated.parse_strokes(
        '{"strokes":[{"tool":"pen","points":[[NaN,0.5],[0.2,0.2]]}]}') == []
    far = animated.parse_strokes(
        '{"strokes":[{"tool":"box","points":[[1e300,0.1],[-1e300,0.2]]}]}')
    assert far[0]["points"] == [[4.0, 0.1], [-3.0, 0.2]]
    animated.DrawingCanvas(64, 36, far).frame(0.0)


def test_a_stroke_set_from_outside_cannot_blank_the_layer_forever():
    canvas = animated.DrawingCanvas(64, 36, [])
    canvas.set_strokes([{"tool": "pen", "color": "#fff", "width": 1e30,
                         "points": [[0.1, 0.1], [0.2, 0.2]]}])
    assert canvas.frame(0.0).shape == (36, 64, 4)


# ------------------------------------------------------------------- pool
def test_a_home_relative_video_path_is_expanded(tmp_path, monkeypatch):
    pytest.importorskip("gi")
    from anycam.pool import capture_spec

    monkeypatch.setenv("HOME", str(tmp_path))
    layer = model.Layer("v", "video", "V", source="~/clip.mp4")
    spec = capture_spec(layer, 320, 180, 15)
    assert spec.source == str(tmp_path / "clip.mp4")
    assert capture_spec(model.Layer("v", "video", "V", source=""), 320, 180, 15).source == ""


# ----------------------------------------------------------------- engine
def test_stopping_an_engine_twice_keeps_the_first_report():
    pytest.importorskip("gi")
    from anycam.pipeline import AnyCamEngine

    project = model.default_project()
    project.scenes[0].layers = [model.Layer("a", "test", "T", source="ball")]
    project.output.canvas_w, project.output.canvas_h, project.output.fps = 320, 180, 15
    engine = AnyCamEngine(project, preview=True)
    engine.build()
    engine.start()
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        engine.pump(50)
    first = engine.stop()
    second = engine.stop()
    assert first["frames"] > 0
    assert second == first


# --------------------------------------------------------------- snapshot
def test_snapshot_reports_a_failed_write(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    from anycam.gui_many import ManyCamWindow
    from anycam.model import empty_project

    app = QApplication.instance() or QApplication([])

    class Refusing(QImage):
        def save(self, *args, **kwargs):          # noqa: D401 - a full disk
            return False

    window = ManyCamWindow(empty_project())
    window.headless = True
    window._persist = False
    try:
        image = Refusing(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(0xFF112233)
        window.preview.image = image
        window.snapshot()
        assert "could not be written" in window.statusBar().currentMessage()
        # A recordings folder that cannot be created is reported, not raised.
        (tmp_path / "blocker").write_text("a file, not a folder")
        window.project.output.record_dir = str(tmp_path / "blocker" / "inner")
        window.snapshot()
        assert window.statusBar().currentMessage().startswith("Snapshot failed")
    finally:
        window.engine = None
        window.close()
        app.processEvents()


# -------------------------------------------------------------- installer
@pytest.mark.skipif(subprocess.run(["bash", "-c", "true"]).returncode != 0,
                    reason="bash unavailable")
def test_installer_survives_awkward_launcher_paths(tmp_path):
    work = tmp_path / "we&ird | $dir `x` 50%"
    import shutil

    work.mkdir(parents=True)
    shutil.copytree(ROOT / "scripts", work / "scripts")
    shutil.copytree(ROOT / "src", work / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "emoji"))
    shutil.copy(ROOT / "anycam.desktop", work / "anycam.desktop")
    data = tmp_path / "data"
    env = dict(os.environ, XDG_DATA_HOME=str(data))
    old_icon = data / "icons/hicolor/256x256/apps/anycam.png"
    old_icon.parent.mkdir(parents=True)
    old_icon.write_bytes(b"old")
    done = subprocess.run(["bash", str(work / "scripts/install-desktop.sh")],
                          env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    entry = (data / "applications/anycam.desktop").read_text()
    exec_line = next(l for l in entry.splitlines() if l.startswith("Exec="))
    assert exec_line == ('Exec="' + str(work / "scripts/anycam-gui")
                         .replace("$", r"\$").replace("`", r"\`")
                         .replace("%", "%%") + '"')
    assert "Name=AnyCam" in entry and "StartupWMClass=AnyCam" in entry
    icon = data / "icons/hicolor/512x512/apps/anycam.png"
    assert icon.is_file() and not icon.stat().st_mode & 0o111
    assert not old_icon.exists()


# ------------------------------------------------------- overlay pictures
def test_the_frame_picture_is_not_rendered_again_for_the_same_recipe(monkeypatch):
    from anycam import overlay

    saves = []
    real = overlay.save_png_atomic
    monkeypatch.setattr(overlay, "save_png_atomic",
                        lambda image, out: (saves.append(str(out)), real(image, out))[1])
    first = overlay.render_window_frame(640, 360, "none")
    again = overlay.render_window_frame(640, 360, "none")
    assert first == again and len(saves) == 1
    overlay.render_window_frame(640, 360, "line", "#ff0000", 4)
    assert len(saves) == 2                       # a different recipe renders


def test_a_colour_slider_does_not_re_render_the_frame_overlay(monkeypatch):
    pytest.importorskip("gi")
    from anycam import overlay, sources

    layer = model.Layer("l", "test", "t", source="ball")
    chain = sources.test_chain(640, 360, 30, layer)
    if chain.effects.get("frame") is None:
        pytest.skip("gdkpixbufoverlay is not installed")
    saves = []
    real = overlay.save_png_atomic
    monkeypatch.setattr(overlay, "save_png_atomic",
                        lambda image, out: (saves.append(str(out)), real(image, out))[1])
    fx = model.Effects()
    for step in range(6):
        fx.brightness = step / 10
        sources.apply_effects(chain.effects, fx)
    assert len(saves) <= 1


def test_overlay_pictures_are_replaced_atomically(tmp_path):
    import threading

    from PIL import Image

    from anycam import overlay

    target = tmp_path / "overlay.png"
    overlay.save_png_atomic(Image.new("RGBA", (400, 300), (1, 2, 3, 255)), target)
    stop = threading.Event()
    failures = []

    def writer():
        flip = 0
        while not stop.is_set():
            flip ^= 1
            overlay.save_png_atomic(
                Image.new("RGBA", (400 + flip * 40, 300), (flip, 9, 9, 255)), target)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                with Image.open(target) as picture:
                    picture.load()
            except Exception as exc:  # noqa: BLE001 - a torn file
                failures.append(repr(exc))
    finally:
        stop.set()
        thread.join()
    assert not failures, failures[:3]
    assert not list(tmp_path.glob(".*.tmp"))


def test_the_background_cache_key_carries_an_explicit_revision():
    from anycam import overlay, stages

    name = next(n for n in overlay.BACKGROUND_NAMES if n not in stages.STATIC)
    path = overlay.render_background(name, 64, 64)
    assert f"-{overlay.BACKGROUND_REV}-" in path.name


def test_a_replacement_backdrop_matches_a_tiny_frame():
    pytest.importorskip("gi")
    from PIL import Image

    from anycam import processors

    background = processors.Background("replace", 0.5, "Studio", asynchronous=False)
    try:
        for size in ((1, 1), (7, 3), (63, 37)):
            picture = Image.new("RGBA", size, (200, 10, 10, 255))
            assert background.process(picture, 1.0).size == size
    finally:
        background.close()


# ---------------------------------------------------------- phone and ufw
def _ufw_files(tmp_path, rules="### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 in\n"):
    ufw = tmp_path / "ufw"
    ufw.mkdir()
    (ufw / "ufw.conf").write_text("ENABLED=yes\n")
    (ufw / "user.rules").write_text(rules)
    defaults = tmp_path / "default-ufw"
    defaults.write_text('DEFAULT_INPUT_POLICY="DROP"\n')
    return {"ufw_dir": ufw, "defaults": defaults,
            "state_file": tmp_path / "allowed.json"}


def test_unreadable_ufw_rules_still_offer_the_fix(tmp_path, monkeypatch):
    from anycam.phone import firewall

    paths = _ufw_files(tmp_path)
    monkeypatch.setattr(firewall, "ufw_rules_readable", lambda **_k: False)
    # Debian and Ubuntu ship user.rules as 640 root: nothing can be proven open.
    assert firewall.ufw_blocks(8443, **paths)
    firewall.remember_allowed(8443, state_file=paths["state_file"])
    assert not firewall.ufw_blocks(8443, **paths)
    assert firewall.ufw_blocks(9000, **paths)              # another port: unknown
    # Disabled or default-allow ufw still reports nothing to fix.
    (paths["ufw_dir"] / "ufw.conf").write_text("ENABLED=no\n")
    assert not firewall.ufw_blocks(9000, **paths)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_ufw_rules_readability_is_detected_for_real(tmp_path):
    from anycam.phone import firewall

    paths = _ufw_files(tmp_path)
    rules = paths["ufw_dir"] / "user.rules"
    assert firewall.ufw_rules_readable(ufw_dir=paths["ufw_dir"])
    rules.chmod(0o000)
    try:
        assert not firewall.ufw_rules_readable(ufw_dir=paths["ufw_dir"])
        assert firewall.ufw_blocks(8443, **paths)
    finally:
        rules.chmod(0o600)


def test_a_phone_name_is_shown_as_text_not_markup(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from anycam.phone import firewall
    from anycam.phone_dialog import PhonePairDialog

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(firewall, "backend", lambda: "")

    class Service:
        fingerprint, host, port = "AA:BB", "192.0.2.7", 8443

        def pairing_url(self):
            return "https://192.0.2.7:8443/#pair=x"

        def page_url(self):
            return "https://192.0.2.7:8443/"

    dialog = PhonePairDialog(Service(), None, log=lambda _m: None)
    try:
        assert dialog.status.textFormat() == Qt.TextFormat.PlainText
        assert dialog.firewall_text.textFormat() == Qt.TextFormat.PlainText
        dialog.on_phone_event("paired", "phone-1", "<h1>FAKE</h1>")
        assert "<h1>FAKE</h1>" in dialog.status.text()
    finally:
        dialog.close()
        app.processEvents()


# ------------------------------------------------------------ pool errors
def _first_error(pool, spec, seconds=6.0):
    pool.start()
    entry = pool.acquire(spec)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not entry.error:
        pool.poll_errors()
        time.sleep(0.05)
    return entry.error


def test_capture_errors_keep_the_file_path_but_hide_network_secrets():
    pytest.importorskip("gi")
    from anycam.pool import CaptureSpec, SourcePool

    pool = SourcePool()
    try:
        error = _first_error(pool, CaptureSpec("video", "/nonexistent/clip.mp4", 64, 36, 10))
        assert "/nonexistent/clip.mp4" in error
    finally:
        pool.stop()
    pool = SourcePool()
    try:
        error = _first_error(
            pool, CaptureSpec("network", "rtsp://admin:hunter2pw@127.0.0.1:1/live", 64, 36, 10))
        assert error and "hunter2pw" not in error
    finally:
        pool.stop()


# ---------------------------------------------------------------- the GUI
def _flaky_window(monkeypatch, tmp_path, fail_cpu):
    pytest.importorskip("PySide6")
    pytest.importorskip("gi")
    monkeypatch.delenv("ANYCAM_CANVAS", raising=False)
    from PySide6.QtWidgets import QApplication

    from anycam import gui
    from anycam.demo import build_demo_project

    app = QApplication.instance() or QApplication([])
    window = gui.AnyCamWindow(build_demo_project("testsrc", 320, 180, 30))
    window.headless = True
    window._persist = False
    real = gui.AnyCamEngine

    class Flaky(real):
        def start(self):
            if self.canvas == "gpu":
                self.canvas_backend = "gpu"
                raise RuntimeError("GL context lost")
            if fail_cpu:
                raise RuntimeError("camera busy")
            return super().start()

    monkeypatch.setattr("anycam.ui.engine_control.AnyCamEngine", Flaky)
    return app, window


def test_the_gpu_canvas_stays_available_when_the_cpu_retry_fails_too(monkeypatch, tmp_path):
    app, window = _flaky_window(monkeypatch, tmp_path, fail_cpu=True)
    try:
        window.start_engine()
        assert window.engine is None
        assert not window._gpu_canvas_failed       # the GPU was not the cause
    finally:
        window.shutdown()
        window.close()
        app.processEvents()


def test_the_gpu_canvas_is_still_disabled_when_the_cpu_retry_works(monkeypatch, tmp_path):
    app, window = _flaky_window(monkeypatch, tmp_path, fail_cpu=False)
    try:
        window.start_engine()
        assert window.engine is not None and window.engine.canvas_backend == "cpu"
        assert window._gpu_canvas_failed
    finally:
        window.shutdown()
        window.close()
        app.processEvents()


def test_shutdown_saves_the_project_even_when_a_step_raises(monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from anycam.gui_many import ManyCamWindow

    app = QApplication.instance() or QApplication([])
    window = ManyCamWindow(model.empty_project())
    window.headless = True
    window._persist = False
    saved = []
    window.engine = object()                       # something that must be stopped
    monkeypatch.setattr(window, "stop_engine",
                        lambda **_k: (_ for _ in ()).throw(RuntimeError("will not stop")))
    monkeypatch.setattr(window, "_finish_recorder",
                        lambda: (_ for _ in ()).throw(RuntimeError("recorder")))
    monkeypatch.setattr(window, "_save_if_needed", lambda: saved.append(True))
    try:
        window.shutdown()
        assert saved == [True]
    finally:
        window.engine = None
        window.close()
        app.processEvents()


# ------------------------------------------------- filter swap rollback
def test_a_filter_swap_that_cannot_link_leaves_the_old_filter_running():
    pytest.importorskip("gi")
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    from anycam import effects

    Gst.init(None)
    pipeline = Gst.Pipeline.new("swap-rollback")
    source = Gst.ElementFactory.make("videotestsrc")
    source.set_property("is-live", True)
    caps_in = Gst.ElementFactory.make("capsfilter")
    caps_in.set_property("caps", Gst.Caps.from_string(
        "video/x-raw,format=RGBA,width=64,height=64,framerate=30/1"))
    slot = effects.FilterSlot("none", 0.5, size=(64, 64), fps=30)
    caps_out = Gst.ElementFactory.make("capsfilter")
    caps_out.set_property("caps", Gst.Caps.from_string(
        "video/x-raw,format=RGBA,width=64,height=64"))
    sink = Gst.ElementFactory.make("fakesink")
    sink.set_property("sync", False)
    for element in (source, caps_in, slot.element, caps_out, sink):
        pipeline.add(element)
    assert source.link(caps_in) and caps_in.link(slot.element)
    assert slot.element.link(caps_out) and caps_out.link(sink)

    frames = [0]

    def count(_pad, _info):
        frames[0] += 1
        return Gst.PadProbeReturn.OK

    sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, count)
    pipeline.set_state(Gst.State.PLAYING)
    try:
        time.sleep(0.4)
        assert frames[0] > 0
        original = slot.element

        def wrong_size(key, amount):
            # Pins 17x17 where the chain downstream insists on 64x64: no link.
            bad = Gst.Bin.new("bad")
            fixed = Gst.ElementFactory.make("capsfilter")
            fixed.set_property("caps", Gst.Caps.from_string(
                "video/x-raw,format=RGBA,width=17,height=17"))
            bad.add(fixed)
            bad.add_pad(Gst.GhostPad.new("sink", fixed.get_static_pad("sink")))
            bad.add_pad(Gst.GhostPad.new("src", fixed.get_static_pad("src")))
            return bad, None

        slot._new_bin = wrong_size
        key = next(k for k, spec in effects.FILTERS.items()
                   if not spec.processor and effects.filter_available(k))
        slot.apply(key, 0.5)
        assert slot.idle.wait(10)
        assert slot.element is original and slot.key == "none"
        before = frames[0]
        time.sleep(0.5)
        assert frames[0] > before, "the picture stopped after the failed swap"
    finally:
        pipeline.set_state(Gst.State.NULL)
        slot.close()


# ---------------------------------------------------------------- recorder
def test_a_recording_reports_whether_it_was_closed_cleanly(tmp_path):
    pytest.importorskip("gi")
    from anycam.demo import build_demo_project
    from anycam.engine.outputs.recorder import Recorder
    from anycam.pipeline import AnyCamEngine

    out = tmp_path / "clean.mp4"
    recorder = Recorder()
    assert recorder.start(str(out), 320, 180, 30)
    engine = AnyCamEngine(build_demo_project("testsrc", 320, 180, 30),
                          recorder=recorder, preview=True).build()
    engine.start()
    try:
        for _ in range(40):
            engine.pump(50)
            time.sleep(0.01)
    finally:
        engine.stop()
    recorder._bus = None            # as if the muxer never reported end-of-stream
    result = recorder.stop()
    assert result["ok"] and result["finalised"] is False
    assert "not closed cleanly" in result["warning"]

    out2 = tmp_path / "clean2.mp4"
    recorder2 = Recorder()
    assert recorder2.start(str(out2), 320, 180, 30)
    engine = AnyCamEngine(build_demo_project("testsrc", 320, 180, 30),
                          recorder=recorder2, preview=True).build()
    engine.start()
    try:
        for _ in range(40):
            engine.pump(50)
            time.sleep(0.01)
    finally:
        engine.stop()
    clean = recorder2.stop()
    assert clean["ok"] and clean["finalised"] is True and clean["warning"] == ""


# ------------------------------------------------------------ phone hardening
def test_the_certificate_follows_the_address_it_serves(tmp_path):
    pytest.importorskip("cryptography")
    from anycam.phone import tls

    cert, key = tls.ensure_certificate(tmp_path, "192.0.2.10")
    first = tls.fingerprint(cert)
    again, _ = tls.ensure_certificate(tmp_path, "192.0.2.10")
    assert tls.fingerprint(again) == first            # same address: kept
    moved, _ = tls.ensure_certificate(tmp_path, "192.0.2.77")
    assert tls.fingerprint(moved) != first            # new address: remade
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    from cryptography import x509

    names = {str(i.value) for i in x509.load_pem_x509_certificate(moved.read_bytes())
             .extensions.get_extension_for_class(x509.SubjectAlternativeName).value}
    assert "192.0.2.77" in names and "localhost" in names


def test_the_openssl_certificate_names_its_address(tmp_path, monkeypatch):
    import shutil

    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    from anycam.phone import tls

    monkeypatch.setattr(tls, "_create_with_cryptography", lambda *a, **k: False)
    cert, _key = tls.ensure_certificate(tmp_path, "192.0.2.33")
    text = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-text"],
                          capture_output=True, text=True).stdout
    assert "IP Address:192.0.2.33" in text
    # Without cryptography the sidecar still tells when the address moved.
    first = tls.fingerprint(cert)
    kept, _ = tls.ensure_certificate(tmp_path, "192.0.2.33")
    assert tls.fingerprint(kept) == first
    moved, _ = tls.ensure_certificate(tmp_path, "192.0.2.34")
    assert tls.fingerprint(moved) != first


def test_the_phone_server_drops_connections_beyond_its_limit(tmp_path, monkeypatch):
    pytest.importorskip("gi")
    import socket

    from anycam.phone import server

    monkeypatch.setattr(server, "MAX_CONNECTIONS", 3)
    monkeypatch.setattr(server, "HANDSHAKE_TIMEOUT_S", 1)
    service = server.PhoneService(state_dir=tmp_path / "s", cert_dir=tmp_path / "c",
                                  host="127.0.0.1", port=0)
    service.start()
    held = []
    try:
        for _ in range(3):                       # idle sockets: handshake never starts
            connection = socket.create_connection(("127.0.0.1", service.port), timeout=3)
            held.append(connection)
        time.sleep(0.4)
        extra = socket.create_connection(("127.0.0.1", service.port), timeout=3)
        extra.settimeout(3)
        try:
            data = extra.recv(1)                 # dropped at once: EOF, not a hang
        except ConnectionError:
            data = b""
        assert data == b""
        extra.close()
        for connection in held:
            connection.close()
        time.sleep(2)                            # idle handshakes time out
        again = socket.create_connection(("127.0.0.1", service.port), timeout=3)
        again.close()
        assert service._httpd._slots.acquire(blocking=False)
        service._httpd._slots.release()
    finally:
        for connection in held:
            connection.close()
        service.stop()


# ---------------------------------------------------- network reconnection
def test_a_network_stream_that_ends_is_reconnected(tmp_path):
    pytest.importorskip("gi")
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required to make a clip")
    from anycam.pool import CaptureSpec, SourcePool

    clip = tmp_path / "short.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=duration=1:size=160x90:rate=10", "-pix_fmt", "yuv420p",
                    str(clip)], check=True)
    pool = SourcePool()
    pool.start()
    entry = pool.acquire(CaptureSpec("network", clip.as_uri(), 160, 90, 10))
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and entry.restarts < 1:
            pool.poll_errors()
            time.sleep(0.05)
        assert entry.restarts >= 1, "the ended stream was never restarted"
        first_pass = entry.frames_at_restart
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and entry.frames <= first_pass + 3:
            pool.poll_errors()
            time.sleep(0.05)
        assert entry.frames > first_pass, "no frames after the reconnect"
        assert entry.error == "" or entry.restart_at     # cleared, or waiting again
    finally:
        pool.stop()


def test_reconnect_backoff_doubles_and_resets_after_a_healthy_run():
    pytest.importorskip("gi")
    from anycam.pool import CaptureSpec, SourcePool

    pool = SourcePool()
    pool.start()
    entry = pool.acquire(CaptureSpec("network", "file:///nonexistent.mp4", 64, 36, 10))
    try:
        delays = []
        for _ in range(4):
            entry.restart_at = 0.0
            pool._schedule_restart(entry)
            delays.append(round(entry.restart_at - time.monotonic()))
        assert delays == [1, 2, 4, 8]
        entry.frames_at_restart = 0
        entry.frames = 100                      # delivered a healthy run
        entry.restart_at = 0.0
        pool._schedule_restart(entry)
        assert round(entry.restart_at - time.monotonic()) == 1
        # Never beyond the cap, and a camera is left to the GUI's own watcher.
        entry.failures = 40
        entry.restart_at = 0.0
        pool._schedule_restart(entry)
        assert entry.restart_at - time.monotonic() <= SourcePool.RESTART_MAX_S + 0.5
        camera = pool.acquire(CaptureSpec("test", "ball", 64, 36, 10))
        pool._schedule_restart(camera)
        assert camera.restart_at == 0.0
    finally:
        pool.stop()


# ------------------------------------------------------------ blocking work
def test_the_nvenc_probe_is_remembered_for_a_minute(monkeypatch):
    pytest.importorskip("gi")
    from anycam import pipeline

    calls = []
    monkeypatch.setattr("anycam.engine.encoders._nvenc_seen", {})
    monkeypatch.setattr("anycam.engine.encoders._probe_nvenc",
                        lambda codec: calls.append(codec) or True)
    assert pipeline.nvenc_available("h264") and pipeline.nvenc_available("h264")
    assert calls == ["h264"]
    pipeline.nvenc_available("h265")
    assert calls == ["h264", "h265"]
    monkeypatch.setattr("anycam.engine.encoders._NVENC_TTL_S", 0.0)
    pipeline.nvenc_available("h264")
    assert calls == ["h264", "h265", "h264"]


def test_a_slow_firewall_check_does_not_hold_up_the_dialog(monkeypatch):
    pytest.importorskip("PySide6")
    import threading

    from PySide6.QtWidgets import QApplication

    from anycam.phone import firewall
    from anycam.phone_dialog import PhonePairDialog

    app = QApplication.instance() or QApplication([])
    release = threading.Event()
    monkeypatch.setattr(firewall, "backend", lambda: (release.wait(10), "ufw")[1])
    monkeypatch.setattr(firewall, "blocks", lambda port, host="": True)

    class Service:
        fingerprint, host, port = "AA", "192.0.2.7", 8443

        def pairing_url(self):
            return "https://192.0.2.7:8443/#pair=x"

        def page_url(self):
            return "https://192.0.2.7:8443/"

    started = time.monotonic()
    dialog = PhonePairDialog(Service(), None, log=lambda _m: None)
    try:
        assert time.monotonic() - started < 2.0       # the D-Bus call was not awaited
        assert dialog.firewall_bar.isHidden()
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and dialog.firewall_bar.isHidden():
            app.processEvents()
            time.sleep(0.02)
        assert not dialog.firewall_bar.isHidden()
        assert dialog.firewall_backend == "ufw"
    finally:
        release.set()
        dialog.close()
        app.processEvents()


def test_shortcuts_and_clicks_wait_while_a_privileged_step_runs(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from anycam.gui_many import ManyCamWindow

    app = QApplication.instance() or QApplication([])
    window = ManyCamWindow(model.empty_project())
    window.headless = False                   # the threaded path
    window._persist = False
    toggled = []
    monkeypatch.setattr(window, "toggle_record", lambda: toggled.append("record"))
    try:
        def work():
            window._on_global_shortcut("record")      # arrives mid-operation
            return "done"

        assert window._run_with_ui(work) == "done"
        assert toggled == []
        window._on_global_shortcut("record")          # normal again afterwards
        assert toggled == ["record"]
    finally:
        window.headless = True
        window.engine = None
        window.close()
        app.processEvents()
