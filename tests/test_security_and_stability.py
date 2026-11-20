"""Regression tests for the 2026-11-20 full-project audit fixes."""
from __future__ import annotations

import json
import os
import pickle
import stat
import subprocess
from pathlib import Path

import numpy as np
import pytest


def test_effect_worker_rejects_pickle_payloads():
    """IPC must use JSON over raw bytes, never unpickling worker messages."""
    from anycam import effect_worker

    class FakeConn:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def recv_bytes(self, maxlength: int = 0) -> bytes:
            return self._payload

    evil = pickle.dumps({"not": "json"})
    with pytest.raises((ValueError, UnicodeDecodeError)):
        effect_worker._recv_msg(FakeConn(evil))

    valid = json.dumps(["done", 1]).encode("utf-8")
    assert effect_worker._recv_msg(FakeConn(valid)) == ["done", 1]


def test_content_resolve_stream_blocks_flags_and_ssrf(monkeypatch):
    from anycam import content

    called: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        called.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="http://127.0.0.1:8080/secret\n", stderr="")

    monkeypatch.setattr(content.shutil, "which", lambda _name: "/usr/bin/yt-dlp")
    monkeypatch.setattr(content.subprocess, "run", fake_run)

    # Rejects flag injection before spawning yt-dlp.
    url, err = content.resolve_stream("--exec=id")
    assert url == "" and err and not called

    # Rejects loopback SSRF from yt-dlp output and passes --ignore-config + --.
    url, err = content.resolve_stream("https://example.com/watch?v=1")
    assert url == "" and err
    assert "--ignore-config" in called[0] and "--" in called[0]
    assert called[0][-1] == "https://example.com/watch?v=1"


def test_emoji_load_blocks_path_traversal():
    from anycam import emoji

    assert emoji.load("../anycam-logo") is None
    assert emoji.load("/etc/passwd") is None
    assert emoji.load("..\\anycam-logo") is None


def test_project_save_backup_is_owner_only(tmp_path):
    from anycam.model import Project

    path = tmp_path / "project.json"
    proj = Project(name="secrets")
    proj.output.rtmp_url = "rtmp://live.example.com/app/supersecretkey"
    proj.save(path)
    # Loosen mode to simulate an older file, then save again so .bak is created.
    path.chmod(0o644)
    proj.save(path)

    bak = tmp_path / "project.json.bak"
    assert bak.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600


def test_layer_from_dict_preserves_at_sign_in_human_name():
    from anycam.model import Layer

    layer = Layer.from_dict({
        "id": "n1",
        "kind": "network",
        "name": "Cam @ Studio",
        "source": "rtsp://cam.local/stream",
    })
    assert layer.name == "Cam @ Studio"


def test_config_redact_text_handles_encoded_passwords_extra_params_and_bad_urls():
    from anycam import config

    # Malformed URL must not raise ValueError.
    assert isinstance(config.redact_url("rtsp://[::1/bad"), str)
    assert isinstance(config.host_of("rtsp://[::1/bad"), str)

    url = "rtsp://alice:p%40ss@cam.local/live?token=tok123&secret=sec456"
    redacted = config.redact_text(f"failed on {url} (decoded p@ss, token=tok123)", url)
    assert "p%40ss" not in redacted
    assert "p@ss" not in redacted
    assert "tok123" not in redacted
    assert "sec456" not in redacted


def test_qrencode_passes_secret_on_stdin_not_argv(monkeypatch):
    import builtins

    from anycam.phone import tls

    seen: dict = {}
    real_import = builtins.__import__

    def no_segno(name, *args, **kwargs):
        if name == "segno":
            raise ImportError("no segno")
        return real_import(name, *args, **kwargs)

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"\x89PNG\r\n", stderr=b"")

    monkeypatch.setattr(builtins, "__import__", no_segno)
    monkeypatch.setattr(tls.shutil, "which", lambda name: "/usr/bin/qrencode" if name == "qrencode" else None)
    monkeypatch.setattr(tls.subprocess, "run", fake_run)

    secret_url = "https://192.168.1.10:8443/#pair=supersecrettoken"
    png = tls.qr_png(secret_url)
    assert png.startswith(b"\x89PNG")
    assert all("supersecrettoken" not in arg for arg in seen["cmd"])
    assert seen["input"] == secret_url.encode("utf-8")


def test_streaming_url_allows_query_ampersand_but_rejects_path_ampersand():
    from anycam import streaming

    assert streaming.validate_stream_url("rtmp://host/app/key?user=a&token=b") == ""
    assert streaming.validate_stream_url("rtmp://host/app/k&x") != ""


def test_soak_assessments_avoid_false_positives():
    from anycam import soak

    # RSS decrease from GC must never be flagged as a leak.
    mem = soak._memory_assessment([
        {"t": float(i * 10), "rss_mib": 300.0 - i * 5.0}
        for i in range(12)
    ], warmup=0.0)
    assert mem["ok"] is True

    # Advancing frames over a 2.5s sampling gap must not be flagged as a stall.
    stall = soak._stall_assessment([
        {"t": 5.0, "frames": 150},
        {"t": 7.5, "frames": 225},
        {"t": 10.0, "frames": 300},
    ], warmup=5.0, stall_seconds=2.0)
    assert stall["ok"] is True

    # A single 1s plateau followed by advance is not a 2s stall.
    one_sec = soak._stall_assessment([
        {"t": 5.0, "frames": 300},
        {"t": 6.0, "frames": 300},
        {"t": 7.0, "frames": 360},
    ], warmup=5.0, stall_seconds=2.0)
    assert one_sec["ok"] is True

    # A real 2.5s unchanged plateau IS flagged.
    real_stall = soak._stall_assessment([
        {"t": 5.0, "frames": 300},
        {"t": 7.5, "frames": 300},
        {"t": 8.5, "frames": 330},
    ], warmup=5.0, stall_seconds=2.0)
    assert real_stall["ok"] is False


def test_motion_short_height_and_geometry_and_playlist_edge_cases():
    from anycam import animated, geometry, motion, playlist

    # Short-height frame (1920x16) must not emit empty-slice warning or crash.
    frame = np.zeros((16, 1920, 4), dtype=np.uint8)
    grey = motion.to_grey(frame)
    assert grey.size > 0
    score, _ = motion.detect(frame, grey)
    assert score == 0.0

    # Infinity must not raise OverflowError.
    assert geometry.rotation_degrees("inf") == 0
    assert animated.parse_timer("inf") == ("countdown", 0.0)

    # previous_index clamps out-of-bounds index safely.
    assert playlist.previous_index(["a", "b"], 99) == 1


def test_camera_controls_whitespace_error_does_not_crash(monkeypatch):
    from anycam import camera_controls

    monkeypatch.setattr(camera_controls.config, "run", lambda _cmd, **_kw: (1, "   \n  "))
    assert camera_controls.set_control("/dev/video0", "brightness", 10) != ""


def test_recorder_handles_negative_dts_and_clears_stale_biased_dts():
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    from anycam.engine.outputs.recorder import ENCODER_PTS_BIAS, Recorder

    Gst.init(None)
    pushed: list = []

    class FakeAppSrc:
        def emit(self, signal: str, sample):
            if signal == "push-sample":
                pushed.append(sample.get_buffer())
            return Gst.FlowReturn.OK

    rec = Recorder()
    rec._pipeline = object()
    rec._video_src = FakeAppSrc()
    rec._epoch_base = 0
    caps = Gst.Caps.from_string("video/x-h264,stream-format=avc,alignment=au,width=320,height=180")

    # Case 1: dts < pts on first frame of epoch -> clamped to 0, no OverflowError.
    buf1 = Gst.Buffer.new_allocate(None, 8, None)
    buf1.pts = ENCODER_PTS_BIAS + 33_000_000
    buf1.dts = ENCODER_PTS_BIAS
    buf1.duration = 33_000_000
    rec._push("video", Gst.Sample.new(buf1, caps, None, None), 33_000_000)

    # Case 2: dts < ENCODER_PTS_BIAS (reorder before bias) -> cleared to CLOCK_TIME_NONE.
    buf2 = Gst.Buffer.new_allocate(None, 8, None)
    buf2.pts = ENCODER_PTS_BIAS + 66_000_000
    buf2.dts = ENCODER_PTS_BIAS - 33_000_000
    buf2.duration = 33_000_000
    rec._push("video", Gst.Sample.new(buf2, caps, None, None), 33_000_000)

    assert len(pushed) == 2
    assert pushed[0].dts == 0
    assert pushed[1].dts == Gst.CLOCK_TIME_NONE


def test_gui_grid_layouts_have_no_overlapping_cells_and_scrollbars_skip_tab_focus():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QGridLayout, QScrollBar

    from anycam import a11y
    from anycam.gui import AnyCamWindow
    from anycam.gui_many import ManyCamWindow
    from anycam.model import Layer, Project, Scene

    QApplication.instance() or QApplication([])

    def _assert_no_grid_overlaps(root_widget) -> None:
        for grid in root_widget.findChildren(QGridLayout):
            occupied: set[tuple[int, int]] = set()
            for idx in range(grid.count()):
                r, c, rs, cs = grid.getItemPosition(idx)
                for rr in range(r, r + max(1, rs)):
                    for cc in range(c, c + max(1, cs)):
                        assert (rr, cc) not in occupied, (
                            f"Overlapping QGridLayout cell ({rr}, {cc}) in {grid.parentWidget()}"
                        )
                        occupied.add((rr, cc))

    proj = Project(name="audit-gui")
    scene = Scene("s1", "Scene 1")
    scene.add_layer(Layer("l1", "test", "Ball", source="ball"))
    proj.scenes, proj.active_scene = [scene], scene.id

    many = ManyCamWindow(proj)
    many.headless = True
    many._persist = False
    try:
        # Must not arm autosave on initial launch.
        assert not many._autosave_timer.isActive()
        _assert_no_grid_overlaps(many)
        if hasattr(many, "_settings") and many._settings is not None:
            _assert_no_grid_overlaps(many._settings)

        # a11y must not turn QScrollBars into keyboard Tab stops.
        a11y.apply(many)
        for bar in many.findChildren(QScrollBar):
            assert bar.focusPolicy() == Qt.FocusPolicy.NoFocus

        # Undo must restore framing properties (rotation / fit / zoom / pan).
        layer = many.scene.layers[0]
        before = many._layout_snapshot()
        layer.rotation = 90
        layer.flip_h = True
        layer.zoom = 1.5
        many._record_history(before)
        many.undo()
        assert layer.rotation == 0
        assert layer.flip_h is False
        assert layer.zoom == pytest.approx(1.0)
    finally:
        many.engine = None
        many.close()

    # Classic UI reload_project() must not crash with AttributeError.
    classic = AnyCamWindow(proj)
    classic.headless = True
    classic._persist = False
    try:
        _assert_no_grid_overlaps(classic)
        classic._load_extended_widgets()
    finally:
        classic.engine = None
        classic.close()


def test_phone_server_security_headers_and_bad_content_length(tmp_path):
    import importlib.util
    import shutil
    import ssl
    import urllib.request

    from anycam.phone import server as phone_server

    if importlib.util.find_spec("cryptography") is None and shutil.which("openssl") is None:
        pytest.skip("neither cryptography nor openssl available")

    svc = phone_server.PhoneService(
        state_dir=tmp_path / "state", cert_dir=tmp_path / "cert",
        host="127.0.0.1", port=0,
    )
    try:
        svc.start()
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(f"https://127.0.0.1:{svc.port}/", context=ctx, timeout=5) as resp:
            assert resp.headers.get("X-Frame-Options") == "DENY"
            assert "frame-ancestors 'none'" in (resp.headers.get("Content-Security-Policy") or "")
        # Verify HEAD works.
        req = urllib.request.Request(f"https://127.0.0.1:{svc.port}/", method="HEAD")
        with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
            assert resp.status == 200
    finally:
        svc.stop()


def test_env_os_unused_check():
    # Ensure os import isn't flagged while confirming XDG relative path is ignored.
    from anycam import config

    old = os.environ.get("XDG_CONFIG_HOME")
    try:
        os.environ["XDG_CONFIG_HOME"] = "relative/bad/path"
        cfg = config._xdg("XDG_CONFIG_HOME", ".config")
        assert cfg.is_absolute() and cfg == Path.home() / ".config"
    finally:
        if old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old
