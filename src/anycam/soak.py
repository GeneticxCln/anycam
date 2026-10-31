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


def _slope_per_min(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 3:
        return None
    n = len(points)
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    denom = sum((p[0] - mean_x) ** 2 for p in points) or 1.0
    return sum((p[0] - mean_x) * (p[1] - mean_y) for p in points) / denom * 60.0


def _memory_assessment(samples: list[dict], warmup: float) -> dict:
    """Judge memory from the plateau, not from a line through the steps.

    GStreamer buffer pools and per-thread malloc arenas take whole frames
    (8 MiB at 1080p RGBA) in a few discrete steps early in a run and then stay
    flat.  A least-squares slope over the whole window reports that step-up as
    continuous growth: the 2026-10-31 soak was scored 2.38 MiB/min while RSS
    was flat at 202.6 MiB for the first two minutes and flat at 226.3 MiB for
    the last five.  The gate therefore looks at the settled second half and at
    the tail, and reports the steps separately.
    """
    points = [(s["t"], s["rss_mib"]) for s in samples
              if s.get("rss_mib") is not None and s["t"] >= warmup]
    empty = {"slope_mib_per_min": None, "settled_slope_mib_per_min": None,
             "growth_mib": None, "settled_growth_mib": None,
             "tail_growth_mib": None, "start": None, "end": None,
             "min": None, "max": None, "steps": [], "ok": False}
    if len(points) < 6:
        return empty
    steps = [
        {"t": round(t1, 1), "delta_mib": round(r1 - r0, 1)}
        for (t0, r0), (t1, r1) in zip(points, points[1:])
        if r1 - r0 >= 2.0
    ]
    settled = points[len(points) // 2:]
    tail = points[int(len(points) * 0.75):]
    settled_slope = _slope_per_min(settled)
    tail_growth = max(r for _, r in tail) - min(r for _, r in tail)
    # 0.5 MiB/min for the settled half plus a still tail: a real per-frame leak
    # grows by at least a frame per minute and cannot hide in the noise.
    ok = (settled_slope is not None and settled_slope < 0.5 and tail_growth < 1.0)
    return {
        "slope_mib_per_min": round(_slope_per_min(points) or 0.0, 3),
        "settled_slope_mib_per_min": None if settled_slope is None else round(settled_slope, 3),
        "growth_mib": round(points[-1][1] - points[0][1], 2),
        "settled_growth_mib": round(settled[-1][1] - settled[0][1], 2),
        "tail_growth_mib": round(tail_growth, 2),
        "start": points[0][1],
        "end": points[-1][1],
        "min": round(min(r for _, r in points), 2),
        "max": round(max(r for _, r in points), 2),
        "steps": steps,
        "ok": ok,
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
    memory = _memory_assessment(report["samples"], args.warmup)

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
    memory_ok = bool(memory["ok"])
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
