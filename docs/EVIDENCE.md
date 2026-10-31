# Evidence log

All runs below were performed on the target machine:

* CachyOS, Linux 7.3.0-4-cachyos, niri/Wayland, NVIDIA RTX 4060 Ti
* Logitech StreamCam at `/dev/video0`
* Python 3.14.8, PySide6 6.11.2, GStreamer 1.28.8
* Secure Boot disabled

No GUI window was opened on the live desktop. GUI evidence uses Qt's
`offscreen` platform; every camera run is headless.

## 1. Environment and build

```bash
./scripts/setup.sh
```

Creates `.venv --system-site-packages`, installs the package editable without
network dependencies, and checks the GStreamer elements. `anycam doctor`
reports:

```json
{
  "checks": {
    "gstreamer": true,
    "camera": true,
    "v4l2loopback": true,
    "ffprobe": true,
    "pactl": true
  }
}
```

This is the post-fix run (2026-10-30). The virtual camera is now exercised in
section 5.

## 2. Automated tests

```bash
python3 -m pytest tests -q
```

The original remediation run in `evidence/00_skeleton/pytest.txt` recorded
`71 passed, 1 skipped`. After the multi-camera additions (§4.10) the same
command reports `87 passed, 1 skipped` (88 collected). The skip is
`tests/test_multicam.py`, which is hardware-gated behind
`ANYCAM_TEST_CAMERA` and `ANYCAM_TEST_V4L2_SINK`; running it with both devices
set gives `88 passed`. The same suite is wired into `.github/workflows/ci.yml`
for Ubuntu 24.04; the counts here are from the CachyOS test machine.

Coverage:

* `tests/test_geometry.py` — normalised boxes, crop, aspect fitting.
* `tests/test_model.py` — default scene, JSON round-trip, presets, playlists,
  editor snap settings, scene playlist state, audio mixing fields, per-layer
  camera capture overrides, the two-camera side-by-side preset, and the
  configurable three/four-camera grid.
* `tests/test_portal.py` — xdg-desktop-portal ScreenCast stream/session parsing
  without a live desktop session.
* `tests/test_overlay.py` — RGBA badge rendering, transparent surround,
  aspect-preserving logo fit, pixel geometry, and the opaque "camera
  disconnected" placeholder card.
* `tests/test_face.py` — the bundled YuNet model detects a real public-domain
  portrait, and a deterministic fake session pins the raw-BGR preprocessing and
  the OpenCV-compatible cls/obj/bbox decode.
* `tests/test_rtsp.py` — starts a local RTSP control server plus an ffmpeg RTP
  producer, points a `network` layer at it, and asserts decoded preview frames
  from the real `uridecodebin`/`rtspsrc` path.
* `tests/test_multicam.py` — opt-in hardware test that feeds `videotestsrc`
  into a v4l2loopback node, composites it beside a real camera layer, and probes
  both source chains for delivered buffers.
* `tests/test_modes.py` — additionally covers continuous-format/loopback
  devices choosing YUYV, the V4L2 `YUYV` → GStreamer `YUY2` mapping, and how a
  layer's capture-size/FPS override turns into the negotiated device mode.
* `tests/test_pipeline.py` — a real `testsrc → compositor → x264 → mp4mux` run,
  ffprobe-verified at 640×360; preview frame delivery; live `set_layer_box`,
  opacity and zorder updates; placeholder-pad graphs used by camera reconnect;
  and rejection of a graph with no output branch.
* `tests/test_modes.py` — camera mode selection: 1080p60 picks MJPG, exact size
  wins, within a size an exact/higher rate beats 30, MJPG preferred at equal rate.
* `tests/test_gui.py` — thirty-five offscreen Qt regressions: preview frame
  delivery, snapshot PNG, close-while-recording finalisation, screenshot
  deadline, CLI screenshot exit, interactive layer move/resize/crop with real
  mouse events, arrow-key nudging, numeric box/crop fields, opacity/order
  controls, multi-select group moves, alignment/spacing guides, configurable
  snap, canvas-alignment shortcuts, undo/redo (including layer
  add/remove/duplicate), image/video/network/screen source layers, per-layer
  camera device switching, the two-camera preset, the configurable camera grid
  dialog, camera loss with placeholder frames and reconnect, playlist
  play-once/loop, persisted playlist and snap state, face-overlay tracking,
  autosave on close, and an edit during recording that finalises both MP4
  segments.

Screen capture is not claimed as verified here: the portal flow and parsing are
covered, but no live ScreenCast artifact has been captured on this machine.

## 3. Headless composited demo (synthetic source)

```bash
PYTHONPATH=src python3 -m anycam demo \
  --seconds 8 --source testsrc \
  --record evidence/00_skeleton/composite.mp4 \
  --preview --shot evidence/00_skeleton/composite.png --shot-at 2 \
  --json evidence/00_skeleton/report.json
```

Measured: 481 frames in 8.01 s, 60.04 fps, 0 dropped, MP4 1920×1080 h264
60/1, 492 frames, 8.2 s, ffprobe OK. Artifacts:

* `evidence/00_skeleton/composite.mp4`
* `evidence/00_skeleton/composite.png`
* `evidence/00_skeleton/report.json`
* `evidence/00_skeleton/demo.log`

## 4. StreamCam end-to-end (10 s)

```bash
PYTHONPATH=src python3 -m anycam demo \
  --seconds 10 --source camera \
  --record evidence/01_camera/composite.mp4 \
  --preview --shot evidence/01_camera/composite.png --shot-at 2 \
  --vcam --json evidence/01_camera/report.json
```

Measured (report):

| Metric | Value |
|---|---|
| camera mode | `MJPG 1920x1080@60` (selected automatically) |
| duration | 10.013 s |
| preview frames | 599 |
| preview fps | 59.821 |
| dropped (preview) | 1.8 (0.299%) |
| recorded MP4 | h264, 1920×1080, 60/1, 611 frames, 10.18 s, ffprobe OK |
| recorded rate | 60.0 fps, 0.0% shortfall |
| CPU | 301–467% (multi-core; x264 veryfast + 1080p60 compositor) |
| RSS | 282 → 313 MiB, flat after warm-up |

The composited frame shows the StreamCam feed plus three independent overlay
layers: `AnyCam` title, `LIVE` badge and the bottom `LIVE · AnyCam` brand strip.
Exit code was `3` in that historical run only because `--vcam` was requested and
the device was missing at the time; the recording itself is valid. Section 5 has
the post-fix virtual-camera run.

Artifacts: `evidence/01_camera/{composite.mp4,composite.png,report.json,demo.log}`.

### 4.1 Why 1080p60 is MJPG-only

`v4l2-ctl -d /dev/video0 --list-formats-ext` on the StreamCam advertises:

| Format | 1920×1080 rates |
|---|---|
| YUYV | 30, 24, 20, 15, 10, 7.5, 5 |
| MJPG | 60, 50, 30, 25, 20, 15, 10, 7.5, 5 |
| NV12 | 30, 25, 20, 15, 10 |

So 60 fps at 1080p exists only in MJPG. `sources.choose_from_modes` therefore
prefers the exact canvas size first (even if another size offers 60), then an
exact frame rate within that size, then the closest **higher** rate, then the
closest lower rate, ranking MJPG first. The camera probe is recorded verbatim
in the run report as `camera_mode: "MJPG 1920x1080@60"`; a fallback is reported
as `(camera fallback from 60 fps)` and a failed probe as `camera_probe_error`.

`nvjpegdec` is installed (`gst-inspect-1.0 nvjpegdec` is present) and was tried
as the MJPG decoder. It rejects this camera's MJPG stream:

```text
$ gst-launch-1.0 v4l2src device=/dev/video0 num-buffers=10 \
    ! image/jpeg,width=1920,height=1080,framerate=60/1 \
    ! nvjpegdec ! videoconvert ! fakesink
ERROR: from element /GstPipeline:pipeline0/nvjpegdec:nvjpegdec0:
No valid frames decoded before end of stream
```

`jpegdec` decodes the same caps cleanly, so it is the default.
`ANYCAM_JPEG_DECODER=nv` remains an opt-in for other cameras. This is why the
CPU cost is ~400%: the 1080p60 MJPG decode plus compositing and x264 encoding
are on the CPU. The measured result still meets the drop criterion.

### 4.2 Long-run stability at 1080p60

Both runs use the same graph as the acceptance test: preview + recording
(virtual camera excluded in these historical runs because the module was not
loaded yet; see the post-fix run in section 5).

| Run | Requested | Elapsed | Camera mode | Recorded | Drops | RSS slope |
|---|---|---|---|---|---|---|
| 60 s | 60 s | 64.24 s | MJPG 1920×1080@60 | 3612 frames / 60.2 s, 60.0 fps | 0.04% | **0.241 MiB/min** |
| 600 s | 600 s | 604.23 s | MJPG 1920×1080@60 | 36011 frames / 600.18 s, 60.0 fps | 0.0% recorded, 0.007% preview | **0.032 MiB/min** |

The 60 s soak artifact is `evidence/soak_60s/report.json` (raw per-second samples,
memory slope and ffprobe result). The 10-minute run is `evidence/soak_10m/`
(`report.json`, `run.log`, `soak.mp4`).

Final 10-minute acceptance numbers (`evidence/soak_10m/report.json`):

| Metric | Value |
|---|---|
| requested / elapsed | 600 s / 604.23 s |
| camera mode | `MJPG 1920x1080@60` |
| preview frames | 35998 |
| preview fps | 59.996 |
| preview drops | 0.007% (2.5 frames over 600 s) |
| recorded MP4 | H.264, 1920×1080, 60/1, 36011 frames, 600.18 s, ffprobe OK |
| recorded rate | 60.0 fps, 0.0% shortfall |
| CPU | 301–493% (multi-core; x264 veryfast + 1080p60 compositor) |
| RSS | 307.4 → 307.6 MiB, slope **0.032 MiB/min**, min 307.4, max 307.6 |
| encoder | x264enc (veryfast / zerolatency, I420) |
| acceptance | `drops_ok` true, `memory_ok` true, `mp4_ok` true; `vcam_ok` false (blocked) |

These soaks predate the virtual-camera fix; `/dev/video10` was absent at the
time, so they ran with preview + recording only. The post-fix 60 s run with
vcam + preview + recording is in section 5.


## 4.3 Audio (mic + media file, mixed)

```bash
PYTHONPATH=src python3 -m anycam demo \
  --seconds 10 --source camera --audio \
  --mic-device alsa_input.usb-046d_Logitech_StreamCam_13882645-02.analog-stereo \
  --audio-file evidence/04_audio/tone.wav --mic-gain 2 --media-gain -4 \
  --record evidence/04_audio/av.mp4 --preview \
  --shot evidence/04_audio/av.png --shot-at 3 \
  --json evidence/04_audio/report.json
./scripts/verify-av.py evidence/04_audio/av.mp4 --json evidence/04_audio/verify.json
```

Result (`evidence/04_audio/verify.json`, all checks true):

| Check | Value |
|---|---|
| video | H.264, 1920×1080, 60/1, 10.18 s |
| audio | AAC, 48000 Hz, 2 ch, 10.26 s |
| A/V delta | 0.078 s |
| levels | mean −27.7 dB, max −5.1 dB (`verify-av` audio_not_silent true) |

Gain verification (StreamCam mic, 5 s captures, `volumedetect`):

| Setting | mean | max |
|---|---|---|
| 0 dB | −43.3 dB | −29.1 dB |
| −12 dB | −53.1 dB | −30.4 dB |
| +12 dB | −30.2 dB | −17.0 dB |
| muted | −91.0 dB | −91.0 dB (digital silence) |

Implementation notes:

* `Gst.SystemClock` is used instead of the PulseAudio clock; otherwise the first
  composited frame (and the recording) is delayed ~1.5 s when audio is on.
* The mixer has a silent `audiotestsrc` anchor and `ignore-inactive-pads=True`
  so a waking USB microphone cannot stall the tee.
* Recording/RTMP branches buffer up to 5 s of encoded video after `h264parse`,
  so a slow mic cannot starve the preview.
* Audio EOS is pushed into each source pad so `mp4mux` finalises the `moov`
  atom instead of producing a truncated file.

Artifacts: `evidence/04_audio/{av.mp4,av.png,report.json,verify.json,run.log}`.

## 4.4 RTMP / RTMPS publish

```bash
# receiver
ffmpeg -listen 1 -i rtmp://127.0.0.1:1935/live/anycam -c copy -y evidence/05_rtmp/received.flv
# sender
PYTHONPATH=src python3 -m anycam demo --seconds 8 --source testsrc \
  --width 1280 --height 720 --fps 30 \
  --rtmp rtmp://127.0.0.1:1935/live/anycam --json evidence/05_rtmp/report.json
```

Received stream (`evidence/05_rtmp/received.flv`): 1280×720, H.264 High,
yuv420p, 30/1, AAC 48000 Hz stereo, 8.23 s. `rtmps://` URLs use the same
`rtmpsink` (librtmp). Artifacts: `evidence/05_rtmp/{received.flv,receiver.log,run.log,report.json}`.

## 4.5 Playlist / slideshow

```bash
PYTHONPATH=src python3 -m anycam demo --seconds 10 --source testsrc \
  --playlist evidence/03_playlist/plate1.png,evidence/03_playlist/plate2.png,evidence/03_playlist/plate3.png \
  --playlist-interval 2 --shots-prefix evidence/03_playlist/rotate_ --shots-every 2 \
  --record evidence/03_playlist/playlist.mp4 --json evidence/03_playlist/report.json
```

Result: 600 frames, 59.94 fps, 0.101% drops, MP4 1920×1080 60/1, 611 frames.
Five rotating frames were captured (`rotate_01..05.png`); the PiP (bottom-right,
0.34×0.30) cycles through the three plates in order. Hidden layers are built
into the compositor and toggled via pad alpha, so rotation is seamless.
Artifacts: `evidence/03_playlist/{plate1..3.png,rotate_01..05.png,playlist.mp4,report.json}`.

## 4.6 Effects

All captures at 1280×720, testsrc or a generated greenscreen clip, shot at t=2 s.

| Effect | Evidence | Measurement |
|---|---|---|
| baseline | `evidence/06_effects/baseline.png` | mean RGB [60, 39, 62] |
| brightness +0.3 | `evidence/06_effects/brightness.png` | mean [142, 121, 142] |
| contrast 1.8 | `evidence/06_effects/contrast.png` | mean [88, 67, 87] |
| saturation 0 (grayscale) | `evidence/06_effects/grayscale.png` | mean [46, 46, 46] |
| blur sigma 12 | `evidence/06_effects/blur.png` | mean gradient 2.66 → 1.05 |
| chroma key #00ff00 | `evidence/06_effects/chroma_before.png` → `chroma_after.png` | green plate (mean [21, 190, 2]) → black (mean [20, 4, 2]) |

`gaussianblur` is followed by a `videoconvert` so it links to the RGBA compositor
input; the `alpha` element uses `method=custom` (not `custom-rgb`).

## 4.7 Face overlay (YuNet)

The bundled `src/anycam/assets/models/yunet.onnx` now runs on preview frames and
drives a real compositor layer. The detector follows OpenCV's `FaceDetectorYN`:
resize to 640×640, raw 0..255 BGR (no mean/std), pad to 32, decode `cls`/`obj`
with `sqrt(cls*obj)`, and decode boxes as `(cell + offset) * stride` with
`exp(log_size) * stride`, then NMS.

| Metric | Result |
|---|---|
| Model / runtime | bundled YuNet ONNX, `onnxruntime` CPUExecutionProvider |
| Detector interval | 0.2 s (5 Hz) on the GUI thread |
| Test source | `tests/data/armstrong.jpg`, a public-domain NASA portrait looped locally as H.264 |
| Detection | score 0.9225, normalised box (0.3147, 0.1912, 0.1118, 0.1644) |
| Compositor face layer | visible, box (0.2910, 0.1564, 0.1592, 0.2342), pad alpha 1.0 |
| Run | 640×480@30 offscreen, 40 frames, 31.02 fps, `error: ""` |

Artifacts: `evidence/09_face/{report.json,preview.png,window.png}`. The preview
and window captures show the cyan frame around the detected face. The source
fixture and its public-domain provenance are documented in
`tests/data/README.md`.

## 4.8 Local RTSP server

`tests/test_rtsp.py` starts a minimal RTSP control server (OPTIONS, DESCRIBE,
SETUP, PLAY, TEARDOWN) and an ffmpeg `-f rtp` producer, then feeds
`rtsp://127.0.0.1:<port>/test` into a real `AnyCamEngine` with a `network` layer.
The engine uses the normal `uridecodebin`/`rtspsrc` chain; the test asserts no
pipeline error, decoded preview samples, and more than 10 composited frames.

Result: `1 passed in 6.76s`; the JUnit XML, pytest log and the local ffmpeg/
GStreamer versions are in `evidence/08_rtsp/`.

## 4.9 Two-camera composite

The per-layer camera picker stores one V4L2 device per camera layer. This
hardware-gated test uses the StreamCam at `/dev/video0` and feeds a live
`videotestsrc` into the v4l2loopback node at `/dev/video10`, then composes one
camera layer from each device at 640×480@30.

| Metric | Result |
|---|---|
| Camera chain 0 (`/dev/video0`) | 119 buffers |
| Camera chain 1 (`/dev/video10`, synthetic) | 119 buffers |
| Composited output | 119 frames, no pipeline error |
| Test duration | 5.988 s |

Artifacts: `evidence/10_multicam/{report.json,junit.xml,pytest.txt,environment.txt}`.
The test is skipped in the default suite and runs with:

```bash
ANYCAM_TEST_CAMERA=/dev/video0 ANYCAM_TEST_V4L2_SINK=/dev/video10 \
  PYTHONPATH=src .venv/bin/python -m pytest tests/test_multicam.py -v
```

## 4.10 Per-camera modes, presets, placeholders, and reconnect

These checks run offscreen and never open a physical camera. They pin the
multi-camera behaviour added on 2026-10-30:

| Check | Test | Result |
|---|---|---|
| Capture-size/FPS overrides survive a project round-trip | `tests/test_model.py::test_layer_camera_capture_overrides_roundtrip` | pass |
| A layer override resolves into the per-device mode negotiation | `tests/test_modes.py::test_layer_capture_overrides_resolve_into_a_mode` | pass |
| Two-camera preset places two 50%-wide frames side by side | `tests/test_model.py::test_two_camera_scene_is_side_by_side` | pass |
| GUI capture-size/FPS controls are per layer | `tests/test_gui.py::test_camera_picker_keeps_a_device_per_layer` | pass |
| Four configured grid slots become four equal quarter cells | `tests/test_model.py::test_camera_grid_scene_fills_four_slots` | pass |
| Empty grid slots are left as empty cells | `tests/test_model.py::test_camera_grid_scene_leaves_configured_slots_empty` | pass |
| An all-empty grid is rejected | `tests/test_model.py::test_camera_grid_scene_needs_a_device` | pass |
| Grid dialog prefills detected cameras and leaves the fourth empty | `tests/test_gui.py::test_camera_grid_dialog_prefills_detected_cameras` | pass |
| Grid button builds the selected three slots | `tests/test_gui.py::test_camera_grid_scene_button_builds_the_selected_slots` | pass |
| Grid requires at least three devices | `tests/test_gui.py::test_camera_grid_needs_three_devices` | pass |
| The two-camera button builds the side-by-side scene | `tests/test_gui.py::test_two_camera_scene_preset_positions_both_frames` | pass |
| Placeholder card is opaque and labeled | `tests/test_overlay.py::test_camera_placeholder_is_an_opaque_labeled_card` | pass |
| A skipped camera keeps a compositor pad with the placeholder | `tests/test_pipeline.py::test_skipped_camera_builds_a_placeholder_pad` | pass |
| A scene made only of skipped cameras still builds placeholders | `tests/test_pipeline.py::test_all_skipped_cameras_still_build_placeholders` | pass |
| A pipeline error keeps the box, shows the placeholder and starts the watcher | `tests/test_gui.py::test_camera_loss_drops_layer_but_keeps_preview_alive` | pass |
| An all-lost grid still renders four placeholders and stays WAITING | `tests/test_gui.py::test_all_lost_cameras_still_show_placeholder_frames` | pass |
| A returning device reconnects while other missing cameras stay out | `tests/test_gui.py::test_camera_reconnects_when_its_device_returns` | pass |

Targeted run: `17 passed in 2.51 s` (`evidence/11_camera_controls/pytest.txt`,
`junit.xml`, `report.json`). The offscreen screenshot
`evidence/11_camera_controls/grid_placeholders.png` shows a 2×2 grid with four
`Camera disconnected` cards and status `WAITING`. Full suite after the change:
`88 collected, 87 passed, 1 skipped` (the skip is `tests/test_multicam.py`,
which needs two real V4L2 nodes). The placeholder graph was also exercised with
a real `testsrc` preview; the remaining layers kept producing frames and the
engine reported no pipeline error.

**Physical pull — blocked.** Two live probes of `scripts/verify-camera-reconnect.py`
opened the StreamCam at `MJPG 1280x720@30` and printed `READY_FOR_UNPLUG`, but
no cable pull was performed before the timeout/stop. `evidence/12_camera_reconnect/`
records the attempts, the live `before.png`, and the exact command needed to
complete the test. The real unplug → placeholder → replug → reconnect sequence
remains unverified; do not cite it as a hardware pass yet.

## 5. Virtual camera (post-fix)

The original build segfaulted in `libgstvideo4linux2` because `v4l2sink` was
linked directly behind the compositor's tee and its caps query proxied back
through the tee. The shipped branch now ends in an `appsink`, and a separate
`appsrc → videoconvert → v4l2sink` pipeline feeds the loopback device, so every
caps query terminates locally. The module is loaded on this machine (no
`exclusive_caps=1`).

```bash
PYTHONPATH=src python3 -m anycam demo --seconds 12 --source testsrc \
  --vcam --preview --json evidence/02_virtual_camera/report.json
```

Result: 720 frames, 59.959 fps, `error: ""`, `vcam_active: true`,
`vcam_error: ""` (`evidence/02_virtual_camera/report.json`). A real consumer
read the device concurrently:

```bash
gst-launch-1.0 -q v4l2src device=/dev/video10 num-buffers=60 \
  ! videoconvert ! fakesink
# rc=0, 60 buffers
```

`v4l2-ctl --get-fmt-video` then reports `1920/1080`, `YUYV`
(`evidence/02_virtual_camera/format.txt`, `consumer.txt`).

### 5.1 Firefox re-drive (2026-10-30)

```bash
./scripts/verify-firefox.py --out evidence/02_virtual_camera
```

The script starts a throwaway HTTP server and headless Firefox 159 over
Marionette, feeds `/dev/video10` with a 1280×720@30 composite (colour bars plus
the AnyCam title, LIVE badge and brand strip), then loads `web/vcam-check.html`.
The page grants camera permission, enumerates two video inputs, selects
**AnyCam**, opens it through `getUserMedia` and measures one captured frame.

Result: **PASS** — all five checks true (`anycam_enumerated`, `playing`,
`resolution`, `frame_rate`, `frame_has_content`):

| Metric | Value |
|---|---|
| video inputs | `Logitech StreamCam`, `AnyCam` |
| selected device | `AnyCam` |
| track settings | 1280×720 @ 30 fps |
| captured canvas | 1280×720 |
| frame mean / std | 119.2 / 64.7 |

Artifacts:

* `evidence/02_virtual_camera/firefox.png` — browser page with the composited
  frame (the AnyCam title, LIVE badge and bottom brand strip are visible).
* `evidence/02_virtual_camera/firefox-frame.png` — the frame read back from the
  `<video>` element.
* `evidence/02_virtual_camera/firefox-report.json` — checks, track settings,
  Firefox version and artifact names.
* `evidence/02_virtual_camera/firefox.txt` — human-readable result of the run.
* `evidence/02_virtual_camera/{firefox-anycam.log,firefox-browser.log}` — raw
  pipeline and browser logs.

This supersedes the earlier manual enumeration capture in `verified.txt`; the
browser now both enumerates and reads the composited feed.

## 6. GUI (offscreen)

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 -m anycam gui \
  --screenshot evidence/00_skeleton/gui.png --seconds 3 --source testsrc
```

Produced a 1440×900 window render with a live composited frame; the run log
ends with `screenshot … frame=yes` (`evidence/00_skeleton/gui.log`). Artifact:
`evidence/00_skeleton/gui.png` (the synthetic source keeps the public screenshot
free of a personal camera image).

Additional offscreen checks (`docs/REGRESSIONS.md`): a 60 s preview run held
30.01 fps with `error: ""`; F5 wrote a PNG from the live frame; closing the
window 4 s into a recording produced a playable 4.33 s H.264 MP4; and a
content-free `--screenshot` run exits non-zero within the 5 s deadline instead
of hanging.

`tests/test_gui.py` now drives the real widget offscreen with `QMouseEvent`:
selecting a layer and dragging moves its normalised box, a corner handle changes
its width/height, and the crop mode trims and pans `Layer.crop` while preserving
the crop window size. Arrow keys nudge the selection (Shift = 10×), and numeric
X/Y/W/H and L/T/R/B fields apply and clamp exact values. The opacity slider and
Raise/Lower/Top/Bottom buttons update `Layer.alpha`/`Layer.z` through the live
compositor pads. A source-layer move/resize is mirrored onto the pad by
`AnyCamEngine.set_layer_box`; text/logo/image and crop edits rebuild the graph so
the overlay is re-rendered at the new size. Ctrl/Shift-click selects multiple
layers; a drag or X/Y field then translates the group, snapping its edges and
centres to the canvas or other layers and drawing the nearest spacing gap in
pixels; the Layers tab has a snap toggle and a 0–32 px tolerance. Ctrl+Alt+
arrows align selected layers to canvas edges, Ctrl+Alt+H/V centre them, and
Ctrl+D duplicates them. `LayoutHistory` snapshots the full layer list and backs
Ctrl+Z / Ctrl+Shift+Z (and the Undo/Redo buttons), including add/remove/duplicate;
undo/redo restarts the engine when it is running, so restored crops and text
render correctly. Scene switches and playlist structural changes clear history.
The recording test starts a real MP4 session, performs a crop edit (forcing a
rebuild), and verifies both the first and second segments with `ffprobe` after
closing the window.

## 7. Feasibility spikes

### Spike 1 — virtual camera + Firefox

**VERIFIED end-to-end** on 2026-10-30 (sections 5 and 5.1): AnyCam produced 720
frames in 12 s, a `v4l2src` consumer read 60/60 buffers at 1920×1080 YUYV, and a
headless Firefox 159 re-drive selected **AnyCam** at 1280×720@30 and captured
the composited overlays (`evidence/02_virtual_camera/firefox.png`).

### Spike 2 — appsink → Qt QVideoSink preview bridge

A 30 s run at 1920×1080@30 passed:

| Metric | Value |
|---|---|
| delivered | 908 / 915.8 expected |
| delivered fps | 29.746 |
| dropped | 7.8 (0.847%) |
| QVideoSink frames | 908 |
| frame conversion | 1.313 ms avg, 1.86 ms p95 |
| UI timer latency | 1.17 ms p95 |
| CPU | 33.2% avg |
| GPU | 13.8% avg |
| RSS | 369.6 → 377.7 MiB |

Artifacts: `spikes/evidence/02_preview_bridge/preview_bridge_30s.json` and
`preview_window.png`.

**Caveat:** 60-second runs were unstable at teardown in the spike harness
(`libshiboken: Internal C++ object (QVideoSink) already deleted`, followed by a
Python-finalisation abort). The 30 s measurement is valid, but the bridge is not
yet proven for 60 s continuous operation. AnyCam's shipped GUI avoids QML and
paints `QImage` frames in a QtWidgets widget, so the production preview path
does not depend on the unstable spike harness. The bridge itself does not need a
zero-copy `QAbstractVideoBuffer` wrapper at 1080p30 based on the 1.3 ms copy
cost, but the lifetime bug must be fixed before calling the QVideoSink variant
production-ready.

### Spike 3 — chroma key and face detection

Chroma key is now implemented and verified (section 4.6): `alpha` element with
`method=custom`, target RGB from `chroma_key`, tolerance via `noise-level`.
This spike originally found no usable face detector (`facedetect`,
`opencvfacedetect` and `cv2` were absent). That gap is closed by the bundled
YuNet ONNX model and `onnxruntime`; see section 4.7 for the live overlay result.

## 8. Reproducing everything

```bash
./scripts/setup.sh
./scripts/test.sh                       # 87 passed, 1 skipped
PYTHONPATH=src .venv/bin/python -m pytest tests/test_face.py tests/test_rtsp.py -v
ANYCAM_TEST_CAMERA=/dev/video0 ANYCAM_TEST_V4L2_SINK=/dev/video10 \
  PYTHONPATH=src .venv/bin/python -m pytest tests/test_multicam.py -v
./scripts/verify-firefox.py --out evidence/02_virtual_camera   # browser check
./scripts/demo.sh                       # camera or SOURCE=testsrc
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 -m anycam gui \
  --screenshot /tmp/anycam-gui.png --seconds 3 --source testsrc

# 60-second smoke soak, then the full 10-minute acceptance run
DURATION=60 WARMUP=10 OUT=/tmp/soak60 ./scripts/soak.sh
DURATION=600 WARMUP=120 OUT=evidence/soak_10m ./scripts/soak.sh
# add VCAM=1 once v4l2loopback is loaded (section 5)

# audio
PYTHONPATH=src python3 -m anycam demo --seconds 10 --source camera --audio \
  --mic-device alsa_input.usb-046d_Logitech_StreamCam_13882645-02.analog-stereo \
  --audio-file evidence/04_audio/tone.wav --record /tmp/av.mp4
./scripts/verify-av.py /tmp/av.mp4

# RTMP (needs a receiver: ffmpeg -listen 1 -i rtmp://127.0.0.1:1935/live/x -c copy out.flv)
PYTHONPATH=src python3 -m anycam demo --seconds 8 --source testsrc \
  --width 1280 --height 720 --fps 30 --rtmp rtmp://127.0.0.1:1935/live/x

# playlist
PYTHONPATH=src python3 -m anycam demo --seconds 10 --source testsrc \
  --playlist evidence/03_playlist/plate1.png,evidence/03_playlist/plate2.png,evidence/03_playlist/plate3.png \
  --playlist-interval 2 --shots-prefix /tmp/rot_ --shots-every 2
```

All measurements above are reproducible from these commands; the JSON reports
contain the raw per-second samples. Recorded-frame drops are computed from
ffprobe (`frames / duration` versus the requested rate), so a fast preview
callback cannot hide dropped output frames.
