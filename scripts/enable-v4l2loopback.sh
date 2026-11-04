#!/usr/bin/env bash
# Enable AnyCam's virtual camera with v4l2loopback.
#
# One root-only command. AnyCam writes no files under /etc; boot persistence
# is the explicit opt-in printed at the end.
#
# Reversible:
#   sudo modprobe -r v4l2loopback
#
# Keep exclusive_caps=0. AnyCam writes the loopback device while other
# applications open it as a camera; exclusive_caps=1 announces only one
# direction at a time and some consumers then fail to open it. If a
# Chromium-based app refuses to list the device, the note at the end has the
# one-line alternative.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -- "$0" "$@"
fi

if ! command -v v4l2-ctl >/dev/null; then
  echo "v4l2-ctl is missing. Install v4l-utils first (distribution package)." >&2
  exit 2
fi

if ! modinfo v4l2loopback >/dev/null 2>&1; then
  echo "The v4l2loopback kernel module is not installed." >&2
  echo "Arch/CachyOS: sudo pacman -S v4l2loopback-dkms v4l2loopback-utils" >&2
  echo "Debian/Ubuntu: sudo apt install v4l2loopback-dkms v4l2loopback-utils" >&2
  exit 3
fi

options='video_nr=10 card_label=AnyCam exclusive_caps=0'

if lsmod | grep -q '^v4l2loopback'; then
  echo "v4l2loopback is already loaded:"
  lsmod | grep '^v4l2loopback'
  if [ ! -e /dev/video10 ]; then
    echo "Reloading with $options ..."
    if ! modprobe -r v4l2loopback 2>/dev/null; then
      echo "Cannot reload: an application is using the loopback device." >&2
      echo "Close AnyCam/Discord, then run this script again." >&2
      exit 4
    fi
    modprobe v4l2loopback $options
  fi
else
  echo "Loading v4l2loopback ($options) ..."
  modprobe v4l2loopback $options
fi

echo "== devices"
v4l2-ctl --list-devices

if [ ! -e /dev/video10 ]; then
  echo "ERROR: /dev/video10 did not appear" >&2
  exit 1
fi

echo "AnyCam virtual camera ready at /dev/video10"
v4l2-ctl -d /dev/video10 --info | head -8

cat <<'EOF'

Next steps:
  1. AnyCam -> "Virtual camera" (the preview must be running).
  2. In Discord: Settings -> Voice & Video -> Camera, pick "AnyCam".
     If it is already open, restart Discord or refresh the camera list.

To load the device at boot (optional, explicit opt-in):
  echo v4l2loopback | sudo tee /etc/modules-load.d/anycam.conf
  echo 'options v4l2loopback video_nr=10 card_label=AnyCam exclusive_caps=0' \
    | sudo tee /etc/modprobe.d/anycam.conf

If a Chromium app still does not list "AnyCam", reload once with capture
capabilities exclusive:
  sudo modprobe -r v4l2loopback
  sudo modprobe v4l2loopback video_nr=10 card_label=AnyCam exclusive_caps=1
then start AnyCam's virtual camera before opening that app's camera list.
EOF
