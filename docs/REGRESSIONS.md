# 2026-10-30 audit and remediation record

This file records the defects found in the full project audit, the fixes that
landed, and the commands that verify them. It is intentionally written as a
before/after record so the same regressions are not silently reintroduced.

Environment: CachyOS, Linux 7.3, niri/Wayland, GStreamer 1.28.8, Python 3.14.8,
PySide6 6.11.2, Logitech StreamCam at `/dev/video0`, v4l2loopback at
`/dev/video10` (card label `AnyCam`).

## Before

| Check | Result |
|---|---|
| `pytest tests` | 25 collected, 1 failed (`test_mjpg_preferred_at_same_rate`) |
| `demo --source testsrc --record … --preview` | worked |
| `demo --source testsrc --shot …` | `WARNING: no composited frame captured`; no PNG |
| `demo --source testsrc --vcam` | SIGSEGV (139) on every run, no report written |
| GUI offscreen preview | `appsink: None`, `preview.image: None` after 3 s |
| GUI default start | `not-linked` after ~0.2 s |
| `gui --screenshot` | never exited (killed at 45 s and 90 s) |
| Close GUI while recording | 0-byte MP4, `moov atom not found` |
| `./scripts/anycam-gui` | exit 127 (stale venv shebang) |

## Fixes

### Virtual camera (SIGSEGV)

`v4l2sink` was linked directly behind the compositor's `tee`. On GStreamer 1.28
its caps query proxies through the tee back into the compositor, recursing until
the stack overflows inside `libgstvideo4linux2`. The branch now ends in an
`appsink`, and a second pipeline (`appsrc → videoconvert → v4l2sink`) owns the
device. The bridge is zero-copy at the appsrc boundary and drops the newest
frame when the sink falls behind.

Verification:

```bash
PYTHONPATH=src python3 -m anycam demo --seconds 12 --source testsrc \
  --vcam --preview --json evidence/02_virtual_camera/report.json
# 720 frames, 59.959 fps, error "", vcam_active true

gst-launch-1.0 -q v4l2src device=/dev/video10 num-buffers=60 \
  ! videoconvert ! fakesink
# rc=0, 60 buffers
```

Artifacts: `evidence/02_virtual_camera/{report.json,consumer.txt,format.txt}`.
Three consecutive 10 s runs also completed with 600/600 frames each. A 60 s
run at 1920×1080@60 with preview + recording + vcam finished with 3600 frames,
59.996 fps, 0.006% preview drops, `record_ok: true`, `vcam_error: ""`, and an
ffprobe-valid 60.2 s MP4 (`evidence/02_virtual_camera/soak60.json`).

### GUI preview and the `not-linked` default

`start_engine()` built the engine with `preview=False` and `on_frame=None`, so
the appsink was never created and the only frame poll site was dead. The engine
now always builds the preview branch for the GUI and delivers samples through a
single `new-sample` → `deliver_frame()` path on the caller's thread. The GUI
starts with `preview=True, on_frame=self._on_frame`, converts RGBA with a
`width * 4` stride, and emits `frame_ready` for the widget. `build()` now raises
a clear error if no output branch was requested instead of leaving the
compositor unlinked.

Verification: `gui --screenshot` reports `frame=yes`; a 60 s offscreen preview
run held 30.01 fps with `error: ""`; F5 wrote a PNG from the live frame.

### Screenshot mode hang

`--screenshot` relied on the `_tick` timer to quit, so a dead engine meant no
exit. A deadline timer now grabs/saves/quits independently, headless mode never
opens a modal dialog, and `run_screenshot` exits non-zero when no live frame was
rendered. The broken-project path (missing logo asset) exits 1 in 6.6 s; the
normal path exits 0 in 3.8 s.

### Close-while-recording data loss

`run_gui` called `os._exit` immediately after `app.exec()`; no finalisation hook
existed. `AnyCamWindow.closeEvent` (and `aboutToQuit`) now stops the timer and
runs the engine's EOS/finalise path before the event is accepted. Closing 4 s
into a recording produced an 84,860-byte H.264 MP4 with a 4.33 s duration that
`ffprobe` reads cleanly.

### Snapshots and CLI frame capture

The same frame path fixes `--shot` and `--shots-prefix`: the demo now receives
samples from `pump()` and writes PNGs. Verified at 640×360:
`--shot` wrote a 640×360 RGBA PNG; `--shots-prefix` wrote four PNGs at the
requested two-second cadence.

### Tests, camera mode, and stale paths

* `PREFERRED_FORMATS` now ranks `MJPG` first, matching the README and the
  failing test. `pytest` is green (71 passed, 1 hardware-gated multicam skip).
* The editable install, console script, desktop entry, and README build path
  were repaired. `anycam.desktop` is path-independent; `scripts/install-desktop.sh`
  expands the launcher path and installs the icon.
* `timestamped_name()` now includes milliseconds so graph rebuilds cannot
  collide on a filename.

### Recording sessions

`Record` creates one session name; each graph rebuild starts a new
`-segN.mp4` segment and the status bar reports the count when recording stops.
Layer visibility toggles use `set_layer_visible()` and never rebuild the graph
or end the recording. Scene switches still rebuild (hard cut) and therefore
create a new segment; segments are playable and announced rather than one
merged file.

### Interactive layer transform and GUI regressions

Layer boxes were fixed at creation: `geometry.py` exposed drag-handle helpers but
no widget used them. `PreviewWidget` now maps pointer positions through the
letterboxed canvas into normalised coordinates, hit-tests the visible layers,
and edits `Layer.box` (move, eight resize handles) or `Layer.crop` (trim, pan)
in two explicit modes. A selection outline and crop-window dimming are painted
over the live frame. Arrow keys nudge by 0.002 (Shift = 0.02), and numeric
X/Y/W/H and L/T/R/B fields apply the same model with clamping. An opacity
slider writes `Layer.alpha` through `set_layer_alpha`, and Raise/Lower/Top/Bottom
normalise `Layer.z` then call `refresh_layer_order`; both are live pad updates,
so they do not rebuild or end a recording. Source-layer moves/resizes also
update the compositor pad through `AnyCamEngine.set_layer_box`, so the effect is
visible during the drag; text/logo/image and crop edits restart the graph on
release to re-render or rebuild the `videocrop` chain. Ctrl/Shift-click
multi-selects layers: a group drag or X/Y field edit translates all selected
boxes, and configurable alignment guides snap the group bbox to
canvas/other-layer edges and centres while showing the nearest spacing gap in
pixels; the Layers tab has a snap toggle and a 0–32 px tolerance. Ctrl+Alt+
arrows align selected layers to canvas edges, Ctrl+Alt+H/V centre them, and
Ctrl+D duplicates them. `LayoutHistory` snapshots the full layer list; drag,
nudge, numeric fields, opacity, order, reset-crop, add, remove and duplicate
each record one step, and Ctrl+Z / Ctrl+Shift+Z (or the buttons) restore with a
graph rebuild when needed. Scene switches and playlist structural changes clear
the history.

The offscreen GUI suite (`tests/test_gui.py`, 28 tests) covers preview delivery,
snapshot output, closing while recording, the screenshot deadline, CLI
screenshot exit, real `QMouseEvent` drags for move/resize/crop, arrow nudging,
numeric fields, opacity/order, multi-select group moves, alignment/spacing
guides, snap toggle/tolerance, alignment shortcuts, undo/redo of layout and
add/remove/duplicate, image/video/network/screen source layers, playlist
play-once/loop, persisted playlist and snap state, autosave on close, and an
edit during a real recording session that finalises both MP4 segments. The
pipeline tests check the live pad geometry, alpha and zorder. `pytest` at that
stage: **71 passed, 1 skipped** (the current suite has 87).

### GUI source types, persistence, and CI

* The Layers tab now exposes **Image**, **Video file**, **Screen** and
  **Network URL** in addition to camera/test/text/logo. Image and video use a
  file picker and a normal `Layer`; network sources validate `rtsp://`,
  `rtsps://`, `http://` and `https://` URLs before adding the layer.
* Screen capture uses the new `portal.py` ScreenCast client:
  CreateSession → SelectSources (monitor/window, hidden cursor) → Start →
  OpenPipeWireRemote. The layer stores the node id in `Layer.source`; the GUI
  keeps the returned fd in a session map and passes it to
  `AnyCamEngine(..., screen_fds=...)`, which gives `pipewiresrc` its `fd` and
  sets `target-object` to the portal's node id (`path` is the deprecated
  PipeWire daemon path and is not a node selector). Sessions are reused across
  graph rebuilds and closed when their last layer is removed or the window
  closes. Saved screen layers ask the portal for access again on the next
  engine start because an fd cannot be serialised.
* `Scene.playlist` (`PlaylistConfig`) stores layer ids, interval, loop and the
  current index per scene. Scene switches save/restore it, and the playlist
  widget/timer are rebuilt from it on load. Clearing a playlist or removing a
  member updates it too. "Loop playlist" unchecked now plays through to the
  last item and stops rather than disabling rotation entirely.
* `Project.editor` (`EditorConfig`) stores the snap toggle and tolerance. The
  Layers tab initialises from it and updates it on change.
* Normal windows autosave `~/.config/anycam/project.json` 800 ms after a dirty
  edit and on close; the screenshot/demo windows created with an explicit
  project never touch the user's saved project. The Output tab still has the
  explicit Save/Reload buttons.
* `.github/workflows/ci.yml` installs Ubuntu GStreamer/PyGObject/Qt dependencies
  and runs the same suite with the offscreen Qt platform. The local evidence is
  still from the CachyOS machine; no live portal capture is claimed.

### Saved projects

`AnyCamWindow` loads `~/.config/anycam/project.json` at startup, falls back to
defaults with a status message on corrupt JSON, and the Output tab has a
"Reload saved project" button. Normal windows now autosave dirty edits and save
again on close; explicit project windows used by tests/screenshots do not write
the user's config. Verified with a saved 800×450@25 `Desk scene` and with an
intentionally corrupt file.

### Face overlay, multi-camera sources, and local RTSP

* Camera layers now own a V4L2 device. The Layers tab has a **Camera source**
  combo (with Refresh) for the selected camera layer, and Add Camera prompts
  when multiple capture nodes exist. AnyCam's own loopback output is excluded
  from the picker to prevent output-to-input feedback.
* `sources.camera_chain` falls back to a supported raw format (for example
  YUYV → GStreamer YUY2) when a loopback/continuous device has no discrete
  frame intervals, instead of requesting MJPG and failing.
* A two-camera hardware test (`tests/test_multicam.py`) feeds a synthetic
  `videotestsrc` into v4l2loopback and composites it beside a real camera; it
  asserted 119 buffers from each source chain and 119 composited frames
  (`evidence/10_multicam/`). The default suite skips it unless
  `ANYCAM_TEST_CAMERA` and `ANYCAM_TEST_V4L2_SINK` are set.
* Each camera layer can now request its own capture size and frame rate:
  `Layer.camera_width/height/fps` are persisted, the Layers tab shows
  **Capture size** and **Capture FPS** with `0`/`Auto` falling back to the
  layer box and canvas rate, and `sources.camera_capture_request` feeds the
  request into the existing per-device mode negotiation. Changing a value
  restarts the graph like a device change.
* **Add two-camera scene** (`model.two_camera_scene`) creates a scene with two
  camera layers at `(0, 0, 0.5, 1)` and `(0.5, 0, 0.5, 1)` using the first two
  detected V4L2 devices; the preset is ordinary layers afterwards.
* **Add camera grid...** opens a dialog with four 2×2 cells. Each cell is
  assigned one detected camera or left empty, and at least three distinct
  devices are required. `model.camera_grid_scene` places the non-empty slots
  at equal quarter cells, so three cameras can occupy any three cells. The
  resulting cells are ordinary layers and can be moved or re-deviced.
* A disconnected camera keeps its box instead of disappearing:
  `overlay.render_camera_placeholder` generates an opaque card with the layer
  name and device path, and `AnyCamEngine(skip_layers=...)` puts it on a normal
  compositor pad at the same position/z-order. The rest of the scene keeps
  producing frames, including when every camera in a grid is disconnected
  (the status chip reads `WAITING` and the placeholders are still visible).
* Camera hot-plug: when a pipeline error is caused by a disappeared camera
  node, the GUI records the layer/device, rebuilds the graph with
  `AnyCamEngine(skip_layers=...)` so the remaining layers stay live with
  placeholders in the missing boxes, and starts a 1.2 s watcher. `start_engine`
  runs the same missing-device check first, so a saved project whose camera is
  not plugged in starts with the other layers and placeholder cards. When `Path(device).exists()` is true again, the layer is
  removed from the skip set and the graph is rebuilt; a camera that is still
  missing stays out. If no layers remain, the compositor still runs with
  placeholder cards (the status chip reads `WAITING`) instead of collapsing,
  and an explicit Stop preview is not undone by the watcher. A real StreamCam
  cable-pull run is not yet verified: `evidence/12_camera_reconnect/` records
  two blocked probe attempts and the command to complete it. Covered by `test_camera_loss_drops_layer_but_keeps_preview_alive`,
  `test_camera_reconnects_when_its_device_returns` and the two skip-layer tests
  in `tests/test_pipeline.py`.
* `face.py` now matches OpenCV's `FaceDetectorYN` preprocessing and decode:
  raw 0..255 BGR at 640×640, `sqrt(cls*obj)` confidence, and log-size bounding
  boxes. The previous mean/std RGB blob never met the score threshold, so the
  detector silently returned no faces.
* The GUI runs YuNet every 200 ms on the latest preview frame, moves a `face`
  compositor layer through `AnyCamEngine.set_layer_box`, and toggles its pad
  visibility when the face leaves the frame. The layer is created/removed by
  the Effects tab's face style, and padding follows `face_strength`.
* Verification: `tests/test_face.py` detects a public-domain NASA portrait
  (score 0.88+) and pins the preprocessing/decode with a deterministic fake
  session; a live offscreen run tracked that portrait at 31 fps with pad alpha
  1.0 (`evidence/09_face/`).
* `tests/test_rtsp.py` starts a minimal RTSP server and an ffmpeg RTP producer,
  then decodes `rtsp://127.0.0.1:<port>/test` through a real `network` layer.
  The test asserts decoded preview samples and composited frames
  (`evidence/08_rtsp/`).

### Cleanup

* `branding/assets` are shipped inside the package (`src/anycam/assets/`,
  `importlib`-visible via `config.assets_dir()`), so wheels contain the logo and
  YuNet model. `onnxruntime` is declared in the `face` extra; the unused OpenCV
  dependency was removed.
* Missing media audio is reported as `media_error`; audio is not built when no
  recorder/RTMP consumer exists (previously an unlinked `audiomixer` produced a
  `not-linked` error).
* `stop()` distinguishes ERROR from EOS and reports `record_ok`; audio records
  get a silent-anchor, mic level messages feed a GUI meter, and the mic list has
  a Refresh button.
* `pipeline._box_pixels` now uses `geometry.box_pixels`, and `image_chain`
  applies `layer.crop`.
* Background-replacement controls are disabled and labelled; the model fields
  remain for project compatibility. The face controls are active and drive the
  live YuNet overlay.
* Dead imports and the unreachable `soak` shortcut were removed; `anycam soak`
  now forwards its arguments.

## Reproduce

```bash
./scripts/setup.sh
./scripts/test.sh                                        # 111 passed, 1 skipped

PYTHONPATH=src python3 -m anycam demo --seconds 3 --source testsrc \
  --width 640 --height 360 --fps 30 --shot /tmp/shot.png --shot-at 1

PYTHONPATH=src python3 -m anycam demo --seconds 12 --source testsrc \
  --vcam --preview --json /tmp/vcam.json

./scripts/verify-firefox.py --out /tmp/anycam-firefox

QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 -m anycam gui \
  --screenshot /tmp/gui.png --seconds 3 --source testsrc
```

## Residual notes

* Firefox was re-driven after the remediation: `scripts/verify-firefox.py`
  selected AnyCam at 1280×720@30 and captured the composited overlays
  (`evidence/02_virtual_camera/firefox.png`, `firefox-report.json`).
* Background blur/replacement remains unimplemented; its GUI controls are
  disabled.
* Screen-capture sessions need a fresh xdg-desktop-portal grant after a restart;
  there is no live ScreenCast artifact in this evidence set. RTSP is covered by
  a local server integration test; a remote/real camera RTSP feed and network
  backpressure remain untested.
* Scene/effect changes during recording produce multiple announced MP4
  segments, not a single concatenated file.

## 2026-10-31 — audit M0: safety and geometry

The 2026-10-31 audit found 55 bugs; M0 fixed the safety and picture-correctness
group. Every item below has a test in `tests/test_audit_regressions.py` (many
inspect real pixels through `tests/helpers/frames.py`). Suite: 111 passed,
1 skipped.

Fixed:

* **B1** Screen layer connects with `path=<node id>` (`target-object` is a name
  or serial); the negotiated frame size is compared with the portal's stream
  size and reported as `report.screen_error`.
* **B2** Private evidence moved out of the tree; `.gitignore` no longer forces
  videos back in.
* **B3** A saved RTMP URL never publishes on preview; streaming is an explicit
  **Go Live** action in the Output tab.
* **B4** The microphone is opened only when `AudioConfig.mic_enabled` is set;
  `--audio` is the CLI switch, a media bed or `--rtmp` no longer opens it.
* **B5** `project.json` is written `0600`, and reports/log lines redact the
  stream key (`config.redact_url`).
* **B6/B7** Recording uses fragmented MP4 (`fragment-duration=500`), so a
  `kill -9` leaves a playable file and stop() no longer copies a tmpfs file.
* **B8** Project saves are atomic (temp file + rename + `.bak`); an unreadable
  project is copied to `project.json.corrupt` before defaults are used.
* **B22** Live sources are scaled to fit and letterboxed into the layer box, so
  aspect ratio and drag handles describe the same rectangle.
* **B23** Crop uses the source size for camera, image, video and screen layers.
* **B24** Chroma tolerance uses the element's real 0..64 range and smoothness
  drives the sensitivity values.
* **B25** Effects are carried onto image/logo/text overlay chains.
* **B26** Overlay cache keys include the file's identity, so two photos with
  the same filename no longer collide.
* **B27** Camera mode selection picks the nearest size instead of the first
  listed mode, and the capture request follows the canvas, not the layer box.
* **B28** Odd canvas sizes are snapped in the GUI and rejected by the engine.
* **B29** Chroma colours are parsed safely (`#rgb` accepted, malformed values
  fall back instead of raising mid-build).
* **B30** Portal `cursor_mode` is 2 (embedded); 1 hid the pointer.
* **B38** `pump()` passes nanoseconds to `timed_pop_filtered`.
* **B39** `record_dir` is used, and the XDG video directory comes from
  `user-dirs.dirs`, not a guessed environment variable.
* **B49** Photo EXIF rotation is applied.
* **B50** Scene preset filenames are sanitised.
* **B51** Test-pattern layers apply crop.

Still open from M0:

* B1's live verification on a real portal session (needs the desktop's
  chooser; the code path and size check are covered by tests).

B31 was closed on 2026-10-31; see the section below.

## 2026-10-31 — B31 (crop editor) and M1 slice 1 (output pipelines)

**B31 — the crop window is drawn over the picture it edits.** Crop mode now
builds the selected layer with its crop suppressed (`AnyCamEngine(
preview_crop_layer=…)`), so the composite shows the whole source while the
model keeps the real crop. The window is drawn over the layer's *picture*
(source fitted into the box, from `AnyCamEngine.source_sizes()` /
`PreviewWidget.picture_rect`), not over the box, and drags track the pointer.
Overlays (image/logo/text) are pre-rendered, so opening the editor rebuilds the
graph once; camera/video/network/screen/test layers get the same treatment.
Tests: `tests/test_audit_regressions.py::test_preview_crop_layer_shows_the_uncropped_picture`
(pixels, both halves visible while the model crop stays `(0.5,0,0,0)`) and
`::test_crop_editor_window_follows_the_picture`. Known cost: opening or closing
crop mode rebuilds the graph, so a recording in progress starts a new segment.

**M1 slice 1 — RTMP output moved into its own pipeline.** The canvas graph now
ends the RTMP branch at a `h264parse → appsink` (drop, max 1 buffer) plus an AAC
`appsink` on the shared audio tee; a separate `anycam-rtmp` pipeline pushes those
samples into `appsrc → flvmux → rtmpsink`. A server that accepts a connection and
never speaks RTMP can no longer reach the tee, so preview, recording and the
virtual camera keep running; the failure is reported as `report.rtmp_error` and
teardown happens off the caller's thread (`stop()` returned in <0.4 s in the
test). This is the first slice of the M1 split: sources are still built into the
canvas graph and the controller/health model is not written yet.
Test: `tests/test_m1_outputs.py::test_unresponsive_rtmp_cannot_stall_the_canvas`.

**Soak re-baseline (2026-10-31, 1920×1080@60, 10 min, fragmented recorder).**
36002 preview frames at 59.998 fps (0.003% dropped), camera mode
`MJPG 1920x1080@60`, 859 MB fragmented MP4 with 36014 packets over 600.23 s
(60.0 fps). Two findings:

* `verify_mp4` reported **100% dropped frames** for the fragmented file because
  `nb_frames` is absent from a fragmented moov atom; it now falls back to
  `ffprobe -count_packets`. The recording was healthy all along.
* RSS grew 202.6 → 226.3 MiB after a 120 s warm-up (**2.38 MiB/min**, gate is
  1.0). The previous 0.032 MiB/min figure came from the faststart recorder and is
  no longer valid. This is now the blocking M1 soak item.

## 2026-10-31 — M1 slice 2: capture source pool

Capture pipelines moved out of the canvas graph. `pool.SourcePool` owns one
`intervideosink` pipeline per distinct `CaptureSpec`; layers on the same device
share it by refcount and the last release sets it to NULL. The canvas consumes
each source through `intervideosrc` (`sources.layer_tail`), so a dead capture
cannot stop the compositor. `AnyCamEngine(..., pool=...)` accepts a pool, and
the GUI keeps one for the window's lifetime (`AnyCamWindow._source_pool`).

Three bugs were found while finishing the interrupted work:

* `compositor ignore-inactive-pads=true` ignored inter-fed pads from the first
  frame (measured: 0 preview frames against 49 with false at 320x180), so the
  canvas never started. The flag is reverted; a dead capture still leaves the
  other pads rendering.
* Pushing EOS into each `intervideosrc` tail deadlocked the compositor's
  streaming thread, and EOS does not cross `intervideosink` from the capture
  pipeline (measured). `stop()` now sends one EOS through the compositor's src
  pad, which reaches every branch and finalises mp4mux/flvmux.
* Two inter sources start at PTS 0, so the compositor emitted two frames with
  the same PTS and qtmux failed with `Buffer has no PTS`. A canvas `videorate`
  (`skip-to-first`) keeps the output monotonic, and the preview holds delivery
  until every compositor pad has produced a buffer so the first shown frame is
  not the bare background.

The soak gate now judges the settled second half and the tail
(`soak._memory_assessment`) instead of a least-squares slope through buffer-pool
step-ups; a real leak of one 1080p frame per minute still fails
(`tests/test_soak_memory.py`). A fresh 10-minute run is still required.

Verification: `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 -m pytest tests -q`
→ 128 collected, 127 passed, 1 hardware-gated multicam skip.
`tests/test_source_pool.py` covers sharing, real pixels through the pool, a dead
capture, and release on stop.

**A/V drift gate (fixed 2026-10-31).** `scripts/verify-av-drift.py` now rates the
composited output (the same duplicate-PTS fix as the engine), measures each
stream against its own median buffer interval, compares drift with one frame,
and waits for the recorder sink's EOS before teardown. A 20 s synthetic run
reports `drift_s: 0.0`, zero duplicate/backward/gap timestamps (1202 video and
1201 audio tap buffers) and a finalised 20.03 s MP4 with 1202 packets. The
10-minute gate is still pending.

## 2026-10-31 — camera replug identity and UVC retry

Camera layers now store the stable `/dev/v4l/by-id/...` path instead of
`/dev/videoN`, and existing projects are migrated when they load. A USB camera
that returns as a different node after a replug therefore keeps its layer and
reconnects without manual re-selection (audit B46).

A camera source error is now quarantined even when the node still exists.
`v4l2src` errors such as `Device or resource busy`, `No space left on device`
or an I/O error put a placeholder in the graph, and the reconnect watch keeps
retrying with persistent backoff (2 s, growing to 10 s) instead of stopping
after one failed restart. The reconnect path also no longer rescans every V4L2
node with `v4l2-ctl` immediately after reopening the camera, which added extra
opens at the worst moment.

This is the app-side mitigation for the Logitech/UVC kernel wedge. A driver
that is already stuck still needs a USB reset or a reboot; physical
cable-pull verification is still pending. `tests/test_camera_identity.py`
covers by-id resolution, and `test_camera_reconnect_retries_with_backoff_when_restart_fails`
covers the retry behaviour.
