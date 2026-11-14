# Changelog

All notable changes. Versions follow `pyproject.toml`; each `vX.Y.Z` tag publishes a GitHub Release.

## 1.5.0 - 2026-11-14

- **Keyring.** Stream keys (RTMP and extra destinations), SRT passphrases and stream ids, and network
  camera passwords are kept in the desktop keyring (Secret Service through libsecret), not in the project
  file. A saved project holds `${secret:<id>}` references; the app resolves them when it opens the file.
  Opening an older project moves its secrets across and scrubs the file, its `.bak` and its `.corrupt` copy.
  With no keyring the secret stays in the owner-only file, as before. A project opened on a computer whose
  keyring lacks the entry asks for the key again.
- **Crash safety.** Everything printed to stderr is also written to `~/.local/state/anycam/anycam.log`
  (masked, rotated), and a native crash leaves every thread's stack there. A marker file notices an unclean
  exit and the stage it happened in; the next start offers **safe mode** (`anycam gui --safe-mode`: CPU
  canvas, image preview, no virtual camera, phone link or AI effects; nothing is saved to the project).
  A project that will not load is moved to `set-aside/` (never deleted) and the last good `.bak` is offered.
  New: `anycam repair`. Diagnostics gets **Open log folder** and **Copy log**.
- **Encoder probing out of process.** `anycam encoders` encodes a few frames with every encoder in a child
  process; a crash or hang there cannot reach the app. NVENC is now judged by a real test encode, not by
  `nvidia-smi`. The answer is cached until the AnyCam version, GStreamer, the kernel or a graphics driver
  changes. Settings → Output lists only encoders that passed, says why the others did not, and can test again.
- **`anycam ctl`.** Drive the running window from a terminal or a key binding (`record`, `mute`,
  `scene <name>`, `state`, `list`, and every hotkey action). The instance socket is owner-only and the phone
  remote now runs through the same action table.
- **After Stop**, the window says how long the recording is, its size and picture size, and whether it has
  sound or was not closed cleanly, with **Play** and **Show in folder**.
- Housekeeping: test files are named after features, plans live in `plans/`, `docs/MANUAL.md` is the user
  guide, and the Flatpak and AppImage recipes (never built) moved to `packaging/unsupported/`. CI gains a
  real-keyring job and an Arch `makepkg` job (neither seen green on GitHub yet).

## 1.4.0 - 2026-11-12

- Audit fixes (2026-11-12, 11 items): the GPU preview no longer crashes on startup with an `ImportError`
  (`from . import gl_preview` had no module); switching scenes with the GPU preview no longer deadlocks the
  GUI thread (the preview sinks no longer wait for preroll, which stalled canvas startup; layer chains are
  stopped before they are unlinked, and layer audio unlinks before it stops); quitting
  with the GPU preview no longer blocks for >8 s (the shutdown flag is set after the engine step, even when
  it fails); motion detection and the floating panel get frames again under the GPU preview through a small
  CPU tap (on the GPU canvas it scales on the GPU and downloads a small texture: scaling full-size frames on
  the CPU segfaulted about every other run during an NVENC recording); recordings made with the GPU preview
  no longer lose their sound after the first few buffers (the audio mixer's output is pinned to 48000 Hz;
  an early first aggregation had made it 44100 Hz and then switched); a stale publisher can no longer flip a torn-down stream back to "live"; a reconnect now forces a
  keyframe from the retained encoder; effects no longer flash off while the worker starts or restarts (the
  last good frame is held); monitoring to the sink whose monitor source is the desktop capture no longer
  feeds back; a failed or timed-out `pdftoppm` render no longer leaves partial pages cached forever; and the
  PKGBUILD installs the AppStream metainfo only when the tarball has it and checks with `python-pytest`.
- **Changed:** AI effects now run in-process by default (measured 21–27 ms/frame through the worker against
  0–5 ms in-process at 1080p). Effect packs still always use a worker; **Settings → AI effects** can isolate
  all AI effects again.
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
- Effect packs always run in a worker process, and AI effects can (off by default); a crash or hang there
  only costs the effect. Effect packs (`docs/EFFECT_PACKS.md`) and favourites.
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
