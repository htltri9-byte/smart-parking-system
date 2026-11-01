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
| `parking/slot/<n>/state` | ESP32 khu đỗ | `{"slot":1,"occupied":true,"distance_cm":8.4,"ts":1730000000.0}` (`n` từ 1 đến 4) |
| `parking/device/<id>/status` | ESP32 | `online` hoặc `offline` (retained, dùng last will) |

`<id>` gồm: `gate_in`, `gate_out`, `slots`.

## 2. Bảng CSDL (PostgreSQL)

- `slots(id PK, occupied bool, distance_cm float, updated_at timestamptz)`
- `events(id PK, ts timestamptz, topic text, payload jsonb)`
- `sessions(id PK, plate text, entry_time timestamptz, exit_time timestamptz null, status text, confidence float, entry_image text)`
  (`status`: `inside` hoặc `exited`)
- `plate_reads(id PK, session_id FK, plate_text text, confidence float, ms int, image_path text, created_at timestamptz)`

## 3. API

### Backend (cổng 8000)
- `GET /api/health` trả `{"status":"ok","mqtt_connected":true}`
- `GET /api/slots` trả danh sách `[{"id":1,"occupied":true,"distance_cm":8.4,"updated_at":"..."}]`
- `GET /api/events?limit=50` trả các sự kiện gần nhất
- `GET /api/sessions?limit=50` trả các phiên gửi xe gần nhất
- `WS /ws` đẩy JSON: `{"type":"slot_update","slot":{...}}`, `{"type":"event","topic":"...","payload":{...}}`

### ALPR (cổng 8001), giai đoạn 3
- `POST /recognize` (multipart, trường `image`) trả `{"plate":"29B-12345","confidence":0.91,"ms":320}`

### Capture agent (cổng 8002, chạy trên máy host), giai đoạn 4
- `GET /capture` trả ảnh JPEG chụp từ USB camera
