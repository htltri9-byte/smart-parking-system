# AGENTS.md - quy tắc cho AI agent

Dự án: bãi xe thông minh. Stack: ESP32 (Arduino/PlatformIO), Mosquitto (MQTT),
FastAPI, PostgreSQL, YOLOv8 + EasyOCR/PaddleOCR, Docker Compose, Web Portal.

## Quy tắc
- Luôn đọc `docs/CONTRACT.md` trước khi viết code.
- Không tự đổi tên topic MQTT, schema CSDL hay API. Cần đổi thì hỏi người dùng trước
  và cập nhật `docs/CONTRACT.md` cùng lúc.
- Mỗi lần chỉ sửa module được giao. Không refactor file khác.
- Cấu hình (host, mật khẩu, chân GPIO) để trong `.env` hoặc `config.h`, không hardcode.
- Python: có type hint, log rõ ràng. Mỗi module có README ngắn: cách chạy và cách test.
- Sau khi viết xong, nêu rõ lệnh chạy thử và kết quả mong đợi.
- Phần cứng (sơ đồ chân, điện áp, nguồn servo) do người kiểm tra, không giả định.

## Cấu trúc
backend/ (FastAPI) | alpr/ | capture-agent/ | forecast/ | portal/ | firmware/ | tools/ | docs/
