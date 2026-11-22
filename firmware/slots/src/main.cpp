/*
 * slots - ESP32 khu đỗ 4 ô của bãi xe thông minh.
 *
 * Tuân theo docs/CONTRACT.md (KHÔNG tự đổi topic):
 *   gửi   parking/slot/<n>/state  {"slot":1,"occupied":true,"distance_cm":8.4,"ts":...}  (n = 1..4)
 *   gửi   parking/device/slots/status  "online" (retained, last will = "offline")
 *
 * 4 cảm biến HC-SR04 được đọc lần lượt (round-robin) để tránh nhiễu chéo.
 * Chỉ publish khi trạng thái ô thay đổi (và publish trạng thái ban đầu sau khi kết nối).
 *
 * Chạy:  pio run -t upload && pio device monitor
 * Test:  docker compose exec mosquitto mosquitto_sub -h localhost -t 'parking/slot/#' -v
 */
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <time.h>

#include "config.h"

// ---- chân cảm biến (thứ tự = số ô 1..4) ----
const int PIN_TRIG[SLOT_COUNT] = {PIN_TRIG_1, PIN_TRIG_2, PIN_TRIG_3, PIN_TRIG_4};
const int PIN_ECHO[SLOT_COUNT] = {PIN_ECHO_1, PIN_ECHO_2, PIN_ECHO_3, PIN_ECHO_4};

// ---- đối tượng ----
WiFiClient net;
PubSubClient mqtt(net);

// ---- trạng thái từng ô ----
struct SlotState {
  bool occupied;
  float distanceCm;
};

SlotState slots[SLOT_COUNT] = {
    {false, 0.0f}, {false, 0.0f}, {false, 0.0f}, {false, 0.0f}};

String topicStatus;

bool ntpSynced = false;
int nextSlot = 0; // round-robin
unsigned long lastSampleMs = 0;
unsigned long lastNetAttemptMs = 0;
unsigned long lastNtpCheckMs = 0;

// ---- thời gian ----
// ts là Unix epoch (giây, số thực). Chưa đồng bộ NTP thì bỏ ts, backend tự gán.
double epochTs() { return (double)time(nullptr); }

void setupTime() {
  configTime(0, 0, NTP_SERVER_1, NTP_SERVER_2); // UTC
  for (int i = 0; i < 50; ++i) {
    if (time(nullptr) > 1600000000L) {
      ntpSynced = true;
      return;
    }
    delay(200);
  }
  Serial.println("[ntp] chưa đồng bộ được, sẽ thử lại khi chạy");
}

void checkNtp() {
  if (!ntpSynced && time(nullptr) > 1600000000L) {
    ntpSynced = true;
    Serial.println("[ntp] đã đồng bộ");
  }
}

// ---- cảm biến siêu âm ----
float readDistanceCm(int trig, int echo) {
  digitalWrite(trig, LOW);
  delayMicroseconds(2);
  digitalWrite(trig, HIGH);
  delayMicroseconds(10);
  digitalWrite(trig, LOW);
  long duration = pulseIn(echo, HIGH, 25000UL); // timeout 25 ms
  if (duration == 0) {
    return -1.0f; // không đọc được
  }
  return duration / 58.0f;
}

// ---- MQTT ----
bool publishJson(const String &topic, JsonDocument &doc) {
  String out;
  serializeJson(doc, out);
  bool ok = mqtt.publish(topic.c_str(), out.c_str());
  if (!ok) {
    Serial.printf("[mqtt] gửi thất bại %s\n", topic.c_str());
  }
  return ok;
}

void publishSlot(int index) {
  String topic = String(MQTT_PREFIX) + "/slot/" + String(index + 1) + "/state";
  JsonDocument doc;
  doc["slot"] = index + 1;
  doc["occupied"] = slots[index].occupied;
  doc["distance_cm"] = roundf(slots[index].distanceCm * 10.0f) / 10.0f;
  if (ntpSynced) {
    doc["ts"] = epochTs();
  }
  publishJson(topic, doc);
  Serial.printf("[slot] %d occupied=%d distance=%.1fcm\n", index + 1,
                slots[index].occupied ? 1 : 0, slots[index].distanceCm);
}

void publishAllSlots() {
  for (int i = 0; i < SLOT_COUNT; ++i) {
    publishSlot(i);
  }
}

void setupMqtt() {
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(30);
}

void connectMqtt() {
  if (mqtt.connected()) {
    return;
  }
  Serial.printf("[mqtt] kết nối %s:%d ...\n", MQTT_HOST, MQTT_PORT);
  // Last will: broker tự phát "offline" retained khi board mất kết nối đột ngột.
  if (mqtt.connect(DEVICE_ID, topicStatus.c_str(), 0, true, "offline")) {
    mqtt.publish(topicStatus.c_str(), "online", true);
    Serial.println("[mqtt] đã kết nối, publish trạng thái " + String(SLOT_COUNT) +
                   " ô");
    publishAllSlots(); // gửi trạng thái ban đầu để backend có dữ liệu
  } else {
    Serial.printf("[mqtt] lỗi rc=%d, thử lại sau 5s\n", mqtt.state());
  }
}

void maintainNetwork(unsigned long now) {
  if (WiFi.status() != WL_CONNECTED) {
    if (now - lastNetAttemptMs >= 10000UL) {
      lastNetAttemptMs = now;
      Serial.println("[wifi] mất kết nối, thử lại");
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
    return;
  }
  if (now - lastNetAttemptMs >= 5000UL) {
    lastNetAttemptMs = now;
    connectMqtt();
  }
}

// ---- đọc 1 ô, cập nhật trạng thái có hysteresis ----
void pollSlot(int index) {
  float distance = readDistanceCm(PIN_TRIG[index], PIN_ECHO[index]);
  if (distance <= 0.0f || distance > 400.0f) {
    return; // lỗi phản hồi, giữ nguyên trạng thái
  }

  slots[index].distanceCm = distance;

  bool newOccupied = slots[index].occupied;
  if (distance <= OCCUPIED_CM) {
    newOccupied = true;
  } else if (distance >= FREE_CM) {
    newOccupied = false;
  }

  if (newOccupied != slots[index].occupied) {
    slots[index].occupied = newOccupied;
    publishSlot(index);
  }
}

// ---- WiFi ----
void setupWifi() {
  Serial.printf("[wifi] kết nối %s", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  for (int i = 0; i < 75 && WiFi.status() != WL_CONNECTED; ++i) {
    delay(200);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[wifi] IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("[wifi] chưa kết nối được, sẽ thử lại khi chạy");
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\n=== slots: ESP32 khu đỗ (%d ô) ===\n", SLOT_COUNT);

  topicStatus = String(MQTT_PREFIX) + "/device/" + DEVICE_ID + "/status";

  for (int i = 0; i < SLOT_COUNT; ++i) {
    pinMode(PIN_TRIG[i], OUTPUT);
    pinMode(PIN_ECHO[i], INPUT);
  }

  setupWifi();
  setupTime();
  setupMqtt();
  connectMqtt();
}

void loop() {
  unsigned long now = millis();

  maintainNetwork(now);
  if (mqtt.connected()) {
    mqtt.loop();
  }

  if (now - lastSampleMs >= SAMPLE_MS) {
    lastSampleMs = now;
    if (WiFi.status() == WL_CONNECTED && mqtt.connected()) {
      pollSlot(nextSlot);
      nextSlot = (nextSlot + 1) % SLOT_COUNT;
    }
  }

  if (now - lastNtpCheckMs >= 30000UL) {
    lastNtpCheckMs = now;
    checkNtp();
  }
}
