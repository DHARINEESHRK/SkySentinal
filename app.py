from __future__ import annotations

import base64
import logging
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge

from skysentinel.config import Settings, load_settings
from skysentinel.database import Repository, utc_text
from skysentinel.detection import DetectionEngine
from skysentinel.firebase_sync import FirestoreOutboxWorker, initialize_firestore
from skysentinel.hardware import (
    ALLOWED_COMMANDS,
    bearer_token,
    token_matches,
    validate_sensor_event,
)
from skysentinel.tracking import IoUTracker

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    repository: Repository | None = None,
    detector: DetectionEngine | None = None,
    start_workers: bool = True,
) -> Flask:
    configuration = settings or load_settings()
    store = repository or Repository(
        configuration.database_path,
        high_risk_threshold=configuration.high_risk_threshold,
        alert_cooldown_seconds=configuration.alert_cooldown_seconds,
        risk_config=configuration.risk_config,
    )
    engine = detector or DetectionEngine(configuration.models, configuration.iou_threshold)
    firebase_client, firebase_status = initialize_firestore(configuration)
    sync_worker = FirestoreOutboxWorker(
        store, firebase_client, configuration.firebase_poll_seconds
    )
    sync_worker.status = firebase_status

    application = Flask(__name__)
    application.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
    application.extensions["skysentinel_repository"] = store
    application.extensions["skysentinel_detector"] = engine
    application.extensions["skysentinel_sync_worker"] = sync_worker
    application.extensions["skysentinel_settings"] = configuration

    cctv_lock = threading.RLock()
    cctv_stop_event = threading.Event()
    tracker_lock = threading.RLock()
    cctv_thread: threading.Thread | None = None
    trackers: dict[str, IoUTracker] = {}
    cctv_state: dict[str, Any] = {
        "active": False,
        "source": None,
        "source_id": None,
        "frame": None,
        "detections": [],
        "winner": None,
        "scores": {},
        "error": None,
        "fps": 0.0,
        "inference_latency_ms": 0.0,
    }

    if start_workers:
        sync_worker.start()

    def shutdown() -> None:
        cctv_stop_event.set()
        sync_worker.stop()
        if cctv_thread and cctv_thread.is_alive():
            cctv_thread.join(timeout=4)

    application.extensions["skysentinel_shutdown"] = shutdown

    @application.errorhandler(RequestEntityTooLarge)
    def handle_large_request(error: RequestEntityTooLarge):
        return jsonify(error="Request body exceeds the 12 MiB limit."), 413

    def process_frame(frame: np.ndarray, source_id: str) -> tuple[np.ndarray, dict]:
        annotated, metadata = engine.infer(frame, configuration.frame_type)
        with tracker_lock:
            tracker = trackers.setdefault(source_id, IoUTracker())
            detections = tracker.update(metadata["detections"])
        for detection in detections:
            x1, y1, x2, y2 = [int(round(value)) for value in detection["box"]]
            cv2.putText(
                annotated,
                f"TRACK {detection['tracking_id']}",
                (x1, min(frame.shape[0] - 5, y2 + 18)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (60, 235, 175),
                1,
                cv2.LINE_AA,
            )
            encounter, _ = store.record_observation(
                source_id,
                detection,
                timeout_seconds=configuration.encounter_timeout_seconds,
                simulate_hardware=configuration.sensor_simulation,
            )
            detection["encounter_id"] = encounter["encounter_id"]
            detection["risk_score"] = encounter["risk_score"]
            detection["risk_level"] = encounter["risk_level"]
        metadata["detections"] = detections
        metadata["inference_latency_ms"] = metadata.get("inference_latency_ms", 0.0)
        return annotated, metadata

    def cctv_worker(source: str, source_id: str) -> None:
        try:
            capture = cv2.VideoCapture(
                source,
                cv2.CAP_FFMPEG,
                [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000,
                ],
            )
        except Exception as error:
            logger.warning("CCTV capture initialization failed (%s)", type(error).__name__)
            with cctv_lock:
                if cctv_state["source_id"] == source_id:
                    cctv_state.update(active=False, error="Could not initialize this CCTV stream.")
            return
        try:
            opened = capture.isOpened()
        except Exception as error:
            logger.warning("CCTV stream status check failed (%s)", type(error).__name__)
            opened = False
        if not opened:
            with cctv_lock:
                if cctv_state["source_id"] == source_id:
                    cctv_state.update(active=False, error="Could not open this CCTV stream.")
            capture.release()
            return

        previous_time = time.monotonic()
        try:
            while True:
                with cctv_lock:
                    if cctv_stop_event.is_set() or not cctv_state["active"] \
                            or cctv_state["source_id"] != source_id:
                        break
                try:
                    ok, frame = capture.read()
                except Exception as error:
                    logger.warning("CCTV frame read failed (%s)", type(error).__name__)
                    with cctv_lock:
                        if cctv_state["source_id"] == source_id:
                            cctv_state.update(
                                active=False, error="CCTV stream failed while reading a frame."
                            )
                    break
                if not ok:
                    with cctv_lock:
                        if cctv_state["source_id"] == source_id:
                            cctv_state.update(
                                active=False, error="CCTV stream stopped sending frames."
                            )
                    break
                try:
                    annotated, metadata = process_frame(frame, source_id)
                    now = time.monotonic()
                    fps = 1 / max(now - previous_time, 1e-6)
                    previous_time = now
                    cv2.putText(
                        annotated,
                        f"SKYSENTINEL  /  {fps:.1f} FPS",
                        (18, 34),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (60, 235, 175),
                        2,
                        cv2.LINE_AA,
                    )
                    ok, encoded = cv2.imencode(".jpg", annotated)
                    if not ok:
                        logger.warning("Could not encode processed CCTV frame.")
                        continue
                    with cctv_lock:
                        cctv_state.update(
                            frame=encoded.tobytes(),
                            detections=metadata["detections"],
                            winner=metadata["winner"],
                            scores=metadata["scores"],
                            fps=fps,
                            inference_latency_ms=metadata["inference_latency_ms"],
                            error=None,
                        )
                except Exception as error:
                    logger.error("CCTV inference failed (%s)", type(error).__name__)
                    with cctv_lock:
                        if cctv_state["source_id"] == source_id:
                            cctv_state.update(active=False, error="CCTV inference failed.")
                    break
        finally:
            capture.release()
            with cctv_lock:
                if cctv_state["source_id"] == source_id:
                    cctv_state["active"] = False
            with tracker_lock:
                trackers.pop(source_id, None)

    def integration_status() -> dict:
        device = store.device_status()
        return {
            "models": engine.model_status(),
            "firebase": {
                "status": sync_worker.status,
                "enabled": configuration.firebase_enabled,
            },
            "cloud_sync": store.outbox_counts(),
            "esp32": device,
            "sensors": store.sensor_status(),
            "sensor_simulation": configuration.sensor_simulation,
            "recent_alerts": store.recent_alerts(10),
            "hardware_commands": store.recent_hardware_commands(5),
        }

    def authorize_device() -> Response | None:
        if not configuration.sensor_token:
            return jsonify(error="ESP32 API is not configured with a shared token."), 503
        if not token_matches(bearer_token(), configuration.sensor_token):
            return jsonify(error="Unauthorized device request."), 401
        return None

    def authorize_remote_control() -> Response | None:
        if request.remote_addr in {"127.0.0.1", "::1", "localhost"}:
            return None
        if not token_matches(bearer_token(), configuration.sensor_token):
            return jsonify(error="Remote hardware control requires device authentication."), 401
        return None

    def validate_shared_token(payload: dict) -> tuple[dict | None, str | None]:
        event, error = validate_sensor_event(payload)
        if error:
            return None, error
        if event.get("self_test_status") is not None:
            if event["sensor_type"] != "heartbeat" or event["self_test_status"] not in {
                "software_started_hardware_unverified",
                "passed",
                "failed",
            }:
                return None, "Invalid self_test_status."
        return event, None

    @application.get("/")
    def index() -> str:
        return render_template("index.html")

    @application.get("/api/status")
    def status():
        return jsonify(ready=True, models=engine.model_status())

    @application.post("/api/detect")
    def detect():
        payload = request.get_json(silent=True)
        image_data = payload.get("image", "") if isinstance(payload, dict) else ""
        if not isinstance(image_data, str) or "," not in image_data:
            return jsonify(error="Expected a base64-encoded camera frame."), 400
        try:
            raw_image = base64.b64decode(image_data.split(",", 1)[1], validate=True)
            frame = cv2.imdecode(np.frombuffer(raw_image, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                return jsonify(error="Could not decode the camera frame."), 400
            annotated, metadata = process_frame(frame, "browser-webcam")
            ok, encoded = cv2.imencode(".jpg", annotated)
            if not ok:
                raise RuntimeError("Image encoding failed")
            metadata["image"] = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
            return jsonify(metadata)
        except FileNotFoundError as error:
            return jsonify(error=str(error)), 503
        except ValueError as error:
            return jsonify(error=str(error)), 503
        except Exception as error:
            logger.error("Frame inference failed (%s)", type(error).__name__)
            return jsonify(error="Frame inference failed."), 500

    @application.post("/api/cctv/start")
    def start_cctv():
        nonlocal cctv_thread
        payload = request.get_json(silent=True)
        source = str(payload.get("source", "")).strip() if isinstance(payload, dict) else ""
        parsed = urlparse(source)
        if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
            return jsonify(error="Enter an authorized RTSP or RTSPS camera stream URL."), 400
        with cctv_lock:
            if cctv_state["active"]:
                return jsonify(error="A CCTV stream is already active."), 409
            source_id = "cctv-" + uuid.uuid4().hex[:12]
            cctv_stop_event.clear()
            cctv_state.update(
                active=True, source=source, source_id=source_id, frame=None,
                detections=[], winner=None,
                error=None, fps=0.0, scores={}, inference_latency_ms=0.0,
            )
        cctv_thread = threading.Thread(
            target=cctv_worker, args=(source, source_id), daemon=True
        )
        cctv_thread.start()
        return jsonify(ok=True, source_id=source_id)

    @application.post("/api/cctv/stop")
    def stop_cctv():
        cctv_stop_event.set()
        with cctv_lock:
            cctv_state.update(active=False, source=None, source_id=None, frame=None,
                              detections=[], winner=None)
        return jsonify(ok=True)

    @application.get("/api/cctv/status")
    def cctv_status():
        with cctv_lock:
            return jsonify(
                active=cctv_state["active"],
                detections=cctv_state["detections"],
                winner=cctv_state["winner"],
                scores=cctv_state["scores"],
                error=cctv_state["error"],
                fps=round(cctv_state["fps"], 1),
                inference_latency_ms=cctv_state["inference_latency_ms"],
                has_frame=cctv_state["frame"] is not None,
            )

    @application.get("/api/cctv/stream")
    def cctv_stream():
        def generate():
            while True:
                with cctv_lock:
                    frame = cctv_state["frame"]
                    active = cctv_state["active"]
                if frame is not None:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                if not active:
                    break
                time.sleep(0.04)
        return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @application.get("/api/dashboard/status")
    @application.get("/api/hardware/status")
    @application.get("/api/sensors/status")
    def dashboard_status():
        return jsonify(integration_status())

    @application.get("/api/encounters")
    def encounters():
        try:
            limit = int(request.args.get("limit", "50"))
            offset = int(request.args.get("offset", "0"))
        except ValueError:
            return jsonify(error="limit and offset must be integers."), 400
        level = request.args.get("risk_level")
        if level and level not in {"low", "medium", "high"}:
            return jsonify(error="risk_level must be low, medium, or high."), 400
        return jsonify(store.list_encounters(
            limit=limit,
            offset=offset,
            risk_level=level,
            source_id=request.args.get("source_id"),
            include_resolved=request.args.get("include_resolved", "true").lower() != "false",
        ))

    @application.get("/api/encounters/export.csv")
    def export_encounters():
        body = store.export_csv(
            risk_level=request.args.get("risk_level"),
            source_id=request.args.get("source_id"),
        )
        return Response(
            body,
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="skysentinel-encounters.csv"'},
        )

    @application.get("/api/encounters/<encounter_id>")
    def encounter_detail(encounter_id: str):
        encounter = store.get_encounter(encounter_id)
        if encounter is None:
            return jsonify(error="Encounter not found."), 404
        return jsonify(encounter=encounter, risk_history=store.risk_history(encounter_id))

    @application.post("/api/encounters/<encounter_id>/acknowledge")
    def acknowledge_encounter(encounter_id: str):
        encounter = store.update_encounter_status(encounter_id, acknowledge=True)
        if encounter is None:
            return jsonify(error="Encounter not found."), 404
        return jsonify(encounter=encounter)

    @application.post("/api/encounters/<encounter_id>/resolve")
    def resolve_encounter(encounter_id: str):
        encounter = store.update_encounter_status(encounter_id, resolve=True)
        if encounter is None:
            return jsonify(error="Encounter not found."), 404
        return jsonify(encounter=encounter)

    @application.get("/api/alerts")
    def alerts():
        try:
            limit = int(request.args.get("limit", "20"))
        except ValueError:
            return jsonify(error="limit must be an integer."), 400
        return jsonify(items=store.recent_alerts(limit))

    @application.post("/api/sensors/events")
    def sensor_event():
        denied = authorize_device()
        if denied:
            return denied
        event, error = validate_shared_token(request.get_json(silent=True))
        if error:
            return jsonify(error=error), 400
        if event.get("encounter_id") and store.get_encounter(event["encounter_id"]) is None:
            return jsonify(error="The correlated encounter does not exist."), 400
        accepted = store.save_sensor_event(event)
        if event["sensor_type"] == "heartbeat":
            states = {
                "startup_self_test": event.get("self_test_status", "not_reported"),
                **({"output_states": event["outputs"]} if event.get("outputs") else {}),
            }
        else:
            states = {event["sensor_type"]: event["state"]}
        store.update_device(event["device_id"], states)
        return jsonify(ok=True, duplicate=not accepted, received_at=utc_text())

    @application.post("/api/sensors/simulate")
    def simulate_sensor_event():
        denied = authorize_remote_control()
        if denied:
            return denied
        if not configuration.sensor_simulation:
            return jsonify(error="Sensor simulation is disabled."), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Expected a JSON object."), 400
        event = {
            "event_id": str(uuid.uuid4()),
            "sensor_type": payload.get("sensor_type"),
            "state": payload.get("state", "active"),
            "device_id": "laptop-simulator",
            "device_timestamp": None,
        }
        normalized, error = validate_sensor_event(event)
        if error:
            return jsonify(error=error), 400
        if normalized["sensor_type"] == "heartbeat":
            return jsonify(error="Simulation endpoint accepts only physical sensor types."), 400
        accepted = store.save_sensor_event(normalized)
        return jsonify(ok=accepted, event=normalized), 201 if accepted else 200

    @application.post("/api/hardware/alerts/test")
    def hardware_alert_test():
        denied = authorize_remote_control()
        if denied:
            return denied
        payload = request.get_json(silent=True)
        command = payload.get("command") if isinstance(payload, dict) else None
        if command not in ALLOWED_COMMANDS:
            return jsonify(error="Unsupported hardware command."), 400
        if command == "buzzer_brief" and store.hardware_command_recent(
            command, configuration.hardware_command_cooldown_seconds
        ):
            return jsonify(error="The buzzer alert cooldown is active."), 429
        simulated = configuration.sensor_simulation
        device = store.device_status()
        command_id = store.enqueue_command(command, simulation=simulated)
        delivered = simulated or device["online"]
        return jsonify(
            ok=True,
            command_id=command_id,
            command=command,
            delivery_status="simulated" if simulated else "pending" if delivered else "offline",
            delivered=simulated,
        ), 202 if not simulated else 200

    @application.get("/api/hardware/commands")
    def hardware_commands():
        denied = authorize_device()
        if denied:
            return denied
        return jsonify(commands=store.pending_commands())

    @application.post("/api/hardware/commands/<command_id>/ack")
    def acknowledge_hardware_command(command_id: str):
        denied = authorize_device()
        if denied:
            return denied
        payload = request.get_json(silent=True)
        status_value = payload.get("status", "delivered") if isinstance(payload, dict) else ""
        if status_value not in {"delivered", "failed"}:
            return jsonify(error="status must be delivered or failed."), 400
        outputs = payload.get("outputs", {}) if isinstance(payload, dict) else {}
        allowed_outputs = {"buzzer", "red_led", "green_led"}
        if not isinstance(outputs, dict) or set(outputs) - allowed_outputs or any(
            not isinstance(value, bool) for value in outputs.values()
        ):
            return jsonify(error="outputs must contain only boolean buzzer/red_led/green_led states."), 400
        if not store.acknowledge_command(command_id, status_value):
            return jsonify(error="Command not found or already acknowledged."), 404
        device = store.device_status()
        if device["device_id"] and outputs:
            store.update_device(device["device_id"], {"output_states": outputs})
        return jsonify(ok=True)

    return application


app = create_app()


if __name__ == "__main__":
    sync_worker = app.extensions["skysentinel_sync_worker"]
    sync_worker.start()
    settings = app.extensions["skysentinel_settings"]
    try:
        app.run(host=settings.host, port=settings.port, threaded=True)
    finally:
        app.extensions["skysentinel_shutdown"]()
