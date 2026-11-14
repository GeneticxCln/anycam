# AnyCam open work

Backlog from the 2026-10-31 full audit (55 bugs, M0–M9), the 2026-11-11 audit
(25 findings) and the 2026-11-12 audit (11 items). Snapshot: 2026-11-12, after
the 11-item fixes; suite **1231 collected, the full run passing** (four
hardware/packaging tests skip without their hardware).

`plans/archive/2026-10-30-remediation.md` is the older 2026-10-30 plan; open plans are in `plans/pending/`; this file tracks the newer audits' open
side. Closed items live in `docs/REGRESSIONS.md` and are not repeated here.

Legend: `[ ]` open · `[~]` in progress · `[x]` done. Bug IDs (`B12`) are the
2026-10-31 audit's; "item N" is the 2026-11-12 audit.

## 2026-11-12 audit — the 11 items

- [x] **1. GPU preview crashed on startup.** `ui/preview.enable_gpu` imported
  `anycam.ui.gl_preview`, which does not exist, so the studio died with
  `ImportError` whenever the GPU preview was available. It now does
  `from .. import gl_preview`. Covered by
  `tests/test_gpu_preview.py::test_enable_gpu_imports_the_gl_preview_module_and_builds_the_view`,
  which patches the real module's builders (offscreen cannot create GL).
- [x] **2. Switching scenes froze the GUI with the GPU preview.** Root cause:
  the preview sinks on the canvas tee (`preview_gl_sink`, `preview_cpu_sink`)
  waited for preroll, which deadlocked canvas startup (0 frames, then hangs on
  scene switch and shutdown). Both are now `async=false`. Layer chains are
  stopped first and unlinked after (`engine/layers.py`): unlinking a chain that
  is still PLAYING made its `appsrc` post `not-linked` and stopped the whole
  canvas. Layer audio unlinks before its elements stop. Checked on the real
  display with the GPU preview on both canvases: 60 fps, layer add/remove and
  scene switch under 0.06 s, shutdown 0.22 s.
- [x] **3. Quitting blocked for >8 s with the GPU preview.** `_closing` was set
  before the engine step, which made `_run_with_ui` inline `engine.stop()` on
  the GUI thread. It is now set after the engine step, in a `finally`, so a
  failing stop still keeps the rest off the inline path. Covered by
  `tests/test_housekeeping_2026_11_12.py::test_a_failing_engine_step_still_marks_the_window_closing`.
- [x] **4. Motion detection and the floating panel got no frames under the GPU
  preview.** `_appsink` was deliberately `None` on the GPU path, so `on_frame`
  never fired. `_build_cpu_preview_tap` adds a small CPU branch: on the GPU
  canvas `glcolorscale` to preview size, then `gldownload` of that small texture
  (CPU scaling of the full-size frames segfaulted about every other run during
  an NVENC recording); on the CPU canvas one extra tee branch. Asserted in
  `tests/test_gpu_preview.py::test_the_gpu_branch_uploads_without_the_udmabuf_pool`
  (resize) and
  `::test_on_the_gpu_canvas_the_cpu_tap_scales_on_the_gpu_before_it_downloads`.
- [x] **12. A recording made with the GPU preview lost its sound.** The audio
  mixer's first output was 44100 Hz, then 48000 Hz; the recorder's single AAC
  track ended after ~50 ms. The mixer output is now pinned to 48000 Hz
  (`audio_mixer_caps`). Covered by
  `tests/test_gpu_preview.py::test_the_audio_mixer_output_is_pinned_to_48_khz`
  and, on a real display (`ANYCAM_REAL_DISPLAY=1`),
  `tests/test_real_gpu_preview.py`.
- [x] **5. A torn-down stream could flip back to "live".** Teardown kept the
  publisher, whose stale `first_data` marked the destination live again. It now
  clears `_rtmp_publisher` and `_rtmp_state.publisher`. Covered by
  `tests/test_phase5_outputs.py::test_teardown_forgets_the_publisher_but_keeps_the_encoder`.
- [x] **6. A reconnect did not request a keyframe.** Teardown dropped
  `_rtmp_encoder` even though it lives in the canvas graph, so
  `force_keyframe(None)` was a no-op. The encoder is kept. Covered by
  `tests/test_phase5_outputs.py::test_a_reconnect_forces_a_keyframe_from_the_encoder_kept_in_the_canvas`.
- [x] **7. The effect worker cost more than it saved.** Measured 21–27 ms/frame
  through the worker against 0–5 ms in-process at 1080p, so
  `OutputConfig.effects_isolated` now defaults to `False`. Effect packs still
  always isolate (third-party code never runs in the main process) and
  **Settings → AI effects** can isolate all AI effects. Covered by
  `tests/test_phase6_worker.py::test_the_isolation_setting_reaches_the_worker_switch`.
- [x] **8. Effects flashed off while the worker started or backed off.** The raw
  frame passed through during startup (~0.2 s) and restart backoff (1–10 s).
  `RemoteProcessor._held` keeps the last processed frame instead (same-size
  frames only; the first frames still pass through). Covered by
  `tests/test_phase6_worker.py::test_a_worker_restart_holds_the_last_processed_frame`.
- [x] **9. Monitoring could feed back into desktop audio.** Sending the mix to
  the sink whose `.monitor` source is the desktop capture loops. The monitor is
  disabled with an explanation. Covered by
  `tests/test_phase4_audio.py::test_monitoring_the_desktop_capture_sink_is_refused_as_feedback`.
- [x] **10. PKGBUILD failed on older release tarballs.** The v1.3.2 tarball has
  no `packaging/metainfo/`, so `package()` now installs the AppStream file only
  when present and declares `checkdepends=('python-pytest')`. Parsed by
  `tests/test_phase9_packaging.py`.
- [x] **11. A partial PDF render was cached forever.** `pdftoppm` pages are
  written only after a successful run, guarded by a `.complete` marker; a
  timeout or non-zero exit removes the partial pages. Covered by
  `tests/test_phase7_content.py::test_a_timed_out_pdf_render_is_never_cached`.

## 0. M1 acceptance gates — do these first

- [x] **10-minute A/V drift gate.** `PYTHONPATH=src python3 scripts/verify-av-drift.py --seconds 600 --warmup 20 --record /tmp/drift.mp4 --json /tmp/drift.json`. Passed 2026-10-31: `drift_s: 0.0`, 36,008 video / 24,405 audio buffers, zero duplicate/gap timestamps, finalised 600.12 s MP4.
- [~] **60-minute soak** tracking RSS, tmpfs usage, disk and file descriptors; fail the run on a frame stall. Instrumentation landed 2026-10-31 (tmpfs, recording filesystem, FDs, `--stall-seconds`); the 10-minute synthetic re-run passed at 0.043 MiB/min with no stalls. Run 60 minutes with vcam.
- [~] **Continuous MP4** across five scene switches and record toggles, plus an unbroken `v4l2src` consumer on the virtual camera (B12). A persistent `Recorder` and a session-owned vcam output span canvas rebuilds; Record start/stop and vcam toggles are now live valve changes rather than rebuilds (2026-10-31). The five-scene-switch gate passes with synthetic sources; the 60-minute soak and a real loopback consumer remain.
- [x] **Unresponsive RTSP/network source**: canvas keeps ≥99% of its frames and `stop()` returns in under 1 s (B10/B11). Passed 2026-10-31 with a local RTSP server that accepts and never answers: ≥99% of 30 fps over a 2 s window, `stop()` 0.32 s.
- [x] **Refused output does not poison the session** (B11): preview, recording and vcam keep running, and the recording flag does not stay on. Passed 2026-10-31 with a missing RTMP sink and with a forced rebuild failure.
- [ ] **Physical camera unplug/replug** on real hardware with `scripts/verify-camera-reconnect.py`. Needs the user and a cable; the app now resolves the by-id path.
- [ ] **UVC wedge recovery**: after repeated open failures, offer a USB reset (pkexec) instead of requiring a reboot.

## 1. M1 engine split and source/output correctness

- [x] Split `pipeline.py` into `src/anycam/engine/`: `source_pool.py`, `canvas.py`, `outputs/{vcam,recorder,stream}.py`, `health.py`, `controller.py`; `AnyCamEngine` is a thin facade. Passed: full suite on the split tree.
- [~] Canvas is never rebuilt. Record/stop and vcam toggles attach/detach live through valves (B12, 2026-10-31); scene switches reposition pads without a restart (`tests/test_housekeeping_2026_11_12.py`); structural changes (add layer, effect chain) still rebuild.
- [x] Video layers loop with a segment seek and never end on a silent black frame (B14); a video-only scene must not leave the chip saying LIVE. Passed 2026-10-31: a 1 s red clip seeked after EOS, `loops >= 1`, and the centre was still red after 2.6 s.
- [x] Hidden playlist layers release their sources; hidden cameras stop holding devices and CPU (B15). Passed 2026-10-31: hidden capture layers acquire no pool entry; showing acquires on the stable channel, hiding releases it again.
- [ ] Stable camera IDs through `/dev/v4l/by-id`, not `/dev/videoN` (B46). **App side implemented 2026-10-31; needs a physical replug run.**
- [x] A missing video/image layer renders an error card while the rest of the scene keeps running (B13). Passed 2026-10-31: missing image and video layers each build a card, and the test layer keeps producing frames.
- [x] Per-source health model and watchdog; FPS stops being a lifetime average; a stalled pipeline is detected (B41). Passed 2026-10-31: source-side frame counters, `instant_fps`, and a dead capture flagged after 2 s.
- [x] Report every camera's negotiated mode, not only the last one (B48). Passed 2026-10-31: two camera layers report both negotiated modes; a hidden camera adds its mode when shown.
- [x] Vcam: surface runtime errors (B17), check the configured device (B18), and bound the `appsrc` queue (B20). Passed 2026-10-31: inactive outputs report their error, the explicit device is checked, and both vcam appsrc paths use `max-buffers=2`, `block=false`, downstream leak.
- [x] Audio tee gets queues when recording and streaming together, so one muxer cannot block the other (B21). Passed 2026-10-31: separate bounded queues feed the MP4 and RTMP muxers.
- [x] Prefer `rtmp2sink` and fall back to `rtmpsink`; the publish leg stays in its own pipeline. Passed 2026-10-31: `rtmp2sink` selected when installed and the black-hole server gate still passes. Actual `rtmp2sink` delivery has not been re-captured because no RTMP receiver is installed here.
- [x] Verify a duplicated camera layer shares one capture through the pool (B16) and cover it at the GUI level. Passed 2026-10-31: one pool entry with two references after `duplicate_layer()`; the shared-channel `intervideosrc` name collision is fixed.
- [x] Resolve the `exclusive_caps=1` documentation contradiction; settle the Chromium/Zoom/Teams compatibility list in M9 (B19). Superseded: Discord and Chromium only list a node loaded with `exclusive_caps=1`, which is what the README, `scripts/enable-v4l2loopback.sh` and the app's setup commands now use. The browser/meeting matrix remains an M9 hardware task.

## 2. M2 — GPU canvas, transforms, transitions

- [x] GL backend (`glvideomixer` + shaders) with the current `compositor` as CPU fallback. Passed: `tests/test_gpu_preview.py`, `tests/test_phase3_canvas.py`. The GPU preview keeps a small CPU tap for `on_frame` (motion detection, floating panel; item 4).
- [x] Encoder selection: NVENC when available, VA-API next, x264 fallback (B42). Passed: `tests/test_pipeline.py`, `tests/test_engine_fixes.py`.
- [x] Fit modes contain/cover/stretch (`Layer.fit`). Passed: `tests/test_phase3_canvas.py` (real GStreamer chain pixels, cover window maths).
- [x] Flip, quarter-turn rotate, live zoom/pan, rounded corners (filter), off-canvas boxes, RGB levels. Passed: `tests/test_phase3_canvas.py` (every flip/rotate combination against numpy; canvas pixels for off-canvas boxes). RGB levels run in the GPU shader only, so they are covered by uniform tests here and need a real GL canvas to see.
- [ ] Chroma shader with smoothness and spill removal; live effect changes, no Apply button.
- [x] Cut/fade/slide/wipe transitions between scenes and a Studio mode with a staged scene, CUT and TRANSITION. Passed: `tests/test_phase3_canvas.py` (blend and slide mid-way, clean stop mid-transition). There is no second Preview canvas: the staged scene is chosen, not shown.
- [ ] Performance gate: ≤100% CPU for 1080p60 with 3 layers + recording on this machine (spike: 68%), and stable 4K30. Failing this re-opens the stack decision.
- [ ] Golden-pixel tests per transform: done for the CPU canvas (`tests/test_phase3_canvas.py`). The GPU canvas does not start under Xvfb + llvmpipe here (`glvideomixer` stream error), so GL pixel tests stay hardware-gated.

## 3. M3 — studio UI

- [x] Split `gui.py` into `ui/` modules. Passed: full suite on the split tree.
- [x] Text/title editor with lower-third templates, dynamic clock and timer (B32). Passed: `tests/test_phase2_foundations.py`.
- [x] Effects panel follows the selected layer and updates without a rebuild (B33). Passed 2026-11-06: selected-layer GUI test plus real compositor live property probe.
- [x] Reload refreshes audio widgets from the model (B34). Passed: `tests/test_audit_fixes_2026_11_07b.py::test_reload_refreshes_the_format_and_forgets_old_state`.
- [x] Persist "Include microphone" and vcam on/off (B35). Passed: `tests/test_gui.py` (microphone), `tests/test_housekeeping_2026_11_12.py` (virtual camera).
- [x] Window fits 1366×768 (B36). Passed: `tests/test_phase2_foundations.py::test_the_windows_fit_a_1366_by_768_screen`.
- [x] Non-blocking portal flow; no 120 s UI stalls (B37). Passed: `tests/test_portal.py::test_portal_wait_pumps_the_gui_while_the_picker_is_open`. Restore tokens implemented and stored privately (`tests/test_cleanups.py`).
- [x] Canvas/FPS edits apply while running (B40). Passed: `tests/test_phase2_foundations.py::test_canvas_edits_reach_the_running_preview_after_a_pause`.
- [x] Playlist: the double-length first item is fixed (B44). Passed: `tests/test_phase2_foundations.py::test_the_first_playlist_item_does_not_stay_twice_as_long`.
- [x] One scene-switch path; Ctrl+1…9 no longer skips a step (B45). Passed: `tests/test_gui.py::test_scene_shortcut_runs_the_same_bookkeeping_as_the_scene_picker`.
- [ ] Program/Preview monitors, live preset thumbnails, realtime toolbar, properties panel that follows selection.
- [x] Rename/reorder/duplicate scenes and layers; lock layers. Passed: `tests/test_phase2_foundations.py`.
- [x] Open, Save As, import/export projects and presets. Passed: `tests/test_phase2_foundations.py`. [ ] Layout preset gallery.
- [x] Camera control panel (V4L2 controls), custom local and global hotkeys, full playlist (per-item time, shuffle, pause, step). Passed: `tests/test_phase2_foundations.py`.
- [x] Screen region capture (keep a region of a share). Passed: `tests/test_phase2_foundations.py::test_a_region_of_a_source_can_be_kept`.
- [x] Translation scaffold (German + English) and accessible names. Passed: `tests/test_phase2_foundations.py`. [ ] A full screen-reader walkthrough.
- [ ] Acceptance: every control has an offscreen test; every setting survives restart; no UI stall over 50 ms during a live operation.

## 4. M4 — audio

- [x] Mixer with meters per input and for the output; desktop audio via PipeWire monitor sources. Passed: `tests/test_phase4_audio.py` (levels from real audio). Meters need a running canvas.
- [x] Audio from video layers using `Layer.volume` / `Layer.muted`; the sound loops with its picture. Passed: `tests/test_phase4_audio.py`.
- [x] Working media loop and audio playlist (shuffle). Passed: `tests/test_phase4_audio.py`.
- [~] Virtual microphone (PipeWire `Audio/Source` node) and monitor output are built when the sound server is there; graph and properties are tested (`tests/test_phase4_audio.py`), and the monitor refuses the sink whose monitor source is the desktop capture (item 9). [ ] Another app recording from it needs a real PipeWire session.
- [x] Noise suppression and automatic gain (`webrtcdsp`; see PARITY).
- [x] Gate, compressor, EQ presets. Passed: `tests/test_phase4_audio.py` (levels through the real elements).
- [x] Per-source sync offset (0–2000 ms) for microphone, desktop and music. Passed: element test in `tests/test_phase4_audio.py`. [ ] Measuring a real A/V offset needs hardware.
- [ ] Acceptance: another app can select AnyCam Mic and receive the mix; no gaps across scene changes; live gain changes without clicks.

## 5. M5 — AI and effects

- [x] AI effects run in-process by default; effect packs always run in a worker process, and **Settings → AI effects** can isolate every AI effect. Measured 21–27 ms/frame in the worker against 0–5 ms in-process at 1080p (item 7). A crashed or hung worker is replaced, and the last processed frame is held so an effect never flashes off (item 8). Passed: `tests/test_phase6_worker.py`. [ ] Face detection for the preview thumbnails is still in-process (B43).
- [x] Person segmentation on ONNX Runtime (MediaPipe selfie segmentation, bundled): blur, remove, replace with image/video/any source. Passed: `tests/test_processors.py`.
- [x] Face landmarks: 2D props that follow the face (sunglasses, hat, crown, mustache, cat, halo, frame, pixelate). Passed 2026-11-06: `tests/test_processors.py`. [x] Full-face 2D masks (cyber, web hero, robot, skull, zombie, tiger and 16 more) warped from five landmarks; passed 2026-11-06: `tests/test_themed_content.py`. [ ] True 3D (depth) masks.
- [x] Stickers on the face, animated GIFs, filters. Passed 2026-11-06: `tests/test_processors.py`, `tests/test_overlays_and_stacking.py`. [x] Emoji pack, emoji eyes, orbiting emoji, emoji rain, 32 themed frames and 42 particle animations; passed 2026-11-06: `tests/test_themed_content.py`. [ ] Drag and drop, `.cube` LUT import.
- [x] Effect browser with search, categories and thumbnails (`tests/test_effects_browser.py`); open effect pack format (`docs/EFFECT_PACKS.md`) and favourites (`tests/test_phase6_worker.py`).
- [x] Auto-framing (existing native filter) — no new work in this round.
- [ ] Acceptance: 1080p30 with AI on at ≤1.5 CPU cores without a GPU; a slow model never slows the canvas; crashed worker recovers; every model licence recorded in `NOTICE`.

## 6. M6 — content tools

- [ ] Web source with custom CSS through QtWebEngine (offscreen).
- [x] PDF slides through poppler's `pdftoppm` (one playlist slide per page). Passed: `tests/test_phase7_content.py`, including that a timed-out render leaves no cached pages (item 11). [ ] PowerPoint through LibreOffice.
- [x] Video-site links through optional `yt-dlp` (only http(s) streams are accepted). Passed with a stand-in `yt-dlp`: `tests/test_phase7_content.py`. [ ] Not run against a real site.
- [ ] Whiteboard; live draw, text, shapes and region blur.
- [x] Motion detection: switch scene and desktop notification (`notify-send`). Passed: `tests/test_phase7_content.py`; it also receives frames under the GPU preview again (item 4). [ ] Email.
- [x] Floating always-on-top panel with a round preview; Wayland limits are stated in the settings text. Passed: `tests/test_phase7_content.py`; fed by the CPU preview tap (item 4).
- [x] MJPEG IP camera test against a local multipart server (`tests/test_phase7_content.py`); network sources of that kind now use `souphttpsrc ! multipartdemux`. [ ] Game capture through window capture.
- [ ] Acceptance: each source type runs inside the M1 failure model; a dead source never affects the canvas.

## 7. M7 — pro outputs

- [x] Recording settings: bitrate, folder, disk-space guard that stops and saves the file. Passed: `tests/test_phase5_outputs.py`. [ ] Naming, pause, gallery.
- [x] Multiple destinations, each with status and reconnect/backoff (1, 2, 4 … 30 s). Passed against local `ffmpeg -listen` ingests: `tests/test_phase5_outputs.py` (two live at once, one dropped and back, the primary dropped and back). A reconnect forces a keyframe from the retained encoder, and a torn-down leg cannot report live (items 5 and 6).
- [x] YouTube/Twitch/Facebook/Kick presets and address checks for `rtmp://`, `rtmps://`, `srt://`. [ ] A TLS handshake with a real `rtmps://` server.
- [x] SRT output (to an `ffmpeg` SRT listener: H.264 + AAC in MPEG-TS) and a chosen stream bitrate. [ ] Auto bitrate, NDI.
- [ ] Several virtual cameras (`devices=N`), each with its own resolution/FPS.
- [ ] Acceptance: a receiver dropped for 30 s reconnects without touching the recording; three destinations run at once; each stream's GOP matches its FPS.

## 8. M8 — phone camera and remote

Design and build order: `docs/PHONE_CAMERA.md`.

- [x] Local HTTPS + WebRTC page with QR pairing; several phones as sources (camera half; see docs/PHONE_CAMERA.md §0).
- [x] Phone remote page (`/remote`): scenes, mute, record. Passed: `tests/test_phase8_remote.py`.
- [x] Authenticated per paired device, off until switched on, only known actions reach the app. Passed: `tests/test_phase8_remote.py`.
- [ ] Acceptance: two Android phones work as source and remote; an unpaired device receives nothing. Needs the user's phones.

## 9. M9 — ship 1.0

- [~] Recipes in `packaging/`: the `.deb` builds and is inspected (`tests/test_phase9_packaging.py`); the PKGBUILD built with `makepkg` on 2026-11-14 and has a CI job (not yet seen green); the Flatpak manifest and AppImage script moved to `packaging/unsupported/`, parsed and checked, never built. The PKGBUILD tolerates a tarball without `packaging/metainfo/` and declares `checkdepends` (item 10).
- [~] First-run checklist (`anycam check`, shown once in the window) says what is missing and the exact fix. [ ] Running the `pkexec` setup from the wizard.
- [~] Browser harness `scripts/compat-matrix.py` (Chromium family over DevTools, Firefox via `verify-firefox.py`); self-tested with Chromium's test camera. [ ] The real matrix needs the loopback device and the apps.
- [x] Declare PySide6 (`gui`) and PyGObject (`gi`) as extras in `pyproject.toml`; there is no `ui/` package data to fix (B53).
- [x] Doctor checks every required plugin and `setup.sh` fails when an element is missing (B52). Passed: `tests/test_housekeeping_2026_11_12.py`.
- [x] `ruff` + coverage in CI (2026-11-12); single-instance guard (B47, `tests/test_gui_state_fixes.py`).
- [x] `NOTICE`, `CHANGELOG`, `CONTRIBUTING`, `SECURITY` (2026-11-12). [ ] User docs; screenshots from synthetic sources only.
- [x] 1.4.0 is tagged (`v1.4.0`); 1.5.0 (keyring, crash safety, encoder probe, `anycam ctl`) is tagged 2026-11-14. The hardware gates in section 11 are still open.
- [ ] Publish the repo only when the user says so.

## 10. Verification and documentation debt

- [ ] CI jobs `test-keyring` and `pkgbuild` have not run on GitHub yet; remove their `continue-on-error` once green.
- [ ] Split the descriptively renamed test files (`tests/test_screen_*`, `test_input_validation_and_resilience.py`, ...) into one file per feature; their fixtures differ, so it needs care.
- [ ] VA-API encoders and a real gnome-keyring are untested on hardware here (the encoder probe and keyring are tested with simulated children and an in-memory store; KWallet and NVENC were run once).
- [ ] "What are you making?" setup (`plans/archive/product-hardening.md`, step 6.2).

> **Feature freeze (2026-11-14):** no new user-facing feature until Phases 1-3 of
> `plans/archive/product-hardening.md` (keyring, crash safety, encoder probe) are merged. Fixes and the plan's own
> steps only.

- [ ] Every **R** bug from the audit keeps its own regression test; every **I** bug is reproduced first or closed as not-a-bug in `docs/REGRESSIONS.md`.
- [ ] Automated runs use synthetic sources, Qt offscreen and temp `XDG_*`; they never open the user's camera or saved project.
- [ ] Soak tracks RSS, tmpfs, disk and FDs; frame stalls fail the run.
- [x] `PLAN.md` moved to `plans/archive/` and `docs/UPGRADE_PLAN.md` to `plans/pending/` (2026-11-14), so the root and `docs/` hold only current documents.
- [x] `PARITY.md` carries a measured-results section for this round (2026-11-12) and states its gaps; older rows keep their original evidence notes.
- [x] `scripts/capture-screencast.py` reports `ok: false` for a flat or wrong-shaped frame (B55). Passed: `tests/test_housekeeping_2026_11_12.py`.
- [ ] Evidence gaps: live portal screen capture, physical unplug, 10-minute soak with the virtual camera; the 2026-11-12 GPU-preview smoke (section 11).
- [ ] `AnyCamEngine.stop()` (up to ~4 s) and `Recorder.stop()` (up to 5 s) block the UI thread while finalising; move them off-thread with the M1 controller split (2026-11-11 audit).

## 11. Needs the user / hardware

- [ ] Physical camera unplug and replug on the real StreamCam.
- [ ] Load v4l2loopback (`sudo scripts/enable-v4l2loopback.sh`) and re-run the soak with `--vcam`; without `/dev/video10` the run is blocked on that one gate.
- [ ] Live xdg-desktop-portal screen-layer frame-size check (B1).
- [ ] 2026-11-12 smoke on the GPU preview: animation, restart and a vcam picture after the deadlock/shutdown fixes (items 2–9).
- [ ] Compatibility matrix on Firefox, Chromium, Zoom, Teams, Meet and OBS.
- [ ] Two Android phones as source and remote.
- [ ] Decision to publish the repository.

## 12. Remaining audit bug index

| ID | Open issue | Target |
|---|---|---|
| B1 | Live portal screen frame size still needs one real check | M0 residual |
| B12 | Live record/vcam toggles done; structural changes still rebuild | M1 |
| B43 | Face detection runs on the UI thread | M5 |
| B46 | Reconnect only handles a missing node | M1 |

## Closed recently (for reference)

- 2026-11-12 audit: items 1–11 (see the section above for each test).
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
- B41: source-side health counters and `source_health`/`stalled_sources` detect a dead capture (`tests/test_source_pool.py::test_health_flags_a_capture_that_stops`).
- B17/B18/B20: vcam failures are reported, the explicit device is checked, and the legacy appsrc is bounded (`tests/test_vcam_output.py`).
- B19: superseded; `exclusive_caps=1` is the documented setting (Discord and Chromium need it); the compatibility matrix remains in M9.
- B21: record and RTMP have separate audio queues (`tests/test_m1_outputs.py::test_recording_and_streaming_have_separate_audio_queues`).
- B12 (partial): Record start/stop and vcam toggles attach/detach valves without rebuilding (`tests/test_gui.py::test_record_toggle_attaches_without_rebuilding`, `test_vcam_output_survives_engine_restarts`); structural changes still rebuild.
- `rtmp2sink` is preferred when installed, with `rtmpsink` fallback (`tests/test_m1_outputs.py::test_stream_prefers_rtmp2sink`).
- M1 slice 2: ref-counted source pool, dead-capture isolation, output-pad EOS, duplicate-PTS handling (`tests/test_source_pool.py`).
- Persistent recorder: one MP4 across canvas rebuilds, with PTS remap and AAC audio (`tests/test_recorder.py`).
- Session-owned virtual camera: one `v4l2sink` across canvas rebuilds (`tests/test_vcam_output.py`).
- Five-scene-switch recording gate: one MP4 across five canvas rebuilds (`tests/test_gui.py::test_five_scene_switches_keep_one_recording`).
- A/V drift harness: 20 s synthetic run with 0.0 s drift and a finalised MP4 (`scripts/verify-av-drift.py`).
- Repo hygiene B54: git history, evidence moved out, launcher/desktop fixed, 123-test suite.
