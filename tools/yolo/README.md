# tools/yolo - phát hiện vùng biển số (chuẩn bị quyết định 3/12)

> **Trạng thái:** mới ở mức công cụ, **chưa tích hợp vào dịch vụ `alpr/`**.
> Dịch vụ vẫn chạy `PLATE_DETECTOR=roi` như cũ. Thư mục này chỉ chứa hướng dẫn gán nhãn,
> script huấn luyện và script đo so sánh, để nhóm có số liệu trước ngày 3/12.

- `train_plate.py` - huấn luyện YOLOv8n một lớp `plate`, chạy được trên Google Colab, xuất `best.pt`.
- `eval_detector.py` - trên cùng bộ ảnh mẫu, so sánh **ROI cố định** và **YOLO**: tỉ lệ tìm được
  biển, thời gian, tỉ lệ ALPR đọc đúng với vùng cắt của mỗi bên.

---

## 1. Quy ước gán nhãn (một lớp duy nhất: `plate`)

Chỉ có **một lớp, id `0`, tên `plate`**. Không tách "biển vuông", "biển dài", "ký tự".

| Quy tắc | Lý do |
|---|---|
| **1 box cho cả cụm biển**, kể cả biển 2 dòng (ô tô) hay 2 dòng xe máy | OCR tự ghép dòng; tách 2 box sẽ làm vùng cắt cụt |
| Box **sát viền biển**, không cắt mất ký tự, không lấn sang đèn/logo/cản | Box rộng làm OCR đọc thêm chữ rác |
| Ảnh có nhiều biển (bãi xe): **mỗi biển một box** | Detection phải trả nhiều vùng |
| Ảnh **không có biển**: không tạo file nhãn (hoặc file rỗng) | Dùng để đo báo nhầm (false positive) |
| Bỏ ảnh mờ/loá mà **mắt người không đọc được** | Nhãn sai sẽ làm model học sai |
| Biển bị che một phần: **vẫn box phần nhìn thấy**, ghi `note=che` nếu muốn lọc sau | |

Định dạng mỗi dòng trong file nhãn YOLO (toạ độ chuẩn hoá 0-1, tâm box):

```
0 0.512 0.640 0.180 0.070
```

`class cx cy w h` - `class` luôn là `0`.

## 2. Cách gán nhãn

### 2.1 LabelImg (offline, dùng được trên Windows)

```bash
pip install labelImg
labelImg dataset/images/train dataset/labels/train classes.txt
```

- `classes.txt` chỉ một dòng: `plate`.
- Trong LabelImg: nút **PascalVOC** đổi thành **YOLO** (góc trái dưới) trước khi vẽ.
- `W` vẽ box, `Ctrl+S` lưu, `D` sang ảnh tiếp, `A` lùi.
- Mặc định LabelImg lưu cạnh ảnh; nếu muốn lưu đúng `labels/...`, dùng **Change Save Dir**
  trỏ tới thư mục nhãn tương ứng.
- Windows: đường dẫn có dấu cách thì đặt trong ngoặc kép.

Kiểm tra nhanh sau khi gán: mỗi ảnh phải có file `.txt` cùng tên, và

```bash
python - <<'PY'
from pathlib import Path
bad = [p for p in Path("dataset/labels/train").glob("*.txt")
       if p.read_text().strip() and any(len(l.split()) != 5 for l in p.read_text().splitlines())]
print("file nhãn sai định dạng:", bad or "không có")
PY
```

### 2.2 Roboflow (nhanh, có chia tập và augment)

1. **Create New Project** → *Object Detection* → tên tuỳ ý.
2. Thêm class `plate` (duy nhất).
3. Upload ảnh (kéo cả thư mục).
4. **Annotate**: box sát viền biển như mục 1.
5. **Split**: 80% train / 10% valid / 10% test (hoặc 85/15 nếu ít ảnh).
6. **Generate** → chọn *Resize* 640 (hoặc 960 nếu biển nhỏ), augment nhẹ
   (brightness ±15%, blur ≤1px) → **Export** → **YOLOv8** → tải zip.
7. Giải nén, đổi `data.yaml`: `names: {0: plate}`, `path` trỏ đúng thư mục dataset.

Roboflow tự sinh `train/`, `valid/`, `test/` và `data.yaml`; vẫn dùng được với
`train_plate.py --data .../data.yaml`.

### 2.3 Số lượng và độ đa dạng

| Tập | Số ảnh tối thiểu | Khuyến nghị |
|---|---|---|
| train | 200 | 300-500 |
| val | 50 | 60-100 |
| test (tuỳ chọn) | 20 | 30-50 |

Cần phủ các điều kiện (ghi vào `note` khi thu bằng `tools/collect_samples.py`):
ánh sáng (`sang`/`toi`/`nguoc_sang`), góc (`thang`/`lech`), khoảng cách 2-5 m,
ô tô và xe máy, biển bẩn/mờ/nhàu, nền đông xe. **Không** để ảnh cùng một phiên
quay (cùng xe, cùng giây) nằm ở cả train và val - model sẽ học thuộc.

## 3. Cấu trúc thư mục và `dataset.yaml`

```
dataset/
├── images/
│   ├── train/  train_0001.jpg ...
│   └── val/    val_0001.jpg ...
├── labels/
│   ├── train/  train_0001.txt ...
│   └── val/    val_0001.txt ...
└── dataset.yaml
```

```yaml
# dataset.yaml
path: /content/drive/MyDrive/plate_dataset   # thư mục gốc (tuyệt đối trên Colab)
train: images/train
val: images/val
names:
  0: plate
```

- `path` có thể bỏ nếu `train`/`val` là đường dẫn tuyệt đối; nếu để tương đối thì
  Ultralytics tính **theo vị trí file yaml**, không theo thư mục đang đứng.
- Tên file nhãn phải trùng tên ảnh (khác đuôi): `train_0001.jpg` ↔ `train_0001.txt`.
- Ảnh không có biển: tạo file `.txt` rỗng (0 byte) để Ultralytics coi là ảnh nền.

## 4. Huấn luyện

### 4.1 Google Colab (khuyến nghị)

1. Runtime → **Change runtime type** → **T4 GPU**.
2. Mount Drive và cài gói:

```python
from google.colab import drive
drive.mount('/content/drive')
!pip install -q ultralytics
```

3. Đưa dataset lên Drive (ví dụ `/content/drive/MyDrive/plate_dataset/`) và sửa `path`
   trong `dataset.yaml` đúng thư mục đó.
4. Train (chạy từ thư mục gốc repo đã upload, hoặc trỏ thẳng đường dẫn script):

```python
!python tools/yolo/train_plate.py \
  --data /content/drive/MyDrive/plate_dataset/dataset.yaml \
  --epochs 100 --imgsz 640 --batch 16 \
  --out /content/drive/MyDrive/plate_dataset/best.pt
```

5. Kết quả: `best.pt` ở đường dẫn `--out` (mặc định `best.pt` cạnh script), log
   `runs/plate_detect/train/` gồm `results.csv`, `results.png`, `confusion_matrix.png`.

Ước lượng thời gian (YOLOv8n, 400 ảnh, 100 epochs, T4): **20-40 phút**. Nếu Colab ngắt
giữa chừng, chạy lại với `--epochs` lớn hơn và `--resume` (truyền `last.pt` qua `--model`).

### 4.2 Chạy trên máy Windows (không GPU vẫn được, chậm)

```bash
pip install ultralytics
python tools/yolo/train_plate.py --data dataset/dataset.yaml --epochs 30 --batch 8 --workers 0
```

`--workers 0` tránh lỗi đa tiến trình trên Windows; không có GPU thì 30 epochs với 300 ảnh
mất khoảng 1-2 giờ (CPU).

### 4.3 Tham số nên quan tâm

| Tham số | Mặc định | Ghi chú |
|---|---|---|
| `--model` | `yolov8n.pt` | tự tải pretrained; đổi `yolov8s.pt` nếu cần chính xác hơn |
| `--epochs` | 100 | 50-150; theo dõi `mAP50-95` có còn tăng |
| `--imgsz` | 640 | biển nhỏ trong khung 1280×720 nên thử 960 nếu mAP thấp |
| `--batch` | 16 | T4 16 GB chạy được 32; lỗi OOM thì giảm 8 |
| `--patience` | 20 | dừng sớm khi không cải thiện |
| `--seed` | 0 | giữ cố định để so sánh giữa các lần chạy |

## 5. Kiểm tra `best.pt` sau khi train

```bash
yolo predict model=best.pt source=dataset/images/val imgsz=640 conf=0.25 save=True
```

Mở `runs/plate_detect/train/results.png`: `mAP50` nên ≥ 0,95 trên val sạch (thực tế
0,90-0,97); nếu thấp hơn, xem `confusion_matrix.png` và các ảnh bị bỏ sót.

## 6. So sánh ROI cố định và YOLO trên bộ ảnh mẫu

Script gửi **đúng vùng cắt của mỗi bên** sang `POST /recognize` của dịch vụ ALPR đang chạy,
nên cần service ALPR bật (cổng 8001) và **để `ROI` trống trong `alpr/.env`** - nếu service
còn cắt ROI lần nữa thì vùng cắt của YOLO bị cắt thêm, số liệu sai.

Chuẩn bị (dùng lại bộ ảnh đã có cho `bench_alpr.py`):

```
anh-mau/
├── labels.csv     # filename,plate,note   (plate để trống nếu ảnh không có biển)
├── boxes.csv      # tuỳ chọn: filename,x1,y1,x2,y2  (toạ độ pixel của biển)
└── 001.jpg ...
```

`boxes.csv` là **nhãn kiểm chứng** để tính đúng "tỉ lệ tìm được biển". Không có file này,
script vẫn chạy nhưng dùng cách đếm thay thế (xem mục 6.2).

```bash
# ROI đang dùng trong service (ví dụ 0.10,0.60,0.90,0.95); bỏ trống = toàn ảnh
python tools/yolo/eval_detector.py --dir anh-mau --weights best.pt \
  --roi 0.10,0.60,0.90,0.95 --url http://localhost:8001
```

Kết quả in bảng markdown và ghi 2 file CSV:

| Cột | Nghĩa |
|---|---|
| `Tìm thấy biển` | ROI: ≥ 90% diện tích box biển thật (từ `boxes.csv`) nằm trong ROI; YOLO: có box với `conf ≥ --conf` và IoU ≥ 0,5 với box thật |
| `p50/p95 cắt (ms)` | thời gian tạo vùng cắt: cắt + phóng to tới `--min-plate-width` (ROI) hoặc cắt + suy luận YOLO + phóng to (YOLO), đo bằng `perf_counter` |
| `ALPR đúng` | biển service trả về khớp nhãn `plate` (bỏ `-`/`.`, không phân biệt hoa thường) |
| `p50/p95 tổng (ms)` | thời gian cắt + gọi `/recognize` (đo phía client, gồm cả mạng nội bộ) |

### 6.1 Cách đọc kết quả

- **ROI thắng** khi biển luôn nằm trong vùng cố định (camera, khung hình cố định) và
  tỉ lệ đọc đúng tương đương: đơn giản hơn, không cần model, không tốn thêm thời gian.
- **YOLO thắng** khi khung hình thay đổi (xe to/nhỏ, lệch trái/phải, nhiều xe): ROI cố định
  sẽ cắt cụt biển, còn YOLO vẫn tìm đúng.
- So sánh `p50 cắt` + `p50 tổng` với ngưỡng 500 ms của dự án (`docs/CONTRACT.md` không đặt
  ngưỡng, `alpr/README.md` lấy 500 ms làm mục tiêu).
- Chênh lệch **≥ 1 ảnh** trên tập nhỏ chỉ là nhiễu; cần ≥ 60 ảnh mới đủ để kết luận.

### 6.2 Khi không có `boxes.csv`

Script in thêm ghi chú `(proxy)`: ROI luôn có 1 vùng cắt nên "tìm thấy" = 100% theo nghĩa
"luôn có gì đó để OCR", còn YOLO "tìm thấy" = có ít nhất một box. Cách này **không** đo được
ROI có cắt đúng biển hay không - muốn kết luận chắc chắn thì phải có `boxes.csv`.

## 7. Giả định

- Ảnh mẫu chụp đúng bố cục demo (camera cố định, biển hướng về camera); kết quả không suy
  rộng cho góc chụp khác.
- Chỉ một lớp `plate`; biển 2 dòng là một box (khớp cách `alpr/app/ocr.py` ghép dòng).
- `MIN_PLATE_WIDTH` mặc định 480 px và cách phóng to tối đa 4× được **sao chép** từ
  `alpr/app/main.py` (`--min-plate-width` để đổi); script không import code của service.
- Dịch vụ ALPR đang chạy với `OCR_ENGINE` mặc định (`rapidocr`); đổi engine sẽ đổi tỉ lệ đọc đúng.
- Thời gian đo trên máy chạy script + service; không phải số đo trên thiết bị biên.
- `train_plate.py` dùng pretrained `yolov8n.pt` tải từ Ultralytics, cần Internet lần đầu.

## 8. Phần chưa kiểm thử

- **Chưa cài `ultralytics`/`torch`** trong repo này và **chưa chạy train thật**: máy hiện tại
  không có GPU, chưa có dataset. Script mới được kiểm tra cú pháp, `--help`, và luồng báo lỗi
  khi thiếu gói.
- **Chưa có dataset thật** nên chưa có `best.pt`, chưa có mAP thực tế.
- **Chưa chạy `eval_detector.py` với YOLO thật** (cần `best.pt`); luồng ROI + gọi `/recognize`
  và phần đọc nhãn/CSV đã chạy thử bằng model giả + ALPR giả.
- Chưa đo trên ảnh chụp từ camera của nhóm; chưa so sánh với ROI thật đang cấu hình trong
  `alpr/.env`.
- **Chưa tích hợp vào `alpr/`** (đúng phạm vi yêu cầu): `PLATE_DETECTOR=yolo` trong service
  vẫn là đường dẫn sẵn có nhưng chưa được dùng/kiểm chứng trong đợt này.

## 9. Lệnh chạy thử và kết quả mong đợi

```bash
# 1) kiểm tra script (không cần GPU)
python tools/yolo/train_plate.py --help
python tools/yolo/eval_detector.py --help

# 2) train trên Colab (sau khi có dataset)
python tools/yolo/train_plate.py --data /content/drive/MyDrive/plate_dataset/dataset.yaml \
  --epochs 100 --imgsz 640 --batch 16 --out /content/drive/MyDrive/plate_dataset/best.pt

# 3) so sánh (ALPR đang chạy, ROI trống trong alpr/.env)
python tools/yolo/eval_detector.py --dir anh-mau --weights best.pt \
  --roi 0.10,0.60,0.90,0.95
```

Mong đợi:

- `train_plate.py` in tiến trình từng epoch, kết thúc bằng `mAP50`, `mAP50-95` và đường dẫn
  `best.pt`; log nằm trong `runs/plate_detect/train/`.
- `eval_detector.py` in bảng 2 dòng (ROI, YOLO) kèm `Tìm thấy biển`, `p50/p95 cắt`,
  `ALPR đúng`, `p50/p95 tổng`, ghi `results/yolo_detector_eval.csv` và
  `results/yolo_detector_summary.csv`.
- Thiếu `ultralytics`: script dừng với thông báo `pip install ultralytics`, mã thoát 2.
