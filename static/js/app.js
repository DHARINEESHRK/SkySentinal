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
    ? detections.map((item) => `<div class="detection-item"><span class="detection-label">${escapeHtml(item.label)}</span><span class="detection-percent">${Math.round(item.confidence * 100)}%</span><div class="detection-bar"><span style="width:${Math.round(item.confidence * 100)}%"></span></div></div>`).join("")
    : '<div class="no-detections"><span class="empty-cross">＋</span><span>No detections in current frame</span></div>';
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
      document.querySelector("#frameFps").textContent = `${data.fps} FPS`;
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
  if (notifyServer) fetch("/api/cctv/stop", { method: "POST" }).catch(() => {});
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
setInterval(() => {
  document.querySelector("#clock").textContent = `${new Date().toISOString().slice(11, 19)} UTC`;
}, 1000);
document.querySelector("#clock").textContent = `${new Date().toISOString().slice(11, 19)} UTC`;