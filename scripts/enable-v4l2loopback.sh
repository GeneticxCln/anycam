#!/usr/bin/env bash
# Enable AnyCam's virtual camera with v4l2loopback.
#
# This is the only system change AnyCam needs and it is reversible:
#   sudo modprobe -r v4l2loopback
#
# NOTE: do NOT pass exclusive_caps=1. With exclusive_caps=1 the device is
# announced as capture- or output-only depending on who opens it first, which
# prevents Firefox from enumerating it as a camera while AnyCam is feeding it.
# The default (exclusive_caps=0) makes /dev/video10 support both capture and
# output simultaneously, which is what a virtual camera needs.
# To make it persistent, add a file under /etc/modules-load.d (documented in
# README.md); AnyCam itself never writes to /etc.
set -euo pipefail

if ! command -v v4l2-ctl >/dev/null; then
  echo "v4l2-ctl is missing. Install v4l-utils first (distribution package)." >&2
  exit 2
fi

if lsmod | grep -q '^v4l2loopback'; then
  echo "v4l2loopback is already loaded:"
  lsmod | grep '^v4l2loopback'
else
  echo "Loading v4l2loopback (requires sudo)..."
  sudo modprobe v4l2loopback video_nr=10 card_label=AnyCam
fi

echo "== devices"
v4l2-ctl --list-devices

if [ -e /dev/video10 ]; then
  echo "AnyCam virtual camera ready at /dev/video10"
  v4l2-ctl -d /dev/video10 --info | head -8
else
  echo "ERROR: /dev/video10 did not appear" >&2
  exit 1
fi
