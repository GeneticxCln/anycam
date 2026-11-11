#!/usr/bin/env bash
# Reproducible AnyCam environment setup.
#
# AnyCam uses the distribution's PySide6 (matching the system Qt) and
# GStreamer 1.28 through PyGObject, so the project venv is created with
# --system-site-packages.
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

# A checkout that was moved (the common `manycam linux` -> `anycam` rename)
# leaves stale console-script shebangs, but the venv interpreter normally
# still works.  Only recreate a venv whose interpreter is actually broken;
# the editable install below regenerates the console scripts.
if [ -d .venv ] && { [ ! -x .venv/bin/python ] \
     || ! .venv/bin/python -c 'import sys' >/dev/null 2>&1; }; then
  echo "== repairing stale .venv (interpreter does not run)"
  rm -rf .venv
  "$PYTHON" -m venv --system-site-packages .venv
fi

echo "== installing AnyCam in editable mode (no network deps)"
.venv/bin/python -m pip install -q --no-deps --no-build-isolation -e .

echo "== checking PySide6 / Qt runtime match"
# A user-site or older PySide6 wheel can load its own QtCore while the system
# libQt6DBus is built against a newer Qt.  Importing QtWidgets catches that
# before the first test collection fails with a versioned-symbol error.
if ! .venv/bin/python -c 'from PySide6.QtWidgets import QApplication' >/dev/null 2>&1; then
  qt_version="$(pkg-config --modversion Qt6Core 2>/dev/null || true)"
  echo "  PySide6 cannot load against this machine's Qt libraries:" >&2
  .venv/bin/python -c 'from PySide6.QtWidgets import QApplication' 2>&1 | sed 's/^/    /' >&2
  echo "  install a matching wheel into the venv, for example:" >&2
  echo "    .venv/bin/python -m pip install --upgrade 'PySide6${qt_version:+==$qt_version}'" >&2
  exit 1
fi
echo "  PySide6 imports against the system Qt"

echo "== checking Python modules"
.venv/bin/python - <<'PY'
import importlib
for name in ("anycam", "gi", "PySide6.QtWidgets", "numpy", "PIL", "psutil"):
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
# The list lives in anycam.config.REQUIRED_ELEMENTS (shared with `anycam doctor`).
if ! .venv/bin/anycam elements; then
  echo "  install the GStreamer plugin packages named above, then re-run this script" >&2
  exit 1
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
