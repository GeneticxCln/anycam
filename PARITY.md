# AnyCam parity matrix

Status legend:

* **verified** — implemented and backed by an automated test or a recorded
  live run. The 2026-10-30 raw artifacts (989 MB of private camera, microphone
  and screen captures) were moved out of the checkout to
  `~/anycam-private-evidence/` during M0, so artifact paths below name that
  archive rather than repository files.
* **partial** — implemented in code or proven in a spike, but not yet exercised
  end-to-end in the app, or missing a required sub-item.
* **not implemented** — no working implementation yet.
* **blocked** — implementation exists but a platform/permission limit prevents
  verification.

Last updated: 2026-10-31 (audit M0, M1 slice 1 output pipelines, and M1 slice 2
capture source pool). Evidence paths name the private archive
`~/anycam-private-evidence/evidence/` after the M0 move.

## 1. Video sources

| Requirement | Status | Implementation | Evidence / gap |
|---|---|---|---|
| V4L2 camera (Logitech StreamCam) | **verified** | `sources.camera_chain`; `choose_from_modes` prefers exact size/fps and MJPG, so it selects `MJPG 1920x1080@60`; `jpegdec` default (`nvjpegdec` rejects this stream) | `evidence/01_camera/composite.mp4`, `report.json` (`camera_mode: MJPG 1920x1080@60`) |
| Multiple camera layers / per-layer device | **verified** (hardware-gated) | Each `camera` layer stores its own `Layer.source`; the Layers tab has a V4L2 device combo plus Refresh, and Add Camera prompts when several capture nodes exist. Loopback/continuous-format devices negotiate a raw GStreamer format instead of defaulting to MJPG. | `tests/test_gui.py::test_camera_picker_keeps_a_device_per_layer`; two real V4L2 branches (StreamCam + synthetic v4l2loopback) in `tests/test_multicam.py` and `evidence/10_multicam/` |
| Per-layer camera capture mode | **verified** (offscreen) | `Layer.camera_width/height/fps` override the canvas mode per camera layer; `sources.camera_capture_request` feeds them into `choose_from_modes`, and the Layers tab exposes **Capture size**/**Capture FPS** with an `Auto` sentinel (`0`). | `tests/test_model.py::test_layer_camera_capture_overrides_roundtrip`; `tests/test_gui.py` camera-picker tests set the spin boxes; probe/selection rules in `tests/test_modes.py` |
| Two-camera scene preset | **verified** (offscreen) | **Add two-camera scene** creates `model.two_camera_scene` with two camera layers at `(0,0,0.5,1)` and `(0.5,0,0.5,1)`, using the first two detected devices. M0 fixed the geometry: each source is fitted into its box and letterboxed instead of being squeezed. | `tests/test_model.py::test_two_camera_scene_is_side_by_side`; `tests/test_gui.py::test_two_camera_scene_preset_positions_both_frames`; pixel check in `tests/test_audit_regressions.py::test_two_camera_boxes_show_their_own_half` and `test_camera_chain_letterboxes_a_1080p_mode` |
| Three/four-camera grid preset | **verified** (offscreen) | **Add camera grid...** opens `CameraGridDialog` with four 2×2 cells; each cell is assigned a detected camera or left empty, and requires at least three distinct devices. `model.camera_grid_scene` places the non-empty slots at equal quarter cells (three cameras can occupy any three cells). | `tests/test_model.py::test_camera_grid_scene_fills_four_slots` and `test_camera_grid_scene_leaves_configured_slots_empty`; `tests/test_gui.py::test_camera_grid_dialog_prefills_detected_cameras`, `test_camera_grid_scene_button_builds_the_selected_slots`, `test_camera_grid_needs_three_devices` |
| Camera hot-plug reconnect | **verified** (offscreen); physical pull still blocked | A missing camera node keeps its box in the graph as an opaque generated placeholder card (`AnyCamEngine(skip_layers=…)` + `overlay.render_camera_placeholder`), so the rest of the scene stays live and the slot keeps its position. The same placeholder appears when preview starts with a camera absent. A 1.2 s watcher rebuilds with the real camera when the node returns. A deliberately stopped preview is not restarted. | `tests/test_overlay.py::test_camera_placeholder_is_an_opaque_labeled_card`; `tests/test_pipeline.py::test_skipped_camera_builds_a_placeholder_pad` and `test_all_skipped_cameras_still_build_placeholders`; `tests/test_gui.py::test_camera_loss_drops_layer_but_keeps_preview_alive`, `test_all_lost_cameras_still_show_placeholder_frames` and `test_camera_reconnects_when_its_device_returns` |
| Screen/window capture via xdg-desktop-portal + PipeWire | **partial** | `portal.request_screen_cast` drives CreateSession → SelectSources → Start → OpenPipeWireRemote; `sources.screen_chain` uses the node id and fd, and the Layers tab has a Screen button | The portal flow is implemented and unit-tested, but no live screen-capture artifact is checked in; a saved screen layer re-requests access on the next engine start |
| Image source | **verified** | `sources.image_chain` (PNG/JPEG/WebP, `imagefreeze`); Layers tab Image button | Full-frame image source tested (`--source plate.png`); effects + crop wired; GUI image-layer creation covered by `tests/test_gui.py` |
| Video-file source | **verified** | `sources.file_video_chain` (`filesrc ! decodebin`); Layers tab Video button | Full-frame video playback tested (`--source colorful.mp4`), 60 fps; GUI video-layer creation covered by `tests/test_gui.py` |
| RTSP/HTTP source | **verified** | `sources.network_chain` (`uridecodebin`); Layers tab Network URL button | Local integration test runs an ffmpeg RTP producer through a minimal RTSP server into `AnyCamEngine` and asserts decoded preview frames (`tests/test_rtsp.py`, `evidence/08_rtsp/`); HTTP was exercised earlier. No explicit backpressure handling |
| Capture pipeline isolation / sharing (M1 slice 2) | **verified** | `pool.SourcePool` owns one `intervideosink` pipeline per distinct `CaptureSpec`; layers on the same device share it by refcount and the last release sets it to NULL. The canvas reads it through `intervideosrc` (`sources.layer_tail`), so a dead capture cannot stop the compositor, and one EOS through the compositor's src pad finalises the outputs. | `tests/test_source_pool.py` (shared refcount, real pixels through the pool, dead capture, release on stop); `tests/test_m1_outputs.py` |

## 2. Scene compositor

| Requirement | Status | Implementation | Evidence / gap |
|---|---|---|---|
| Layered position/scale | **verified** | GStreamer `compositor`, normalised `Layer.box`, per-pad xpos/ypos/width/height/zorder | Three independently positioned overlay layers in `evidence/00_skeleton/composite.png` |
| Crop | **verified** (M0) | `geometry.crop_pixels` + `videocrop`; the crop is a fraction of the **source** (camera mode size, image pixels, probed video size, portal stream size) rather than of the layer box. Crop mode builds the selected layer with its crop suppressed (`preview_crop_layer`), so the editor draws its window over the source picture it edits and drags track the pointer | `tests/test_audit_regressions.py::test_crop_is_a_fraction_of_the_source_image`, `::test_preview_crop_layer_shows_the_uncropped_picture` and `::test_crop_editor_window_follows_the_picture` (real pixels). Network streams use the canvas size as a proxy until the stream is probed |
| Picture-in-picture | **verified** | Layer boxes + playlist PiP (0.34×0.30, bottom-right) | `evidence/03_playlist/rotate_01.png` |
| Interactive layer transform | **verified** (offscreen) | Preview drag maps to normalised `Layer.box`; corner/edge handles resize; Crop mode pans and trims `Layer.crop`; arrow-key nudging; numeric X/Y/W/H and L/T/R/B fields; Ctrl/Shift-click multi-select and group X/Y moves; configurable alignment snapping to canvas/layer edges with spacing labels; Ctrl+Alt+arrows align selections to canvas edges/centres; move/resize updates the live compositor pad (`pipeline.set_layer_box`) | `tests/test_gui.py` drives real mouse/key events offscreen (move, resize, crop trim/pan/reset, nudge, numeric fields, multi-select group move, snap toggle/tolerance, snap/spacing guides, alignment shortcuts); `tests/test_pipeline.py` checks pad xpos/ypos/width/height |
| Per-layer opacity and stacking | **verified** (offscreen) | Opacity slider writes `Layer.alpha` through `pipeline.set_layer_alpha` (all selected layers); Raise/Lower/Top/Bottom normalise `Layer.z` and call `pipeline.refresh_layer_order`; neither rebuilds the graph | `tests/test_gui.py` slider and order buttons; `tests/test_pipeline.py` checks live pad alpha and zorder |
| Undo/redo layer edits | **verified** (offscreen) | Full layer snapshots for the active scene; drag, nudge, numeric fields, opacity, order, reset-crop, add, remove and duplicate record one step; Ctrl+Z / Ctrl+Shift+Z, Ctrl+D and the Undo/Redo buttons restore through a graph rebuild when the engine is running; scene switches and playlist structural changes clear history | `tests/test_gui.py` covers drag, nudge, opacity, order, multi-select, add/remove/duplicate and redo; `LayoutHistory` unit coverage is exercised through window actions |
| Text overlays | **verified** | `overlay.render_text_png` → `imagefreeze` layer | `evidence/00_skeleton/composite.png` |
| Image/logo overlays | **verified** | `overlay.fit_rgba`, `logo`/`image` layer kind | Playlist PiP composites image overlays end-to-end (`evidence/03_playlist/rotate_*.png`) |
| Presets | **partial** | `save_scene_preset` / `load_scene_preset`, JSON project | Model round-trip test; no GUI preset browser |
| Live scene switching | **partial** | GUI scene list + engine restart; `Ctrl+1…9` shortcuts | Switching implemented and exercised via shortcuts; no multi-scene capture artifact |

## 3. Effects

| Requirement | Status | Implementation | Evidence / gap |
|---|---|---|---|
| Brightness/contrast/saturation | **verified** | `videobalance` in `sources._effects_chain` | `evidence/06_effects/brightness.png`, `contrast.png`, `grayscale.png` |
| Blur | **verified** | `gaussianblur` sigma (RGBA-safe via trailing `videoconvert`) | `evidence/06_effects/blur.png` (mean gradient 2.66 → 1.05) |
| Chroma key | **verified** (M0) | `alpha` element (`custom` method). M0 fixed the mapping: `noise-level` is 0..64, not a byte, and `chroma_smoothness` now drives black/white sensitivity; `#rgb` and malformed colours no longer break the graph | `evidence/06_effects/chroma_before.png` → `chroma_after.png` (green → black); `tests/test_audit_regressions.py::test_chroma_tolerance_is_not_scaled_to_bytes` and `test_bad_chroma_colour_cannot_break_the_graph` |
| Background blur/replacement | **not implemented** | — | Requires segmentation/matte; only the model fields exist |
| Face-tracked effect | **verified** (offscreen) | `face.FaceDetector` (YuNet ONNX via onnxruntime, OpenCV-compatible raw-BGR/log-size decode), bundled model, GUI `_track_face` moves a `face` layer through `pipeline.set_layer_box` at 5 Hz | `tests/test_face.py` detects a real public-domain portrait; `tests/test_gui.py` covers overlay add/move/hide; `evidence/09_face/` is a live 640×480 offscreen run with a 0.92-score detection, visible pad alpha 1.0 and 31 fps |

## 4. Audio

| Requirement | Status | Implementation | Evidence / gap |
|---|---|---|---|
| Microphone selection | **verified** | `config.list_audio_sources` / `microphone`, `pulsesrc` | `evidence/04_audio/av.mp4` (StreamCam mic selected) |
| Gain | **verified** | `volume` element, dB→linear in `_build_audio` | −12 dB → −53.1 dB mean, 0 dB → −43.3 dB, +12 dB → −30.2 dB |
| Mute | **verified** | `volume mute=true` | muted: −91.0 dB (digital silence) |
| Mic/media mixing | **verified** | `audiomixer` with mic + media file + silent anchor, 48 kHz stereo | `evidence/04_audio/av.mp4`, A/V delta 0.078 s, `verify.json` all true |

## 5. Synchronised MP4 recording

| Requirement | Status | Implementation | Evidence / gap |
|---|---|---|---|
| H.264 MP4, correct resolution/frame rate | **verified** | `x264enc → h264parse → mp4mux`, explicit EOS finalisation, I420 caps | `evidence/01_camera/composite.mp4`: 1920×1080, 60/1, 611 frames, 10.18 s |
| Audio + video synchronised | **verified** | AAC branch built when `--audio` is used; SystemClock; 5 s record buffer | `evidence/04_audio/av.mp4`, `verify.json` (av_sync true, 0.078 s) |
| 10-min 1080p60 soak | **partial** (2026-10-31 re-baseline) | `soak.py` + `scripts/soak.sh`. 36002 preview frames at 59.998 fps (0.003% dropped) and a 600.23 s fragmented MP4 with 36014 packets (60.0 fps, 859 MB). RSS grew 202.6 → 226.3 MiB in buffer-pool steps, so the old flat-memory claim no longer holds. The soak gate now judges the settled half and the tail (`soak._memory_assessment`); a fresh 10-minute run is still required before calling it pass. The virtual camera was not exercised in this run (v4l2loopback not loaded). | `~/anycam-private-evidence/evidence/soak_10m/report.json`, `soak.mp4` |

## 6. RTMP / RTMPS

| Requirement | Status | Implementation | Evidence / gap |
|---|---|---|---|
| Publish to user-supplied RTMP/RTMPS URL | **verified** | `flvmux` + `rtmpsink`, video + AAC audio, I420 H.264 High. Since M1 slice 1 the publish branch lives in its own `anycam-rtmp` pipeline fed by `appsrc` samples, so a stalled server cannot reach the canvas tee; it is reported as `rtmp_error` | `evidence/05_rtmp/received.flv`: 1280×720, H.264 High yuv420p + AAC, 8.23 s. `tests/test_m1_outputs.py::test_unresponsive_rtmp_cannot_stall_the_canvas` keeps preview and recording alive with a black-hole server |

## 7. Playlists and shortcuts

| Requirement | Status | Implementation | Evidence / gap |
|---|---|---|---|
| Image/video playlist or slideshow | **verified** | `PlaylistItem` model + GUI Playlist tab + live rotation via pad alpha | `evidence/03_playlist/rotate_01..05.png` |
| Keyboard shortcuts | **verified** | `Ctrl+P/R/Shift+V/M/1…9`, `F5` via `QShortcut` | Listed in GUI Output tab; not captured as a screenshot |

## 8. Standalone GUI

| Requirement | Status | Implementation | Evidence / gap |
|---|---|---|---|
| Live preview | **verified** (offscreen) | QtWidgets `PreviewWidget` fed by `appsink` → `QImage`; the engine always builds the preview branch | `evidence/00_skeleton/gui.png` regenerated 2026-10-30 with `frame=yes`; 60 s offscreen run at 30.01 fps with `error: ""` (`docs/REGRESSIONS.md`) |
| Source management | **partial** | Camera/test/text/logo/image/video/screen/network add/remove/visibility; screen uses the portal chooser | Offscreen tests cover layer creation and session retention; whole app not exercised with a real window session |
| Project state persistence | **verified** (offscreen) | `EditorConfig` snap settings + `PlaylistConfig` per scene + 800 ms autosave/close save | `tests/test_gui.py` restores snap, playlist and unsaved layer state across windows; `tests/test_model.py` round-trips the new JSON fields |
| Effect/scene controls | **partial** | Effects tab, scene list, output tab, Audio tab, Playlist tab | Controls exist; before/after captures pending |
| Desktop entry | **verified** | path-independent `anycam.desktop` + `scripts/install-desktop.sh` | `desktop-file-validate` passes; the installer expands `Exec`/`TryExec` and installs the icon (verified with a temporary `XDG_DATA_HOME`) |
| Windowed launch on niri | **partial** | `scripts/anycam-gui` | Deliberately not launched on the live desktop; no-window constraint |

## Virtual camera in Firefox

| Requirement | Status | Evidence / gap |
|---|---|---|
| `/dev/video10` exposed by AnyCam and selectable in Firefox | **verified** | Re-driven 2026-10-30 with Firefox 159: `scripts/verify-firefox.py` enumerated `Logitech StreamCam; AnyCam`, opened **AnyCam** at 1280×720@30 through `getUserMedia`, and captured the composited frame (mean 119.2, std 64.7). `evidence/02_virtual_camera/{firefox.png,firefox-frame.png,firefox-report.json,firefox.txt}`. |

Required user action:

```bash
sudo modprobe v4l2loopback video_nr=10 card_label=AnyCam
```

## Long-run stability

| Requirement | Status | Evidence / gap |
|---|---|---|
| A/V sync across the split source pipelines | **partial** (20 s synthetic) | `scripts/verify-av-drift.py` drives synthetic video + audio producers through `intervideosink`/`intervideosrc` into a compositor and measures the canvas offsets and a recorded file. A 20 s run reports `drift_s: 0.0` with no duplicate or gap timestamps and a finalised 20.03 s MP4; the 10-minute gate is pending. | `scripts/verify-av-drift.py --seconds 20 --warmup 5` |
| 10 min 1080p60, preview + recording, <1% drops, no monotonic memory growth | **partial** (re-baselined 2026-10-31) | `~/anycam-private-evidence/evidence/soak_10m/report.json`: 600.23 s, 36002 preview frames at 59.998 fps (0.003% dropped), 36014 recorded packets at 60.0 fps (0.0% dropped), `MJPG 1920x1080@60`. **Memory**: the old least-squares gate scored 2.38 MiB/min through buffer-pool step-ups; it now judges the settled window and tail (`soak._memory_assessment`) and a real per-frame leak still fails. A fresh 10-minute run is pending. The old 0.032 MiB/min figure came from the faststart recorder and no longer applies. The virtual camera was not exercised in this run (v4l2loopback not loaded). |

## Cross-cutting requirements

| Requirement | Status | Evidence |
|---|---|---|
| Clean-room, original branding/assets, no ManyCam runtime | **verified** | Original `assets/anycam-logo.png`, no Wine/Proton dependency, no ManyCam files referenced at runtime |
| Local recordings, no telemetry/cloud | **verified** | No network code except user-supplied source/RTMP fields |
| Wayland portal capture, no niri-only API | **verified** (spike) | `xdg-desktop-portal` ScreenCast + PipeWire |
| Reversible system changes | **verified** | Only `modprobe v4l2loopback`; `scripts/disable-v4l2loopback.sh`; documented |
| Reproducible build/run docs | **verified** | `README.md`, `scripts/setup.sh` |
| Runnable automated tests | **verified** | `./scripts/test.sh` (127 passed, 1 hardware-gated multicam skip; 128 collected); `.github/workflows/ci.yml` runs the suite on Ubuntu 24.04 |
