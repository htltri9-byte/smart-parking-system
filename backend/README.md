# backend

FastAPI nhận bản tin MQTT (theo `../docs/CONTRACT.md`), lưu PostgreSQL, đẩy cập nhật qua WebSocket.

## Chạy cùng Docker Compose (khuyến nghị)
Từ thư mục gốc repo: `cp .env.example .env`, rồi `docker compose up --build`.
Kiểm tra: http://localhost:8000/api/health và http://localhost:8000/docs

## Chạy riêng để phát triển (SQLite, không cần Postgres)
    pip install -r requirements.txt
    MQTT_HOST=localhost uvicorn app.main:app --reload
(`DATABASE_URL` mặc định là SQLite `parking.db`.)

## Phạm vi giai đoạn 1
Đã có: nhận MQTT, lưu `events`, cập nhật `slots`, REST + WebSocket.
Chưa có (giai đoạn 4): luồng cổng (gọi camera, ALPR, mở barrier) và bảng `sessions`/`plate_reads` mới chỉ được tạo, chưa dùng.
