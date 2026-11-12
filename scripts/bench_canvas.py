#!/usr/bin/env python3
"""Relative canvas benchmark: how much CPU each kind of layer costs.

Absolute frame rates depend on the machine, so every row is reported against
a baseline run made in the same process: ``cpu_ratio`` is that row's CPU seconds
per delivered frame divided by the baseline's.  A change that makes a layer kind
twice as expensive shows as a doubled ratio on any hardware.

    PYTHONPATH=src python3 scripts/bench_canvas.py --seconds 3 --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import psutil

from anycam.model import Effects, Layer, Project, Scene
from anycam.pipeline import AnyCamEngine

CANVAS = (640, 360)


def case_layers(name: str) -> list[Layer]:
    base = dict(source="smpte", box=(0, 0, 1, 1), z=1)
    if name == "baseline":
        return [Layer("a", "test", "A", **base)]
    if name == "four-layers":
        return [Layer(f"l{i}", "test", f"L{i}", source="smpte",
                      box=((i % 2) * 0.5, (i // 2) * 0.5, 0.5, 0.5), z=i + 1) for i in range(4)]
    if name == "rotated-cover":
        return [Layer("a", "test", "A", fit="cover", rotation=90, zoom=1.5, **base)]
    if name == "colour-look":
        return [Layer("a", "test", "A", effects=Effects(brightness=0.1, saturation=1.4,
                                                        color_effect="sepia"), **base)]
    if name == "rounded-corners":
        return [Layer("a", "test", "A", effects=Effects(filter="round-corners"), **base)]
    raise KeyError(name)


CASES = ("baseline", "four-layers", "rotated-cover", "colour-look", "rounded-corners")


def run_case(name: str, seconds: float) -> dict:
    project = Project(name=name)
    project.output.canvas_w, project.output.canvas_h = CANVAS
    project.output.fps = 30
    scene = Scene("scene_bench", "Bench")
    for layer in case_layers(name):
        scene.add_layer(layer)
    project.scenes = [scene]
    project.active_scene = scene.id
    engine = AnyCamEngine(project, preview=True, on_frame=lambda _sample: None).build()
    process = psutil.Process()
    engine.start()
    try:
        warm = time.monotonic() + 1.0
        while time.monotonic() < warm:
            engine.pump(40)
        frames0, cpu0 = engine._frame_count, sum(process.cpu_times()[:2])
        wall0 = time.monotonic()
        while time.monotonic() - wall0 < seconds:
            engine.pump(40)
        wall = time.monotonic() - wall0
        frames = engine._frame_count - frames0
        cpu = sum(process.cpu_times()[:2]) - cpu0
    finally:
        engine.stop()
    return {"case": name, "fps": round(frames / wall, 1), "frames": frames,
            "cpu_s_per_frame": round(cpu / max(1, frames), 5)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--json", help="also write the rows to this file")
    parser.add_argument("--cases", nargs="*", default=list(CASES), choices=CASES)
    args = parser.parse_args(argv)
    names = list(dict.fromkeys(["baseline", *args.cases]))
    rows = [run_case(name, args.seconds) for name in names]
    base = rows[0]["cpu_s_per_frame"] or 1e-9
    for row in rows:
        row["cpu_ratio"] = round(row["cpu_s_per_frame"] / base, 2)
        print(f"{row['case']:<16} {row['fps']:>6} fps  {row['cpu_ratio']:>5}x baseline CPU/frame")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
