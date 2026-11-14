"""Encoder probing: a child process test-encodes, a crash or hang cannot reach the app, answers are cached."""
import sys

import pytest

pytest.importorskip("gi")

from anycam.engine import encoders  # noqa: E402


def _child(code: str) -> list[str]:
    return [sys.executable, "-c", code]


GOOD = ("import json;"
        "print('ANYCAM-PROBE '+json.dumps({'factory':'x264enc','ok':True,'reason':''}),flush=True);")


def test_the_candidates_cover_nvenc_va_and_x264():
    names = encoders.candidate_encoders()
    assert {"nvh264enc", "nvav1enc", "vah264enc", "vaav1lpenc", "x264enc"} <= set(names)


def test_a_clean_child_reports_what_it_answered_and_marks_the_rest():
    found = encoders.run_probe(command=_child(GOOD))
    assert found["x264enc"] == {"ok": True, "reason": ""}
    assert found["nvh264enc"]["ok"] is False and "did not finish" in found["nvh264enc"]["reason"]


def test_a_child_that_segfaults_leaves_the_app_standing_and_names_the_crash():
    found = encoders.run_probe(command=_child(GOOD + "import ctypes; ctypes.string_at(0)"))
    assert found["x264enc"]["ok"] is True
    assert "crashed" in found["nvh264enc"]["reason"]


def test_a_child_that_hangs_is_stopped_after_the_timeout():
    found = encoders.run_probe(timeout=1.5, command=_child(GOOD + "import time; time.sleep(60)"))
    assert found["x264enc"]["ok"] is True
    assert "stopped after" in found["vah264enc"]["reason"]


def test_a_child_that_cannot_start_is_reported():
    found = encoders.run_probe(command=["/nonexistent/probe-binary"])
    assert not any(entry["ok"] for entry in found.values())
    assert "could not start" in found["x264enc"]["reason"]


def test_the_real_test_encode_works_for_x264_and_names_a_missing_plugin():
    assert encoders.test_encode("x264enc") == ""
    assert encoders.test_encode("no-such-encoder") == "plugin not installed"


def test_answers_are_cached_until_the_fingerprint_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    answers = {"nvh264enc": {"ok": True, "reason": ""}}
    encoders.save_cache(answers)
    assert encoders.load_cache() == answers
    monkeypatch.setattr("platform.release", lambda: "a-new-kernel")
    assert encoders.load_cache() is None


def test_nothing_is_known_without_a_probe_and_the_plan_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert encoders.probe_results() is None
    assert encoders.nvenc_available("h264") is False
    plan = encoders.encoder_plan("auto")
    assert plan.factory == "x264enc" and "not finished" in plan.note


def test_a_probe_answer_decides_nvenc_and_va(monkeypatch):
    monkeypatch.setattr(encoders, "_results", {
        "nvh264enc": {"ok": True, "reason": ""},
        "nvh265enc": {"ok": False, "reason": "driver refused the session"},
        "vah264enc": {"ok": True, "reason": ""}})
    assert encoders.nvenc_available("h264") and not encoders.nvenc_available("h265")
    assert encoders._va_encodes("vah264enc") and not encoders._va_encodes("vah265enc")
    assert encoders.encoder_problems() == {"nvh265enc": "driver refused the session"}


def test_a_blocking_probe_runs_and_saves_the_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(encoders, "run_probe", lambda: {"x264enc": {"ok": True, "reason": ""}})
    assert encoders.probe_results(block=True)["x264enc"]["ok"]
    encoders.forget_probe()
    assert encoders.probe_results()["x264enc"]["ok"], "the second ask reads the cache"
