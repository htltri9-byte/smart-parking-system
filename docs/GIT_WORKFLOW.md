# Quy trình làm việc với Git

## Nhánh
- `main`: luôn chạy được, chỉ gộp code đã thử.
- Mỗi việc một nhánh: `feature/<tên-việc>`, ví dụ `feature/firmware-gate-in`, `feature/backend-core`, `feature/alpr-service`.
- Sửa lỗi: `fix/<tên-lỗi>`.

## Vòng làm việc hằng ngày
```
git switch main
git pull                          # lấy code mới nhất
git switch -c feature/ten-viec    # tạo nhánh cho việc mới
# ... sửa code, chạy thử ...
git add -A
git commit -m "feat(firmware): doc HC-SR04 va gui MQTT"
git push -u origin feature/ten-viec
```
Sau đó mở Pull Request trên GitHub, để người kia xem rồi bấm Merge. Gộp xong thì `git switch main && git pull`.

## Quy ước commit (ngắn gọn)
`<loại>(<module>): <mô tả>`. Loại: `feat` (tính năng), `fix` (sửa lỗi), `docs`, `chore`.
Ví dụ: `feat(backend): luu su kien MQTT vao PostgreSQL`.

## Nên làm
- Một việc một commit nhỏ, chạy thử rồi mới commit.
- Đổi topic MQTT, schema CSDL hay API thì sửa `docs/CONTRACT.md` trong cùng commit và báo người kia.
- Pull trước khi bắt đầu làm, tránh xung đột.

## Không làm
- Không đẩy `.env`, mật khẩu, khóa API (chỉ đẩy `.env.example`).
- Không đẩy file model nặng (`*.pt`, `*.onnx`) và ảnh dữ liệu lớn lên repo thường. Để ngoài, ghi cách tải vào README của module.
- Không `git push --force` lên `main`.

## Lỗi thường gặp
- **Xung đột khi gộp:** mở file có dấu `<<<<<<<`, giữ phần đúng, xóa dấu, rồi `git add` và `git commit`.
- **Lỡ commit nhầm file bí mật:** đổi ngay mật khẩu đó, rồi nhờ mình hướng dẫn xóa khỏi lịch sử.
