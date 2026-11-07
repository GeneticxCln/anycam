# Upgrade plan: native Linux integration

A plan for moving AnyCam further onto native Linux mechanisms: a PipeWire camera, zero-copy
GPU paths, D-Bus instead of subprocesses, hardware encoders and desktop portals. Each item
says what AnyCam does today (checked against the code and the reference machine on
2026-11-07), what the proposal gets wrong if anything, the plan, and how we know it is done.

Reference machine: CachyOS, KDE/Wayland (kinetic-we), NVIDIA RTX 4060 Ti, GStreamer 1.28,
PipeWire 1.6.10, Vesktop (Electron 44).

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
