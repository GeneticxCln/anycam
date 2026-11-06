#!/usr/bin/env python3
"""Measure what each Python effect costs per frame.

Runs every portrait, face and framing processor on a real portrait at the
sizes you choose and reports milliseconds per frame, the frame rate that
allows, and whether it keeps up with the canvas rate.  Use it on the machine
that will stream::

    scripts/bench-effects.py                  # 720p and 1080p, 30 fps target
    scripts/bench-effects.py --sizes 1920x1080 --fps 60 --frames 40
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anycam import effect_thumbs, effects, processors  # noqa: E402


def bench(spec, size, frames: int, fps: int) -> tuple[float, float]:
    """(mean, worst) milliseconds per frame.  The mean is the sustained cost: an
    effect that refreshes a cache every few frames has a cheap median but a
    costlier average, and the average decides whether it keeps up."""
    sample = effect_thumbs.default_sample(size).convert("RGBA")
    processor = processors.make_processor(spec.processor, spec.default)
    for step in range(4):                      # warm the models and caches
        processor.process(sample.copy(), step / fps)
    times = []
    for step in range(frames):
        picture = sample.copy()
        started = time.perf_counter()
        # Advance the clock by one real frame: the effects cache work for a
        # fraction of a second, so a longer step would overstate their cost.
        processor.process(picture, 1.0 + step / fps)
        times.append((time.perf_counter() - started) * 1000.0)
    processor.close()
    return statistics.fmean(times), max(times)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sizes", default="1280x720,1920x1080")
    parser.add_argument("--fps", type=int, default=30, help="canvas rate to hold")
    parser.add_argument("--frames", type=int, default=40)
    args = parser.parse_args()
    sizes = [tuple(int(v) for v in item.split("x")) for item in args.sizes.split(",")]
    budget = 1000.0 / args.fps
    specs = [s for s in effects.available_filters() if s.processor]
    print(f"budget {budget:.1f} ms per frame at {args.fps} fps\n")
    print("milliseconds per frame: average (slowest single frame)")
    print(f"{'effect':<24}" + "".join(f"{w}x{h:<19}" for w, h in sizes))
    slow = []
    for spec in specs:
        cells = []
        for size in sizes:
            ms, worst = bench(spec, size, args.frames, args.fps)
            flag = "" if ms <= budget else " SLOW"
            cells.append(f"{ms:5.1f} (max {worst:4.0f}){flag:<5}")
            if ms > budget:
                slow.append((spec.label, size, ms))
        print(f"{spec.label:<24}" + "  ".join(cells))
    print()
    if slow:
        print(f"{len(slow)} combination(s) exceed the frame budget; the effect will")
        print("run at a lower frame rate than the canvas (the picture stays live).")
    else:
        print("Every effect fits the frame budget at these sizes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
