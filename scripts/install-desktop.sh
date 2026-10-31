#!/usr/bin/env bash
# Install the AnyCam desktop entry and icon for the current user.
#
# The checked-in anycam.desktop is path-independent (Exec=anycam gui).  This
# script rewrites Exec/TryExec to the project launcher and installs the icon
# into the user's icon theme so desktop environments can resolve both.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor/256x256/apps"

launcher="$ROOT/scripts/anycam-gui"
if [ ! -x "$launcher" ]; then
  echo "launcher not found: $launcher" >&2
  exit 1
fi
icon="$ROOT/src/anycam/assets/anycam-logo.png"
if [ ! -f "$icon" ]; then
  echo "icon not found: $icon" >&2
  exit 1
fi

mkdir -p "$APPS" "$ICONS"
install -m 0755 "$icon" "$ICONS/anycam.png"

escaped="${launcher//\\/\\\\}"
escaped="${escaped//\"/\\\"}"
sed -e "s|^Exec=.*|Exec=\"$escaped\"|" \
    -e "s|^TryExec=.*|TryExec=$launcher|" \
    "$ROOT/anycam.desktop" > "$APPS/anycam.desktop"

if command -v desktop-file-validate >/dev/null; then
  desktop-file-validate "$APPS/anycam.desktop"
fi
if command -v update-desktop-database >/dev/null; then
  update-desktop-database "$APPS" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -q "$DATA/icons/hicolor" >/dev/null 2>&1 || true
fi

echo "installed $APPS/anycam.desktop"
echo "installed $ICONS/anycam.png"
