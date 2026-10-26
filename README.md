# SkySentinel

SkySentinel is a local-first aerial drone observation prototype. It combines a Flask dashboard, two existing YOLO11n drone-only weight files, OpenCV video capture, lightweight object tracking, SQLite encounter history and explainable risk scoring. Optional integrations provide Firebase Cloud Firestore synchronization and sensor/alert communication with an ESP32 over a private Wi-Fi network.

The core camera and history features run locally. Firestore and ESP32 integrations are optional; a missing cloud connection or device does not prevent local video analysis and SQLite recording.

> **Important limitations:** This is a prototype operational-priority tool, not a certified aviation, security, safety, or surveillance product. A detection is not proof of identity, intent, or threat. Do not use the risk score as a probability of malicious intent or as the sole basis for consequential decisions. Use only cameras and streams you own or are authorized to access.

## Contents

- [Current implementation and verified model files](#current-implementation-and-verified-model-files)
- [Architecture and data flow](#architecture-and-data-flow)
- [Detection, tracking, and encounters](#detection-tracking-and-encounters)
- [Risk assessment and alerts](#risk-assessment-and-alerts)
- [Requirements and installation](#requirements-and-installation)
- [Run and use the dashboard](#run-and-use-the-dashboard)
- [Standalone webcam utility](#standalone-webcam-utility)
- [Configuration](#configuration)
- [SQLite and Firestore setup](#sqlite-and-firestore-setup)
- [ESP32 hardware and firmware](#esp32-hardware-and-firmware)
- [HTTP API](#http-api)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [Privacy, security, cost, and limitations](#privacy-security-cost-and-limitations)

## Current implementation and verified model files

The dashboard uses the existing root-level files:

| Dashboard role | Path | Inspected metadata | Image capability currently enabled |
| --- | --- | --- | --- |
| Close-range | `best.pt` | YOLO11n detection model; one class named `drone` | Visible RGB |
| Long-range | `best1.pt` | YOLO11n detection model; one class named `drone` | Visible RGB |

The model files report the `yolo11n.yaml` architecture and a single `drone` class. Both are run on eligible frames with independent confidence and image-size settings. The close/long range labels describe the intended application roles; they do not establish validated performance at any distance.

The files under `Model/` were inspected and contain `drone`, `bird`, `airplane`, and `helicopter` classes. They are **not** used by this application's detection pipeline because the configured application is drone-only. The code also filters predictions to class label `drone` and refuses to load configured weights whose names are not exactly the single class `drone`.

The repository metadata does not establish thermal/infrared training or validation. Thermal/IR input is therefore **not enabled or claimed as supported**. Do not route thermal images to these models unless their training data, preprocessing, and performance have been separately verified and the supported image-type configuration is deliberately updated.

## Architecture and data flow

```text
Browser webcam ──JPEG/JSON──┐
                            ├── Flask app ── YOLO11n close + long models
Authorized RTSP/RTSPS ──────┘                    │
                                                 ├── drone-only filter / IoU merge
                                                 ├── per-source IoU tracker
                                                 └── SQLite encounters, alerts, risk history
                                                           │
                              ┌────────────────────────────┴─────────────┐
                              │                                          │
                     Firestore outbox worker                 dashboard/API queries
                              │
                     Cloud Firestore (optional)

ESP32 sensors ──authenticated HTTP JSON──> Flask ──> SQLite sensor events
ESP32 command poll <──fixed allow-list──── Flask <──> dashboard alert controls
```

- The browser obtains webcam permission and sends JPEG frames to the local server. No audio is requested.
- The server opens authorized RTSP/RTSPS URLs with OpenCV and processes the CCTV stream.
- Both enabled models infer sequentially. Their predictions are merged using an IoU-based duplicate suppression threshold; distinct boxes are kept and a duplicate records both model sources. A high score from one model does not discard detections from the other.
- A per-source IoU tracker assigns short-lived tracking IDs. The SQLite service groups repeated observations into UUID encounters using source ID, tracking ID, and an encounter timeout.
- SQLite is the local source of truth. Encounter creation, meaningful risk changes, alerts, and sensor events are queued in a durable Firestore outbox. Cloud calls happen on a background worker, not in the inference path.
- The ESP32 reports sensor state/heartbeat events and polls only fixed buzzer/LED commands. It cannot select arbitrary GPIO pins.

## Detection, tracking, and encounters

The two model configs are defined in `skysentinel/config.py`. Each has its own enabled flag, confidence threshold, inference image size, supported image types, role, path, and source-model metadata. Defaults preserve the actual repository paths `best.pt` and `best1.pt`, set image size to `640`, confidence to `0.25`, and support `visible_rgb` only.

The dashboard returns annotated `drone` boxes, confidence, model source(s), tracker ID, encounter ID, and risk score. The browser webcam client downsizes frames wider than 960 pixels and submits them about every 180 ms; requests do not overlap. CCTV reports approximate processing FPS and inference latency.

The tracker is a lightweight IoU association tracker, not a persistent identity system. IDs can change after missed frames, source restarts, crossings, or occlusion. An encounter's UUID is stable while the same source/tracking pair is observed before the configured timeout (30 seconds by default). Stale encounters remain in history; they are not created anew for each frame.

An encounter contains:

- Class `drone`, tracking ID, camera/source ID, model source(s), UTC first/last seen times, and bounding box.
- Latest and maximum confidence, observation count, approximate duration, and inference-derived risk fields.
- Risk explanation, alert status, acknowledgement/resolution state, optional evidence reference, correlated sensor event IDs, and cloud synchronization status.

Evidence images are not currently saved; evidence references remain empty. The application does not save every frame. Encounter history supports filters, pagination, detail/risk-history views, acknowledgement, resolution, and CSV export.

## Risk assessment and alerts

The risk score is a configurable 0–100 heuristic with these default components:

| Evidence | Points |
| --- | ---: |
| Detection confidence | `20 × confidence`, capped at 20 |
| Encounter persistence | 0 under 2 sec; 5 at 2–under 5; 12 at 5–under 15; 18 at 15–under 30; 25 at 30+ |
| Calibrated proximity | Far 5; medium 15; near 30; unknown 0 |
| Repeated observations | Up to 15 when at least three detections are observed |
| Recent correlated sensor event | 10 for a recent active event explicitly associated with that encounter |

The score is clamped to 0–100. Default bands are low 0–29, medium 30–59, high 60–100. The point values and medium/high band boundaries can be configured with the `SKY_RISK_*` environment variables listed below. `SKY_ALERT_THRESHOLD` independently controls the alert trigger; its default is the same as the default high-risk band boundary. Missing calibration, motion evidence, and sensor correlation are included in the explanation.

No calibrated distance sensor is currently configured, so proximity contributes zero. Bounding-box size is not used as a distance measurement. FC-51 modules and the PIR are nearby obstacle/motion triggers, not long-range aerial detectors or drone classifiers. Sensor inputs never create an AI-confirmed drone detection. Sensor score can contribute only when an authenticated sensor event explicitly names an existing encounter and its active state is recent.

When the score reaches the configured alert threshold, the service records an alert and history entry subject to `SKY_ALERT_COOLDOWN` (300 seconds by default). A severity escalation can create an immediate escalation alert even during that cooldown. A high-risk alert queues fixed `buzzer_brief` and `red_led_on` commands; it does not claim delivery. The buzzer pulse is short and configured in the ESP32's local `BUZZER_DURATION_MS`. The dashboard's manual buzzer request has a separate `SKY_HARDWARE_COMMAND_COOLDOWN`. Commands expire if the device does not collect them promptly. Delivery is reported only after the ESP32 acknowledges the command.

## Requirements and installation

- Python 3.10 or newer and pip.
- The root-level model weights `best.pt` and `best1.pt`.
- A browser webcam, or an authorized and reachable RTSP/RTSPS stream.
- Firebase Admin SDK credentials only if cloud synchronization is wanted.
- For hardware use: the specified ESP32 DevKit V1, verified sensor modules, safe voltage adaptation as required, and Wi-Fi access to the laptop.

Create and activate a virtual environment, then install dependencies:

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If activation is blocked by PowerShell policy, use the virtual environment interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Run and use the dashboard

Start the local server:

```powershell
python app.py
```

Open **http://127.0.0.1:5000** in a browser on the same computer. The server binds to loopback by default. Stop it with `Ctrl+C`.

### Webcam mode

1. Select **Browser webcam** and choose **Start Webcam**.
2. Grant the browser permission to use the camera.
3. The browser sends resized JPEG frames to local inference and displays the annotated response.
4. Select **Stop Webcam** to end capture.

### CCTV mode

1. Select **CCTV / RTSP**.
2. Enter an authorized `rtsp://` or `rtsps://` stream URL.
3. Choose **Connect CCTV**; choose **Disconnect** to stop.

The URL is submitted to the Flask server and held in the process for the connection. It is not intentionally logged or saved. Do not include credentials in screenshots or shared logs.

### Dashboard panels

The dashboard shows model file/config status, current detections and model sources, tracker/encounter history, risk and recent alerts, Firestore/outbox state, ESP32 heartbeat, individual sensor zone states, and reported buzzer/LED outputs. The sensor simulation button appears only when `SKY_SENSOR_SIMULATION=true`. Simulated events are clearly separate from physical device readings and do not count as drone detections.

Encounter history includes a risk-level filter, pagination, risk explanation, acknowledgement/resolution controls, and CSV export. CSV export applies the active filters.

### Standalone webcam utility

The repository also includes `webcam_detect.py`, an independent OpenCV desktop-window script that uses root-level `best.pt`, opens camera index `0`, prints changed detections, and exits when **Q** is pressed in its window:

```powershell
python webcam_detect.py
```

To use another camera, update `CAMERA_INDEX` in that script. It does not use Flask, Firestore, encounter history, CCTV, ESP32, or `best1.pt`.

### Project layout

```text
SkySentinel/
├── app.py
├── webcam_detect.py
├── best.pt
├── best1.pt
├── requirements.txt
├── .env.example
├── skysentinel/
│   ├── config.py
│   ├── database.py
│   ├── detection.py
│   ├── firebase_sync.py
│   ├── hardware.py
│   ├── risk.py
│   └── tracking.py
├── firmware/esp32/
├── templates/index.html
├── static/css/style.css
├── static/js/app.js
└── tests/test_core.py
```

## Configuration

The application reads environment variables. It does **not** automatically load `.env`; copy `.env.example` for reference and set variables in your shell or use a local environment loader. `.env` is ignored by Git.

| Setting | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Flask bind address; keep loopback unless LAN access is deliberately configured. |
| `PORT` | `5000` | Flask port. |
| `SKY_DATABASE_PATH` | `data/skysentinel.sqlite3` | Local SQLite database. |
| `SKY_EVIDENCE_DIR` | `evidence` | Reserved evidence directory; automatic evidence saving is not implemented. |
| `SKY_CLOSE_MODEL` | `best.pt` | Close-range model path. |
| `SKY_CLOSE_MODEL_ENABLED` | `true` | Enable/disable close-range model. |
| `SKY_CLOSE_CONFIDENCE` | `0.25` | Its independent confidence threshold. |
| `SKY_CLOSE_IMAGE_SIZE` | `640` | Its independent inference image size. |
| `SKY_LONG_MODEL` | `best1.pt` | Long-range model path. |
| `SKY_LONG_MODEL_ENABLED` | `true` | Enable/disable long-range model. |
| `SKY_LONG_CONFIDENCE` | `0.25` | Its independent confidence threshold. |
| `SKY_LONG_IMAGE_SIZE` | `640` | Its independent inference image size. |
| `SKY_FRAME_TYPE` | `visible_rgb` | Current capture type; no thermal/IR support is asserted. |
| `SKY_MERGE_IOU` | `0.55` | Duplicate suppression overlap threshold. |
| `SKY_ENCOUNTER_TIMEOUT` | `30` | Seconds to group detections under one encounter. |
| `SKY_ALERT_THRESHOLD` | `60` | Risk score that triggers a high-risk alert. |
| `SKY_ALERT_COOLDOWN` | `300` | Per-encounter alert cooldown in seconds. |
| `SKY_HARDWARE_COMMAND_COOLDOWN` | `3` | Manual buzzer command cooldown in seconds. |
| `SKY_RISK_CONFIDENCE_POINTS` | `20` | Maximum detection-confidence component. |
| `SKY_RISK_PERSISTENCE_UNDER_2`, `..._2_TO_5`, `..._5_TO_15`, `..._15_TO_30`, `..._OVER_30` | `0`, `5`, `12`, `18`, `25` | Persistence points for the five duration buckets (seconds). |
| `SKY_RISK_PROXIMITY_FAR`, `..._MEDIUM`, `..._NEAR` | `5`, `15`, `30` | Proximity points, used only when validated proximity is supplied. |
| `SKY_RISK_MOTION_POINTS` | `15` | Repeated-observation/motion evidence points. |
| `SKY_RISK_SENSOR_POINTS` | `10` | Recent correlated sensor points. |
| `SKY_RISK_MEDIUM_THRESHOLD` | `30` | Lower boundary of the medium band. |
| `SKY_RISK_HIGH_THRESHOLD` | `60` | Lower boundary of the high band. Must be greater than the medium boundary. |
| `SKY_SENSOR_TOKEN` | unset | Required shared token for ESP32 endpoints. |
| `SKY_SENSOR_SIMULATION` | `false` | Enable laptop-only sensor simulation and simulated hardware replies. |
| `SKY_FIREBASE_ENABLED` | `true` | Enable optional Firestore client initialization. |
| `FIREBASE_PROJECT_ID` | unset | Optional Firestore project ID override. |
| `GOOGLE_APPLICATION_CREDENTIALS` | unset | Local Firebase Admin service-account JSON path or ADC configuration. |
| `SKY_FIREBASE_POLL_SECONDS` | `5` | Background outbox polling interval. |

Weights can be redirected for experiments, but loading is refused unless the weights expose exactly the single `drone` class. Keep the supplied model files unchanged unless you intentionally replace them with validated drone-only weights.

## SQLite and Firestore setup

### SQLite

SQLite is the durable local source of truth. The database and containing `data/` directory are ignored by Git. It stores encounters, risk history, alerts, sensor events, device state, hardware commands, and the Firestore outbox.

### Firebase Admin credentials

The Firebase Admin SDK is initialized once per Flask process and reused. Configure credentials locally without placing them in source code:

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\secure\local\service-account.json"
$env:FIREBASE_PROJECT_ID = "your-firebase-project-id"
python app.py
```

The service-account JSON stays on the local machine. The repository's local Firebase Admin key file is not read by application code unless you explicitly configure its path; it is excluded from Git. Never print its private key, copy it into `.env.example`, place it in the frontend, or include it in a commit. Application Default Credentials are also supported when configured by the Google Cloud environment.

Set `SKY_FIREBASE_ENABLED=false` for a fully local run. If the SDK, credentials, or network is unavailable, local detection and SQLite continue; the dashboard reports cloud state. The Firestore SDK may be configured before any network write succeeds, so `configured` is not the same as verified connectivity. Successful writes set `connected`; transient errors show `degraded`.

### Firestore collections and synchronization

The background worker writes stable document IDs to:

- `encounters` — current encounter snapshot with risk, detections, status, and sync metadata.
- `alerts` — alert events, risk band/score, timestamps, acknowledgement/resolution, and delivery status.
- `risk_history` — meaningful risk-score/band changes with components and explanations.
- `sensor_events` — validated sensor payloads, server receipt time, device time where supplied, and optional encounter association.

SQLite records are committed before cloud synchronization. The durable outbox coalesces updates for an encounter document, tracks pending/synced/failed delivery, uses idempotent document IDs, and retries failures with capped exponential delays. It avoids one cloud write per video frame. Firebase operations run on a separate worker thread.

All timestamps are stored as ISO-8601 UTC strings. Current dashboard reads use SQLite; no Firestore query endpoints or composite Firestore queries are implemented, so no composite indexes are currently required. Firestore access should remain restricted by Google Cloud IAM and service-account permissions. Never make these collections publicly readable or writable.

For local integration tests, mock Firestore or configure the official Firestore Emulator with `FIRESTORE_EMULATOR_HOST`; unit tests do not require real project credentials. Real cloud validation may incur project usage/cost.

## ESP32 hardware and firmware

The firmware and wiring procedure are in [firmware/esp32/README.md](./firmware/esp32/README.md). The source is [esp32_controller.ino](./firmware/esp32/esp32_controller.ino), with GPIO definitions in [pins.h](./firmware/esp32/pins.h).

### Inventory

- 1 ESP32 DevKit V1
- 5 FC-51 infrared obstacle/proximity modules
- 1 HC-SR501 PIR motion module
- 1 active buzzer module
- 1 red LED and 1 green LED
- 2 × 220-ohm LED series resistors
- 1 breadboard, jumper wires, USB data cable, laptop and laptop camera

### Proposed GPIO map

| Signal | GPIO |
| --- | ---: |
| FC-51 zones 1–5 digital inputs | 32, 33, 34, 35, 36 |
| PIR digital input | 39 |
| Buzzer logic control | 25 |
| Red LED | 26 |
| Green LED | 27 |

The pin map is intended for a standard ESP32 DevKit V1/WROOM-32. Verify the exact board and module pinouts before wiring. GPIO 34, 35, 36, and 39 are input-only and have no internal pull resistors. FC-51 logic defaults active-low and PIR defaults active-high; both settings are configurable in firmware.

The two 220-ohm resistors are used in series with the LEDs. The inventory does **not** specify a buzzer transistor/driver, base/gate resistor, or optional flyback component. Do not drive a buzzer directly from a GPIO unless its measured/specification-backed current is within GPIO limits; add a suitable driver and any required protection. Do not connect a sensor output that might exceed 3.3 V to an ESP32 GPIO; verify module output levels and add level shifting/division when required. Check sensor supply requirements, common grounds, LED polarity/current, and PIR warm-up/settings.

Firmware sends debounced sensor changes and a 15-second heartbeat to the laptop over authenticated HTTP and polls an allow-list of fixed output commands. If Wi-Fi is lost it sets buzzer/red/green outputs off. The server command queue expires commands that are not collected quickly; the ESP32 reports output states when acknowledging. HTTP is not encrypted, so use a trusted private network and firewall, or protect transport with an appropriate VPN/TLS gateway. Detailed wiring and manual test steps are in the firmware guide.

No physical ESP32, sensor, LED, or buzzer has been connected or tested as part of this implementation. The selected GPIOs, voltage levels, active logic, driver circuit, and physical outputs require board-specific and hands-on validation.

## HTTP API

All routes are served from the local Flask origin. JSON APIs return an `error` field for validation or operation failures.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Dashboard. |
| `GET` | `/api/status` | Configured model status. |
| `POST` | `/api/detect` | Infer one base64 image data URL. |
| `POST` | `/api/cctv/start` | Start authorized RTSP/RTSPS source. |
| `POST` | `/api/cctv/stop` | Stop CCTV. |
| `GET` | `/api/cctv/status` | CCTV activity, detections, model scores, FPS, latency. |
| `GET` | `/api/cctv/stream` | MJPEG processed CCTV frames. |
| `GET` | `/api/dashboard/status` | Models, Firestore/outbox, ESP32/sensors, alerts, command state. |
| `GET` | `/api/encounters` | Filtered, paginated encounter list (`limit`, `offset`, `risk_level`, `source_id`, `include_resolved`). |
| `GET` | `/api/encounters/<id>` | Encounter and risk-history detail. |
| `POST` | `/api/encounters/<id>/acknowledge` | Acknowledge encounter alerts. |
| `POST` | `/api/encounters/<id>/resolve` | Resolve encounter/alerts. |
| `GET` | `/api/encounters/export.csv` | CSV export with optional `risk_level` and `source_id`. |
| `GET` | `/api/alerts` | Recent alerts. |
| `POST` | `/api/sensors/events` | Authenticated ESP32 sensor event/heartbeat. |
| `GET` | `/api/sensors/status` | Device and zone state. |
| `POST` | `/api/sensors/simulate` | Simulated sensor event when simulation is enabled. |
| `GET` | `/api/hardware/status` | Hardware status alias. |
| `POST` | `/api/hardware/alerts/test` | Request a fixed, validated buzzer/LED command. |
| `GET` | `/api/hardware/commands` | Authenticated ESP32 command poll. |
| `POST` | `/api/hardware/commands/<id>/ack` | Authenticated output-state acknowledgement. |

### Detection request and response

`POST /api/detect` accepts:

```json
{
  "image": "data:image/jpeg;base64,<base64-encoded-image>"
}
```

The response contains `detections`, `scores`, `winner`, `inference_latency_ms`, and an annotated JPEG `image` data URL. A detection includes `label`, `confidence` (0–1), `box` coordinates, `model_sources`, `tracking_id`, `encounter_id`, and current risk summary. Invalid/undecodable input returns `400`; missing enabled weights return `503`.

### ESP32 event example

Every ESP32 endpoint requires a `Bearer` token matching `SKY_SENSOR_TOKEN`:

```json
{
  "event_id": "device-unique-event-id",
  "sensor_type": "fc51_zone_1",
  "state": "active",
  "device_id": "esp32-devkit-v1",
  "device_timestamp": "2026-10-26T04:00:00+00:00"
}
```

`device_timestamp` is optional; the server receipt timestamp is authoritative. `encounter_id` is optional and must refer to an existing encounter for its event to be correlated. Repeated `event_id` values are deduplicated. The endpoint does not turn a sensor event into a drone detection.

Hardware control accepts only `buzzer_brief`, `buzzer_stop`, `red_led_on`, `red_led_off`, `green_led_on`, `green_led_off`, and `reset_outputs`. It does not accept arbitrary pin numbers or durations from HTTP. Remote hardware-control requests require the shared token; local loopback dashboard controls are allowed. Device APIs return `401` for an invalid token and `503` if the server has no token configured.

## Tests

Run the automated suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests cover risk boundaries/missing data, duplicate merging, tracking, SQLite encounter history, sensor deduplication/correlation, alert cooldown and command queuing, mocked Firestore success/failure, API token validation, simulation, and missing model handling. They do not require real Firebase credentials, an emulator, a webcam, or hardware.

For a real local Firebase project or physical ESP32, follow the setup above and validate connectivity/outputs separately. No live Firestore write or physical hardware test is implied by passing the unit tests.

## Troubleshooting

### Model weights missing or refused

Place `best.pt` and `best1.pt` beside `app.py`, or update their environment paths. Verify that configured weights contain exactly the `drone` class. The multi-class files under `Model/` are intentionally rejected by the drone-only pipeline.

### Webcam permission or capture fails

Grant camera permission in the browser and OS settings, close other camera applications, and use `http://127.0.0.1:5000` on the laptop. Browser camera APIs require a secure context; localhost is treated as secure by modern browsers.

### RTSP stream cannot open

Verify the authorized stream URL and camera reachability from the laptop. OpenCV/FFmpeg must support the stream. Check CCTV status for errors; credentials embedded in URLs should not be shared.

### Firestore stays unavailable or pending

Install requirements, confirm `GOOGLE_APPLICATION_CREDENTIALS` points to a local key or ADC is configured, and confirm the project ID/IAM permissions. The key file is not read by the UI and never belongs in Git. Check dashboard Firebase/outbox state; local SQLite remains usable when cloud sync is unavailable.

### ESP32 remains offline

Confirm `HOST` is deliberately configured for trusted LAN access, laptop firewall allows the private subnet, `SKY_API_BASE_URL` points to the laptop's private IP, Wi-Fi credentials are correct in local `config.h`, and both sides use the same token. Default loopback binding cannot be reached from the ESP32.

### Inference is slow

Both models run sequentially per frame. Performance depends on model/device/hardware; CCTV FPS is approximate processing throughput and not camera source FPS. Reduce image sizes or disable a model only if that change is appropriate for the intended validated operation.

## Privacy, security, cost, and limitations

- Flask binds to `127.0.0.1` by default. There is no general user login, TLS server, or production hardening. Do not expose the server publicly.
- Browser webcam frames are sent to the local Flask process. CCTV is opened by that process. The application does not intentionally persist frames or credentials.
- Firebase Admin credentials, Wi-Fi credentials, and the shared ESP32 token must stay in local untracked files/environment configuration. The Firebase key filename pattern and firmware `config.h` are ignored by Git; verify staged files before every commit.
- The HTTP device protocol does not encrypt the shared token. Use a trusted isolated LAN plus a host firewall; prefer VPN/TLS transport for less trusted networks.
- Firestore usage can incur cloud cost. The worker batches/coalesces encounter updates and avoids per-frame writes, but alerts, sensor events, risk changes, and updated encounter documents still produce writes.
- The dashboard loads fonts from Google Fonts, so the browser may contact Google for fonts. Inference and SQLite processing remain on the application host.
- Risk values are heuristic, tracker IDs are short-lived, no distance calibration/evidence capture is implemented, and thermal imagery is not validated.
- FC-51/PIR and firmware electrical behavior, board variant, voltage adaptation, buzzer driver, physical alert delivery, and real Firebase connectivity still require separate verification.
