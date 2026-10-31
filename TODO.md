# AnyCam open work

Backlog derived from the 2026-10-31 full audit (55 bugs, M0–M9) and the work
landed since. Snapshot: 2026-10-31, after the camera-replug fix and the
10-minute soak re-run; suite **150 collected, 149 passed, 1 hardware-gated
multicam skip**.

`PLAN.md` is the older 2026-10-30 plan; this file tracks the newer audit's open
side. Closed items live in `docs/REGRESSIONS.md` and are not repeated here.

Legend: `[ ]` open · `[~]` in progress. Bug IDs (`B12`) are the audit's.

## 0. M1 acceptance gates — do these first

- [x] **10-minute A/V drift gate.** `PYTHONPATH=src python3 scripts/verify-av-drift.py --seconds 600 --warmup 20 --record /tmp/drift.mp4 --json /tmp/drift.json`. Passed 2026-10-31: `drift_s: 0.0`, 36,008 video / 24,405 audio buffers, zero duplicate/gap timestamps, finalised 600.12 s MP4.
- [~] **60-minute soak** tracking RSS, tmpfs usage, disk and file descriptors; fail the run on a frame stall. Instrumentation landed 2026-10-31 (tmpfs, recording filesystem, FDs, `--stall-seconds`); the 10-minute synthetic re-run passed at 0.043 MiB/min with no stalls. Run 60 minutes with vcam.
- [~] **Continuous MP4** across five scene switches and record toggles, plus an unbroken `v4l2src` consumer on the virtual camera (B12). A persistent `Recorder` and a session-owned vcam output now span canvas rebuilds, and the five-scene-switch gate passes with synthetic sources (2026-10-31); the 60-minute soak and a real loopback consumer remain.
- [x] **Unresponsive RTSP/network source**: canvas keeps ≥99% of its frames and `stop()` returns in under 1 s (B10/B11). Passed 2026-10-31 with a local RTSP server that accepts and never answers: ≥99% of 30 fps over a 2 s window, `stop()` 0.32 s.
- [x] **Refused output does not poison the session** (B11): preview, recording and vcam keep running, and the recording flag does not stay on. Passed 2026-10-31 with a missing `rtmpsink` and with a forced rebuild failure.
- [ ] **Physical camera unplug/replug** on real hardware with `scripts/verify-camera-reconnect.py`. Needs the user and a cable; the app now resolves the by-id path.
- [ ] **UVC wedge recovery**: after repeated open failures, offer a USB reset (pkexec) instead of requiring a reboot.

## 1. M1 engine split and source/output correctness

- [ ] Split `pipeline.py` into `src/anycam/engine/`: `source_pool.py`, `canvas.py`, `outputs/{vcam,recorder,stream}.py`, `health.py`, `controller.py`; keep `AnyCamEngine` as a thin facade while tests migrate.
- [ ] Canvas is never rebuilt. Record/stop, vcam toggle, scene switch, add layer and apply-effects attach/detach live (B12).
- [x] Video layers loop with a segment seek and never end on a silent black frame (B14); a video-only scene must not leave the chip saying LIVE. Passed 2026-10-31: a 1 s red clip seeked after EOS, `loops >= 1`, and the centre was still red after 2.6 s.
- [x] Hidden playlist layers release their sources; hidden cameras stop holding devices and CPU (B15). Passed 2026-10-31: hidden capture layers acquire no pool entry; showing acquires on the stable channel, hiding releases it again.
- [ ] Stable camera IDs through `/dev/v4l/by-id`, not `/dev/videoN` (B46). **App side implemented 2026-10-31; needs a physical replug run.**
- [x] A missing video/image layer renders an error card while the rest of the scene keeps running (B13). Passed 2026-10-31: missing image and video layers each build a card, and the test layer keeps producing frames.
- [ ] Per-source health model and watchdog; FPS stops being a lifetime average; a stalled pipeline is detected (B41).
- [x] Report every camera's negotiated mode, not only the last one (B48). Passed 2026-10-31: two camera layers report both negotiated modes; a hidden camera adds its mode when shown.
- [x] Vcam: surface runtime errors (B17), check the configured device (B18), and bound the `appsrc` queue (B20). Passed 2026-10-31: inactive outputs report their error, the explicit device is checked, and both vcam appsrc paths use `max-buffers=2`, `block=false`, downstream leak.
- [x] Audio tee gets queues when recording and streaming together, so one muxer cannot block the other (B21). Passed 2026-10-31: separate bounded queues feed the MP4 and RTMP muxers.
- [ ] Replace or wrap `rtmpsink` with `rtmp2sink`; keep reconnect/backoff out of the canvas.
- [x] Verify a duplicated camera layer shares one capture through the pool (B16) and cover it at the GUI level. Passed 2026-10-31: one pool entry with two references after `duplicate_layer()`; the shared-channel `intervideosrc` name collision is fixed.
- [x] Resolve the `exclusive_caps=1` documentation contradiction; settle the Chromium/Zoom/Teams compatibility list in M9 (B19). Docs now require the default `exclusive_caps=0`; the browser/meeting matrix remains an M9 hardware task.

## 2. M2 — GPU canvas, transforms, transitions

- [ ] GL backend (`glvideomixer` + shaders) with the current `compositor` as CPU fallback.
- [ ] Encoder selection: NVENC when available, x264 fallback (B42).
- [ ] Fit modes contain/cover/stretch, reusing `geometry.fit_contain`.
- [ ] Flip, rotate, zoom/pan, rounded corners, off-canvas boxes, RGB levels.
- [ ] Chroma shader with smoothness and spill removal; live effect changes, no Apply button.
- [ ] Program and Preview canvases; TRANS and CUT; cut/fade/slide/wipe transitions.
- [ ] Performance gate: ≤100% CPU for 1080p60 with 3 layers + recording on this machine (spike: 68%), and stable 4K30. Failing this re-opens the stack decision.
- [ ] Golden-pixel tests per transform; CI runs on software GL.

## 3. M3 — studio UI

- [ ] Split `gui.py` into `ui/` modules.
- [ ] Text/title editor with lower-third templates, dynamic clock and timer (B32).
- [ ] Effects panel follows the selected layer and updates without a rebuild (B33).
- [ ] Reload refreshes audio widgets from the model (B34).
- [ ] Persist "Include microphone" and vcam on/off (B35).
- [ ] Window fits 1366×768; numeric fields readable; remove the dead right strip (B36).
- [ ] Non-blocking portal flow with restore tokens; no 120 s UI stalls (B37).
- [ ] Canvas/FPS edits apply while running, with a clear hint (B40).
- [ ] Playlist: fix the double-length first item and undo of membership changes (B44).
- [ ] One scene-switch path; Ctrl+1…9 no longer skips a step (B45).
- [ ] Program/Preview monitors, live preset thumbnails, realtime toolbar, properties panel that follows selection.
- [ ] Rename/reorder/duplicate scenes and layers; lock layers.
- [ ] Open, Save As, import/export projects and presets; layout preset gallery.
- [ ] Camera control panel; custom local and global hotkeys; full playlist.
- [ ] Screen region capture.
- [ ] Translation scaffold (German + English) and keyboard/screen-reader pass.
- [ ] Acceptance: every control has an offscreen test; every setting survives restart; no UI stall over 50 ms during a live operation.

## 4. M4 — audio

- [ ] Mixer with many sources and always-on meters; desktop audio via PipeWire monitor sources.
- [ ] Audio from video layers using the reserved `Layer.volume` / `Layer.muted`.
- [ ] Working media loop and audio playlist.
- [ ] Virtual microphone as a PipeWire Audio/Source node; monitor output.
- [ ] Noise suppression (`webrtcdsp` / noise-suppression-for-voice), gate, compressor, EQ presets.
- [ ] Per-source sync offset.
- [ ] Acceptance: another app can select AnyCam Mic and receive the mix; no gaps across scene changes; live gain changes without clicks.

## 5. M5 — AI and effects

- [ ] Move inference to a separate worker process; also removes face detection from the UI thread (B43).
- [ ] Person segmentation on ONNX Runtime: blur, remove, replace with image/video/any source.
- [ ] Face landmarks: 2D props and 3D masks that follow the face.
- [ ] Stickers, emoji, animated GIFs; drag and drop; filters and LUTs.
- [ ] Open effect pack format, groups, favourites, original starter pack.
- [ ] Auto-framing.
- [ ] Acceptance: 1080p30 with AI on at ≤1.5 CPU cores without a GPU; a slow model never slows the canvas; crashed worker recovers; every model licence recorded in `NOTICE`.

## 6. M6 — content tools

- [ ] Web source with custom CSS through QtWebEngine (offscreen).
- [ ] PDF through QtPdf; PowerPoint through optional LibreOffice conversion.
- [ ] YouTube and other URLs through optional `yt-dlp`, only when such a source is added.
- [ ] Whiteboard; live draw, text, shapes and region blur.
- [ ] Motion detection: auto-switch scene, local notification, optional email.
- [ ] Floating overlay panel with circle webcam; document Wayland placement limits.
- [ ] MJPEG IP camera test; game capture through window capture.
- [ ] Acceptance: each source type runs inside the M1 failure model; a dead source never affects the canvas.

## 7. M7 — pro outputs

- [ ] Recording settings: codec, bitrate/quality, folder, naming, pause; gallery and disk-space guard.
- [ ] Multiple RTMP destinations, each with status and reconnect/backoff.
- [ ] YouTube/Twitch/Facebook presets; verify `rtmps://`.
- [ ] Auto bitrate, live stats, SRT, optional NDI in/out.
- [ ] Several virtual cameras (`devices=N`), each with its own resolution/FPS.
- [ ] Acceptance: a receiver dropped for 30 s reconnects without touching the recording; three destinations run at once; each stream's GOP matches its FPS.

## 8. M8 — phone camera and remote

- [ ] Local HTTPS + WebRTC page with QR pairing; several phones as sources.
- [ ] Phone remote / stream deck for switching presets.
- [ ] Local-network only, authenticated per device, no cloud.
- [ ] Acceptance: Android and iPhone both work as source and remote; an unpaired device receives nothing. Needs the user's phones.

## 9. M9 — ship 1.0

- [ ] Packages: AUR, Flatpak, AppImage, `.deb`.
- [ ] First-run wizard that sets up `v4l2loopback` via `pkexec`, with a clear undo.
- [ ] Compatibility matrix: Firefox, Chromium, Zoom, Teams, Meet, OBS (settles B19).
- [ ] Declare PySide6 and PyGObject in `pyproject.toml`; fix `ui/*.qss` package data; finish the B53 audit (B53).
- [ ] Doctor checks every required plugin and `setup.sh` fails when an element is missing (B52).
- [ ] `ruff` + coverage in CI; single-instance guard (B47).
- [ ] `NOTICE`, `CHANGELOG`, `CONTRIBUTING`, `SECURITY`, user docs; screenshots from synthetic sources only.
- [ ] Publish the repo only when the user says so.

## 10. Verification and documentation debt

- [ ] Every **R** bug from the audit keeps its own regression test; every **I** bug is reproduced first or closed as not-a-bug in `docs/REGRESSIONS.md`.
- [ ] Automated runs use synthetic sources, Qt offscreen and temp `XDG_*`; they never open the user's camera or saved project.
- [ ] Soak tracks RSS, tmpfs, disk and FDs; frame stalls fail the run.
- [ ] Replace `PLAN.md` with the 2026-10-31 audit (or check the audit text in under `docs/`) so the plan and `PARITY.md` stop disagreeing.
- [ ] Rebuild `PARITY.md` from measured results only; close the remaining overclaims (B55).
- [ ] Fix `scripts/capture-screencast.py`, which reports `ok: true` on the wrong frame (B55).
- [ ] Evidence gaps: live portal screen capture, physical unplug, 10-minute soak with the virtual camera.

## 11. Needs the user / hardware

- [ ] Physical camera unplug and replug on the real StreamCam.
- [ ] Load v4l2loopback (`sudo scripts/enable-v4l2loopback.sh`) and re-run the soak with `--vcam`; without `/dev/video10` the run is blocked on that one gate.
- [ ] Live xdg-desktop-portal screen-layer frame-size check (B1).
- [ ] Compatibility matrix on Firefox, Chromium, Zoom, Teams, Meet and OBS.
- [ ] Android and iPhone source/remote tests.
- [ ] Decision to publish the repository.

## 12. Remaining audit bug index

| ID | Open issue | Target |
|---|---|---|
| B1 | Live portal screen frame size still needs one real check | M0 residual |
| B12 | Every rebuild splits the MP4 and drops outputs | M1 |
| B32 | Text cannot be edited; no font/colour; no renaming | M3 |
| B33 | Effects panel edits the wrong layer; Apply rebuilds | M3 |
| B34 | Reload leaves audio widgets stale | M3 |
| B35 | Mic and vcam toggles are not persisted | M3 |
| B36 | Window minimum height 1109 px | M3 |
| B37 | Portal calls block the UI; no restore token | M3 |
| B40 | Canvas/FPS edits do nothing while running | M3 |
| B41 | FPS is a lifetime average; no watchdog | M1 |
| B42 | `auto` never picks NVENC; fixed bitrate/GOP | M2 |
| B43 | Face detection runs on the UI thread | M5 |
| B44 | First playlist item lasts twice as long | M3 |
| B45 | Three scene-switch implementations; shortcuts skip | M3 |
| B46 | Reconnect only handles a missing node | M1 |
| B47 | No single-instance guard | M9 |
| B52 | Doctor/setup do not require every plugin | M9 |
| B53 | PySide6/PyGObject undeclared; empty `ui/` package data | M9 |
| B55 | Remaining docs/evidence overclaims | Cross-cutting |

## Closed recently (for reference)

- M0 safety/correctness group: B1–B8, B22–B30, B38, B39, B49–B51 (see `docs/REGRESSIONS.md`).
- B31: crop editor overlay (`docs/REGRESSIONS.md`, 2026-10-31).
- M1 slice 1: RTMP output isolation, B9 (`tests/test_m1_outputs.py`).
- B10: a silent RTSP source cannot hold the canvas (`tests/test_m1_outputs.py::test_silent_rtsp_source_cannot_stall_the_canvas`).
- B11: a refused RTMP output cannot abort preview/recording/vcam, and a failed rebuild clears Record (`tests/test_m1_outputs.py::test_missing_rtmp_sink_keeps_preview_and_recording`, `tests/test_gui.py::test_failed_start_clears_recording_state`).
- B13: missing image/video/logo/face assets render an error card without stopping the scene (`tests/test_pipeline.py::test_missing_media_layers_render_cards_without_stopping_scene`).
- B14: video-file captures seek back to the start after EOS and keep the picture (`tests/test_pipeline.py::test_video_layer_loops_without_a_black_frame`).
- B15: hidden capture layers acquire no source until shown and release it when hidden (`tests/test_source_pool.py::test_hidden_capture_is_acquired_only_while_visible`).
- B16: duplicated camera layers share one pool capture (`tests/test_gui.py::test_duplicate_camera_shares_one_pooled_capture`).
- B48: every camera layer's negotiated mode is kept in the report (`tests/test_source_pool.py::test_report_lists_every_camera_mode`).
- B17/B18/B20: vcam failures are reported, the explicit device is checked, and the legacy appsrc is bounded (`tests/test_vcam_output.py`).
- B19: `exclusive_caps=0` is documented as required; the compatibility matrix remains in M9.
- B21: record and RTMP have separate audio queues (`tests/test_m1_outputs.py::test_recording_and_streaming_have_separate_audio_queues`).
- M1 slice 2: ref-counted source pool, dead-capture isolation, output-pad EOS, duplicate-PTS handling (`tests/test_source_pool.py`).
- Persistent recorder: one MP4 across canvas rebuilds, with PTS remap and AAC audio (`tests/test_recorder.py`).
- Session-owned virtual camera: one `v4l2sink` across canvas rebuilds (`tests/test_vcam_output.py`).
- Five-scene-switch recording gate: one MP4 across five canvas rebuilds (`tests/test_gui.py::test_five_scene_switches_keep_one_recording`).
- A/V drift harness: 20 s synthetic run with 0.0 s drift and a finalised MP4 (`scripts/verify-av-drift.py`).
- Repo hygiene B54: git history, evidence moved out, launcher/desktop fixed, 123-test suite.
