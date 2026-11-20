"use strict";
// AnyCam remote: switch scenes, mute the microphone and start or stop recording
// from a phone that is already paired (same login as the camera page).

const STORE = "anycam-phone";
const $ = (id) => document.getElementById(id);
const ui = { message: $("message"), scenes: $("scenes"), mute: $("mute"), record: $("record") };
let creds = null;
try { creds = JSON.parse(localStorage.getItem(STORE) || "null"); } catch { creds = null; }
let pollTimer = null;
let refreshing = false;
let lastScenesKey = "";

function auth() { return { Authorization: "Bearer " + creds.device + "." + creds.secret, "Content-Type": "application/json" }; }
function say(text, error) { ui.message.textContent = text; ui.message.className = error ? "error" : ""; }

function clearCreds() {
  creds = null;
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  try { localStorage.removeItem(STORE); } catch { /* private mode */ }
}

function show(state) {
  const scenes = state.scenes || [];
  const scenesKey = JSON.stringify(scenes.map((scene) => [scene.id, scene.name]));
  if (scenesKey !== lastScenesKey) {
    lastScenesKey = scenesKey;
    ui.scenes.textContent = "";
    for (const scene of scenes) {
      const button = document.createElement("button");
      button.textContent = scene.name;
      button.dataset.sceneId = scene.id;
      button.className = scene.id === state.active ? "on" : "";
      button.addEventListener("click", () => send({ action: "scene", scene_id: scene.id }));
      ui.scenes.appendChild(button);
    }
  } else {
    for (const button of ui.scenes.children) {
      button.className = button.dataset.sceneId === state.active ? "on" : "";
    }
  }
  ui.mute.textContent = state.muted ? "Microphone: muted" : "Microphone: live";
  ui.mute.className = state.muted ? "" : "on";
  ui.record.textContent = state.recording ? "Stop recording" : "Record";
  ui.record.className = "rec" + (state.recording ? " on" : "");
  say("Connected to AnyCam.");
}

async function call(path, body) {
  const response = await fetch(path, body === undefined
    ? { headers: auth() }
    : { method: "POST", headers: auth(), body: JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || "Request failed (" + response.status + ")");
    error.status = response.status;
    throw error;
  }
  return data;
}

function handleError(error) {
  if (error && error.status === 401) {
    clearCreds();
    say("This phone is no longer paired. Open the camera page from AnyCam's QR code to pair again.", true);
    return;
  }
  if (error && error.status === 403) {
    say(error.message || "Remote control is switched off in AnyCam.", true);
    return;
  }
  say((error && error.message) || "Could not reach AnyCam.", true);
}

async function send(body) {
  if (!creds) return;
  try { show(await call("/api/control", body)); } catch (error) { handleError(error); }
}

async function refresh() {
  if (refreshing || !creds) return;
  refreshing = true;
  try { show(await call("/api/control/state")); } catch (error) { handleError(error); }
  finally { refreshing = false; }
}

if (!creds) {
  say("This phone is not paired. Open the camera page from AnyCam's QR code first.", true);
} else {
  ui.mute.addEventListener("click", () => send({ action: "mute" }));
  ui.record.addEventListener("click", () => send({ action: "record" }));
  refresh();
  pollTimer = setInterval(refresh, 4000);
}
