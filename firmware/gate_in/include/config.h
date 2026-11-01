#pragma once
// Cấu hình board cổng vào (gate_in).
// Theo AGENTS.md: host, mật khẩu và chân GPIO nằm ở đây, không hardcode trong code.
// Sơ đồ chân, điện áp và nguồn servo do người kiểm tra duyệt trước khi lắp model.

// ---- WiFi ----
#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

// ---- MQTT ----
// MQTT_HOST phải là IP LAN của máy chạy `docker compose up -d` (xem bằng ipconfig).
// KHÔNG dùng "mosquitto": tên service chỉ phân giải được bên trong mạng Docker.
#define MQTT_HOST "192.168.1.5"
#define MQTT_PORT 1883
#define MQTT_PREFIX "parking"
// Theo docs/CONTRACT.md: gate_in | gate_out | slots
#define DEVICE_ID "gate_in"

// ---- Cảm biến siêu âm HC-SR04 ----
// Cảm biến 1: tại gờ giảm tốc, trước barrier -> gửi gate/in/event
// Cảm biến 2: sau barrier -> gửi gate/in/passed
#define PIN_TRIG_BUMP 25
#define PIN_ECHO_BUMP 26
#define PIN_TRIG_PASSED 27
#define PIN_ECHO_PASSED 14
// Ngưỡng có/không có xe (hysteresis, đơn vị cm)
#define DETECT_ON_CM 40.0f
#define DETECT_OFF_CM 55.0f
// Thời gian giữa hai lần gửi của cùng cảm biến (ms)
#define PUBLISH_COOLDOWN_MS 2000UL
// Thời gian lấy mẫu cảm biến (ms)
#define SAMPLE_MS 200UL

// ---- Barrier (servo SG90) ----
#define PIN_SERVO 13
#define BARRIER_OPEN_DEG 0    // góc khi mở, chỉnh theo model
#define BARRIER_CLOSED_DEG 90 // góc khi đóng, chỉnh theo model
#define SERVO_MIN_US 500
#define SERVO_MAX_US 2400

// ---- LCD I2C 16x2 ----
#define LCD_I2C_ADDR 0x27
#define PIN_I2C_SDA 21
#define PIN_I2C_SCL 22

// ---- NTP (để có ts đúng theo hợp đồng) ----
#define NTP_SERVER_1 "pool.ntp.org"
#define NTP_SERVER_2 "time.google.com"