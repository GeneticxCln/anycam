#!/usr/bin/env bash
# Undo the AnyCam virtual camera module load (does not touch packages).
# Pass --clean-config to also remove AnyCam's /etc boot/udev config files.
set -euo pipefail

clean_config=0
for arg in "$@"; do
  case "$arg" in
    --clean-config) clean_config=1 ;;
    -h|--help)
      echo "Usage: $0 [--clean-config]"
      echo "Unloads v4l2loopback; with --clean-config also removes AnyCam's /etc files."
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

if lsmod | grep -q '^v4l2loopback'; then
  sudo modprobe -r v4l2loopback
  echo "v4l2loopback unloaded"
else
  echo "v4l2loopback is not loaded"
fi

etc_files=(
  /etc/modprobe.d/anycam.conf
  /etc/modules-load.d/anycam.conf
  /etc/udev/rules.d/70-anycam-vcam.rules
)
present=()
for path in "${etc_files[@]}"; do
  [ -e "$path" ] && present+=("$path")
done

if [ "${#present[@]}" -gt 0 ]; then
  if [ "$clean_config" -eq 1 ]; then
    sudo rm -f "${present[@]}"
    echo "Removed persistent config files: ${present[*]}"
  else
    echo "Note: persistent AnyCam config files remain and will reload v4l2loopback on next boot:"
    for path in "${present[@]}"; do
      echo "  $path"
    done
    echo "Run '$0 --clean-config' to remove them."
  fi
fi
