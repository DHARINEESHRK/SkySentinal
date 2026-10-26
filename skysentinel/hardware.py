from __future__ import annotations

import hmac
import re
from datetime import datetime
from typing import Any

ALLOWED_SENSOR_TYPES = {*(f"fc51_zone_{index}" for index in range(1, 6)), "pir", "heartbeat"}
ALLOWED_SENSOR_STATES = {"active", "inactive", "online", "offline"}
ALLOWED_COMMANDS = {
    "buzzer_brief",
    "buzzer_stop",
    "red_led_on",
    "red_led_off",
    "green_led_on",
    "green_led_off",
    "reset_outputs",
}
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_sensor_event(payload: Any) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Expected a JSON object."
    event_id = payload.get("event_id")
    sensor_type = payload.get("sensor_type")
    state = payload.get("state")
    device_id = payload.get("device_id", "esp32-devkit-v1")
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 80:
        return None, "event_id must be a non-empty string up to 80 characters."
    if sensor_type not in ALLOWED_SENSOR_TYPES:
        return None, "sensor_type is not an allowed configured sensor."
    if state not in ALLOWED_SENSOR_STATES:
        return None, "state must be active, inactive, online, or offline."
    if not isinstance(device_id, str) or not DEVICE_ID_PATTERN.fullmatch(device_id):
        return None, "device_id contains unsupported characters."
    device_timestamp = payload.get("device_timestamp")
    if device_timestamp is not None:
        if not isinstance(device_timestamp, str):
            return None, "device_timestamp must be an ISO-8601 string."
        try:
            datetime.fromisoformat(device_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None, "device_timestamp must be a valid ISO-8601 timestamp."
    normalized = {
        "event_id": event_id,
        "sensor_type": sensor_type,
        "state": state,
        "device_id": device_id,
        "device_timestamp": device_timestamp,
    }
    self_test_status = payload.get("self_test_status")
    if self_test_status is not None:
        normalized["self_test_status"] = self_test_status
    encounter_id = payload.get("encounter_id")
    if encounter_id is not None:
        if not isinstance(encounter_id, str) or len(encounter_id) > 80:
            return None, "encounter_id must be a string up to 80 characters."
        normalized["encounter_id"] = encounter_id
    outputs = payload.get("outputs")
    if outputs is not None:
        if sensor_type != "heartbeat" or not isinstance(outputs, dict) \
                or set(outputs) != {"buzzer", "red_led", "green_led"} \
                or any(not isinstance(value, bool) for value in outputs.values()):
            return None, "outputs must be boolean buzzer/red_led/green_led heartbeat states."
        normalized["outputs"] = outputs
    return normalized, None


def token_matches(provided: str | None, expected: str) -> bool:
    return bool(expected and provided and hmac.compare_digest(provided, expected))


def bearer_token() -> str | None:
    from flask import request
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("X-SkySentinel-Token")
