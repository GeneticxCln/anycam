"""Phase 3 tests: README quick-win limitations completed."""
from __future__ import annotations

import os
import sys
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anycam import config, firstrun, phone
from anycam.demo import build_demo_project
from anycam.model import AudioConfig, Effects, Layer, empty_project
from anycam.phone import firewall
from anycam.phone.auth import Pairing
from anycam.pipeline import AnyCamEngine
from anycam.sources import gpu_colour_is_equivalent


def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ----------------------------------------------------------------- Paired phones UI
def test_paired_phones_ui_list_and_forget(tmp_path, monkeypatch):
    _qapp()
    from anycam.gui_many import ManyCamWindow

    monkeypatch.setattr(config, "state_dir", lambda: tmp_path)
    pairing = Pairing(tmp_path / "phones.json")
    t1 = pairing.new_token()
    pairing.pair(t1, "Pixel 9")
    t2 = pairing.new_token()
    pairing.pair(t2, "iPhone")

    win = ManyCamWindow(empty_project())
    win.headless = True
    win._persist = False
    try:
        win._refresh_paired_phones()
        assert win.paired_phones_list.count() == 2
        assert win.forget_all_phones_button.isEnabled()

        win.paired_phones_list.setCurrentRow(0)
        first_id = win.paired_phones_list.currentItem().data(0x0100)  # Qt.UserRole
        assert win.forget_phone(first_id) is True
        assert len(win.paired_phones()) == 1
        assert win.paired_phones_list.count() == 1

        removed = win.forget_all_phones()
        assert removed == 1
        assert win.paired_phones() == {}
        assert win.paired_phones_list.count() == 0
        assert not win.forget_all_phones_button.isEnabled()
    finally:
        win.close()


# ----------------------------------------------------------------- Phone audio
def test_phone_audio_channel_and_layer_audio_attachment():
    ch = phone.phone_audio_channel("my-phone-1")
    assert ch == "anycam-phone-my-phone-1-audio"

    project = build_demo_project("testsrc", 320, 180, 30)
    layer = Layer(id="p1", kind="phone", name="Phone", source="my-phone-1")
    project.scene.layers.append(layer)
    engine = AnyCamEngine(project, preview=True, audio=True).build()
    try:
        assert "p1" in engine._layer_audio
        assert engine.set_layer_volume("p1", 0.5, True) is True
    finally:
        engine.pipeline.set_state(engine.pipeline.get_state(0)[1].NULL)


# ----------------------------------------------------------------- Static screen
def test_static_screen_flagged_static_not_slow():
    project = empty_project()
    screen_layer = Layer(id="scr", kind="screen", name="Screen", source="1")
    project.scene.layers = [screen_layer]
    engine = AnyCamEngine(project, preview=True)
    # Simulate a screen that delivered frames earlier, then went static (0 fps in window).
    engine._layer_pads = {"scr": object()}
    engine._layer_frames = {"scr": 120}
    engine._layer_last = {"scr": 100.0}
    engine._health_since = {"scr": 90.0}
    engine._source_rate_history["scr"] = deque([(98.0, 120), (100.0, 120)], maxlen=16)
    engine._last_sample_time = 99.5
    engine._last_sample_frames = 0
    engine._frame_count = 30
    engine._update_health(100.5)
    entry = engine.report.source_health["scr"]
    assert entry["static"] is True
    assert entry["slow"] is False


# ----------------------------------------------------------------- Camera by-path & realpath
def test_camera_identity_falls_back_to_by_path(tmp_path):
    video = tmp_path / "video0"
    video.write_text("")
    by_id = tmp_path / "by-id"
    by_path = tmp_path / "by-path"
    by_id.mkdir()
    by_path.mkdir()
    link = by_path / "pci-0000:00:14.0-usb-0:2:1.0-video-index0"
    link.symlink_to(video)

    assert config.camera_identity(str(video), by_id_dir=by_id, by_path_dir=by_path) == str(link)


def test_camera_error_layers_matches_realpath(tmp_path):
    _qapp()
    from anycam.gui_many import ManyCamWindow

    real_dev = tmp_path / "video2"
    real_dev.write_text("")
    link = tmp_path / "usb-logitech-index0"
    link.symlink_to(real_dev)

    project = empty_project()
    project.scene.layers = [
        Layer(id="cam1", kind="camera", name="Cam 1", source=str(link)),
        Layer(id="cam2", kind="camera", name="Cam 2", source=str(tmp_path / "video5")),
    ]
    win = ManyCamWindow(project)
    win.headless = True
    win._persist = False
    try:
        blamed = win._camera_error_layers(f"Could not open {real_dev}: Device or resource busy")
        assert blamed == {"cam1": str(link)}
    finally:
        win.close()


# ----------------------------------------------------------------- nftables firewall
def test_nftables_backend_detection_and_command(monkeypatch, tmp_path):
    conf = tmp_path / "nftables.conf"
    conf.write_text(
        "table inet filter {\n"
        "  chain input {\n"
        "    type filter hook input priority 0; policy drop;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(firewall, "_allowed_path", lambda: tmp_path / "fw.json")
    monkeypatch.setattr(firewall, "firewalld_running", lambda: False)
    monkeypatch.setattr(firewall, "UFW_DIR", tmp_path / "no-ufw")
    monkeypatch.setattr(firewall, "NFTABLES_CONF", conf)
    monkeypatch.setattr(firewall.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("nft", "pkexec") else None)
    assert firewall.backend() == "nftables"
    assert firewall.nftables_blocks(8443, conf=conf) is True
    assert firewall.blocks(8443) is True

    cmd = firewall.nftables_allow_command(8443)
    assert cmd is not None
    assert cmd[0].endswith("pkexec")
    assert "tcp dport 8443 accept" in cmd[3]

    firewall.remember_allowed(8443, backend_name="nftables")
    assert firewall.nftables_blocks(8443, conf=conf) is False


# ----------------------------------------------------------------- First-run actionable checklist
def test_firstrun_checks_include_action_tags(monkeypatch):
    monkeypatch.setattr(config, "loopback_device", lambda: "/dev/video10")
    monkeypatch.setattr(config, "vcam_available", lambda _dev: False)
    monkeypatch.setattr(os, "access", lambda _path, _mode: False)
    checks = firstrun.checks(
        elements={"missing": [], "optional_missing": {}},
        cameras=["/dev/video0"],
        environ={},
    )
    by_key = {c.key: c for c in checks}
    assert by_key["vcam"].action == "setup-vcam"
    assert by_key["camera"].action == "video-group"


def test_firstrun_dialog_keeps_rows_and_adds_action_buttons():
    _qapp()
    from anycam.ui.dialogs import FirstRunDialog

    checks = [
        firstrun.Check("vcam", "Virtual camera", False, "not loaded", "fix", action="setup-vcam"),
        firstrun.Check("audio", "Sound server", True, "ready"),
    ]
    dlg = FirstRunDialog(checks)
    try:
        assert dlg.rows.count() == 2
        assert dlg.setup_vcam_button.isEnabled()
        assert dlg.recheck_button.isEnabled()
    finally:
        dlg.close()


# ----------------------------------------------------------------- 4 Filter Slots
def test_four_filter_slots_model_and_ui():
    _qapp()
    from anycam.gui_many import ManyCamWindow

    fx = Effects.from_dict({
        "filter": "sepia", "filter_amount": 0.8,
        "filter2": "blur", "filter2_amount": 0.4,
        "filter3": "edge", "filter3_amount": 1.5,
        "filter4": "mirror", "filter4_amount": -0.2,
    })
    assert fx.filter3 == "edge"
    assert fx.filter3_amount == 1.0
    assert fx.filter4 == "mirror"
    assert fx.filter4_amount == 0.0
    assert gpu_colour_is_equivalent(Effects()) is True
    assert gpu_colour_is_equivalent(Effects(filter3="edge")) is False
    assert gpu_colour_is_equivalent(Effects(filter4="blur")) is False

    win = ManyCamWindow(empty_project())
    win.headless = True
    win._persist = False
    try:
        assert win.filter_slot.count() == 4
        win.filter_slot.setCurrentIndex(2)
        assert win._slot_attr() == "filter3"
        win.filter_slot.setCurrentIndex(3)
        assert win._slot_attr() == "filter4"
    finally:
        win.close()


# ----------------------------------------------------------------- Live mic DSP reconfigure
def test_reconfigure_mic_swaps_dsp_without_rebuild(monkeypatch):
    monkeypatch.setattr(config, "microphone", lambda: "fake-mic")
    project = build_demo_project("testsrc", 320, 180, 30)
    project.audio = AudioConfig(mic_enabled=True, mic_device="fake-mic",
                                mic_denoise="off", mic_eq="flat", mic_dynamics="off")
    engine = AnyCamEngine(project, preview=True, audio=True).build()
    try:
        assert engine.pipeline.get_by_name("mic_dsp") is None
        project.audio.mic_denoise = "high"
        project.audio.mic_eq = "voice"
        assert engine.reconfigure_mic() is True
        assert engine.pipeline.get_by_name("mic_fx_eq") is not None
        assert "voice equaliser" in engine.report.mic_processing

        project.audio.mic_denoise = "off"
        project.audio.mic_eq = "flat"
        assert engine.reconfigure_mic() is True
        assert engine.pipeline.get_by_name("mic_dsp") is None
        assert engine.pipeline.get_by_name("mic_fx_eq") is None
        assert engine.report.mic_processing == ""
    finally:
        engine.pipeline.set_state(engine.pipeline.get_state(0)[1].NULL)


# ----------------------------------------------------------------- Live attach/detach RTMP
def test_attach_and_detach_rtmp_live(monkeypatch):
    project = build_demo_project("testsrc", 320, 180, 30)
    engine = AnyCamEngine(project, preview=True, audio=True).build()
    try:
        fake_pub = MagicMock()
        fake_pub.pipeline = MagicMock()
        fake_pub.feed = MagicMock()
        fake_pub.bus = MagicMock()
        fake_pub.video_src = MagicMock()
        fake_pub.audio_src = MagicMock()
        fake_pub.lock = None
        monkeypatch.setattr(engine, "_make_publisher", lambda url, name, prefix="rtmp": fake_pub)
        monkeypatch.setattr(engine, "_start_async", lambda pub: None)
        assert engine.attach_rtmp("rtmp://localhost:1935/live/test") is True
        assert engine.rtmp == "rtmp://localhost:1935/live/test"
        assert engine._rtmp_video_tee_pad is not None
        assert engine.detach_rtmp() is True
        assert engine.rtmp == ""
        assert engine._rtmp_video_tee_pad is None
    finally:
        engine.pipeline.set_state(engine.pipeline.get_state(0)[1].NULL)
