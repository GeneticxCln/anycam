"""Regression tests for Phase 1 audit fixes (correctness, concurrency & security)."""
from __future__ import annotations

import json
import socket
import threading
from unittest import mock

import pytest

from anycam import content, effect_worker, i18n, models, motion, packs, privileged, segment
from anycam.engine.outputs.recorder import ENCODER_PTS_BIAS, Recorder
from anycam.engine.source_pool import CaptureSpec, SourcePool
from anycam.model import Layer, Scene
from anycam.phone.server import _TLSServer
from anycam.ui.settings_view import SideTabWidget


def test_motion_monitor_reset_clears_cooldown_level_and_region():
    import numpy as np

    mon = motion.MotionMonitor(sensitivity=0.8, hold_s=0.0, cooldown_s=60.0)
    black = np.zeros((64, 64, 4), dtype=np.uint8)
    white = np.full((64, 64, 4), 255, dtype=np.uint8)
    assert mon.update(black, 1.0) is False
    assert mon.update(white, 2.0) is True
    assert mon.level > 0.0
    assert mon.region is not None
    mon.reset()
    assert mon.level == 0.0
    assert mon.region is None
    # After reset, cooldown must be cleared so new motion fires immediately.
    assert mon.update(black, 3.0) is False
    assert mon.update(white, 4.0) is True


def test_content_ssrf_blocks_dns_resolving_to_private_ip(monkeypatch):
    def fake_getaddrinfo(host, port, proto=0):
        if host == "rebind.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        if host == "meta.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert content._is_safe_stream_host("rebind.example") is False
    assert content._is_safe_stream_host("meta.example") is False
    assert content._is_safe_stream_host("public.example") is True

    stream, err = content.resolve_stream("http://127.0.0.1/secret")
    assert stream == ""
    assert "disallowed" in err.lower()


def test_models_reject_non_https_redirect():
    handler = models._HttpsOnlyRedirectHandler()
    with pytest.raises(ValueError, match="non-HTTPS"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example/model.onnx")


def test_packs_failed_exec_does_not_pollute_sys_modules(tmp_path):
    import sys

    pack_dir = tmp_path / "broken"
    pack_dir.mkdir()
    (pack_dir / "pack.json").write_text(json.dumps({
        "name": "broken",
        "effects": [{"key": "boom", "label": "Boom", "file": "boom.py"}],
    }), encoding="utf-8")
    (pack_dir / "boom.py").write_text("raise RuntimeError('kaboom')\n", encoding="utf-8")
    before = set(sys.modules)
    with pytest.raises(RuntimeError, match="kaboom"):
        packs.load_effect("broken/boom", 0.5, root=tmp_path)
    leaked = [name for name in set(sys.modules) - before if name.startswith("anycam_pack_broken_boom")]
    assert leaked == []


def test_non_ai_touchup_variants_are_not_isolated(monkeypatch):
    monkeypatch.setattr(effect_worker, "_enabled", True)
    monkeypatch.delenv("ANYCAM_AI_WORKER", raising=False)
    assert effect_worker.wants_isolation("touchup:glow") is False
    assert effect_worker.wants_isolation("touchup:night") is False
    assert effect_worker.wants_isolation("touchup:smooth") is True
    assert effect_worker.wants_isolation("pack:demo/fx") is True


def test_segment_reset_notifies_live_workers(monkeypatch):
    notified = []
    monkeypatch.setattr(effect_worker, "notify_runtime_settings",
                        lambda device=None, model=None: notified.append((device, model)))
    segment.reset()
    assert notified and notified[-1][1] == segment.chosen_model()


def test_source_pool_canonicalizes_camera_symlinks(tmp_path):
    target = tmp_path / "video0"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "by-id-cam"
    link.symlink_to(target)
    pool = SourcePool()
    ch1 = pool.channel_for(CaptureSpec("camera", str(target)))
    ch2 = pool.channel_for(CaptureSpec("camera", str(link)))
    assert ch1 == ch2


def test_tls_server_picks_ipv6_address_family():
    class DummyTLS(_TLSServer):
        def server_bind(self):
            pass

        def server_activate(self):
            pass

    srv = DummyTLS(("::1", 0), None, mock.MagicMock(), mock.MagicMock())
    try:
        assert srv.address_family == socket.AF_INET6
    finally:
        srv.server_close()


def test_privileged_polkit_agents_include_common_desktops():
    joined = "\n".join(privileged.POLKIT_AGENTS)
    assert "policykit-1-gnome" in joined
    assert "hyprpolkitagent" in joined
    assert "xfce-polkit" in joined


def test_recorder_pts_bias_near_boundary_and_dts_monotonicity():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    rec = Recorder()
    pushed = []

    class FakeSrc:
        def emit(self, sig, sample):
            pushed.append(sample.get_buffer())
            return Gst.FlowReturn.OK

    rec._pipeline = object()
    rec._video_src = FakeSrc()
    rec._epoch_base = 0
    caps = Gst.Caps.from_string("video/x-h264,width=640,height=360")
    # 1 ns below ENCODER_PTS_BIAS must still strip the 1000-hour bias cleanly.
    buf = Gst.Buffer.new_allocate(None, 8, None)
    buf.pts = ENCODER_PTS_BIAS - 1
    buf.dts = ENCODER_PTS_BIAS + 10_000_000  # dts > pts must be clamped to <= pts
    buf.duration = Gst.SECOND // 30
    sample = Gst.Sample.new(buf, caps, None, None)
    rec._push("video", sample, Gst.SECOND // 30)
    assert len(pushed) == 1
    assert pushed[0].pts == 0
    assert pushed[0].dts <= pushed[0].pts


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_side_tab_widget_keeps_translated_subtitle_on_switch(qapp):
    from PySide6.QtWidgets import QWidget

    de = i18n.load("de")
    try:
        tabs = SideTabWidget()
        tabs.addTab(QWidget(), "Audio", subtitle="Microphone, desktop audio, mixer and virtual mic")
        tabs.addTab(QWidget(), "Stream", subtitle="RTMP, RTMPS and SRT destinations, bitrate and live status.")
        i18n.apply(tabs, de)
        tabs.setCurrentIndex(1)
        assert tabs._header_sub.text() == i18n.translate("RTMP, RTMPS and SRT destinations, bitrate and live status.", de)
        assert tabs._header_sub.text() != "RTMP, RTMPS and SRT destinations, bitrate and live status."
    finally:
        i18n.apply(QWidget(), {})


def _make_window(tmp_path, monkeypatch, *, headless: bool = True):
    from anycam.gui_many import ManyCamWindow
    from anycam.model import empty_project

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    window = ManyCamWindow(empty_project())
    window.headless = headless
    window._persist = False
    return window


def test_export_scene_scrubs_network_credentials(tmp_path, qapp, monkeypatch):
    window = _make_window(tmp_path, monkeypatch)
    try:
        scene = Scene("s1", "Secret Scene", [
            Layer("l1", "network", "IP Cam", source="rtsp://admin:SuperSecret99@10.0.0.5:554/live?token=MyToken123"),
        ])
        window.project.scenes = [scene]
        window.project.active_scene = "s1"
        out = tmp_path / "exported.anycam-scene.json"
        assert window.export_scene("s1", out) is True
        raw = out.read_text(encoding="utf-8")
        assert "SuperSecret99" not in raw
        assert "MyToken123" not in raw
    finally:
        window.shutdown()


def test_ctl_dispatch_blocks_mutations_when_ui_blocked(tmp_path, qapp, monkeypatch):
    window = _make_window(tmp_path, monkeypatch)
    try:
        window._ui_blocked = 1
        assert window.ctl_dispatch({"action": "mute"}) == {"ok": False, "error": "busy"}
        assert window.ctl_dispatch({"action": "state"})["ok"] is True
        assert window.ctl_dispatch({"action": "list"})["ok"] is True
    finally:
        window._ui_blocked = 0
        window.shutdown()


def test_run_with_ui_returns_on_timeout_sentinel(tmp_path, qapp, monkeypatch):
    window = _make_window(tmp_path, monkeypatch, headless=False)
    gate = threading.Event()
    sentinel = object()
    try:
        res = window._run_with_ui(lambda: gate.wait(5.0), timeout=0.5, on_timeout=sentinel)
        assert res is sentinel
    finally:
        gate.set()
        window.shutdown()


def test_stream_url_masked_and_extra_list_redacted(tmp_path, qapp, monkeypatch):
    from PySide6.QtWidgets import QLineEdit

    window = _make_window(tmp_path, monkeypatch)
    try:
        assert window.rtmp_edit.echoMode() == QLineEdit.EchoMode.Password
        window.rtmp_show_button.setChecked(True)
        assert window.rtmp_edit.echoMode() == QLineEdit.EchoMode.Normal
        assert window.add_extra_destination("rtmp://live.example/app/secret_stream_key_99") == ""
        assert window.extra_list.count() == 1
        assert "secret_stream_key_99" not in window.extra_list.item(0).text()
        assert window.project.output.rtmp_extra_urls == ["rtmp://live.example/app/secret_stream_key_99"]
    finally:
        window.shutdown()
