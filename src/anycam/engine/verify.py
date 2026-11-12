"""MP4 verification helpers."""
from __future__ import annotations


def ffprobe(path: str) -> dict:
    """Return a compact ffprobe summary used by tests and the CLI."""
    import json
    import subprocess

    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()}
    data = json.loads(proc.stdout or "{}")
    streams = []
    for stream in data.get("streams", []):
        streams.append({
            "codec_type": stream.get("codec_type"),
            "codec_name": stream.get("codec_name"),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "r_frame_rate": stream.get("r_frame_rate"),
            "nb_frames": stream.get("nb_frames"),
            "duration": stream.get("duration"),
        })
    return {"format": data.get("format", {}), "streams": streams}


def _count_packets(path: str) -> int:
    """Count encoded video packets in a file.

    Fragmented MP4 has no ``nb_frames`` in the moov atom (sample tables live in
    each fragment), so ffprobe reports 0 and a healthy 10-minute recording was
    scored as 100% dropped frames.  Packet counting is accurate for both
    fragmented and faststart files.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-count_packets", "-show_entries", "stream=nb_read_packets",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    for line in (proc.stdout or "").splitlines():
        line = line.strip().rstrip(",")
        if line.isdigit():
            return int(line)
    return 0


def _parse_frame_rate(text) -> float:
    """``"60/1"`` -> 60.0; ``"0/0"`` (no frame rate) and junk -> 0.0."""
    try:
        num, _, den = str(text or "0/1").partition("/")
        denominator = float(den) if den else 1.0
        return float(num) / denominator if denominator else 0.0
    except ValueError:
        return 0.0


def verify_mp4(path: str, *, width: int = 1920, height: int = 1080, fps: int = 60,
               min_seconds: float = 1.0) -> tuple[bool, dict]:
    info = ffprobe(path)
    if "error" in info:
        return False, info
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        return False, {"error": "no video stream", **info}
    try:
        duration = float(info["format"].get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    rate = _parse_frame_rate(video.get("r_frame_rate"))
    try:
        frames = int(video.get("nb_frames") or 0)
    except (TypeError, ValueError):  # "N/A"
        frames = 0
    if frames <= 0:
        frames = _count_packets(path)
    recorded_fps = frames / duration if duration > 0 else 0.0
    ok = (
        video.get("width") == width
        and video.get("height") == height
        and abs(rate - fps) < 0.5
        and duration >= min_seconds
        and frames > 0
    )
    return ok, {**info, "duration": duration, "rate": rate,
                "frames": frames, "recorded_fps": round(recorded_fps, 3), "ok": ok}
