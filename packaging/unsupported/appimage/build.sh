#!/usr/bin/env bash
# Build an AppImage with python-appimage (pip install python-appimage appimage-builder tools needed).
# Usage: packaging/unsupported/appimage/build.sh [python-version]   (default 3.12)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../../.." && pwd)"
py="${1:-3.12}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
command -v python-appimage >/dev/null || { echo "install python-appimage first: pip install python-appimage" >&2; exit 1; }

mkdir -p "$work/recipe"
sed -e '/^TryExec=/d' -e 's/^Exec=anycam gui$/Exec=anycam/' \
    "$root/anycam.desktop" > "$work/recipe/anycam.desktop"
cp "$root/src/anycam/assets/anycam-logo.png" "$work/recipe/anycam.png"
cat > "$work/recipe/entrypoint.sh" <<'ENTRY'
#! /bin/bash
# PyGObject (gi) comes from the host system Python; append system package dirs
# as a fallback so bundled packages stay preferred while host `gi` is importable.
host_paths=""
for dir in /usr/lib/python3/dist-packages /usr/lib/python3/site-packages /usr/lib64/python3.*/site-packages /usr/lib/python3.*/site-packages; do
  if [ -d "$dir" ]; then
    host_paths="${host_paths:+${host_paths}:}$dir"
  fi
done
if [ -n "$host_paths" ]; then
  export PYTHONPATH="${PYTHONPATH:+${PYTHONPATH}:}${host_paths}"
fi
if [ "$#" -eq 0 ]; then
  exec {{ python-executable }} -m anycam gui
fi
exec {{ python-executable }} -m anycam "$@"
ENTRY
printf 'numpy\nPillow\npsutil\nPySide6\n%s\n' "$root" > "$work/recipe/requirements.txt"
cp "$here/appimage.yml" "$work/recipe/metadata.yml" 2>/dev/null || true

# PyGObject and the GStreamer plugins come from the host: an AppImage cannot ship the
# distribution's plugin set, so AnyCam's first-run check lists what is missing.
python-appimage build app -p "$py" "$work/recipe"
mkdir -p "$root/dist"
mv AnyCam-*.AppImage "$root/dist/" 2>/dev/null || mv ./*.AppImage "$root/dist/"
