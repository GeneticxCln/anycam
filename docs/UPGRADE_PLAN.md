# Upgrade plan: native Linux integration

A plan for moving AnyCam further onto native Linux mechanisms: a PipeWire camera, zero-copy
GPU paths, D-Bus instead of subprocesses, hardware encoders and desktop portals. Each item
says what AnyCam does today (checked against the code and the reference machine on
2026-11-07), what the proposal gets wrong if anything, the plan, and how we know it is done.

Reference machine: CachyOS, KDE/Wayland (kinetic-we), NVIDIA RTX 4060 Ti, GStreamer 1.28,
PipeWire 1.6.10, Vesktop (Electron 44).

## Progress

| Item | Status |
|---|---|
| 3.1 In-process TLS (`cryptography`, `openssl` fallback) | **Done.** Tested with and without the package; CI runs both |
| 3.2 firewalld over D-Bus | **Done.** Tested against a simulated system bus; not yet run on a real firewalld |
| 4 VA-API encoders (`va` plugin, real-encode probe) | **Done.** Plan logic tested with simulated elements; not yet run on Intel/AMD |
| 5b Tray icon and portal global shortcuts | **Done.** Portal flow tested with a simulated bus; on KDE all 14 shortcuts bound after the user accepted the dialog |
| 5a Screencast restore token on KDE | **Waiting for a check on screen**: does a second launch skip the chooser? |
| 3.3 Next free port when 8443 is taken | Open |
| 1.1 PipeWire camera spike | **Done**, findings below |
| 1.2–1.3, 1.5 PipeWire camera output and setting | **Done.** "Publish as: loopback / PipeWire / both"; loopback stays the default |
| 1.4 Live rename of the node | Open (the name is fixed to "AnyCam") |
| 2a.1 Packaging check for `qml6glsink` | **Done.** Usable only with the distribution's PySide6 (same Qt as `gst-plugin-qml6`); pip's bundles its own Qt and the plugin refuses to load. The reference machine now uses `pyside6` 6.12.0 from pacman |
| 2a.2 GPU preview (`qml6glsink` in a `QQuickWidget`) | **Done, optional.** Used when the plugin loads, CPU preview otherwise. Window thread median 40–47% → 18–20%; 15% target met in some samples, with periodic spikes still unexplained |
| 2a.3 Cheaper CPU preview (the fallback) | **Done.** Frames capped at 960×540 and following the widget, bilinear paint, zero-copy QImage: GUI-thread Python work down ~25–30% |
| Hardware MJPEG decode (`nvjpegdec`) | **Not possible with the StreamCam.** NVDEC decodes only 4:2:0 JPEG; the camera sends 4:2:2 MJPEG and has no H.264. `jpegdec` stays: 34% of a core at 1080p60 including RGBA conversion, the cheapest option measured |
| D.1 Where the CPU goes now | **Done**, findings under 2b |
| D.2 GL canvas prototype (`glvideomixer`) | **Done: passes the gate on a synthetic scene.** Mixing ~120–140% → ~35–40% of a core; pixels match |
| D.2 GL canvas in AnyCam | **Done, automatic with CPU fallback.** Settings → Canvas; `ANYCAM_CANVAS` override; a failed GPU start retries on the CPU |
| D small fixes | **Done.** Native frame counter (no Python on every canvas frame), the first-buffer probe removes itself, empty Drawing layers are left out (engine on the saved scene 240–250% → 217%) |
| Effects CPU (found while testing D) | **Fixed.** ONNX Runtime sessions used one thread per CPU with idle threads busy-spinning: a virtual background cost ~460% of a core, a mask ~530%. Two threads with spinning off: ~166% and ~234%, models still 1.4 ms and 4.9 ms per run |
| Looks swapping (found while testing D) | **Fixed.** A film style stayed on through every later look, "Original" included; Looks tiles now replace each other |
| Looks froze the camera (found while testing D) | **Fixed, two causes.** A colour look adds ~14 ms per 1080p frame in `videobalance`. (1) The frame overlay (`gdkpixbufoverlay`, QoS on) then dropped every camera frame; QoS is now off for every filter AnyCam creates (sinks untouched). (2) On the GPU canvas, `glvideomixer` dropped the late camera frames and repeated its last one: frames flowed at 60/s but the picture stood still, in the preview and the virtual camera. The GPU mixer now has 33 ms of extra latency (10 ms still froze, 20 ms did not). Found with a tracer in the user's own session, then reproduced with the real startup on the saved scene |
| Effect tiles froze the window (found while testing D) | **Fixed.** Tiles were rendered two per tick on the GUI thread (~220 ms per mask), blocking the window and, through the GIL, the effect threads. Now one background thread, and tiles of the built-in sample are cached on disk |
| D.3 Colour step and mirror as GPU shaders | **Done.** On the GPU canvas a `glshader` that reproduces videobalance's maths (within ~1/255; `glcolorbalance` was ~10/255 off on Noir) and `glvideoflip` run after the upload whenever nothing order-sensitive follows (no filter, frame, film style or chroma key); otherwise the CPU elements do it as before. The camera chain keeps 57–60 fps with a look on (34 fps with the CPU step) |
| GPU preview from the GL canvas | **Done.** The preview takes the mixed texture from a GL tee before the one download (no download and re-upload); GStreamer shares Qt's context automatically. Process CPU 187%/158% → 155%/108% of a core |
| 33 ms GPU mixer latency | **Kept.** A look combined with a filter, frame, film style or chroma key still runs on the CPU, and that combination froze without it |
| Mask warm-up / lag | **Fixed.** On live sources the processor slot takes the newest frame instead of pushing back: back-pressure blocked the camera thread, videorate duplicated (394 in 7 s) and a mask showed 7–19 pictures a second. Now 31 in the first second, 52–60 after |
| Distort froze the camera (found while testing D) | **Fixed, two causes.** (1) A live layer's `videorate` padded slow frames with duplicates; the source thread then distorted copies, the source queue dropped the real frames, and Bulge showed 4 new frames in 2 s (51 out). Live sources now get `drop-only` videorate: 62 of 62 frames pass. (2) On the GPU canvas, frames late after slow effects were dropped by `glvideomixer` (canvas frozen for ~3 s); live layers are now re-timestamped as they leave the CPU chain, so they are never late. Distort now shows 49–52 different pictures a second on the GPU canvas (was 0–17) |
| D.4 CUDA interop | **Not built: the measurement says no.** onnxruntime here has no CUDA provider, and the copy interop would avoid is 432 KiB per refresh (~0.05 ms) against 1.5 ms of inference; frames are already shrunk with a cheap box filter first |

## Summary

| # | Upgrade | Today | Value | Effort | Risk | Order |
|---|---|---|---|---|---|---|
| 1 | PipeWire camera node | v4l2loopback only | High: no root, Flatpak/portal friendly | Medium | Client support varies | Phase B |
| 2a | Zero-copy preview (GStreamer → Qt) | appsink → QImage → QPainter | High: the UI thread used ~83% CPU at 1080p60 | Medium–large | Rewrite of the preview widget | Phase C |
| 2b | GPU canvas and GPU model input | CPU `compositor`, CPU ONNX | Medium | Large | Effects are CPU-side today | Phase D (research) |
| 3 | Firewall over D-Bus, in-process TLS | `pkexec ufw`, `openssl` CLI | Medium | Small | ufw has no D-Bus API | Phase A |
| 4 | VA-API encoders | NVENC (auto) → x264 | Medium for Intel/AMD users | Small | Not testable on this NVIDIA box | Phase A |
| 5a | Screencast restore token | **Already done** | — | Verify only | — | Phase A |
| 5b | Tray icon and global shortcuts | In-app shortcuts only | Medium | Small | Portal support differs per compositor | Phase A |

---

## 1. Virtual camera as a PipeWire node

**Today.** AnyCam writes to a v4l2loopback device (`/dev/video10`). The first launch runs a
one-time setup through `pkexec`: modprobe config with `exclusive_caps=1`, a modules-load
entry, and a udev rule that lets the `video` group re-announce the device. Chromium apps only
list it while AnyCam writes to it.

**Proposal check.** Correct in principle. `pipewiresink` is installed here and can create a
node with `media.class = Video/Source`, so this needs no root, no `/etc` files and no kernel
module, and the name can change at runtime. Two caveats the proposal leaves out:

* **Clients must look in PipeWire.** Browsers and Flatpaks reach PipeWire cameras through the
  camera portal (`org.freedesktop.portal.Camera`). Chromium only does this with its PipeWire
  camera feature enabled (`chrome://flags/#enable-webrtc-pipewire-camera`); whether it is on
  by default in Vesktop/Electron 44 must be tested, not assumed. Firefox has a PipeWire camera
  backend whose default also varies by version. Apps that only open `/dev/video*` (OBS's V4L2
  source, older native clients, Zoom) will **not** see a PipeWire-only camera.
* **The portal must accept the node.** The camera portal exposes nodes intended as cameras
  (`media.role = Camera`). Whether a user-space producer node is offered next to real
  webcams needs a spike on KDE and on niri/GNOME portals.

**Plan.**

1. Spike: a GStreamer pipeline `… ! pipewiresink stream-properties="props,media.class=Video/Source,media.role=Camera,node.description=AnyCam"`
   fed by the canvas. Check that it appears in `pw-cli ls Node`, in Firefox and in
   Chrome/Vesktop with and without the PipeWire camera flag, and in a Flatpak browser.
2. Add `engine/outputs/pipewire_cam.py`, a `PipeWireCamOutput` with the same start/stop/idle
   card contract as `VcamOutput`, so the canvas tap feeds either one or both.
3. Settings → Virtual camera: "Output: v4l2loopback / PipeWire / both". Default to both when
   v4l2loopback is set up, PipeWire only when it is not (then the root setup is never needed).
4. Live rename: `node.description` from the setting, applied without a rebuild if
   `pipewiresink` allows it, otherwise with a quick restart of that output only.
5. Keep v4l2loopback as the compatibility path: it is still the only thing `/dev/video*`
   clients can open.

**Spike results (2026-11-07, reference machine).**

* A `pipewiresink mode=provide` stream with `media.class=Video/Source`, `media.role=Camera`
  appears in `pw-dump` next to the StreamCam with the same class and role, and a separate
  process reads frames from it. A reader with no caps at all (ANY) never negotiates; every
  reader that asks for raw video does, within 0.3 s.
* Camera portal: WirePlumber's `find-portal-access.lua` grants a sandboxed app that was
  allowed the camera access to every node matching `media.role=Camera` and
  `media.class=Video/Source`. The AnyCam node is treated exactly like the hardware webcam.
  Non-sandboxed clients get all nodes.
* Headless runs with throwaway profiles. Firefox 159 by default lists only
  "Logitech StreamCam" (V4L2); with `media.webrtc.camera.allow-pipewire=true` it lists
  "AnyCam" and reads 1280×720 frames from it. Chrome 157 is the same: off by default, and
  with `--enable-features=WebRtcPipeWireCamera` it lists "AnyCam" and reads frames.
  Vesktop is Chromium-based, and its Arch launcher reads `~/.config/vesktop-flags.conf`;
  it has not been tested.
* Not tested: a Flatpak browser, GNOME/niri portals.

**What was built.** The PipeWire sink is a branch of the existing `VcamOutput` pipeline, not
a second output class. It reuses the ready card, the last-frame hold through rebuilds and the
one GPU/CPU conversion. With both outputs on, a `tee` with a small leaky queue per branch
keeps a stalled reader on one side from holding up the other. "Both" falls back to the
loopback when PipeWire refuses, and to the PipeWire node when the loopback is missing. With
PipeWire only, the root setup is never offered. Because browsers have PipeWire cameras off by
default, the default stays the loopback, not "both" as first planned.

**Done when:** Firefox and at least one Chromium app pick "AnyCam" from PipeWire with no root
setup on a fresh user account; the output survives canvas rebuilds like the loopback does;
disabling v4l2loopback leaves the PipeWire camera working.

## 2. Zero-copy GPU paths

### 2a. GStreamer → Qt preview without copies

**Today.** The preview pulls RGBA frames from an `appsink`, wraps them in a `QImage` and paints
them with `QPainter` on the UI thread. On the reference machine at 1080p60 the main thread
used about 83% of a core; that is the copy and the paint.

**Proposal check.** The direction is right, but the element named is not available here:
`qmlglsink`/`qml6glsink` are not installed (they come from the Qt6 QML plugin of
gst-plugins-good and need a Qt Quick scene, while AnyCam's preview is a `QWidget` that also
draws selection handles, slots and drag feedback).

**Plan.**

1. Packaging check: which distribution packages provide `qml6glsink` (Arch, Ubuntu, Flatpak
   runtime). If it cannot be relied on, fall back to step 3.
2. Prototype a `QQuickWidget` preview hosting `GstGLQt6VideoItem` via `qml6glsink`, with the
   handles, slot "+" buttons and drag logic drawn as a QML/QPainter overlay on top.
3. Fallback without the QML sink: a `QOpenGLWidget` that uploads each frame as a texture (one
   copy, no `QImage` conversion, GPU scaling), or a lower preview rate and size (the preview
   does not need 1080p60).
4. Measure the UI thread and total CPU before and after.

**Findings (2026-11-07, reference machine, 1080p60 canvas, StreamCam scene).**

* Step 1 fails: AnyCam's PySide6 comes from pip with its own Qt 6.11.2; `gst-plugin-qml6`
  links the system Qt 6.12 and Qt private APIs. Loading both in one process is not workable,
  and the same mismatch would hit every pip install.
* Offscreen, the GUI-thread costs per frame were: the smooth `QImage.scaled` per paint (about
  1 ms full screen), the QImage `.copy()` (0.75 ms live), and the PyGObject map, which copies
  every buffer into new `bytes` (0.6 ms in isolation, about 1.2 ms live at 960×540). The map
  copy grows with the frame size, so scaling the preview to the full widget size (tried
  first) made the live total slightly worse: the copy grew more than the paint shrank.
* Kept: preview frames at the widget's shown size but at most 960×540 (smaller panels get
  smaller frames; a resize changes the caps live, without a rebuild); painting with a
  bilinear `drawImage` instead of `QImage.scaled` (0.04 ms at panel size); a zero-copy
  QImage over the frame bytes, which the widget keeps alive.
* Live, profiled per handler by main-thread CPU time, before → after:
  `_tick`+`paintEvent` 130/111 → 96/82 ms per second, `_update_preview` 20 → 4 ms/s.
  Whole-thread totals are noisy (old 61%/34%, new 33%/31%).
* What remains is mostly Qt itself, outside AnyCam's handlers: repainting the window and
  handing it to the Wayland compositor at the preview rate. The levers left are a lower
  preview frame rate, gst-python's zero-copy `map()` (an extra system package), or a GL
  widget.

**GPU preview (2026-11-08).** After switching to the distribution's PySide6 6.12.0 (same Qt
as `gst-plugin-qml6`), `qml6glsink` loads. What it took:

* A prototype in a widget window: `qml6glsink` → `GstGLQt6VideoItem` in a `QQuickWidget`,
  with a transparent `QWidget` on top for the editing marks. GPU-native frames
  (`gltestsrc`) rendered correctly straight away; system-memory frames rendered **nothing**,
  with `GL_INVALID_OPERATION: <image> and <target> are incompatible` per frame. `glupload`
  had proposed a udmabuf pool upstream and imported those buffers as EGLImages, which
  NVIDIA rejects. `identity drop-allocation=true` before `glupload` keeps the pool off, so
  it falls back to a plain texture upload. Same source, prototype: main thread 2–3%
  against 8–9% for appsink + QImage, process 18% against 71%.
* The sink takes Qt's GL context from the item's window, which Qt Quick creates only for a
  **visible** view; a hidden view made the pipeline fail with "failed to retrieve wrapped
  context". The view stays visible, and a QML `live` property swaps the video for the
  "Preview stopped" text.
* PyGObject cannot pass a C++ object to the sink's `widget` (gpointer) property; it is set
  through `g_object_set` with ctypes.
* Verified in AnyCam on Wayland: live picture in the window, three engine rebuilds re-attach
  a new sink to the same item, and Snapshot reads the view back (the framebuffer is in
  device pixels while reporting a pixel ratio of 1.0, so the crop scales by width).
* A/B in AnyCam (same build, `ANYCAM_PREVIEW=image` against GPU, two rounds, six 5 s samples
  each, StreamCam scene at 1080p60): window thread median 40% / 47% → 18% / 20%, process
  median 400% / 448% → 351% / 380%. Single samples still spike to ~40% in GPU mode;
  the cause is not known yet.

**Done when:** at 1080p60 the UI thread stays under 15% of a core, the preview keeps every
editing interaction (select, drag, resize, crop, slot menus), and the offscreen GUI tests still
run.

### 2b. GPU canvas and GPU model input

**Today.** The canvas uses the CPU `compositor`; `glvideomixer` is installed but unused. Python
effects work on CPU frames from an `appsink`; ONNX runs on the CPU by default, with CUDA
opt-in (`inference.py`).

**Proposal check.** Two statements are inaccurate:

* ONNX Runtime has **no VA-API or Vulkan execution provider** and **no API that accepts a
  dma_buf**. Device input goes through CUDA (IOBinding with device memory). Getting a GStreamer
  GL/dma_buf frame into CUDA without a copy needs CUDA–OpenGL or CUDA–EGL interop
  (`cuGraphicsGLRegisterImage` / EGLImage), which means a small native extension or CuPy, not a
  setting.
* The canvas copies are mostly CPU-side today, because effects are CPU-side. Moving the
  compositor to the GPU only pays off once the per-layer chains stay on the GPU too.

**Plan (research, in this order).**

1. Measure where copies happen now (`GST_DEBUG=GST_PERFORMANCE:5`, per-thread CPU) with a
   camera, a stage and one effect.
2. Prototype a GL canvas: `glupload` per layer → `glvideomixer` → `gldownload` once for the
   recorder and the vcam (NVENC can take GL/CUDA memory directly). Keep the CPU compositor as
   the fallback.
3. Move the cheap colour rack (balance, flip, chroma key) to GL shaders (`glcolorbalance`,
   `gleffects`, a `glshader` for keying).
4. Only then: a CUDA–GL interop spike for feeding segmentation from GPU memory, gated on a real
   measurement showing the Host→Device copy matters. The bundled models take 256×144 input, so
   the copy is small today.

**Research results (2026-11-08, reference machine).**

* AnyCam's engine on the saved scene (StreamCam inset with a frame, plus an empty Drawing
  layer, 1080p60, image preview, no virtual camera) costs 240–250% of a core. Per thread:
  camera decode 44% (`jpegdec`, the cheapest option; see the MJPEG row), the camera layer's
  convert and scale 23%, the image preview branch 19%, and mixing about 90–120% (the
  `compositor:src` thread plus about 11 GLib pool threads at 7–12% each).
* The **empty Drawing layer costs ~35% of a core** (250% → 213% without it): a full-canvas
  transparent layer blended 60 times a second while drawing nothing.
* The compositor's `max-threads` makes no measurable difference (auto, 1, 2, 4: 240–250%).
* `GST_PERFORMANCE` was not needed. A gdb stack sample found the `compositor:src` thread
  waiting for Python's GIL in `_on_compositor_buffer`, the frame-count pad probe: every
  canvas frame enters Python. Signal-based gdb sampling missed short worker bursts, so the
  per-thread numbers come from `/proc`.
* Prototype with the same three 1080p60 RGBA inputs (full-frame background, inset picture,
  full-frame overlay): CPU `compositor` 150–170% of a core for the whole process against
  65–71% for `glupload` → `glvideomixer` → one `gldownload` (61% without the download); the
  sources alone cost 32%. That puts mixing at ~120–140% against ~35–40%. Each `glupload`
  needs the same `identity drop-allocation=true` as the GPU preview, or NVIDIA leaves the
  textures black.
* Pixels: CPU and GL output differ by 0.4/255 on average. The pixels off by more than 2
  (3.8%) are all inside the scaled inset: the test pattern's per-frame random noise patch,
  plus scaling-filter differences at edges.
* `glvideomixer` pads have every property AnyCam's layout uses (`xpos`, `ypos`, `width`,
  `height`, `alpha`, `zorder`, `sizing-policy`), plus GPU `crop-*`. AnyCam does not use the
  compositor-only `operator` or `max-last-buffer-repeat`.

**Done when:** a camera + stage + text scene at 1080p60 runs with at least 40% less total CPU
on the GL canvas, output pixels match the CPU canvas within tolerance, and every scene still
works on the CPU fallback.

## 3. Network and firewall

**Today.** The phone link serves HTTPS from the standard library, creates its certificate with
the `openssl` CLI, binds to the LAN address on TCP 8443, and opens ufw with
`pkexec ufw allow 8443/tcp` from the pairing dialog (starting a polkit agent if the session has
none).

**Proposal check.** Partly correct:

* **In-process TLS: yes.** The `cryptography` package can create the self-signed certificate
  without a subprocess. It is not installed on the reference machine, so it should be an
  optional dependency with the `openssl` CLI kept as the fallback.
* **Firewall over D-Bus: only for firewalld.** firewalld has a D-Bus API
  (`org.fedoraproject.FirewallD1`, authorised by polkit). **ufw has no D-Bus API**, and
  systemd-networkd, systemd-resolved and NetworkManager do not manage firewall rules
  (NetworkManager only assigns firewalld zones to connections). ufw keeps the `pkexec` path.
* **Socket activation / 0.0.0.0 on ephemeral ports: no.** Socket activation is for system or
  user services, not a GUI app that starts its own server. Binding to every interface on a
  random port would undo two deliberate choices: listening only on the LAN address, and a fixed
  port that one firewall rule can allow.

**Plan.**

1. `phone/tls.py`: use `cryptography` when importable (EC P-256, SAN with the LAN IP, 10-year
   validity), else the `openssl` CLI. Same files, same fingerprint display.
2. `phone/firewall.py`: add a firewalld backend: query with `getZoneOfInterface`/`queryPort`
   and add a permanent and runtime rule with `addPort` over D-Bus (polkit prompts as needed).
   Detection order: firewalld active → D-Bus; ufw enabled → existing `pkexec ufw`; neither →
   no button.
3. Fall back to the next free port only if 8443 is taken, and show that port in the QR code and
   the firewall button.

**Done when:** pairing works with no `openssl` binary when `cryptography` is installed; on a
firewalld system **Allow phones** opens the port through D-Bus with no subprocess; ufw behaves
as today.

## 4. Hardware encoding

**Today.** The proposal says recording falls back to software unless NVENC is set up
explicitly. **That is out of date:** the encoder setting defaults to **Auto**, which prefers
NVENC (H.264, with H.265 and AV1 selectable) when the nvcodec plugin works, and falls back to
x264. VA-API is missing.

**Proposal check.** VA-API is the right addition for Intel and AMD, but the element names are
from the deprecated `gstreamer-vaapi` plugin. Current GStreamer provides VA-API encoding through
the `va` plugin in gst-plugins-bad: `vah264enc`, `vah265enc`, `vaav1enc` (and low-power `…lpenc`
variants). Neither is installed on this NVIDIA machine.

**Plan.**

1. Extend the encoder probe: Auto = NVENC → VA (`vah264enc`, then `vah264lpenc`) → x264, and
   add explicit "VA-API H.264/H.265/AV1" choices that appear only when the elements exist.
2. A real-encode probe (encode a few frames, check for errors) like the NVENC one, because a
   present element does not mean a working driver.
3. Zero-copy where possible: feed `vah264enc` VA memory (`vapostproc` upload) and NVENC
   CUDA/GL memory once the GL canvas (2b) exists.
4. CI: the encoder selection logic tested with mocked element availability; a real VA-API
   encode needs an Intel/AMD machine (manual gate).

**Done when:** on an Intel or AMD machine Auto records with VA-API and the file plays; on this
machine nothing changes; the probe falls back cleanly when the VA driver is broken.

## 5. Desktop portal integration

### 5a. Screencast restore token: already implemented

`portal.py` requests screencasts with `persist_mode = 2` and stores the returned
`restore_token` (`~/.local/state/anycam/screen-restore-token`); the next share passes it back.
Whether the chooser is skipped depends on the compositor's portal backend honouring
persistence.

**Plan:** verify on KDE, niri and GNOME portals; if it works, remove "Screen share asks the
portal chooser each launch" from the README limitations, otherwise document which backends
ignore the token.

### 5b. Tray icon and global shortcuts

**Today.** No tray icon; shortcuts (`Ctrl+1…9` scenes, `Ctrl+R`, `Ctrl+Shift+V`, `Ctrl+M`)
work only while the AnyCam window has focus.

**Proposal check.** Correct. Note that Qt's `QSystemTrayIcon` already speaks
`org.kde.StatusNotifierItem` over D-Bus on Linux, so no hand-written D-Bus code is needed for the
tray. The GlobalShortcuts portal is implemented by the KDE and Hyprland portal backends; support
in niri's and GNOME's backends must be checked before relying on it.

**Plan.**

1. Tray (`QSystemTrayIcon`): show/hide window, virtual camera on/off, record, mute, scene list,
   quit. Closing the window can keep running in the tray (opt-in setting).
2. `portal_shortcuts.py`: `CreateSession` + `BindShortcuts` on
   `org.freedesktop.portal.GlobalShortcuts` for: next/previous scene, scenes 1–9, mute, record,
   virtual camera. Handle the `Activated` signal on the GLib loop, forward to Qt with queued
   signals (the same pattern as the phone link).
3. Settings → Shortcuts shows whether the portal is available and lets the user rebind through
   the portal's own dialog; without the portal, the in-app shortcuts stay as they are.

**Done when:** on KDE, scene switching and mute work while another app has focus; the tray
controls work; on a compositor without the portal nothing breaks and Settings says why.

---

## Order of work

**Phase A: small, low risk.**
1. Verify 5a and fix the README limitation.
2. In-process TLS with `cryptography` (3.1).
3. firewalld D-Bus backend (3.2).
4. VA-API in the encoder probe (4.1–4.2).
5. Tray icon and GlobalShortcuts (5b).

**Phase B: PipeWire camera.** Spike first (1.1). Ship as an output beside v4l2loopback only
after Firefox and one Chromium app are confirmed.

**Phase C: zero-copy preview** (2a). The largest measured CPU win available today.

**Phase D: GPU canvas and GPU model input** (2b). Research and measurement first; build only
what the measurements justify.

## Rules for every item

* Measure before and after, on the reference machine, and record the numbers in `PARITY.md`.
* Keep the current path as the fallback until the new one has passed its gate on real hardware.
* Root and network changes only through the user's explicit action and the normal password
  prompt, as today.
* A new optional dependency (`cryptography`, a QML sink package) must not become required for
  the basic install.
