#!/usr/bin/env python3
"""Measure which browsers see and open the AnyCam virtual camera.

    scripts/compat-matrix.py                 # needs AnyCam feeding the loopback device
    scripts/compat-matrix.py --fake-device   # self-test of the harness with Chromium's test camera
    scripts/compat-matrix.py --json out.json --markdown out.md

Exit code 0 when every browser that was found passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anycam import compat  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake-device", action="store_true",
                        help="use Chromium's built-in test camera (proves the harness, not AnyCam)")
    parser.add_argument("--no-firefox", action="store_true")
    parser.add_argument("--json")
    parser.add_argument("--markdown")
    args = parser.parse_args()
    rows = compat.matrix(fake=args.fake_device, include_firefox=not args.no_firefox)
    table = compat.as_markdown(rows)
    print(table)
    if args.json:
        Path(args.json).write_text(compat.to_json(rows))
    if args.markdown:
        Path(args.markdown).write_text(table + "\n")
    return 0 if all(row.state == "ok" for row in rows if row.state != "not found") else 1


if __name__ == "__main__":
    sys.exit(main())
