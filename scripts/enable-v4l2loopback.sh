#!/usr/bin/env bash
# Enable AnyCam's virtual camera with v4l2loopback.
#
# One root-only command. AnyCam writes no files under /etc; boot persistence
# is the explicit opt-in printed at the end.
#
# Reversible:
#   sudo modprobe -r v4l2loopback
#
# exclusive_caps=1 is required for Chromium-based apps (Discord, Chrome) to
# list the node at all: with the default 0 the device advertises capture and
# output together and their device enumeration filters it out.  AnyCam holds
# the device from launch, so the node keeps advertising capture even while no
# preview is running.
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

options='video_nr=10 card_label=AnyCam exclusive_caps=1'

if lsmod | grep -q '^v4l2loopback'; then
  echo "v4l2loopback is already loaded:"
  lsmod | grep '^v4l2loopback'
  caps=$(cut -d, -f1 \
    /sys/module/v4l2loopback/parameters/exclusive_caps 2>/dev/null || true)
  if [ ! -e /dev/video10 ] || [ "${caps:-N}" != "Y" ]; then
    echo "Reloading with $options ..."
    if ! modprobe -r v4l2loopback 2>/dev/null; then
      echo "Cannot reload: an application is using the loopback device." >&2
      echo "Close AnyCam and Discord, then run this script again." >&2
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
  1. Start AnyCam: it holds /dev/video10 from launch, even before the
     preview runs, so the node keeps advertising capture.
  2. In Discord: Settings -> Voice & Video -> Camera, pick "AnyCam".
     If it is already open, restart Discord or refresh the camera list.

To load the device at boot (optional, explicit opt-in):
  echo v4l2loopback | sudo tee /etc/modules-load.d/anycam.conf
  echo 'options v4l2loopback video_nr=10 card_label=AnyCam exclusive_caps=1' \
    | sudo tee /etc/modprobe.d/anycam.conf

Chromium-based apps (Discord/Chrome) only see the node while a writer is
streaming, so start AnyCam first: it holds the device from launch.  If the
module was already loaded with exclusive_caps=0, this script reloads it (which
needs AnyCam and Discord closed).
EOF
