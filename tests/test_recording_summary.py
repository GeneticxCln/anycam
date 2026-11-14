"""What the window says about a recording right after Stop."""
import shutil
import subprocess

import pytest

pytest.importorskip("gi")

from anycam.engine import verify  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="ffmpeg/ffprobe unavailable")


def _clip(path, audio=True):
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=2"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=duration=2"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac"] if audio else []) + [str(path)]
    subprocess.run(command, check=True)


@needs_ffmpeg
def test_a_good_recording_is_described_with_length_size_and_sound(tmp_path):
    path = tmp_path / "good.mp4"
    _clip(path)
    summary = verify.summarize_recording(str(path))
    assert summary["ok"] and summary["audio"] and (summary["width"], summary["height"]) == (320, 240)
    assert 1.5 < summary["seconds"] < 2.5
    text = verify.describe_recording(summary)
    assert "good.mp4" in text and "0:02" in text and "with sound" in text


@needs_ffmpeg
def test_a_silent_recording_says_so(tmp_path):
    path = tmp_path / "silent.mp4"
    _clip(path, audio=False)
    summary = verify.summarize_recording(str(path))
    assert summary["ok"] and not summary["audio"]
    assert "no sound" in verify.describe_recording(summary)


@needs_ffmpeg
def test_a_file_that_is_not_closed_cleanly_carries_a_warning(tmp_path):
    path = tmp_path / "open.mp4"
    _clip(path)
    summary = verify.summarize_recording(str(path), finalised=False)
    assert summary["ok"] and "not closed cleanly" in verify.describe_recording(summary)


def test_a_missing_file_and_a_garbage_file_are_problems_not_exceptions(tmp_path):
    missing = verify.summarize_recording(str(tmp_path / "nope.mp4"))
    assert not missing["ok"] and "missing" in missing["problem"]
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all" * 100)
    result = verify.summarize_recording(str(junk))
    assert not result["ok"] and "problem" in verify.describe_recording(result)
