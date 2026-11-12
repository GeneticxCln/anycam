"use strict";
// AnyCam remote: switch scenes, mute the microphone and start or stop recording
// from a phone that is already paired (same login as the camera page).

const STORE = "anycam-phone";
const $ = (id) => document.getElementById(id);
const ui = { message: $("message"), scenes: $("scenes"), mute: $("mute"), record: $("record") };
let creds = null;
try { creds = JSON.parse(localStorage.getItem(STORE) || "null"); } catch { creds = null; }

function auth() { return { Authorization: "Bearer " + creds.device + "." + creds.secret, "Content-Type": "application/json" }; }
function say(text, error) { ui.message.textContent = text; ui.message.className = error ? "error" : ""; }

function show(state) {
  ui.scenes.textContent = "";
  for (const scene of state.scenes || []) {
    const button = document.createElement("button");
    button.textContent = scene.name;
    button.className = scene.id === state.active ? "on" : "";
    button.addEventListener("click", () => send({ action: "scene", scene_id: scene.id }));
    ui.scenes.appendChild(button);
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
  if (!response.ok) throw new Error(data.error || "Request failed (" + response.status + ")");
  return data;
}

async function send(body) {
  try { show(await call("/api/control", body)); } catch (error) { say(error.message, true); }
}

async function refresh() {
  try { show(await call("/api/control/state")); } catch (error) { say(error.message, true); }
}

if (!creds) {
  say("This phone is not paired. Open the camera page from AnyCam's QR code first.", true);
} else {
  ui.mute.addEventListener("click", () => send({ action: "mute" }));
  ui.record.addEventListener("click", () => send({ action: "record" }));
  refresh();
  setInterval(refresh, 4000);
}
