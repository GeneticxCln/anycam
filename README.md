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
- **Phone camera:** **Add → Phone camera (QR code)** shows a QR code; scan it with the
  phone, accept the one-time certificate warning, allow the camera, and the phone streams
  into the scene over WebRTC. Local network only, no app to install; a paired phone
  reconnects by opening the page again. If ufw or firewalld blocks the port, **Allow phones**
  in the same dialog opens it with one password prompt (firewalld over D-Bus). The TLS
  certificate and QR code come from the `cryptography` and `segno` packages
  (`anycam[phone]`), or else the `openssl` and `qrencode` commands. See
  [docs/PHONE_CAMERA.md](docs/PHONE_CAMERA.md).
- **Effects browser:** thumbnails drawn through the real pipeline, applied live with no
  rebuild; two stacking filter slots; copy, paste, clear.
  - About 70 filters and effects: colour looks, distortions, retro TV, touch-up, background
    blur and replace, spotlight, auto-frame.
  - **22 face masks** warped from the eyes and mouth (cyber, robot, skull, tiger, ...), plus
    emoji eyes and orbiting emoji.
  - **32 themed frames** (Christmas, Halloween, Cyber HUD, gold, neon, ...) and shaped windows.
  - **42 animations** (snow, hearts, confetti, fireworks, Matrix code, ...) and emoji rain.
    One animation at a time, swapped live; click it again to remove it.
  - **Virtual backgrounds** that replace the room behind you (Effects → Portrait: studio,
    aurora, sunset, ocean, and animated tunnel, starfield, plasma, water), and 171 bundled
    Noto emoji.
  - **Stage** tab: 19 static and 17 animated canvas backgrounds around the camera tiles
    (gradients, film grain, frosted glass, blueprint, topography, terrazzo; mesh glow, liquid
    chrome, thermal, bokeh, cosmos, fireflies, pixel grid, sine waves, wireframe, halftone,
    VHS, digital rain, ...). It frames the camera; it does not hide your room.
- **AI effects:** ONNX models through onnxruntime, on the CPU by default or the NVIDIA GPU
  (CUDA) when chosen in **Settings → AI effects**. Person cut-out uses the bundled MediaPipe
  model, or Robust Video Matting (soft edges and hair) as a one-click verified download. Slow
  model work runs beside the frame thread, so every effect fits a 1080p60 frame on the
  reference machine. Add your own segmentation model with `ANYCAM_SEG_MODEL`. See
  [docs/EFFECT_MODELS.md](docs/EFFECT_MODELS.md).
- **Outputs:** V4L2 virtual camera, MP4 recording (x264, NVENC, or VA-API on Intel/AMD), RTMP,
  audio mixing. Auto picks NVENC, then VA-API, then x264, after a real test encode.
- **Desktop:** a tray icon (show/hide, virtual camera, record, mute, scenes) and global
  shortcuts that work while other apps have focus, through the desktop portal (KDE,
  Hyprland): Ctrl+Alt+1…9 scenes, Ctrl+Alt+N/P next/previous, Ctrl+Alt+M mute, Ctrl+Alt+R
  record, Ctrl+Alt+V virtual camera.
- **Microphone:** noise suppression (off/low/moderate/high/very high, moderate by default)
  and automatic gain through GStreamer's `webrtcdsp`, applied to recordings and streams.

## Requirements

Developed on CachyOS (Arch), KDE/Wayland, NVIDIA, GStreamer 1.28. Used by the author with a
real webcam as a virtual camera in Discord and Telegram, and in other apps that list V4L2
cameras. The automated tests use synthetic sources and the offscreen GUI; raw measurements
are in [docs/EVIDENCE.md](docs/EVIDENCE.md).

- Python 3.11+ and GStreamer with `base good bad ugly libav opengl pulse`
- PyGObject (from your distro, not PyPI)
- PySide6 matching your system Qt (`pip install 'anycam[gui]'`), `numpy`, `Pillow`, `psutil`
- `onnxruntime` for portrait and face effects (`anycam[face]`; the models are bundled), or
  `onnxruntime-gpu[cuda,cudnn]` instead to run them on an NVIDIA GPU
- `xdg-desktop-portal` + PipeWire (screen capture), `ffmpeg`, `v4l-utils`
- `v4l2loopback` for the virtual camera (AnyCam sets it up on first launch, one password)

```bash
sudo pacman -S --needed python pyside6 python-gobject gst-plugins-{base,good,bad,ugly} \
  gst-libav gst-plugin-opengl ffmpeg v4l-utils pipewire-pulse xdg-desktop-portal \
  python-numpy python-pillow python-psutil
```

## Install and run

```bash
pip install anycam-1.2.1.tar.gz     # from the GitHub release, or from a checkout:
./scripts/setup.sh                  # creates .venv with system site-packages
./scripts/anycam-gui                # the studio (or: python -m anycam gui)
./scripts/install-desktop.sh        # optional: add it to the app menu
```

Headless demo, no window: `./scripts/demo.sh` (or `python -m anycam demo --help`).

### Virtual camera

On first launch AnyCam offers a one-time setup (one password prompt). It writes
`/etc/modprobe.d/anycam.conf` (`video_nr=10 card_label=AnyCam exclusive_caps=1`),
`/etc/modules-load.d/anycam.conf` (load at boot) and `/etc/udev/rules.d/70-anycam-vcam.rules`,
then reloads the driver. `exclusive_caps=1` is what Discord, Vesktop and Chrome need to
list the camera; the udev rule lets AnyCam re-announce the device without root each time it
starts writing. After that, opening AnyCam is enough: the camera is held from launch with a
standby picture and follows the preview. Remove the three files to undo.

If the session has no polkit agent running (some compositors never start one), AnyCam starts
the installed one for the prompt. **Settings → Diagnostics** shows the state and repeats the
setup; `scripts/enable-v4l2loopback.sh` does the same from a terminal.

### Recording and streaming

```bash
python -m anycam demo --seconds 60 --source camera --audio --record ~/Videos/AnyCam/s.mp4
python -m anycam demo --seconds 30 --source camera --rtmp rtmp://127.0.0.1:1935/live/anycam
```

Keys: `Ctrl+P` preview, `Ctrl+R` record, `Ctrl+Shift+V` virtual camera, `F5` snapshot,
`Ctrl+M` mute, `Ctrl+1..9` scenes, `Ctrl+Z` / `Ctrl+Shift+Z` undo / redo.

## Releases

**v1.2.1** is the current release (v1.2.0, v1.1.0, v1.0.3, v1.0.2, v1.0.1 and v1.0.0 came before); **Settings → About** shows the version and what
it runs on. Pushing a `vX.Y.Z` tag runs `.github/workflows/release.yml`: it builds the
tarball and wheel, checks the bundled assets, smoke-tests a clean install and publishes a
GitHub Release with `SHA256SUMS`. To release, bump `version` in `pyproject.toml` and
`VERSION` in `src/anycam/config.py`, commit, then `git tag vX.Y.Z && git push origin vX.Y.Z`.

## Privacy

Everything stays local: recordings go to `~/Videos/AnyCam/`, with no telemetry, accounts or
update checks. Privileged changes are the one-time virtual camera setup and, only if you
click **Allow phones**, a ufw or firewalld rule for TCP 8443; both go through the normal
password prompt.
The phone link runs only after you add a phone (or open a scene with one): HTTPS on your LAN
address, pairing by single-use QR code, phone secrets stored hashed, video never leaves the
network. The only internet request AnyCam can make is the optional model download, and
only when you click **Download** in Settings → AI effects; it fetches the file from the
model author's GitHub release and checks it against a pinned SHA-256. Screen capture asks
permission through the portal.

## Known limitations

- **Effects:** every effect fits a 1080p60 frame on the reference machine, but they still
  use CPU, on worker threads as well as per frame (`scripts/bench-effects.py` measures only
  the per-frame part). Robust Video Matting costs about 10 ms of CPU per refresh, about 14
  times a second. Live Auto-frame moves a GStreamer crop, which costs extra CPU while you
  move. Masks are 2D, built from five face landmarks: no depth and no expression tracking.
- **GPU effects:** the CUDA path needs `onnxruntime-gpu` with CUDA and cuDNN, which AnyCam
  does not install. It is covered by tests with a simulated CUDA provider only; it has not
  been run on real CUDA. Robust Video Matting is GPL-3.0, so it is a download, not bundled.
- **Microphone:** noise suppression removes steady noise (fans, hum, hiss), not keyboard
  clicks or voices; there is no echo cancelling. Changing it restarts the canvas, like
  changing the microphone does.
- **Canvas:** one virtual camera, and one canvas size and FPS for all layers; two filter slots
  per layer. A scene change rebuilds the graph (a hard cut) and starts a new recording segment.
- **Virtual camera:** with `exclusive_caps=1`, Chromium apps list AnyCam only while AnyCam is
  running. An app that was already open when AnyCam started may need its camera list
  reopened. The setup will not reload the driver while another app's v4l2loopback camera
  (OBS, for example) exists, or while an app has the camera open.
- **Phone camera:** video only (no phone audio). The phone shows a certificate warning the
  first time, and again whenever its browser forgets the exception: the certificate is
  self-signed because a LAN address cannot get a public one. Same Wi-Fi only, with no
  internet relay. Tested on one Android phone (Pixel 10) in Chrome; other phones and
  browsers are untested. Video is decoded on the CPU. Paired phones cannot be removed from
  the UI yet (delete `~/.local/state/anycam/phones.json`), and the phone remote control is
  not built.
- **Firewall:** ufw and firewalld are opened automatically. With plain nftables or another
  firewall, allow TCP 8443 yourself.
- **VA-API and firewalld:** both are covered by tests with simulated elements and a simulated
  D-Bus; neither has been run on real Intel/AMD hardware or a real firewalld yet.
- **Global shortcuts:** need the portal's GlobalShortcuts (KDE, Hyprland); elsewhere only the
  in-window shortcuts work.
- **Devices:** a camera that returns under a different `/dev/video*` name must be re-selected.
  Screen share asks the portal chooser each launch, and a static share can show `SLOW`.
- NVIDIA Broadcast and the Maxine SDK are not used (Windows-only app; restrictive SDK
  licence). The AI effects above run through onnxruntime instead, on the CPU or with CUDA.

## Develop

```bash
.venv/bin/python -m pytest tests     # 763 tests; the GUI and engine tests run offscreen
```

### Reproduce CI locally

`.github/workflows/ci.yml` runs the suite on Ubuntu 24.04 with system GStreamer and no
camera, loopback device, display or desktop portal, so the hardware-only tests skip there.
Run the same steps in a matching container:

```bash
docker run --rm -it -v "$PWD:/src" -w /src ubuntu:24.04 bash -c '
  apt-get update &&
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-pip python3-gi python3-gi-cairo \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-x \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly gstreamer1.0-libav ffmpeg \
    gir1.2-gst-plugins-bad-1.0 gstreamer1.0-nice openssl qrencode \
    libegl1 libgl1 libxkbcommon0 libxkbcommon-x11-0 \
    libxcb-cursor0 libxcb-xinerama0 &&
  gst-inspect-1.0 textoverlay &&
  python3 -m pip install --break-system-packages \
    "PySide6-Essentials>=6.6" numpy Pillow psutil pytest pytest-timeout onnxruntime &&
  PYTHONPATH=src QT_QPA_PLATFORM=offscreen PYTHONUNBUFFERED=1 \
    python3 -m pytest tests -v --timeout=180 --timeout-method=thread'
```

The `gst-inspect-1.0 textoverlay` line is deliberate: the Pango overlay lives in
`gstreamer1.0-x` on Ubuntu, not in `plugins-base`, and without it every AnyCam standby
chain fails. With `--timeout`, one blocked GStreamer call shows up as a single failed test
with its stack dump instead of a silent step. `act -j test` runs the workflow file itself
when Docker is available.

On a machine that does have the hardware, the same conditions can be simulated without
a container: `pytest tests --no-hardware` (or `ANYCAM_TEST_ENV=ci`) makes device discovery
report no cameras, no loopback node, no audio sources and no desktop portal. CI runs both
variants — `test` on the bare runner and `test-no-hardware` with the switch on — so a masked
local run has a reference for its pass/skip list.

More: [PARITY.md](PARITY.md) (feature matrix), [TODO.md](TODO.md),
[docs/UPGRADE_PLAN.md](docs/UPGRADE_PLAN.md) (native Linux upgrade plan), [docs/](docs/) (evidence,
regressions, phone camera), and the `NOTICE.md` files in `src/anycam/assets/models/` and
`src/anycam/assets/emoji/` (third-party assets and licences).

MIT licence. Emoji: Google Noto Emoji (Apache-2.0). Face detector: YuNet (Apache-2.0).
Segmentation: MediaPipe selfie segmentation (Apache-2.0).
