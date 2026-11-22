# firmware/gate_out - ESP32 cổng ra

Board cổng ra: 2 cảm biến siêu âm HC-SR04 (tại gờ giảm tốc và sau barrier), servo SG90 đóng/mở barrier, LCD I2C 16x2. Nói chuyện với backend qua MQTT theo [docs/CONTRACT.md](../../docs/CONTRACT.md).

Cùng khung code với `gate_in`, chỉ khác topic `gate/out/*` và device id `gate_out`.

## Cấu hình trước khi chạy

Sao chép `include/config.example.h` thành `include/config.h` (file `config.h` đã được `.gitignore` bỏ qua, không commit), rồi sửa:

- `WIFI_SSID`, `WIFI_PASSWORD` — WiFi 2.4 GHz của địa điểm lắp model.
- `MQTT_HOST` — **IP LAN của máy chạy `docker compose up -d`** (xem bằng `ipconfig`), ví dụ `192.168.1.5`. Không dùng `mosquitto`.
- Chân GPIO (`PIN_*`), góc servo, địa chỉ I2C của LCD — chỉnh theo sơ đồ do người kiểm tra duyệt.

> ECHO của HC-SR04 trả 5V, phải qua cầu phân áp về 3.3V trước khi vào ESP32. Servo SG90 cần nguồn 5V riêng và mass chung.

Chạy broker + backend trước: từ thư mục gốc `docker compose up -d`.

## Chạy

```bash
cd firmware/gate_out
pio run                 # build
pio run -t upload       # nạp vào board
pio device monitor      # baud 115200
```

Kết quả mong đợi khi lên board:

```
=== gate_out: ESP32 cổng ra ===
[wifi] IP: 192.168.1.51
[mqtt] đã kết nối, đang nghe parking/gate/out/cmd
[servo] barrier close
```

## Test

**1. Xem toàn bộ bản tin trên broker:**

```bash
docker compose exec mosquitto mosquitto_sub -h localhost -t 'parking/#' -v
```

Khi có xe tới gờ giảm tốc cổng ra:

```
parking/device/gate_out/status online
parking/gate/out/event  {"type":"vehicle_detected","distance_cm":34.6,"ts":1730000000}
parking/gate/out/passed {"type":"vehicle_passed","ts":1730000003}
```

**2. Kiểm tra backend nhận được:**

```bash
curl http://localhost:8000/api/health
curl "http://localhost:8000/api/events?limit=10"
```

**3. Thử lệnh barrier + LCD:**

```bash
docker compose exec mosquitto mosquitto_pub -h localhost -t 'parking/gate/out/cmd' \
  -m '{"barrier":"open","lcd":["29B-12345","Tam biet"]}'
```

Board in `[servo] barrier open` và LCD hiện 2 dòng; gửi `{"barrier":"close"}` để đóng lại.

**4. Không có phần cứng?** Simulator từ thư mục gốc publish các topic tương ứng (xem `tools/sim_esp32.py`).

## Ghi chú

- Publish của PubSubClient là QoS 0; backend subscribe QoS 1 vẫn nhận bình thường.
- `ts` chỉ được thêm sau khi đồng bộ NTP; chưa sync thì backend tự gán thời gian.
- Barrier chỉ mở khi backend phát lệnh `gate/out/cmd` (luồng điều phối ở giai đoạn 4).
