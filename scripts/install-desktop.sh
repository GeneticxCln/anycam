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
ICONS="$DATA/icons/hicolor/512x512/apps"
OLD_ICON="$DATA/icons/hicolor/256x256/apps/anycam.png"

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
# The logo is 512x512: it belongs in the 512x512 theme directory (an older
# install put it in 256x256, executable).
install -m 0644 "$icon" "$ICONS/anycam.png"
rm -f "$OLD_ICON"

# Desktop Entry Exec quoting: inside double quotes " ` $ \ need a backslash
# (a literal backslash needs four, the string escape applies first) and % is
# the field-code introducer.  sed could not do this: & and | in the path
# broke its replacement.
desktop_quote() {
  local in=$1 out="" ch i
  for ((i = 0; i < ${#in}; i++)); do
    ch=${in:i:1}
    case $ch in
      '\') out+='\\\\' ;;
      '"') out+='\"' ;;
      '`') out+='\`' ;;
      '$') out+='\$' ;;
      '%') out+='%%' ;;
      *) out+=$ch ;;
    esac
  done
  printf '%s' "$out"
}

quoted="$(desktop_quote "$launcher")"
while IFS= read -r line || [ -n "$line" ]; do
  case $line in
    Exec=*) printf 'Exec="%s"\n' "$quoted" ;;
    TryExec=*) printf 'TryExec=%s\n' "$launcher" ;;
    *) printf '%s\n' "$line" ;;
  esac
done < "$ROOT/anycam.desktop" > "$APPS/anycam.desktop"

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
