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
- Audio: a meter per input and for the output, the sound of video layers (volume, mute), music loop,
  shuffle and playlist, gate/compressor/EQ presets, sync offsets, a monitor output and a virtual microphone.
- Streaming: several destinations from one encode, each with status and reconnect (1–30 s backoff); YouTube,
  Twitch, Facebook and Kick presets; `rtmps://` and `srt://` addresses; SRT output; stream and recording
  bitrate; a disk guard that stops and saves a recording before the disk fills.
- AI effects and effect packs run in a worker process; a crash or hang there only costs the effect. Effect
  packs (`docs/EFFECT_PACKS.md`) and favourites.
- Content: PDF slides, video-site links through `yt-dlp`, MJPEG IP cameras, motion detection (switch scene,
  notification), a floating always-on-top panel.
- Phone remote (`/remote`) for scenes, mute and record over the paired HTTPS link; off until switched on.
- Packaging recipes (`packaging/`), `anycam check` and a first-run checklist, `scripts/compat-matrix.py`.
- Fixed: a silent video clip no longer stops looping; a GL conversion probe can no longer hang the
  diagnostics; the playlist timer follows the caller's clock.
- Layers have a fit mode (contain, cover, stretch), flips, a quarter-turn rotation, zoom and pan; zoom and
  pan change live. A layer box may leave the canvas (numeric entry), and RGB levels join the colour sliders
  (GPU canvas).
- Scenes arrive with a transition (fade, slide, wipe) and Studio mode stages the next scene for Cut or
  Transition. A "Rounded corners" filter and `scripts/bench_canvas.py`, a CPU-per-frame benchmark reported
  relative to a baseline run, were added.

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
