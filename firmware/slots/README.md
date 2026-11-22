# firmware/slots - ESP32 khu đỗ 4 ô

Board khu đỗ: 4 cảm biến siêu âm HC-SR04 đo khoảng cách từng ô, publish trạng thái lên MQTT theo [docs/CONTRACT.md](../../docs/CONTRACT.md).

## Cấu hình trước khi chạy

Sao chép `include/config.example.h` thành `include/config.h` (file `config.h` đã được `.gitignore` bỏ qua, không commit), rồi sửa:

- `WIFI_SSID`, `WIFI_PASSWORD` — WiFi 2.4 GHz của địa điểm lắp model.
- `MQTT_HOST` — **IP LAN của máy chạy `docker compose up -d`** (xem bằng `ipconfig`). Không dùng `mosquitto`.
- `PIN_TRIG_*` / `PIN_ECHO_*` — theo sơ đồ dây thật. ECHO nên dùng chân input-only (34, 35, 32, 33) và **qua cầu phân áp về 3.3V** (HC-SR04 trả 5V).
- `OCCUPIED_CM` / `FREE_CM` — ngưỡng có xe / trống, chỉnh theo chiều cao lắp cảm biến.

> 4 cảm biến được đọc lần lượt (round-robin, mỗi 200 ms một ô) để tránh nhiễu chéo giữa các HC-SR04.

Chạy broker + backend trước: từ thư mục gốc `docker compose up -d`.

## Chạy

```bash
cd firmware/slots
pio run                 # build
pio run -t upload       # nạp vào board
pio device monitor      # baud 115200
```

Kết quả mong đợi khi lên board:

```
=== slots: ESP32 khu đỗ (4 ô) ===
[wifi] IP: 192.168.1.52
[mqtt] đã kết nối, publish trạng thái 4 ô
[slot] 1 occupied=0 distance=120.0cm
[slot] 2 occupied=0 distance=118.5cm
[slot] 3 occupied=0 distance=125.0cm
[slot] 4 occupied=0 distance=121.0cm
```

## Test

**1. Xem trạng thái các ô trên broker:**

```bash
docker compose exec mosquitto mosquitto_sub -h localhost -t 'parking/slot/#' -v
docker compose exec mosquitto mosquitto_sub -h localhost -t 'parking/device/slots/status' -v
```

Đưa tay/vật vào ô 1 (dưới 20 cm) → thấy:

```
parking/slot/1/state {"slot":1,"occupied":true,"distance_cm":8.4,"ts":1730000000}
```

Rút vật ra (trên 30 cm) → `occupied:false`.

**2. Kiểm tra backend cập nhật ô đỗ:**

```bash
curl http://localhost:8000/api/slots
```

Trạng thái ô tương ứng phải đổi theo vật trước cảm biến. Portal nhận `slot_update` qua WebSocket `/ws`.

**3. Kiểm tra last will:**

Rút nguồn board → broker tự phát `parking/device/slots/status offline` (retained).

**4. Không có phần cứng?** Dùng simulator từ thư mục gốc: `py tools/sim_esp32.py --host localhost --interval 2`.

## Ghi chú

- Chỉ publish khi trạng thái ô **thay đổi** (có hysteresis `OCCUPIED_CM` < `FREE_CM`) và publish trạng thái ban đầu sau khi kết nối MQTT.
- Publish của PubSubClient là QoS 0; backend subscribe QoS 1 vẫn nhận bình thường.
- `ts` chỉ được thêm sau khi đồng bộ NTP; chưa sync thì backend tự gán thời gian.
