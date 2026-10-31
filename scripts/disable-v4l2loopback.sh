#!/usr/bin/env bash
# Undo the AnyCam virtual camera module load (does not touch packages).
set -euo pipefail
if lsmod | grep -q '^v4l2loopback'; then
  sudo modprobe -r v4l2loopback
  echo "v4l2loopback unloaded"
else
  echo "v4l2loopback is not loaded"
fi
