#!/usr/bin/env bash
# Run the automated test suite (geometry, model, overlays, GStreamer pipeline).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m pytest tests "$@"
fi
exec python3 -m pytest tests "$@"
