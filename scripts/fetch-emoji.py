#!/usr/bin/env python3
"""Rebuild ``src/anycam/assets/emoji`` from Google's Noto emoji.

The pictures are Apache-2.0 (the *fonts* in that repository are OFL and are not
used).  The commit is pinned, so the pack is reproducible, and every file's
SHA-256 is written next to it.  Run from the repository root::

    scripts/fetch-emoji.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from anycam import emoji  # noqa: E402

COMMIT = "99701e07d3d1fe1bdbcdb14964547f0fd592966f"
URL = ("https://raw.githubusercontent.com/googlefonts/noto-emoji/"
       f"{COMMIT}/2D/png/128/emoji_u{{code}}.png")
OUT = ROOT / "src" / "anycam" / "assets" / "emoji"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes, missing = {}, []
    for code, name, _category, _words in emoji.CATALOGUE:
        target = OUT / f"{code}.png"
        if not target.exists():
            try:
                with urllib.request.urlopen(URL.format(code=code), timeout=30) as reply:
                    target.write_bytes(reply.read())
            except OSError as exc:
                missing.append((code, name, str(exc)))
                continue
        hashes[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    (OUT / "SHA256.json").write_text(json.dumps(hashes, indent=1, sort_keys=True) + "\n")
    print(f"{len(hashes)} emoji in {OUT}")
    for code, name, why in missing:
        print(f"  missing {code} {name}: {why}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
