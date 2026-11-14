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
    elements = config.check_elements()
    summary["elements"] = elements
    checks = {
        "gstreamer": summary["gstreamer"] != "unavailable",
        "gstreamer_elements": not elements["missing"],
        "camera": Path(summary["camera"]).exists(),
        "v4l2loopback": summary["vcam_available"],
        "ffprobe": config.has_command("ffprobe"),
        "v4l2-ctl": config.has_command("v4l2-ctl"),
        "pactl": config.has_command("pactl"),
    }
    summary["checks"] = checks
    try:
        from .engine.outputs.vcam import measure_conversion

        summary["vcam_conversion"] = measure_conversion(
            config.CANVAS_W, config.CANVAS_H, config.CANVAS_FPS)
    except Exception as exc:  # noqa: BLE001 - doctor must still report
        summary["vcam_conversion"] = {"error": str(exc)}
    print(json.dumps(summary, indent=2))
    return 0 if all(checks.values()) else 1


def cmd_firstrun(_args) -> int:
    """The first-run checklist as text; exit 1 when something required is missing."""
    from . import firstrun

    results = firstrun.checks()
    print(firstrun.summary_text(results))
    return 0 if firstrun.ready(results) else 1


def cmd_elements(_args) -> int:
    """List missing GStreamer elements; exit 1 when a required one is absent."""
    report = config.check_elements()
    for name in report["missing"]:
        print(f"MISSING {name}")
    for name, why in report["optional_missing"].items():
        print(f"optional {name} ({why})")
    if not report["missing"]:
        print("all required elements present")
    return 1 if report["missing"] else 0


def cmd_demo(args) -> int:
    from .demo import main as demo_main

    return demo_main(args.demo_args)


def cmd_encoders(args) -> int:
    from .engine import encoders

    if args.json:
        return encoders.probe_main()    # the child the app runs; one line per encoder
    answers = encoders.probe_results(block=True, force=args.again)
    for name in encoders.candidate_encoders():
        entry = answers.get(name, {})
        print(f"{name:12} {'ok' if entry.get('ok') else 'no: ' + entry.get('reason', '')}")
    return 0


def cmd_ctl(args) -> int:
    from . import ctl

    return ctl.main(args.action, " ".join(args.argument))


def cmd_repair(args) -> int:
    from . import repair

    problems = repair.check()
    if not problems:
        print("Nothing to repair: the project, hotkeys and phones files all read.")
        return 0
    for problem, moved in repair.repair(problems):
        print(f"{problem.kind}: {problem.path} {problem.why}; "
              + (f"set aside in {moved.parent}" if moved else "could not be moved"))
    return 0


def cmd_gui(args) -> int:
    if args.screenshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        if getattr(args, "ui", "manycam") == "classic":
            from .gui import run_gui, run_screenshot
        else:
            from .gui_many import run_gui, run_screenshot
    except ImportError as exc:
        if "PySide6" in str(exc) or getattr(exc, "name", "") == "PySide6":
            print("AnyCam's window needs PySide6 (matching your system Qt): "
                  "install your distribution's pyside6 package, or "
                  "pip install 'anycam[gui]'.\n"
                  f"({exc})", file=sys.stderr)
            return 2
        raise

    if args.screenshot:
        return run_screenshot(args.screenshot, args.seconds, source=args.source)
    return run_gui(safe_mode=bool(getattr(args, "safe_mode", False)))


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
    sub.add_parser("elements", help="list missing GStreamer elements").set_defaults(
        func=cmd_elements)
    sub.add_parser("check", help="the first-run checklist: what works and how to fix the rest"
                   ).set_defaults(func=cmd_firstrun)

    # Listed for `anycam --help`; the early dispatch above forwards the real
    # soak arguments to its own parser.
    encoders_cmd = sub.add_parser("encoders", help="test-encode with every encoder (what Auto may choose from)")
    encoders_cmd.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    encoders_cmd.add_argument("--again", action="store_true", help="ignore the cached answer")
    encoders_cmd.set_defaults(func=cmd_encoders)
    ctl_cmd = sub.add_parser("ctl", help="control the running window: record, mute, scene <name>, state, list")
    ctl_cmd.add_argument("action", help="an action id (try: anycam ctl list)")
    ctl_cmd.add_argument("argument", nargs="*", help="for scene: its name, number or id")
    ctl_cmd.set_defaults(func=cmd_ctl)
    sub.add_parser("repair", help="set aside project/hotkeys/phones files that no longer read"
                   ).set_defaults(func=cmd_repair)
    sub.add_parser("soak", help="long-run acceptance test (drops + memory)")

    demo = sub.add_parser("demo", help="headless camera -> composition -> MP4 / vcam")
    demo.add_argument("demo_args", nargs=argparse.REMAINDER)
    demo.set_defaults(func=cmd_demo)

    gui = sub.add_parser("gui", help="launch the standalone GUI")
    gui.add_argument("--screenshot", default="", help="render headless (Qt offscreen) and save a PNG")
    gui.add_argument("--seconds", type=float, default=3.0)
    gui.add_argument("--source", default="", help="use the headless demo scene (camera, testsrc, or a media path)")
    gui.add_argument("--ui", choices=("manycam", "classic"), default="manycam",
                     help="UI layout: manycam (default) or classic")
    gui.add_argument("--safe-mode", action="store_true",
                     help="start with the CPU canvas and image preview, no virtual camera, phone link "
                          "or AI effects (nothing is changed in the project)")
    gui.set_defaults(func=cmd_gui)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
