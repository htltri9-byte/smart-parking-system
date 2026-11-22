# backend

FastAPI nhận bản tin MQTT (theo `../docs/CONTRACT.md`), lưu PostgreSQL, đẩy cập nhật qua WebSocket.

## Chạy cùng Docker Compose (khuyến nghị)
Từ thư mục gốc repo: `cp .env.example .env`, rồi `docker compose up --build`.
Kiểm tra: http://localhost:8000/api/health và http://localhost:8000/docs

## Chạy riêng để phát triển (SQLite, không cần Postgres)
    pip install -r requirements.txt
    MQTT_HOST=localhost uvicorn app.main:app --reload
(`DATABASE_URL` mặc định là SQLite `parking.db`.)

## Phạm vi
- Giai đoạn 1: nhận MQTT, lưu `events`, cập nhật `slots`, REST + WebSocket.
- Giai đoạn 4 (`app/gate_flow.py`): luồng cổng — `gate/<in|out>/event` → gọi capture-agent → ALPR → mở barrier + ghi `sessions`/`plate_reads`; `gate/<in|out>/passed` → đóng barrier.

### Cấu hình luồng cổng (biến môi trường)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `CAPTURE_URL` | `http://host.docker.internal:8002/capture` | capture-agent chạy trên máy host |
| `ALPR_URL` | `http://alpr:8001/recognize` | service ALPR trong Docker Compose |
| `GATE_OPEN_SECONDS` | `5` | tự đóng barrier nếu không nhận được `passed` |
| `GATE_COOLDOWN_SECONDS` | `5` | chống mở barrier nhiều lần cho cùng một xe |
| `HTTP_TIMEOUT` | `10` | timeout gọi capture/ALPR (giây) |
| `IMAGE_DIR` | `data/images` | nơi lưu ảnh vào (mount ra `./data/images`) |

### Test luồng cổng

```bash
docker compose up -d --build          # backend + alpr + broker + db
python capture-agent/app.py           # chạy trên host, cổng 8002
```

Giả lập xe vào (khi chưa có ESP32):

```bash
docker compose exec mosquitto mosquitto_pub -h localhost \
  -t 'parking/gate/in/event' -m '{"type":"vehicle_detected","distance_cm":30}'
curl http://localhost:8000/api/sessions      # có phiên status=inside nếu đọc được biển
```

Sau khi test, xem barrier mở/đóng trên cổng MQTT:

```bash
docker compose exec mosquitto mosquitto_sub -h localhost -t 'parking/gate/#' -v
```

Lưu ý: capture-agent phải truy cập được từ container qua `host.docker.internal`; nếu timeout, mở firewall cổng 8002 (xem `capture-agent/README.md`).
