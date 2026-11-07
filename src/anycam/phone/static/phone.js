"use strict";
// AnyCam phone camera: pair once from the QR code, then send the camera to
// AnyCam over WebRTC (local network only, no servers outside this computer).

const STORE = "anycam-phone";
const $ = (id) => document.getElementById(id);
const ui = {
  intro: $("intro"), introText: $("intro-text"), stage: $("stage"), bar: $("bar"),
  status: $("status"), text: $("status-text"), video: $("preview"),
  toggle: $("toggle"), flip: $("flip"), quality: $("quality"),
};

let creds = loadCreds();
let facing = read("facing") || "environment";
ui.quality.value = read("quality") === "1080" ? "1080" : "720";
let stream = null;
let pc = null;
let session = null;
let wanted = false;
let retries = 0;
let retryTimer = null;
let dropTimer = null;
let wakeLock = null;

function read(key) { try { return localStorage.getItem(STORE + "-" + key); } catch { return null; } }
function write(key, value) { try { localStorage.setItem(STORE + "-" + key, value); } catch { /* private mode */ } }
function loadCreds() {
  try { return JSON.parse(localStorage.getItem(STORE) || "null"); } catch { return null; }
}
function saveCreds(value) {
  creds = value;
  try {
    if (value) localStorage.setItem(STORE, JSON.stringify(value));
    else localStorage.removeItem(STORE);
  } catch { /* private mode: paired for this visit only */ }
}
function auth() { return { Authorization: "Bearer " + creds.device + "." + creds.secret }; }

function setStatus(text, kind) {
  ui.text.textContent = text;
  ui.status.className = kind || "";
}

function showIntro(message) {
  ui.intro.hidden = false;
  ui.stage.hidden = true;
  ui.bar.hidden = true;
  if (message) ui.introText.textContent = message;
}

function updateButtons() {
  ui.toggle.textContent = wanted ? "Stop" : "Start";
  ui.toggle.className = wanted ? "" : "primary";
  ui.video.style.transform = facing === "user" ? "scaleX(-1)" : "";
}

async function deviceName() {
  try {
    if (navigator.userAgentData && navigator.userAgentData.getHighEntropyValues) {
      const values = await navigator.userAgentData.getHighEntropyValues(["model"]);
      if (values.model) return values.model;
    }
  } catch { /* fall through */ }
  const match = /Android [^;]+; ([^;)]+)/.exec(navigator.userAgent);
  return match && match[1].trim() !== "K" ? match[1].trim() : "Phone";
}

async function pair(token) {
  setStatus("Pairing with AnyCam…");
  const response = await fetch("/api/pair", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, name: await deviceName() }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Pairing failed (" + response.status + ")");
  saveCreds({ device: data.device, secret: data.secret, name: data.name });
}

function unpaired() {
  saveCreds(null);
  stop(false);
  showIntro("This phone is no longer paired. In AnyCam choose Add → Phone camera and scan the new QR code.");
}

// ------------------------------------------------------------------ camera
function stopCamera() {
  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = null;
}

async function openCamera() {
  stopCamera();
  const height = Number(ui.quality.value);
  const width = height === 1080 ? 1920 : 1280;
  setStatus("Opening the camera…");
  stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: {
      facingMode: { ideal: facing },
      width: { ideal: width }, height: { ideal: height },
      frameRate: { ideal: 30, max: 30 },
    },
  });
  ui.video.srcObject = stream;
  const track = stream.getVideoTracks()[0];
  track.contentHint = "motion";
  const owner = stream;
  track.addEventListener("ended", () => {
    if (wanted && stream === owner) retryLater("The camera stopped");
  });
}

function cameraLive() {
  return !!stream && stream.getVideoTracks().some((track) => track.readyState === "live");
}

// ----------------------------------------------------------------- network
function closePeer() {
  clearTimeout(dropTimer);
  if (pc) pc.close();
  pc = null;
  session = null;
}

function iceGathered(peer) {
  if (peer.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, 3000);
    peer.addEventListener("icegatheringstatechange", () => {
      if (peer.iceGatheringState === "complete") { clearTimeout(timer); resolve(); }
    });
  });
}

function preferCodecs(transceiver) {
  if (!transceiver.setCodecPreferences || !window.RTCRtpSender || !RTCRtpSender.getCapabilities) return;
  const caps = RTCRtpSender.getCapabilities("video");
  if (!caps) return;
  // Hardware H.264 first (cool phone, small battery cost), then VP8.
  const rank = (codec) => {
    const mime = codec.mimeType.toLowerCase();
    if (mime === "video/h264") return /42e01f/i.test(codec.sdpFmtpLine || "") ? 0 : 1;
    if (mime === "video/vp8") return 2;
    if (mime === "video/rtx" || mime === "video/red" || mime === "video/ulpfec") return 4;
    return 3;
  };
  try { transceiver.setCodecPreferences(caps.codecs.slice().sort((a, b) => rank(a) - rank(b))); }
  catch { /* keep the browser's order */ }
}

async function limitBitrate(sender) {
  try {
    const params = sender.getParameters();
    if (!params.encodings || !params.encodings.length) params.encodings = [{}];
    params.encodings[0].maxBitrate = ui.quality.value === "1080" ? 6000000 : 3500000;
    params.degradationPreference = "maintain-framerate";
    await sender.setParameters(params);
  } catch { /* older WebView: browser default bitrate */ }
}

async function connect() {
  closePeer();
  setStatus("Connecting to AnyCam…");
  const peer = new RTCPeerConnection({ iceServers: [] });
  pc = peer;
  const track = stream.getVideoTracks()[0];
  const transceiver = peer.addTransceiver(track, { direction: "sendonly", streams: [stream] });
  preferCodecs(transceiver);
  peer.addEventListener("connectionstatechange", () => onPeerState(peer));
  await peer.setLocalDescription(await peer.createOffer());
  await iceGathered(peer);
  if (peer !== pc) return;
  const response = await fetch("/api/whip", {
    method: "POST",
    headers: { ...auth(), "Content-Type": "application/sdp" },
    body: peer.localDescription.sdp,
  });
  if (response.status === 401) { unpaired(); return; }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || "AnyCam refused the stream (" + response.status + ")");
  }
  const answer = await response.text();
  if (peer !== pc) return;
  session = response.headers.get("Location");
  await peer.setRemoteDescription({ type: "answer", sdp: answer });
  await limitBitrate(transceiver.sender);
}

function onPeerState(peer) {
  if (peer !== pc) return;
  const state = peer.connectionState;
  clearTimeout(dropTimer);
  if (state === "connected") {
    retries = 0;
    setStatus("Live in AnyCam" + (creds && creds.name ? " · " + creds.name : ""), "live");
    keepAwake();
  } else if (state === "disconnected") {
    setStatus("Connection interrupted…", "error");
    // A short Wi-Fi hiccup recovers on its own; give it a few seconds.
    dropTimer = setTimeout(() => { if (peer === pc) retryLater("Connection lost"); }, 5000);
  } else if (state === "failed") {
    retryLater("Could not reach AnyCam (same Wi-Fi? firewall?)");
  }
}

// --------------------------------------------------------------- lifecycle
function retryLater(reason) {
  if (!wanted) return;
  closePeer();
  clearTimeout(retryTimer);
  retries += 1;
  const seconds = Math.min(30, 2 ** Math.min(retries, 5));
  setStatus(reason + " · retrying in " + seconds + " s", "error");
  retryTimer = setTimeout(start, seconds * 1000);
}

function failed(error) {
  if (error && (error.name === "NotAllowedError" || error.name === "SecurityError")) {
    wanted = false;
    updateButtons();
    setStatus("Camera permission denied. Allow the camera for this page in the browser's site settings, then tap Start.", "error");
    return;
  }
  if (error && (error.name === "NotFoundError" || error.name === "OverconstrainedError")) {
    wanted = false;
    updateButtons();
    setStatus("This phone has no camera that can be used here.", "error");
    return;
  }
  retryLater((error && error.message) || "Something went wrong");
}

async function start() {
  wanted = true;
  clearTimeout(retryTimer);
  updateButtons();
  try {
    if (!cameraLive()) await openCamera();
    await connect();
  } catch (error) {
    failed(error);
  }
}

function stop(notify = true) {
  wanted = false;
  clearTimeout(retryTimer);
  const ended = session;
  closePeer();
  stopCamera();
  releaseWake();
  updateButtons();
  setStatus("Stopped. Tap Start to send the camera again.");
  if (notify && ended && creds) {
    fetch(ended, { method: "DELETE", headers: auth(), keepalive: true }).catch(() => {});
  }
}

async function reopenCamera() {
  if (!wanted) return;
  try {
    await openCamera();
    const sender = pc && pc.getSenders()[0];
    if (sender && pc.connectionState === "connected") {
      await sender.replaceTrack(stream.getVideoTracks()[0]);
      await limitBitrate(sender);
    } else {
      await connect();
    }
  } catch (error) {
    failed(error);
  }
}

async function keepAwake() {
  try {
    if ("wakeLock" in navigator && !wakeLock) {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
    }
  } catch { /* battery saver or unsupported */ }
}

function releaseWake() {
  if (wakeLock) wakeLock.release().catch(() => {});
  wakeLock = null;
}

ui.toggle.addEventListener("click", () => (wanted ? stop() : start()));
ui.flip.addEventListener("click", () => {
  facing = facing === "user" ? "environment" : "user";
  write("facing", facing);
  updateButtons();
  reopenCamera();
});
ui.quality.addEventListener("change", () => {
  write("quality", ui.quality.value);
  reopenCamera();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !wanted) return;
  keepAwake();
  // Android stops the camera while the browser is in the background.
  if (!cameraLive() || !pc) start();
});
window.addEventListener("pagehide", () => {
  if (session && creds) {
    fetch(session, { method: "DELETE", headers: auth(), keepalive: true }).catch(() => {});
  }
});

async function main() {
  updateButtons();
  if (!window.isSecureContext || !navigator.mediaDevices || !window.RTCPeerConnection) {
    showIntro("Open this page from the QR code in AnyCam (it must be the https:// address).");
    return;
  }
  const token = new URLSearchParams(location.hash.slice(1)).get("pair");
  if (token) {
    history.replaceState(null, "", location.pathname);
    try {
      await pair(token);
    } catch (error) {
      if (!creds) { showIntro(error.message); return; }
      setStatus(error.message, "error");
    }
  }
  if (!creds) { showIntro(); return; }
  const check = await fetch("/api/check", { headers: auth() }).then((r) => r.status).catch(() => 0);
  if (check === 401) { unpaired(); return; }
  start();
}

main();
