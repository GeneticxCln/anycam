# Recipes that have never been built

The Flatpak manifest and the AppImage script here are parsed and cross-checked by
`tests/test_phase9_packaging.py`, but nobody has built them with their own tools, so
they are not supported and nothing promises they work. Supported and built in CI:
the `.deb` (`packaging/deb/build.sh`) and the Arch `PKGBUILD` (`packaging/arch/`).

To bring one back: build it on a machine that has the tool, fix what breaks, add a CI
job that builds it, then move it out of this folder.
