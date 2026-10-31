#!/usr/bin/env bash
# Headless end-to-end demo: StreamCam -> compositor -> preview frame + MP4.
# Safe to run without a desktop window; uses no GUI.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SECONDS_TO_RUN="${SECONDS_TO_RUN:-10}"
OUT="${OUT:-evidence/demo}"
mkdir -p "$OUT"

PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
if [ -x .venv/bin/python ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY=python3
fi

"$PY" -m anycam demo \
  --seconds "$SECONDS_TO_RUN" \
  --source "${SOURCE:-camera}" \
  --record "$OUT/composite.mp4" \
  --preview \
  --shot "$OUT/composite.png" \
  --shot-at 2 \
  --json "$OUT/report.json" \
  "$@"
