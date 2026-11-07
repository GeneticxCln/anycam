# Phone cameras and the phone remote (M8)

Design and build order for using a phone as an AnyCam **source** and as a
**remote control**. Local network only: no cloud, no account, no relay.
**Android only.** The phone page targets Android Chrome and Android WebView;
no other mobile platform is supported, tested or targeted.

## 0. Status (built)

The camera half is built (`src/anycam/phone/`, `src/anycam/phone_dialog.py`):

1. **Add → Phone camera (QR code)…** starts the phone link (HTTPS on the LAN
   address, port 8443) and shows a one-time QR code:
   `https://<lan-ip>:8443/#pair=<token>`. The token rides in the URL fragment,
   so it never appears in a request line; it works once, for five minutes, and
   the dialog renews it before it expires.
2. The phone opens the page, accepts the self-signed certificate once
   (**Advanced → Proceed**; the dialog shows the SHA-256 fingerprint), pairs
   (`POST /api/pair`) and keeps its device secret in `localStorage`. Secrets
   are stored hashed in `$XDG_STATE_HOME/anycam/phones.json` (mode 600).
3. The page sends its camera with WHIP (`POST /api/whip`, non-trickle: both
   SDPs carry their host candidates). `webrtcbin` decodes it on the CPU and
   letterboxes it into 1920x1080 on `intervideosink channel=anycam-phone-<id>`.
4. The pool's `phone` capture kind reads that channel. Before the phone
   connects, and a second after it leaves, the layer shows black; the canvas
   never waits on the network. A paired phone reconnects by opening the page
   again; a scene with a phone layer starts the link on its own.

Deviations from the plan below, and why:

- **No `aiohttp`/`cryptography`/`segno` requirement.** The server is stdlib
  (`http.server` + `ssl`, TLS handshake on the request thread), the
  certificate comes from the `openssl` CLI, and the QR from `segno` if
  installed or else the `qrencode` CLI. No WebSocket is needed for the camera.
- **No pinned fingerprint in the QR.** A web page cannot check its own
  certificate; the trust step is the browser's warning plus the fingerprint
  shown in the dialog.
- **No fixed UDP port range.** Reading `webrtcbin`'s `ice-agent` from Python
  drops a reference PyGObject never owned (criticals, then a crash). Media
  passes a stateful firewall anyway, because the desktop's own ICE checks open
  the return path; only TCP 8443 needs a rule.
- The offer's `urn:3gpp:video-orientation` extension is removed, so Android
  rotates the pixels itself instead of tagging frames the receiver ignores.

Not built yet: the phone remote (M8.2), phone audio, a devices manager to
forget a paired phone from the UI (`PhoneService.forget` exists), and the
hardware pass on real Android phones (M8.5).

## 1. Without pairing: a camera app's URL

The **Network** layer already accepts any URL GStreamer can decode, so a phone
running an MJPEG or RTSP server is a usable camera right now:

1. Install a camera server app on the Android phone (IP Webcam, DroidCam, or
   anything that exposes an MJPEG/RTSP URL).
2. Put the phone and the computer on the same Wi-Fi.
3. AnyCam → **Add → Network stream URL…** and enter the URL shown by the app, for example
   `http://192.168.1.42:8080/video` (MJPEG).

That path is intentionally basic: the video hangs off `uridecodebin`
(`sources.network_chain`), there is no pairing, no encryption and no remote
control, and the phone app must keep the camera open. Everything below replaces
it with a first-party protocol.

## 2. Target architecture

```
 phone browser (HTTPS page, getUserMedia)
   │  WebRTC media (SRTP/DTLS)          ┌─ WebSocket JSON-RPC (remote)
   ▼                                    ▼
 AnyCam remote server (asyncio, TLS, pairing, auth)
   │  one receive pipeline per phone
   ▼
 webrtcbin → rtph264depay → h264parse → avdec_h264 → videoconvert → videorate
   → intervideosink channel=anycam-phone-<device_id>
   │
   ▼
 existing SourcePool / layer_tail / compositor → preview, vcam, MP4, RTMP
```

The important design rule: **the phone is just another capture behind the
existing `intervideosink` channel contract**. The compositor, crop, effects,
recording, virtual camera and startup/teardown logic do not learn anything
about phones. A phone that stops sending behaves like a stalled camera: the
pool health watchdog marks it dead and the layer keeps its box.

Checklist against this machine: `webrtcbin`, `nice`, `dtls`, `srtp`, `rtsp`
are installed; `whipclientsrc`/`webrtcsrc` (gst-plugins-rs) are **not**, so the
receive side is built on `webrtcbin` directly. If a packaged build later ships
`whipclientsrc`, it can replace that pipeline without changing the protocol.

## 3. Protocol

Pairing (one-time token, shown as a QR code on the desktop):

- Desktop **Pair phone…** generates a 32-byte token valid for 5 minutes and
  renders `anycam://pair?host=<ip>&port=8443&token=<tok>&fp=<sha256-cert>`.
- Phone opens `https://<ip>:8443/` inside a TLS session whose certificate must
  match `fp` (the page pins it; the QR is the trust anchor).
- `POST /api/pair {"token": ..., "name": ..., "kind": "source|remote|both"}`
  returns a device id and a per-device secret. The token is consumed.
- Secrets are stored hashed (SHA-256) in
  `$XDG_STATE_HOME/anycam/devices.json`; the phone stores its secret in
  `localStorage` and sends it as `Authorization: Bearer`.

Source session (after pairing):

- `POST /api/whip` with the SDP offer → SDP answer, session id.
- `DELETE /api/whip/<id>` ends the stream. WHIP is a small standard; if the
  phone page uses plain `RTCPeerConnection`, the server can also accept the
  offer as JSON over the authenticated WebSocket and answer there. Pick one
  and keep it: WHIP first, WebSocket signaling as the fallback for Android
  WebViews that cannot POST an SDP body easily.
- Host ICE candidates only. No STUN/TURN: both ends are on the LAN, and a
  failed host candidate should be reported as such instead of silently trying
  the internet.

Remote (same authenticated WebSocket, `wss://<ip>:8443/ws`):

```json
{"id": 1, "cmd": "status"}
{"id": 2, "cmd": "scene.list"}
{"id": 3, "cmd": "scene.switch", "scene": "scene_ab12cd34"}
{"id": 4, "cmd": "record.start"}
{"id": 5, "cmd": "vcam.toggle", "on": true}
{"id": 6, "cmd": "live.start"}
{"id": 7, "cmd": "layer.visible", "layer": "layer_..., ", "visible": false}
```

Answers echo `id`; every command is authorized per device, and a `remote`-only
device never receives frames. Keep the command set small and explicit — the
phone must not send arbitrary GStreamer properties.

## 4. Files and integration points

```
src/anycam/remote/__init__.py
src/anycam/remote/server.py      # asyncio HTTPS + WebSocket, TLS, routing
src/anycam/remote/auth.py        # pairing tokens, hashed device registry
src/anycam/remote/webrtc.py      # webrtcbin receive pipeline -> intervideosink
src/anycam/remote/protocol.py    # message schema + validation (no Qt import)
src/anycam/remote/control.py     # RemoteControl(QObject): Qt signals only
src/anycam/remote/static/index.html, phone.js, remote.js, style.css
tests/test_remote_auth.py, tests/test_remote_control.py, tests/test_phone_source.py
```

- `server.py` runs on its own asyncio thread and imports no Qt. `control.py`
  exposes Qt signals (`scene_requested`, `record_toggle_requested`, …) that the
  window connects to; the server emits them with `QMetaObject.invokeMethod`
  (queued) so no widget is ever touched from the network thread.
- `SourcePool` needs one small addition: `acquire(spec)` currently builds the
  capture itself. Add `SourcePool.adopt(spec, entry)` (or an optional
  `builder=`) so an externally-built phone receiver pipeline can be ref-counted,
  given a stable channel with `channel_for(spec)`, and torn down with the same
  `release()` path. Add `"phone"` to `CAPTURE_KINDS`; `capture_spec()` returns
  `CaptureSpec("phone", device_id, width, height, fps, expected=...)`.
- The window gets **Pair phone…** and **Phone devices…** actions plus a
  `Layer(kind="phone", source=device_id)`. The existing **Add → Phone**
  (network URL) stays for third-party apps.
- Optional extra in `pyproject.toml`:

```toml
remote = ["aiohttp>=3.9", "cryptography>=42", "segno>=1.6"]
```

`aiohttp` is needed because the stdlib cannot serve WebSocket; `cryptography`
generates the self-signed certificate on first enable; `segno` renders the QR
with no C dependency. All three stay optional so the current install does not
grow.

## 5. Security model

- Remote control is **off by default**, enabled from the Output tab, and binds
  to one chosen LAN address (not `0.0.0.0` by default). Turning it off stops
  the server and drops every session.
- TLS from first run, self-signed, pinned by the QR fingerprint; no certificate
  authority and no cloud service involved.
- Pairing tokens are single-use, 5 minute TTL, rate-limited per source IP;
  device secrets are random and stored hashed. An unpaired request gets HTTP
  401/WS close before any SDP or frame is accepted.
- No STUN/TURN by default, so traffic cannot silently leave the LAN. Document
  the firewall rule (`tcp/8443`) and the Wi-Fi trust assumption.
- The stream key is never sent to a phone; remote commands are allow-listed.

## 6. Build order and acceptance gates

| Slice | Work | Gate |
|---|---|---|
| M8.1 | `server.py` + `auth.py`: TLS, pairing QR, device registry, `/ws` status | `curl` gets 401 unpaired; a paired loopback client gets status; token replay fails; certificate fingerprint matches the QR |
| M8.2 | `control.py` + Output-tab switch: scene switch, record, vcam, live, snapshot | offscreen Qt test: each command changes the same state as the button; a command while stopped answers `ok:false` and does not start a hidden engine |
| M8.3 | `webrtc.py`: `webrtcbin` receive → `intervideosink`; pool `adopt` | synthetic GStreamer sender on loopback appears as a frame in the canvas; killing it leaves the scene running and marks the source stalled; recording/vcam survive |
| M8.4 | Phone page: camera preview, start/stop, remote buttons, reconnect with backoff | page works in Android Chrome and Android WebView; unpaired page shows only the pairing form; after a 30 s network drop it reconnects without touching recording |
| M8.5 | Hardware pass: two Android phones as source and as remote | both phones usable at the same time as one source + one remote; unpaired device receives nothing |

Test these slices with synthetic sources and Qt offscreen. A pytest fixture
built on `webrtcbin` (sender side) or a recorded SDP offer covers M8.3 without
a phone; the physical phones are only the last gate.

## 7. Risks and fallbacks

- **`webrtcbin` pad plumbing** (bundle, `pad-added`, RTP funnel) is the highest
  risk. Budget for a spike with two local peers before building the UI.
- **Codec choice on Android**: offer H.264 baseline as well as VP8. Android
  Chrome can hardware-encode either; H.264 decodes more reliably on older
  devices. Test both in Android Chrome, do not assume VP8.
- **Qt threading**: the asyncio thread must only emit signals. A direct widget
  call will pass a smoke test and fail under load.
- **Fallback if WebRTC blocks the milestone**: MJPEG over authenticated
  WebSocket into a server-owned `appsrc` + `videorate` is far simpler, works on
  every WebView, and still satisfies source + remote. It costs CPU and latency
  (~5–10 Mbit/s at 720p15) and should be labelled as the fallback, not the
  design.
- **Phone battery/heat**: WebRTC hardware encode is the reason to prefer it;
  MJPEG at 1080p will drain a phone quickly. Default the page to 720p30 with a
  quality selector.
