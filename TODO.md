# AnyCam open work

Backlog derived from the 2026-10-31 full audit (55 bugs, M0–M9) and the work
landed since. Snapshot: `master` at `9718a3a`; suite **123 collected, 122 passed,
1 hardware-gated multicam skip**.

`PLAN.md` is the older 2026-10-30 plan; this file tracks the newer audit's open
side. Closed items live in `docs/REGRESSIONS.md` and are not repeated here.

Legend: `[ ]` open · `[~]` in progress. Bug IDs (`B12`) are the audit's.

## 0. M1 acceptance gates — do these first

- [ ] **10-minute A/V drift gate.** `PYTHONPATH=src python3 scripts/verify-av-drift.py --seconds 600 --warmup 20 --record /tmp/drift.mp4 --json /tmp/drift.json`. The 20 s run currently passes with `drift_s: 0.0`, zero duplicate/gap timestamps and a finalised 20.03 s MP4; the 10-minute result must be recorded in `evidence/` (or the private archive).
- [ ] **60-minute soak** tracking RSS, tmpfs usage, disk and file descriptors; fail the run on a frame stall. `soak.py` currently samples CPU/RSS only and has no stall watchdog.
- [ ] **Continuous MP4** across five scene switches and record toggles, plus an unbroken `v4l2src` consumer on the virtual camera (B12). Today each graph rebuild finalises a new `-segN.mp4`.
- [ ] **Unresponsive RTSP/network source**: canvas keeps ≥99% of its frames and `stop()` returns in under 1 s (B10/B11). Add a failure-injection test next to `tests/test_m1_outputs.py`; `tests/test_rtsp.py` only exercises a healthy feed.
- [ ] **Refused output does not poison the session** (B11): preview, recording and vcam keep running, and the recording flag does not stay on.
- [ ] **Physical camera unplug/replug** on real hardware with `scripts/verify-camera-reconnect.py`. Needs the user and a cable.

## 1. M1 engine split and source/output correctness

- [ ] Split `pipeline.py` into `src/anycam/engine/`: `source_pool.py`, `canvas.py`, `outputs/{vcam,recorder,stream}.py`, `health.py`, `controller.py`; keep `AnyCamEngine` as a thin facade while tests migrate.
- [ ] Canvas is never rebuilt. Record/stop, vcam toggle, scene switch, add layer and apply-effects attach/detach live (B12).
- [ ] Video layers loop with a segment seek and never end on a silent black frame (B14); a video-only scene must not leave the chip saying LIVE.
- [ ] Hidden playlist layers release their sources; hidden cameras stop holding devices and CPU (B15).
- [ ] Stable camera IDs through `/dev/v4l/by-id`, not `/dev/videoN` (B46).
- [ ] A missing video/image layer renders an error card while the rest of the scene keeps running (B13).
- [ ] Per-source health model and watchdog; FPS stops being a lifetime average; a stalled pipeline is detected (B41).
- [ ] Report every camera's negotiated mode, not only the last one (B48).
- [ ] Vcam: surface runtime errors (B17), check the configured device (B18), and bound the `appsrc` queue (B20).
- [ ] Audio tee gets queues when recording and streaming together, so one muxer cannot block the other (B21).
- [ ] Replace or wrap `rtmpsink` with `rtmp2sink`; keep reconnect/backoff out of the canvas.
- [ ] Verify a duplicated camera layer shares one capture through the pool (B16) and cover it at the GUI level.
- [ ] Resolve the `exclusive_caps=1` documentation contradiction; settle the Chromium/Zoom/Teams compatibility list in M9 (B19).

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
- [ ] Evidence gaps: live portal screen capture, remote RTSP/backpressure, physical unplug, 10-minute soak with the virtual camera.

## 11. Needs the user / hardware

- [ ] Physical camera unplug and replug on the real StreamCam.
- [ ] Live xdg-desktop-portal screen-layer frame-size check (B1).
- [ ] Compatibility matrix on Firefox, Chromium, Zoom, Teams, Meet and OBS.
- [ ] Android and iPhone source/remote tests.
- [ ] Decision to publish the repository.

## 12. Remaining audit bug index

| ID | Open issue | Target |
|---|---|---|
| B1 | Live portal screen frame size still needs one real check | M0 residual |
| B10 | Unresponsive network camera can still hold the canvas | M1 |
| B11 | Refused RTMP/RTSP session state and UI flag | M1 |
| B12 | Every rebuild splits the MP4 and drops outputs | M1 |
| B13 | One missing media file refuses the whole scene | M1 |
| B14 | Video layers do not loop | M1 |
| B15 | Hidden layers still play and hold sources | M1 |
| B16 | Duplicate camera must share one capture (verify) | M1 |
| B17 | Vcam runtime errors are silent | M1 |
| B18 | Vcam availability checks a different device | M1 |
| B19 | `exclusive_caps=1` docs/error contradiction | M9 |
| B20 | Vcam `appsrc` has no queue bound | M1 |
| B21 | Audio tee has no queues for record + stream | M1 |
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
| B48 | Report keeps only the last camera mode | M1 |
| B52 | Doctor/setup do not require every plugin | M9 |
| B53 | PySide6/PyGObject undeclared; empty `ui/` package data | M9 |
| B55 | Remaining docs/evidence overclaims | Cross-cutting |

## Closed recently (for reference)

- M0 safety/correctness group: B1–B8, B22–B30, B38, B39, B49–B51 (see `docs/REGRESSIONS.md`).
- B31: crop editor overlay (`docs/REGRESSIONS.md`, 2026-10-31).
- M1 slice 1: RTMP output isolation, B9 (`tests/test_m1_outputs.py`).
- M1 slice 2: ref-counted source pool, dead-capture isolation, output-pad EOS, duplicate-PTS handling (`tests/test_source_pool.py`).
- A/V drift harness: 20 s synthetic run with 0.0 s drift and a finalised MP4 (`scripts/verify-av-drift.py`).
- Repo hygiene B54: git history, evidence moved out, launcher/desktop fixed, 123-test suite.
