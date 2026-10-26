# ESP32 DevKit V1 firmware

The firmware reads five FC-51 digital outputs and one HC-SR501 PIR output, reports debounced state changes and a heartbeat to the Flask API, and polls for a small fixed set of buzzer/LED commands. Sensors report nearby motion/proximity only; they do not classify or confirm drones.

## Files and prerequisites

- `esp32_controller.ino` — Arduino firmware.
- `pins.h` — GPIO assignments and active-level/debounce configuration.
- `config.example.h` — placeholder-only Wi-Fi and API configuration.
- ESP32 board package for Arduino IDE / Arduino CLI.
- ArduinoJson library.
- USB data cable and a laptop-hosted SkySentinel server reachable over Wi-Fi.

Copy `config.example.h` to `config.h` in this directory and replace the placeholders locally. `config.h` is ignored by Git. Use the same long, random token in `SKY_SENSOR_TOKEN` on the Flask host and `SKY_SHARED_TOKEN` in the local firmware configuration. Never commit the real configuration header, token, or Wi-Fi password.

The firmware uses ordinary HTTP. Use only a trusted, isolated local network; HTTP does not encrypt the shared token. A protected TLS reverse proxy or VPN is preferable when available. Do not port-forward this service or expose it to the public internet.

## Laptop network setup

Flask binds to `127.0.0.1` by default, which is intentionally unreachable from the ESP32. For a trusted private Wi-Fi network only:

1. Set the laptop's `HOST` to its LAN interface (`0.0.0.0` is the usual all-interface binding), and restrict access with the operating-system firewall to the private network.
2. Find the laptop's private IPv4 address and put it in `SKY_API_BASE_URL`, for example `http://192.168.1.10:5000`.
3. Set the identical shared token in the laptop environment and local `config.h`.
4. Ensure the ESP32 and laptop can route to each other on the same trusted network.

PowerShell example:

```powershell
$env:HOST = "0.0.0.0"
$env:SKY_SENSOR_TOKEN = "use-a-long-random-local-token"
python app.py
```

This binding is not a production deployment recipe. The firewall should allow only the intended private subnet and port. The API does not add TLS.

## Pin assignment

This is a conservative proposed map for a standard ESP32 DevKit V1 using an ESP32-WROOM-32 module. Check the exact board silkscreen and datasheet before wiring. Do not use it as proof that a different ESP32 variant exposes the same pins.

| Function | ESP32 GPIO | Notes |
| --- | ---: | --- |
| FC-51 numbered zone 1 digital output | 32 | Input |
| FC-51 numbered zone 2 digital output | 33 | Input |
| FC-51 numbered zone 3 digital output | 34 | Input-only; no internal pull-up/down |
| FC-51 numbered zone 4 digital output | 35 | Input-only; no internal pull-up/down |
| FC-51 numbered zone 5 digital output | 36 | Input-only; no internal pull-up/down |
| HC-SR501 PIR output | 39 | Input-only; no internal pull-up/down |
| Active buzzer control | 25 | Logic control to an appropriately rated driver, not an assumed direct load |
| Red LED | 26 | Series resistor required |
| Green LED | 27 | Series resistor required |
| Common reference | GND | ESP32, sensors, and driver need a common ground where the circuit requires it |

These GPIOs avoid the common ESP32 boot-strapping pins. GPIO 34, 35, 36, and 39 are input-only and have no internal pull resistors. The firmware defaults each FC-51 input to active-low, PIR to active-high, and LED/buzzer outputs to active-high; change the per-zone `FC51_ACTIVE_HIGH` array, `PIR_ACTIVE_HIGH`, or `OutputLogic` in `pins.h` only after observing and validating the particular modules. The five FC-51 labels mean numbered zones; they do not claim calibrated directions or distances.

## Wiring guide

| Device / signal | Connect to | Electrical checks before powering |
| --- | --- | --- |
| Each FC-51 `DO` | GPIO 32, 33, 34, 35, or 36 (one zone per input) | Identify each board's `DO`, `VCC`, and `GND` from its own marking/datasheet. Measure or verify its output high level. ESP32 GPIO is **not 5-V tolerant**; add a suitable level shifter/divider if output can exceed 3.3 V. GPIO 34/35/36 need a valid externally driven logic level. |
| Each FC-51 `VCC` | A supply permitted by that module | Do not assume all FC-51 boards share a supply range or output circuit. Power rails are not interchangeable; check the exact module. |
| Each FC-51 `GND` | Common circuit ground | Use the ground reference required for the digital outputs. |
| PIR `OUT` | GPIO 39 | Verify the exact HC-SR501 board's output voltage before connection; add level adaptation if it can exceed 3.3 V. |
| PIR `VCC` / `GND` | Supply allowed by the module / common ground | Many PIR boards need warm-up time. Check its manufacturer information, allow stabilization, and verify jumper/trigger settings. |
| GPIO 26 → 220-ohm resistor → red LED anode; LED cathode → GND | Red alert indicator | Keep the resistor in series. Check LED polarity, forward voltage, resistor rating, and current. |
| GPIO 27 → 220-ohm resistor → green LED anode; LED cathode → GND | Ready indicator | Same series-resistor checks as the red LED. |
| GPIO 25 | Input of a suitable transistor/MOSFET driver | The inventory has no specified driver or base/gate resistor. Add appropriately rated components; do not drive a buzzer directly until its voltage and current are verified within the ESP32 GPIO limits. Add a flyback diode if required by the actual load. |
| Buzzer supply and return | Rated supply and driver output | Confirm active-buzzer voltage/current and polarity. Join grounds as required by the driver circuit. |
| ESP32 USB | Laptop USB data port | Use a known data-capable cable. Avoid powering a sensor rail from the ESP32 3.3-V pin unless the board and total load are confirmed within ratings. |

Do not connect any sensor output to the ESP32 until its output voltage is verified. If a reading is unknown, stop and obtain the module specification or use a proper level-shifting interface first. The breadboard and jumper wires do not provide over-voltage protection.

## Message protocol

- Sensor events: `POST /api/sensors/events`, JSON fields `event_id`, `sensor_type`, `state`, and `device_id`; optional UTC ISO-8601 `device_timestamp` and an explicitly assigned `encounter_id` can be included.
- `sensor_type` is one of `fc51_zone_1` through `fc51_zone_5`, `pir`, or `heartbeat`.
- `state` is `active`/`inactive` for sensors and `online` for a heartbeat. The server records its own UTC receipt time and deduplicates event IDs.
- Heartbeat every 15 seconds updates the server-side online state. The firmware reports `software_started_hardware_unverified`; this does not mean physical sensors passed an electrical self-test.
- Event submission uses two bounded attempts with the same event ID so server-side deduplication protects against a retry after a lost response. Sensor polling can pause briefly while the bounded HTTP timeout/retry runs.
- Hardware command polling: `GET /api/hardware/commands`; the ESP32 acknowledges an allow-listed command at `/api/hardware/commands/<command_id>/ack` and includes boolean buzzer/red/green output states.
- Every ESP32 endpoint requires `Authorization: Bearer <shared-token>`. Do not put the token in a URL, log it, or display it in the dashboard.
- A lost Wi-Fi connection returns outputs to buzzer off, red off, green off. The buzzer also has a short configured pulse duration (`BUZZER_DURATION_MS` in local `config.h`).
- The firmware caches recently processed command IDs in RAM so a repeated poll/ack does not replay the same buzzer pulse during one boot session.
- Commands expire if not collected promptly; server-side delivery remains pending until the device acknowledges them. An HTTP request accepted by Flask is not proof a physical output changed.

The server validates the sensor ID and payload, but a sensor event is not a drone detection. Sensor corroboration contributes to risk only when the incoming event explicitly names an existing encounter and is recent; proximity distance is not inferred from these sensors.

## Upload and manual test

1. Install the Arduino ESP32 board support package and ArduinoJson.
2. Make a local `config.h` and set Wi-Fi SSID/password, laptop URL, shared token, and buzzer pulse length.
3. Inspect the board and modules, build the breadboard circuit with power disconnected, verify common grounds and all GPIO input voltages, and use a current-limited supply where practical.
4. Connect the ESP32 by USB, select the exact board and serial port in Arduino IDE, compile/upload `esp32_controller.ino`, and open the serial monitor at 115200 baud.
5. Start SkySentinel with LAN binding/firewall rules configured. Confirm `/api/hardware/status` reports the ESP32 online and initial zone states.
6. Trigger each sensor individually and confirm its numbered zone/PIR state updates without producing an AI-confirmed drone encounter.
7. Send a `buzzer_brief`, LED, and `reset_outputs` command from the dashboard. Confirm the API shows pending until acknowledgement, then compare the ESP32-reported output states with the actual circuit.
8. Disconnect Wi-Fi and confirm buzzer/LED outputs return to the safe state; reconnect and verify the heartbeat resumes.

The code has not been physically tested with the user's board or modules. Board revisions, sensor logic polarity, output voltage, PIR warm-up, buzzer current, and LED behavior all require hands-on validation.
