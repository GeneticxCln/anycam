#!/usr/bin/env bash
# Build a .deb from the source tree (no network, no debhelper) and lint it.
# Usage: packaging/deb/build.sh [output-dir]
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
out="${1:-$root/dist}"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml" | head -1)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$out"

site="$stage/usr/lib/python3/dist-packages"
distinfo="$site/anycam-${version}.dist-info"
mkdir -p "$site" "$distinfo" "$stage/usr/bin" "$stage/usr/share/applications" "$stage/usr/share/metainfo" \
         "$stage/usr/share/icons/hicolor/512x512/apps" "$stage/usr/share/doc/anycam" "$stage/DEBIAN"
cp -r "$root/src/anycam" "$site/anycam"
find "$site" -name '__pycache__' -type d -prune -exec rm -rf {} +
cat > "$distinfo/METADATA" <<META
Metadata-Version: 2.1
Name: anycam
Version: $version
Summary: Linux-native virtual camera studio
License: MIT
META
find "$site" -type d -exec chmod 755 {} + && find "$site" -type f -exec chmod 644 {} +

cat > "$stage/usr/bin/anycam" <<'LAUNCH'
#!/usr/bin/python3
from anycam.__main__ import main
raise SystemExit(main())
LAUNCH
chmod 755 "$stage/usr/bin/anycam"
install -m644 "$root/anycam.desktop" "$stage/usr/share/applications/anycam.desktop"
install -m644 "$root/packaging/metainfo/io.github.geneticxcln.AnyCam.metainfo.xml" "$stage/usr/share/metainfo/"
install -m644 "$root/src/anycam/assets/anycam-logo.png" "$stage/usr/share/icons/hicolor/512x512/apps/anycam.png"
install -m644 "$root/LICENSE" "$stage/usr/share/doc/anycam/copyright"
install -m644 "$root/NOTICE" "$stage/usr/share/doc/anycam/NOTICE"
install -m644 "$root/CHANGELOG.md" "$stage/usr/share/doc/anycam/CHANGELOG.md"
date_rfc="$(LC_ALL=C date -Ru)"
cat <<CHANGELOG | gzip -9n -c > "$stage/usr/share/doc/anycam/changelog.gz"
anycam ($version) unstable; urgency=medium

  * Release $version (see /usr/share/doc/anycam/CHANGELOG.md for full notes).

 -- AnyCam contributors <anycam@users.noreply.github.com>  $date_rfc
CHANGELOG

size="$(du -sk "$stage" | cut -f1)"
cat > "$stage/DEBIAN/control" <<CONTROL
Package: anycam
Version: $version
Section: video
Priority: optional
Architecture: all
Maintainer: AnyCam contributors <anycam@users.noreply.github.com>
Installed-Size: $size
Depends: python3 (>= 3.11), python3-numpy, python3-pil, python3-psutil, python3-gi, python3-gi-cairo, gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0, gir1.2-gdkpixbuf-2.0, gir1.2-gst-plugins-bad-1.0, gstreamer1.0-plugins-base, gstreamer1.0-x, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-nice, gstreamer1.0-libav, ffmpeg, v4l-utils, python3-pyside6.qtwidgets | python3-pyside6, python3-pyside6.qtnetwork | python3-pyside6
Recommends: gstreamer1.0-plugins-ugly, gstreamer1.0-pipewire, gstreamer1.0-qt6, gir1.2-secret-1, v4l2loopback-dkms
Suggests: python3-onnxruntime, python3-cryptography, python3-segno, qrencode, openssl, poppler-utils, yt-dlp, libreoffice-impress
Homepage: https://github.com/GeneticxCln/anycam
Description: Linux-native virtual camera studio
 AnyCam mixes cameras, screens, images and overlays into scenes and publishes
 them as a virtual camera, a recording or a live stream.
CONTROL

package="$out/anycam_${version}_all.deb"
dpkg-deb --root-owner-group --build "$stage" "$package" >/dev/null
echo "built $package"
if command -v lintian >/dev/null; then
  lint_out="$(lintian --no-tag-display-limit "$package" 2>&1 || true)"
  if [ -n "$lint_out" ]; then
    printf '%s\n' "$lint_out"
  fi
  if printf '%s\n' "$lint_out" | grep -q '^E:'; then
    echo "lintian reported errors" >&2
    exit 1
  fi
else
  echo "lintian not installed: package contents only checked with dpkg-deb" >&2
fi
dpkg-deb --info "$package" | sed -n '1,12p'
