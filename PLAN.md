# AnyCam remediation plan

Derived from the full project audit (code read + dynamic verification on this
machine, 2026-10-30). Every item below was reproduced, not inferred. Each task
lists the symptom, the evidence, the suspected cause, and the acceptance check
that proves the fix.

Status legend: `[ ]` open · `[~]` in progress · `[x]` done.

> **Remediation status (2026-10-30):** Phases 1–3 are implemented and verified
> on this machine. `pytest` is green (87 passed, 1 hardware-gated skip, 88 collected), the vcam crash is gone
> (720 frames in 12 s; a `v4l2src` consumer read the device), the GUI preview
> and snapshot work, `--screenshot` exits on a deadline, and closing the window
> mid-recording leaves a playable MP4. See `docs/REGRESSIONS.md` for the
> before/after record and residual notes. F-3 (RTSP test) and F-5 (YuNet face
> overlay) landed on 2026-10-30; the rest of Phase 4 (feature backlog) remains
> open. Phase 5 docs/evidence were updated from the new artifacts.

---

## 0. Baseline: what was measured

| Check | Command | Result |
|---|---|---|
| Test suite | `PYTHONPATH=src python3 -m pytest tests -q` | **25 collected, 1 failed** (`test_mjpg_preferred_at_same_rate`) |
| Doctor | `PYTHONPATH=src python3 -m anycam doctor` | pass (gst 1.28.8, camera, vcam, ffprobe, pactl) |
| Headless record+preview | `anycam demo --seconds 4 --source testsrc --record … --preview` | **works**, 1920×1080 H.264 60/1, 251 frames, `mp4_ok: true` |
| Headless snapshot | `anycam demo --shot …` | **fails**: `WARNING: no composited frame captured` |
| Virtual camera | `anycam demo --source testsrc --vcam` | **SIGSEGV (139)**, 5/5 runs |
| GUI preview | offscreen engine driven for 3 s | `appsink: None`, `preview.image: None` |
| GUI default start | offscreen, default project | pipeline error `not-linked` after ~0.2 s |
| GUI screenshot mode | `anycam gui --screenshot … --seconds 3` | **hangs**, killed at 45 s and 90 s |
| GUI close while recording | close window after 4 s | **0-byte MP4**, `moov atom not found` |
| Documented GUI launcher | `./scripts/anycam-gui` | **exit 127** (stale venv shebang) |

Working paths that must not regress: `demo` record/preview, overlays, effects,
audio mixing, playlist rotation, MP4 finalisation on explicit `stop()`,
`ffprobe` verification.

---

## Phase 1 — P0: restore the core product

### P0-1. Virtual camera branch segfaults
**Symptom.** `anycam demo --source testsrc --vcam` dies with SIGSEGV; no report
or JSON is written.
**Evidence.**
- Reproduced 5/5: `--vcam` alone, `--vcam --preview`, `--vcam --record`,
  `--vcam --fps 30`. Exit code 139, core dumped.
- Crash stack ends in `libgstvideo4linux2.so` via a recursive
  `gst_pad_query → gst_pad_peer_query_caps → gst_pad_proxy_query_caps` loop
  (stack overflow).
- Single-layer scene does not crash but yields `frames = 0` (stalled
  negotiation); inserting `videorate` turns it into
  `gst_queue_handle_sink_event … reason error (-5)` instead of a crash.
- Control: `gst-launch-1.0 videotestsrc ! videoconvert ! video/x-raw,format=YUY2,width=1920,height=1080,framerate=30/1 ! v4l2sink device=/dev/video10`
  runs clean, so the device and element are fine.

**Cause.** `pipeline.py:335` (`_build_vcam`) builds
`queue → videoconvert → capsfilter(YUY2 1920×1080@**30**) → v4l2sink` while the
compositor emits RGBA@60 (`pipeline.py:347` hard-codes 30 fps). There is no
`videorate` in the branch and no explicit format/rate negotiation, so the
branch can never agree with the compositor.

**Fix direction.**
1. Derive the vcam caps from the project (`self.fps`, canvas size) and insert
   `videorate` (and `videoscale` if the device needs it) before the caps.
2. Probe the loopback device's supported format with `v4l2-ctl --list-formats-ext`
   and pick YUY2/NV12 accordingly; fall back to `v4l2convert` if available.
3. Fail fast with `report.vcam_error` instead of negotiating into a crash.
4. Keep `queue leaky=2`; verify the branch does not back-pressure the compositor.

**Files.** `src/anycam/pipeline.py`, `src/anycam/sources.py` (caps helper),
`src/anycam/config.py` (device format probe).

**Acceptance.**
- `anycam demo --seconds 10 --source testsrc --vcam --preview --json out.json`
  exits 0, `vcam_active: true`, `error: ""`, `frames` ≈ 600.
- `v4l2-ctl -d /dev/video10 --get-fmt-video` reports YUYV at the canvas size.
- Firefox enumerates the device and shows the composed scene (re-capture
  `evidence/02_virtual_camera/verified.txt`).
- 3 consecutive runs without a crash.

### P0-2. GUI never shows a live preview
**Symptom.** Preview pane always paints "Preview stopped"; F5 snapshot says
"No frame to snapshot".
**Evidence.** `gui.py:739` passes `preview=False`; `gui.py:742` passes
`on_frame=None`. The only consumer, `gui.py:840`, is guarded by
`engine._appsink is not None and engine.on_frame is not None`, so neither branch
is ever built or polled. Offscreen run: `appsink: None`, `preview.image: None`,
`_pending_frame: False` after 3 s. `frame_ready` is connected at `gui.py:116`
but never emitted. Verified fix: `AnyCamEngine(..., preview=True, on_frame=w._on_frame)`
→ 88 samples in 3 s, `preview.image = QSize(640, 360)`, no error.

**Fix direction.**
1. In `start_engine()`: `preview=True` and `on_frame=self._on_frame`.
2. In `_tick()`: pull from `engine._appsink` unconditionally when it exists; drop
   the `on_frame is not None` guard.
3. Emit `frame_ready` from `_on_frame` (queued connection) and let the slot
   convert the buffer, instead of the current mix of direct calls and a dead
   signal.
4. Fix stride handling: use `width * 4` (or `GstVideoInfo`) rather than
   `info.size // height`.
5. Re-wire `snapshot()` to the compositor frame, not the widget pixmap.

**Files.** `src/anycam/gui.py`, `src/anycam/pipeline.py`.

**Acceptance.**
- Offscreen: after 3 s `window.preview.image` is non-null and matches canvas size.
- Offscreen: `snapshot()` writes a PNG that opens and is > 0 bytes.
- `evidence/00_skeleton/gui.png` regenerated from current code and shows a live
  frame (it is currently stale — it shows output the code cannot produce).

### P0-3. Default GUI start stops the pipeline with `not-linked`
**Symptom.** With default settings the stream errors ~0.2 s after start:
`imagefreeze0: streaming stopped, reason not-linked (-1)`.
**Evidence.** `_tick` at `gui.py:857` sees `report.error` and calls
`stop_engine()`; status bar shows the message above. Root cause: no output
branch is built (preview off, no record/vcam/RTMP), so the `tee` has zero src
pads and the compositor output is unlinked.

**Fix direction.**
1. P0-2 makes the preview branch always present — this removes the trigger.
2. Additionally guard `build()`/`start()`: if no branch was requested, raise a
   clear error (`"no output branch: enable preview, recording, vcam or RTMP"`)
   instead of letting GStreamer fail asynchronously.
3. On pipeline error, stop the engine **and** surface it non-fatally (status +
   re-enable Start button), never leave a half-dead state.

**Files.** `src/anycam/pipeline.py` (`build`), `src/anycam/gui.py` (`_tick`).

**Acceptance.** Default `Start preview` on `testsrc` runs ≥ 60 s with
`error == ""` and a moving preview.

### P0-4. `anycam gui --screenshot` hangs forever
**Symptom.** The documented headless GUI check never exits.
**Evidence.** Killed at 45 s and 90 s (exit 124). `faulthandler` stack:
`run_screenshot` (`gui.py:902`) → `QCoreApplication::exec`. Cause: P0-3 stops the
engine, `_tick` returns at its first line (`engine is None`), so
`_render_seconds`/`_auto_screenshot` can never fire and `quit()` is never called.
Two further hang paths: a modal `QMessageBox.critical` in `start_engine()` when
no user can dismiss it, and `run_screenshot` not using the `os._exit` teardown
workaround that `run_gui` uses.

**Fix direction.**
1. Add a hard deadline timer in screenshot mode that grabs + saves + quits
   regardless of engine state, and exits non-zero if no frame was rendered.
2. In headless mode, never show modal dialogs — write the error to stderr and
   exit non-zero.
3. Give `run_screenshot` the same teardown guard as `run_gui`.

**Files.** `src/anycam/gui.py`, `src/anycam/__main__.py`.

**Acceptance.**
- `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 -m anycam gui --screenshot /tmp/g.png --seconds 3 --source testsrc`
  returns 0 in < 15 s and `/tmp/g.png` exists and is non-trivial.
- A deliberately broken scene returns non-zero within 15 s, no hang.

### P0-5. Closing the GUI while recording destroys the file
**Symptom.** 0-byte MP4, `ffprobe: moov atom not found`.
**Evidence.** No `closeEvent`/`aboutToQuit`/`atexit` hook exists (grep: 0
matches). `run_gui` calls `os._exit()` at `gui.py:885` immediately after
`app.exec()`, so `engine.stop()` (which pushes EOS and finalises the `moov`) is
never reached. Verified by closing the window 4 s into a recording.
This also loses vcam/RTMP cleanliness and any in-flight snapshot.

**Fix direction.**
1. Implement `AnyCamWindow.closeEvent`: stop the timer, `stop_engine()`, wait for
   the MP4 finalise (bounded), then accept the event.
2. Connect `QApplication.aboutToQuit` to the same shutdown path as a backstop.
3. Only then use `os._exit`, and keep it as the last statement.

**Files.** `src/anycam/gui.py`.

**Acceptance.** Start recording → close window after 5 s → the MP4 is playable
(`ffprobe` shows duration and a video stream, `moov` present) and > 0 bytes.
Repeat for pre-existing frames to ensure no regression.

---

## Phase 2 — P1: restore trust in the project

### P1-1. `--shot` and `--shots-prefix` never capture a frame
**Symptom.** `WARNING: no composited frame captured`; no PNG written.
**Evidence.** `pipeline.py:99` stores `self.on_frame` but it is **never
called**. `_on_preview_sample` exists but is never connected (`new-sample` is
explicitly not connected, `pipeline.py:259`), and `run()` never polls the
appsink. The only poll site is `gui.py:840`, which is disabled (P0-2).

**Fix direction.** Give the engine a single, documented frame-delivery path:
connect `appsink::new-sample` to `_on_preview_sample` (queue + drop policy) and
invoke `on_frame` from `pump()`/`run()` on the main thread; or connect
`new-sample` with `emit-signals` and marshal into the GUI. Remove the dead
`_frame_queue = None` stub.

**Files.** `src/anycam/pipeline.py`, `src/anycam/demo.py`, `src/anycam/gui.py`.

**Acceptance.**
- `anycam demo --seconds 3 --source testsrc --shot /tmp/s.png --shot-at 1`
  writes a valid PNG (non-black, contains the text layers).
- `--shots-prefix` writes N PNGs at the expected cadence.
- Add a test (`tests/test_pipeline.py`) that asserts the shot callback fired.

### P1-2. Test suite is red / README count is wrong
**Symptom.** `tests/test_modes.py:39 test_mjpg_preferred_at_same_rate` fails:
`assert 'YUYV' == 'MJPG'`.
**Cause.** `sources.py:80` orders `PREFERRED_FORMATS = ("YUYV","NV12","MJPG","H264")`,
so `_format_rank` prefers YUYV — the opposite of what `choose_from_modes`'
docstring and the README promise. The test encodes the intended behaviour.

**Fix direction.** Decide the intended rule (README says MJPG is preferred
because it is the only 1080p60 format and uses less USB bandwidth), make
`PREFERRED_FORMATS = ("MJPG","H264","NV12","YUYV")` or rank explicitly, then
re-run the full suite.

**Acceptance.** `PYTHONPATH=src python3 -m pytest tests -q` → 26 passed (update
the README/`PARITY.md` count to match the real number).

### P1-3. Stale `manycam linux` paths break the launcher and desktop entry
**Symptom.** `./scripts/anycam-gui` exits 127.
**Evidence.**
- `.venv/lib/python3.14/site-packages/__editable__.anycam-0.1.0.pth` contains
  `/home/quinton/Projects/manycam linux/src` → `import anycam` fails inside the
  venv.
- `.venv/bin/anycam` shebang points at the old interpreter → console script
  fails; `scripts/anycam-gui` prefers it and therefore never starts.
- `anycam.desktop` `Exec`/`TryExec`/`Icon` point at
  `/home/quinton/Projects/manycam linux/...` (nonexistent).
- README "Build and run" tells the user to `cd "/home/quinton/Projects/manycam linux"`.
- Stale `tests/__pycache__` embeds the old path, so tracebacks cite the old
  directory.

**Fix direction.**
1. Re-create the venv (`scripts/setup.sh` after deleting `.venv`) or rewrite the
   `.pth`/console-script shebangs; confirm `.venv/bin/anycam --version`.
2. Make `anycam.desktop` path-independent: `Exec=anycam gui`,
   `TryExec=anycam` (plus a note to install the script on `PATH`), or generate
   the file at install time. `Icon=anycam` with an installed icon theme, or a
   relative-to-repo fallback documented in README.
3. Update README build instructions to the actual checkout path (or use `cd` to
   "the repository root").
4. Remove `tests/__pycache__` (and add it to `.gitignore` if missing).

**Acceptance.**
- `./scripts/anycam-gui` launches (offscreen smoke: `--screenshot` returns 0).
- `desktop-file-validate anycam.desktop` passes *and* `TryExec`/`Icon` targets
  resolve on this machine.
- `import anycam` works inside `.venv/bin/python`.

### P1-4. Every engine restart starts a new recording file
**Symptom.** One session produces many `anycam-*.mp4` files; the status bar only
mentions the last.
**Evidence.** `_record_path()` (`gui.py:721`) mints a new timestamped name on
each `start_engine()`; `restart_engine()` is triggered by scene switch, **every
layer-visibility checkbox**, "Apply effects", mic device change, vcam toggle and
playlist edits. Verified indirectly: each GUI start/stop pair creates a new file.

**Fix direction.**
1. Keep a single recording path for the lifetime of a "recording session"
   (set on `Record` press, cleared on `Stop recording`), and re-open/continue on
   restart — or explicitly finalise + announce the previous file when the graph
   is rebuilt.
2. Use `set_layer_visible()` for visibility toggles instead of `restart_engine()`
   (as the playlist already does); use `set_mic_gain()`/`set_mic_muted()` for
   those controls (already implemented, currently bypassed).
3. Add millisecond precision to `timestamped_name` to avoid same-second
   collisions.

**Files.** `src/anycam/gui.py`, `src/anycam/overlay.py`.

**Acceptance.** Toggle three layer checkboxes and switch a scene while
recording → exactly one MP4 exists for the session and it is playable.

### P1-5. Saved projects are never loaded
**Symptom.** "Save project" persists, but restarting the app always shows the
default project.
**Evidence.** `config.project_path()` is only ever written (`save_project`); no
`Project.load` call exists in the GUI or entry point.

**Fix direction.** Load `config.project_path()` in `run_gui`/`AnyCamWindow.__init__`
with a `try/except` fallback to `default_project()`; add a "Reload project"
action; surface a status message when a saved project is loaded or fails.

**Acceptance.** Change canvas/scene → Save → relaunch → values are restored.
Corrupt JSON → app starts with defaults and a warning (no traceback).

---

## Phase 3 — P2: correctness and code health

### P2-1. Model fields with no implementation
`Effects.background_mode/background_blur/background_image`,
`Effects.face_effect/face_strength` (GUI combo/slider exist and do nothing),
`Effects.chroma_smoothness`, `AudioConfig.media_loop`, `Layer.volume/muted`,
`Layer.playlist`/`PlaylistItem` (GUI uses separate layers), `Scene.transition`.
`face.FaceDetector` is never imported by `gui.py`/`pipeline.py`.

Plan: either implement or remove before 1.0 — no silent no-op controls.
- Implement `media_loop` with a loop-aware `uridecodebin`/`filesrc` segment seek.
- Implemented face overlay by feeding `FaceDetector` from the preview frames and
  positioning an RGBA layer on the compositor (uses P1-1's frame path).
- Background blur/replacement controls remain disabled and labelled; the face
  controls are now active.
- Drop or document `Layer.volume/muted`, `Layer.playlist`, `Scene.transition`.

### P2-2. Packaging and dependency declarations
- `[tool.setuptools.package-data]` points at `ui/*.qss` (directory is empty) and
  `assets/*` (assets live at the repo root, not in the package) → wheels ship
  neither logo nor `yunet.onnx`, and `config.assets_dir()` (`parents[2]`) only
  works from a checkout.
- `opencv-python-headless` is a hard dependency but never imported (face uses
  `onnxruntime` + PIL); it is duplicated under the `face` extra with a stale
  comment; `onnxruntime` is required by `face.py` but **not declared at all**.
- `setup.sh` installs with `--no-deps`, so nothing verifies the declared deps.

Plan: move runtime assets into `src/anycam/assets/` (or add `data-files`),
resolve them via `importlib.resources` with a checkout fallback; drop the opencv
dependency or actually use it; add `onnxruntime` to the `face` extra; add a
smoke check in `setup.sh` that imports the modules the package declares.

### P2-3. Audio robustness
A missing `media_file` is silently ignored; the mic list is built once at window
construction (no hot-plug refresh); there is no level metering next to the gain
slider.

Plan: append a `media_error` to the report/status when the file is missing; add
a Refresh button for devices; add a `level` element + timer meter in the Audio
tab.

### P2-4. Stop/EOF semantics
`stop()` waits at most 4 s for EOS then sets `NULL` **without flagging failure**
(a slow finalise silently truncates the MP4 while still reporting
`record_bytes`); `pump()` treats ERROR as EOS (`_eos_seen = True`), so `run()`
cannot distinguish failure from a clean end.

Plan: separate `_error_seen` from `_eos_seen`; in `stop()` record
`finalised = bool(eos)` and set `report.error` if false; verify the MP4 in
`stop()` when recording and surface `record_ok`.

### P2-5. Dead code / unused imports
`_on_preview_sample`, `_frame_queue`, `AnycamEngine.on_frame` (GUI),
`statistics` in `pipeline.py` and `soak.py`, `QAction`/`QFont` in `gui.py`,
`Path` in `tests/test_overlay.py`, duplicate `import os` in `run_gui`, the
unreachable `soak` subparser in `__main__.py`.

Plan: delete after P0-2/P1-1 land (the preview path rewrite supersedes the dead
code); sift with `ruff --select F` (adds a dev dependency — optional).

### P2-6. Geometry fork
`geometry.crop_pixels/layer_geometry/fit_contain` are used only by tests; the
engine has its own `_box_pixels`, with different clamps (`0.01`/`max(2,…)` vs
`0.001`/`max(1,…)`), so the "single place for geometry" is already forked.

Plan: make `pipeline._box_pixels` call `geometry.layer_geometry`, update tests,
and add a test that the compositor pad (xpos/ypos/width/height) equals
`layer_geometry(...)` for a few representative boxes/crops.

### P2-7. `image_chain` ignores `layer.crop`
Cropping an image or logo layer has no effect. Plan: insert
`_crop_chain(layer, …)` in `image_chain` (before `videoscale`), and cover it with
the geometry test above.

### P2-8. Camera mode rule vs README
`choose_from_modes` locks to the exact size even if that forces 30 fps while
another size offers 60 — the README's "will never silently settle for 30 fps"
only holds per-size. Also `camera_modes()` swallows probe errors/timeouts and
returns `[]`, which silently becomes a `MJPG` request.

Plan: when no exact-size 60 fps mode exists, prefer a same-or-higher-fps mode at
another size over a 30 fps exact-size mode (or document the current behaviour
explicitly); expose "probe failed" in the report so the silent default is
visible.

### P2-9. `_effects_chain` signature
Takes `(layer, width, height, fps)` and uses only `layer`. Plan: drop the unused
parameters or use them (they look intended for resolution-dependent blur).

---

## Phase 4 — Missing features (product backlog)

Ordered roughly by user value. These are **not** bugs; they are gaps between the
"virtual camera studio" claim and what ships.

| ID | Feature | Notes |
|---|---|---|
| F-1 | Layer transform UI | **Implemented 2026-10-30**: drag/move, eight resize handles, crop mode (trim + pan), arrow-key nudging, numeric box/crop fields, opacity slider, Raise/Lower/Top/Bottom ordering, multi-select group moves, configurable alignment/spacing guides, canvas-alignment shortcuts, duplicate, and snapshot undo/redo including add/remove/duplicate; live pad updates; offscreen mouse/key tests. |
| F-2 | Screen capture in the GUI | **Implemented 2026-10-30**: Layers → + Screen drives xdg-desktop-portal ScreenCast (`portal.py`), keeps the PipeWire node/fd alive, and re-requests access for saved screen layers on the next engine start. Still **partial** in PARITY because no live capture artifact is checked in. |
| F-3 | Video-file and network (URL/RTSP) layers | **Implemented 2026-10-30**: Layers tab has + Video file, + Image and + Network URL buttons; a local RTSP server integration test now drives `uridecodebin`/`rtspsrc`. No explicit backpressure handling. |
| F-4 | Background blur / replacement | `Effects.background_*` fields only; needs segmentation/matte and a `compositor`-based two-layer graph. |
| F-5 | Face-tracked effect | **Implemented 2026-10-30**: `face.FaceDetector` runs the bundled YuNet model on preview frames; the GUI adds a `face` compositor layer and tracks the largest detection with live pad moves. Offscreen evidence in `evidence/09_face/`. |
| F-6 | Scene preset browser | `save_scene_preset`/`load_scene_preset` exist; no UI, no project load (see P1-5). |
| F-7 | Scene transitions | `Scene.transition` field unused; currently hard cuts via engine restart. |
| F-8 | Audio level meter / monitoring | a mic dB label appears while a recorder/RTMP mux is active; there is no monitor output, ducking, noise suppression or media meter. |
| F-9 | RTMP robustness | no reconnect/backoff, no bitrate/resolution settings, no connection status; publish settings are hard-coded (`6000 kbps`, canvas size). |
| F-10 | Recording settings | no codec/bitrate/container choice beyond `ANYCAM_ENCODER`; filename pattern and directory are fixed. |
| F-11 | Hot-plug device refresh | **Implemented 2026-10-30 (offscreen; physical pull blocked)**: each camera layer has a V4L2 device picker, per-layer capture size/FPS overrides and the camera/mic lists have Refresh. A camera that is missing when preview starts or disappears mid-stream keeps its box as a generated **Camera disconnected** placeholder card (other layers keep compositing), and a 1.2 s watcher rebuilds the real layer when the device node returns. A device that returns under a different `/dev/video*` name still needs manual re-selection. `scripts/verify-camera-reconnect.py` is ready for a real cable pull; two live attempts are recorded as blocked in `evidence/12_camera_reconnect/`. |
| F-12 | GUI test coverage | **Implemented 2026-10-30**: `tests/test_gui.py` has 35 offscreen tests covering preview delivery, snapshot, close-while-recording, screenshot deadline, CLI screenshot, interactive move/resize/crop, nudging, numeric fields, opacity/order, multi-select, alignment/spacing guides, configurable snap, alignment shortcuts, undo/redo including layer add/remove/duplicate, image/video/network/screen layer creation, playlist play-once/loop, persisted playlist/snap state, per-layer camera device/mode switching, two-camera preset, the configurable 2×2 camera grid, camera loss with placeholder frames and reconnect, autosave on close, and editing during a recording that finalises both segments. Start/stop remains implicitly covered by the preview test. |
| F-13 | CI / lint | **Partially implemented 2026-10-30**: `.github/workflows/ci.yml` compiles and runs the full suite on Ubuntu 24.04 with system GStreamer/PyGObject; there is still no ruff/flake8 config. |

---

## Phase 5 — Documentation and evidence corrections

Everything below currently overstates the working state and must be updated
**after** the fixes land (not before):

1. `README.md`
   - "Current state: … end-to-end … working" → qualify once P0-1/P0-2 are fixed.
   - "Verified today" table: vcam and GUI preview rows are false today.
   - Build path `cd "/home/quinton/Projects/manycam linux"` → real path or "repo root".
   - "Tests: 25 passed" → real count (26 collected, 1 failing now).
   - Architecture diagram says the preview appsink is always there; the GUI does
     not build it.
   - Known limitations must list: vcam crash (until fixed), GUI preview disabled,
     no layer transform UI, no project load.
2. `PARITY.md`
   - "V4L2 virtual camera + Firefox — verified" and "Live preview — verified" →
     `blocked`/`partial` with the current evidence until re-verified.
   - "Desktop entry — verified (`desktop-file-validate` passes)" → note that the
     target paths were stale and that validation does not check them.
3. `evidence/00_skeleton/gui.png` is stale (shows a composed preview the current
   code cannot render). Regenerate it as part of P0-2 and note the date/commit.
4. Add a short `docs/REGRESSIONS.md` (or a section in `docs/EVIDENCE.md`) that
   records this audit, the reproduction commands, and the before/after results,
   so the same regressions are not silently re-introduced.

---

## Definition of done (v0.1.1)

- [x] `pytest` fully green (61/61) and the README count matches.
- [x] `anycam demo --vcam --preview --record` runs 60 s, exit 0, no crash, report
      clean, MP4 + vcam both verified.
- [x] `anycam gui --screenshot` terminates in < 15 s with a live preview PNG.
- [x] Closing the GUI mid-recording yields a playable MP4.
- [x] `./scripts/anycam-gui` launches; `anycam.desktop` targets resolve.
- [x] Default GUI start runs ≥ 60 s without error.
- [x] `--shot` / `--shots-prefix` produce PNGs; test added.
- [x] A saved project survives a restart.
- [x] No layer-visibility toggle restarts the graph or the recording.
- [x] README/PARITY/evidence match the verified state.

Note: a scene/effect change still rebuilds the graph and therefore starts a new
MP4 segment; segments are finalised, playable and reported. Merging them would
require a persistent recorder pipeline (F-10 follow-up).

---

## Suggested execution order

1. **P1-2 + P1-3** first (cheap, unblocks all other verification: a working venv,
   a launchable GUI and a green suite).
2. **P0-2 + P0-3** together (the preview branch both fixes the preview and
   removes the `not-linked` trigger), then **P0-4** (screenshot deadline) and
   **P0-5** (close finalisation). This makes the GUI usable.
3. **P0-1** (vcam) — the deepest investigation; budget for a GStreamer
   negotiation bisect and keep the `gst-launch` control pipeline as the oracle.
4. **P1-1** (frame delivery path) — needed by snapshots and by F-5 later.
5. **P1-4, P1-5**, then P2 items as cleanup.
6. Update docs and evidence last, from real captured artifacts.

Each step ends with the acceptance command for that item and a fresh record in
`evidence/`; do not update the PARITY table from intent.
