"""Long-run acceptance test for AnyCam.

Runs the full graph (preview + optional virtual camera + recording), samples
CPU/RSS every second and evaluates the acceptance criteria: <1% dropped frames
and no monotonic memory growth after the warm-up window.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config
from .demo import build_demo_project
from .pipeline import AnyCamEngine, verify_mp4


def _rss_slope(samples: list[dict], warmup: float) -> dict:
    points = [(s["t"], s["rss_mib"]) for s in samples
              if s.get("rss_mib") is not None and s["t"] >= warmup]
    if len(points) < 3:
        return {"slope_mib_per_min": None, "growth_mib": None, "start": None, "end": None}
    n = len(points)
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    denom = sum((p[0] - mean_x) ** 2 for p in points) or 1.0
    slope_per_s = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points) / denom
    return {
        "slope_mib_per_min": round(slope_per_s * 60.0, 3),
        "growth_mib": round(points[-1][1] - points[0][1], 2),
        "start": points[0][1],
        "end": points[-1][1],
        "min": round(min(p[1] for p in points), 2),
        "max": round(max(p[1] for p in points), 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anycam soak", description=__doc__)
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--warmup", type=float, default=120.0)
    parser.add_argument("--source", default="camera")
    parser.add_argument("--record", default="")
    parser.add_argument("--vcam", action="store_true")
    parser.add_argument("--json", default="")
    parser.add_argument("--width", type=int, default=config.CANVAS_W)
    parser.add_argument("--height", type=int, default=config.CANVAS_H)
    parser.add_argument("--fps", type=int, default=config.CANVAS_FPS)
    args = parser.parse_args(argv)

    project = build_demo_project(args.source, args.width, args.height, args.fps)
    engine = AnyCamEngine(
        project,
        record_path=args.record or None,
        vcam=args.vcam,
        preview=True,
        audio=False,
    )
    engine.build()
    started = time.monotonic()
    print(f"soak: {args.seconds:.0f}s at {args.width}x{args.height}@{args.fps} "
          f"(warm-up {args.warmup:.0f}s)", flush=True)
    report = engine.run(args.seconds, sample_cb=lambda r: print(
        f"t={r['t']:7.1f}s fps={r['fps']:6.2f} cpu={r['cpu_pct']} "
        f"rss={r['rss_mib']}MiB", flush=True))
    elapsed = time.monotonic() - started
    memory = _rss_slope(report["samples"], args.warmup)

    mp4_ok = True
    mp4 = {}
    if args.record:
        mp4_ok, mp4 = verify_mp4(args.record, width=args.width, height=args.height,
                                 fps=args.fps, min_seconds=max(1.0, args.seconds * 0.9))
    recorded = {}
    recorded_drops = None
    if mp4_ok and mp4.get("duration"):
        expected = mp4["duration"] * args.fps
        recorded_drops = 100.0 * max(0.0, expected - mp4["frames"]) / max(expected, 1.0)
        recorded = {
            "frames": mp4["frames"],
            "duration": round(mp4["duration"], 3),
            "fps": mp4["recorded_fps"],
            "dropped_pct": round(recorded_drops, 3),
        }
    preview_drops_ok = report["dropped_pct"] < 1.0
    drops_ok = preview_drops_ok and (recorded_drops is None or recorded_drops < 1.0)
    memory_ok = (memory["slope_mib_per_min"] is not None
                 and memory["slope_mib_per_min"] < 1.0
                 and (memory["growth_mib"] or 0) < 50.0)
    vcam_ok = bool(report["vcam_active"])
    overall = "pass" if (drops_ok and memory_ok and mp4_ok and vcam_ok
                         and not report["error"]) else (
        "blocked" if (drops_ok and memory_ok and mp4_ok and not report["error"]) else "fail")

    result = {
        "soak": {
            "requested_seconds": args.seconds,
            "elapsed_seconds": round(elapsed, 2),
            "resolution": [args.width, args.height],
            "fps": args.fps,
            "camera_mode": report.get("camera_mode"),
            "encoder": report.get("encoder"),
            "vcam": report.get("vcam"),
            "vcam_active": report.get("vcam_active"),
            "vcam_error": report.get("vcam_error"),
        },
        "acceptance": {
            "overall": overall,
            "drops_ok": drops_ok,
            "memory_ok": memory_ok,
            "mp4_ok": mp4_ok,
            "vcam_ok": vcam_ok,
            "error": report["error"],
        },
        "report": report,
        "memory": memory,
        "recorded": recorded,
        "mp4": mp4,
    }
    print(json.dumps({k: v for k, v in result.items() if k != "report"}, indent=2))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, indent=2) + "\n")
    return 0 if overall == "pass" else (3 if overall == "blocked" else 1)


if __name__ == "__main__":
    sys.exit(main())
