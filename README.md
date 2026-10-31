# AnyCam

AnyCam is a Linux-native virtual camera studio: it composites one or more
webcams, screen capture, images, video files, overlays and effects into one
1920×1080@60 feed, then exposes that feed as a V4L2 virtual camera, an MP4
recorder and an RTMP stream.

AnyCam is an original clean-room application. It does not contain, call, bundle
or depend on ManyCam code, assets or trademarks, and it does not use Wine or
Proton.

**Current state: the end-to-end camera → composition → virtual camera → MP4 path
is working and measured on this machine.** The 2026-10-30 remediation re-verified
the virtual camera with a real V4L2 consumer and regenerated the GUI preview
evidence; see [docs/REGRESSIONS.md](docs/REGRESSIONS.md) for the before/after
record, [PARITY.md](PARITY.md) for the per-feature matrix and
[docs/EVIDENCE.md](docs/EVIDENCE.md) for raw numbers.

The 2026-10-31 audit (milestone M0) fixed the safety and geometry bugs listed in
`docs/REGRESSIONS.md` § "M0". Its raw artifacts — 989 MB including private
webcam frames, microphone recordings and a desktop screenshot — were **moved out
of this checkout** to `~/anycam-private-evidence/`; the paths in the tables
below therefore refer to that archive, not to files in the repository.

The GUI screenshot that used to be embedded here is in that archive too
(`00_skeleton/gui.png`), because it shows a real camera feed.

## Verified today

| Capability | Result | Evidence |
|---|---|---|
| StreamCam capture → 1080p60 compositor (MJPG) | PASS, 0.276% dropped in 10 s | `evidence/01_camera/report.json` |
| Text/badge overlay layers (three independent layers) | PASS | `evidence/00_skeleton/composite.png` |
| Synchronised H.264 MP4, ffprobe-valid (I420, H.264 High) | PASS, 1920×1080, 60/1, 611 frames | `evidence/01_camera/composite.mp4` |
| Standalone Qt GUI with live preview + snapshot | PASS (offscreen; live frame, F5 snapshot written) | `evidence/00_skeleton/gui.png`, `docs/REGRESSIONS.md` |
| Audio (mic + media file, mixed, AAC, gain/mute) | PASS, 0.078 s A/V delta, levels verified | `evidence/04_audio/av.mp4`, `verify.json` |
| RTMP publish to local receiver | PASS, 1280×720 H.264 High yuv420p + AAC, 8.23 s | `evidence/05_rtmp/received.flv` |
| Playlist / slideshow with PiP rotation | PASS, 5 rotating plates captured | `evidence/03_playlist/rotate_01..05.png` |
| Effects (brightness, contrast, saturation, blur, chroma) | PASS, before/after captures | `evidence/06_effects/*.png` |
| YuNet face overlay in the live compositor | PASS, real portrait tracked, 640×480 preview at 31 fps, pad alpha 1.0 | `evidence/09_face/report.json`, `preview.png` |
| Local RTSP network source | PASS, ffmpeg RTP → local RTSP server → `uridecodebin` → compositor | `evidence/08_rtsp/pytest.txt`, `junit.xml` |
| Two-camera composite (real + synthetic v4l2loopback) | PASS, 119 composited frames; both camera chains delivered 119 buffers | `evidence/10_multicam/report.json`, `junit.xml` |
| Camera modes, grid preset, placeholders and reconnect | PASS (offscreen; placeholder graph and reconnect timer exercised) | `evidence/11_camera_controls/`, `docs/EVIDENCE.md` §4.10 |
| 10-min 1080p60 soak (synthetic re-run) | PASS on drops/memory/stalls: 36,009 frames over 600.15 s (0.0% dropped), RSS 188.7→189.0 MiB (0.043 MiB/min), 12→12 FDs; vcam blocked (no `/dev/video10`) | `evidence/soak_10m/report.json` |
| Output isolation (M1 slice 1) | PASS, an RTMP server that stops reading cannot stall preview, recording or the virtual camera | `tests/test_m1_outputs.py::test_unresponsive_rtmp_cannot_stall_the_canvas` |
| Continuous MP4 across rebuilds | PASS, one persistent recorder spans rebuilds, including five scene switches, and writes one playable file | `tests/test_recorder.py`, `tests/test_gui.py::test_five_scene_switches_keep_one_recording`, `tests/test_gui.py::test_layer_edit_while_recording_keeps_one_continuous_file` |
| Persistent virtual-camera output | PASS (offscreen wiring), one session-owned `v4l2sink` survives engine rebuilds; real loopback consumer not exercised because `/dev/video10` is absent | `tests/test_vcam_output.py`, `tests/test_gui.py::test_vcam_output_survives_engine_restarts` |
| Automated tests | PASS, 138 passed + 1 hardware-gated multicam test | `./scripts/test.sh`; `docs/EVIDENCE.md` §2, §4.10 |
| V4L2 virtual camera | PASS — 720 frames in 12 s, and a `v4l2src` consumer read 60/60 buffers at 1920×1080 YUYV | `evidence/02_virtual_camera/report.json`, `consumer.txt`, `format.txt` |

## Output mode: 1920×1080@60 (MJPG)

The default canvas, output config and GUI are 1920×1080@**60**. The StreamCam
carries 60 fps at 1080p only in MJPG; its YUYV and NV12 modes stop at 30 fps
(verified with `v4l2-ctl --list-formats-ext`). `choose_camera_mode` therefore
selects `MJPG 1920x1080@60`. The rule is: the exact canvas size wins (a 60 fps
mode at another size does not displace 1080p); within that size the exact rate
wins, then the closest higher rate, then the closest lower rate; MJPG ranks
first. A fallback is reported as `(camera fallback from 60 fps)`, and a failed
`v4l2-ctl` probe is surfaced as `camera_probe_error` instead of silently
requesting MJPG.

`nvjpegdec` is installed but rejects this camera's MJPG stream
(`No valid frames decoded before end of stream`), so `jpegdec` is the default;
`ANYCAM_JPEG_DECODER=nv` opts in for other hardware. 1080p30 remains a
documented fallback only: run with `--fps 30` if a future machine cannot hold 60
without exceeding the drop budget.

## Multi-camera and ManyCam comparison

AnyCam is in the same category as ManyCam — a virtual camera studio that
composites several sources into one camera feed — but it is an original
Linux-native implementation, not a ManyCam clone, wrapper or compatibility
layer. It does not use ManyCam code, assets or trademarks.

### What works

* Add one **Camera** layer per device. With more than one capture node the
  Layers tab shows a V4L2 device picker; with one node it selects that node.
* Select any camera layer and use **Camera source** in the Layers tab to switch
  it to another `/dev/video*` device, or **Refresh** to rescan after plugging a
  camera in. The choice is stored as `Layer.source` in the project JSON.
* Each camera layer is an independent `v4l2src` branch with its own position,
  size, crop, opacity and effects, all composited into the same canvas.
  `tests/test_multicam.py` proves two camera layers can run at once with a
  real camera plus a synthetic v4l2loopback source.
* Every camera layer can request its own capture size and frame rate (Layers
  tab → **Capture size** / **Capture FPS**, `Auto` follows the layer box and
  canvas rate). The request is stored as `Layer.camera_width/height/fps`, so a
  720p camera can feed a small inset while another layer captures 1080p60; the
  mode is negotiated against that device's advertised formats.
* **Add two-camera scene** creates a ready-made side-by-side scene with two
  positioned camera layers (0–50% and 50–100% of the canvas) using the first
  two detected V4L2 devices. Rename, reorder or re-device either layer
  afterwards like any other layer.
* **Add camera grid...** opens a 2×2 slot dialog: assign any detected camera to
  each cell (or leave a cell empty) to build a three- or four-camera grid. The
  slots are configurable at creation time, and the resulting cells are normal
  layers you can drag, reorder or re-device later.
* A camera unplugged mid-stream keeps its box in the compositor as a generated
  **Camera disconnected** placeholder card (layer name and device path
  included), so the rest of the scene stays live and the layout does not jump.
  A preview started with a camera missing uses the same placeholder. AnyCam
  polls for the missing device node and rebuilds the real camera automatically
  when it returns. The status bar shows which device is disconnected or
  reconnected.
* AnyCam's own virtual-camera node is excluded from the input picker, so an
  accidental output-to-input feedback loop is not offered.

### Limits compared with ManyCam

| Area | AnyCam |
|---|---|
| Platform | Linux (V4L2, PipeWire, GStreamer); ManyCam is cross-platform |
| Multi-camera | Yes, layered camera sources; each layer owns one V4L2 device and can request its own resolution/FPS. Two-camera side-by-side preset and a configurable 2×2 grid for three or four cameras. No arbitrary N-camera layout editor |
| Camera controls | Per-layer capture size/FPS plus automatic pixel-format selection; no per-layer exposure, focus, white-balance or manual mode UI |
| Shared output | All camera layers share one canvas size, frame rate and virtual-camera output |
| Scenes | Multiple scenes and layers, but switching rebuilds the graph as a hard cut; no animated transitions |
| Effects | Colour, blur, chroma key, face overlay; no ManyCam-style effect library, filters or masks |
| Virtual cameras | One v4l2loopback output at a time, not multiple named virtual devices |
| Recording | Local H.264 MP4; a graph rebuild while recording starts a new segment rather than one continuous file |
| Streaming | RTMP/RTMPS output, no built-in platform presets or reconnect/bitrate UI |
| Hot-plug | A disconnected camera keeps its box with a generated placeholder card and reconnects when its device node returns; the rest of the scene stays live |
| Audio | One microphone plus one media file mixed to 48 kHz stereo; no per-camera audio |

USB bandwidth is the practical ceiling for simultaneous cameras: two
1080p60 MJPG feeds on the same controller may exceed the bus even when each
camera supports the mode. Use a lower resolution/frame rate, separate USB
controllers, or capture one feed over the network instead.

The two-camera integration test is opt-in so the normal suite never opens a
camera:

```bash
ANYCAM_TEST_CAMERA=/dev/video0 \
ANYCAM_TEST_V4L2_SINK=/dev/video10 \
PYTHONPATH=src .venv/bin/python -m pytest tests/test_multicam.py -v
```

`/dev/video10` must be a v4l2loopback node; the test feeds it a synthetic
`videotestsrc` and composites it beside `/dev/video0`. See
`evidence/10_multicam/` for the measured run.

## Requirements

Tested on CachyOS (Arch family), kernel 7.3, niri/Wayland, NVIDIA RTX 4060 Ti.

* Python 3.11+ (3.14 here)
* GStreamer 1.28 with `base`, `good`, `bad`, `ugly`, `libav`, `opengl`, `pulse`
* PyGObject (`python-gobject`), PySide6 6.11
* `xdg-desktop-portal` with a desktop backend and PipeWire for screen capture
* `numpy`, `Pillow`, `psutil`
* `ffprobe` (ffmpeg), `v4l-utils` (`v4l2-ctl`), `pactl` (PipeWire/PulseAudio)
* Optional: `onnxruntime` for the YuNet face overlay (also available as the
  `face` extra: `pip install -e '.[face]'`)
* Root once: `v4l2loopback` kernel module, for the virtual camera only

Arch/CachyOS package names:

```bash
sudo pacman -S --needed python python-pyside6 python-gobject gst-plugins-base \
  gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav gst-plugin-opengl \
  gst-plugins-base-libs ffmpeg v4l-utils libpulse pipewire-pulse pipewire \
  xdg-desktop-portal python-numpy python-pillow python-psutil
```

`v4l2loopback` is already present in the running CachyOS kernel
(`/lib/modules/$(uname -r)/kernel/drivers/media/v4l2-core/v4l2loopback.ko.zst`,
version 0.15.4). No DKMS build is required on this machine.

## Build and run

```bash
cd /path/to/anycam          # the checkout containing this README
./scripts/setup.sh          # creates .venv --system-site-packages, editable install, checks elements
```

The setup script uses `--no-deps` so it works offline; the runtime modules are
already system packages.

### Headless end-to-end demo (no window)

```bash
./scripts/demo.sh
# or explicitly:
PYTHONPATH=src .venv/bin/python -m anycam demo \
  --seconds 10 --source camera \
  --record evidence/01_camera/composite.mp4 \
  --preview --shot evidence/01_camera/composite.png --shot-at 2 \
  --json evidence/01_camera/report.json
```

It prints per-second frames/fps/CPU/RSS, writes an MP4, a PNG of the composited
frame, and a JSON report that includes an ffprobe check. Exit codes: `0` pass,
`1` pipeline/recording failure, `2` build failure, `3` virtual camera requested
but unavailable.

Use `--source testsrc` when no camera is available, or pass an image/video path.

### GUI

```bash
./scripts/anycam-gui
```

To add AnyCam to the desktop application menu (user-level install of the
entry and icon):

```bash
./scripts/install-desktop.sh
```

The GUI opens a normal window and is **not** launched by the automated checks.
To prove the GUI renders without touching a live desktop session:

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 -m anycam gui \
  --screenshot evidence/00_skeleton/gui.png --seconds 3 --source testsrc
```

The GUI has scene switching, layer add/remove/visibility, a per-layer camera
device picker with capture size/FPS controls, **Add two-camera scene** and
**Add camera grid...** presets, camera/test/text/logo/image/video/screen/network
sources,
interactive layer editing (select a layer in
the preview and drag it;
drag a corner or edge handle to resize; the Crop mode moves and trims the source
window; arrow keys nudge by 0.002, Shift+arrow by 0.02; X/Y/W/H and L/T/R/B
fields give exact placement; an opacity slider and Raise/Lower/Top/Bottom
controls change stacking and transparency without rebuilding; Ctrl/Shift-click
multi-selects layers so one drag or X/Y field moves them together; dragging
snaps to canvas/layer edges and centres, shows the nearest spacing in pixels,
and the snap toggle/tolerance in the Layers tab can turn that off or make it
stricter; Ctrl+Alt+arrows align selected layers to canvas edges, Ctrl+Alt+H/V
centre them, and Ctrl+D duplicates them; Ctrl+Z / Ctrl+Shift+Z or the
Undo/Redo buttons step through layout edits and layer add/remove/duplicate),
colour/blur/chroma and face-overlay controls (background replacement remains
disabled), record, virtual-camera and
snapshot buttons, an audio tab (device, gain, mute, media mixing), a playlist tab
(images/video, interval, loop), and an output tab for canvas size, FPS, V4L2
device and RTMP URL. Video and image files, RTSP/HTTP URLs and screen capture are
added from the Layers tab; screen capture goes through the xdg-desktop-portal
ScreenCast chooser and keeps the returned PipeWire session alive while the layer
exists. Playlist state and snap preferences are saved with the project, and
edits autosave shortly after they settle (and again on close).

Keyboard shortcuts: `Ctrl+P` start/stop preview, `Ctrl+R` record,
`Ctrl+Shift+V` virtual camera, `F5` snapshot, `Ctrl+M` mute the microphone,
`Ctrl+1…9` switch scene, `Ctrl+Alt+←↑→↓` align selected layers to the canvas,
`Ctrl+Alt+H`/`Ctrl+Alt+V` centre them, `Ctrl+D` duplicate them, and Ctrl+Z /
Ctrl+Shift+Z undo/redo layer edits. Arrow keys nudge the selected layer(s)
(Shift = 10×); in Crop mode they pan the crop window.

### Streaming and audio

```bash
# publish the composed feed to a local or remote RTMP endpoint
PYTHONPATH=src python3 -m anycam demo --seconds 30 --source camera   --rtmp rtmp://127.0.0.1:1935/live/anycam

# record camera + microphone + a music bed mixed together
PYTHONPATH=src python3 -m anycam demo --seconds 60 --source camera --audio   --audio-file music.wav --mic-gain 3 --record ~/Videos/AnyCam/session.mp4
```

`rtmps://` URLs are passed to the same `rtmpsink` (librtmp). Both inputs are
resampled to 48 kHz stereo and mixed by `audiomixer`; the AAC track is muxed
with the H.264 video so the MP4 stays synchronised.

### Virtual camera

One root-only command (reversible):

```bash
sudo modprobe v4l2loopback video_nr=10 card_label=AnyCam
# or: ./scripts/enable-v4l2loopback.sh

v4l2-ctl --list-devices           # expect "AnyCam" / /dev/video10
```

Then run AnyCam with the virtual camera branch:

```bash
PYTHONPATH=src python3 -m anycam demo --seconds 30 --vcam   # or use the GUI button
```

Undo it with:

```bash
sudo modprobe -r v4l2loopback      # ./scripts/disable-v4l2loopback.sh
```

No file in `/etc` is written by AnyCam. If you want the module after reboot,
that is an explicit opt-in: add `v4l2loopback` to
`/etc/modules-load.d/anycam.conf` and options to
`/etc/modprobe.d/anycam.conf`; remove both files to undo.

### Firefox check

With v4l2loopback loaded, one command drives a real headless Firefox through
`getUserMedia` and saves the browser screenshot, the captured composited frame
and a JSON report:

```bash
./scripts/verify-firefox.py --out evidence/02_virtual_camera
# PASS: anycam_enumerated, playing, resolution, frame_rate, frame_has_content
```

The page under test is `web/vcam-check.html`. It grants permission, selects the
camera labelled **AnyCam**, opens it at 1280×720@30 and measures the captured
frame. For a manual check, run `./scripts/serve-web.sh` and open
`http://127.0.0.1:8777/getusermedia.html`; the preview must show the composed
scene (AnyCam title + LIVE badges), not the raw StreamCam image.

## Architecture

```
camera layers (v4l2src → jpegdec/YUY2) ─┐
image/video/network layer ──────────────┤
text/logo RGBA overlay ─────────────────┤→ compositor 1920×1080@60 RGBA
screen (pipewiresrc) ───────────────────┘        │
                                     ├→ tee → preview appsink → Qt QImage
                                     ├→ tee → v4l2sink /dev/video10
                                     ├→ tee → x264enc → h264parse → mp4mux (I420, H.264 High)
                                     └→ tee → x264enc → h264parse → flvmux → rtmpsink
```

* `src/anycam/model.py` — plain-data Project/Scene/Layer/Effects, JSON presets.
* `src/anycam/geometry.py` — normalised boxes, crop and aspect fitting.
* `src/anycam/sources.py` — camera probing and source chains.
* `src/anycam/overlay.py` — original text/badge/logo rendering with Pillow.
* `src/anycam/portal.py` — xdg-desktop-portal ScreenCast client (session, node
  id and PipeWire fd).
* `src/anycam/face.py` — YuNet ONNX face detection for the live face overlay.
* `src/anycam/pipeline.py` — GStreamer graph, preview/vcam/record branches, EOS
  handling, reports and ffprobe verification.
* `src/anycam/gui.py` — standalone Qt window.
* `src/anycam/demo.py` — headless end-to-end runner.
* `spikes/` — feasibility spikes and their raw evidence.

Shutdown is explicit: sources are paused, an EOS event is pushed into each
compositor input pad, the pipeline waits for the recorder to finalise the MP4
`moov` atom, then the pipeline goes to `NULL`. The GUI window's `closeEvent`
runs the same shutdown before the process exits, so closing the window during a
recording still produces a playable file. Scene changes and effect changes
rebuild the graph; an in-progress recording is finalised as a playable segment
and a new segment starts (the status bar reports the segment count).

## Tests

```bash
./scripts/test.sh
# 138 passed, 1 skipped (hardware-gated multicam; 139 collected)
```

Covers geometry, model/JSON round-trips (including audio mixing fields, editor
state and playlist state), camera mode selection (1080p60 MJPG), overlay pixel
output, portal stream parsing, a real GStreamer
`testsrc → compositor → x264 → MP4` run verified with ffprobe, YuNet
preprocessing/decoding and detection of a real public-domain portrait, a local
RTSP server feeding an ffmpeg RTP stream through the network source layer, a
per-layer camera picker with capture-mode overrides, a hardware-gated
two-camera composite, and thirty-five offscreen GUI regressions (preview
delivery, snapshot, close-while-recording, screenshot deadline, interactive
move/resize/crop, keyboard/numeric placement, opacity/order, multi-select,
alignment/spacing guides, configurable snap, canvas alignment shortcuts,
undo/redo including layer add/remove/duplicate, image/video/network/screen
source layers, playlist loop/play-once, persisted playlist and snap state,
face-overlay tracking, camera device switching, two-camera preset, configurable
camera-grid preset, camera loss and placeholder/reconnect, autosave on close,
and editing while recording).

GitHub Actions runs the same suite on Ubuntu 24.04 with system PyGObject and
GStreamer (`.github/workflows/ci.yml`); the checked-in evidence was produced on
the CachyOS test machine.

## Privacy and system changes

* All recordings are local (`~/Videos/AnyCam/` by default).
* No telemetry, accounts, cloud services or update checks.
* The only privileged change is loading `v4l2loopback`; it is unloaded with one
  command and nothing is persisted unless you add module config yourself.
* Screen capture goes through `xdg-desktop-portal` ScreenCast + PipeWire, never
  a niri-specific API. The portal consent dialog is the normal user-facing
  permission prompt.

## Known limitations

* Screen capture requires the desktop's xdg-desktop-portal ScreenCast chooser
  and a live PipeWire session. The fd cannot be serialised, so a saved screen
  layer asks the portal for access again when preview/recording next starts.
  There is no checked-in live screen-capture artifact yet.
* Multi-camera is layer-based and shares one canvas/FPS/virtual-camera output;
  capture resolution/FPS can differ per layer, but there are no per-camera
  exposure/focus controls. See the multi-camera section above and
  `tests/test_multicam.py`.
* A camera unplugged while previewing is replaced by a static placeholder
  card until its device node returns; the other layers keep running. Reconnect
  polling is per device node, so a camera that returns under a different
  `/dev/video*` name must be re-selected manually. The reconnect state machine
  is tested offscreen, but a physical StreamCam cable-pull reconnect is not yet
  verified: `evidence/12_camera_reconnect/` records the blocked attempts and
  the command to complete it.
* RTSP and HTTP are exposed as GUI sources. RTSP is now covered by a local
  ffmpeg/RTP → RTSP server integration test; there is still no explicit
  backpressure handling for a slow network source.
* Image and video layers cannot have their source file replaced in place; remove
  and re-add the layer to switch files.
* Background blur/replacement is not implemented and its controls remain
  disabled. The face-tracked overlay is implemented with the bundled YuNet
  model and runs locally on preview frames; it follows the largest detected
  face and can use the frame or spotlight style.
* Rebuilding the graph while recording (scene/effect change, text/image/crop
  edit, or undo/redo) starts a new MP4 segment; segments are playable and
  reported, not merged into one file. Live transforms, opacity, ordering and
  canvas alignment do not rebuild, so they do not split a recording.
* Undo/redo covers layer layout and add/remove/duplicate in the active scene.
  Scene switches and playlist add/clear still reset the history.
* Playlist rotation is full-frame and uses one global interval; there is no
  per-item duration, order editor or picture-in-picture preset. All playlist
  media are instantiated in the graph even while hidden.
* Autosave writes the single project at `~/.config/anycam/project.json`; there
  is no Save As/Open dialog.
