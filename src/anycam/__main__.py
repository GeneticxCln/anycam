"""AnyCam command line entry point."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config


def cmd_probe(_args) -> int:
    print(json.dumps(config.system_summary(), indent=2))
    return 0


def cmd_sources(_args) -> int:
    cameras = [cam.as_dict() for cam in config.list_video_devices()]
    audio = [src.as_dict() for src in config.list_audio_sources()]
    print(json.dumps({"cameras": cameras, "audio": audio}, indent=2))
    return 0


def cmd_doctor(_args) -> int:
    summary = config.system_summary()
    checks = {
        "gstreamer": summary["gstreamer"] != "unavailable",
        "camera": Path(summary["camera"]).exists(),
        "v4l2loopback": summary["vcam_available"],
        "ffprobe": config.has_command("ffprobe"),
        "pactl": config.has_command("pactl"),
    }
    summary["checks"] = checks
    print(json.dumps(summary, indent=2))
    return 0 if all(checks.values()) else 1


def cmd_demo(args) -> int:
    from .demo import main as demo_main

    return demo_main(args.demo_args)


def cmd_gui(args) -> int:
    if args.screenshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from .gui import run_gui, run_screenshot

    if args.screenshot:
        return run_screenshot(args.screenshot, args.seconds, source=args.source)
    return run_gui()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "demo":
        from .demo import main as demo_main
        return demo_main(argv[1:])
    if argv and argv[0] == "soak":
        from .soak import main as soak_main
        return soak_main(argv[1:])
    parser = argparse.ArgumentParser(prog="anycam", description="AnyCam virtual camera studio")
    parser.add_argument("--version", action="version", version=f"AnyCam {config.VERSION}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("probe", help="print the detected system/devices as JSON").set_defaults(func=cmd_probe)
    sub.add_parser("sources", help="list cameras and microphones").set_defaults(func=cmd_sources)
    sub.add_parser("doctor", help="check runtime prerequisites").set_defaults(func=cmd_doctor)

    # Listed for `anycam --help`; the early dispatch above forwards the real
    # soak arguments to its own parser.
    sub.add_parser("soak", help="long-run acceptance test (drops + memory)")

    demo = sub.add_parser("demo", help="headless camera -> composition -> MP4 / vcam")
    demo.add_argument("demo_args", nargs=argparse.REMAINDER)
    demo.set_defaults(func=cmd_demo)

    gui = sub.add_parser("gui", help="launch the standalone GUI")
    gui.add_argument("--screenshot", default="", help="render headless (Qt offscreen) and save a PNG")
    gui.add_argument("--seconds", type=float, default=3.0)
    gui.add_argument("--source", default="", help="use the headless demo scene (camera, testsrc, or a media path)")
    gui.set_defaults(func=cmd_gui)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
