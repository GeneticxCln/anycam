#!/usr/bin/env bash
# Long-run acceptance test at the configured output mode (default 1080p60).
#   DURATION=600 WARMUP=120 OUT=evidence/soak_10m ./scripts/soak.sh
# Pass VCAM=1 only after v4l2loopback is loaded (see evidence/02_virtual_camera).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
DURATION="${DURATION:-600}"
WARMUP="${WARMUP:-120}"
OUT="${OUT:-evidence/soak}"
mkdir -p "$OUT"
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
PY=python3
[ -x .venv/bin/python ] && PY="$ROOT/.venv/bin/python"
VCAM_ARGS=()
[ "${VCAM:-0}" = "1" ] && VCAM_ARGS=(--vcam)
exec "$PY" -m anycam soak \
  --seconds "$DURATION" --warmup "$WARMUP" \
  --width 1920 --height 1080 --fps 60 \
  --record "$OUT/soak.mp4" \
  "${VCAM_ARGS[@]}" \
  --json "$OUT/report.json" "$@"
