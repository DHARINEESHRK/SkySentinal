#pragma once

// Copy to config.h (ignored by Git) and replace the placeholders locally.
#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#define SKY_API_BASE_URL "http://192.168.1.10:5000"
#define SKY_SHARED_TOKEN "REPLACE_WITH_A_LONG_RANDOM_TOKEN"
#define DEVICE_ID "esp32-devkit-v1"
#define BUZZER_DURATION_MS 350

#if BUZZER_DURATION_MS < 50 || BUZZER_DURATION_MS > 5000
#error "BUZZER_DURATION_MS must be between 50 and 5000 milliseconds."
#endif
