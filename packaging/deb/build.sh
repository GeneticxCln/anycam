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
mkdir -p "$site" "$stage/usr/bin" "$stage/usr/share/applications" "$stage/usr/share/metainfo" \
         "$stage/usr/share/icons/hicolor/256x256/apps" "$stage/usr/share/doc/anycam" "$stage/DEBIAN"
cp -r "$root/src/anycam" "$site/anycam"
find "$site" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$site" -type d -exec chmod 755 {} + && find "$site" -type f -exec chmod 644 {} +

cat > "$stage/usr/bin/anycam" <<'LAUNCH'
#!/usr/bin/python3
from anycam.__main__ import main
raise SystemExit(main())
LAUNCH
chmod 755 "$stage/usr/bin/anycam"
install -m644 "$root/anycam.desktop" "$stage/usr/share/applications/anycam.desktop"
install -m644 "$root/packaging/metainfo/io.github.geneticxcln.AnyCam.metainfo.xml" "$stage/usr/share/metainfo/"
install -m644 "$root/src/anycam/assets/anycam-logo.png" "$stage/usr/share/icons/hicolor/256x256/apps/anycam.png"
install -m644 "$root/LICENSE" "$stage/usr/share/doc/anycam/copyright"
install -m644 "$root/NOTICE" "$stage/usr/share/doc/anycam/NOTICE"
gzip -9n -c "$root/CHANGELOG.md" > "$stage/usr/share/doc/anycam/changelog.gz"

size="$(du -sk "$stage" | cut -f1)"
cat > "$stage/DEBIAN/control" <<CONTROL
Package: anycam
Version: $version
Section: video
Priority: optional
Architecture: all
Maintainer: AnyCam contributors <noreply@example.com>
Installed-Size: $size
Depends: python3 (>= 3.11), python3-numpy, python3-pil, python3-psutil, python3-gi, gir1.2-gstreamer-1.0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-libav, v4l-utils, python3-pyside6.qtwidgets | python3-pyside6
Recommends: gstreamer1.0-plugins-ugly, gstreamer1.0-pipewire, v4l2loopback-dkms
Suggests: python3-onnxruntime, python3-cryptography, python3-segno, poppler-utils, yt-dlp
Homepage: https://github.com/GeneticxCln/anycam
Description: Linux-native virtual camera studio
 AnyCam mixes cameras, screens, images and overlays into scenes and publishes
 them as a virtual camera, a recording or a live stream.
CONTROL

package="$out/anycam_${version}_all.deb"
dpkg-deb --root-owner-group --build "$stage" "$package" >/dev/null
echo "built $package"
if command -v lintian >/dev/null; then
  lintian --no-tag-display-limit "$package" || true
else
  echo "lintian not installed: package contents only checked with dpkg-deb" >&2
fi
dpkg-deb --info "$package" | sed -n '1,12p'
