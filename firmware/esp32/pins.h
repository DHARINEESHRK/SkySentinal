#pragma once

namespace Pins {
constexpr int FC51_ZONES[5] = {32, 33, 34, 35, 36};
constexpr int PIR = 39;
constexpr int BUZZER = 25;
constexpr int RED_LED = 26;
constexpr int GREEN_LED = 27;
}

namespace SensorLogic {
constexpr bool FC51_ACTIVE_HIGH[5] = {false, false, false, false, false};
constexpr bool PIR_ACTIVE_HIGH = true;
constexpr unsigned long DEBOUNCE_MS = 60;
constexpr unsigned long EVENT_COOLDOWN_MS = 500;
}

namespace OutputLogic {
constexpr bool BUZZER_ACTIVE_HIGH = true;
constexpr bool LED_ACTIVE_HIGH = true;
}
