/*
 * gate_in - ESP32 cổng vào của bãi xe thông minh.
 *
 * Tuân theo docs/CONTRACT.md (KHÔNG tự đổi topic):
 *   gửi   parking/gate/in/event   {"type":"vehicle_detected","distance_cm":35.2,"ts":...}
 *   gửi   parking/gate/in/passed  {"type":"vehicle_passed","ts":...}
 *   nhận  parking/gate/in/cmd     {"barrier":"open|close","lcd":["dong 1","dong 2"]}
 *   gửi   parking/device/gate_in/status  "online" (retained, last will = "offline")
 *
 * Chạy:  pio run -t upload && pio device monitor
 * Test:  docker compose exec mosquitto mosquitto_sub -h localhost -t 'parking/#' -v
 */
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <LiquidCrystal_I2C.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <Wire.h>
#include <time.h>

#include "config.h"

// ---- đối tượng ----
WiFiClient net;
PubSubClient mqtt(net);
Servo barrierServo;
LiquidCrystal_I2C lcd(LCD_I2C_ADDR, 16, 2);

// ---- topic (xây từ MQTT_PREFIX để khớp với backend) ----
String topicEvent;
String topicPassed;
String topicCmd;
String topicStatus;

// ---- trạng thái ----
struct Sensor {
  int trig;
  int echo;
  bool present; // đang có vật trước cảm biến
  unsigned long lastPublishMs;
};

Sensor bumpSensor = {PIN_TRIG_BUMP, PIN_ECHO_BUMP, false, 0};
Sensor passedSensor = {PIN_TRIG_PASSED, PIN_ECHO_PASSED, false, 0};

bool ntpSynced = false;
unsigned long lastSampleMs = 0;
unsigned long lastNetAttemptMs = 0;
unsigned long lastNtpCheckMs = 0;

// ---- thời gian ----
// ts là Unix epoch (giây, số thực). Nếu chưa đồng bộ NTP thì bỏ ts khỏi payload,
// backend sẽ tự gán thời gian hiện tại (tránh ghi nhầm về 1970).
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

void publishEvent(float distanceCm) {
  JsonDocument doc;
  doc["type"] = "vehicle_detected";
  doc["distance_cm"] = roundf(distanceCm * 10.0f) / 10.0f;
  if (ntpSynced) {
    doc["ts"] = epochTs();
  }
  publishJson(topicEvent, doc);
}

void publishPassed() {
  JsonDocument doc;
  doc["type"] = "vehicle_passed";
  if (ntpSynced) {
    doc["ts"] = epochTs();
  }
  publishJson(topicPassed, doc);
}

void showOnLcd(const String &line1, const String &line2) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line1.substring(0, 16)); // hợp đồng: tối đa 16 ký tự/dòng
  lcd.setCursor(0, 1);
  lcd.print(line2.substring(0, 16));
}

void setBarrier(bool open) {
  barrierServo.write(open ? BARRIER_OPEN_DEG : BARRIER_CLOSED_DEG);
  Serial.printf("[servo] barrier %s\n", open ? "open" : "close");
}

// Xử lý parking/gate/in/cmd: {"barrier":"open","lcd":["29B-12345","Con trong: 3"]}
void handleCmd(const byte *payload, unsigned int length) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) {
    Serial.println("[cmd] JSON không hợp lệ, bỏ qua");
    return;
  }

  const char *barrier = doc["barrier"];
  if (barrier != nullptr) {
    if (strcmp(barrier, "open") == 0) {
      setBarrier(true);
    } else if (strcmp(barrier, "close") == 0) {
      setBarrier(false);
    }
  }

  JsonArray lines = doc["lcd"].as<JsonArray>();
  if (!lines.isNull()) {
    String l1, l2;
    if (lines.size() > 0) {
      l1 = lines[0].as<String>();
    }
    if (lines.size() > 1) {
      l2 = lines[1].as<String>();
    }
    showOnLcd(l1, l2);
    Serial.printf("[lcd] '%s' | '%s'\n", l1.c_str(), l2.c_str());
  }
}

void onMqttMessage(char *topic, byte *payload, unsigned int length) {
  if (topicCmd.equals(topic)) {
    handleCmd(payload, length);
  }
}

void setupMqtt() {
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
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
    mqtt.subscribe(topicCmd.c_str());
    Serial.println("[mqtt] đã kết nối, đang nghe " + topicCmd);
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

// ---- quét cảm biến, gửi bản tin khi có vật xuất hiện ----
void pollSensor(Sensor &s, bool isBumpSensor) {
  float distance = readDistanceCm(s.trig, s.echo);
  if (distance <= 0.0f || distance > 400.0f) {
    return; // lỗi phản hồi, giữ nguyên trạng thái
  }

  if (!s.present && distance <= DETECT_ON_CM) {
    s.present = true;
    unsigned long now = millis();
    if (now - s.lastPublishMs >= PUBLISH_COOLDOWN_MS) {
      s.lastPublishMs = now;
      if (isBumpSensor) {
        publishEvent(distance);
      } else {
        publishPassed();
      }
    }
  } else if (s.present && distance >= DETECT_OFF_CM) {
    s.present = false; // xe đã khuất, sẵn sàng cho lượt kế tiếp
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
  Serial.println("\n=== gate_in: ESP32 cổng vào ===");

  topicEvent = String(MQTT_PREFIX) + "/gate/in/event";
  topicPassed = String(MQTT_PREFIX) + "/gate/in/passed";
  topicCmd = String(MQTT_PREFIX) + "/gate/in/cmd";
  topicStatus = String(MQTT_PREFIX) + "/device/" + DEVICE_ID + "/status";

  pinMode(PIN_TRIG_BUMP, OUTPUT);
  pinMode(PIN_ECHO_BUMP, INPUT);
  pinMode(PIN_TRIG_PASSED, OUTPUT);
  pinMode(PIN_ECHO_PASSED, INPUT);

  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
  lcd.init();
  lcd.backlight();
  showOnLcd("gate_in ready", "cho xe...");

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  barrierServo.setPeriodHertz(50);
  barrierServo.attach(PIN_SERVO, SERVO_MIN_US, SERVO_MAX_US);
  setBarrier(false); // khởi động: barrier đóng

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
      pollSensor(bumpSensor, true);
      pollSensor(passedSensor, false);
    }
  }

  if (now - lastNtpCheckMs >= 30000UL) {
    lastNtpCheckMs = now;
    checkNtp();
  }
}