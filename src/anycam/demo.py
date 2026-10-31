"""Headless end-to-end demo: StreamCam -> compositor -> MP4 / v4l2sink."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from . import config
from .model import Layer, Project, default_project
from .pipeline import AnyCamEngine, verify_mp4


def _save_frame(sample, path: str) -> None:
    from PIL import Image
    import numpy as np

    buf = sample.get_buffer()
    caps = sample.get_caps().get_structure(0)
    width = caps.get_value("width")
    height = caps.get_value("height")
    ok, info = buf.map(Gst.MapFlags.READ)
    if not ok:
        return
    try:
        stride = width * 4  # preview caps are always RGBA
        arr = np.frombuffer(info.data, dtype=np.uint8).reshape(height, stride)
        arr = arr[:, : width * 4].reshape(height, width, 4)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(arr, "RGBA").save(path)
    finally:
        buf.unmap(info)


VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}


def _is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_SUFFIXES


def build_demo_project(source: str, canvas_w: int, canvas_h: int, fps: int) -> Project:
    project = default_project("AnyCam demo")
    project.output.canvas_w = canvas_w
    project.output.canvas_h = canvas_h
    project.output.fps = fps
    scene = project.scene
    if source != "camera":
        scene.layers = [l for l in scene.layers if l.kind != "camera"]
        if source == "testsrc":
            kind = "test"
        elif source.startswith(("http://", "https://", "rtsp://", "rtsps://")):
            kind = "network"
        elif _is_video(source):
            kind = "video"
        else:
            kind = "image"
        scene.add_layer(Layer("layer_source", kind, "Source", source=source, box=(0, 0, 1, 1), z=1))
    else:
        for layer in scene.layers:
            if layer.kind == "camera":
                layer.source = config.camera_device()
    # Brand strip bottom-left: proves a second generated overlay layer.
    scene.add_layer(Layer(
        "layer_brand", "text", "Brand",
        source="LIVE  ·  AnyCam", box=(0.035, 0.865, 0.22, 0.06),
        z=20, font_size=0.03, style="badge", color="#d7e3ea", background="#101418cc",
    ))
    return project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anycam demo", description=__doc__)
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--source", default="camera", help="camera, testsrc, or an image/video path")
    parser.add_argument("--record", default="", help="write an MP4 here")
    parser.add_argument("--vcam", action="store_true", help="also feed the v4l2loopback device")
    parser.add_argument("--audio", action="store_true", help="include the microphone in the recording")
    parser.add_argument("--preview", action="store_true", help="create the preview appsink")
    parser.add_argument("--shot", default="", help="save a composited frame as PNG")
    parser.add_argument("--shot-at", type=float, default=1.5)
    parser.add_argument("--json", default="", help="write the run report as JSON")
    parser.add_argument("--rtmp", default="", help="publish to this RTMP/RTMPS URL")
    parser.add_argument("--audio-file", default="", help="mix this audio file with the microphone")
    parser.add_argument("--mic-device", default="", help="PipeWire/Pulse source name")
    parser.add_argument("--mic-gain", type=float, default=0.0, help="microphone gain in dB")
    parser.add_argument("--mic-muted", action="store_true", help="mute the microphone input")
    parser.add_argument("--media-gain", type=float, default=0.0, help="media audio gain in dB")
    parser.add_argument("--playlist", default="", help="comma-separated image/video paths to rotate")
    parser.add_argument("--playlist-interval", type=float, default=3.0)
    parser.add_argument("--playlist-box", default="0.60,0.62,0.34,0.30",
                        help="normalised x,y,w,h for the playlist picture-in-picture")
    parser.add_argument("--shots-prefix", default="", help="save periodic PNGs to <prefix>NN.png")
    parser.add_argument("--shots-every", type=float, default=3.0)
    parser.add_argument("--brightness", type=float, default=0.0, help="-1..1")
    parser.add_argument("--contrast", type=float, default=1.0)
    parser.add_argument("--saturation", type=float, default=1.0)
    parser.add_argument("--blur", type=float, default=0.0, help="gaussian sigma")
    parser.add_argument("--chroma", default="", help="key colour, e.g. #00ff00")
    parser.add_argument("--chroma-similarity", type=float, default=0.35)
    parser.add_argument("--crop", default="", help="left,top,right,bottom normalised fractions")
    parser.add_argument("--width", type=int, default=config.CANVAS_W)
    parser.add_argument("--height", type=int, default=config.CANVAS_H)
    parser.add_argument("--fps", type=int, default=config.CANVAS_FPS)
    args = parser.parse_args(argv)

    project = build_demo_project(args.source, args.width, args.height, args.fps)
    if args.audio_file:
        project.audio.media_file = args.audio_file
    if args.mic_device:
        project.audio.mic_device = args.mic_device
    project.audio.mic_gain_db = args.mic_gain
    project.audio.mic_muted = args.mic_muted
    project.audio.media_gain_db = args.media_gain
    playlist_ids: list[str] = []
    if args.playlist:
        box = tuple(float(v) for v in args.playlist_box.split(","))
        for index, path in enumerate(p for p in args.playlist.split(",") if p.strip()):
            kind = "video" if _is_video(path) else "image"
            layer = Layer(f"playlist_{index}", kind, Path(path).stem, source=path,
                          box=box, z=25, visible=(index == 0))
            project.scene.add_layer(layer)
            playlist_ids.append(layer.id)
    if any((args.brightness, args.contrast != 1.0, args.saturation != 1.0,
            args.blur, args.chroma, args.crop)):
        source_layer = next((l for l in project.scene.layers
                             if l.kind in ("camera", "test", "video", "network", "image", "logo", "screen")), None)
        if source_layer is not None:
            fx = source_layer.effects
            fx.brightness = args.brightness
            fx.contrast = args.contrast
            fx.saturation = args.saturation
            fx.blur = args.blur
            if args.crop:
                source_layer.crop = tuple(float(v) for v in args.crop.split(","))
            if args.chroma:
                fx.chroma_enabled = True
                fx.chroma_key = args.chroma
                fx.chroma_similarity = args.chroma_similarity
    shot_state = {"done": False, "path": args.shot}
    shot_series = {"last": -1.0}

    def on_frame(sample):
        elapsed = time.monotonic() - engine._start_time
        if shot_state["path"] and not shot_state["done"] and elapsed >= args.shot_at:
            _save_frame(sample, shot_state["path"])
            shot_state["done"] = True
        if args.shots_prefix and args.shots_every > 0:
            index = int(elapsed // args.shots_every)
            if index > shot_series["last"] and elapsed >= args.shots_every:
                shot_series["last"] = index
                _save_frame(sample, f"{args.shots_prefix}{index:02d}.png")

    # --audio is the only thing that opens the microphone; an audio bed or a
    # stream must not capture it implicitly.
    project.audio.mic_enabled = bool(args.audio)
    engine = AnyCamEngine(
        project,
        record_path=args.record or None,
        vcam=args.vcam,
        preview=args.preview or bool(args.shot) or bool(args.shots_prefix),
        audio=args.audio or bool(args.audio_file),
        rtmp=args.rtmp or None,
        on_frame=on_frame,
    )
    print(json.dumps({"system": config.system_summary()}, indent=2))
    try:
        engine.build()
    except Exception as exc:  # noqa: BLE001 - surfaced as an explicit failure
        if engine.report.vcam_error:
            print(f"VCAM BLOCKED: {engine.report.vcam_error}", file=sys.stderr)
            return 3
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 2

    def on_tick(elapsed: float) -> None:
        if len(playlist_ids) > 1 and args.playlist_interval > 0:
            index = int(elapsed // args.playlist_interval) % len(playlist_ids)
            for position, layer_id in enumerate(playlist_ids):
                engine.set_layer_visible(layer_id, position == index)

    report = engine.run(args.seconds, tick_cb=on_tick, sample_cb=lambda rec: print(
        f"t={rec['t']:6.1f}s frames={rec['frames']:5d} fps={rec['fps']:6.2f} "
        f"cpu={rec['cpu_pct']} rss={rec['rss_mib']}MiB", flush=True))
    if shot_state["path"] and not shot_state["done"]:
        print("WARNING: no composited frame captured", file=sys.stderr)

    result = {"report": report, "system": config.system_summary()}
    if args.record:
        ok, info = verify_mp4(args.record, width=args.width, height=args.height, fps=args.fps,
                              min_seconds=max(1.0, args.seconds * 0.5))
        result["mp4"] = info
        result["mp4_ok"] = ok
        if not ok:
            report["error"] = report.get("error") or "MP4 verification failed"
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))

    if report.get("error"):
        print(f"FAILED: {report['error']}", file=sys.stderr)
        return 1
    if args.vcam and not report.get("vcam_active"):
        print(f"VCAM BLOCKED: {report.get('vcam_error')}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
