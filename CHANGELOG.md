# Changelog

All notable changes. Versions follow `pyproject.toml`; each `vX.Y.Z` tag publishes a GitHub Release.

## Unreleased

- Audit fixes (2026-11-11): see `docs/REGRESSIONS.md` for the list. Highlights: the frame overlay is no
  longer re-rendered on every colour-slider change, "Allow phones" works with ufw on Debian/Ubuntu, capture
  errors keep the device or file path, `~/` video paths work, network streams reconnect with backoff,
  the phone link caps connections and remakes its certificate when the LAN address changes, the recorder
  reports whether a file was closed cleanly, and shutdown always saves the project.
- `anycam doctor` and `scripts/setup.sh` check one shared list of required GStreamer elements; the new
  `anycam elements` command prints the missing ones.
- The virtual camera's on/off choice is remembered across launches.
- CI runs `ruff` (defect rules) and reports coverage.
- Added `CONTRIBUTING.md`, `SECURITY.md` and `NOTICE`.

## 1.3.2 - 2026-11-08

Review fixes (screen shares, CPU fallback, hidden GPU preview, frame loader) and a frame overlay that does
not need glycin.

## 1.3.1 - 2026-11-08

README with the known limitations spelled out.

## 1.3.0 - 2026-11-08

GPU canvas and preview, PipeWire camera output, tray icon and global shortcuts.

## 1.2.1 - 2026-11-07

The Stage and the virtual backgrounds are named apart.

## 1.2.0 - 2026-11-07

Microphone noise suppression, 1080p60 effects, optional CUDA for the AI models, Robust Video Matting cut-out
(downloaded on request).

## 1.1.0 - 2026-11-07

Phone camera by QR code and the one-time virtual camera setup.

## 1.0.3 / 1.0.2 / 1.0.1 - 2026-11-06 to 2026-11-07

Removed-layer disposal, camera reuse and usable camera modes (1.0.3); audit and live-check fixes (1.0.2);
first patch release (1.0.1).

## 1.0.0 - 2026-11-06

Effects, themed content, the About tab and the release workflow.
