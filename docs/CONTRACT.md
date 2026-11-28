# CONTRACT - hợp đồng giữa các module

Mọi module (firmware, backend, ALPR, portal) phải tuân theo file này.
Muốn đổi: sửa file này trước, báo cả nhóm.

## 1. Topic MQTT (tiền tố `parking/`)

Thời gian `ts` là Unix epoch (giây, số thực). Payload là JSON UTF-8.

| Topic | Bên gửi | Payload |
|---|---|---|
| `parking/gate/in/event` | ESP32 cổng vào | `{"type":"vehicle_detected","distance_cm":35.2,"ts":1730000000.0}` (cảm biến ở gờ giảm tốc) |
| `parking/gate/in/passed` | ESP32 cổng vào | `{"type":"vehicle_passed","ts":1730000003.0}` (cảm biến sau barrier) |
| `parking/gate/in/cmd` | Backend | `{"barrier":"open","lcd":["29B-12345","Con trong: 3"]}` (`barrier`: `open` hoặc `close`; `lcd`: tối đa 2 dòng, mỗi dòng tối đa 16 ký tự) |
| `parking/gate/out/event` | ESP32 cổng ra | Như `gate/in/event` |
| `parking/gate/out/passed` | ESP32 cổng ra | Như `gate/in/passed` |
| `parking/gate/out/cmd` | Backend | Như `gate/in/cmd` |
| `parking/gate/in/ack` | ESP32 cổng vào | `{"type":"barrier_opened","ts":1730000004.2}` — gửi **sau khi servo đã quay xong** tới góc mở/đóng. `type`: `barrier_opened` hoặc `barrier_closed`; bỏ `ts` nếu chưa đồng bộ NTP |
| `parking/gate/out/ack` | ESP32 cổng ra | Như `gate/in/ack` |
| `parking/slot/<n>/state` | ESP32 khu đỗ | `{"slot":1,"occupied":true,"distance_cm":8.4,"ts":1730000000.0}` (`n` từ 1 đến 4) |
| `parking/device/<id>/status` | ESP32 | `online` hoặc `offline` (retained, dùng last will) |

`<id>` gồm: `gate_in`, `gate_out`, `slots`.

Ghi chú về `.../ack` (thêm ở giai đoạn 7, **không thay đổi topic cũ**):

- Đây là xác nhận **cơ khí**: ESP32 chỉ gửi sau khi servo đã quay xong tới góc đích,
  không gửi ngay lúc nhận lệnh. Nhờ vậy đo được độ trễ từ `cmd` đến lúc barrier mở xong.
- `ack` **không thay thế** `passed`: `passed` là cảm biến sau barrier báo xe đã đi qua,
  `ack` là phản hồi của chính barrier.
- Backend có thể bỏ qua topic này; chưa có endpoint/CSDL nào phụ thuộc vào nó.

## 2. Bảng CSDL (PostgreSQL)

- `slots(id PK, occupied bool, distance_cm float, updated_at timestamptz)`
- `events(id PK, ts timestamptz, topic text, payload jsonb)`
- `sessions(id PK, plate text, entry_time timestamptz, exit_time timestamptz null, status text, confidence float, entry_image text)`
  (`status`: `inside` hoặc `exited`)
- `plate_reads(id PK, session_id FK, plate_text text, confidence float, ms int, image_path text, created_at timestamptz)`
- `gate_traces(id PK, gate text, session_id FK null, t_event_recv timestamptz, t_capture_done timestamptz null, t_alpr_done timestamptz null, t_cmd_published timestamptz null, t_ack_recv timestamptz null, alpr_ms int null, ok bool, note text null)`
  (`gate`: `in` hoặc `out`; thêm ở giai đoạn 7)

Ghi chú về `gate_traces`:

- Mọi mốc thời gian là **đồng hồ của backend**. Khoảng giữa các chặng tính bằng
  `time.perf_counter()` (đơn điệu), rồi quy ra timestamp neo tại `t_event_recv` để lưu.
- **Không** dùng `ts` của ESP32 để tính độ trễ (đồng hồ NTP của board lệch laptop);
  `ts` đó chỉ được ghi vào `note` dưới dạng `esp32_ts=... (tham khảo, không dùng để tính)`.
- `alpr_ms` là thời gian service ALPR tự báo (tham khảo). Các chặng đo bằng đồng hồ
  backend được tính khi đọc: `capture_ms`, `alpr_ms`, `decide_ms`, `barrier_ms`, `total_ms`.
- Ghi vết chạy **bất đồng bộ** trong luồng riêng, không làm chậm luồng cổng.
- `t_ack_recv` lấy từ topic `parking/gate/<in|out>/ack`, ghép với lượt gần nhất của
  cùng cổng. `ok = true` khi đã publish lệnh mở **và** đã nhận ack.

## 3. API

### Backend (cổng 8000)
- `GET /api/health` trả `{"status":"ok","mqtt_connected":true,"db_connected":true}`
  - `db_connected`: kết quả `SELECT 1` trên CSDL. Mất kết nối DB → HTTP **503** với
    `{"status":"error","mqtt_connected":...,"db_connected":false}` (healthcheck của Docker dựa vào đây).
  - `mqtt_connected` chỉ để báo trạng thái, không làm endpoint trả lỗi.
- `GET /api/slots` trả danh sách `[{"id":1,"occupied":true,"distance_cm":8.4,"updated_at":"..."}]`
- `GET /api/events?limit=50` trả các sự kiện gần nhất
- `GET /api/sessions?limit=50` trả các phiên gửi xe gần nhất
- `WS /ws` đẩy JSON: `{"type":"slot_update","slot":{...}}`, `{"type":"event","topic":"...","payload":{...}}`
- `GET /api/traces?limit=100` trả các lượt gần nhất, mỗi dòng kèm chặng tính sẵn:
  ```json
  {"id":1,"gate":"in","session_id":3,"t_event_recv":"...","t_capture_done":"...",
   "t_alpr_done":"...","t_cmd_published":"...","t_ack_recv":"...","ok":true,
   "note":"esp32_ts=1730000000.0 (tham khảo, không dùng để tính)","alpr_service_ms":320,
   "capture_ms":180.2,"alpr_ms":322.5,"decide_ms":41.0,"barrier_ms":305.7,"total_ms":849.4}
  ```
  - `capture_ms` = `t_capture_done - t_event_recv`
  - `alpr_ms` = `t_alpr_done - t_capture_done` (chặng ALPR, khác `alpr_service_ms` do service tự báo)
  - `decide_ms` = `t_cmd_published - t_alpr_done` (xử lý phiên + publish lệnh)
  - `barrier_ms` = `t_ack_recv - t_cmd_published` (**null** nếu chưa có ack)
  - `total_ms` = từ `t_event_recv` tới `t_ack_recv` (chưa có ack thì tới `t_cmd_published`)
  - Mốc nào thiếu thì chặng liên quan trả `null`.
- `GET /api/traces.csv?limit=100` tải CSV (UTF-8 kèm BOM), cột:
  `id,gate,session_id,t_event_recv,t_capture_done,t_alpr_done,t_cmd_published,t_ack_recv,ok,note,alpr_service_ms,capture_ms,alpr_ms,decide_ms,barrier_ms,total_ms`
  (`ok` ghi `1`/`0`; chặng thiếu mốc để trống)

### ALPR (cổng 8001), giai đoạn 3
- `GET /health` trả `{"status":"ok","ready":true}` khi pipeline đã nạp model xong;
  trong lúc warmup trả HTTP **503** với `{"status":"error","ready":false}` (healthcheck Docker).
- `POST /recognize` (multipart, trường `image`) trả
  `{"plate":"29B-12345","confidence":0.91,"valid":true,"ms":320}`
  - `plate`: biển đã chuẩn hoá dạng `XXy-NNNNN`, hoặc **`null`** khi không đọc được biển hợp lệ.
  - `valid`: `true` khi `plate` khớp mẫu biển Việt Nam (ô tô hoặc xe máy), ngược lại `false`.
  - `confidence`: 0–1 (độ tin cậy OCR của lần đọc dùng để ra kết quả; `0.0` nếu không có).
  - `ms`: thời gian xử lý (mili giây, số nguyên).
  - Không đọc được biển **vẫn trả HTTP 200** với `plate: null, valid: false`; chỉ trả `400`
    khi body không phải ảnh (không giải mã được).
  - Thêm `?debug=true` để nhận thêm trường `raw` (chữ OCR thô) — dùng khi gỡ lỗi, không dùng trong luồng thật.

### Capture agent (cổng 8002, chạy trên máy host), giai đoạn 4
- `GET /capture` trả ảnh JPEG chụp từ USB camera
