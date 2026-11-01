# firmware/gate_in - ESP32 cổng vào

Board cổng vào: 2 cảm biến siêu âm HC-SR04 (tại gờ giảm tốc và sau barrier), servo SG90 đóng/mở barrier, LCD I2C 16x2. Nói chuyện với backend qua MQTT theo [docs/CONTRACT.md](../../docs/CONTRACT.md).

## Cấu hình trước khi chạy

Sửa [include/config.h](include/config.h):

- `WIFI_SSID`, `WIFI_PASSWORD` — WiFi của địa điểm lắp model.
- `MQTT_HOST` — **IP LAN của máy chạy `docker compose up -d`** (xem bằng `ipconfig`), ví dụ `192.168.1.5`. Không dùng `mosquitto` (tên service chỉ có ý nghĩa bên trong Docker).
- Chân GPIO (`PIN_*`), góc servo, địa chỉ I2C của LCD — chỉnh theo sơ đồ do người kiểm tra duyệt. Code không giả định sơ đồ dây.

Chạy broker + backend trước: từ thư mục gốc `docker compose up -d`.

## Chạy

```bash
cd firmware/gate_in
pio run                 # build
pio run -t upload       # nạp vào board (chọn cổng: pio device monitor)
pio device monitor      # baud 115200
```

Kết quả mong đợi khi lên board:

```
=== gate_in: ESP32 cổng vào ===
[wifi] IP: 192.168.1.50
[ntp] đã đồng bộ          (hoặc "chưa đồng bộ được, sẽ thử lại khi chạy")
[mqtt] đã kết nối, đang nghe parking/gate/in/cmd
[servo] barrier close
```

## Test

**1. Xem toàn bộ bản tin trên broker** (máy chạy Docker):

```bash
docker compose exec mosquitto mosquitto_sub -h localhost -t 'parking/#' -v
```

Kết quả mong đợi khi có xe tới gờ giảm tốc:

```
parking/device/gate_in/status online          (retained, khi board vừa kết nối)
parking/gate/in/event  {"type":"vehicle_detected","distance_cm":34.6,"ts":1730000000}
parking/gate/in/passed {"type":"vehicle_passed","ts":1730000003}
```

Board mất kết nối đột ngột thì broker tự phát `parking/device/gate_in/status offline` (last will, retained).

**2. Kiểm tra backend nhận được:**

```bash
curl http://localhost:8000/api/health     # {"status":"ok","mqtt_connected":true}
curl http://localhost:8000/api/events?limit=10   # có các bản tin gate/in/...
```

**3. Thử lệnh barrier + LCD** (chưa có code gửi lệnh ở backend, giai đoạn 4):

```bash
docker compose exec mosquitto mosquitto_pub -h localhost -t 'parking/gate/in/cmd' \
  -m '{"barrier":"open","lcd":["29B-12345","Con trong: 3"]}'
```

Board in `[servo] barrier open` và LCD hiện 2 dòng; gửi `{"barrier":"close"}` để đóng lại.

**4. Không có phần cứng?** Dùng simulator thay thế từ thư mục gốc:

```bash
py -m pip install -r tools/requirements.txt
py tools/sim_esp32.py --host localhost --interval 2
```

## Ghi chú

- Publish của PubSubClient là QoS 0 (thư viện không hỗ trợ publish QoS 1); backend subscribe QoS 1 vẫn nhận bình thường.
- `ts` chỉ được thêm vào payload sau khi đồng bộ NTP; khi chưa sync backend tự gán thời gian hiện tại.
- Chưa tự mở barrier khi phát hiện xe: hợp đồng quy định backend phát lệnh qua `gate/in/cmd` (giai đoạn 4 chưa làm).