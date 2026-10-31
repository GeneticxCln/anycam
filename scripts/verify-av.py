#!/usr/bin/env python3
"""Verify an AnyCam recording has synchronised video and audio.

Usage: scripts/verify-av.py FILE [--json OUT] [--width 1920] [--height 1080] [--fps 60]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def ffprobe(path: str) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"ffprobe failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}")


def volume_stats(path: str) -> dict:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0",
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    text = proc.stderr
    stats = {}
    for key in ("mean_volume", "max_volume"):
        match = re.search(rf"{key}:\s*(-?[\d.]+) dB", text)
        if match:
            stats[key] = float(match.group(1))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument("--json", default="")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=60.0)
    args = parser.parse_args()

    info = ffprobe(args.file)
    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    audio = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    duration = float(info["format"].get("duration") or 0)
    checks: dict[str, bool] = {}
    detail: dict = {"file": args.file, "duration": duration, "checks": checks}

    if video:
        num, den = (video.get("r_frame_rate") or "0/1").split("/")
        rate = float(num) / float(den or 1)
        detail["video"] = {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": round(rate, 3),
            "duration": float(video.get("duration") or duration),
        }
        checks["video_resolution"] = (video.get("width") == args.width
                                      and video.get("height") == args.height)
        checks["video_fps"] = abs(rate - args.fps) < 0.5
    else:
        checks["video_present"] = False

    if audio:
        detail["audio"] = {
            "codec": audio.get("codec_name"),
            "sample_rate": int(audio.get("sample_rate") or 0),
            "channels": audio.get("channels"),
            "duration": float(audio.get("duration") or duration),
        }
        checks["audio_present"] = True
        checks["audio_rate"] = detail["audio"]["sample_rate"] == 48000
        checks["audio_channels"] = detail["audio"]["channels"] == 2
        delta = abs(detail["audio"]["duration"] - (detail["video"]["duration"] if video else duration))
        detail["av_delta_s"] = round(delta, 3)
        checks["av_sync"] = delta < 0.25
        detail["levels"] = volume_stats(args.file)
        checks["audio_not_silent"] = detail["levels"].get("max_volume", -999.0) > -60.0
    else:
        checks["audio_present"] = False

    detail["ok"] = all(checks.values())
    print(json.dumps(detail, indent=2))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(detail, indent=2) + "\n")
    return 0 if detail["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
