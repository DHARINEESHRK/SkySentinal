const webcamTab = document.querySelector("#webcamTab");
const cctvTab = document.querySelector("#cctvTab");
const webcamControls = document.querySelector("#webcamControls");
const cctvControls = document.querySelector("#cctvControls");
const webcamVideo = document.querySelector("#webcamVideo");
const annotatedImage = document.querySelector("#annotatedImage");
const cctvImage = document.querySelector("#cctvImage");
const viewport = document.querySelector("#viewport");
const canvas = document.querySelector("#frameCanvas");
const context = canvas.getContext("2d", { alpha: false });
let cameraStream = null;
let detectionTimer = null;
let cctvTimer = null;
let activeMode = "webcam";
let busy = false;
let toastTimer = null;
let encounterOffset = 0;
let encounterRequestId = 0;
const encounterLimit = 10;

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 4200);
}

function setLive(isLive, label = "NO ACTIVE FEED") {
  document.querySelector("#stateIndicator").parentElement.classList.toggle("is-live", isLive);
  document.querySelector("#stateText").textContent = isLive ? "MONITORING" : "STANDBY";
  document.querySelector("#liveMark").classList.toggle("active", isLive);
  document.querySelector("#feedLabel").textContent = label;
  if (!isLive) document.querySelector("#frameFps").textContent = "-- FPS";
}

function resetResults() {
  document.querySelector("#detectionCount").textContent = "00";
  document.querySelector("#detectionList").innerHTML = '<div class="no-detections"><span class="empty-cross">＋</span><span>No detections in current frame</span></div>';
}

function setMode(mode) {
  if (mode === activeMode) return;
  if (activeMode === "webcam") stopWebcam();
  if (activeMode === "cctv") stopCctv();
  activeMode = mode;
  const isWebcam = mode === "webcam";
  webcamTab.classList.toggle("active", isWebcam);
  cctvTab.classList.toggle("active", !isWebcam);
  webcamTab.setAttribute("aria-selected", String(isWebcam));
  cctvTab.setAttribute("aria-selected", String(!isWebcam));
  webcamControls.classList.toggle("hidden", !isWebcam);
  cctvControls.classList.toggle("hidden", isWebcam);
  viewport.classList.remove("has-webcam", "has-cctv");
  document.querySelector("#emptyState").style.display = "flex";
  setLive(false);
  resetResults();
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function renderResults(data) {
  const detections = data.detections || [];
  document.querySelector("#detectionCount").textContent = String(detections.length).padStart(2, "0");
  document.querySelector("#detectionList").innerHTML = detections.length
    ? detections.map((item) => `<div class="detection-item"><span class="detection-label">${escapeHtml(item.label)} · ${escapeHtml((item.model_sources || []).join(", "))}</span><span class="detection-percent">${Math.round(item.confidence * 100)}%</span><div class="detection-bar"><span style="width:${Math.round(item.confidence * 100)}%"></span></div><span class="detection-label">TRACK ${escapeHtml(item.tracking_id || "--")} · RISK ${escapeHtml(item.risk_score ?? "--")}</span></div>`).join("")
    : '<div class="no-detections"><span class="empty-cross">＋</span><span>No detections in current frame</span></div>';
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

async function refreshSystemStatus() {
  try {
    const data = await fetchJson("/api/dashboard/status");
    document.querySelector("#cloudQueueState").textContent =
      `${data.cloud_sync.pending} pending · ${data.cloud_sync.failed} failed`;
    document.querySelector("#esp32State").textContent =
      data.esp32.online ? `ONLINE · ${data.esp32.device_id}` : "OFFLINE";
    document.querySelector("#esp32SelfTestState").textContent =
      String(data.esp32.sensor_states.startup_self_test || "unknown").replaceAll("_", " ").toUpperCase();
    const outputs = data.esp32.sensor_states.output_states;
    const outputSummary = outputs
      ? ` · last reported outputs: buzzer ${outputs.buzzer ? "ON" : "OFF"}, red ${outputs.red_led ? "ON" : "OFF"}, green ${outputs.green_led ? "ON" : "OFF"}`
      : "";
    if (!data.hardware_commands.length) {
      document.querySelector("#hardwareDelivery").textContent =
        `No hardware command has been sent.${outputSummary}`;
    } else {
      const latestCommand = data.hardware_commands[0];
      const delivery = latestCommand.simulation
        ? "simulated; physical output not verified"
        : latestCommand.status === "delivered"
          ? "device acknowledged"
          : latestCommand.status === "pending"
            ? "pending; delivery not confirmed"
            : latestCommand.status;
      document.querySelector("#hardwareDelivery").textContent =
        `Latest: ${latestCommand.command} · ${delivery}${outputSummary}`;
    }
    const states = data.sensors || {};
    document.querySelector("#sensorStates").innerHTML = Object.entries(states)
      .map(([sensor, info]) => `<div class="sensor-state"><span>${escapeHtml(sensor.replaceAll("_", " ").toUpperCase())}</span><strong class="${info.state === "active" ? "active" : ""}">${escapeHtml(info.state.toUpperCase())}</strong></div>`)
      .join("");
    document.querySelector("#simulateSensor").classList.toggle("hidden", !data.sensor_simulation);
    renderAlerts(data.recent_alerts || []);
  } catch {
    document.querySelector("#cloudQueueState").textContent = "UNAVAILABLE · RETRYING";
    document.querySelector("#esp32State").textContent = "STATUS UNAVAILABLE";
    document.querySelector("#esp32SelfTestState").textContent = "STATUS UNAVAILABLE";
    document.querySelector("#sensorStates").innerHTML =
      '<p class="sensor-disclaimer">System status unavailable. Retrying automatically.</p>';
  }
}

function renderAlerts(items) {
  document.querySelector("#recentAlerts").innerHTML = items.length
    ? items.map((item) => `<div class="alert-item"><strong>${escapeHtml(item.risk_level.toUpperCase())} · ${escapeHtml(item.risk_score)}</strong><br>Encounter ${escapeHtml(item.encounter_id.slice(0, 8))}<br>${escapeHtml(new Date(item.timestamp).toLocaleString())}<br>Physical notification: ${escapeHtml(item.delivery_status.toUpperCase())} · ${item.acknowledged ? "ACKNOWLEDGED" : "UNACKNOWLEDGED"}</div>`).join("")
    : '<p class="no-detections">No alerts recorded.</p>';
}

async function loadEncounters() {
  const requestId = ++encounterRequestId;
  try {
    const level = document.querySelector("#riskFilter").value;
    const query = new URLSearchParams({ limit: String(encounterLimit), offset: String(encounterOffset), include_resolved: "true" });
    if (level) query.set("risk_level", level);
    const data = await fetchJson(`/api/encounters?${query}`);
    if (requestId !== encounterRequestId) return;
    document.querySelector("#encounterCount").textContent = String(data.total).padStart(2, "0");
    document.querySelector("#pageLabel").textContent = `PAGE ${Math.floor(encounterOffset / encounterLimit) + 1} · ${data.total} TOTAL`;
    document.querySelector("#previousPage").disabled = encounterOffset <= 0;
    document.querySelector("#nextPage").disabled = encounterOffset + encounterLimit >= data.total;
    document.querySelector("#encounterList").innerHTML = data.items.length
      ? data.items.map((item) => `<article class="encounter-card" data-id="${escapeHtml(item.encounter_id)}">
          <div class="encounter-topline"><strong>DRONE · ${escapeHtml(item.encounter_id.slice(0, 8))}</strong><span class="risk-${escapeHtml(item.risk_level)}">${escapeHtml(item.risk_level.toUpperCase())} ${escapeHtml(item.risk_score)}</span></div>
          <div class="encounter-meta"><span>${escapeHtml(item.source_id)} · track ${escapeHtml(item.tracking_id)}</span><span>${Math.round(item.latest_confidence * 100)}% · ${item.observation_count} obs</span></div>
          <div class="encounter-meta"><span>${escapeHtml(new Date(item.last_seen).toLocaleString())}</span><span>${escapeHtml(item.model_sources.join(", "))}</span></div>
          <div class="encounter-actions"><button class="small-button" data-action="details">DETAILS</button><button class="small-button" data-action="acknowledge" ${item.acknowledged ? "disabled" : ""}>ACK</button><button class="small-button" data-action="resolve" ${item.resolved ? "disabled" : ""}>RESOLVE</button></div>
        </article>`).join("")
      : '<p class="no-detections">No encounters match this filter.</p>';
  } catch {
    if (requestId === encounterRequestId) {
      document.querySelector("#encounterList").innerHTML =
        '<p class="no-detections">Encounter history unavailable. Retrying automatically.</p>';
    }
  }
}

async function startWebcam() {
  const button = document.querySelector("#webcamButton");
  button.disabled = true;
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false,
    });
    webcamVideo.srcObject = cameraStream;
    await webcamVideo.play();
    viewport.classList.add("has-webcam");
    setLive(true, "BROWSER WEBCAM");
    button.innerHTML = '<span class="button-symbol">■</span> STOP WEBCAM';
    button.disabled = false;
    detectionTimer = setInterval(sendWebcamFrame, 180);
    sendWebcamFrame();
  } catch (error) {
    button.disabled = false;
    showToast(error.name === "NotAllowedError"
      ? "Camera access was denied. Allow camera permission in your browser."
      : `Could not start webcam: ${error.message}`);
  }
}

function stopWebcam() {
  clearInterval(detectionTimer);
  detectionTimer = null;
  cameraStream?.getTracks().forEach((track) => track.stop());
  cameraStream = null;
  webcamVideo.srcObject = null;
  viewport.classList.remove("has-webcam");
  document.querySelector("#webcamButton").innerHTML = '<span class="button-symbol">▶</span> START WEBCAM';
  document.querySelector("#webcamButton").disabled = false;
  setLive(false);
}

async function sendWebcamFrame() {
  if (busy || !cameraStream || webcamVideo.readyState < 2 || !webcamVideo.videoWidth) return;
  busy = true;
  try {
    const scale = Math.min(1, 960 / webcamVideo.videoWidth);
    canvas.width = Math.round(webcamVideo.videoWidth * scale);
    canvas.height = Math.round(webcamVideo.videoHeight * scale);
    context.save();
    context.translate(canvas.width, 0);
    context.scale(-1, 1);
    context.drawImage(webcamVideo, 0, 0, canvas.width, canvas.height);
    context.restore();
    const response = await fetch("/api/detect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: canvas.toDataURL("image/jpeg", 0.78) }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Frame inference failed.");
    annotatedImage.src = result.image;
    renderResults(result);
    if (result.inference_latency_ms) {
      document.querySelector("#frameFps").textContent =
        `${(1000 / result.inference_latency_ms).toFixed(1)} FPS · ${result.inference_latency_ms} ms`;
    }
  } catch (error) {
    showToast(error.message);
    stopWebcam();
  } finally {
    busy = false;
  }
}

async function startCctv() {
  const source = document.querySelector("#cctvUrl").value.trim();
  if (!source) return showToast("Enter an authorized RTSP or RTSPS camera stream URL.");
  const button = document.querySelector("#cctvButton");
  button.disabled = true;
  try {
    const response = await fetch("/api/cctv/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not connect to CCTV.");
    viewport.classList.add("has-cctv");
    cctvImage.src = `/api/cctv/stream?ts=${Date.now()}`;
    setLive(true, "CCTV / RTSP");
    button.innerHTML = '<span class="button-symbol">■</span> DISCONNECT';
    button.disabled = false;
    cctvTimer = setInterval(pollCctvStatus, 700);
    pollCctvStatus();
  } catch (error) {
    showToast(error.message);
    button.disabled = false;
  }
}

async function pollCctvStatus() {
  try {
    const response = await fetch("/api/cctv/status");
    const data = await response.json();
    if (data.has_frame) {
      renderResults({
        winner: data.winner,
        confidence: Math.max(...Object.values(data.scores || { none: 0 })),
        scores: data.scores,
        detections: data.detections,
      });
      document.querySelector("#frameFps").textContent = `${data.fps} FPS · ${data.inference_latency_ms} ms`;
    }
    if (data.error) showToast(data.error);
    if (!data.active) stopCctv(false);
  } catch (error) {
    showToast(`CCTV status unavailable: ${error.message}`);
    stopCctv();
  }
}

function stopCctv(notifyServer = true) {
  clearInterval(cctvTimer);
  cctvTimer = null;
  if (notifyServer) {
    fetchJson("/api/cctv/stop", { method: "POST" })
      .catch((error) => showToast(`Could not stop CCTV cleanly: ${error.message}`));
  }
  cctvImage.removeAttribute("src");
  viewport.classList.remove("has-cctv");
  const button = document.querySelector("#cctvButton");
  button.innerHTML = '<span class="button-symbol">↗</span> CONNECT CCTV';
  button.disabled = false;
  setLive(false);
}

webcamTab.addEventListener("click", () => setMode("webcam"));
cctvTab.addEventListener("click", () => setMode("cctv"));
document.querySelector("#webcamButton").addEventListener("click", () => cameraStream ? stopWebcam() : startWebcam());
document.querySelector("#cctvButton").addEventListener("click", () => cctvTimer ? stopCctv() : startCctv());
document.querySelector("#revealUrl").addEventListener("click", () => {
  const input = document.querySelector("#cctvUrl");
  input.type = input.type === "password" ? "text" : "password";
});
document.querySelector("#cctvUrl").addEventListener("keydown", (event) => {
  if (event.key === "Enter") startCctv();
});
document.querySelector("#riskFilter").addEventListener("change", () => {
  encounterOffset = 0;
  loadEncounters();
});
document.querySelector("#previousPage").addEventListener("click", () => {
  encounterOffset = Math.max(0, encounterOffset - encounterLimit);
  loadEncounters();
});
document.querySelector("#nextPage").addEventListener("click", () => {
  encounterOffset += encounterLimit;
  loadEncounters();
});
document.querySelector("#encounterList").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const card = button.closest("[data-id]");
  const action = button.dataset.action;
  try {
    if (action === "details") {
      const data = await fetchJson(`/api/encounters/${encodeURIComponent(card.dataset.id)}`);
      const detail = document.createElement("div");
      detail.className = "encounter-detail";
      detail.textContent = `Risk reasons: ${data.encounter.risk_reasons.join(" ")} Missing: ${data.encounter.risk_missing.join(" ")} Risk history entries: ${data.risk_history.length}`;
      const old = card.querySelector(".encounter-detail");
      if (old) old.remove();
      else card.append(detail);
    } else {
      await fetchJson(`/api/encounters/${encodeURIComponent(card.dataset.id)}/${action}`, { method: "POST" });
      await Promise.all([loadEncounters(), refreshSystemStatus()]);
    }
  } catch (error) {
    showToast(`Encounter action failed: ${error.message}`);
  }
});
document.querySelectorAll("[data-command]").forEach((button) => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await fetchJson("/api/hardware/alerts/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: button.dataset.command }),
      });
      document.querySelector("#hardwareDelivery").textContent = result.delivered
        ? `Command ${result.command} simulated; no physical output was verified.`
        : `Command ${result.command} queued; physical delivery is not confirmed.`;
      await refreshSystemStatus();
    } catch (error) {
      showToast(`Hardware command failed: ${error.message}`);
    } finally {
      button.disabled = false;
    }
  });
});
document.querySelector("#simulateSensor").addEventListener("click", async () => {
  try {
    await fetchJson("/api/sensors/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sensor_type: "fc51_zone_1", state: "active" }),
    });
    await refreshSystemStatus();
  } catch (error) {
    showToast(`Sensor simulation failed: ${error.message}`);
  }
});
setInterval(() => {
  refreshSystemStatus();
  loadEncounters();
}, 5000);
refreshSystemStatus();
loadEncounters();
setInterval(() => {
  document.querySelector("#clock").textContent = `${new Date().toISOString().slice(11, 19)} UTC`;
}, 1000);
document.querySelector("#clock").textContent = `${new Date().toISOString().slice(11, 19)} UTC`;