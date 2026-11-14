"""The persistent log: stderr is copied to a redacted, rotated file, and a native crash leaves a stack."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from anycam import applog, credentials

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    yield tmp_path / "state"
    applog.uninstall()


def test_stderr_lines_are_copied_to_the_log_with_a_timestamp(state, capsys):
    path = applog.install()
    print("[diag] hello world", file=sys.stderr, flush=True)
    assert path is not None and path.parent.parent == state
    text = path.read_text()
    assert "[diag] hello world" in text and "AnyCam started" in text
    assert (path.stat().st_mode & 0o777) == 0o600


def test_known_secrets_and_url_passwords_never_reach_the_log(state):
    credentials._known_ids["live_supersecretkey"] = "id"
    path = applog.install()
    print("publishing to rtmp://x/app/live_supersecretkey failed", file=sys.stderr, flush=True)
    print("camera rtsp://admin:hunter22@10.0.0.2/live down", file=sys.stderr, flush=True)
    text = path.read_text()
    assert "supersecretkey" not in text and "hunter22" not in text
    assert "failed" in text and "down" in text


def test_a_big_old_log_is_rotated_at_start(state):
    log = applog.log_path()
    log.write_text("x" * (applog.MAX_BYTES + 10))
    applog.install()
    assert log.with_name(applog.LOG_NAME + ".1").exists()
    assert log.stat().st_size < applog.MAX_BYTES


def test_tail_returns_the_last_lines(state):
    applog.install()
    for number in range(5):
        applog.write(f"line {number}")
    assert applog.tail(2).splitlines()[-1].endswith("line 4")


def test_a_native_crash_leaves_every_stack_in_the_log(tmp_path):
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / "s"), PYTHONPATH=SRC)
    code = "from anycam import applog; applog.install(); import ctypes; ctypes.string_at(0)"
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=60)
    assert result.returncode != 0
    logs = list((tmp_path / "s").glob("*/anycam.log"))
    assert logs and "Fatal Python error" in logs[0].read_text()
