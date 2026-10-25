from __future__ import annotations

import base64
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template, request
from ultralytics import YOLO

ROOT = Path(__file__).resolve().parent
MODEL_PATHS = {"best.pt": ROOT / "best.pt", "best1.pt": ROOT / "best1.pt"}
IMAGE_SIZE = 640
CONFIDENCE = 0.25

app = Flask(__name__)
models: dict[str, YOLO] = {}
model_lock = threading.Lock()
inference_lock = threading.Lock()
cctv_lock = threading.Lock()
cctv_state: dict[str, Any] = {
    "active": False,
    "source": None,
    "frame": None,
    "detections": [],
    "winner": None,
    "error": None,
    "fps": 0.0,
}


def get_models() -> dict[str, YOLO]:
    if models:
        return models
    with model_lock:
        if not models:
            missing = [str(path) for path in MODEL_PATHS.values() if not path.is_file()]
            if missing:
                raise FileNotFoundError("Missing model weights: " + ", ".join(missing))
            models.update({name: YOLO(str(path)) for name, path in MODEL_PATHS.items()})
    return models


def compare_frame(frame: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    scores: dict[str, float] = {}
    predictions: dict[str, Any] = {}
    with inference_lock:
        loaded_models = get_models()
        for name, model in loaded_models.items():
            result = model.predict(source=frame, imgsz=IMAGE_SIZE, conf=CONFIDENCE, verbose=False)[0]
            predictions[name] = result
            scores[name] = max((float(box.conf[0]) for box in result.boxes), default=0.0)

    winner_name = max(scores, key=scores.get)
    chosen_result = predictions[winner_name]
    detections = []
    for box in chosen_result.boxes:
        class_id = int(box.cls[0])
        detections.append({
            "label": chosen_result.names[class_id],
            "confidence": float(box.conf[0]),
            "box": [round(float(value), 1) for value in box.xyxy[0].tolist()],
        })
    return chosen_result.plot(), {
        "winner": winner_name,
        "confidence": scores[winner_name],
        "scores": scores,
        "detections": detections,
    }


def cctv_worker(source: str) -> None:
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        with cctv_lock:
            cctv_state.update(active=False, error="Could not open this CCTV stream.")
        capture.release()
        return

    previous_time = time.monotonic()
    try:
        while True:
            with cctv_lock:
                if not cctv_state["active"] or cctv_state["source"] != source:
                    break
            ok, frame = capture.read()
            if not ok:
                with cctv_lock:
                    cctv_state.update(active=False, error="CCTV stream stopped sending frames.")
                break
            try:
                annotated, metadata = compare_frame(frame)
                now = time.monotonic()
                fps = 1 / max(now - previous_time, 1e-6)
                previous_time = now
                cv2.putText(annotated, f"SKYSENTINEL  /  {fps:.1f} FPS", (18, 34),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 235, 175), 2, cv2.LINE_AA)
                ok, encoded = cv2.imencode(".jpg", annotated)
                if not ok:
                    continue
                with cctv_lock:
                    cctv_state.update(frame=encoded.tobytes(), detections=metadata["detections"],
                                      winner=metadata["winner"], scores=metadata["scores"], fps=fps, error=None)
            except Exception as error:
                with cctv_lock:
                    cctv_state.update(active=False, error=str(error))
                break
    finally:
        capture.release()
        with cctv_lock:
            cctv_state["active"] = False


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/api/status")
def status():
    return jsonify(ready=True, models=list(MODEL_PATHS))


@app.post("/api/detect")
def detect():
    payload = request.get_json(silent=True) or {}
    image_data = payload.get("image", "")
    if not isinstance(image_data, str) or "," not in image_data:
        return jsonify(error="Expected a base64-encoded camera frame."), 400
    try:
        raw_image = base64.b64decode(image_data.split(",", 1)[1], validate=True)
        frame = cv2.imdecode(np.frombuffer(raw_image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return jsonify(error="Could not decode the camera frame."), 400
        annotated, metadata = compare_frame(frame)
        ok, encoded = cv2.imencode(".jpg", annotated)
        if not ok:
            raise RuntimeError("Could not encode the annotated frame")
        metadata["image"] = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
        return jsonify(metadata)
    except Exception as error:
        app.logger.exception("Frame inference failed")
        return jsonify(error=str(error)), 500


@app.post("/api/cctv/start")
def start_cctv():
    payload = request.get_json(silent=True) or {}
    source = str(payload.get("source", "")).strip()
    parsed = urlparse(source)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        return jsonify(error="Enter an authorized RTSP or RTSPS camera stream URL."), 400
    with cctv_lock:
        cctv_state.update(active=True, source=source, frame=None, detections=[], winner=None,
                          error=None, fps=0.0, scores={})
    threading.Thread(target=cctv_worker, args=(source,), daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/cctv/stop")
def stop_cctv():
    with cctv_lock:
        cctv_state.update(active=False, source=None, frame=None, detections=[], winner=None)
    return jsonify(ok=True)


@app.get("/api/cctv/status")
def cctv_status():
    with cctv_lock:
        return jsonify(active=cctv_state["active"], detections=cctv_state["detections"],
                       winner=cctv_state["winner"], scores=cctv_state.get("scores", {}),
                       error=cctv_state["error"], fps=round(cctv_state["fps"], 1),
                       has_frame=cctv_state["frame"] is not None)


@app.get("/api/cctv/stream")
def cctv_stream():
    def generate():
        while True:
            with cctv_lock:
                frame = cctv_state["frame"]
                active = cctv_state["active"]
            if frame is not None:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            elif not active:
                break
            time.sleep(0.04)
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), threaded=True)