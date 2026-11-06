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
| Hardware H.264/H.265/AV1 recording (NVENC) | PASS on RTX 4060 Ti: HEVC and AV1 muxed to MP4; H.264 4K60 CUDA path tested | `tests/test_pipeline.py::test_hardware_hevc_and_av1_record_to_mp4`, `tests/test_recorder.py::test_recorder_parses_the_selected_hevc_codec` |
| NVENC vs x264 at 4K60 (real 10 s recording) | NVENC 53.69 fps, 10.52% wall-clock drop; x264 30.3 fps, 49.51% drop; per-frame CPU 0.152 vs 0.236 CPU-s | `.freebuff/encoder-benchmark/report.json` |
| Standalone Qt GUI with live preview + snapshot | PASS (offscreen; live frame, F5 snapshot written) | `evidence/00_skeleton/gui.png`, `docs/REGRESSIONS.md` |
| Audio (mic + media file, mixed, AAC, gain/mute) | PASS, 0.078 s A/V delta, levels verified | `evidence/04_audio/av.mp4`, `verify.json` |
| RTMP publish to local receiver | PASS, 1280×720 H.264 High yuv420p + AAC, 8.23 s | `evidence/05_rtmp/received.flv` |
| Playlist / slideshow with PiP rotation | PASS, 5 rotating plates captured | `evidence/03_playlist/rotate_01..05.png` |
| Effects (brightness, contrast, saturation, chroma, film looks, mirror, window frames, backdrops, 3D motion) | PASS, live per-layer updates and pixel regressions | `evidence/06_effects/*.png`; `tests/test_audit_regressions.py`; `tests/test_overlay.py` |
| YuNet face overlay in the live compositor | PASS, real portrait tracked, 640×480 preview at 31 fps, pad alpha 1.0 | `evidence/09_face/report.json`, `preview.png` |
| Local RTSP network source | PASS, ffmpeg RTP → local RTSP server → `uridecodebin` → compositor | `evidence/08_rtsp/pytest.txt`, `junit.xml` |
| Two-camera composite (real + synthetic v4l2loopback) | PASS, 119 composited frames; both camera chains delivered 119 buffers | `evidence/10_multicam/report.json`, `junit.xml` |
| Camera modes, grid preset, placeholders and reconnect | PASS (offscreen; placeholder graph and reconnect timer exercised) | `evidence/11_camera_controls/`, `docs/EVIDENCE.md` §4.10 |
| 10-min 1080p60 soak (synthetic re-run) | PASS on drops/memory/stalls: 36,009 frames over 600.15 s (0.0% dropped), RSS 188.7→189.0 MiB (0.043 MiB/min), 12→12 FDs; vcam blocked (no `/dev/video10`) | `evidence/soak_10m/report.json` |
| Output isolation (M1 slice 1) | PASS, an RTMP server that stops reading cannot stall preview, recording or the virtual camera | `tests/test_m1_outputs.py::test_unresponsive_rtmp_cannot_stall_the_canvas` |
| Unresponsive RTSP source (B10) | PASS, silent RTSP server; canvas keeps ≥99% of 30 fps and `stop()` returns in under 1 s | `tests/test_m1_outputs.py::test_silent_rtsp_source_cannot_stall_the_canvas` |
| Refused output (B11) | PASS, missing RTMP sink leaves preview, recording and the vcam tap running; a failed rebuild clears Record and finalises the recorder | `tests/test_m1_outputs.py::test_missing_rtmp_sink_keeps_preview_and_recording`, `tests/test_gui.py::test_failed_start_clears_recording_state` |
| Missing media cards (B13) | PASS, missing image/video layers render cards while the test layer keeps producing frames | `tests/test_pipeline.py::test_missing_media_layers_render_cards_without_stopping_scene` |
| Video-file looping (B14) | PASS, 1 s clip seeked after EOS; centre still red after 2.6 s | `tests/test_pipeline.py::test_video_layer_loops_without_a_black_frame` |
| Vcam diagnostics and bounded queue (B17/B18/B20) | PASS, inactive output reports its error, explicit device is checked, appsrc is capped at 2 buffers | `tests/test_vcam_output.py` |
| Record + stream audio queues (B21) | PASS, separate queues on the shared audio tee | `tests/test_m1_outputs.py::test_recording_and_streaming_have_separate_audio_queues` |
| Duplicate camera sharing (B16) | PASS, one pool entry with two references after GUI duplicate | `tests/test_gui.py::test_duplicate_camera_shares_one_pooled_capture` |
| Hidden capture release (B15) | PASS, hidden layers acquire no capture; showing acquires, hiding releases | `tests/test_source_pool.py::test_hidden_capture_is_acquired_only_while_visible` |
| Per-camera mode report (B48) | PASS, two camera layers report both negotiated modes | `tests/test_source_pool.py::test_report_lists_every_camera_mode` |
| Per-source health/watchdog (B41) | PASS, source-side counters, instant FPS, dead capture flagged after 2 s | `tests/test_source_pool.py::test_health_flags_a_capture_that_stops` |
| Continuous MP4 across rebuilds | PASS, one persistent recorder spans rebuilds, including five scene switches, and writes one playable file | `tests/test_recorder.py`, `tests/test_gui.py::test_five_scene_switches_keep_one_recording`, `tests/test_gui.py::test_layer_edit_while_recording_keeps_one_continuous_file` |
| Persistent virtual-camera output | PASS (offscreen wiring), one session-owned `v4l2sink` survives engine rebuilds; real loopback consumer not exercised because `/dev/video10` is absent | `tests/test_vcam_output.py`, `tests/test_gui.py::test_vcam_output_survives_engine_restarts` |
| Live record/vcam taps | PASS, Record start/stop and vcam toggle attach/detach valves without rebuilding the canvas | `tests/test_gui.py::test_record_toggle_attaches_without_rebuilding`, `tests/test_gui.py::test_vcam_output_survives_engine_restarts` |
| `rtmp2sink` preference | PASS, `rtmp2sink` selected when installed, `rtmpsink` fallback; actual delivery still uses the existing receiver evidence | `tests/test_m1_outputs.py::test_stream_prefers_rtmp2sink` |
| Automated tests | PASS, 218 passed + 1 hardware-gated multicam test | `./scripts/test.sh`; `docs/EVIDENCE.md` §2, §4.10 |
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
* **Effects browser.** The Effects tab has a thumbnail library in sixteen
  categories (Looks, Portrait, Face, Masks, Framing, Distort, Retro TV,
  Touch-up, Frames, Shapes, Holiday / Nature / Fun / Tech / Fancy frames,
  Animations, Emoji rain) with search; click a tile to apply it. Each tile is drawn
  with the real effect on a sample picture. Effects apply live and never
  rebuild the canvas. "Apply to all sources", Copy, Paste and Clear work on
  whole layers, and every individual control remains on the Adjust page.
* **Filters.** 31 GStreamer filters (16 distortions such as pinch, bulge,
  sphere, twirl, fisheye, kaleidoscope and water ripple; 12 retro TV looks;
  blur, soften and denoise) plus the Python effects below. A layer has two
  filter slots, so two filters stack (for example a portrait effect and a
  distortion). A filter is swapped in on the running pipeline.
* **Portrait effects** keep the person and change what is behind them: blur,
  dim, black and white, spotlight, and replacement with a gradient or an
  animated backdrop. They use the bundled MediaPipe selfie-segmentation model
  (`src/anycam/assets/models/NOTICE.md`, Apache-2.0); without it, a
  head-and-shoulders mask from the face detector is used.
* **Face effects** follow the face using the detector's five landmarks:
  sunglasses, party hat, crown, mustache, clown nose, cat, halo and a face
  frame, plus pixelate or blur face for privacy. **Framing**: digital zoom
  and auto-frame (keeps the face centred).
* **Face masks** (the Masks category) are painted full-face masks that are
  warped onto each face from its eyes and mouth, so they scale, roll and follow
  the head: cyber face (with a scanning line and pulsing visor), neon contour,
  robot, cyborg, alien, a web-pattern hero mask, hero eye mask, ninja, skull,
  sugar skull, zombie, vampire, devil, clown, harlequin, pirate, masquerade,
  frosty snowman, tiger, panda, puppy and fox. The Face category also has
  emoji eyes (hearts, stars, fire, sparkles, money, dizzy) and emoji that
  orbit the head (hearts, stars, butterflies, flowers, snowflakes, notes, party,
  bats). The picture-based ones use the bundled emoji pack.
* **Themed frames.** 32 finished, multi-colour frames in five groups:
  Holiday (Christmas, Christmas lights, winter snow, Halloween, Valentine,
  birthday bunting, New Year, autumn leaves, confetti), Nature (cherry
  blossom, flower garden, tropical, ocean, cute animals, deep space), Fun
  (candy, champion, music, comic book, washi tape, postage stamp), Tech (cyber
  HUD, RGB gaming, VHS tape, neon pink + blue, neon green, orange and violet)
  and Fancy (gold, wood, royal blue and gold, rainbow). They mix gradients,
  stripes, drawn art and emoji; thickness scales them.
* **Animations.** 42 particle animations that are added as a layer on top of
  the picture and really move: snowfall, blizzard, rain, storm, clouds,
  autumn leaves, cherry blossoms, butterflies, bubbles, fireflies, rising
  hearts, heart rain, roses, confetti, party, balloons, fireworks, sparkle
  burst, twinkling and shooting stars, glitter, magic dust, fire embers,
  Matrix code, Christmas, Halloween bats and ghosts, New Year, space, pizza,
  money, music, fire, animal parade and more. The **Emoji rain** category
  makes any of the 171 bundled emoji fall. Each frame is a function of time
  (drawn at most 960 px wide, 1-6 ms), so a slow frame never desyncs it.
* **Emoji pack.** 171 curated pictures from Google's Noto Emoji (Apache-2.0,
  pinned commit, SHA-256 per file; see `src/anycam/assets/emoji/NOTICE.md`),
  rebuilt by `scripts/fetch-emoji.py`.
* **Window frames and shapes** are per layer: 16 styles including neon glow,
  vignette, polaroid, TV screen, film strip, and shaped windows (circle,
  oval, rounded, hexagon, diamond, heart, star) that really cut the picture,
  with a border colour, a matte colour and a thickness.
* **Backdrops** are in the Backdrop tab: ten generated static backgrounds
  and five procedural animated ones (3D tunnel, starfield, plasma, aurora,
  water ripples) that are drawn with numpy at low resolution and really move.
  Applying one replaces the previous backdrop and can inset capture layers so
  the background is visible; clearing it puts them back.
* **Overlays.** A scrolling ticker, a lower third that slides in each time it
  is shown, and animated GIF/WebP/APNG layers, each added from the source
  menus; the ticker and lower third can be edited live from the layer's
  right-click menu.
* **Drawing.** The **Draw** button turns the pointer into a pen, marker,
  arrow or box with a colour and size, plus Undo and Clear. Strokes are stored
  with the project and update the running canvas without a rebuild.

## Releases

**Version 1 (v1.0.0)** is the first release. Settings → **About** shows the version and
the versions of Python, Qt, GStreamer and onnxruntime this copy is running on.

Pushing a tag such as `v1.0.0` runs `.github/workflows/release.yml`: it checks the tag
matches `pyproject.toml`, builds the source tarball (`anycam-1.0.0.tar.gz`) and the wheel,
verifies the bundled emoji, models and styles are inside both, installs the wheel into a
clean environment as a smoke test, and publishes a GitHub Release with `SHA256SUMS`.
To cut the next release, bump `version` in `pyproject.toml` and `VERSION` in
`src/anycam/config.py`, commit, then `git tag vX.Y.Z && git push origin vX.Y.Z`.

Install from a release:

```
pip install anycam-1.0.0.tar.gz            # or the .whl
pip install 'anycam[gui,face]'             # PySide6 and onnxruntime, for the window and face effects
```

PyGObject and the GStreamer plugins come from the distribution (see Requirements).

### Limits compared with ManyCam

| Area | AnyCam |
|---|---|
| Platform | Linux (V4L2, PipeWire, GStreamer); ManyCam is cross-platform |
| Multi-camera | Yes, layered camera sources; each layer owns one V4L2 device and can request its own resolution/FPS. Two-camera side-by-side preset and a configurable 2×2 grid for three or four cameras. No arbitrary N-camera layout editor |
| Camera controls | Per-layer capture size/FPS plus automatic pixel-format selection; no per-layer exposure, focus, white-balance or manual mode UI |
| Shared output | All camera layers share one canvas size, frame rate and virtual-camera output |
| Scenes | Multiple scenes and layers, but switching rebuilds the graph as a hard cut; no animated transitions |
| Effects | A browsable library of about 300 effects, frames, masks and animations (colour looks, filters, portrait and face effects, frames and shapes, animated backdrops, overlays, drawing). There is no community effect pack format, true 3D (depth) face masks, or lip/expression tracking; masks are 2D paintings warped from five landmarks |
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

Tested on CachyOS (Arch family), kernel 7.3, KineticWE 6.7.80 on KWin/Wayland,
NVIDIA RTX 4060 Ti.

* Python 3.11+ (3.14 here)
* GStreamer 1.28 with `base`, `good`, `bad`, `ugly`, `libav`, `opengl`, `pulse`
* PyGObject (`python-gobject`), always required and only available from your
  distribution, not PyPI
* PySide6 6.12 (matching the system Qt 6.12) for the window; from PyPI use the
  `gui` extra, `pip install -e '.[gui]'`, but the wheel's Qt must match the
  system Qt or the import fails
* `xdg-desktop-portal` with a desktop backend and PipeWire for screen capture
* `numpy`, `Pillow`, `psutil`
* `ffprobe` (ffmpeg), `v4l-utils` (`v4l2-ctl`), `pactl` (PipeWire/PulseAudio)
* `onnxruntime` for the portrait and face effects (the YuNet and selfie
  segmentation models are bundled; without it those effects are simply
  unavailable). Also the `face` extra: `pip install -e '.[face]'`
* Root once: `v4l2loopback` kernel module, for the virtual camera only

Arch/CachyOS package names:

```bash
sudo pacman -S --needed python pyside6 python-gobject gst-plugins-base \
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
colour, film-look, mirror and chroma controls, record, virtual-camera and
snapshot buttons, an audio tab (device, gain, mute, media mixing), a playlist tab
(images/video, interval, loop), and an output tab for canvas size, FPS, encoder,
V4L2 device and RTMP URL. Video and image files, RTSP/HTTP URLs and screen capture are
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

`rtmps://` URLs use `rtmp2sink` when installed and fall back to `rtmpsink` (librtmp). Both inputs are
resampled to 48 kHz stereo and mixed by `audiomixer`; the AAC track is muxed
with the H.264 video so the MP4 stays synchronised.

### Hardware encoding and latency

The Output tab can select **Auto**, **NVIDIA NVENC H.264**, **NVIDIA NVENC
H.265 (HEVC)**, **NVIDIA NVENC AV1** or **CPU x264**. Auto uses H.264 NVENC
only when both the `nvcodec` GStreamer plugin and a working `nvidia-smi` driver
are present; otherwise it records with x264. `ANYCAM_ENCODER=nvenc`,
`nvenc_h265`, `nvenc_av1` and `cpu` override the saved setting for headless
runs.

Recording MP4s can carry H.264, H.265 or AV1. An unavailable H.265 encoder
falls back to H.264 NVENC, and AV1 falls back through H.265 NVENC to H.264
NVENC, then x264. There is deliberately no CPU H.265/AV1 fallback: those
software encoders cannot sustain a 4K60 multi-camera canvas. RTMP/FLV carries
H.264/AAC only, so the stream branch stays H.264 even when recording uses HEVC
or AV1. RTMP bitrate follows the canvas above 1080p: H.264 uses 18 Mbit/s at
1440p and 34 Mbit/s at 4K instead of sending a soft 12 Mbit/s 4K stream.

The NVENC branch feeds compositor RGBA through `cudaupload` when CUDA is
available and encodes directly from that GPU buffer. It skips the CPU
`videoconvert` and I420 copy used by x264, so 1080p/4K recording does not add
that CPU conversion pass while several camera layers are compositing. NVENC also
runs with zero-latency mode. The 4K canvas limit is 3840×2160; NVENC itself
accepts up to 4096×4096. If the NVIDIA driver or plugin disappears, Auto falls
back to x264 rather than killing the recording.

Measured 2026-11-06 on the RTX 4060 Ti, 4K60, 10 s of synthetic test pattern
plus badge, recording only (no preview or audio): NVENC H.264 delivered 561
frames in 10.45 s (53.69 fps, 10.52% missing against wall time) at 813.9% mean
process CPU; x264 delivered 314 frames in 10.36 s (30.3 fps, 49.51% missing) at
716.3% CPU. CPU is process-wide, where 100% is one saturated core; NVENC's raw
total is higher because it delivered 1.79× more frames. Per delivered frame,
NVENC used about 0.152 CPU-seconds versus 0.236 for x264. Even NVENC did not
hold a perfect 60 fps in this 4K synthetic run.

NVIDIA Broadcast SDK AI effects (background blur, noise removal and
auto-framing) are not wired in. The SDK is distributed for Windows only, so a
Linux control would be a fake; AnyCam does not expose one.

### Virtual camera

One root-only command (reversible):

```bash
sudo modprobe v4l2loopback video_nr=10 card_label=AnyCam exclusive_caps=1
# or: ./scripts/enable-v4l2loopback.sh   (reloads a module loaded with 0)

# exclusive_caps=1 is required for Discord, Chrome and other Chromium apps to
# list the node at all.  With the module default (0) the device advertises
# capture and output together and their device enumeration skips it.  AnyCam
# holds the device from launch, so it keeps advertising capture while idle.
v4l2-ctl --list-devices           # expect "AnyCam" / /dev/video10
```

Then run AnyCam with the virtual camera branch:

```bash
PYTHONPATH=src python3 -m anycam demo --seconds 30 --vcam   # or use the GUI button
```

AnyCam holds the loopback node open from launch and feeds it a "virtual camera
ready" card at 10 fps, so Discord/Zoom can select **AnyCam** at any time — even
before the preview starts — instead of failing with `EIO`.  With
`exclusive_caps=1` the node only advertises capture while something writes to
it, so start AnyCam before opening the application's camera list; **Settings →
Diagnostics** says whether the loaded module will be listed by Chromium. Clicking **Virtual
camera** (or starting the preview with it enabled) replaces the card with the
live canvas; switching it off returns to the card.

If an application already had the node open when AnyCam launched, that open
failed before there was a stream. AnyCam shows a dismissible hint naming the
application; re-select "AnyCam" in its camera list to make it retry. Nothing
outside the application can repair that open.

The on-screen preview is delivered at 960×540 instead of 1280×720, which is
already panel resolution for the default window. Measured at 1080p60 on this
machine, that cut the GUI's CPU from 188% to 162% of one core; the canvas and
recording stay at the project resolution, and the virtual camera follows it up
to 3840×2160.

The vcam mode is fixed when it starts so changing resolution does not close the
node under Discord. Toggle **Virtual camera** off and on after changing from
1080p to 1440p/4K to apply the new mode; a 4K vcam was verified by writing a 4K
YUYV stream to `/dev/video10` and reading it back with ffmpeg. When a 4K canvas
is viewed through an already-open 1080p vcam, the tap uses 4-tap scaling instead
of bilinear so text stays crisp.

The virtual-camera branch converts each frame once, not twice. The canvas tap
forwards the composited RGBA frame untouched and the vcam's own pipeline
converts it straight to the driver's YUYV. Converting in both stages added a
second full 1080p60 pass: measured on this machine with the same 480 frames in
8 s, the tap path went from 58.5% to 40.9% of one core.

When `glupload`/`glcolorconvert`/`gldownload` are available, that single
conversion runs on the GPU (measured ~21% of a core against ~31% for
`videoconvert` at 1080p60). The GPU chain is only kept if its first frame
actually reaches `v4l2sink`; a failed or missing GL setup rebuilds the CPU
chain automatically, so the virtual camera never depends on working GL.

**Settings → Diagnostics** (and `anycam doctor`) reports which converter the
virtual camera is running and what each converter costs, in milliseconds of
pipeline time per frame, measured on synthetic frames at the project size and
rate. The measurement runs in a background thread and opens neither a camera
nor the loopback device.

When the loaded module was started with `exclusive_caps=0`, the same tab shows
**Fix Discord visibility**: it releases AnyCam's handle on the node, reloads
v4l2loopback through `pkexec` with `exclusive_caps=1`, and holds the device
again. A running preview is not stopped — only its virtual-camera tap detaches
for the reload. If authorization is declined or another application still has
the node open, the reason is shown and the manual command stays on screen.

AnyCam also refreshes udev automatically after it starts feeding the node.
With `exclusive_caps=1` the kernel only advertises capture while a writer is
streaming, and udev caches the capabilities from device-add time, so the node
can stay invisible to Chromium clients even when the module is configured
correctly. If the cached value still says video output, AnyCam asks for
authorization once and re-runs `udevadm trigger`; Discord, Vesktop and Chrome
then list **AnyCam** without a manual step.

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
                                     └→ tee → x264enc → h264parse → flvmux → rtmp2sink/rtmpsink
```

* `src/anycam/model.py` — plain-data Project/Scene/Layer/Effects, JSON presets.
* `src/anycam/geometry.py` — normalised boxes, crop and aspect fitting.
* `src/anycam/sources.py` — camera probing and source chains.
* `src/anycam/overlay.py` — original text/badge/logo rendering with Pillow.
* `src/anycam/portal.py` — xdg-desktop-portal ScreenCast client (session, node
  id and PipeWire fd).
* `src/anycam/face.py` — YuNet ONNX face detection with five landmarks.
* `src/anycam/segment.py` — person segmentation (bundled ONNX model).
* `src/anycam/effects.py` — the filter registry and the live-swappable filter
  slot; `src/anycam/processors.py` — Python per-frame effects (portrait, face,
  framing) and their worker bin; `src/anycam/effect_thumbs.py` — browser
  thumbnails.
* `src/anycam/procedural.py`, `src/anycam/animated.py` — animated backdrops,
  ticker, lower third, GIF player and the drawing canvas.
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
# 218 passed, 1 skipped (hardware-gated multicam; 219 collected)
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
  a compositor-specific API. The portal consent dialog is the normal
  user-facing permission prompt.

## Known limitations

* Screen capture requires the desktop's xdg-desktop-portal ScreenCast chooser
  and a live PipeWire session. The fd cannot be serialised, so a saved screen
  layer asks the portal for access again when preview/recording next starts.
  There is no checked-in live screen-capture artifact yet.
* Portal shares carry an `image-orientation` tag. **Automatic** follows that
  tag and does not force a KWin rotation: rotating at the source also flips
  AnyCam's preview and every vcam consumer. The **Output → Screen capture →
  Rotation** setting can override it: choose **Rotate 180°** if a specific
  share is upside down, **Flip vertically** for a pure vertical mirror, or
  **No correction** to disable it. `ANYCAM_SCREEN_FLIP` (`rotate-180`,
  `vertical-flip`, `none`) is available for headless runs. A receiver that
  flips only its own preview does not require changing the source.
* Compositor frame delivery still bounds a screen share. AnyCam keeps its
  canvas on the system clock, so a static or damage-driven share no longer
  caps the preview: the last captured frame is presented at the canvas rate.
  On KineticWE/KWin a visible window is captured live; a window the compositor
  has stopped painting (hidden on another virtual desktop, minimised, or a
  static page) can still stop producing fresh frames. AnyCam cannot capture
  frames the compositor has not rendered.
  The status chip shows `SLOW` for screen input below the lesser of 10 fps
  and half the canvas rate, measured over approximately three seconds after
  a two-second warmup. Static, damage-driven shares can also show `SLOW`;
  it reports low update rate, not necessarily a fault. `STALE` means fresh
  source identities stopped, while canvas FPS alone may still look normal.
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
* NVIDIA Broadcast / Tensor Core AI effects are not available in this Linux
  build. NVIDIA distributes the Broadcast SDK for Windows 10/11 only; there is
  no public Linux runtime here to call. Hardware **encoding** is separate and
  supported through NVENC; background blur, noise removal and auto-framing
  remain absent rather than being simulated with slow CPU filters.
* H.265 and AV1 recording need the matching NVENC codec. Missing codecs fall
  back to H.264 NVENC and then x264; CPU H.265/AV1 is not offered. RTMP/FLV
  stays H.264 because FLV does not carry HEVC or AV1 here.
* Python effects (portrait, face, framing) cost CPU per frame. At 1080p most
  fit a 30 fps budget; blurred backgrounds, animated backdrops and
  auto-frame are close to it, and a layer then updates at a lower rate
  while the rest of the canvas stays live. Run `scripts/bench-effects.py` on
  the streaming machine to see the cost per effect at your canvas size.
* Radioactive and Optical (retro TV) show only what moves, so they look odd on
  a still picture.
* A layer has two filter slots, so at most two filters at once.
* A ticker's or lower third's text can be edited, but its position and size
  are changed like any other layer.
* Rebuilding the graph while recording (scene change, text/image/crop edit, or
  undo/redo) starts a new MP4 segment; segments are playable and reported, not
  merged into one file. Live transforms, opacity, ordering, canvas alignment
  and per-layer effects do not rebuild, so they do not split a recording.
* Undo/redo covers layer layout and add/remove/duplicate in the active scene.
  Scene switches and playlist add/clear still reset the history.
* Playlist rotation is full-frame and uses one global interval; there is no
  per-item duration, order editor or picture-in-picture preset. All playlist
  media are instantiated in the graph even while hidden.
* Autosave writes the single project at `~/.config/anycam/project.json`; there
  is no Save As/Open dialog.
