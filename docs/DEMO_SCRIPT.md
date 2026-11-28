# Kịch bản demo - Bãi xe thông minh (HUTECH)

> Khung kịch bản. Các chỗ `[...]` là phần nhóm tự điền (số liệu đo thật, tên người,
> thời lượng thực tế). Đừng sửa số liệu đo cho đẹp báo cáo.

## 1. Mục tiêu

- Trình diễn trọn luồng: xe tới gờ giảm tốc → đọc biển số → barrier mở → ghi phiên.
- Chứng minh số liệu đo được: độ trễ từng chặng, độ chính xác ALPR, trạng thái ô đỗ realtime.
- Chứng minh hệ thống chịu lỗi: mất mạng / mất dịch vụ rồi tự phục hồi.
- Thời lượng mục tiêu: **[__] phút** (dự kiến 10 phút) + **[__] phút** Q&A.

## 2. Chuẩn bị

### 2.1 Thiết bị

| Thiết bị | Số lượng | Ghi chú | Người phụ trách |
|---|---|---|---|
| ESP32 cổng vào (`gate_in`) | 1 | đã nạp firmware, có `config.h` | [__] |
| ESP32 cổng ra (`gate_out`) | 1 | | [__] |
| ESP32 khu đỗ (`slots`) | 1 | 4 cảm biến HC-SR04 | [__] |
| Servo barrier + LCD I2C | 2 bộ | nguồn 5V riêng, mass chung | [__] |
| Camera USB | 1 | cắm máy chạy capture-agent | [__] |
| Xe mô hình / vật thay thế | [__] | biển số in rõ, kích thước **[__] cm** | [__] |
| Laptop chạy Docker + capture-agent | 1 | đã `docker compose up -d` | [__] |

### 2.2 Mạng

- Toàn bộ ESP32 và laptop cùng một mạng LAN **2.4 GHz**; SSID **[__]**.
- `MQTT_HOST` trong `firmware/*/include/config.h` = IP LAN của laptop: **[__]**.
- Cổng cần mở: 1883 (MQTT), 8000 (backend), 8001 (ALPR), 8002 (capture-agent), 8080 (portal).
- Dự phòng mạng: hotspot điện thoại, SSID **[__]**, đã lưu trên các board (nếu có).
- Kiểm tra nhanh: `ping <IP laptop>` từ điện thoại cùng mạng.

### 2.3 Ánh sáng và bố trí

- Đèn chính: **[__]**, tránh ngược sáng từ cửa sổ; bật trước khi đo **[__] phút**.
- Biển số cách camera **[__] cm**, vuông góc **[__]°**, không bóng loá.
- Mặt bàn/mô hình: đánh dấu vị trí gờ giảm tốc và vạch dừng bằng băng dính.
- Phương án khi phòng thiếu sáng: **[__]** (đèn ring / đèn bàn / đổi vị trí).

### 2.4 Phần mềm (chạy trước 15 phút)

```bash
cp .env.example .env                 # nếu chưa có
docker compose up -d --build
docker compose ps                    # tất cả phải healthy
python capture-agent/app.py          # cửa sổ riêng, nguồn video/camera
python tools/preflight.py            # phải PASS 5/5
```

- Portal: http://localhost:8080, backend: http://localhost:8000/docs
- Mở sẵn: portal, `docker compose logs -f backend`, terminal đo số liệu.

## 3. Phân vai

| Vai | Người | Việc |
|---|---|---|
| Thuyết trình | [__] | nói mục tiêu, kiến trúc, kết luận |
| Điều khiển | [__] | cho xe qua cổng, rút/khôi phục mạng, gõ lệnh |
| Ghi số liệu & quay | [__] | quay video, chụp màn hình, điền số vào mục 5 |

## 4. Các bước demo

| # | Nội dung | Thời lượng | Người nói |
|---|---|---|---|
| 0 | Mở màn, kiến trúc | 1 phút | [__] |
| 1 | Xe vào + ALPR + barrier | 3 phút | [__] |
| 2 | Xe ra + đóng phiên | 1,5 phút | [__] |
| 3 | Ô đỗ realtime | 1,5 phút | [__] |
| 4 | Mất mạng và khôi phục | 2 phút | [__] |
| 5 | Số liệu đo | 1 phút | [__] |
| 6 | Kết luận, Q&A | [__] | [__] |

### Bước 0 - Mở màn (1 phút)

- Nói: mục tiêu, 3 board ESP32, backend FastAPI, ALPR cục bộ, portal realtime.
- Chiếu sơ đồ trong `README.md` (mục Kiến trúc).
- Trạng thái mong đợi: portal đang xanh "Đã kết nối", 4 ô đỗ hiện trạng thái.

### Bước 1 - Xe vào (3 phút)

1. Đặt xe vào gờ giảm tốc cổng vào.
2. Nói: cảm biến HC-SR04 phát hiện → MQTT `parking/gate/in/event` → backend chụp ảnh →
   ALPR trả biển → barrier mở, LCD hiện biển + số ô trống.
3. Chỉ ra trên portal: phiên mới xuất hiện, trạng thái "Đang gửi".
4. Số liệu cần điền: biển đọc được **[__]**, độ tin cậy **[__] %**, thời gian mở barrier **[__] ms**.

Lệnh kiểm tra (nếu cần chiếu terminal):

```bash
docker compose exec mosquitto mosquitto_sub -h localhost -t 'parking/gate/#' -v
```

### Bước 2 - Xe ra (1,5 phút)

1. Đặt xe vào gờ giảm tốc cổng ra.
2. Nói: backend tìm phiên `inside` theo biển, đóng phiên, cập nhật `exit_time`.
3. Chỉ ra: portal đổi trạng thái "Đã ra"; LCD hiện "Tam biet".
4. Số liệu: thời gian xử lý cổng ra **[__] ms**; tổng thời gian gửi **[__] phút**.

### Bước 3 - Ô đỗ realtime (1,5 phút)

1. Đặt vật vào ô 1 (và ô 3 nếu còn thời gian).
2. Nói: HC-SR04 mỗi 5 giây gửi `parking/slot/<n>/state`, WebSocket đẩy `slot_update`.
3. Chỉ ra: thẻ ô đổi "Có xe"/"Trống" **không cần tải lại trang**, thống kê "Ô trống" giảm.
4. Số liệu: độ trễ hiển thị **[__] s**, số ô đang có xe **[__]/4**.

### Bước 4 - Mất mạng và khôi phục (2 phút)

Chọn **một** kịch bản (hoặc làm lần lượt nếu còn thời gian):

- **4a. Rút WiFi của một board**: rút nguồn/che WiFi cổng vào → broker phát last will
  `parking/device/gate_in/status offline` (retained) → cắm lại → board tự nối lại, phát `online`.
- **4b. Dừng một dịch vụ**: `docker compose stop backend` rồi `docker compose start backend`;
  đo thời gian tới khi healthy và thử lại một lượt xe.

Nói: hệ thống tự phục hồi, không cần thao tác tay; log backend thể hiện reconnect.

Số liệu: thời gian board online lại **[__] s**; thời gian backend healthy **[__] s**;
số lượt rớt ghi nhận **[__]**. (Có thể chạy `python tools/chaos_test.py --services backend`
trước buổi demo để có sẵn bảng.)

### Bước 5 - Số liệu đo (1 phút)

Chạy trước hoặc chiếu kết quả đã lưu:

```bash
python tools/export_latency.py      # bảng p50/p95 theo chặng (gate_traces)
python tools/bench_alpr.py --dir anh-mau   # độ đúng ALPR + nhầm lẫn ký tự
```

| Chỉ số | Giá trị | Ghi chú |
|---|---|---|
| Độ trễ capture (p50) | [__] ms | `/api/traces` |
| Độ trễ ALPR (p50 / p95) | [__] / [__] ms | mục tiêu < 500 ms |
| Độ trễ quyết định + publish | [__] ms | `decide_ms` |
| Độ trễ barrier (cmd → ack) | [__] ms | `barrier_ms` |
| Tổng một lượt (p50) | [__] ms | `total_ms` |
| Tỉ lệ ALPR dưới 500 ms | [__] % | `alpr_summary.csv` |
| Tỉ lệ đọc đúng biển | [__] % | `bench_alpr.py` |
| Thời gian backend không nối MQTT (soak) | [__] s | `soak_summary.csv` |

### Bước 6 - Kết luận ([__] phút)

- Nhắc lại mục tiêu đã đạt, hạn chế đã biết: **[__]**.
- Hướng phát triển: **[__]**.

## 5. Phương án dự phòng

| Tình huống | Dấu hiệu | Xử lý tại chỗ | Phương án 2 |
|---|---|---|---|
| ALPR không đọc được biển | `plate: null` | đổi góc/ánh sáng, đưa biển gần hơn | chiếu video quay sẵn **[tên file]** |
| Camera không lên | `camera_ok=false` | đổi `CAMERA_SOURCE` sang file video | phát video demo **[tên file]** |
| Mất MQTT/broker | portal mất kết nối | `docker compose restart mosquitto`, chờ healthy | chiếu ảnh chụp portal **[tên file]** |
| Backend lỗi | `/api/health` 503 | `docker compose logs backend`, restart | chiếu bảng số liệu đã lưu + ảnh log |
| Một board chết | status `offline` | thay board dự phòng **[__]** | bỏ qua board đó, nói rõ phạm vi |
| Servo/LCD không chạy | không thấy barrier/LCD | kiểm tra nguồn 5V, mass chung | quay cảnh barrier mở bằng tay |
| Hết thời gian | — | bỏ bước 3 hoặc 4b | tổng kết sớm bằng số liệu đã có |

**Vật liệu dự phòng phải có sẵn trên máy chiếu:**

- Video quay sẵn toàn luồng: `[đường dẫn/tên file]`, dài **[__] giây**.
- Video riêng ALPR cận cảnh: `[tên file]`.
- Ảnh chụp màn hình portal (lưới ô đỗ + bảng phiên): `[tên file]`.
- Bảng số liệu đã xuất: `results/latency_summary.csv`, `results/alpr_summary.csv`,
  `results/soak_summary.csv` — in giấy **[__] bản** để phát nếu cần.
- Sơ đồ chân và ảnh mô hình: `docs/images/[__]`.

## 6. Checklist trước giờ demo (T-30 phút)

- [ ] `docker compose ps` — cả 5 dịch vụ `healthy`.
- [ ] `python capture-agent/app.py` — `/health` có `camera_ok: true`.
- [ ] `python tools/preflight.py` — PASS 5/5.
- [ ] Cả 3 board đã cắm nguồn, LCD sáng, barrier đóng đúng vị trí.
- [ ] Đã chạy thử 1 lượt xe vào + 1 lượt xe ra thành công.
- [ ] Portal mở sẵn, đúng màn hình, chữ to vừa máy chiếu.
- [ ] Điện thoại/hotspot dự phòng đã bật; pin laptop **[__] %**.
- [ ] Video/ảnh dự phòng mở sẵn trong thư mục, không cần mạng.
- [ ] Đã điền trước các số liệu đo vào mục 5 (buổi demo chỉ chiếu lại).
