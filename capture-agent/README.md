# capture-agent - chụp ảnh từ camera trên máy host

Dịch vụ chạy **trên máy host, không dùng Docker** (truyền USB camera vào container trên
Windows rất rắc rối). Nó mở camera, giữ khung hình mới nhất trong RAM và trả JPEG cho
backend qua `GET /capture` (cổng 8002) — đúng như mục 3 của [docs/CONTRACT.md](../docs/CONTRACT.md).

## Endpoint

| Endpoint | Trả về |
|---|---|
| `GET /capture` | ảnh **JPEG** của khung hình mới nhất. Trả `503` nếu chưa có khung hình hoặc khung hình cũ hơn 5 giây (camera bị rút) |
| `GET /health` | `{"camera_ok":true,"frame_age_s":0.03,"source":"0","source_kind":"camera","frame_size":[1280,720],"stale_after_s":5.0,"last_error":null}` |

`camera_ok=false` sau vài giây khi mất camera (tuổi khung hình vượt `STALE_AFTER_S`).

## Cấu hình (biến môi trường)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `CAMERA_SOURCE` | `0` | `0`, `1`... = số thứ tự camera USB · `http://IP:8080/video` = URL luồng (điện thoại chạy IP Webcam) · `D:\video\mau.mp4` = file video (tự lặp lại) |
| `CAMERA_WIDTH` | `1280` | chiều rộng yêu cầu |
| `CAMERA_HEIGHT` | `720` | chiều cao yêu cầu |
| `CAPTURE_PORT` | `8002` | cổng HTTP |
| `JPEG_QUALITY` | `85` | chất lượng JPEG (0–100) |
| `STALE_AFTER_S` | `5` | tuổi tối đa của khung hình trước khi trả 503 |

Trên Windows camera USB được mở bằng `cv2.CAP_DSHOW` (backend MSMF hay treo/khởi động chậm).

Đã kiểm thử trên Windows 11: Python 3.14 + OpenCV 5.0 (wheel `opencv-python` có cho 3.12 trở lên).

## Chạy trên Windows (PowerShell)

```powershell
cd capture-agent
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# camera USB số 0
python app.py

# hoặc điện thoại chạy IP Webcam (cùng WiFi với laptop)
$env:CAMERA_SOURCE = "http://192.168.1.20:8080/video"; python app.py

# hoặc file video để thử khi chưa có camera (tự lặp lại)
$env:CAMERA_SOURCE = "D:\video\baixe.mp4"; python app.py
```

Nếu PowerShell chặn script: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` rồi kích hoạt lại venv.

## Kiểm thử

**1. Thấy ảnh trên trình duyệt:**

```
http://localhost:8002/capture
```

**2. Health:**

```powershell
curl.exe http://localhost:8002/health
```

Mong đợi: `"camera_ok":true` và `frame_age_s` rất nhỏ (< 0.2).

**3. Rút camera (hoặc tắt IP Webcam):** sau khoảng `STALE_AFTER_S` giây

```json
{"camera_ok": false, "frame_age_s": 5.42, "last_error": "không đọc được khung hình", ...}
```

và `curl.exe -i http://localhost:8002/capture` trả `503`. Cắm lại camera thì agent tự mở lại
sau ~2 giây, `camera_ok` trở về `true` — không cần khởi động lại dịch vụ.

**4. Từ backend trong Docker** (dùng cho giai đoạn 4):

```bash
docker compose exec backend python -c "import urllib.request; print(len(urllib.request.urlopen('http://host.docker.internal:8002/capture').read()))"
```

Nếu bị timeout, mở firewall cho cổng 8002 (PowerShell **admin**):

```powershell
netsh advfirewall firewall add rule name="SmartParking capture 8002" dir=in action=allow protocol=TCP localport=8002
```

## Ghi chú

- Agent bind `0.0.0.0:8002` để container gọi được qua `host.docker.internal`; gọi từ máy host vẫn dùng `localhost`.
- Luồng nền đọc liên tục và **ghi đè** khung hình cũ, `CAP_PROP_BUFFERSIZE=1` → không bị trễ do buffer khi backend gọi thưa.
- Không lưu ảnh ra đĩa; việc lưu `entry_image` là của backend (giai đoạn 4).
- Camera phải dùng được bởi một ứng dụng tại một thời điểm: đóng Zoom/Teams/Camera trước khi chạy.
