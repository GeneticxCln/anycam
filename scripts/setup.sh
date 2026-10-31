#!/usr/bin/env bash
# Reproducible AnyCam environment setup.
#
# AnyCam uses the distribution's PySide6 6.11 and GStreamer 1.28 through
# PyGObject, so the project venv is created with --system-site-packages.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null; then
  echo "python3 not found" >&2
  exit 1
fi

echo "== python: $($PYTHON -V)"
echo "== creating .venv (system site packages)"
if [ ! -d .venv ]; then
  "$PYTHON" -m venv --system-site-packages .venv
fi

echo "== installing AnyCam in editable mode (no network deps)"
.venv/bin/python -m pip install -q --no-deps --no-build-isolation -e .

echo "== checking Python modules"
.venv/bin/python - <<'PY'
import importlib
for name in ("anycam", "gi", "PySide6", "numpy", "PIL", "psutil"):
    importlib.import_module(name)
print("  anycam and runtime modules import")
PY

echo "== checking optional face overlay"
if .venv/bin/python -c 'import onnxruntime' >/dev/null 2>&1; then
  echo "  onnxruntime present (bundled YuNet overlay enabled)"
else
  echo "  onnxruntime missing (face overlay disabled)"
  echo "  install with: .venv/bin/pip install 'onnxruntime>=1.16'"
fi

echo "== checking GStreamer elements"
missing=0
for element in compositor appsrc imagefreeze v4l2sink v4l2src jpegdec \
               videoconvert videoscale videocrop videobalance gaussianblur \
               tee queue x264enc mp4mux h264parse pulsesrc audiomixer avenc_aac \
               flvmux rtmpsink textoverlay gdkpixbufoverlay; do
  if ! gst-inspect-1.0 "$element" >/dev/null 2>&1; then
    echo "  MISSING $element"
    missing=$((missing + 1))
  fi
done
if [ "$missing" -eq 0 ]; then
  echo "  all required elements present"
fi

echo "== checks"
.venv/bin/anycam doctor || true

cat <<'EOF'

Environment ready. Next:

  scripts/anycam-gui                 # standalone GUI (needs a desktop session)
  scripts/demo.sh                    # headless camera -> MP4 demo
  scripts/enable-v4l2loopback.sh     # one-time, needs sudo; creates /dev/video10

The virtual camera is optional for recording/preview and is the only step
that requires root. Undo it with scripts/disable-v4l2loopback.sh.
EOF
