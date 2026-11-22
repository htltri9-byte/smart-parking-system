# portal - Web Portal (giai đoạn 5)

Trang tĩnh (HTML/CSS/JS, không cần build) hiển thị trạng thái bãi xe theo thời gian thực.
Đọc REST `/api/slots`, `/api/sessions` và nhận cập nhật qua WebSocket `/ws` của backend.
Toàn bộ gọi qua nginx proxy cùng origin nên không cần cấu hình CORS.

## Tính năng

- Lưới ô đỗ realtime (cập nhật khi backend đẩy `slot_update`).
- Thống kê: số ô trống, số xe đang gửi, số phiên đang hiển thị.
- Bảng lịch sử phiên (50 phiên gần nhất).
- Tìm biển số ngay trên trình duyệt (lọc phía client, không thêm endpoint).
- Tự kết nối lại khi mất WebSocket.

## Chạy

Cùng Docker Compose từ thư mục gốc:

```bash
docker compose up -d --build
```

Mở **http://localhost:8080**.

Yêu cầu backend + broker + db đã chạy (`docker compose ps`). Nginx proxy `/api/` và `/ws`
sang service `backend:8000`.

Chạy riêng không Docker (chỉ cần backend ở cổng 8000): mở `portal/site/index.html` sẽ
trỏ `/api` và `/ws` về chính origin của file — không dùng được; khi phát triển nên chạy
qua nginx. Nếu thật sự cần chạy tĩnh, sửa `API_BASE`/`WS_URL` trong `portal/site/app.js`.

## Test

1. Mở http://localhost:8080 — chấm kết nối chuyển xanh "Đã kết nối".
2. Đẩy trạng thái ô đỗ bằng simulator (thư mục gốc):

   ```bash
   py tools/sim_esp32.py --host localhost --interval 2
   ```

   Trên portal, thẻ ô 1..4 đổi "Có xe"/"Trống" mà không cần tải lại trang.

3. Tạo phiên để thấy lịch sử (nếu capture-agent đang chạy):

   ```bash
   docker compose exec mosquitto mosquitto_pub -h localhost \
     -t 'parking/gate/in/event' -m '{"type":"vehicle_detected","distance_cm":30}'
   ```

   Bảng phiên tự cập nhật; gõ vào ô "Tìm biển số…" để lọc.

4. Kiểm tra proxy:

   ```bash
   curl http://localhost:8080/api/health
   curl http://localhost:8080/api/slots
   ```

## Ghi chú

- Không thêm API mới: tìm biển số lọc phía client trên dữ liệu `/api/sessions`.
- `GET /api/stats` dự kiến ở giai đoạn 6; hiện thống kê tính từ `/api/slots` và `/api/sessions`.
- Portal là static; muốn thêm trang chỉ cần thêm file trong `portal/site/`.
