# AnyCam

**A Linux-native virtual camera studio.** Mix webcams, screen capture, images and video
with overlays, face masks, frames and animations into one 1080p60 picture, and send it to
Discord, Chrome, OBS, a recording or an RTMP stream. Clean-room: no ManyCam code or assets,
no Wine.

![AnyCam: a Christmas frame and snowfall over the program, with the effects browser](docs/screenshot.png)

## Features

- **Sources:** cameras (per-layer size and FPS), screen share (xdg-desktop-portal), image,
  video, RTSP/HTTP, text, ticker, lower third, animated GIF, drawing. Drag, resize, crop,
  snap, undo/redo, scenes, layouts, multi-view.
- **Effects browser:** thumbnails drawn through the real pipeline, applied live with no
  rebuild; two stacking filter slots; copy, paste, clear.
  - About 70 filters and effects: colour looks, distortions, retro TV, touch-up, background
    blur and replace, spotlight, auto-frame.
  - **22 face masks** warped from the eyes and mouth (cyber, robot, skull, tiger, ...), plus
    emoji eyes and orbiting emoji.
  - **32 themed frames** (Christmas, Halloween, Cyber HUD, gold, neon, ...) and shaped windows.
  - **42 animations** (snow, hearts, confetti, fireworks, Matrix code, ...) and emoji rain.
    One animation at a time, swapped live; click it again to remove it.
  - Animated backdrops and 171 bundled Noto emoji.
- **Outputs:** V4L2 virtual camera, MP4 recording (x264 or NVENC), RTMP, audio mixing.

## Requirements

Developed on CachyOS (Arch), KDE/Wayland, NVIDIA, GStreamer 1.28. Used by the author with a
real webcam as a virtual camera in Discord and Telegram, and in other apps that list V4L2
cameras. The automated tests use synthetic sources and the offscreen GUI; raw measurements
are in [docs/EVIDENCE.md](docs/EVIDENCE.md).

- Python 3.11+ and GStreamer with `base good bad ugly libav opengl pulse`
- PyGObject (from your distro, not PyPI)
- PySide6 matching your system Qt (`pip install 'anycam[gui]'`), `numpy`, `Pillow`, `psutil`
- `onnxruntime` for portrait and face effects (`anycam[face]`; the models are bundled)
- `xdg-desktop-portal` + PipeWire (screen capture), `ffmpeg`, `v4l-utils`
- `v4l2loopback` (root, once) for the virtual camera

```bash
sudo pacman -S --needed python pyside6 python-gobject gst-plugins-{base,good,bad,ugly} \
  gst-libav gst-plugin-opengl ffmpeg v4l-utils pipewire-pulse xdg-desktop-portal \
  python-numpy python-pillow python-psutil
```

## Install and run

```bash
pip install anycam-1.0.1.tar.gz     # from the GitHub release, or from a checkout:
./scripts/setup.sh                  # creates .venv with system site-packages
./scripts/anycam-gui                # the studio (or: python -m anycam gui)
./scripts/install-desktop.sh        # optional: add it to the app menu
```

Headless demo, no window: `./scripts/demo.sh` (or `python -m anycam demo --help`).

### Virtual camera

```bash
sudo modprobe v4l2loopback video_nr=10 card_label=AnyCam exclusive_caps=1
v4l2-ctl --list-devices             # expect "AnyCam" at /dev/video10
```

`exclusive_caps=1` is required for Discord and Chromium apps to list the camera. Then press
**Virtual cam** in the window. **Settings → Diagnostics** says whether the module is set up
for Chromium. `scripts/enable-v4l2loopback.sh` reloads a module loaded with other options.

### Recording and streaming

```bash
python -m anycam demo --seconds 60 --source camera --audio --record ~/Videos/AnyCam/s.mp4
python -m anycam demo --seconds 30 --source camera --rtmp rtmp://127.0.0.1:1935/live/anycam
```

Keys: `Ctrl+P` preview, `Ctrl+R` record, `Ctrl+Shift+V` virtual camera, `F5` snapshot,
`Ctrl+M` mute, `Ctrl+1..9` scenes, `Ctrl+Z` / `Ctrl+Shift+Z` undo / redo.

## Releases

**v1.0.1** is the current release (v1.0.0 was the first); **Settings → About** shows the version and what
it runs on. Pushing a `vX.Y.Z` tag runs `.github/workflows/release.yml`: it builds the
tarball and wheel, checks the bundled assets, smoke-tests a clean install and publishes a
GitHub Release with `SHA256SUMS`. To release, bump `version` in `pyproject.toml` and
`VERSION` in `src/anycam/config.py`, commit, then `git tag vX.Y.Z && git push origin vX.Y.Z`.

## Privacy

Everything stays local: recordings go to `~/Videos/AnyCam/`, with no telemetry, accounts or
update checks. The only privileged change is loading `v4l2loopback`. Screen capture asks
permission through the portal.

## Known limitations

- Python effects cost CPU per frame (`scripts/bench-effects.py` measures them). Masks are 2D,
  built from five face landmarks: no depth and no expression tracking.
- One virtual camera, and one canvas size and FPS for all layers; two filter slots per
  layer. A scene change rebuilds the graph (a hard cut) and starts a new recording segment.
- A camera that returns under a different `/dev/video*` name must be re-selected.
- Screen share asks the portal chooser each launch; a static share can show `SLOW`.
- No NVIDIA Broadcast effects (the SDK is Windows-only); NVENC is used for encoding only.

## Develop

```bash
.venv/bin/python -m pytest tests     # 676 tests; the GUI and engine tests run offscreen
```

More: [PARITY.md](PARITY.md) (feature matrix), [TODO.md](TODO.md), [docs/](docs/) (evidence,
regressions, phone camera), and the `NOTICE.md` files in `src/anycam/assets/models/` and
`src/anycam/assets/emoji/` (third-party assets and licences).

MIT licence. Emoji: Google Noto Emoji (Apache-2.0). Face detector: YuNet (Apache-2.0).
Segmentation: MediaPipe selfie segmentation (Apache-2.0).
