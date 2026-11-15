# alpr - nhận diện biển số (giai đoạn 3)

Dịch vụ FastAPI nhận ảnh và trả biển số đã chuẩn hoá theo [docs/CONTRACT.md](../docs/CONTRACT.md).
Chạy trong Docker (cổng 8001), hoặc chạy trực tiếp bằng Python.

## Endpoint

`POST /recognize` (multipart, trường `image`):

```json
{"plate": "30A-12345", "confidence": 0.94, "valid": true, "ms": 180}
```

- Không đọc ra biển hợp lệ: `{"plate": null, "confidence": 0.0, "valid": false, "ms": 150}` — **vẫn HTTP 200**.
- Thêm `?debug=true` để nhận thêm `"raw"` (chữ OCR thô, gồm cả lần thử CLAHE).
- Ảnh hỏng/không giải mã được: HTTP `400`.

## Cấu hình (biến môi trường)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `OCR_ENGINE` | `rapidocr` | `rapidocr` (PP-OCR qua ONNX, nhẹ) hoặc `easyocr` (cần cài thêm + torch) |
| `OCR_USE_CLS` | `0` | `1` = bật model phân loại hướng xoay của RapidOCR (chậm hơn ~25%, chỉ cần khi ảnh bị xoay) |
| `OCR_THREADS` | `4` | số luồng ONNX; đo thực tế 4 luồng nhanh nhất trong Docker Desktop |
| `PLATE_DETECTOR` | `roi` | `roi` = cắt vùng cố định; `yolo` = YOLOv8 (**để sẵn, chưa dùng**) |
| `ROI` | rỗng = toàn ảnh | `"x1,y1,x2,y2"` tỉ lệ 0–1, ví dụ `0.1,0.6,0.9,0.95` |
| `YOLO_WEIGHTS` | rỗng | đường dẫn trọng số khi `PLATE_DETECTOR=yolo` |
| `YOLO_CONF` | `0.25` | ngưỡng tin cậy YOLO |
| `MIN_PLATE_WIDTH` | `480` | vùng biển hẹp hơn thì phóng to trước khi OCR |

## Chạy trong Docker (khuyến nghị)

```bash
docker compose build alpr
docker compose up -d alpr
curl -F image=@anh.jpg http://localhost:8001/recognize
curl -F image=@anh.jpg "http://localhost:8001/recognize?debug=true"
```

Log khởi động có dòng `ALPR khởi động: ocr=rapidocr detector=roi roi=-` và `Warmup xong sau N ms`
(model được nạp một lần, ảnh trắng chạy thử để lần gọi đầu không bị chậm).

## Chạy trực tiếp (không Docker)

```bash
cd alpr
python -m venv .venv && .\.venv\Scripts\Activate.ps1     # Windows PowerShell
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

## Cách hoạt động

1. `detector.py` cắt vùng nghi là biển (`roi`; `yolo` để sẵn cho giai đoạn sau).
2. Vùng rộng < `MIN_PLATE_WIDTH` px được phóng to (tối đa 4×, nội suy CUBIC).
3. `ocr.py` đọc chữ; nhiều đoạn chữ được ghép: 1 dòng nối theo trục x, **2 dòng (xe máy) ghép dòng trên trước, dòng dưới sau theo toạ độ y**.
4. `plate.py` chuẩn hoá: bỏ ký tự ngoài `A-Z0-9`, sửa nhầm theo vị trí (số: `O→0 I→1 B→8 S→5 Z→2`; chữ: `8→B 0→D 5→S`), khớp mẫu ô tô / xe máy, xuất `"30A-12345"`.
5. Bản gốc không ra biển hợp lệ thì thử lại trên bản tăng tương phản **CLAHE**.

## Kiểm thử

```bash
# ảnh biển rõ nét -> phải ra đúng số
curl -F image=@bien-so.jpg http://localhost:8001/recognize

# ảnh trắng / không có biển -> HTTP 200, plate null, valid false
curl -F image=@trang.jpg http://localhost:8001/recognize

# đo thời gian
curl -s -F image=@bien-so.jpg http://localhost:8001/recognize | python -c "import json,sys;print(json.load(sys.stdin)['ms'])"
```

Mục tiêu < 500 ms/ảnh trên CPU (đo bằng trường `ms`). Số đo thực tế trên laptop i5/Ryzen 4 nhân
(Docker Desktop, `OCR_USE_CLS=0`, `OCR_THREADS=4`): ảnh 1280×720 có biển ≈ **320 ms**, ảnh biển
phóng to 900×300 (biển chiếm gần hết khung) ≈ **550 ms** — trường hợp nặng nhất vẫn xấp xỉ ngưỡng.

## Ghi chú

- Vùng ROI mặc định là toàn bộ ảnh: với ảnh chụp cả ô tô, đặt `ROI` quanh biển sẽ nhanh và chính xác hơn.
- `PLATE_DETECTOR=yolo` cần `ultralytics` + trọng số; chưa cài nên sẽ báo lỗi rõ ràng nếu bật.
- Chữ series hợp lệ: `ABCDEFGHKLMNPRSTUVXYZ` (không có I, J, O, Q, W).
