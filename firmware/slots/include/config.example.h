#pragma once
// Cấu hình board khu đỗ (slots) - 4 cảm biến HC-SR04.
// Sao chép file này thành config.h rồi điền giá trị thật (config.h đã được .gitignore bỏ qua).
// Sơ đồ chân, điện áp do người kiểm tra duyệt trước khi lắp model.

// ---- WiFi (chỉ 2.4 GHz) ----
#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

// ---- MQTT ----
// MQTT_HOST = IP LAN của máy chạy `docker compose up -d` (xem bằng ipconfig).
#define MQTT_HOST "192.168.1.5"
#define MQTT_PORT 1883
#define MQTT_PREFIX "parking"
// Theo docs/CONTRACT.md: gate_in | gate_out | slots
#define DEVICE_ID "slots"

// ---- 4 cảm biến siêu âm HC-SR04 ----
#define SLOT_COUNT 4
// TRIG là chân output
#define PIN_TRIG_1 25
#define PIN_TRIG_2 26
#define PIN_TRIG_3 27
#define PIN_TRIG_4 14
// ECHO là chân input-only (34..39), ECHO 5V phải qua cầu phân áp về 3.3V
#define PIN_ECHO_1 34
#define PIN_ECHO_2 35
#define PIN_ECHO_3 32
#define PIN_ECHO_4 33

// Ngưỡng có/không có xe (hysteresis, đơn vị cm)
#define OCCUPIED_CM 20.0f // <= ngưỡng này: có xe
#define FREE_CM 30.0f     // >= ngưỡng này: trống
// Thời gian lấy mẫu mỗi cảm biến (ms). 4 cảm biến đọc lần lượt (round-robin).
#define SAMPLE_MS 200UL

// ---- NTP (để có ts đúng theo hợp đồng) ----
#define NTP_SERVER_1 "pool.ntp.org"
#define NTP_SERVER_2 "time.google.com"
