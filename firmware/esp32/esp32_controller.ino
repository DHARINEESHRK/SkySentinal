#include <Arduino.h>
#include <ArduinoJson.h>
#include <esp_timer.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include "config.h"
#include "pins.h"

struct InputState {
  bool stableState = false;
  bool candidateState = false;
  unsigned long changedAt = 0;
  unsigned long lastEventAt = 0;
};

InputState zoneStates[5];
InputState pirState;
unsigned long lastHeartbeatAt = 0;
unsigned long lastCommandPollAt = 0;
bool outputsReady = false;
bool sensorStatesInitialized = false;
esp_timer_handle_t buzzerOffTimer = nullptr;
String processedCommandIds[16];
size_t nextProcessedCommand = 0;

void setOutput(int pin, bool enabled, bool activeHigh);

void stopBuzzerTimer(void *) {
  setOutput(Pins::BUZZER, false, OutputLogic::BUZZER_ACTIVE_HIGH);
}

void setOutput(int pin, bool enabled, bool activeHigh) {
  digitalWrite(pin, (enabled == activeHigh) ? HIGH : LOW);
}

bool outputEnabled(int pin, bool activeHigh) {
  return (digitalRead(pin) == HIGH) == activeHigh;
}

String makeEventId(const char *sensorType, bool state) {
  return String(DEVICE_ID) + "-" + sensorType + "-" + String(millis()) + "-" +
         (state ? "1" : "0");
}

void safeOutputs() {
  setOutput(Pins::BUZZER, false, OutputLogic::BUZZER_ACTIVE_HIGH);
  setOutput(Pins::RED_LED, false, OutputLogic::LED_ACTIVE_HIGH);
  setOutput(Pins::GREEN_LED, false, OutputLogic::LED_ACTIVE_HIGH);
  if (buzzerOffTimer) esp_timer_stop(buzzerOffTimer);
  outputsReady = false;
}

void setReadyOutputs() {
  setOutput(Pins::BUZZER, false, OutputLogic::BUZZER_ACTIVE_HIGH);
  setOutput(Pins::RED_LED, false, OutputLogic::LED_ACTIVE_HIGH);
  setOutput(Pins::GREEN_LED, true, OutputLogic::LED_ACTIVE_HIGH);
  if (buzzerOffTimer) esp_timer_stop(buzzerOffTimer);
  outputsReady = true;
}

bool postEvent(const String &sensorType, const String &state) {
  if (WiFi.status() != WL_CONNECTED) return false;
  String url = String(SKY_API_BASE_URL) + "/api/sensors/events";
  StaticJsonDocument<512> document;
  document["event_id"] = makeEventId(sensorType.c_str(), state == "active");
  document["sensor_type"] = sensorType;
  document["state"] = state;
  document["device_id"] = DEVICE_ID;
  if (sensorType == "heartbeat") {
    document["self_test_status"] = "software_started_hardware_unverified";
    document["outputs"]["buzzer"] = outputEnabled(Pins::BUZZER, OutputLogic::BUZZER_ACTIVE_HIGH);
    document["outputs"]["red_led"] = outputEnabled(Pins::RED_LED, OutputLogic::LED_ACTIVE_HIGH);
    document["outputs"]["green_led"] = outputEnabled(Pins::GREEN_LED, OutputLogic::LED_ACTIVE_HIGH);
  }
  String body;
  serializeJson(document, body);
  for (int attempt = 0; attempt < 2; ++attempt) {
    if (WiFi.status() != WL_CONNECTED) return false;
    HTTPClient http;
    http.setConnectTimeout(2000);
    http.setTimeout(2500);
    if (!http.begin(url)) continue;
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + SKY_SHARED_TOKEN);
    int response = http.POST(body);
    http.end();
    if (response >= 200 && response < 300) return true;
    if (response >= 400 && response < 500) return false;
    if (attempt == 0) delay(150);
  }
  return false;
}

void reportInput(const String &sensorType, bool active, InputState &input) {
  unsigned long now = millis();
  if (active == input.candidateState) {
    if (active != input.stableState && now - input.changedAt >= SensorLogic::DEBOUNCE_MS &&
        now - input.lastEventAt >= SensorLogic::EVENT_COOLDOWN_MS) {
      input.stableState = active;
      input.lastEventAt = now;
      postEvent(sensorType, active ? "active" : "inactive");
    }
  } else {
    input.candidateState = active;
    input.changedAt = now;
  }
}

bool runCommand(const String &command) {
  if (command == "buzzer_brief") {
    if (!buzzerOffTimer) return false;
    esp_timer_stop(buzzerOffTimer);
    setOutput(Pins::BUZZER, true, OutputLogic::BUZZER_ACTIVE_HIGH);
    if (esp_timer_start_once(buzzerOffTimer, BUZZER_DURATION_MS * 1000ULL) != ESP_OK) {
      setOutput(Pins::BUZZER, false, OutputLogic::BUZZER_ACTIVE_HIGH);
      return false;
    }
  } else if (command == "buzzer_stop") {
    if (buzzerOffTimer) esp_timer_stop(buzzerOffTimer);
    setOutput(Pins::BUZZER, false, OutputLogic::BUZZER_ACTIVE_HIGH);
  } else if (command == "red_led_on") {
    setOutput(Pins::RED_LED, true, OutputLogic::LED_ACTIVE_HIGH);
  } else if (command == "red_led_off") {
    setOutput(Pins::RED_LED, false, OutputLogic::LED_ACTIVE_HIGH);
  } else if (command == "green_led_on") {
    setOutput(Pins::GREEN_LED, true, OutputLogic::LED_ACTIVE_HIGH);
  } else if (command == "green_led_off") {
    setOutput(Pins::GREEN_LED, false, OutputLogic::LED_ACTIVE_HIGH);
  } else if (command == "reset_outputs") {
    if (buzzerOffTimer) esp_timer_stop(buzzerOffTimer);
    setReadyOutputs();
  } else {
    return false;
  }
  return true;
}

void acknowledgeCommand(const String &commandId, const char *status) {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  String url = String(SKY_API_BASE_URL) + "/api/hardware/commands/" + commandId + "/ack";
  http.setConnectTimeout(2000);
  http.setTimeout(2500);
  if (!http.begin(url)) return;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + SKY_SHARED_TOKEN);
  String body = String("{\"status\":\"") + status +
                "\",\"outputs\":{\"buzzer\":" +
                (outputEnabled(Pins::BUZZER, OutputLogic::BUZZER_ACTIVE_HIGH) ? "true" : "false") +
                ",\"red_led\":" + (outputEnabled(Pins::RED_LED, OutputLogic::LED_ACTIVE_HIGH) ? "true" : "false") +
                ",\"green_led\":" + (outputEnabled(Pins::GREEN_LED, OutputLogic::LED_ACTIVE_HIGH) ? "true" : "false") + "}}";
  http.POST(body);
  http.end();
}

void pollCommands() {
  if (WiFi.status() != WL_CONNECTED) return;
  String commandIds[10];
  String commandNames[10];
  int commandCount = 0;
  HTTPClient http;
  String url = String(SKY_API_BASE_URL) + "/api/hardware/commands";
  http.setConnectTimeout(2000);
  http.setTimeout(2500);
  if (!http.begin(url)) return;
  http.addHeader("Authorization", String("Bearer ") + SKY_SHARED_TOKEN);
  int response = http.GET();
  if (response == HTTP_CODE_OK) {
    DynamicJsonDocument document(2048);
    DeserializationError error = deserializeJson(document, http.getString());
    if (!error && document["commands"].is<JsonArray>()) {
      for (JsonObject item : document["commands"].as<JsonArray>()) {
        if (commandCount >= 10) break;
        String id = item["command_id"] | "";
        String command = item["command"] | "";
        if (id.length() && command.length()) {
          commandIds[commandCount] = id;
          commandNames[commandCount] = command;
          ++commandCount;
        }
      }
    }
  }
  http.end();
  for (int index = 0; index < commandCount; ++index) {
    bool alreadyProcessed = false;
    for (String processedId : processedCommandIds) {
      if (processedId == commandIds[index]) {
        alreadyProcessed = true;
        break;
      }
    }
    if (!alreadyProcessed) {
      bool applied = runCommand(commandNames[index]);
      if (applied) {
        processedCommandIds[nextProcessedCommand] = commandIds[index];
        nextProcessedCommand = (nextProcessedCommand + 1) % 16;
      }
      acknowledgeCommand(commandIds[index], applied ? "delivered" : "failed");
      continue;
    }
    acknowledgeCommand(commandIds[index], "delivered");
  }
}

void initializeSensorStates() {
  unsigned long now = millis();
  for (int index = 0; index < 5; ++index) {
    bool raw = digitalRead(Pins::FC51_ZONES[index]) == HIGH;
    bool active = SensorLogic::FC51_ACTIVE_HIGH[index] ? raw : !raw;
    zoneStates[index].stableState = active;
    zoneStates[index].candidateState = active;
    zoneStates[index].changedAt = now;
    postEvent(String("fc51_zone_") + String(index + 1), active ? "active" : "inactive");
  }
  bool pirRaw = digitalRead(Pins::PIR) == HIGH;
  bool pirActive = SensorLogic::PIR_ACTIVE_HIGH ? pirRaw : !pirRaw;
  pirState.stableState = pirActive;
  pirState.candidateState = pirActive;
  pirState.changedAt = now;
  postEvent("pir", pirActive ? "active" : "inactive");
  sensorStatesInitialized = true;
}

void setup() {
  Serial.begin(115200);
  for (int pin : Pins::FC51_ZONES) pinMode(pin, INPUT);
  pinMode(Pins::PIR, INPUT);
  pinMode(Pins::BUZZER, OUTPUT);
  pinMode(Pins::RED_LED, OUTPUT);
  pinMode(Pins::GREEN_LED, OUTPUT);
  safeOutputs();
  esp_timer_create_args_t buzzerTimerArgs = {};
  buzzerTimerArgs.callback = &stopBuzzerTimer;
  buzzerTimerArgs.name = "buzzer-off";
  if (esp_timer_create(&buzzerTimerArgs, &buzzerOffTimer) != ESP_OK) {
    buzzerOffTimer = nullptr;
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long startedAt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startedAt < 15000) {
    delay(250);
  }
  if (WiFi.status() == WL_CONNECTED) {
    setReadyOutputs();
    initializeSensorStates();
  }
}

void loop() {
  unsigned long now = millis();
  for (int index = 0; index < 5; ++index) {
    bool raw = digitalRead(Pins::FC51_ZONES[index]) == HIGH;
    bool active = SensorLogic::FC51_ACTIVE_HIGH[index] ? raw : !raw;
    reportInput(String("fc51_zone_") + String(index + 1), active, zoneStates[index]);
  }
  bool pirRaw = digitalRead(Pins::PIR) == HIGH;
  reportInput("pir", SensorLogic::PIR_ACTIVE_HIGH ? pirRaw : !pirRaw, pirState);

  if (WiFi.status() == WL_CONNECTED) {
    if (!outputsReady) setReadyOutputs();
    if (!sensorStatesInitialized) initializeSensorStates();
    if (now - lastHeartbeatAt >= 15000) {
      postEvent("heartbeat", "online");
      lastHeartbeatAt = now;
    }
    if (now - lastCommandPollAt >= 1000) {
      pollCommands();
      lastCommandPollAt = now;
    }
  } else {
    safeOutputs();
    WiFi.reconnect();
    delay(250);
  }
  delay(20);
}
